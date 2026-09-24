"""Scout: the scout of one machine, put together from its parts."""
from __future__ import annotations

from pathlib import Path

from caravan_scout.autostart import Autostart
from caravan_scout.builds import LlamaBuilds
from caravan_scout.cells import Cells
from caravan_scout.config import ScoutConfig
from caravan_scout.heartbeat import Heartbeat
from caravan_scout.machine import Machine
from caravan_scout.report import Report
from caravan_scout.saved_configs import SavedConfigs
from caravan_scout.state import ScoutState


class Scout:
    """The scout of one machine: who owns what, and nothing else.

    The scout knows its machine only — GPUs, cells, builds. What it once
    said about the agents on it (a fleet registry, OpenClaw configs, routes
    to apply) is gone since 2.0: clients are the controller's records, made
    by hand. Each part below does one job and gets only the parts it needs;
    none of them reaches the others through the scout.
    """

    def __init__(self, config_path: Path, state_path: Path):
        self.config = ScoutConfig(config_path)
        self.state = ScoutState(state_path)
        self.machine = Machine(self.config)
        self.cells = Cells(self.machine, self.config, self.state)
        # The cells' model cache, also reached from here: its listing and
        # purge are asked over HTTP.
        self.models = self.cells.models
        self.builds = LlamaBuilds(self.config)
        self.configs = SavedConfigs(self.state.path.parent / "llama-node-configs")
        self.autostart = Autostart(self.state, self.cells, self.machine)
        self.report = Report(self.config, self.state, self.machine, self.cells, self.builds, self.autostart)
        self.heartbeat = Heartbeat(self.config, self.state, self.report, self.cells)
