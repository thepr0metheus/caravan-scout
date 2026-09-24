"""What this scout says about its machine."""
from __future__ import annotations

import socket
import sys
import time
from typing import Any

from caravan_scout import __version__ as APP_VERSION


class Report:
    """What this scout says about its machine, in three shapes:

    - public(): everything, for /api/state;
    - heartbeat(): the body of a heartbeat — the same facts under the same
      names;
    - pairing(): what the scout's page shows and the controller reads first
      when it adds the scout — open even behind a token.

    One set of names for all of them: the controller keeps whichever report
    arrived last, and a field only one of them carried was erased by the
    other every minute.
    """

    def __init__(self, config, state, machine, cells, builds, autostart, suspect):
        self.config = config
        self.state = state
        self.machine = machine
        self.cells = cells
        self.builds = builds
        self.autostart = autostart
        self.suspect = suspect

    def public(self) -> dict[str, Any]:
        """Everything this scout reports, for /api/state."""
        gpus = self.machine.gpus()
        compute_apps = self.machine.compute_apps()
        cpu_ram = self.machine.cpu_ram()
        version = self.builds.binary_version()
        # Before the state's lock: the verdict takes it itself.
        suspect = self.suspect.verdict(version)
        with self.state.lock:
            return {
                "service": "caravan-scout",
                # One name in both reports, the heartbeat and /api/state: the
                # controller keeps whichever arrived last, and a field only one
                # of them carries is erased by the other every minute.
                "scoutVersion": APP_VERSION,
                "llamaBinaryVersion": version,
                "llamaBinaryMtime": self.builds.binary_mtime(),
                "llamaUpdate": self.builds.status_slim(),
                # Cells crashing soon after a fresh llama.cpp build (2.6+):
                # the board's banner offers a rollback.
                "llamaSuspect": suspect,
                "host": {
                    "id": self.config.get("hostId"),
                    "name": self.config.get("displayName"),
                    "hostname": socket.gethostname(),
                    "ip": self.machine.address(),
                },
                "controllerUrl": self.config.get("controllerUrl"),
                "gpus": gpus,
                "computeApps": compute_apps,
                "cpu": cpu_ram,
                "platform": sys.platform,
                "heartbeat": self.state.get("heartbeat", {}),
                "llamaNode": self.cells.first_view(),
                "llamaNodes": self.cells.views(),
                # The ports that start with the machine — stopped ones too:
                # the board shows ↟ on a parked cell as well.
                "autostart": self.autostart.ports(),
                "time": int(time.time()),
            }

    def pairing(self) -> dict[str, Any]:
        """What the scout's page shows, and nothing more — open even when a
        fleet token closes the rest. The page read /api/state, which the token
        closes too, so on a scout that had a token the page stayed blank. The
        controller reads this first when it adds the scout: that a scout
        answers at the address, which version, whether it holds a token.

        No controller reply here (the heartbeat's `result` carries the host
        record the controller keeps) and nothing a caller could act on.
        """
        gpus = self.machine.gpus()
        nodes = self.cells.views()
        with self.state.lock:
            beat = dict(self.state.get("heartbeat") or {})
        return {
            "service": "caravan-scout",
            "version": APP_VERSION,
            "hostId": self.config.get("hostId"),
            "hostname": socket.gethostname(),
            "ip": self.machine.address(),
            # What the operator types on the controller: address and this port.
            "port": int(self.config.get("listenPort") or 8092),
            "platform": sys.platform,
            "gpus": [str(g.get("name") or g.get("model") or "GPU") for g in gpus],
            "cells": {"running": sum(1 for n in nodes if n.get("running")), "total": len(nodes)},
            "controllerUrl": self.config.get("controllerUrl") or "",
            "tokenRequired": bool(self.config.token()),
            "heartbeat": {key: beat[key] for key in ("state", "lastAt", "error") if key in beat},
        }

    def heartbeat(self) -> dict[str, Any]:
        """The heartbeat's body: the same facts as public(), under the same
        names, minus what only the scout's own API needs."""
        state = self.public()
        return {
            "host": state["host"],
            "gpus": state.get("gpus", []),
            "computeApps": state.get("computeApps", []),
            "cpu": state.get("cpu", {}),
            "platform": state.get("platform", ""),
            "llamaNode": self.cells.first_view(),
            "llamaNodes": self.cells.views(),
            "llamaBinaryVersion": state.get("llamaBinaryVersion", ""),
            "llamaBinaryMtime": state.get("llamaBinaryMtime", ""),
            # The update job's status rode only /api/state, which the
            # controller reads between heartbeats; each heartbeat then replaced
            # the host record without it, and "building…" blinked on the board.
            "llamaUpdate": state["llamaUpdate"],
            "llamaSuspect": state["llamaSuspect"],
            "scoutVersion": state["scoutVersion"],
            "autostart": state["autostart"],
            "agentUrl": f"http://{state['host']['ip']}:{self.config.get('listenPort')}",
            "time": state["time"],
        }
