"""The machine a scout runs on, as its OS tells it: the GPUs and the
processes on them, CPU and memory, who the firewall lets in, what listens,
and which address faces the controller."""
from __future__ import annotations

import ipaddress
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
    LISTEN_TTL = 2

    def __init__(self, config):
        self.config = config
        self._gpus: list[dict[str, Any]] = []
        self._gpus_at = 0.0
        self._apps: list[dict[str, Any]] = []
        self._apps_at = 0.0
        self._firewall: dict[Any, tuple[float, dict[str, Any]]] = {}
        self._listening: set[int] | None = None
        self._listening_at = 0.0

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

    def listening_ports(self) -> set[int] | None:
        """The TCP ports something listens on here, asked at most every 2 s
        for all the cells at once; None when the OS will not say."""
        now = time.time()
        if self._listening_at and now - self._listening_at < self.LISTEN_TTL:
            return self._listening
        self._listening = self.listening_now()
        self._listening_at = now
        return self._listening

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
        """The address others can reach this machine at.

        UDP-connect trick: no packet is sent; the kernel just picks the
        interface that routes towards the target. Aimed at the controller, so
        a multi-homed host reports the address the controller can reach — but
        not when the controller runs on this very machine and was paired over
        loopback: 127.0.0.1 reaches nobody else, and the board's links to the
        machine's cells opened the viewer's own computer. Then it is aimed at
        the default route: the address the network knows the machine by."""
        target, port = "8.8.8.8", 80
        try:
            parsed = urlparse(str(self.config.get("controllerUrl") or ""))
            if parsed.hostname and not self.is_loopback(parsed.hostname):
                target, port = parsed.hostname, int(parsed.port or 80)
        except Exception:
            pass
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target, port))
                return sock.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    @staticmethod
    def is_loopback(host: str) -> bool:
        """localhost, 127.0.0.0/8 or ::1: a controller on this very machine."""
        if str(host).lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def name(self) -> str:
        """What this machine is called where one word is wanted."""
        return self.config.get("hostId") or self.config.get("displayName") or "remote"

    @staticmethod
    def listening_now() -> set[int] | None:
        """The ports LISTENing right now: `ss` on Linux, `lsof` on macOS
        (the local address is the 4th and the 9th column); None when
        neither answers — not "nothing listens"."""
        for cmd, column in ((["ss", "-ltnH"], 3), (["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], 8)):
            try:
                res = subprocess.run(cmd, text=True, capture_output=True, timeout=5)
            except Exception:
                continue
            if res.returncode != 0 and not (res.stdout or "").strip():
                continue
            ports: set[int] = set()
            for line in (res.stdout or "").splitlines():
                parts = line.split()
                if len(parts) > column and parts[column].rpartition(":")[2].isdigit():
                    ports.add(int(parts[column].rpartition(":")[2]))
            return ports
        return None

    # ── asked on demand ─────────────────────────────────────────────────────

    def listeners(self) -> dict[str, Any]:
        """TCP ports LISTENing on this host: the owning process where the OS
        will say, and the addresses each port is bound on.

        The controller's cell-port picker can only see its own box, so a
        listener on a CLIENT — someone's dev server, a leftover service — was
        invisible: the picker painted the number free, the cell reserved fine
        and then failed to bind. This is the client half of that answer.

        `ss -ltnpH` on Linux, `lsof` where there is no ss (macOS, where this
        answered nothing at all before 2.12). Neither reveals the pid of
        another user's process without root; an unknown owner still reports
        the port, with an empty proc. Knowing the number is taken matters more
        than knowing by whom.

        `addrs` (2.12): where the port accepts connections — 127.0.0.1 only,
        or the network. An engine on this machine (ForeignEngines) that
        listens on loopback alone cannot be reached by the controller's proxy.

        A tool that fails is no answer: {"ok": false} — not "nothing listens".
        A failed ss read as an empty machine until 2.12.
        """
        rows: list[dict[str, Any]] | None = None
        errors = []
        for tool, read in (("ss", self._ss_rows), ("lsof", self._lsof_rows)):
            try:
                rows = read()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{tool}: {exc}")
                continue
            if rows is not None:
                break
            errors.append(f"{tool} failed")
        if rows is None:
            return {"ok": False, "error": "; ".join(errors)[:160], "ports": []}
        # One row per port: a socket bound on both v4 and v6 is one listener.
        best: dict[int, dict[str, Any]] = {}
        for r in rows:
            cur = best.get(r["port"])
            if cur is None:
                best[r["port"]] = cur = {"port": r["port"], "proc": r["proc"], "pid": r["pid"], "addrs": []}
            elif not cur.get("proc") and r.get("proc"):
                cur["proc"], cur["pid"] = r["proc"], r["pid"]
            if r["addr"] and r["addr"] not in cur["addrs"]:
                cur["addrs"].append(r["addr"])
        return {"ok": True, "ports": sorted(best.values(), key=lambda r: r["port"])}

    @staticmethod
    def _ss_rows() -> list[dict[str, Any]] | None:
        """`ss -ltnpH`, one row per socket; None when ss failed."""
        res = subprocess.run(["ss", "-ltnpH"], text=True, capture_output=True, timeout=6)
        if res.returncode != 0:
            return None
        rows = []
        for line in (res.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            addr, _, port = parts[3].rpartition(":")
            if not port.isdigit():
                continue
            m = re.search(r'\("([^"]+)",pid=(\d+)', line)
            rows.append({"port": int(port), "addr": addr,
                         "proc": m.group(1) if m else "",
                         "pid": int(m.group(2)) if m else 0})
        return rows

    @staticmethod
    def _lsof_rows() -> list[dict[str, Any]] | None:
        """`lsof` for where there is no ss: one row per socket; None when it
        failed. `+c 0` keeps a command's whole name, and lsof writes a space
        in it as \\x20 ("LM\\x20Studio"). Exit 1 with nothing on stderr is
        lsof's "no such sockets" — nothing listens."""
        res = subprocess.run(["lsof", "+c", "0", "-nP", "-iTCP", "-sTCP:LISTEN"],
                             text=True, capture_output=True, timeout=6)
        if res.returncode != 0 and (res.returncode != 1 or (res.stderr or "").strip()):
            return None
        rows = []
        for line in (res.stdout or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 9 or not parts[1].isdigit():
                continue
            name = parts[-2] if parts[-1] == "(LISTEN)" else parts[-1]
            addr, _, port = name.rpartition(":")
            if not port.isdigit():
                continue
            rows.append({"port": int(port), "addr": addr,
                         "proc": parts[0].replace("\\x20", " "), "pid": int(parts[1])})
        return rows

    def processes(self) -> dict[int, dict[str, Any]] | None:
        """Every process on this machine: pid -> {ppid, rssKb, name}, the name
        being the executable's own (its path's last part). None when ps will
        not say — not "no processes".

        `ps -A` sees every user's processes where `ss -p` does not: an engine
        run as a service user (Ollama's installer makes one) shows here with
        its name and memory. On Linux the name is cut at 15 characters by the
        kernel; on macOS it is the whole path, spaces and all — so it is the
        last column.
        """
        try:
            res = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss=,comm="],
                                 text=True, capture_output=True, timeout=5)
        except Exception:
            return None
        if res.returncode != 0:
            return None
        table: dict[int, dict[str, Any]] = {}
        for line in (res.stdout or "").splitlines():
            parts = line.split(None, 3)
            if len(parts) < 4 or not (parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit()):
                continue
            table[int(parts[0])] = {"ppid": int(parts[1]), "rssKb": int(parts[2]),
                                    "name": parts[3].strip().rsplit("/", 1)[-1]}
        return table

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

    #: What nvidia-smi answers "who is on the card" with, in this order: the
    #: card, the process, its name (2.12) and the memory it holds.
    APPS_QUERY = "--query-compute-apps=gpu_uuid,pid,process_name,used_memory"

    #: How nvidia-smi says it cannot name a process (another PID namespace,
    #: a process gone between two reads).
    NO_PROCESS_NAME = ("[not found]", "[n/a]", "n/a", "[insufficient permissions]")

    @classmethod
    def nvidia_apps(cls) -> list[dict[str, Any]]:
        """Per-process GPU memory via nvidia-smi, so the admin can map a llama-server
        PID to the GPU(s) it occupies (many-to-many: N servers per GPU, or one
        server split across N GPUs). Returns [{gpuUuid, pid, name, usedMiB}].

        `name` (2.12) is the process's executable, its path's last part, ""
        when nvidia-smi cannot name it: the board names the memory that is
        not a cell's — "ollama 6.1 GB" instead of "outside 6.1 GB". A name
        with a comma in it stays whole: the memory is the last column."""
        try:
            result = subprocess.run(
                ["nvidia-smi", cls.APPS_QUERY, "--format=csv,noheader,nounits"],
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
            if len(parts) < 4 or not parts[1].isdigit():
                continue
            name = ",".join(parts[2:-1]).strip()
            if name.lower() in cls.NO_PROCESS_NAME:
                name = ""
            apps.append({
                "gpuUuid": parts[0],
                "pid": int(parts[1]),
                "name": name.rsplit("/", 1)[-1],
                "usedMiB": int(parts[-1]) if parts[-1].isdigit() else 0,
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

    # The kernel's id of this boot; a class attribute so a test can stand in for it.
    BOOT_ID = "/proc/sys/kernel/random/boot_id"

    @classmethod
    def boot_id(cls) -> str:
        """This boot of the machine: a string that changes on every boot and on
        nothing else — the kernel's boot id on Linux, the boot time on macOS.
        "" when the machine will not say."""
        try:
            with open(cls.BOOT_ID, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            pass
        return cls.run_text(["sysctl", "-n", "kern.boottime"]).strip()

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
