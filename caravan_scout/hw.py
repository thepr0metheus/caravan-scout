"""Host hardware/runtime probes: GPUs, CPU/RAM, firewall, docker, libvirt, ports."""
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


def query_nvidia_gpus() -> list[dict[str, Any]]:
    """Live NVIDIA GPU stats via nvidia-smi (driver required).

    Field names are a CONTRACT with the controller's own gpu_state(), so one card
    renderer draws local and client GPUs alike. The board reads exactly these:
    index, name, memoryUsedMiB, memoryTotalMiB, utilizationGpuPct, temperatureC,
    powerDrawW (see nodeGpuRowHtml in the controller's topology-nodes.js).
    Rename one here and that value silently turns into "?" on the client's card —
    no error anywhere, which is how this kind of drift survives. The controller
    reports a superset (clocks, PCIe); the names above are the shared floor.

    Returns [] when nvidia-smi is missing or fails (no driver, macOS/Metal host).
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,memory.free,"
                "utilization.gpu,temperature.gpu,power.draw,uuid",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    gpus: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 8:
            continue
        gpus.append({
            "index": parts[0],
            "name": parts[1],
            "vendor": "nvidia",
            "driverStatus": "ok",
            "memoryTotalMiB": parts[2],
            "memoryUsedMiB": parts[3],
            "memoryFreeMiB": parts[4],
            "utilizationGpuPct": parts[5],
            "temperatureC": parts[6],
            "powerDrawW": parts[7],
            "uuid": parts[8] if len(parts) > 8 else "",
        })
    return gpus


def query_compute_apps() -> list[dict[str, Any]]:
    """Per-process GPU memory via nvidia-smi, so the admin can map a llama-server
    PID to the GPU(s) it occupies (many-to-many: N servers per GPU, or one
    server split across N GPUs). Returns [{gpuUuid, pid, usedMiB}]."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    apps: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        apps.append({
            "gpuUuid": parts[0],
            "pid": int(parts[1]),
            "usedMiB": int(parts[2]) if parts[2].isdigit() else 0,
        })
    return apps


