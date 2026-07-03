"""OpenclawMixin: read agents' OpenClaw configs to derive live assignments."""
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


class OpenclawMixin:
    def openclaw_config_path_for_agent(self, agent: dict[str, Any]) -> Path | None:
        configured = str(agent.get("openclawConfigPath") or self.config.get("openclawConfigPath") or "").strip()
        if configured:
            return Path(configured).expanduser()
        if agent.get("id") == self.config.get("openclawAgentId", "openclaw"):
            path = Path("~/.openclaw/openclaw.json").expanduser()
            if path.exists():
                return path
        return None

    def openclaw_model_refs_for_agent(self, payload: dict[str, Any], agent_id: str) -> tuple[str, list[str]]:
        agents = payload.get("agents") if isinstance(payload.get("agents"), dict) else {}
        defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
        selected = None
        for row in agents.get("list") or []:
            if isinstance(row, dict) and str(row.get("id") or "") == agent_id:
                selected = row
                break
        model = {}
        if isinstance(defaults.get("model"), dict):
            model.update(defaults.get("model") or {})
        if isinstance(selected, dict) and isinstance(selected.get("model"), dict):
            model.update(selected.get("model") or {})
        primary = str(model.get("primary") or "").strip()
        fallbacks = model.get("fallbacks") or []
        if not isinstance(fallbacks, list):
            fallbacks = []
        return primary, [str(row).strip() for row in fallbacks if str(row).strip()]

    def openclaw_endpoint_for_model_ref(self, payload: dict[str, Any], model_ref: str) -> str:
        provider_id = str(model_ref or "").split("/", 1)[0].strip()
        if not provider_id:
            return ""
        providers = (((payload.get("models") or {}).get("providers")) or {})
        if not isinstance(providers, dict):
            return ""
        provider = providers.get(provider_id)
        if not isinstance(provider, dict):
            return ""
        return str(provider.get("baseUrl") or "").strip()

    def live_openclaw_assignments(self) -> list[dict[str, Any]]:
        assignments = []
        for agent in self.effective_agents():
            if str(agent.get("kind") or "").lower() != "openclaw":
                continue
            path = self.openclaw_config_path_for_agent(agent)
            if not path or not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            primary_ref, fallback_refs = self.openclaw_model_refs_for_agent(payload, str(agent.get("id") or ""))
            routes = []
            primary_endpoint = self.openclaw_endpoint_for_model_ref(payload, primary_ref)
            if primary_endpoint:
                routes.append(self.endpoint_to_route(primary_endpoint, "primary"))
            for fallback_ref in fallback_refs[:1]:
                endpoint = self.openclaw_endpoint_for_model_ref(payload, fallback_ref)
                if endpoint:
                    routes.append(self.endpoint_to_route(endpoint, "fallback"))
            if routes:
                assignments.append({"agentId": agent["id"], "routes": routes})
        return assignments

    def current_assignments(self) -> list[dict[str, Any]]:
        live = self.live_openclaw_assignments()
        if live:
            live_by_agent = {row.get("agentId"): row for row in live}
            with self.lock:
                stored = self.state.get("assignments") if isinstance(self.state.get("assignments"), list) else []
                merged = [live_by_agent.get(row.get("agentId"), row) for row in stored if isinstance(row, dict)]
                existing_ids = {row.get("agentId") for row in merged if isinstance(row, dict)}
                merged.extend(row for row in live if row.get("agentId") not in existing_ids)
                if merged != stored:
                    self.state["assignments"] = merged
                    self.state["applyStatus"] = {"state": "live", "appliedAt": int(time.time())}
                    self.save_state()
            return merged
        return self.state.get("assignments", [])


    def agent_openclaw_config(self, agent_id: str) -> dict[str, Any]:
        meta = next((a for a in self.effective_agents() if a.get("id") == agent_id), None)
        if meta is None:
            raise AppError(f"agent not found: {agent_id!r}", 404)
        path = self.openclaw_config_path_for_agent(meta)
        if not path:
            raise AppError(f"no openclawConfigPath for {agent_id!r}", 404)
        if not path.exists():
            raise AppError(f"config not found: {path}", 404)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise AppError(f"read failed: {exc}", 500)
        return {"ok": True, "agentId": agent_id, "path": str(path), "data": data}

