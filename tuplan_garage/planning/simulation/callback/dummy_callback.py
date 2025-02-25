import logging
import pathlib
from concurrent.futures import Future
from typing import List, Optional, Union

from nuplan.common.utils.s3_utils import is_s3_path
from nuplan.planning.scenario_builder.abstract_scenario import AbstractScenario
from nuplan.planning.simulation.callback.abstract_callback import AbstractCallback
from nuplan.planning.simulation.history.simulation_history import SimulationHistory, SimulationHistorySample
from nuplan.planning.simulation.planner.abstract_planner import AbstractPlanner
from nuplan.planning.simulation.simulation_log import SimulationLog
from nuplan.planning.simulation.simulation_setup import SimulationSetup
from nuplan.planning.simulation.trajectory.abstract_trajectory import AbstractTrajectory
from nuplan.planning.utils.multithreading.worker_pool import Task, WorkerPool

logger = logging.getLogger(__name__)


class DummyCallback(AbstractCallback):
    """
    Callback for simulation logging/object serialization to disk.
    """

    def __init__(
        self,
    ):
        """
        Construct simulation log callback.
        :param output_directory: where scenes should be serialized.
        :param simulation_log_dir: Folder where to save simulation logs.
        :param serialization_type: A way to serialize output, options: ["json", "pickle", "msgpack"].
        """
        pass

    @property
    def futures(self) -> List[Future[None]]:
        """
        Returns a list of futures, eg. for the main process to block on.
        :return: any futures generated by running any part of the callback asynchronously.
        """
        return self._futures

    def on_initialization_start(self, setup: SimulationSetup, planner: AbstractPlanner) -> None:
        """
        Create directory at initialization
        :param setup: simulation setup
        :param planner: planner before initialization
        """
        pass

    def on_initialization_end(self, setup: SimulationSetup, planner: AbstractPlanner) -> None:
        """Inherited, see superclass."""
        pass

    def on_step_start(self, setup: SimulationSetup, planner: AbstractPlanner) -> None:
        """Inherited, see superclass."""
        pass

    def on_step_end(self, setup: SimulationSetup, planner: AbstractPlanner, sample: SimulationHistorySample) -> None:
        """Inherited, see superclass."""
        pass

    def on_planner_start(self, setup: SimulationSetup, planner: AbstractPlanner) -> None:
        """Inherited, see superclass."""
        pass

    def on_planner_end(self, setup: SimulationSetup, planner: AbstractPlanner, trajectory: AbstractTrajectory) -> None:
        """Inherited, see superclass."""
        pass

    def on_simulation_start(self, setup: SimulationSetup) -> None:
        """Inherited, see superclass."""
        pass

    def on_simulation_end(self, setup: SimulationSetup, planner: AbstractPlanner, history: SimulationHistory) -> None:
        """
        On reached_end validate that all steps were correctly serialized.
        :param setup: simulation setup.
        :param planner: planner when simulation ends.
        :param history: resulting from simulation.
        """
        pass

    def _get_scenario_folder(self, planner_name: str, scenario: AbstractScenario) -> pathlib.Path:
        """
        Compute scenario folder directory where all files will be stored.
        :param planner_name: planner name.
        :param scenario: for which to compute directory name.
        :return directory path.
        """
        pass