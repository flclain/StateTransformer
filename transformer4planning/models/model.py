import os
import torch
import numpy as np
from typing import Tuple, Optional, Dict
from transformers import (GPT2Model, GPT2PreTrainedModel, GPT2Config)
from transformers.modeling_outputs import CausalLMOutputWithCrossAttentions
from transformer4planning.libs.mlp import DecoderResCat
from transformer4planning.utils import nuplan_utils 
from transformer4planning.models.decoder.base import TrajectoryDecoder
from transformers.modeling_outputs import CausalLMOutputWithCrossAttentions
from dataclasses import dataclass

@dataclass
class LTMOutput(CausalLMOutputWithCrossAttentions):
    loss: Optional[torch.FloatTensor] = None
    logits: torch.FloatTensor = None
    past_key_values: Optional[Tuple[Tuple[torch.FloatTensor]]] = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None
    cross_attentions: Optional[Tuple[torch.FloatTensor]] = None
    loss_items: Optional[Dict[str, torch.FloatTensor]] = None


class TrajectoryGPTConfig(GPT2Config):
    def update_by_model_args(self, model_args):
        for each_key in model_args.__dict__:
            self.__dict__[each_key] = model_args.__dict__[each_key]


class TrajectoryGPT(GPT2PreTrainedModel):
    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.transformer = GPT2Model(config)
        self.model_args = kwargs["model_args"]
        self.traj_decoder = None
        self.k = int(self.model_args.k)
        self.use_key_points = self.model_args.use_key_points
        self.kp_decoder_type = self.model_args.kp_decoder_type
        self.model_parallel = False
        self.device_map = None
        self.clf_metrics = None
        # Initialize weights and apply final processing
        self.post_init()
        self.build_encoder()
        self.build_decoder()
        if self.model_args.generate_diffusion_dataset_for_key_points_decoder:
            self.save_training_diffusion_feature_dir = os.path.join(self.model_args.diffusion_feature_save_dir,'train/')
            self.save_testing_diffusion_feature_dir  = os.path.join(self.model_args.diffusion_feature_save_dir,'val/')
            self.save_test_diffusion_feature_dir = os.path.join(self.model_args.diffusion_feature_save_dir,'test/')
            if not os.path.exists(self.save_training_diffusion_feature_dir):
                os.makedirs(self.save_training_diffusion_feature_dir)
            if not os.path.exists(self.save_testing_diffusion_feature_dir):
                os.makedirs(self.save_testing_diffusion_feature_dir)
            if not os.path.exists(self.save_test_diffusion_feature_dir):
                os.makedirs(self.save_test_diffusion_feature_dir)
            self.current_idx = 0
            self.gpu_device_count = torch.cuda.device_count()
            # Notice that although we check and create two directories (train/ and test/) here, in the forward method we only save features in eval loops.
            # This is because evaluation is way faster than training (since there are no backward propagation), and after saving features for evaluation, we just change our test set to training set and then run the evaluation loop again.
            # The related code can be found in runner.py at around line 511.
        
    def build_encoder(self):
        if self.model_args.task == "nuplan":
            tokenizer_kwargs = dict(
                dirpath=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tokenizer', 'gpt2-tokenizer'),
                d_embed=self.config.n_embd,
            )
            if "raster" in self.model_args.encoder_type:
                from transformer4planning.models.encoder.nuplan_raster_encoder import NuplanRasterizeEncoder
                cnn_kwargs = dict(
                    d_embed=self.config.n_embd // 2,
                    in_channels=self.model_args.raster_channels,
                    resnet_type=self.model_args.resnet_type, 
                    pretrain=self.model_args.pretrain_encoder
                )
                action_kwargs = dict(
                    d_embed=self.config.n_embd
                )
                self.encoder = NuplanRasterizeEncoder(cnn_kwargs, action_kwargs, tokenizer_kwargs, self.model_args)
            elif "vector" in self.model_args.encoder_type:
                from transformer4planning.models.encoder.pdm_encoder import PDMEncoder
                pdm_kwargs = dict(
                    hidden_dim=self.config.n_embd,
                    centerline_dim=120,
                    history_dim=20
                )
                self.encoder = PDMEncoder(pdm_kwargs, tokenizer_kwargs, self.model_args)
            else:
                raise AttributeError("encoder_type should be either raster or vector")
        elif self.model_args.task == "waymo":
            from transformer4planning.models.encoder.mtr_encoder import WaymoVectorizeEncoder
            from dataset_gen.waymo.config import cfg_from_yaml_file, cfg
            cfg_from_yaml_file(self.model_args.mtr_config_path, cfg)
            action_kwargs = dict(
                d_embed=self.config.n_embd
            )
            tokenizer_kwargs = dict(
                dirpath=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gpt2-tokenizer'),
                d_embed=self.config.n_embd,
                max_token_len=self.model_args.max_token_len,
            ) if self.model_args.token_scenario_tag else None
            self.encoder = WaymoVectorizeEncoder(cfg, action_kwargs, tokenizer_kwargs, self.model_args)
        else:
            raise NotImplementedError

    def build_decoder(self):
        # load pretrained diffusion keypoint decoder
        #TODO: add diffusion decoder trained from scratch
        if self.model_args.task == "waymo":
            raise NotImplementedError("waymo task has not been tested yet")
        self.traj_decoder = TrajectoryDecoder(self.model_args, self.config)
        if self.use_key_points != 'no':
            if self.kp_decoder_type == "diffusion":
                from transformer4planning.models.decoder.diffusion_decoder import KeyPointDiffusionDecoder
                self.key_points_decoder = KeyPointDiffusionDecoder(self.model_args, self.config)
                if self.model_args.key_points_diffusion_decoder_load_from is not None:
                    print(f"Now loading pretrained key_points_diffusion_decoder from {self.model_args.key_points_diffusion_decoder_load_from}.")
                    state_dict = torch.load(self.model_args.key_points_diffusion_decoder_load_from)
                    self.key_points_decoder.model.load_state_dict(state_dict)
                    print("Pretrained keypoint decoder has been loaded!")
                else:
                    print("Now initializing diffusion decoder from scratch. Training will consume lots of time.")
            elif self.kp_decoder_type == "mlp":
                from transformer4planning.models.decoder.base import KeyPointMLPDeocder
                self.key_points_decoder = KeyPointMLPDeocder(self.model_args, self.config)

        
    def _prepare_attention_mask_for_generation(self, input_embeds):
        return torch.ones(input_embeds.shape[:2], dtype=torch.long, device=input_embeds.device)

    def _prepare_position_ids_for_generation(self, attention_mask):
        position_ids = attention_mask.long().cumsum(-1) - 1
        position_ids.masked_fill_(attention_mask == 0, 1)
        return position_ids

    def from_joint_to_marginal(self, hidden_state, info_dict):
        agents_num_per_scenario = info_dict["agents_num_per_scenario"]
        scenario_num, _, _ = hidden_state.shape
        assert len(agents_num_per_scenario) == scenario_num
        hidden_state_marginal = []
        for i in range(scenario_num):
            agents_num = agents_num_per_scenario[i]
            for j in range(agents_num):
                hidden_state_marginal.append(hidden_state[i, j::agents_num, :])
        hidden_state_marginal = torch.stack(hidden_state_marginal)
        return hidden_state_marginal

    def forward(
            self,
            return_dict: Optional[bool] = None,
            **kwargs
    ):
        # gpt non-autoregression version
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        input_embeds, info_dict = self.encoder(is_training=self.training, **kwargs)

        attention_mask = info_dict["input_embeds_mask"] if self.model_args.interaction else None
        
        transformer_outputs = self.transformer(
            inputs_embeds=input_embeds,
            attention_mask=attention_mask,
            return_dict=return_dict,
            # **kwargs
        )
        if self.model_args.generate_diffusion_dataset_for_key_points_decoder:
            current_device_idx = int(str(input_embeds.device)[-1])

        transformer_outputs_hidden_state = transformer_outputs['last_hidden_state']
        
        trajectory_label = info_dict["trajectory_label"]

        loss = torch.tensor(0, dtype=torch.float32, device=transformer_outputs_hidden_state.device)
        traj_loss, traj_logits = self.traj_decoder.compute_traj_loss(transformer_outputs_hidden_state,
                                                                     trajectory_label,
                                                                     info_dict)
        loss += traj_loss
        kp_loss = 0
        if self.use_key_points != 'no':
            if self.model_args.generate_diffusion_dataset_for_key_points_decoder:
                context_length = info_dict.get("context_length", None)
                assert context_length is not None, "context length can not be None"
                if context_length is None: # pdm encoder
                    input_length = info_dict.get("input_length", None)
                
                future_key_points = info_dict["future_key_points"]
                key_points_num = future_key_points.shape[-2]
                scenario_type_len = self.model_args.max_token_len if self.model_args.token_scenario_tag else 0
                # hidden state to predict future kp is different from mlp decoder
                kp_end_index = scenario_type_len + context_length * 2 if context_length is not None \
                            else scenario_type_len + input_length
                save_id = (self.gpu_device_count * self.current_idx + current_device_idx)*key_points_num
                for key_point_idx in range(key_points_num):
                    current_save_id = save_id + key_point_idx
                    torch.save(transformer_outputs_hidden_state[:,kp_end_index-1+key_point_idx:kp_end_index-1+key_point_idx+1,:].detach().cpu(), os.path.join(self.save_testing_diffusion_feature_dir, f'future_key_points_hidden_state_{current_save_id}.pth'), )
                    torch.save(info_dict['future_key_points'][...,key_point_idx:key_point_idx+1,:].detach().cpu(), os.path.join(self.save_testing_diffusion_feature_dir, f'future_key_points_{current_save_id}.pth'), )
                self.current_idx += 1
            if self.model_args.kp_decoder_type == "diffusion":
                assert not self.training, "please train diffusion decoder separately."
                # return a dummy loss&kp_logits here. The real data for computing metrics will be computed in the generate function
                kp_loss = torch.tensor(0.0).to(transformer_outputs_hidden_state.device)
                kp_logits = info_dict["future_key_points"].to(transformer_outputs_hidden_state.device) if self.model_args.predict_yaw else \
                            info_dict["future_key_points"][..., :2].to(transformer_outputs_hidden_state.device)
            else:
                kp_loss, kp_logits = self.key_points_decoder.compute_keypoint_loss(transformer_outputs_hidden_state, info_dict)
                # kp_loss will be 10x larger than traj_loss when converged
            loss += kp_loss
            traj_logits = torch.cat([kp_logits, traj_logits], dim=1)
        loss_items = dict(
            traj_loss=traj_loss,
            kp_loss=kp_loss
        )
        if not return_dict:
            output = (traj_logits,) + transformer_outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return LTMOutput(
            loss=loss,
            logits=traj_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
            cross_attentions=transformer_outputs.cross_attentions,
            loss_items=loss_items
        )

    @torch.no_grad()
    def generate(self, **kwargs) -> torch.FloatTensor:
        """
        For nuplan generation, the input include those nuplan encoder requires; 
        additionally, it also requires: `map_api`, `route_ids`, `ego_pose`, `road_dic`, `idm_reference_global`
        to post process the generated trajectory which are out of route or out of road

        For waymo generation, the input include a `input_dict` and waymo encoder processes it in its 
        forward function.
        """
        # pass the following infos during generate for one sample (non-batch) generate with KP checking
        map_name = kwargs.get("map", None)
        route_ids = kwargs.get("route_ids", None)
        ego_pose = kwargs.get("ego_pose", None)
        road_dic = kwargs.get("road_dic", None)
        idm_reference_global = kwargs.get("idm_reference_global", None)  # WIP, this was not fulled tested
        """
        Used for generate with key points
        """
        input_embeds, info_dict = self.encoder(is_training=False, **kwargs)

        selected_indices = info_dict["selected_indices"]
        pred_length = info_dict["pred_length"]
        trajectory_label = info_dict["trajectory_label"]
        context_length = info_dict.get("context_length", None)
        if context_length is None: # pdm encoder
            input_length = info_dict.get("input_length", None)

        device = input_embeds.device
        batch_size = trajectory_label.shape[0]

        additional_token_num = 0
        additional_token_num += self.model_args.max_token_len if self.model_args.token_scenario_tag else 0
        # additional_token_num += 1 if self.model_args.use_centerline else 0
        kp_start_index = additional_token_num + context_length * 2 if context_length is not None else additional_token_num + input_length
        # Loop for generation with mlp decoder. Generate key points in autoregressive way.
        if self.use_key_points != 'no':
            assert selected_indices is not None and len(selected_indices) > 0, f'{selected_indices} selected_indices is None or empty'
            trajectory_label_dummy = torch.zeros((batch_size, pred_length, 4), device=device)
            if 'specified' in self.use_key_points:
                future_key_points = trajectory_label_dummy[:, selected_indices, :]
            else:
                ar_future_interval = 20
                future_key_points = trajectory_label_dummy[:, ar_future_interval - 1::ar_future_interval, :]
            assert future_key_points.shape[1] > 0, 'future points not enough to sample'
            future_key_embeds_dummy = self.encoder.action_m_embed(future_key_points)
            key_points_num = future_key_points.shape[1]

            if self.model_args.interaction:
                input_embeds = self.from_joint_to_marginal(input_embeds, info_dict)

            input_embeds[:, kp_start_index:kp_start_index + key_points_num, :] = future_key_embeds_dummy
            pred_key_points_during_generate = []
            for i in range(key_points_num):
                input_embeds_current = input_embeds[:, :kp_start_index + i, :]
                attention_mask = torch.ones(input_embeds_current.shape[:2], dtype=torch.long, device=input_embeds.device)
                position_ids = self._prepare_position_ids_for_generation(attention_mask.clone())
                transformer_output = self.transformer(
                    inputs_embeds=input_embeds_current,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                )
                transformer_outputs_hidden_state = transformer_output['last_hidden_state']
                future_key_point_hidden_state = transformer_outputs_hidden_state[:,
                                                kp_start_index + i - 1,
                                                :].reshape(batch_size, 1, -1)

                if self.k > 1:
                    key_points_logit, pred_logits = self.key_points_decoder.generate_keypoints(future_key_point_hidden_state)
                    selected_key_point = key_points_logit.reshape(batch_size, self.k, -1)[torch.arange(batch_size),
                                        pred_logits.argmax(dim=-1).reshape(-1), :].reshape(batch_size, 1, -1)
                    key_points_logit = selected_key_point
                else:
                    key_points_logit, _ = self.key_points_decoder.generate_keypoints(future_key_point_hidden_state)
                pred_key_point = torch.zeros((batch_size, 1, 4), device=device)
                if self.model_args.predict_yaw:
                    pred_key_point[:, 0, :] = key_points_logit[:, 0, :]
                else:
                    pred_key_point[:, 0, :2] = key_points_logit[:, 0, :]

                off_road_checking = False
                if off_road_checking and batch_size == 1 and map_api is not None and route_ids is not None and road_dic is not None:
                    # Check key points with map_api
                    # WARNING: WIP, do not use
                    pred_key_point_global = nuplan_utils.change_coordination(pred_key_point[0, 0, :2].cpu().numpy(),
                                                                ego_pose,
                                                                ego_to_global=True)
                    closest_lane_road_dic = query_current_lane(map_api=map_api, target_point=pred_key_point_global)
                    nearest = closest_lane_road_dic['road_id']
                    nearest_lane = closest_lane_road_dic['lane_id']
                    dist = closest_lane_road_dic['distance_to_road_block']
                    if nearest not in route_ids or dist > 0.5:
                        # off-road, move to nearest lane according to PDMPath
                        dist = nuplan_utils.euclidean_distance(pred_key_point[0, 0, :2].cpu().numpy(), [0, 0])
                        interpolate_point = center_path.interpolate(np.array([dist]))[0]
                        print('test offroad correction: ', pred_key_point[0, 0, :2].cpu().numpy(), interpolate_point)
                        pred_key_point[0, 0, :2] = torch.tensor(interpolate_point, device=pred_key_point.device)

                if idm_reference_global is not None and self.use_key_points == 'specified_backward':
                    # replace last key point with IDM reference
                    ego_state_global = idm_reference_global[selected_indices[i]]
                    idm_reference_lastpt_relative = nuplan_utils.change_coordination(np.array([ego_state_global.rear_axle.x,
                                                                                                ego_state_global.rear_axle.y]),
                                                                                        ego_pose,
                                                                                        ego_to_global=False)
                    print('replace key points with IDM reference, index: ', selected_indices[i], pred_key_point[0, 0, :2], idm_reference_lastpt_relative)  # idm relative has an unusual large negative y value?
                    pred_key_point[0, 0, :2] = torch.tensor(idm_reference_lastpt_relative, device=pred_key_point.device)
                    pred_key_point[0, 0, -1] = nuplan_utils.normalize_angle(ego_state_global.rear_axle.heading - ego_pose[-1])
                key_point_embed = self.encoder.action_m_embed(pred_key_point).reshape(batch_size, 1, -1)  # b, 1, n_embed
                # replace embed at the next position
                input_embeds[:, kp_start_index + i, :] = key_point_embed[:, 0, :]
                if self.model_args.predict_yaw:
                    pred_key_points_during_generate.append(pred_key_point[:, 0, :].unsqueeze(1))
                else:
                    pred_key_points_during_generate.append(pred_key_point[:, 0, :2].unsqueeze(1))
            key_points_logits = torch.cat(pred_key_points_during_generate, dim=1).reshape(batch_size, key_points_num, -1)
        else:
            key_points_logits = None
        # predict the whole trajectory
        if self.model_args.interaction:
            input_embeds = self.encoder.from_marginal_to_joint(input_embeds, info_dict, update_info_dict=False)
            attention_mask = info_dict["input_embeds_mask"]
        else:
            attention_mask = None
        # generate remaining trajectory
        transformer_output = self.transformer(
            inputs_embeds=input_embeds,
            attention_mask=attention_mask,
            position_ids=None,
        )
        transformer_outputs_hidden_state = transformer_output['last_hidden_state']

        if self.model_args.interaction:
            transformer_outputs_hidden_state = self.from_joint_to_marginal(transformer_outputs_hidden_state, info_dict)

        # expected shape for pred trajectory is (b, pred_length, 4)
        if self.traj_decoder is not None:
            _, traj_logits = self.traj_decoder.compute_traj_loss(transformer_outputs_hidden_state,
                                                                 trajectory_label,
                                                                 info_dict)
            if self.model_args.predict_yaw:
                traj_logits = interplate_yaw(traj_logits, mode=self.model_args.postprocess_yaw)
        else:
            traj_logits = trajectory_label_dummy[..., :2]

        if key_points_logits is not None:
            return torch.cat([key_points_logits, traj_logits], dim=1)
        else: # predict trajectory directly
            return traj_logits

