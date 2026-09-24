"""The machine a scout runs on, as its OS tells it: the GPUs and the
processes on them, CPU and memory, who the firewall lets in, what listens,
and which address faces the controller."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse


class Machine:
    """This machine's probes, with the caches that keep a polling board from
    spawning nvidia-smi and sudo on every request.

    A probe that cannot ask — the tool is missing, fails, or outlives its
    timeout — answers with nothing: an empty list, or {"state": "unknown"}
    for the firewall. The caches keep an empty answer too, so a host without
    cards does not run nvidia-smi and lspci on every request either.

    The names come from the config: hostId and displayName are the
    operator's; the hostname and the address are the OS's.
    """

    #: How long an answer stays good, in seconds.
    GPUS_TTL = 10
    APPS_TTL = 5
    FIREWALL_TTL = 30

    def __init__(self, config):
        self.config = config
        self._gpus: list[dict[str, Any]] = []
        self._gpus_at = 0.0
        self._apps: list[dict[str, Any]] = []
        self._apps_at = 0.0
        self._firewall: dict[Any, tuple[float, dict[str, Any]]] = {}

    # ── cached probes ───────────────────────────────────────────────────────

    def gpus(self) -> list[dict[str, Any]]:
        """The GPU inventory, refreshed at most every 10 s, so polling
        /api/state does not spawn nvidia-smi on every request."""
        now = time.time()
        if self._gpus_at and now - self._gpus_at < self.GPUS_TTL:
            return self._gpus
        self._gpus = self.gpu_inventory()
        self._gpus_at = now
        return self._gpus

    def compute_apps(self) -> list[dict[str, Any]]:
        """Per-process GPU memory (pid -> gpu), refreshed at most every 5 s."""
        now = time.time()
        if self._apps_at and now - self._apps_at < self.APPS_TTL:
            return self._apps
        self._apps = self.nvidia_apps()
        self._apps_at = now
        return self._apps

    def firewall(self, port) -> dict[str, Any]:
        """Who ufw lets reach `port`, cached ~30 s PER PORT.

        This was a single-slot cache (one tuple for the whole agent), so a host
        running more than one cell thrashed it: each port missed the other's
        entry and re-ran `sudo -n ufw status`. With a few cells and a ~1 s
        status pass that is several sudo+ufw forks per second — measured at
        232/min on a 3-cell host, driving its load average past 25 and stalling
        cell starts. One entry per port fixes it.
        """
        now = time.time()
        hit = self._firewall.get(port)
        if hit and now - hit[0] < self.FIREWALL_TTL:
            return hit[1]
        fw = self.ufw_access(port)
        self._firewall[port] = (now, fw)
        return fw

    # ── who this machine is ─────────────────────────────────────────────────

    def address(self) -> str:
        """The address the controller can reach this machine at.

        UDP-connect trick: no packet is sent; the kernel just picks the
        interface that routes towards the target. Aim at the controller so
        multi-homed hosts report the address the controller can reach."""
        target, port = "8.8.8.8", 80
        try:
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

    def name(self) -> str:
        """What this machine is called where one word is wanted."""
        return self.config.get("hostId") or self.config.get("displayName") or "remote"

    # ── asked on demand ─────────────────────────────────────────────────────

    def listeners(self) -> dict[str, Any]:
        """TCP ports LISTENing on this host, with the owning process where the
        OS will say.

        The controller's cell-port picker can only see its own box, so a
        listener on a CLIENT — someone's dev server, a leftover service — was
        invisible: the picker painted the number free, the cell reserved fine
        and then failed to bind. This is the client half of that answer.

        `ss -ltnp` only reveals pids for our own processes without root; an
        unknown owner still reports the port, with an empty proc. Knowing the
        number is taken matters more than knowing by whom.
        """
        rows: list[dict[str, Any]] = []
        try:
            res = subprocess.run(["ss", "-ltnpH"], text=True, capture_output=True, timeout=6)
            for line in (res.stdout or "").splitlines():
                parts = line.split()
                if len(parts) < 4:
                    continue
                _, _, port = parts[3].rpartition(":")
                if not port.isdigit():
                    continue
                m = re.search(r'\("([^"]+)",pid=(\d+)', line)
                rows.append({"port": int(port),
                             "proc": m.group(1) if m else "",
                             "pid": int(m.group(2)) if m else 0})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:160], "ports": []}
        # One row per port: a socket bound on both v4 and v6 is one listener.
        best: dict[int, dict[str, Any]] = {}
        for r in rows:
            cur = best.get(r["port"])
            if cur is None or (not cur.get("proc") and r.get("proc")):
                best[r["port"]] = r
        return {"ok": True, "ports": sorted(best.values(), key=lambda r: r["port"])}

    def nvidia_smi(self) -> dict[str, Any]:
        """nvidia-smi as its own table, for the admin's monitor panel."""
        try:
            result = subprocess.run(
                ["nvidia-smi"], text=True, capture_output=True, timeout=5
            )
            ok = result.returncode == 0
            output = (result.stdout if ok else result.stderr or result.stdout).strip()
        except FileNotFoundError:
            ok, output = False, "nvidia-smi not found"
        except Exception as exc:
            ok, output = False, str(exc)
        return {
            "kind": "nvidia-smi",
            "ok": ok,
            "output": output,
            "source": self.name(),
            "time": int(time.time()),
        }

    # ── the probes themselves: one question to the OS each ──────────────────

    @classmethod
    def gpu_inventory(cls) -> list[dict[str, Any]]:
        """Preferred path: live stats via nvidia-smi. Fallback: lspci detection so a
        card with a missing driver is still reported (driverStatus=driver_missing)."""
        gpus = cls.nvidia_gpus()
        if gpus:
            return gpus
        return cls.lspci_gpus()

    @staticmethod
    def nvidia_gpus() -> list[dict[str, Any]]:
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

    @staticmethod
    def nvidia_apps() -> list[dict[str, Any]]:
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

    @staticmethod
    def lspci_gpus() -> list[dict[str, Any]]:
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

    @classmethod
    def ufw_access(cls, port) -> dict[str, Any]:
        """Classify who may reach `port` per ufw. {state, allowedFrom[]}.
        state: open (ufw off) | all | restricted | blocked | unknown."""
        try:
            port = int(port)
        except (TypeError, ValueError):
            return {"state": "unknown"}
        out = cls.run_text(["sudo", "-n", "ufw", "status"])
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

    @classmethod
    def cpu_ram(cls) -> dict[str, Any]:
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
                out = cls.run_text(["sysctl", "-n", "hw.memsize"])
                if out.strip().isdigit():
                    info["ram"] = {"usedGb": None,
                                   "totalGb": round(int(out.strip()) / 1024**3, 1)}
        except Exception:
            pass
        return info

    @staticmethod
    def run_text(cmd: list[str], timeout: int = 4) -> str:
        """Run a command, return stdout on success, "" on any failure/non-zero."""
        try:
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        except Exception:
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout
