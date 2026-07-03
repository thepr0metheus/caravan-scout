"""ThreadingHTTPServer handler factory: the agent's HTTP surface on :8092."""
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


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def make_handler(agent: RouteAgent):
    class Handler(BaseHTTPRequestHandler):
        server_version = "caravan-scout/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}")

        def send_json(self, payload: Any, status: int = 200) -> None:
            data = json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise AppError("body must be a JSON object")
            return payload

        def do_GET(self) -> None:
            try:
                if self.path in ("/", "/index.html"):
                    from caravan_scout.webui import pair_page_bytes
                    data = pair_page_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.path == "/api/health":
                    self.send_json({"ok": True, "service": "caravan-scout", "time": int(time.time())})
                    return
                if self.path == "/api/state":
                    self.send_json(agent.public_state())
                    return
                if self.path == "/api/llama-node/status":
                    self.send_json({"ok": True, "nodes": agent.llama_nodes_public()})
                    return
                if self.path.startswith("/api/agent-config"):
                    from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs
                    _q = _parse_qs(_urlparse(self.path).query or "")
                    _id = (_q.get("id") or [""])[0].strip()
                    self.send_json(agent.agent_openclaw_config(_id))
                    return
                if self.path == "/api/monitor/nvidia-smi":
                    self.send_json(agent.monitor_nvidia_smi())
                    return
                if self.path == "/api/llama-node/configs":
                    self.send_json({"ok": True, "configs": agent.list_llama_node_configs()})
                    return
                if self.path == "/api/llama-node/list-cache":
                    self.send_json({"ok": True, "models": agent.list_cached_models()})
                    return
                self.send_json({"error": "not found"}, 404)
            except AppError as exc:
                self.send_json({"error": str(exc)}, exc.status)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)

        def do_POST(self) -> None:
            try:
                if self.path == "/api/routing/apply":
                    self.send_json(agent.apply_assignments(self.read_body()))
                    return
                if self.path == "/api/heartbeat":
                    self.send_json(agent.heartbeat_once())
                    return
                if self.path == "/api/controller-url":
                    body = self.read_body()
                    self.send_json(agent.set_controller_url(str(body.get("url") or "")))
                    return
                if self.path == "/api/llama-node/start":
                    result = agent.llama_node_start(self.read_body())
                    self.send_json(result, 200 if result.get("ok") else 400)
                    return
                if self.path == "/api/llama-node/stop":
                    body = self.read_body()
                    _p = body.get("port")
                    _ports = [int(_p)] if _p else [pp for pp, _ in agent._slots_snapshot()]
                    _results = []
                    _purge_any = False
                    for pp in _ports:
                        _sl = agent._slot(pp)
                        _results.append(_sl.node.stop())
                        agent._set_llama_startup(pp, phase="idle", error="",
                                                 downloadedBytes=0, totalBytes=0)
                        # Don't keep models on client disks (unless caching is on).
                        if not _sl.cache_models:
                            _purge_any = True
                        agent._drop_slot(pp)   # a stopped slot disappears from the fleet view
                    # Purge once, after the stopped slots are dropped, via the SAFE
                    # variant so a model still served by another running slot isn't
                    # evicted (stopping whisper must not delete the translator gguf).
                    if _purge_any:
                        try:
                            agent.purge_model_cache_safe()
                        except Exception:
                            pass
                    self.send_json(_results[0] if len(_results) == 1
                                   else {"ok": True, "results": _results})
                    return
                if self.path == "/api/llama-node/purge-cache":
                    self.send_json({"ok": True, **agent.purge_model_cache_safe()})
                    return
                if self.path == "/api/llama-node/configs/delete":
                    body = self.read_body()
                    agent.delete_llama_node_config(str(body.get("filename") or ""))
                    self.send_json({"ok": True})
                    return
                self.send_json({"error": "not found"}, 404)
            except AppError as exc:
                self.send_json({"error": str(exc)}, exc.status)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)

    return Handler


