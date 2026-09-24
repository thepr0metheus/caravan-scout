"""Watchdog: a crashed cell comes back, as systemd brings the controller's."""
from __future__ import annotations

import time
from typing import Any, Callable


class Watchdog:
    """What `Restart=on-failure` does for the controller's cells, for this
    machine's: a cell that died without being stopped — a non-zero exit, or
    gone while adopted — is launched again the same way 10 s later
    (CellProcess.relaunch), at most 3 times in 10 minutes; then it stays down
    and says why. A clean exit (code 0) is not a crash.

    Each cell carries its crash note (Cell.crash): how many times it has
    crashed since the operator last started it by hand, when, and the
    reason — the 💥 on the board. A start by hand clears it; a stop drops
    the cell and the note with it.

    `tick()` is called every couple of seconds by the scout (start());
    `clock` is a parameter so a test can move time.
    """

    RESTART_SEC = 10
    BURST = 3
    WINDOW_SEC = 600

    def __init__(self, cells, clock: Callable[[], float] = time.time):
        self.cells = cells
        self.clock = clock

    @staticmethod
    def when(ts: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))

    def tick(self) -> None:
        now = self.clock()
        for port, cell in self.cells.all():
            st = cell.process.status()
            with cell.lock:
                note = cell.crash
            if note and note.get("due") is not None:
                if not st.get("running") and now >= note["due"]:
                    self._restart(port, cell, note, now)
                continue
            if st.get("running") or not st.get("crashed") or (note and note.get("gaveUp")):
                continue
            self._crashed(port, cell, st, note, now)

    def _crashed(self, port: int, cell, st: dict[str, Any], note: dict[str, Any] | None, now: float) -> None:
        note = dict(note or {"count": 0, "restarts": []})
        note["count"] += 1
        note["at"] = self.when(now)
        note["reason"] = str(st.get("lastError") or f"exited (code {st.get('exitCode')})")[:300]
        self._schedule(port, note, now)
        with cell.lock:
            cell.crash = note

    def _schedule(self, port: int, note: dict[str, Any], now: float) -> None:
        recent = [t for t in note.get("restarts", []) if now - t < self.WINDOW_SEC]
        note["restarts"] = recent
        if len(recent) >= self.BURST:
            note["gaveUp"] = True
            note["due"] = None
            print(f"[watchdog] :{port} crashed {len(recent) + 1} times in "
                  f"{self.WINDOW_SEC // 60} minutes — not restarting it: {note['reason']}")
        else:
            note["due"] = now + self.RESTART_SEC
            print(f"[watchdog] :{port} crashed ({note['reason']}) — restarting in {self.RESTART_SEC} s")

    def _restart(self, port: int, cell, note: dict[str, Any], now: float) -> None:
        result = cell.process.relaunch()
        note = dict(note)
        note["restarts"] = list(note.get("restarts", [])) + [now]
        note["due"] = None
        if result.get("ok"):
            print(f"[watchdog] :{port} restarted (pid {result.get('pid')})")
        else:
            note["reason"] = str(result.get("error") or "the restart failed")[:300]
            self._schedule(port, note, now)
        with cell.lock:
            cell.crash = note

    @staticmethod
    def public(note: dict[str, Any] | None) -> dict[str, Any] | None:
        """The crash note as the controller reads it, or None."""
        if not note or int(note.get("count") or 0) <= 0:
            return None
        return {"count": int(note["count"]), "at": note.get("at", ""), "reason": note.get("reason", ""),
                **({"gaveUp": True} if note.get("gaveUp") else {})}

    def run(self, interval: float = 2.0, sleep: Callable[[float], None] = time.sleep) -> None:
        """The loop the scout runs it in (a daemon thread)."""
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — one bad tick must not end the watch
                print(f"[watchdog] tick failed: {exc}")
            sleep(interval)
