"""MemoryLedger: what this machine's memory has left for a cell that keeps its weights in it."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class MemoryLedger:
    """What the machine's memory can still promise.

    Some cells keep their whole model in RAM: Strata holds every expert there
    (37 GB for a model of 68 GB on the disk) and streams the hot ones to the
    card. The card has its own check (CellStart.short_of_vram); the RAM had
    none, and a second such cell — or the same one started again beside the
    first (2026-10-14) — loaded its model next to the first while the first
    was still loading, each growing toward the machine's 60 GB: the machine
    swapped, the OOM killer took the scout, and the board hung.

    A cell that keeps its weights in RAM says how much (`ram`, sent by the
    controller with the start: reserveMiB, who, why). Two things have to hold:
    what the kernel says is available now covers it, with HEADROOM left for
    the system; and what the cells that run and have promised memory, and it
    itself, promised — loaded yet or not — fits in BUDGET of the machine's
    memory. The second is the one that sees a cell still loading: its memory
    has not arrived, its promise has.
    """

    #: Left for the system and the other cells' own growth: never promised.
    HEADROOM_MIB = 2048
    #: The part of the machine's memory all the promises together may be.
    BUDGET = 0.90
    MEMINFO = Path("/proc/meminfo")    # a class attribute so a test can stand in for it

    def __init__(self, cells):
        self.cells = cells

    @classmethod
    def meminfo(cls) -> dict[str, float]:
        """MemTotal and MemAvailable of /proc/meminfo, in MiB; {} where it does not say."""
        out: dict[str, float] = {}
        try:
            for line in cls.MEMINFO.read_text().splitlines():
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    out[key] = int(rest.split()[0]) / 1024
        except (OSError, ValueError, IndexError):
            return {}
        return out

    def promised(self, port: int) -> list[tuple[int, float]]:
        """(port, MiB) of every other cell of this scout that is up or on its
        way up and has promised memory."""
        found = []
        for other, cell in self.cells.all():
            if other != port and cell.ram_mib and self.cells.tracking(other):
                found.append((other, float(cell.ram_mib)))
        return found

    def short(self, port: int, want: dict[str, Any]) -> dict[str, Any] | None:
        """The refusal when the memory cannot hold what `want` reserves
        ({reserveMiB, who, why}); None when it can, or when this machine does
        not say what it has (no /proc/meminfo: no refusal, as short_of_vram
        without a card)."""
        try:
            need = float(want.get("reserveMiB") or 0)
        except (TypeError, ValueError):
            return None
        mem = self.meminfo()
        total, available = mem.get("MemTotal"), mem.get("MemAvailable")
        if need <= 0 or not total or available is None:
            return None
        who, why = str(want.get("who") or "the cell"), str(want.get("why") or "")
        held = self.promised(port)
        holders = ", ".join(f":{p}" for p, _mib in held)
        hint = f" — stop {holders} first" if holders else ""
        wants = f"{who} wants {need / 1024:.1f} GiB of memory{f' ({why})' if why else ''}"
        if need > available - self.HEADROOM_MIB:
            return {"ok": False, "error": f"{wants} but only {available / 1024:.1f} GiB is available "
                                          f"(and {self.HEADROOM_MIB / 1024:.0f} GiB stays for the system){hint}"}
        promised = sum(mib for _p, mib in held) + need
        if promised > total * self.BUDGET:
            return {"ok": False, "error": f"{wants}, and with what the cells running here promised "
                                          f"({(promised - need) / 1024:.1f} GiB) that is more than "
                                          f"{self.BUDGET:.0%} of the machine's {total / 1024:.1f} GiB{hint}"}
        return None