def query_current_lane(map_api, target_point):
    """
    Query the current road_block id and lane id given a point on the map with map_api from NuPlan.
    Args:
        map_api: NuPlan's Map Api
        target_point: [x, y, ..] in global coordination
    Returns:
        {
            'road_id': int,
            'lane_id': int,
            'distance_to_road_block': float,
            'distance_to_lane': float
        }
    """
    from nuplan.common.actor_state.state_representation import Point2D
    from nuplan.common.maps.maps_datatypes import SemanticMapLayer
    from nuplan_garage.planning.simulation.planner.pdm_planner.utils.pdm_path import PDMPath
    point2d = Point2D(target_point[0], target_point[1])
    nearest_road_block_id, distance_to_road_block = map_api.get_distance_to_nearest_map_object(
        point=point2d,
        layer=SemanticMapLayer.ROADBLOCK
    )
    nearest_road_blockc_id, distance_to_road_block_c = map_api.get_distance_to_nearest_map_object(
        point=point2d,
        layer=SemanticMapLayer.ROADBLOCK_CONNECTOR
    )
    nearest_lane_id, distance_to_lane = map_api.get_distance_to_nearest_map_object(
        point=point2d,
        layer=SemanticMapLayer.LANE
    )
    nearest_lanec_id, distance_to_lanec = map_api.get_distance_to_nearest_map_object(
        point=point2d,
        layer=SemanticMapLayer.LANE_CONNECTOR
    )
    # check if on route
    if distance_to_road_block < distance_to_road_block_c:
        nearest_road_blockc_id = int(nearest_road_block_id)
        dist_to_road_block = distance_to_road_block
    else:
        nearest_road_blockc_id = int(nearest_road_blockc_id)
        dist_to_road_block = distance_to_road_block_c
    if distance_to_lane < distance_to_lanec:
        nearest_lane = int(nearest_lane_id)
        dist_to_nearest_lane = distance_to_lane
    else:
        nearest_lane = int(nearest_lanec_id)
        dist_to_nearest_lane = distance_to_lanec
    return {
        'road_id': nearest_road_blockc_id,
        'lane_id': nearest_lane,
        'distance_to_road_block': dist_to_road_block,
        'distance_to_lane': dist_to_nearest_lane
    }

