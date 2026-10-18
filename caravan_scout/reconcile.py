"""Reconcile: a cell this scout started and no longer holds, found again."""
from __future__ import annotations

import time
from typing import Any, Callable


class Reconcile:
    """What `Cells.adopt_survivors` does once at a start, done again while the
    scout runs.

    A cell can leave this scout's hands without dying: the scout is killed
    while the cell loads (2026-10-14: systemd-oomd killed it in the middle of
    Strata's 37 GB load, and the new scout found nothing listening and forgot
    the cell), a record is lost, a stop is half done. The process lives on —
    holding its port, its card and its memory — and the board, which knows only
    what the scout reports, can neither stop it nor start another: Stop is
    off, and Start runs a second copy beside it.

    A process that carries this scout's mark for a port (HostProcesses.port_of)
    and that no cell of this scout holds is that cell, and it is held again.
    Only once two passes in a row have seen it: a start that has made its
    process and not yet put it in its cell would otherwise be taken from
    itself.

    `tick()` is called every INTERVAL seconds by the scout (run()).
    """

    INTERVAL = 15.0
    #: The passes in a row that must see the same process before it is taken.
    SIGHTINGS = 2

    def __init__(self, cells):
        self.cells = cells
        #: (port, pid) -> the passes in a row that have seen it.
        self.seen: dict[tuple[int, int], int] = {}

    def tick(self) -> list[tuple[int, int]]:
        """One pass: the (port, pid) pairs taken by it."""
        found = self.cells.processes.tagged_cells()
        seen: dict[tuple[int, int], int] = {}
        taken: list[tuple[int, int]] = []
        for port, roots in found.items():
            if self.cells.tracking(port):
                continue
            for pid in roots:
                count = self.seen.get((port, pid), 0) + 1
                if count >= self.SIGHTINGS:
                    print(f"[reconcile] :{port} runs as pid {pid} outside this scout's cells — holding it again")
                    self.cells.adopt_found(port, pid)
                    taken.append((port, pid))
                else:
                    seen[(port, pid)] = count
        self.seen = seen
        return taken

    def run(self, interval: float | None = None, sleep: Callable[[float], Any] = time.sleep) -> None:
        """The loop the scout runs it in (a daemon thread)."""
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — one bad pass must not end the watch
                print(f"[reconcile] pass failed: {exc}")
            sleep(self.INTERVAL if interval is None else interval)
