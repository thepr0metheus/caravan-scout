"""HeartbeatMixin: public state snapshot + heartbeat POST loop to the controller."""
from __future__ import annotations

import json
import os
import re
import signal
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from caravan_scout.errors import AppError
from caravan_scout.hw import host_cpu_ram


class HeartbeatMixin:
    def local_ip(self) -> str:
        # UDP-connect trick: no packet is sent; the kernel just picks the
        # interface that routes towards the target. Aim at the controller so
        # multi-homed hosts report the address the controller can reach.
        target, port = "8.8.8.8", 80
        try:
            from urllib.parse import urlparse
            parsed = urlparse(str(self.config.get("controllerUrl") or ""))
            if parsed.hostname:
                target, port = parsed.hostname, int(parsed.port or 80)
        except Exception:
            pass
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target, port))
                return sock.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    def endpoint_to_route(self, endpoint: str, role: str) -> dict[str, str]:
        cleaned = str(endpoint or "").strip()
        proxy_id = ""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(cleaned)
            if parsed.port:
                proxy_id = f"skynet:proxy:{parsed.port}"
        except Exception:
            pass
        return {"role": role, "proxyId": proxy_id, "endpoint": cleaned}

    def _llama_binary_version(self) -> str:
        """Return the llama-server version string, e.g. 'version: 362 (3ac3c20)'."""
        bin_path = str(self.config.get("llamaServerBin") or "").strip()
        if not bin_path or not os.path.isfile(bin_path):
            return ""
        try:
            result = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=5)
            return (result.stdout or result.stderr or "").strip().splitlines()[0] if result.returncode == 0 else ""
        except Exception:
            return ""

    def _llama_binary_mtime(self) -> str:
        """Return ISO-8601 mtime of the llama-server binary (date it was built/replaced)."""
        bin_path = str(self.config.get("llamaServerBin") or "").strip()
        if not bin_path or not os.path.isfile(bin_path):
            return ""
        try:
            import datetime
            mtime = os.path.getmtime(bin_path)
            return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            return ""

    def public_state(self) -> dict[str, Any]:
        assignments = self.current_assignments()
        gpus = self.detect_gpus()
        agents = self.annotate_agent_runtimes(self.effective_agents())
        compute_apps = self.detect_compute_apps()
        cpu_ram = host_cpu_ram()
        with self.lock:
            return {
                "service": "caravan-scout",
                "version": "0.1.0",
                "llamaBinaryVersion": self._llama_binary_version(),
                "llamaBinaryMtime": self._llama_binary_mtime(),
                "host": {
                    "id": self.config.get("hostId"),
                    "name": self.config.get("displayName"),
                    "hostname": socket.gethostname(),
                    "ip": self.local_ip(),
                },
                "controllerUrl": self.config.get("controllerUrl"),
                "agents": agents,
                "candidates": self.discovery_candidates(),
                "assignments": assignments,
                "gpus": gpus,
                "computeApps": compute_apps,
                "cpu": cpu_ram,
                "platform": sys.platform,
                "applyStatus": self.state.get("applyStatus", {}),
                "heartbeat": self.state.get("heartbeat", {}),
                "llamaNode": self.llama_node_public(),
                "llamaNodes": self.llama_nodes_public(),
                "time": int(time.time()),
            }

    def heartbeat_payload(self) -> dict[str, Any]:
        state = self.public_state()
        return {
            "host": state["host"],
            "agents": state["agents"],
            "candidates": state.get("candidates", []),
            "assignments": state["assignments"],
            "gpus": state.get("gpus", []),
            "computeApps": state.get("computeApps", []),
            "cpu": state.get("cpu", {}),
            "platform": state.get("platform", ""),
            "llamaNode": self.llama_node_public(),
            "llamaNodes": self.llama_nodes_public(),
            "llamaBinaryVersion": state.get("llamaBinaryVersion", ""),
            "llamaBinaryMtime": state.get("llamaBinaryMtime", ""),
            "applyStatus": state["applyStatus"],
            "agentUrl": f"http://{state['host']['ip']}:{self.config.get('listenPort')}",
            "time": state["time"],
        }

    def post_json(self, url: str, payload: dict[str, Any], timeout: int = 5) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {"ok": True}

    def heartbeat_once(self) -> dict[str, Any]:
        controller = str(self.config.get("controllerUrl") or "").rstrip("/")
        if not controller:
            raise AppError("controllerUrl is required")
        url = f"{controller}/api/topology/client-heartbeat"
        return self.post_json(url, self.heartbeat_payload())

    def heartbeat_loop(self) -> None:
        while True:
            started = int(time.time())
            try:
                result = self.heartbeat_once()
                status = {"state": "ok", "lastAt": started, "result": result}
            except Exception as exc:
                status = {"state": "error", "lastAt": started, "error": str(exc)}
            with self.lock:
                self.state["heartbeat"] = status
                self.save_state()
            # Use a short interval during llama-node startup so the admin UI
            # receives download/loading progress updates in near-real-time.
            startup_phases = {"resolving", "downloading", "loading", "warming"}
            if any(n.get("phase") in startup_phases for n in self.llama_nodes_public()):
                interval = 5
            else:
                interval = int(self.config.get("heartbeatIntervalSeconds") or 60)
            time.sleep(interval)

