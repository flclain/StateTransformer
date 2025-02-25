from typing import Dict
import torch
from torch import nn
from transformer4planning.models.utils import *
from transformer4planning.models.encoder.base import TrajectoryEncoder
from transformers.utils import logging
logging.set_verbosity_info()
logger = logging.get_logger("transformers")


def normalize_angles(angles):
    return torch.atan2(torch.sin(angles), torch.cos(angles))


class CNNDownSamplingResNet(nn.Module):
    def __init__(self, d_embed, in_channels, resnet_type='resnet18', pretrain=False):
        super(CNNDownSamplingResNet, self).__init__()
        import torchvision.models as models
        if resnet_type == 'resnet18':
            self.cnn = models.resnet18(pretrained=pretrain, num_classes=d_embed)
            cls_feature_dim = 512
        elif resnet_type == 'resnet34':
            self.cnn = models.resnet34(pretrained=pretrain, num_classes=d_embed)
            cls_feature_dim = 512
        elif resnet_type == 'resnet50':
            self.cnn = models.resnet50(pretrained=pretrain, num_classes=d_embed)
            cls_feature_dim = 2048
        elif resnet_type == 'resnet101':
            self.cnn = models.resnet101(pretrained=pretrain, num_classes=d_embed)
            cls_feature_dim = 2048
        elif resnet_type == 'resnet152':
            self.cnn = models.resnet152(pretrained=pretrain, num_classes=d_embed)
            cls_feature_dim = 2048
        else:
            assert False, f'Unknown resnet type: {resnet_type}'
        self.cnn = torch.nn.Sequential(*(list(self.cnn.children())[1:-1]))
        self.layer1 = nn.Sequential(
            nn.Conv2d(in_channels, 64, kernel_size=(7, 7), stride=(2, 2), padding=(3, 3), bias=False)
        )
        self.classifier = nn.Sequential(
            nn.Linear(in_features=cls_feature_dim, out_features=d_embed, bias=True)
        )

    def forward(self, x):
        x = self.layer1(x)
        x = self.cnn(x)
        output = self.classifier(x.squeeze(-1).squeeze(-1))
        return output


from transformers.activations import ACT2FN
class STRMultiModalProjector(nn.Module):
    def __init__(self, config):
        super().__init__()
        projector_hidden_act = 'gelu'
        vision_hidden_size = 768
        model_hidden_size = config.get("d_embed")
        self.linear_1 = nn.Linear(vision_hidden_size, model_hidden_size, bias=True)
        self.act = ACT2FN[projector_hidden_act]
        self.linear_2 = nn.Linear(model_hidden_size, model_hidden_size, bias=True)

    def forward(self, image_features):
        hidden_states = self.linear_1(image_features)
        hidden_states = self.act(hidden_states)
        hidden_states = self.linear_2(hidden_states)
        return hidden_states


