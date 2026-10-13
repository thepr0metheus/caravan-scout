"""Autostart: the cells this machine starts by itself when it boots."""
from __future__ import annotations

import time
from typing import Any

from caravan_scout.errors import AppError


class Autostart:
    """The cells this machine starts by itself when it boots.

    The controller did it for its own cells with `systemctl enable
    lama-cell@<port>`; a scout's cells had nothing, and a reboot left them
    down. (The controller's machine runs its cells through its scout since
    the controller's step 6.8, and those units went in 6.9.) An entry keeps the start request the controller last sent for its
    port, so the scout starts the cell with no controller at hand — a model
    read in place or cached needs none.

    At boot, not at every scout start: the scout also restarts on an update,
    and starting its autostart cells then would bring back a cell the
    operator had stopped. The machine's boot id tells the two apart
    (Machine.boot_id): the first scout start in a boot starts the cells and
    writes the boot down, later ones leave them alone — as systemd's enable
    does. A machine that will not say which boot it is gets no autostart,
    and the log says why: a surprise start is worse than a missing one.

    Kept in state.json — "autostart" ({port: {payload, savedAt}}) and
    "autostartBoot" (the boot it last ran in) — under the state's lock.
    """

    def __init__(self, state, cells, machine):
        self.state = state
        self.cells = cells
        self.machine = machine

    @staticmethod
    def port(value: Any) -> int:
        try:
            port = int(value)
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            raise AppError("port must be a number from 1 to 65535", 400)
        return port

    def _entries(self) -> dict[str, Any]:
        entries = self.state.get("autostart")
        return entries if isinstance(entries, dict) else {}

    def ports(self) -> list[int]:
        return sorted(int(key) for key in self._entries() if str(key).isdigit())

    def requests(self) -> dict[str, Any]:
        """The kept start requests by port, as a controller keeps a cell (2.26): its settings
        (`config`, whole — an empty value is a flag taken off), the model it names (`modelPath`) and
        whether its models stay cached (`cacheModels`).

        The scout drops a stopped cell from its report, and a controller shows a stopped cell only from
        a record of its own. A controller that did not start the cell — one moved to another machine
        (2026-10-12: every cell of a machine went off its board as it was stopped) — had none, so the
        cell was gone with its switch. From these it keeps a record of each it lacks. An entry without
        settings is no request, and is left out."""
        out: dict[str, Any] = {}
        for key, entry in self._entries().items():
            payload = entry.get("payload") if isinstance(entry, dict) else None
            if not str(key).isdigit() or not isinstance(payload, dict) or not isinstance(payload.get("config"), dict):
                continue
            out[str(int(key))] = {"config": dict(payload["config"]), "modelPath": str(payload.get("modelPath") or ""),
                                  "cacheModels": bool(payload.get("cacheModels"))}
        return out

    def set(self, port: Any, enabled: Any, payload: Any = None) -> dict[str, Any]:
        """Turn a cell's autostart on (with the request that starts it) or off."""
        port = self.port(port)
        if enabled and (not isinstance(payload, dict) or not payload):
            raise AppError("the start request is required to turn autostart on", 400)
        with self.state.lock:
            entries = dict(self._entries())
            if enabled:
                entries[str(port)] = {"payload": {**payload, "port": port}, "savedAt": int(time.time())}
            else:
                entries.pop(str(port), None)
            self.state["autostart"] = entries
            self.state.save()
        return {"ok": True, "port": port, "autostart": self.ports()}

    def refresh(self, port: Any, payload: Any) -> None:
        """A cell with autostart on was started: keep the request it was started
        with, so the next boot starts what runs now, not what ran then."""
        try:
            port = self.port(port)
        except AppError:
            return
        if not isinstance(payload, dict):
            return
        with self.state.lock:
            entries = dict(self._entries())
            if str(port) not in entries:
                return
            entries[str(port)] = {"payload": {**payload, "port": port}, "savedAt": int(time.time())}
            self.state["autostart"] = entries
            self.state.save()

    def start_all(self) -> list[int]:
        """Start the autostart cells — on the first scout start of a boot only.
        Returns the ports it started."""
        boot = self.machine.boot_id()
        with self.state.lock:
            if not boot:
                print("[autostart] this machine does not say which boot it is — not starting anything")
                return []
            if self.state.get("autostartBoot") == boot:
                return []
            self.state["autostartBoot"] = boot
            self.state.save()
            entries = dict(self._entries())
        started = []
        for key in sorted(entries, key=lambda k: int(k) if str(k).isdigit() else 0):
            if not str(key).isdigit():
                continue
            port = int(key)
            if self.cells.at(port).process.status().get("running"):
                print(f"[autostart] :{port} is running already")
                continue
            try:
                result = self.cells.start(dict((entries[key] or {}).get("payload") or {}))
            except Exception as exc:  # noqa: BLE001 — one cell's failure is not the others'
                print(f"[autostart] :{port} did not start: {exc}")
                continue
            if result.get("ok"):
                started.append(port)
                print(f"[autostart] :{port} started")
            else:
                print(f"[autostart] :{port} did not start: {result.get('error')}")
        return started
