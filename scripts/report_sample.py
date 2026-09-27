#!/usr/bin/env python3
"""The report's sample: what this scout says about its machine — the
heartbeat and /api/state — with every probe answered by a fixed fake.

It pins the shape and the names, one file for both sides. The scout's side:
test_scout_report.py checks that the scout still produces exactly
docs/report-sample.json. The controller's side: lama-caravan keeps a
byte-identical copy (scripts/fixtures/scout-report-sample.json) and checks
that it reads every field of it — so a field renamed here, the way
`version` once became `scoutVersion`, reddens a test instead of blinking on
the board.

The cells' views come from the real Cells code over faked processes, so a
renamed node field shows up here too. The version is a marker, not the
package's: the sample changes when the shape does, not on every release.

Run: python3 scripts/report_sample.py          — say whether the file is current
     python3 scripts/report_sample.py --write  — rewrite it
"""
from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import TMP, make_scout, patched  # noqa: E402

import caravan_scout.report as report_module  # noqa: E402
from caravan_scout.engines import LmStudio, Ollama  # noqa: E402
from caravan_scout.process import CellProcess  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class ReportSample:
    """One imagined machine, reported the way a scout reports it."""

    PATH = ROOT / "docs" / "report-sample.json"
    NOW = 1_790_000_000
    VERSION = "X.Y.Z"

    GPU = {"index": "0", "name": "NVIDIA GeForce RTX 3090", "vendor": "nvidia", "driverStatus": "ok",
           "memoryTotalMiB": "24576", "memoryUsedMiB": "20480", "memoryFreeMiB": "4096",
           "utilizationGpuPct": "37", "temperatureC": "61", "powerDrawW": "212.40",
           "uuid": "GPU-00000000-0000-0000-0000-000000000000"}
    APP = {"gpuUuid": "GPU-00000000-0000-0000-0000-000000000000", "pid": 4242, "name": "llama-server",
           "usedMiB": 20000}
    # Its NVIDIA driver as the next boot will meet it (2.19): a new kernel is
    # installed, and all it has of the driver is a DKMS build signed by the
    # machine's own key, which the firmware does not trust — the state that
    # took a machine's card on 2026-09-26.
    DRIVER = {"secureBoot": True, "kernelRunning": "7.0.0-31-generic", "kernelNext": "7.0.0-34-generic",
              "loaded": "610.43.02", "installed": "610.43.02",
              "nextModule": {"path": "/lib/modules/7.0.0-34-generic/updates/dkms/nvidia.ko.zst",
                             "version": "610.43.02", "signer": "linux Secure Boot Module Signature key"},
              "dkmsKey": {"signer": "linux Secure Boot Module Signature key", "enrolled": False},
              "package": "nvidia-driver-610-open"}
    # Ollama's runner on the same card (2.12): its memory is named by its engine.
    OLLAMA_APP = {"gpuUuid": "GPU-00000000-0000-0000-0000-000000000000", "pid": 5151, "name": "ollama",
                  "usedMiB": 3500}
    # What listens and runs besides the cells (2.12): Ollama on the network
    # with its runner, LM Studio on this machine only.
    LISTENERS = {"ok": True, "ports": [
        {"port": 1234, "proc": "LM Studio", "pid": 613, "addrs": ["127.0.0.1"]},
        {"port": 11434, "proc": "", "pid": 0, "addrs": ["0.0.0.0", "[::]"]},
        {"port": 22001, "proc": "llama-server", "pid": 4242, "addrs": ["0.0.0.0"]}]}
    PROCESSES = {5100: {"ppid": 1, "rssKb": 204800, "name": "ollama"},
                 5151: {"ppid": 5100, "rssKb": 1048576, "name": "ollama"},
                 613: {"ppid": 1, "rssKb": 409600, "name": "LM Studio"},
                 4242: {"ppid": 1, "rssKb": 2097152, "name": "llama-server"}}
    # The engines' own answers, by port and path.
    ENGINE_ANSWERS = {
        (11434, "/api/version"): (200, {"version": "0.12.3"}),
        (11434, "/api/ps"): (200, {"models": [
            {"name": "qwen3:8b", "model": "qwen3:8b", "size": 6_591_830_464, "size_vram": 5_333_539_264,
             "context_length": 4096, "expires_at": "2026-09-22T17:00:00+00:00",
             "details": {"format": "gguf", "family": "qwen3", "parameter_size": "8.2B",
                         "quantization_level": "Q4_K_M"}}]}),
        (11434, "/api/tags"): (200, {"models": [
            {"name": "qwen3:8b", "model": "qwen3:8b", "size": 5_225_388_164,
             "details": {"format": "gguf", "family": "qwen3", "parameter_size": "8.2B",
                         "quantization_level": "Q4_K_M"}},
            {"name": "gpt-oss:120b-cloud", "model": "gpt-oss:120b-cloud", "size": 384,
             "remote_host": "https://ollama.com:443", "details": {"format": "", "family": "gptoss",
                                                                 "parameter_size": "116.8B",
                                                                 "quantization_level": "MXFP4"}}]}),
        (1234, "/api/v1/models"): (200, {"models": [
            {"type": "llm", "publisher": "google", "key": "google/gemma-3-4b", "display_name": "Gemma 3 4B",
             "architecture": "gemma3", "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
             "size_bytes": 3_340_000_000, "params_string": "4B", "max_context_length": 131072, "format": "gguf",
             "loaded_instances": [{"id": "google/gemma-3-4b", "config": {"context_length": 8192, "parallel": 4}}]}]}),
    }
    # LM Studio's command line (2.15): the loaded model's idle limit, which
    # its REST list does not say.
    LMS_PS = [{"identifier": "google/gemma-3-4b", "modelKey": "google/gemma-3-4b", "ttlMs": 3_600_000,
               "lastUsedTime": (NOW - 600) * 1000}]
    # Who runs the engines' servers (2.16): LM Studio this scout's user, the
    # Ollama on the network another user — a system service.
    PROCESS_OWNERS = {613: {"uid": 1000, "exe": "/opt/lm-studio/lm-studio", "args": ["lm-studio"], "env": {},
                            "marked": False}}
    CPU = {"loadPct": 12.5, "load1": 1.5, "ncpu": 12, "logicalCores": 12, "availableCores": 12,
           "physicalCores": 6, "ram": {"usedGb": 18.2, "totalGb": 62.7}}
    METRICS = {"promptTps": 812.5, "genTps": 41.3, "requestsProcessing": 1, "ctxMax": 8192, "ctxUsed": 2048}
    # A vLLM cell says its queue, and its rates come from its counters (2.7).
    VLLM_METRICS = {"requestsProcessing": 2, "requestsWaiting": 1, "promptTps": 150.0, "genTps": 40.0}
    UPDATE = {"running": False, "done": True, "rc": 0, "startedAt": NOW - 86_400, "tag": "b9947",
              "lastLine": "llama.cpp b9947 installed"}

    def build(self) -> dict:
        scout = make_scout({"controllerUrl": "http://10.0.0.1:7990", "listenPort": 8092})
        scout.state["heartbeat"] = {"state": "ok", "lastAt": self.NOW - 60}
        running = scout.cells.at(22001)
        running.process.adopt(4242, {"modelPath": "/models/org/model-q4.gguf", "mmprojPath": "", "specPath": "",
                                     "specType": "", "port": 22001, "gpuLayers": 999, "ctxSize": 8192},
                              started_at=self.NOW - 600)
        scout.cells.report(22001, phase="running", error="")
        # It crashed once and its watchdog brought it back (2.5): the note the
        # board shows as 💥 rides the cell's view, with the last lines of the
        # crashed run's log under it (2.6).
        running.crash = {"count": 1, "at": "2026-09-24T09:00:00+0000", "reason": "CUDA error: out of memory",
                         "tail": "0.01.200.000 I srv  load_model: loading model\n"
                                 "0.01.900.000 E ggml_cuda: CUDA error: out of memory",
                         "restarts": [self.NOW - 700], "due": None}
        # And it starts with the machine (2.4): its port rides "autostart".
        scout.autostart.set(22001, True, {"modelPath": "models/org/model-q4.gguf", "port": 22001,
                                          "args": ["--port", "22001"], "config": {"PORT": 22001}})
        vllm = scout.cells.at(22012)
        vllm.process.adopt(4343, {"modelPath": "", "port": 22012, "cellKind": "command",
                                  "command": "$HOME/vllm-venv/bin/vllm serve org/model --port \"$PORT\""},
                           started_at=self.NOW - 300)
        scout.cells.report(22012, phase="running", error="")
        # Another vLLM cell still installs: its process runs, its port does
        # not listen yet, and it says where the start is (2.7).
        log = TMP / "sample-logs" / "command-cell.22013.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("[caravan] provisioning vLLM venv at $HOME/vllm-venv (first start on this host, several "
                       "minutes)…\nCollecting vllm==0.24.0\n", encoding="utf-8")
        scout.cells.at(22013).process.adopt(4444, {"modelPath": "", "port": 22013, "cellKind": "command",
                                                   "command": "$HOME/vllm-venv/bin/vllm serve org/other"},
                                            log_path=log, started_at=self.NOW - 60)
        scout.cells.report(22013, phase="running", error="")
        scout.cells.report(22002, phase="downloading", modelPath="models/org/other-q8.gguf",
                           downloadedBytes=1_000_000, totalBytes=4_000_000, downloadingFile="other-q8.gguf",
                           startedAt=self.NOW - 30)
        machine = {"gpus": lambda: [dict(self.GPU)],
                   "compute_apps": lambda: [dict(self.APP), dict(self.OLLAMA_APP)],
                   "listeners": lambda: json.loads(json.dumps(self.LISTENERS)),
                   "processes": lambda: {pid: dict(p) for pid, p in self.PROCESSES.items()},
                   "cpu_ram": lambda: json.loads(json.dumps(self.CPU)), "address": lambda: "10.0.0.5",
                   "firewall": lambda port: {"state": "open", "allowedFrom": []},
                   "listening_ports": lambda: {22001, 22012}}
        # Its cells crash since a build made an hour ago (2.6): the board's
        # banner offers the archived build before it.
        built_at = self.NOW - 3600
        scout.state["llamaSuspect"] = {"key": f"abc1234:{built_at}", "commit": "abc1234", "builtAt": built_at,
                                       "firstSeenAt": self.NOW - 900, "lastSeenAt": self.NOW - 120, "crashes15m": 3}
        archive = {"ok": True, "builds": [
            {"id": "20260924-090000-abc1234", "commit": "abc1234", "version": "version: 9947 (abc1234)",
             "builtAt": built_at, "sizeMb": 88},
            {"id": "20260920-090000-def5678", "commit": "def5678", "version": "version: 9900 (def5678)",
             "builtAt": self.NOW - 400_000, "sizeMb": 87}]}
        builds = {"binary_version": lambda: "version: 9947 (abc1234)", "binary_built_at": lambda: built_at,
                  "binary_mtime": lambda: "2026-09-01T10:00:00", "status_slim": lambda: dict(self.UPDATE),
                  "archive": lambda: archive}
        with patched(scout.machine, **machine), patched(scout.builds, **builds), \
                patched(scout.driver, facts=lambda: json.loads(json.dumps(self.DRIVER))), \
                patched(scout.cells.probe, metrics=lambda port: dict(self.VLLM_METRICS if port == 22012
                                                                      else self.METRICS if port == 22001
                                                                      else {})), \
                patched(CellProcess, pid_alive=staticmethod(lambda pid: pid in (4242, 4343, 4444))), \
                patched(socket, gethostname=lambda: "box-a.lan"), patched(time, time=lambda: float(self.NOW)), \
                patched(sys, platform="linux"), patched(report_module, APP_VERSION=self.VERSION), \
                patched(scout.engines, ask=lambda port, host="127.0.0.1":
                        (lambda path: self.ENGINE_ANSWERS.get((port, path), (404, None))),
                        kinds=(Ollama(), LmStudio(cli=SampleLms(self.LMS_PS)))), \
                patched(scout.engines.servers, procs=SampleProcs(self.PROCESS_OWNERS)):
            # The engines are scanned by their own thread; here, once, by hand.
            scout.engines.refresh()
            # Acts on them from the board (2.14): one that failed, with the
            # engine's words, and one still under way.
            queued = []
            scout.engines.spawn = queued.append
            scout.engines.call = lambda port, host="127.0.0.1", timeout=None: (
                lambda path, body: (500, {"error": {"message": "the instance is busy"}}, "the instance is busy"))
            with contextlib.redirect_stdout(io.StringIO()):
                scout.engines.act("unload", "lmstudio", 1234, "google/gemma-3-4b")
                queued.pop()()
                scout.engines.act("unload", "ollama", 11434, "qwen3:8b")
                # And a download into Ollama under way (2.17), a fifth of it come.
                scout.engines.pull("ollama", 11434, "qwen3:4b")
                with scout.engines._lock:
                    scout.engines._downloads[("ollama", 11434)].update(doneBytes=500_000_000, totalBytes=2_500_000_000)
            return {"heartbeat": scout.report.heartbeat(), "state": scout.report.public()}

    def text(self) -> str:
        return json.dumps(self.build(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def current(self) -> bool:
        return self.PATH.exists() and self.PATH.read_text(encoding="utf-8") == self.text()

    def write(self) -> None:
        self.PATH.write_text(self.text(), encoding="utf-8")


class SampleProcs:
    """The machine's side of the engines' servers in the sample: the scout
    runs as uid 1000, and the processes it can name are the ones given."""

    def __init__(self, owners):
        self.owners = owners

    @staticmethod
    def uid():
        return 1000

    def info(self, pid):
        return self.owners.get(int(pid))


class SampleLms:
    """LM Studio's command line in the sample: its server runs, and `lms ps
    --json` answers with the loaded model's idle limit; nothing else is
    asked of it."""

    def __init__(self, ps):
        self.ps = ps

    @staticmethod
    def available() -> bool:
        return True

    def __call__(self, args, timeout=None):
        if list(args) == ["server", "status"]:
            return 0, "The server is running on port 1234."
        return (0, json.dumps(self.ps)) if list(args) == ["ps", "--json"] else (1, "not in the sample")


def main(argv: list[str]) -> int:
    sample = ReportSample()
    if "--write" in argv:
        sample.write()
        print(f"written: {sample.PATH.relative_to(ROOT)}")
        return 0
    if sample.current():
        print(f"{sample.PATH.relative_to(ROOT)} is current")
        return 0
    print(f"{sample.PATH.relative_to(ROOT)} differs from what the scout reports — "
          f"run with --write, then copy it to the controller's scripts/fixtures/scout-report-sample.json")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