def project_point_to_nearest_lane_on_route(road_dic, route_ids, org_point):
    import numpy as np
    points_of_lane = []
    for each_road_id in route_ids:
        each_road_id = int(each_road_id)
        if each_road_id not in road_dic:
            continue
        road_block = road_dic[each_road_id]
        lanes_in_block = road_block['lower_level']
        for each_lane in lanes_in_block:
            each_lane = int(each_lane)
            if each_lane not in road_dic:
                continue
            points_of_lane.append(road_dic[each_lane]['xyz'])
    if len(points_of_lane) <= 1:
        print('Warning: No lane found in route, return original point.')
        return org_point
    points_np = np.vstack(points_of_lane)
    total_points = points_np.shape[0]
    dist_xy = abs(points_np[:, :2] - np.repeat(org_point[np.newaxis, :], total_points, axis=0))
    dist = dist_xy[:, 0] + dist_xy[:, 1]
    minimal_index = np.argmin(dist)
    minimal_point = points_np[minimal_index, :2]
    min_dist = min(dist)
    return minimal_point
    # return minimal_point if min_dist < 10 else org_point

def interplate_yaw(pred_traj, mode, yaw_change_upper_threshold=0.1):
    if mode == "normal":
        return pred_traj
    elif mode == "interplate" or mode == "hybrid":
        # Warning: this function is tested not better than normal mode
        assert False, "Warning: this function is tested not better than normal mode, to be updated in the future"
        # generating yaw angle from relative_traj
        dx = pred_traj[:, 4::5, 0] - pred_traj[:, :-4:5, 0]
        dy = pred_traj[:, 4::5, 1] - pred_traj[:, :-4:5, 1]
        distances = torch.sqrt(dx ** 2 + dy ** 2)
        relative_yaw_angles = torch.where(distances > 0.1, torch.arctan2(dy, dx), 0)
        # accumulate yaw angle
        # relative_yaw_angles = yaw_angles.cumsum()
        relative_yaw_angles_full = relative_yaw_angles.repeat_interleave(5, dim=1)
        if mode == "interplate":
            pred_traj[:, :, -1] = relative_yaw_angles_full
        else:
            pred_traj[:, :, -1] = torch.where(torch.abs(pred_traj[:, :, -1]) > yaw_change_upper_threshold, relative_yaw_angles_full, pred_traj[:, :, -1])
    return pred_traj

