#!/usr/bin/env python3
"""Thin launcher. The systemd/launchd entry `python3 -m caravan_scout.app`
must keep working, so this module stays and just wires the package together.
The code lives in the sibling modules (see docs/architecture.md)."""
from __future__ import annotations

import argparse
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from caravan_scout.http import Api
from caravan_scout.scout import Scout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM Easy Route Agent")
    parser.add_argument("--config", default=os.environ.get("CARAVAN_SCOUT_CONFIG",
                        os.environ.get("LLM_EASY_ROUTE_CONFIG", "config.json")))
    parser.add_argument("--state", default=os.environ.get("CARAVAN_SCOUT_STATE",
                        os.environ.get("LLM_EASY_ROUTE_STATE", "state.json")))
    args = parser.parse_args(argv)

    agent = Scout(Path(args.config).expanduser(), Path(args.state).expanduser())
    agent.cells.adopt_survivors()
    # The cells that start with the machine; off the main thread, so a slow
    # start does not keep the port closed.
    threading.Thread(target=agent.autostart.start_all, daemon=True).start()
    # A crashed cell comes back, as systemd brings the controller's (Watchdog).
    threading.Thread(target=agent.watchdog.run, daemon=True).start()
    # The machine second by second while a board watches it (Telemetry).
    threading.Thread(target=agent.telemetry.run, daemon=True).start()
    # The engines next to the cells (ForeignEngines), rescanned on their own.
    threading.Thread(target=agent.engines.run, daemon=True).start()
    heartbeat = threading.Thread(target=agent.heartbeat.loop, daemon=True)
    heartbeat.start()

    host = str(agent.config.get("listenHost") or "0.0.0.0")
    port = int(agent.config.get("listenPort") or 8092)
    server = ThreadingHTTPServer((host, port), Api(agent).handler())
    print(f"caravan-scout listening on http://{host}:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
