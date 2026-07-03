"""RouteAgent: slots/config/state core, assembled from the domain mixins."""
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
from caravan_scout.paths import DEFAULT_CONFIG
from caravan_scout.errors import AppError
from caravan_scout.hw import firewall_port_access
from caravan_scout.node import _Slot
from caravan_scout.registry import RegistryMixin
from caravan_scout.openclaw import OpenclawMixin
from caravan_scout.heartbeat import HeartbeatMixin
from caravan_scout.models import ModelsMixin
from caravan_scout.cells import CellsMixin


class RouteAgent(RegistryMixin, OpenclawMixin, HeartbeatMixin, ModelsMixin, CellsMixin):
    def __init__(self, config_path: Path, state_path: Path):
        self.config_path = config_path
        self.state_path = state_path
        self.lock = threading.Lock()
        self.config = self.load_config()
        self.state = self.load_state()
        self.state.setdefault("startedAt", int(time.time()))
        self.state.setdefault("assignments", [])
        self.state.setdefault("applyStatus", {"state": "none"})
        self.state.setdefault("heartbeat", {"state": "pending"})
        self._gpu_cache: list[dict[str, Any]] = []
        self._gpu_cache_at = 0.0
        self._runtime_cache: dict[str, Any] = {}
        self._runtime_cache_at = 0.0
        # Multiple concurrent server slots keyed by port (a client can hold more
        # than one server at once — e.g. a translator + a whisper cell).
        self.slots: dict[int, _Slot] = {}
        self._slots_lock = threading.Lock()
        self._configs_dir = self.state_path.parent / "llama-node-configs"

    def _slot(self, port: int) -> "_Slot":
        port = int(port)
        with self._slots_lock:
            slot = self.slots.get(port)
            if slot is None:
                slot = self.slots[port] = _Slot()
            return slot

    def _slots_snapshot(self) -> list:
        with self._slots_lock:
            return list(self.slots.items())

    def _drop_slot(self, port: int) -> None:
        with self._slots_lock:
            self.slots.pop(int(port), None)

    def _set_llama_startup(self, port: int, **kw: Any) -> None:
        slot = self._slot(port)
        with slot.lock:
            slot.startup.update(kw)

    def _get_llama_startup(self, port: int) -> dict[str, Any]:
        slot = self._slot(port)
        with slot.lock:
            return dict(slot.startup)

    def _llama_metrics(self, port: int) -> dict[str, Any]:
        """Scrape the local llama-server /metrics (Prometheus) for live token
        rates. Cached ~2s. Returns {promptTps, genTps, requestsProcessing}."""
        now = time.time()
        cache = getattr(self, "_metrics_cache", None)
        if cache and now - cache[0] < 2 and cache[1] == port:
            return cache[2]
        out: dict[str, Any] = {}
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/metrics", timeout=1) as r:
                for line in r.read().decode("utf-8", "replace").splitlines():
                    if line.startswith("#") or " " not in line:
                        continue
                    key, _, val = line.partition(" ")
                    try:
                        num = float(val.split()[0])
                    except (ValueError, IndexError):
                        continue
                    if key == "llamacpp:prompt_tokens_seconds":
                        out["promptTps"] = round(num, 2)
                    elif key == "llamacpp:predicted_tokens_seconds":
                        out["genTps"] = round(num, 2)
                    elif key == "llamacpp:requests_processing":
                        out["requestsProcessing"] = int(num)
                    elif key == "llamacpp:kv_cache_usage_ratio":
                        out["_kvRatio"] = num
        except Exception:
            out = {}
        # Context window the server launched with + live KV-cache occupancy.
        ctx_max = self._llama_ctx_max(port)
        ratio = out.pop("_kvRatio", None)
        if ctx_max:
            out["ctxMax"] = ctx_max
            if ratio is not None:
                out["ctxUsed"] = int(round(ratio * ctx_max))
        self._metrics_cache = (now, port, out)
        return out

    def _llama_ctx_max(self, port: int) -> int:
        """n_ctx the llama-server was launched with, from /props. Cached ~30s."""
        now = time.time()
        cache = getattr(self, "_ctx_cache", None)
        if cache and now - cache[0] < 30 and cache[1] == port:
            return cache[2]
        ctx = 0
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/props", timeout=1) as r:
                props = json.loads(r.read().decode("utf-8", "replace"))
            gen = props.get("default_generation_settings") or {}
            ctx = int(gen.get("n_ctx") or props.get("n_ctx") or 0)
        except Exception:
            ctx = 0
        self._ctx_cache = (now, port, ctx)
        return ctx

    def _firewall(self, port: int) -> dict[str, Any]:
        """Cached (~30s) ufw access classification for the llama-node port."""
        now = time.time()
        cache = getattr(self, "_fw_cache", None)
        if cache and now - cache[0] < 30 and cache[1] == port:
            return cache[2]
        fw = firewall_port_access(port)
        self._fw_cache = (now, port, fw)
        return fw

    def _node_public(self, port: int, slot: "_Slot") -> dict[str, Any]:
        """slot.node.status() merged with its async startup phase/progress, so
        the admin sees downloading/loading state before the server is up."""
        st = slot.node.status()
        with slot.lock:
            startup = dict(slot.startup)
        phase = startup.get("phase")
        if st.get("running"):
            p = st.get("port") or port
            metrics = self._llama_metrics(p) if p else {}
            return {**st, "port": p, "phase": "running", **metrics,
                    "firewall": self._firewall(p) if p else {}}
        # Crashed shortly after start (non-zero exit) — surface as error even if
        # the startup worker already marked it "running".
        if st.get("crashed"):
            return {**st, "phase": "error", "port": startup.get("port") or port,
                    "modelPath": startup.get("modelPath", ""),
                    "lastError": st.get("lastError") or f"exited (code {st.get('exitCode')})"}
        if phase in ("resolving", "downloading", "loading"):
            return {
                **st, "running": False, "phase": phase,
                "modelPath": startup.get("modelPath", ""),
                "port": startup.get("port") or port,
                "downloadedBytes": startup.get("downloadedBytes", 0),
                "totalBytes": startup.get("totalBytes", 0),
                "downloadingFile": startup.get("downloadingFile", ""),
                "startedAt": startup.get("startedAt"),
            }
        if phase == "error":
            return {**st, "port": port, "phase": "error",
                    "lastError": startup.get("error") or st.get("lastError", "")}
        return {**st, "port": port}

    def llama_nodes_public(self) -> list:
        return [self._node_public(p, s) for p, s in self._slots_snapshot()]

    def llama_node_public(self) -> dict[str, Any]:
        # Backward-compat single-node view (first slot) for un-updated controllers.
        nodes = self.llama_nodes_public()
        return nodes[0] if nodes else {"running": False, "phase": "idle"}

    def load_config(self) -> dict[str, Any]:
        config = DEFAULT_CONFIG.copy()
        if self.config_path.exists():
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise AppError("config must be a JSON object")
            config.update(payload)
        config["listenPort"] = int(config.get("listenPort") or 8092)
        config["heartbeatIntervalSeconds"] = max(2, int(config.get("heartbeatIntervalSeconds") or 60))
        config["agents"] = self.normalize_agents(config.get("agents") or [])
        return config

    def load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
            except Exception:
                pass
        return {}

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def validate_assignments(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        assignments = payload.get("assignments")
        if not isinstance(assignments, list):
            raise AppError("assignments must be a list")
        known_agents = {row["id"] for row in self.effective_agents()}
        normalized = []
        for item in assignments:
            if not isinstance(item, dict):
                raise AppError("assignment must be an object")
            agent_id = str(item.get("agentId") or "").strip()
            if not agent_id:
                raise AppError("agentId is required")
            if known_agents and agent_id not in known_agents:
                raise AppError(f"unknown agentId: {agent_id}", 404)
            routes = item.get("routes") or []
            if not isinstance(routes, list):
                raise AppError("routes must be a list")
            seen_roles = set()
            normalized_routes = []
            for route in routes:
                if not isinstance(route, dict):
                    raise AppError("route must be an object")
                role = str(route.get("role") or "primary").strip()
                endpoint = str(route.get("endpoint") or "").strip()
                proxy_id = str(route.get("proxyId") or "").strip()
                if not endpoint:
                    raise AppError("route.endpoint is required")
                if role in seen_roles:
                    raise AppError(f"duplicate route role for {agent_id}: {role}")
                seen_roles.add(role)
                normalized_routes.append({
                    "role": role,
                    "proxyId": proxy_id,
                    "endpoint": endpoint,
                })
            normalized.append({"agentId": agent_id, "routes": normalized_routes})
        return normalized

    def apply_assignments(self, payload: dict[str, Any]) -> dict[str, Any]:
        assignments = self.validate_assignments(payload)
        apply_payload = {
            "hostId": self.config.get("hostId"),
            "assignments": assignments,
            # full agent metadata (runtime/endpoint) so the apply command can reach VM agents —
            # since registry-sourced VM agents are no longer in the static config.json.
            "agents": self.effective_agents(),
            "time": int(time.time()),
        }
        command = str(self.config.get("applyCommand") or "").strip()
        status = {"state": "stored", "appliedAt": int(time.time())}
        if command:
            result = subprocess.run(
                command,
                input=json.dumps(apply_payload),
                text=True,
                shell=True,
                capture_output=True,
                timeout=30,
            )
            status = {
                "state": "ok" if result.returncode == 0 else "error",
                "appliedAt": int(time.time()),
                "returnCode": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
        with self.lock:
            self.state["assignments"] = assignments
            self.state["applyStatus"] = status
            self.save_state()
        return {"ok": status["state"] in {"stored", "ok"}, "assignments": assignments, "applyStatus": status}