class NuplanRasterizeEncoder(TrajectoryEncoder):
    def __init__(self,  config=None):
        super().__init__(config)
        action_kwargs = dict(
            d_embed=self.config.n_embd
        )
        # if 'resnet' in self.config.raster_encoder_type:
        if isinstance(self.config.raster_encoder_type, str) and 'vit' in self.config.raster_encoder_type:
            from transformers import ViTModel, ViTConfig
            vit_config = ViTConfig()
            vit_config.hidden_size = self.config.n_embd // 2
            vit_config.num_channels = self.config.raster_channels
            vit_config.intermediate_size = self.config.vit_intermediate_size  # must be multiplier of 12 (number of the head)
            vit_config.num_attention_heads = self.config.n_head
            vit_config.return_dict = True
            self.image_downsample = ViTModel(vit_config)
            logger.info(f'Building ViT encoder with key points indices of {self.selected_indices}')
        else:
            try:
                cnn_kwargs = dict(
                    d_embed=self.config.n_embd // 2,
                    in_channels=self.config.raster_channels,
                    resnet_type=self.config.resnet_type,
                    pretrain=self.config.pretrain_encoder
                )
            except:
                cnn_kwargs = dict(
                    d_embed=self.config.n_embd // 2,
                    in_channels=self.config.raster_channels,
                    resnet_type=self.config.raster_encoder_type,
                    pretrain=self.config.pretrain_encoder
                )
            self.cnn_downsample = CNNDownSamplingResNet(d_embed=cnn_kwargs.get("d_embed", None),
                                                        in_channels=cnn_kwargs.get("in_channels", None),
                                                        resnet_type=cnn_kwargs.get("resnet_type", "resnet18"),
                                                        pretrain=cnn_kwargs.get("pretrain", False))
            logger.info(f'Building ResNet encoder with key points indices of {self.selected_indices}')

        # separate key point encoder is hard to train with larger models due to sparse signals
        input_dim = 7 if self.config.use_speed else 4
        self.action_m_embed = nn.Sequential(nn.Linear(input_dim, action_kwargs.get("d_embed")), nn.Tanh())
        self.action_m_embed_traj = nn.Sequential(nn.Linear(4, action_kwargs.get("d_embed")), nn.Tanh())

        self.traj_tokenizer = None

        # For key points, only use x, and y
        # currently forcing key point to be 2 dimension, with no speed and no yaw
        # if self.config.separate_kp_encoder:
        if self.use_proposal:
            # self.proposal_m_embed = nn.Sequential(nn.Linear(1, action_kwargs.get("d_embed")), nn.Tanh())  # Deprecated
            # each point has 3 dimensions, x, y, and yaw
            self.proposal_m_embed = nn.Sequential(nn.Linear(self.config.traj_proposal_points_num * 3, action_kwargs.get("d_embed")), nn.Tanh())
            self.proposal_score_embed = nn.Sequential(nn.Linear(int(self.config.use_proposal), action_kwargs.get("d_embed")), nn.Tanh())
        if self.config.use_key_points != 'no':
            self.kps_m_embed = nn.Sequential(nn.Linear(2, action_kwargs.get("d_embed")), nn.Tanh())

        self.image_processor = None
        self.camera_image_encoder = None
        self.image_feature_connector = None
        if config.camera_image_encoder == 'dinov2':
            # WIP
            from transformers import AutoImageProcessor, Dinov2Model
            try:
                self.image_processor = AutoImageProcessor.from_pretrained("facebook/dinov2-base")
                self.camera_image_encoder = Dinov2Model.from_pretrained("facebook/dinov2-base")
            except:
                # using local checkpoints due to the GFW blocking of China
                # self.image_processor = AutoImageProcessor.from_pretrained("/public/MARS/t4p/dinov2", local_files_only=True)
                # self.camera_image_encoder = Dinov2Model.from_pretrained("/public/MARS/t4p/dinov2", local_files_only=True)
                self.image_processor = AutoImageProcessor.from_pretrained("/cephfs/sunq/dinov2", local_files_only=True)
                self.camera_image_encoder = Dinov2Model.from_pretrained("/cephfs/sunq/dinov2", local_files_only=True)
            # self.camera_image_m_embed = nn.Sequential(nn.Linear(257*768, action_kwargs.get("d_embed")), nn.Tanh())
            # self.camera_image_m_embed = nn.Sequential(nn.Linear(768, action_kwargs.get("d_embed")), nn.Tanh())
            # self.camera_image_m_embed = nn.Sequential(nn.Linear(768, action_kwargs.get("d_embed"), bias=False))
            self.camera_image_m_embed = STRMultiModalProjector(action_kwargs)
            for param in self.camera_image_encoder.parameters():
                param.requires_grad = False

    def get_proposal_cluster_embedding(self, info_dict, device, input_embeds, trajectory_label=None):
        # pass score when generate function is called
        candidate_proposal = self.traj_tokenizer.trajs.to(device)[:, :self.config.traj_proposal_points_num, :]
        info_dict['candi_proposal_num'] = candidate_proposal.shape[0]
        assert int(info_dict['candi_proposal_num']) == int(self.config.use_proposal), f'proposal {candidate_proposal.shape[0]}, but got {self.config.use_proposal}'
        # n_candi,80, 3 -> n_candi,240 -> n_candi,256
        candidate_proposal_ = candidate_proposal.reshape(candidate_proposal.shape[0], -1)
        candi_embeds = self.proposal_m_embed(candidate_proposal_)
        # n_candi,256 -> bs,n_candi,256
        bs = input_embeds.shape[0]
        candi_embeds = candi_embeds.unsqueeze(0).repeat(bs, 1, 1)
        input_embeds = torch.cat([input_embeds, candi_embeds], dim=1)  # bs,context_length+n_candi,256
        # use gt score for training
        traj_gt = trajectory_label[:, :, [0, 1]].unsqueeze(1)  # bs,1,seq,2
        expanded_candidate_trajectory = candidate_proposal[:, :, [0, 1]].unsqueeze(0).repeat(bs, 1, 1, 1)  # bs,n_candi,seq,2
        # 计算每条轨迹与目标轨迹对应点之间的差值
        diff = expanded_candidate_trajectory - traj_gt  # bs, n_candi, seq, 2
        # 计算欧式距离
        distances = torch.sqrt(torch.sum(diff ** 2, dim=-1))  # 对最后一个维度求和  bs, n_candi, seq
        distances = torch.mean(distances, dim=-1)  # ADE: bs, n_candi
        info_dict['future_traj_diff'] = distances.to(device)
        # import matplotlib.pyplot as plt
        # for i in range(candidate_proposal.shape[0]):
        #     plt.plot(candidate_proposal[i,:,0].cpu(),candidate_proposal[i,:,1].cpu(),alpha=0.2)
        # index = torch.argmin(future_trajs_diff[0,0,:])
        # plt.plot(candidate_proposal[index,:,0].cpu(),candidate_proposal[index,:,1].cpu(),'r')
        # plt.plot(traj_gt[0,0,:,0].cpu(),traj_gt[0,0,:,1].cpu(),'b')
        # plt.savefig('help.jpg')
        # plt.close()

        # compute gt distance to score and embed
        gt_l2_distance = info_dict['future_traj_diff']  # bs,n_cluster,2
        # mask = gt_l2_distance > 30
        # gt_l2_distance[mask] = float('inf')
        # gt_prob = torch.softmax(-gt_l2_distance, dim=1)
        # if not self.training:
        #     gt_prob = torch.zeros_like(gt_prob)
        # gt_prob_embed = self.proposal_score_embed(gt_prob)  # bs, 256
        # input_embeds = torch.cat([input_embeds,
        #                           gt_prob_embed.unsqueeze(1)], dim=1)  # bs,context_length+n_candi+1,256
        gt_index = torch.argmin(gt_l2_distance, dim=-1)
        traj_cls_gt = self.traj_tokenizer.decode(gt_index)
        zeros_tensor = torch.zeros([traj_cls_gt.shape[0], traj_cls_gt.shape[1], 1]).to(traj_cls_gt.device)
        traj_cls_gt = torch.cat((traj_cls_gt[:, :, :2], zeros_tensor, traj_cls_gt[:, :, 2:]), dim=-1)
        traj_cls_embedding = self.action_m_embed_traj(traj_cls_gt)
        input_embeds = torch.cat([input_embeds, traj_cls_embedding], dim=1)

        return input_embeds, info_dict, candi_embeds

    def forward(self, **kwargs):
        """
        Nuplan raster encoder require inputs:
        `high_res_raster`: torch.Tensor, shape (batch_size, 224, 224, seq)
        `low_res_raster`: torch.Tensor, shape (batch_size, 224, 224, seq)
        `context_actions`: torch.Tensor, shape (batch_size, seq, 4 / 6)
        `trajectory_label`: torch.Tensor, shape (batch_size, seq, 2/4), depend on whether pred yaw value
        `pred_length`: int, the length of prediction trajectory
        `context_length`: int, the length of context actions

        To use camera image encoder, the input should also contain:
        `camera_image`: torch.Tensor, shape (batch_size, 8(cameras), 1080, 1920, 3)
        """
        high_res_raster = kwargs.get("high_res_raster", None)
        low_res_raster = kwargs.get("low_res_raster", None)
        context_actions = kwargs.get("context_actions", None)
        trajectory_label = kwargs.get("trajectory_label", None)
        aug_current = kwargs.get("aug_current", None)

        is_training = kwargs.get("is_training", None)
        assert is_training is not None, "is_training should not be None"
        assert self.training == is_training, "training status should be the same"
        self.augmentation.training = is_training

        # assert trajectory_label is not None, "trajectory_label should not be None"
        if trajectory_label is None:
            trajectory_label = torch.zeros((high_res_raster.shape[0], 80, 4), device=high_res_raster.device)
        device = trajectory_label.device
        _, pred_length = trajectory_label.shape[:2]
        action_seq_length = context_actions.shape[1] if context_actions is not None else -1  # -1 in case of pdm encoder

        # add noise to context actions
        context_actions = self.augmentation.trajectory_linear_augmentation(context_actions, self.config.x_random_walk, self.config.y_random_walk)
        # raster observation encoding & context action ecoding
        action_embeds = self.action_m_embed(context_actions)

        high_res_seq = cat_raster_seq(high_res_raster.permute(0, 3, 2, 1).to(device), action_seq_length, self.config.with_traffic_light)
        low_res_seq = cat_raster_seq(low_res_raster.permute(0, 3, 2, 1).to(device), action_seq_length, self.config.with_traffic_light)
        # casted channel number: 33 - 1 goal, 20 raod types, 3 traffic light, 9 agent types for each time frame
        # context_length: 8, 40 frames / 5
        batch_size, action_seq_length, c, h, w = high_res_seq.shape
        assert c == self.config.raster_channels, "raster channel number should be {}, but got {}".format(self.config.raster_channels, c)

        if self.config.raster_encoder_type == 'vit':
            high_res_embed = self.image_downsample(pixel_values=high_res_seq.to(action_embeds.dtype).reshape(batch_size * action_seq_length, c, h, w)).last_hidden_state[:, 1:, :]
            low_res_embed = self.image_downsample(pixel_values=low_res_seq.to(action_embeds.dtype).reshape(batch_size * action_seq_length, c, h, w)).last_hidden_state[:, 1:, :]
            # batch_size * context_length, 196 (14*14), embed_dim//2
            _, sequence_length, half_embed = high_res_embed.shape
            high_res_embed = high_res_embed.reshape(batch_size, action_seq_length, sequence_length, half_embed)
            low_res_embed = low_res_embed.reshape(batch_size, action_seq_length, sequence_length, half_embed)

            state_embeds = torch.cat((high_res_embed, low_res_embed), dim=-1).to(action_embeds.dtype)  # batch_size, action_seq_length, sequence_length, embed_dim
            n_embed = action_embeds.shape[-1]
            context_length = action_seq_length + action_seq_length * sequence_length
            input_embeds = torch.zeros(
                (batch_size, context_length, n_embed),
                dtype=action_embeds.dtype,
                device=device
            )
            for j in range(action_seq_length):
                input_embeds[:, j * (1 + sequence_length): j * (1 + sequence_length) + sequence_length, :] = state_embeds[:, j, :, :]
            input_embeds[:, sequence_length::1 + sequence_length, :] = action_embeds
        else:
            high_res_embed = self.cnn_downsample(high_res_seq.to(action_embeds.dtype).reshape(batch_size * action_seq_length, c, h, w))
            low_res_embed = self.cnn_downsample(low_res_seq.to(action_embeds.dtype).reshape(batch_size * action_seq_length, c, h, w))
            high_res_embed = high_res_embed.reshape(batch_size, action_seq_length, -1)
            low_res_embed = low_res_embed.reshape(batch_size, action_seq_length, -1)

            state_embeds = torch.cat((high_res_embed, low_res_embed), dim=-1).to(action_embeds.dtype)
            n_embed = action_embeds.shape[-1]
            context_length = action_seq_length * 2
            input_embeds = torch.zeros(
                (batch_size, context_length, n_embed),
                dtype=action_embeds.dtype,
                device=device
            )
            input_embeds[:, ::2, :] = state_embeds  # index: 0, 2, 4, .., 18
            input_embeds[:, 1::2, :] = action_embeds  # index: 1, 3, 5, .., 19

        if self.camera_image_encoder is not None:
            camera_images = kwargs.get("camera_images", None)
            assert camera_images is not None, "camera_image should not be None"
            if self.image_processor is not None:
                _, _, image_width, image_height, image_channels = camera_images.shape
                camera_images = camera_images.reshape(batch_size*8, image_width, image_height, image_channels)
                camera_inputs = self.image_processor(camera_images, return_tensors="pt")
            camera_inputs = camera_inputs.to(device)
            camera_image_feature = self.camera_image_encoder(**camera_inputs).last_hidden_state  # batch_size * 8, 257, 768

            # compress all patches into one hidden state
            # camera_image_feature = camera_image_feature.reshape(batch_size, 8, 257 * 768)
            # camera_image_embed = self.camera_image_m_embed(camera_image_feature)  # batch_size, 8 * 257, n_embed
            # input_embeds = torch.cat([input_embeds, camera_image_embed], dim=1)
            # context_length += 8

            # spead all patches into hidden states
            camera_image_feature = camera_image_feature.reshape(batch_size, 8 * 257, 768)
            camera_image_embed = self.camera_image_m_embed(camera_image_feature)  # batch_size, 8 * 257, n_embed
            input_embeds = torch.cat([input_embeds, camera_image_embed], dim=1)
            context_length += 8 * 257

        info_dict = {
            "trajectory_label": trajectory_label,
            "pred_length": pred_length,
            "context_length": context_length,
            "aug_current": aug_current,
        }

        # add proposal embedding
        if self.use_proposal:
            assert self.config.traj_tokenizer == 'cluster_traj', 'only support cluster_traj for now'
            # inpute_embedc -> bs,context_length+n_candi+1,256  (788->1301 (512+1))
            input_embeds, info_dict, candi_embeds = self.get_proposal_cluster_embedding(info_dict, device, input_embeds,
                                                                                        trajectory_label=trajectory_label)

        # add keypoints encoded embedding
        if self.use_key_points == 'no':
            info_dict['future_key_points'] = None
        else:
            future_key_points = self.select_keypoints(info_dict)
            assert future_key_points.shape[1] != 0, 'future points not enough to sample'
            # expanded_indices = indices.unsqueeze(0).unsqueeze(-1).expand(future_key_points.shape)
            # argument future trajectory
            future_key_points_aug = self.augmentation.trajectory_linear_augmentation(future_key_points.clone(), self.config.arf_x_random_walk, self.config.arf_y_random_walk)  # bs, seq, 4
            future_key_points_aug = future_key_points_aug[:, :, :2]

            if self.config.separate_kp_encoder:
                if self.config.kp_decoder_type == "mlp":
                    # 1380 -> 1380+5=1385
                    future_key_embeds = self.kps_m_embed(future_key_points_aug)
                    input_embeds = torch.cat([input_embeds, future_key_embeds], dim=1)
            else:
                assert False, 'deprecated for clarity, use separate_kp_encoder instead'
            info_dict['future_key_points'] = future_key_points

        # padding the input_embeds with pred_length zeros
        # 1385 -> 1385+80=1465
        input_embeds = torch.cat([input_embeds,
                                  torch.zeros((batch_size, pred_length, n_embed),
                                              device=device,
                                              dtype=action_embeds.dtype)], dim=1)
        info_dict['selected_indices'] = self.selected_indices
        return input_embeds, info_dict


