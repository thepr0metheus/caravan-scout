"""Scout: the scout of one machine, put together from its parts."""
from __future__ import annotations

from pathlib import Path

from caravan_scout.autostart import Autostart
from caravan_scout.builds import LlamaBuilds
from caravan_scout.cells import Cells
from caravan_scout.config import ScoutConfig
from caravan_scout.engine_servers import EngineProcs, EngineServers
from caravan_scout.engines import ForeignEngines
from caravan_scout.heartbeat import Heartbeat
from caravan_scout.identity import HostIdentity
from caravan_scout.job import BackgroundJob
from caravan_scout.reconcile import Reconcile
from caravan_scout.driver import DriverFacts
from caravan_scout.driver_packages import DriverPackages
from caravan_scout.entry import Copies, KnownCells, MachineEntry, MachineModels
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
        # cells — who holds the cards' memory besides them; their models
        # loaded and unloaded, their servers started and stopped, from the
        # board (the servers' output next to the state, in engine-logs/).
        self.engines = ForeignEngines(self.machine, self.cells, servers=EngineServers(
            self.state, EngineProcs(self.state.path.parent / "engine-logs")))
        self.watchdog = Watchdog(self.cells, self.suspect)
        # A cell this scout started and lost track of is held again (Reconcile).
        self.reconcile = Reconcile(self.cells)
        # What decides whether the NVIDIA card comes back after a reboot
        # (2.19): the controller warns before the reboot, not after it.
        self.driver = DriverFacts(Machine.run_text)
        # Its packages as apt sees them, and installing one (2.24): the
        # controller's driver panel asks the machine's scout, never its own
        # machine — a controller in a container has none.
        self.driver_packages = DriverPackages(self.driver.secure_boot)
        self.driver_install = BackgroundJob("driver-install", "a driver install is already running")
        # The machine's models by name (2.27): what its entry lists and serves, and what both
        # reports carry (2.28) — one list for the entry and the controller.
        # The cells the controller keeps for this machine (2.37): its answer to a heartbeat says them, so a stopped
        # cell this scout has not started is a model of the machine too.
        self.known_cells = KnownCells(self.state)
        self.machine_models = MachineModels(self.cells, self.autostart, lambda: str(self.config.get("hostId") or ""),
                                            known=self.known_cells)
        self.report = Report(self.config, self.state, self.machine, self.cells, self.builds, self.autostart,
                             self.suspect, self.telemetry, identity=self.identity, engines=self.engines,
                             driver=self.driver, entry_models=self.machine_models)
        self.heartbeat = Heartbeat(self.config, self.state, self.report, self.cells, known=self.known_cells)
        # The machine's entry by OpenAI's API (2.27): its models by name, a request to a running copy.
        self.entry = MachineEntry(self.machine_models,
                                  Copies(lambda port: self.cells.probe.metrics(port).get("requestsProcessing")))

    def start_cells(self) -> None:
        """The cells that start with the machine, and then the cache trimmed to its setting: the cells have asked for their files
        by then, so a trim does not take them. Off the main thread — a slow start does not keep the port closed."""
        self.autostart.start_all()
        try:
            self.cells.keeper.trim()
        except Exception as exc:  # noqa: BLE001 — a trim that fails does not stop a scout
            print(f"[llama-node] cache trim at start failed: {exc}")

