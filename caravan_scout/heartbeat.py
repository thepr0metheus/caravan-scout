"""The scout's heartbeat to its controller."""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

from caravan_scout.errors import AppError


class Heartbeat:
    """The heartbeat to the controller: one beat is the report POSTed to
    <controllerUrl>/api/topology/client-heartbeat; the loop beats forever;
    pairing — which the CONTROLLER does, from its board — points the scout at
    it and beats once to show whether that works; unpairing lets go. Every
    outcome is written to state.json — the scout's page and /api/state read
    it there.
    """

    def __init__(self, config, state, report, cells):
        self.config = config
        self.state = state
        self.report = report
        self.cells = cells

    def record(self, status: dict[str, Any]) -> None:
        with self.state.lock:
            self.state["heartbeat"] = status
            self.state.save()

    def post_json(self, url: str, payload: dict[str, Any], timeout: int = 5,
                  headers: dict[str, str] | None = None) -> dict[str, Any]:
        """POST JSON to the controller, with the configured token unless the
        caller brings its own headers."""
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json",
                     **(self.config.headers() if headers is None else headers)},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {"ok": True}

    def once(self) -> dict[str, Any]:
        """One beat: the report POSTed to the controller; its answer."""
        controller = str(self.config.get("controllerUrl") or "").rstrip("/")
        if not controller:
            raise AppError("controllerUrl is required")
        url = f"{controller}/api/topology/client-heartbeat"
        return self.post_json(url, self.report.heartbeat())

    def loop(self) -> None:
        """Beat forever, writing each outcome down. A scout nobody has paired
        yet has nobody to beat to: it records that it waits, rather than an
        error every minute."""
        while True:
            started = int(time.time())
            if not self.config.get("controllerUrl"):
                status = {"state": "unpaired"}
            else:
                try:
                    result = self.once()
                    status = {"state": "ok", "lastAt": started, "result": result}
                except Exception as exc:
                    status = {"state": "error", "lastAt": started, "error": str(exc)}
            self.record(status)
            # Use a short interval during llama-node startup so the admin UI
            # receives download/loading progress updates in near-real-time.
            startup_phases = {"resolving", "downloading", "loading", "warming"}
            if any(n.get("phase") in startup_phases for n in self.cells.views()):
                interval = 5
            else:
                interval = int(self.config.get("heartbeatIntervalSeconds") or 60)
            time.sleep(interval)

    def takes_new_token(self, raw_url: str, raw_token: str) -> bool:
        """Whether a pairing may bring a fleet token this scout does not hold.

        When the controller's token is rotated, a paired scout still holds the
        old one, and a pairing bringing the new one was checked against the
        old one and refused. The way back was editing config.json by hand on
        the machine. Since 2.1 the controller adds the scout again from its
        board, with its new token, and this is what lets that through.

        A new token is taken only for the SAME controller, and only after that
        controller accepts a heartbeat carrying it: whoever brings it knows
        the controller's current token, which is what the gate asks for.
        Pointing the scout at another controller still needs the token it
        holds.
        """
        token = str(raw_token or "").strip()
        current = str(self.config.get("controllerUrl") or "")
        if not token or not current:
            return False
        try:
            same = self.config.controller_address(raw_url) == self.config.controller_address(current)
        except AppError:
            return False
        if not same:
            return False
        try:
            self.post_json(f"{self.config.controller_address(current)}/api/topology/client-heartbeat",
                           self.report.heartbeat(), headers={"X-Caravan-Token": token})
        except Exception:  # noqa: BLE001 — refused, unreachable or broken: the token is not proven
            return False
        print("[pairing] the controller accepted a fleet token this scout did not hold — taking it")
        return True

    def unpair(self) -> dict[str, Any]:
        """The controller lets go of this machine: the scout forgets its address
        and token and stops beating. Its cells keep running — stopping them
        is a separate request, made before this one when wanted."""
        self.config.unpair()
        self.record({"state": "unpaired"})
        print("[pairing] unpaired: the controller let go of this machine")
        return {"ok": True}

    def pair(self, raw_url: str, raw_token: str = "") -> dict[str, Any]:
        """Point the scout at a controller — the controller calls this when it
        adds the scout: store its address (and token) in config.json and try
        one heartbeat right away, so the controller learns at once whether the
        scout can reach it back."""
        url = self.config.pair(raw_url, raw_token)
        try:
            result = self.once()
            status = {"state": "ok", "lastAt": int(time.time()), "result": result}
        except Exception as exc:
            status = {"state": "error", "lastAt": int(time.time()), "error": str(exc)}
        self.record(status)
        return {"ok": True, "controllerUrl": url, "heartbeat": status}