def firewall_port_access(port: int) -> dict[str, Any]:
    """Classify who may reach `port` per ufw. {state, allowedFrom[]}.
    state: open (ufw off) | all | restricted | blocked | unknown."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"state": "unknown"}
    out = _run_text(["sudo", "-n", "ufw", "status"])
    if not out:
        return {"state": "unknown"}
    if "status: inactive" in out.lower():
        return {"state": "open", "allowedFrom": []}
    anywhere = False
    allowed: list[str] = []
    for line in out.splitlines():
        toks = line.split()
        if not toks:
            continue
        to = toks[0].split("/")[0]
        if not to.isdigit() or int(to) != port:
            continue
        up = line.upper()
        if "ALLOW" not in up:
            continue
        frm = line.split("ALLOW", 1)[1].strip()
        frm = frm.replace("IN", "", 1).split("#")[0].strip()
        if not frm or frm.lower().startswith("anywhere"):
            anywhere = True
        elif "(v6)" not in frm.lower():
            allowed.append(frm)
    if anywhere:
        return {"state": "all", "allowedFrom": ["Anywhere"]}
    if allowed:
        # dedup preserving order
        seen, uniq = set(), []
        for a in allowed:
            if a not in seen:
                seen.add(a); uniq.append(a)
        return {"state": "restricted", "allowedFrom": uniq}
    return {"state": "blocked", "allowedFrom": []}


def host_cpu_ram() -> dict[str, Any]:
    """Best-effort node CPU load + RAM usage (Linux/macOS, stdlib only)."""
    info: dict[str, Any] = {}
    ncpu = os.cpu_count() or 1
    try:
        load1 = os.getloadavg()[0]
        info["loadPct"] = round(min(100.0, load1 / ncpu * 100.0), 1)
        info["load1"] = round(load1, 2)
        info["ncpu"] = ncpu
    except Exception:
        pass
    # Core counts for the admin's CPU/GPU compute-target picker. availableCores
    # uses sched_getaffinity, so on a core-pinned VM it reports the slice the
    # process can actually use, not the host total.
    info["logicalCores"] = ncpu
    try:
        info["availableCores"] = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        info["availableCores"] = ncpu
    try:
        phys, cur = set(), ""
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("physical id"):
                    cur = line.split(":", 1)[1].strip()
                elif line.startswith("core id"):
                    phys.add((cur, line.split(":", 1)[1].strip()))
        info["physicalCores"] = len(phys) or ncpu
    except Exception:
        info["physicalCores"] = ncpu
    try:
        if sys.platform.startswith("linux") and os.path.exists("/proc/meminfo"):
            mem = {}
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    k, _, v = line.partition(":")
                    mem[k.strip()] = v.strip()
            total_kb = int(mem.get("MemTotal", "0 kB").split()[0])
            avail_kb = int(mem.get("MemAvailable", "0 kB").split()[0])
            used_kb = max(0, total_kb - avail_kb)
            info["ram"] = {
                "usedGb": round(used_kb / 1024 / 1024, 1),
                "totalGb": round(total_kb / 1024 / 1024, 1),
            }
        else:
            out = _run_text(["sysctl", "-n", "hw.memsize"])
            if out.strip().isdigit():
                info["ram"] = {"usedGb": None,
                               "totalGb": round(int(out.strip()) / 1024**3, 1)}
    except Exception:
        pass
    return info


def detect_nvidia_via_lspci() -> list[dict[str, Any]]:
    """Find NVIDIA cards via lspci — works even with no driver installed.

    Used as a fallback so a client with a physical GPU but no nvidia-smi still
    surfaces the card in the admin UI with a "driver missing" hint, instead of
    looking like it has no GPU at all. Linux only; returns [] elsewhere/on error.
    """
    if not sys.platform.startswith("linux"):
        return []
    try:
        result = subprocess.run(
            ["lspci"], text=True, capture_output=True, timeout=5
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    gpus: list[dict[str, Any]] = []
    index = 0
    for line in result.stdout.splitlines():
        low = line.lower()
        if "nvidia" not in low:
            continue
        if not any(k in low for k in ("vga", "3d controller", "display")):
            continue
        # Extract a human name from the bracketed model, e.g.
        # "... [GeForce RTX 3090] (rev a1)" -> "NVIDIA GeForce RTX 3090"
        name = "NVIDIA GPU"
        if "[" in line and "]" in line:
            inner = line[line.rfind("[") + 1:line.rfind("]")].strip()
            if inner:
                name = inner if inner.lower().startswith("nvidia") else f"NVIDIA {inner}"
        gpus.append({
            "index": str(index),
            "name": name,
            "vendor": "nvidia",
            "driverStatus": "driver_missing",
        })
        index += 1
    return gpus


def gpu_inventory() -> list[dict[str, Any]]:
    """Preferred path: live stats via nvidia-smi. Fallback: lspci detection so a
    card with a missing driver is still reported (driverStatus=driver_missing)."""
    gpus = query_nvidia_gpus()
    if gpus:
        return gpus
    return detect_nvidia_via_lspci()


def _run_text(cmd: list[str], timeout: int = 4) -> str:
    """Run a command, return stdout on success, "" on any failure/non-zero."""
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def docker_running_containers() -> dict[str, str]:
    """Map running docker container name -> published-ports string.

    Empty when docker is absent or the user can't reach the daemon. Only RUNNING
    containers are reported, so a stale *exited* container named the same as a
    live VM (e.g. agent-dwight) does not shadow the real runtime.
    """
    out = _run_text(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"])
    containers: dict[str, str] = {}
    for line in out.splitlines():
        name, _, ports = line.partition("\t")
        name = name.strip()
        if name:
            containers[name] = ports.strip()
    return containers


def libvirt_running_vms() -> set[str]:
    """Running libvirt domain names. Tries the system URI first (where homelab
    VMs usually live), then the per-user session. Empty when virsh is absent."""
    for uri in ("qemu:///system", "qemu:///session"):
        out = _run_text(["virsh", "-c", uri, "list", "--name", "--state-running"])
        names = {line.strip() for line in out.splitlines() if line.strip()}
        if names:
            return names
    return set()


def libvirt_domain_ip(name: str) -> str:
    """Best-effort IPv4 of a running libvirt domain via the guest agent. '' if unknown."""
    for uri in ("qemu:///system", "qemu:///session"):
        out = _run_text(["virsh", "-c", uri, "domifaddr", name, "--source", "agent"], timeout=6)
        for line in out.splitlines():
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", line)
            if m and not m.group(1).startswith("127."):
                return m.group(1)
    return ""


def _published_host_ports(ports_field: str) -> set[int]:
    """Extract host-side ports from a `docker ps` ports string, e.g.
    "0.0.0.0:18795->18795/tcp, [::]:18795->18795/tcp" -> {18795}."""
    found: set[int] = set()
    for chunk in ports_field.split(","):
        host, sep, _ = chunk.partition("->")
        if not sep:
            continue
        _, _, port = host.rpartition(":")
        if port.strip().isdigit():
            found.add(int(port.strip()))
    return found


def host_listen_ports() -> set[int]:
    """TCP ports in LISTEN state on the host (any process). Linux-only via `ss`;
    returns empty elsewhere. Used as a positive signal that an agent runs as a
    plain host process rather than in a container or VM."""
    out = _run_text(["ss", "-ltnH"])
    ports: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        _, _, port = parts[3].rpartition(":")
        if port.isdigit():
            ports.add(int(port))
    return ports


