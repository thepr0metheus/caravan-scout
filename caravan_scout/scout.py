"""Scout: the scout of one machine, put together from its parts."""
from __future__ import annotations

from pathlib import Path

from caravan_scout.autostart import Autostart
from caravan_scout.builds import LlamaBuilds
from caravan_scout.cells import Cells
from caravan_scout.config import ScoutConfig
from caravan_scout.engines import ForeignEngines
from caravan_scout.heartbeat import Heartbeat
from caravan_scout.identity import HostIdentity
from caravan_scout.machine import Machine
from caravan_scout.report import Report
from caravan_scout.saved_configs import SavedConfigs
from caravan_scout.state import ScoutState
from caravan_scout.suspect import CrashSuspect
from caravan_scout.telemetry import Telemetry
from caravan_scout.vllm import VllmVenv
from caravan_scout.watchdog import Watchdog


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
        # Before anything reads hostId: the id that stays across renames.
        self.identity = HostIdentity(self.config, self.state)
        self.identity.settle()
        self.machine = Machine(self.config)
        self.cells = Cells(self.machine, self.config, self.state)
        # The cells' model cache, also reached from here: its listing and
        # purge are asked over HTTP.
        self.models = self.cells.models
        self.builds = LlamaBuilds(self.config)
        # The venv the controller's vLLM start line uses ($HOME/vllm-venv);
        # the versions it had are kept next to the state, like the configs.
        self.vllm = VllmVenv(Path.home() / "vllm-venv", self.state.path.parent / "vllm-versions.json")
        self.configs = SavedConfigs(self.state.path.parent / "llama-node-configs")
        self.autostart = Autostart(self.state, self.cells, self.machine)
        self.suspect = CrashSuspect(self.state, self.builds)
        self.telemetry = Telemetry(self.machine)
        # Ollama, LM Studio: model engines on this machine that are not its
        # cells — who holds the cards' memory besides them (read only).
        self.engines = ForeignEngines(self.machine, self.cells)
        self.watchdog = Watchdog(self.cells, self.suspect)
        self.report = Report(self.config, self.state, self.machine, self.cells, self.builds, self.autostart,
                             self.suspect, self.telemetry, identity=self.identity, engines=self.engines)
        self.heartbeat = Heartbeat(self.config, self.state, self.report, self.cells)
