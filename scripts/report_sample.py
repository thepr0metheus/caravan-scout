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

import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import make_scout, patched  # noqa: E402

import caravan_scout.report as report_module  # noqa: E402
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
    APP = {"gpuUuid": "GPU-00000000-0000-0000-0000-000000000000", "pid": 4242, "usedMiB": 20000}
    CPU = {"loadPct": 12.5, "load1": 1.5, "ncpu": 12, "logicalCores": 12, "availableCores": 12,
           "physicalCores": 6, "ram": {"usedGb": 18.2, "totalGb": 62.7}}
    METRICS = {"promptTps": 812.5, "genTps": 41.3, "requestsProcessing": 1, "ctxMax": 8192, "ctxUsed": 2048}
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
        # board shows as 💥 rides the cell's view.
        running.crash = {"count": 1, "at": "2026-09-24T09:00:00+0000", "reason": "CUDA error: out of memory",
                         "restarts": [self.NOW - 700], "due": None}
        # And it starts with the machine (2.4): its port rides "autostart".
        scout.autostart.set(22001, True, {"modelPath": "models/org/model-q4.gguf", "port": 22001,
                                          "args": ["--port", "22001"], "config": {"PORT": 22001}})
        scout.cells.report(22002, phase="downloading", modelPath="models/org/other-q8.gguf",
                           downloadedBytes=1_000_000, totalBytes=4_000_000, downloadingFile="other-q8.gguf",
                           startedAt=self.NOW - 30)
        machine = {"gpus": lambda: [dict(self.GPU)], "compute_apps": lambda: [dict(self.APP)],
                   "cpu_ram": lambda: json.loads(json.dumps(self.CPU)), "address": lambda: "10.0.0.5",
                   "firewall": lambda port: {"state": "open", "allowedFrom": []}}
        builds = {"binary_version": lambda: "version: 9947 (abc1234)",
                  "binary_mtime": lambda: "2026-09-01T10:00:00", "status_slim": lambda: dict(self.UPDATE)}
        with patched(scout.machine, **machine), patched(scout.builds, **builds), \
                patched(scout.cells.probe, metrics=lambda port: dict(self.METRICS)), \
                patched(CellProcess, pid_alive=staticmethod(lambda pid: pid == 4242)), \
                patched(socket, gethostname=lambda: "box-a.lan"), patched(time, time=lambda: float(self.NOW)), \
                patched(sys, platform="linux"), patched(report_module, APP_VERSION=self.VERSION):
            return {"heartbeat": scout.report.heartbeat(), "state": scout.report.public()}

    def text(self) -> str:
        return json.dumps(self.build(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def current(self) -> bool:
        return self.PATH.exists() and self.PATH.read_text(encoding="utf-8") == self.text()

    def write(self) -> None:
        self.PATH.write_text(self.text(), encoding="utf-8")


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