class NuplanRasterizeAutoRegressiveEncoder(NuplanRasterizeEncoder):
    def forward(self, **kwargs):
        high_res_raster = kwargs.get("high_res_raster", None)
        low_res_raster = kwargs.get("low_res_raster", None)
        trajectory = kwargs.get("trajectory_label", None)
        aug_current = kwargs.get("aug_current", None)

        is_training = kwargs.get("is_training", None)
        assert is_training is not None, "is_training should not be None"
        self.augmentation.training = is_training

        assert trajectory is not None, "trajectory should not be None"
        device = trajectory.device
        _, trajectory_length = trajectory.shape[:2]

        assert self.config.x_random_walk == 0 and self.config.y_random_walk == 0, "AutoRegressiveEncoder does not support random walk"
        # assert not self.config.use_speed, "AutoRegressiveEncoder does not support speed, generating speed with autoregression is not reasonable"
        action_embeds = self.action_m_embed(trajectory)

        high_res_seq = high_res_raster.permute(0, 1, 4, 2, 3).to(device)
        low_res_seq = low_res_raster.permute(0, 1, 4, 2, 3).to(device)
        batch_size, raster_seq_length, c, h, w = high_res_seq.shape
        assert c == self.config.raster_channels, "raster channel number should be {}, but got {}".format(self.config.raster_channels, c)

        if self.config.raster_encoder_type == 'vit':
            high_res_embed = self.image_downsample(pixel_values=high_res_seq.to(action_embeds.dtype).reshape(batch_size * raster_seq_length, c, h, w)).last_hidden_state[:, 1:, :]
            low_res_embed = self.image_downsample(pixel_values=low_res_seq.to(action_embeds.dtype).reshape(batch_size * raster_seq_length, c, h, w)).last_hidden_state[:, 1:, :]
            # batch_size * context_length, 196 (14*14), embed_dim//2
            _, sequence_length, half_embed = high_res_embed.shape
            high_res_embed = high_res_embed.reshape(batch_size, raster_seq_length, sequence_length, half_embed)
            low_res_embed = low_res_embed.reshape(batch_size, raster_seq_length, sequence_length, half_embed)

            state_embeds = torch.cat((high_res_embed, low_res_embed), dim=-1).to(action_embeds.dtype)
            n_embed = action_embeds.shape[-1]
            embed_sequence_length = raster_seq_length + raster_seq_length * sequence_length  # each O occupy 196 states
            input_embeds = torch.zeros(
                (batch_size, embed_sequence_length, n_embed),
                dtype=action_embeds.dtype,
                device=device
            )
            for j in range(raster_seq_length):
                # apply raster embedding to the input
                input_embeds[:, j * (1 + sequence_length): j * (1 + sequence_length) + sequence_length, :] = state_embeds[:, j, :, :]
            input_embeds[:, sequence_length::1 + sequence_length, :] = action_embeds
        else:
            assert False, "AutoRegressiveEncoder does not support ResNet encoder"

        assert self.camera_image_encoder is None, "AutoRegressiveEncoder does not support camera image encoder"
        assert not self.use_proposal, "AutoRegressiveEncoder does not support proposal"
        assert self.use_key_points == 'no', "AutoRegressiveEncoder does not support key points"

        info_dict = {
            "trajectory_label": trajectory,
            "context_length": kwargs.get('past_frame_num')[0] * (1 + sequence_length) + 1,  # OAOAO -> A
            "aug_current": aug_current,
            "selected_indices": self.selected_indices,
            "pred_length": kwargs.get('future_frame_num')[0],
            "sequence_length": sequence_length
        }
        return input_embeds, info_dict
