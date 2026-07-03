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

from caravan_scout.agent import RouteAgent
from caravan_scout.errors import AppError            # noqa: F401  (re-export)
from caravan_scout.http import json_bytes, make_handler  # noqa: F401


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM Easy Route Agent")
    parser.add_argument("--config", default=os.environ.get("CARAVAN_SCOUT_CONFIG",
                        os.environ.get("LLM_EASY_ROUTE_CONFIG", "config.json")))
    parser.add_argument("--state", default=os.environ.get("CARAVAN_SCOUT_STATE",
                        os.environ.get("LLM_EASY_ROUTE_STATE", "state.json")))
    args = parser.parse_args(argv)

    agent = RouteAgent(Path(args.config).expanduser(), Path(args.state).expanduser())
    agent.adopt_or_reap_strays()
    heartbeat = threading.Thread(target=agent.heartbeat_loop, daemon=True)
    heartbeat.start()

    host = str(agent.config.get("listenHost") or "0.0.0.0")
    port = int(agent.config.get("listenPort") or 8092)
    server = ThreadingHTTPServer((host, port), make_handler(agent))
    print(f"caravan-scout listening on http://{host}:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
