"""Watchdog: a crashed cell comes back, as systemd brings the controller's."""
from __future__ import annotations

import signal
import time
from typing import Any, Callable


class Watchdog:
    """What `Restart=on-failure` does for the controller's cells, for this
    machine's: a cell that died without being stopped — a non-zero exit, or
    gone while adopted — is launched again the same way 10 s later
    (CellProcess.relaunch), at most 3 times in 10 minutes; then it stays down
    and says why. A clean exit (code 0) is not a crash.

    Each cell carries its crash note (Cell.crash): how many times it has
    crashed since the operator last started it by hand, when, the reason,
    and the last lines of its log — the 💥 on the board, and the lines under
    it on hover. The lines are read when the crash is seen: the relaunch
    moves that log aside. A start by hand clears it; a stop drops
    the cell and the note with it.

    Each crash is also told to the machine's CrashSuspect, with its words:
    crashes soon after a fresh llama.cpp build make the board offer a
    rollback.

    `tick()` is called every couple of seconds by the scout (start());
    `clock` is a parameter so a test can move time.
    """

    RESTART_SEC = 10
    BURST = 3
    WINDOW_SEC = 600
    #: What a SIGKILL that this scout did not send usually is. The process cannot catch it, so its own log says nothing of it —
    #: the last line is merely the last thing it said before it was killed (2026-10-18: a Strata cell killed four times by
    #: systemd-oomd, its card saying "still starting (461 s)").
    KILLED = " (killed from outside — on a machine short of memory that is systemd-oomd or the kernel's OOM killer)"

    def __init__(self, cells, suspect, clock: Callable[[], float] = time.time):
        self.cells = cells
        self.suspect = suspect
        self.clock = clock

    @staticmethod
    def how(st: dict[str, Any]) -> str:
        """How the process ended, in words — the card's and the crash note's
        alike: a signal by its name ("died of SIGSEGV" says what "exited
        (code -11)" hides, and the build's suspect reads it), the exit code,
        or that the code is not known: a process adopted after a scout
        restart is not this scout's child, and its exit code is lost."""
        code = st.get("exitCode")
        if code is None:
            return "ended with an unknown exit code (adopted after a scout restart)"
        if isinstance(code, int) and code < 0:
            try:
                return f"died of {signal.Signals(-code).name}"
            except ValueError:
                pass
        return f"exited (code {code})"

    @classmethod
    def reason(cls, st: dict[str, Any]) -> str:
        """The crash note's reason: what the log says was the cause — or, when the process was ended by SIGKILL, which is not
        a thing it did itself, that first, with the log's last words after it. Any other signal and any exit code: the log's
        line when there is one (a process that aborts or segfaults says why on its way out), else how it ended."""
        cause, how = str(st.get("lastError") or ""), cls.how(st)
        if st.get("exitCode") == -signal.SIGKILL:
            return (how + cls.KILLED + (f" — its last words: {cause}" if cause else ""))[:300]
        return (cause or how)[:300]

    @staticmethod
    def when(ts: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))

    def tick(self) -> None:
        now = self.clock()
        listening = self.cells.machine.listening_ports()
        for port, cell in self.cells.all():
            st = cell.process.status()
            if st.get("running"):
                self._watch(port, cell, listening)
            with cell.lock:
                note = cell.crash
            if note and note.get("due") is not None:
                if not st.get("running") and now >= note["due"]:
                    self._restart(port, cell, note, now)
                continue
            if st.get("running") or not st.get("crashed") or (note and note.get("gaveUp")):
                continue
            self._crashed(port, cell, st, note, now)

    @staticmethod
    def _watch(port: int, cell, listening: set[int] | None) -> None:
        """Note that this run's port has listened: a run that did is a run that got ready. A machine that will not say what
        listens (None) leaves it unknown — the rule of a cell that never gets ready (`_crashed`) then has nothing to go on."""
        if listening is not None:
            cell.listened = bool(cell.listened) or int(port) in listening

    def _crashed(self, port: int, cell, st: dict[str, Any], note: dict[str, Any] | None, now: float) -> None:
        note = dict(note or {"count": 0, "restarts": []})
        note["count"] += 1
        note["at"] = self.when(now)
        how = self.how(st)
        note["reason"] = self.reason(st)
        # A run that listened got ready, and the starts before it were not "never ready"; one that was watched and never
        # listened was a start that did not take. A run nobody could watch changes nothing.
        if cell.listened:
            note["unready"] = 0
        elif cell.listened is False:
            note["unready"] = int(note.get("unready", 0)) + 1
        cell.listened = None
        note["tail"] = cell.process.log_tail()
        self._schedule(port, note, now)
        with cell.lock:
            cell.crash = note
        if st.get("runner") != "prism":
            self.suspect.crashed("\n".join((note["reason"], how, note["tail"])))

    def _schedule(self, port: int, note: dict[str, Any], now: float) -> None:
        recent = [t for t in note.get("restarts", []) if now - t < self.WINDOW_SEC]
        note["restarts"] = recent
        # A start that takes minutes to die never fills the window: Strata's load ends in a kill after 4 to 8 minutes, and the
        # cell was restarted without end (2026-10-18). Starts that all died before the port ever listened are counted
        # on their own, whatever the clock says: after BURST restarts that did not take, the next death is the last.
        never = int(note.get("unready", 0)) > self.BURST
        if len(recent) >= self.BURST or never:
            note["gaveUp"] = True
            note["neverReady"] = never and len(recent) < self.BURST
            note["due"] = None
            if note["neverReady"]:
                print(f"[watchdog] :{port} never got ready in {note['count']} starts — not restarting it: {note['reason']}")
            else:
                print(f"[watchdog] :{port} crashed {len(recent) + 1} times in "
                      f"{self.WINDOW_SEC // 60} minutes — not restarting it: {note['reason']}")
        else:
            note["due"] = now + self.RESTART_SEC
            print(f"[watchdog] :{port} crashed ({note['reason']}) — restarting in {self.RESTART_SEC} s")

    def _restart(self, port: int, cell, note: dict[str, Any], now: float) -> None:
        # Another process of this scout's still runs the port — the cell that
        # was lost and is still loading, or running: the crash was a second
        # copy's, started beside it. Launching a third would load the model
        # once more; the one that runs is held instead (Cells.survivor).
        lost = self.cells.survivor(port)
        if lost:
            print(f"[watchdog] :{port} not restarted: pid {lost} still runs it — holding that one")
            self.cells.adopt_found(port, lost)
            with cell.lock:
                cell.crash = None
            return
        result = cell.process.relaunch()
        cell.listened = None            # a new run: it has not listened yet
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
                **({"tail": note["tail"]} if note.get("tail") else {}),
                **({"gaveUp": True} if note.get("gaveUp") else {}),
                **({"neverReady": True} if note.get("neverReady") else {})}

    def run(self, interval: float = 2.0, sleep: Callable[[float], None] = time.sleep) -> None:
        """The loop the scout runs it in (a daemon thread)."""
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — one bad tick must not end the watch
                print(f"[watchdog] tick failed: {exc}")
            sleep(interval)