def build_models(model_args):
    if 'vector' in model_args.model_name and 'gpt' in model_args.model_name:
        config_p = None
        if not model_args.autoregressive:
            from transformer4planning.models.vector_model import GPTNonAutoRegressiveModelVector, GPTAutoRegressiveModelVector
            ModelCls = GPTNonAutoRegressiveModelVector
            tag = 'Vector GPT nonauto'
        else:
            ModelCls = GPTAutoRegressiveModelVector
            tag = 'Vector GPT auto'
    elif 'gpt' in model_args.model_name:
        # config_p = GPT2Config()
        config_p = TrajectoryGPTConfig()
        config_p.update_by_model_args(model_args)
        if 'gpt-mini' in model_args.model_name:
            """
            Number of parameters: 300k
            """
            config_p.n_layer = 1
            config_p.n_embd = config_p.d_model = 64
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 1
        elif 'gpt-small' in model_args.model_name:
            """
            Number of parameters: 16M
            """
            config_p.n_layer = 4
            config_p.n_embd = config_p.d_model = 256
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 8
        elif 'gpt-medium' in model_args.model_name:
            """
            Number of parameters: 124M
            """
            config_p.n_layer = 12
            config_p.n_embd = config_p.d_model = 768
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 12
        elif 'gpt-large' in model_args.model_name:
            """
            Number of parameters: 1.5B
            """
            config_p.n_layer = 48
            config_p.n_embd = config_p.d_model = 1600
            config_p.n_inner = config_p.n_embd * 4
            config_p.n_head = 25
        else:
            print('Warning: using default GPT2 config')
            config_p.n_layer = model_args.n_layers
            config_p.n_embd = model_args.d_embed
            config_p.n_inner = model_args.d_inner
            config_p.n_head = model_args.n_heads
        config_p.activation_function = model_args.activation_function
        
        if model_args.task == "train_diffusion_decoder":
            from transformer4planning.models.decoder.diffusion_decoder import (KeypointDiffusionModel, T4PTrainDiffWrapper)
            out_features = 4 if model_args.predict_yaw else 2
            diffusion_model = KeypointDiffusionModel(config_p.n_inner,
                                                     config_p.n_embd,
                                                     out_features=out_features,
                                                     key_point_num=1,
                                                     input_feature_seq_lenth=model_args.diffusion_condition_sequence_lenth,
                                                     use_key_points=model_args.use_key_points,
                                                     feat_dim=model_args.key_points_diffusion_decoder_feat_dim,)
            model = T4PTrainDiffWrapper(diffusion_model, num_key_points=model_args.key_points_num, model_args=model_args)
            if model_args.key_points_diffusion_decoder_load_from is not None:
                state_dict = torch.load(model_args.key_points_diffusion_decoder_load_from)
                model.load_state_dict(state_dict)
                print("Pretrained keypoint decoder has been loaded!")
            print("Only diffusion decoder will be trained singlely!")
            return model
        # whole model training
        else:
            ModelCls = TrajectoryGPT
            tag = 'GPTTrajectory'
    else:
        raise ValueError("Model name must choose from ['scratch', 'pretrain'] + ['nonauto-gpt', 'transxl', 'gpt', 'xlnet']!")
    if 'scratch' in model_args.model_name:
        model = ModelCls(config_p, model_args=model_args)
        print('Scratch ' + tag + ' Initialized!')
    elif 'pretrain' in model_args.model_name:
        model = ModelCls.from_pretrained(model_args.model_pretrain_name_or_path, model_args=model_args, config=config_p)
        print('Pretrained ' + tag + 'from {}'.format(model_args.model_pretrain_name_or_path))
        if model_args.key_points_diffusion_decoder_load_from is not None:
                print(f"Now loading pretrained key_points_diffusion_decoder from {model_args.key_points_diffusion_decoder_load_from}.")
                state_dict = torch.load(model_args.key_points_diffusion_decoder_load_from)
                model.key_points_decoder.model.load_state_dict(state_dict)
    elif 'transfer' in model_args.model_name:
        model = ModelCls(config_p, model_args=model_args)
        print('Transfer' + tag + ' from {}'.format(model_args.model_pretrain_name_or_path))
        
    return model
