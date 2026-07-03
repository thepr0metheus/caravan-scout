"""RegistryMixin: agent roster from the fleet registry + runtime detection."""
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
from caravan_scout.hw import _published_host_ports, docker_running_containers, gpu_inventory, host_listen_ports, libvirt_domain_ip, libvirt_running_vms, query_compute_apps


class RegistryMixin:
    def normalize_agents(self, agents: list[Any]) -> list[dict[str, Any]]:
        normalized = []
        seen = set()
        optional_fields = ("runtime", "scope", "container", "port", "endpoint", "url", "description", "openclawConfigPath")
        for row in agents:
            if not isinstance(row, dict):
                continue
            agent_id = str(row.get("id") or row.get("name") or "").strip()
            if not agent_id or agent_id in seen:
                continue
            seen.add(agent_id)
            agent = {
                "id": agent_id,
                "name": str(row.get("name") or agent_id).strip(),
                "kind": str(row.get("kind") or "manual").strip(),
                "status": str(row.get("status") or "configured").strip(),
            }
            for field in optional_fields:
                value = row.get(field)
                if value is not None and str(value).strip():
                    agent[field] = str(value).strip()
            normalized.append(agent)
        return normalized

    def get_json(self, url: str, timeout: int = 5) -> Any:
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}

    def _fetch_registry_agents(self) -> list[dict[str, Any]] | None:
        """Derive THIS host's VM/docker agents from the fleet registry (<registryUrl>/api/agents).

        Identity (id/name/host/openclaw-port) lives only in the registry; here we just keep the
        entries whose machine (`agent-<id>`, from `placement`) is actually running on this host —
        so route-agent stops hand-maintaining an agent list and only contributes host knowledge.
        Returns None on any failure so effective_agents() falls back to the static list (the fleet
        is never wiped by a registry blip). Host/orchestrator agents are NOT taken from here; they
        stay in the static "agents" list for continuity of their existing proxy assignment."""
        url = str(self.config.get("registryUrl") or "").strip()
        if not url:
            return None
        try:
            data = self.get_json(url.rstrip("/") + "/api/agents", timeout=5)
        except Exception as exc:  # network/json/etc — keep static agents
            print(f"[registry] fetch failed ({url}): {exc} — keeping static agents")
            return None
        if not isinstance(data, dict):
            return None
        entries = list(data.get("agents") or [])
        if isinstance(data.get("orchestrator"), dict):
            entries.append(data["orchestrator"])
        inv = self.detect_runtime_inventory()  # {docker, vm, hostPorts} live on this host
        out: list[dict[str, Any]] = []
        for a in entries:
            if not isinstance(a, dict):
                continue
            aid = str(a.get("id") or "").strip()
            if not aid:
                continue
            placement = str(a.get("placement") or "")
            match = re.search(r"agent-[a-z0-9-]+", placement, re.I)
            machine = match.group(0) if match else f"agent-{aid}"
            on_vm = machine in inv["vm"]
            on_docker = machine in inv["docker"] or aid in inv["docker"]
            if not (on_vm or on_docker):
                continue  # not a vm/docker agent on this host → leave to the static list
            host_ip = str(a.get("host") or "").strip()
            port = a.get("port")
            endpoint = str(a.get("url") or "").strip() or (
                f"http://{host_ip}:{port}" if host_ip and port else "")
            row: dict[str, Any] = {
                "id": aid,
                "name": str(a.get("name") or aid).strip(),
                "kind": "openclaw",
                "scope": "agent",
                "runtime": "vm" if on_vm else "docker",
                "container": machine,
            }
            if port:
                row["port"] = str(port)
            if endpoint:
                row["endpoint"] = endpoint
            out.append(row)
        return out

    def _registry_agents_cached(self) -> list[dict[str, Any]] | None:
        now = time.time()
        if getattr(self, "_reg_at", 0) and now - self._reg_at < 30:
            return self._reg_cache
        self._reg_cache = self._fetch_registry_agents()
        self._reg_at = now
        return self._reg_cache

    def effective_agents(self) -> list[dict[str, Any]]:
        """Static "agents" (host/orchestrator + any manual entries) merged with the registry-derived
        VM/docker agents for this host. Registry wins for ids it owns. This is the runtime agent list
        every consumer should read (not the raw static config), so a newly-registered agent appears
        within one cache window with no config edit and no restart."""
        base = list(self.config.get("agents", []))
        reg = self._registry_agents_cached()
        if not reg:
            return base
        merged = {a["id"]: a for a in base}
        for a in reg:
            merged[a["id"]] = a
        return self.normalize_agents(list(merged.values()))

    def discovery_candidates(self) -> list[dict[str, Any]]:
        """Running `agent-*` machines on this host that are NOT in the fleet registry —
        surfaced as 'add me?' hints so a VM created outside the registry is still noticed."""
        inv = self.detect_runtime_inventory()
        known = set()
        for a in self.effective_agents():
            if a.get("container"):
                known.add(str(a["container"]))
            known.add(f"agent-{a.get('id')}")
        out = []
        for machine in sorted(inv["vm"] | inv["docker"]):
            if not machine.startswith("agent-") or machine in known:
                continue
            runtime = "vm" if machine in inv["vm"] else "docker"
            cand = {"machine": machine, "suggestedId": machine[len("agent-"):], "runtime": runtime}
            if runtime == "vm":
                ip = libvirt_domain_ip(machine)
                if ip:
                    cand["ip"] = ip
            out.append(cand)
        return out

    def detect_gpus(self) -> list[dict[str, Any]]:
        """Cached local GPU inventory (refreshed at most every 10s) so polling
        /api/state does not spawn nvidia-smi on every request."""
        now = time.time()
        if self._gpu_cache_at and now - self._gpu_cache_at < 10:
            return self._gpu_cache
        self._gpu_cache = gpu_inventory()
        self._gpu_cache_at = now
        return self._gpu_cache

    def detect_compute_apps(self) -> list[dict[str, Any]]:
        """Cached (<=5s) per-process GPU memory map (pid -> gpu)."""
        now = time.time()
        if getattr(self, "_capps_at", 0) and now - self._capps_at < 5:
            return self._capps_cache
        self._capps_cache = query_compute_apps()
        self._capps_at = now
        return self._capps_cache

    def detect_runtime_inventory(self) -> dict[str, Any]:
        """Cached (<=10s) snapshot of where things actually run on this host:
        live docker containers, live libvirt VMs, and host-process listen ports.
        Used to report each agent's *real* runtime instead of trusting whatever
        the config declared."""
        now = time.time()
        if self._runtime_cache_at and now - self._runtime_cache_at < 10:
            return self._runtime_cache
        docker = docker_running_containers()
        docker_ports: set[int] = set()
        for ports_field in docker.values():
            docker_ports |= _published_host_ports(ports_field)
        # A port published by docker shows up as a host listener too (docker-proxy);
        # exclude those so only genuine host processes count as "host".
        host_ports = host_listen_ports() - docker_ports
        self._runtime_cache = {
            "docker": set(docker.keys()),
            "vm": libvirt_running_vms(),
            "hostPorts": host_ports,
        }
        self._runtime_cache_at = now
        return self._runtime_cache

    def annotate_agent_runtimes(self, agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Overlay live-detected runtime (docker/vm/host) onto declared agents.

        Matching is by machine name (container/id/name/`agent-<id>`) against the
        running docker and libvirt inventories, then by listen-port against host
        processes. Detection only *overrides* when it finds a positive live
        signal; otherwise the declared runtime is kept (agent offline, or a host
        like macOS where docker/virsh/ss are absent)."""
        inv = self.detect_runtime_inventory()
        annotated: list[dict[str, Any]] = []
        for agent in agents:
            row = dict(agent)
            agent_id = str(row.get("id") or "").strip()
            names = {
                str(value).strip()
                for value in (row.get("container"), agent_id, row.get("name"),
                              f"agent-{agent_id}" if agent_id else "")
                if value and str(value).strip()
            }
            port = None
            raw_port = str(row.get("port") or "").strip()
            if raw_port.isdigit():
                port = int(raw_port)

            docker_hit = names & inv["docker"]
            vm_hit = names & inv["vm"]
            if docker_hit:
                row["runtime"] = "docker"
                row["container"] = sorted(docker_hit)[0]
                row["runtimeDetected"] = True
            elif vm_hit:
                row["runtime"] = "vm"
                row["container"] = sorted(vm_hit)[0]
                row["runtimeDetected"] = True
            elif port is not None and port in inv["hostPorts"]:
                row["runtime"] = "host"
                row.pop("container", None)
                row["runtimeDetected"] = True
            annotated.append(row)
        return annotated

