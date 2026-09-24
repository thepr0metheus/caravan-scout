"""The machine second by second, for the board's charts."""
from __future__ import annotations

import collections
import threading
import time
from pathlib import Path
from typing import Any, Callable


class Telemetry:
    """This machine's cards and processor, sampled every second while a board
    watches it and every ten seconds otherwise; ten minutes kept.

    The controller draws its own machine from a sample a second. A scout's
    machine was drawn from its reports: a GPU reading kept ten seconds, a
    report every few seconds at best — charts ten times coarser, and the
    machine the controller runs on will be a scout's too. The controller
    asks for what is new (`since`) about once a second while a board is
    open; each ask marks the machine watched for 30 s. Unwatched, the
    history thins to one sample in ten seconds instead of stopping, so an
    opened board still has the last ten minutes.

    A sample: {t, gpus: [{index, memUsedMiB, memTotalMiB, utilPct, powerW,
    tempC}], cpuPct, ram: {usedGb, totalGb}} — cpuPct from /proc/stat between
    two samples, as the controller measures its own; without /proc (macOS)
    the one-minute load average stands in, as the scout's reports have it.
    """

    WATCHED_SEC = 1.0
    IDLE_SEC = 10.0
    WATCH_WINDOW = 30.0
    RETENTION = 600
    PROC_STAT = Path("/proc/stat")

    def __init__(self, machine, clock: Callable[[], float] | None = None):
        self.machine = machine
        self.clock = clock
        self._ring: collections.deque[dict[str, Any]] = collections.deque()
        self._lock = threading.Lock()
        self._asked_at = 0.0
        self._cpu_before: tuple[int, int] | None = None

    def now(self) -> float:
        return self.clock() if self.clock else time.time()

    def watched(self) -> bool:
        return self.now() - self._asked_at < self.WATCH_WINDOW

    def cpu_times(self) -> tuple[int, int] | None:
        """(total, idle) jiffies of all cores, or None without /proc/stat."""
        try:
            first = self.PROC_STAT.read_text(encoding="utf-8").splitlines()[0].split()
        except (OSError, IndexError):
            return None
        if not first or first[0] != "cpu":
            return None
        values = [int(v) for v in first[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    def cpu_pct(self, fallback: float | None) -> float | None:
        """The share of the processor busy since the last sample."""
        now = self.cpu_times()
        before, self._cpu_before = self._cpu_before, now
        if now is None:
            return fallback
        if before is None or now[0] <= before[0]:
            return None
        return round(100.0 * (1 - (now[1] - before[1]) / (now[0] - before[0])), 1)

    @staticmethod
    def number(value: Any) -> float | None:
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            return None

    def sample(self) -> dict[str, Any]:
        """Read the machine once and keep it."""
        cpu = self.machine.cpu_ram()
        row = {"t": int(self.now()),
               "gpus": [{"index": int(g["index"]) if str(g.get("index", "")).isdigit() else None,
                         "memUsedMiB": self.number(g.get("memoryUsedMiB")),
                         "memTotalMiB": self.number(g.get("memoryTotalMiB")),
                         "utilPct": self.number(g.get("utilizationGpuPct")),
                         "powerW": self.number(g.get("powerDrawW")),
                         "tempC": self.number(g.get("temperatureC"))}
                        for g in self.machine.nvidia_gpus()],
               "cpuPct": self.cpu_pct(cpu.get("loadPct")),
               "ram": dict(cpu["ram"]) if isinstance(cpu.get("ram"), dict) else None}
        with self._lock:
            if self._ring and self._ring[-1]["t"] == row["t"]:
                self._ring[-1] = row
            else:
                self._ring.append(row)
            while self._ring and self._ring[0]["t"] < row["t"] - self.RETENTION:
                self._ring.popleft()
        return row

    def describe(self) -> dict[str, Any]:
        """How this machine is sampled — said in both reports."""
        return {"watchedSeconds": self.WATCHED_SEC, "idleSeconds": self.IDLE_SEC,
                "retentionSeconds": self.RETENTION}

    def since(self, since: Any = 0) -> dict[str, Any]:
        """The samples newer than `since` (epoch s) — all ten minutes for 0 —
        and the machine is watched for the next 30 s."""
        try:
            after = int(float(since or 0))
        except (TypeError, ValueError):
            after = 0
        self._asked_at = self.now()
        with self._lock:
            rows = [dict(r) for r in self._ring if r["t"] > after]
        return {"ok": True, **self.describe(), "samples": rows}

    def run(self, sleep: Callable[[float], None] = time.sleep) -> None:
        """The loop the scout runs it in (a daemon thread)."""
        while True:
            try:
                self.sample()
            except Exception as exc:  # noqa: BLE001 — one bad reading must not end the history
                print(f"[telemetry] sample failed: {exc}")
            sleep(self.WATCHED_SEC if self.watched() else self.IDLE_SEC)
