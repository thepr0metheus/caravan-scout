"""A cell's process on this machine and the log it writes: how it is started
or adopted, how it stops, and what its log says when it dies."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any


class CellLog:
    """The log a cell's process writes.

    A new run moves the previous run's log aside instead of truncating it,
    and a dead process's log is read for the reason it died. A process
    started without a log has a CellLog of nothing: nothing is kept, nothing
    is read.
    """

    def __init__(self, path):
        self.path = path

    def crash_reason(self) -> str:
        """Pull a concise crash reason from the tail of the cell's log.

        Priority: corruption/OOM patterns first (most actionable), then any
        other error line, then last line as fallback.

        LEVEL-AWARE. llama.cpp prefixes each line "<time> <LEVEL> <subsys>: …"
        where LEVEL is one of I/W/E. Reading the last line blindly turned a
        benign "W load: control-looking token … its type will be overridden"
        into "Model loading failed" on the card. When the tail carries levels at
        all, informational and warning lines are skipped — and the blind
        last-line fallback is dropped, because on a levelled log the last line
        is usually chatter, not a cause. Command cells (whisper, moonshine, tts)
        print plain bash/python output with no level prefix; for those nothing
        changes and the fallback still applies, or a real traceback would vanish.
        """
        log_path = self.path
        if not log_path:
            return ""
        try:
            lines = [ln.rstrip() for ln in Path(log_path).read_text(
                encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        except Exception:
            return ""
        tail = lines[-80:]  # look further back than before
        # llama.cpp: "0.00.817.809 W load: …" — capture the level per line.
        _lvl = re.compile(r"^\s*[\d.]+\s+([IWED])\s")
        levels = {ln: (_lvl.match(ln).group(1) if _lvl.match(ln) else "") for ln in tail}
        levelled = any(levels.values())
        def _skip(ln):
            """True for a line that cannot be a crash reason on a levelled log."""
            return levelled and levels.get(ln) in ("I", "W")
        # High-priority: actionable patterns the UI can classify into friendly messages
        priority = (
            "not within the file bounds",
            "corrupted or incomplete",
            "unexpected end of file",
            "out of memory",
            "cudaerroromemoryallocation",
            "failed to allocate",
            "not enough memory",
            "mismatch between text model",
            "wrong mmproj",
            "mtmd_init_from_file",
            "no such file",
            "failed to open",
        )
        # NOT level-filtered on purpose. These patterns are unambiguous failure
        # signatures — nothing benign says "corrupted or incomplete" — and the
        # corrupted-download auto-repair (ModelFetcher.is_corruption_error) reads
        # this very return value. Dropping one because a build happened to log it
        # at W would cost a self-healing download to save nothing.
        for ln in reversed(tail):
            low = ln.lower()
            if any(p in low for p in priority):
                return ln[:300]
        # Fallback: any error/failure line
        markers = ("error", "abort", "failed", "invalid argument", "what()")
        for ln in reversed(tail):
            if _skip(ln):
                continue
            low = ln.lower()
            if any(m in low for m in markers) and "build:" not in low:
                return ln[:300]
        # A crash with no level prefix at all (C++ terminate, a python traceback)
        # still deserves to be surfaced even though it matched nothing above.
        catastrophes = ("terminate called", "what():", "traceback", "cuda error",
                        "segmentation fault", "killed")
        for ln in reversed(tail):
            if any(c in ln.lower() for c in catastrophes):
                return ln[:300]
        # Levelled log with nothing but I/W left: we genuinely do not know why it
        # went. Say nothing rather than blame the last harmless line — the board
        # renders an unexplained failure as such.
        return "" if levelled else (lines[-1][:300] if lines else "")

    def rotate(self, keep: int = 15) -> None:
        """Preserve the previous run's log instead of truncating it.

        llama-server's stdout/stderr is opened in "w" mode on every start, which
        wipes the log of a crashed run the moment the cell is relaunched (e.g. by
        an auto-restart or a route-agent redeploy). Before that happens, move an
        existing non-empty log aside to a timestamped backup
        (llama-server.<YYYYmmdd-HHMMSS>.log) so the crash can still be inspected.
        Keep only the most recent `keep` backups; never let logging block a start.
        """
        log_path = self.path
        if not log_path:
            return
        try:
            p = Path(log_path)
            if p.exists() and p.stat().st_size > 0:
                ts = time.strftime("%Y%m%d-%H%M%S")
                backup = p.with_name(f"{p.stem}.{ts}{p.suffix}")
                n = 1
                while backup.exists():  # >1 start within the same second
                    backup = p.with_name(f"{p.stem}.{ts}-{n}{p.suffix}")
                    n += 1
                p.rename(backup)
            backups = sorted(
                p.parent.glob(f"{p.stem}.*{p.suffix}"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            for old in backups[keep:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except Exception:
            pass


class CellProcess:
    """The process of one cell: a llama-server, or whatever a command cell
    runs. It knows nothing of ports or phases — only its process.

    Lifecycle: start()/start_command() or adopt() → running → stop() or crash.
    Thread-safe: all state access is behind self._lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._adopted_pid: int | None = None  # re-attached process (not our child)
        self._cfg: dict[str, Any] = {}
        self._started_at: int = 0
        self._last_error: str = ""
        self._log = CellLog(None)
        self._exit_info: dict[str, Any] | None = None

    @staticmethod
    def pid_alive(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False

    # ── public API ──────────────────────────────────────────────────────────

    def adopt(self, pid: int, cfg: dict[str, Any], log_path: Path | None = None,
              started_at: int = 0) -> dict[str, Any]:
        """Re-attach to a server that survived an agent restart (KillMode=process /
        AbandonProcessGroup). The process is NOT our child, so it is managed by
        pid: liveness via kill(pid, 0), stop via SIGTERM→SIGKILL."""
        with self._lock:
            self._proc = None
            self._adopted_pid = int(pid)
            self._cfg = dict(cfg or {})
            self._started_at = int(started_at) or int(time.time())
            self._last_error = ""
            self._log = CellLog(log_path)
            self._exit_info = None
            return {"ok": True, "pid": int(pid), "adopted": True}

    def _running_locked(self) -> bool:
        if self._proc and self._proc.poll() is None:
            return True
        return bool(self._adopted_pid and self.pid_alive(self._adopted_pid))

    def start(self, bin_path: str, args: list[str], cfg: dict[str, Any],
              log_path: Path | None = None) -> dict[str, Any]:
        """Launch llama-server. `args` is the full token list after the binary
        (already includes --model/--host/--port). `cfg` is metadata surfaced by
        status() (modelPath, port, gpuLayers, ctxSize)."""
        with self._lock:
            if self._running_locked():
                return {"ok": False, "error": "llama-server is already running",
                        "port": self._cfg.get("port")}
            self._adopted_pid = None
            bp = Path(bin_path).expanduser()
            if not bp.exists():
                return {"ok": False,
                        "error": f"llama-server binary not found: {bp}"}
            model_path = str(cfg.get("modelPath") or "")
            if model_path and not Path(model_path).exists():
                return {"ok": False, "error": f"model file not found: {model_path}"}
            cmd = [str(bp), *[str(a) for a in args]]
            if log_path:
                CellLog(log_path).rotate()  # keep the crashed run's log, don't truncate it
            try:
                log_fh = open(log_path, "w") if log_path else subprocess.DEVNULL
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT if log_path else subprocess.DEVNULL,
                    close_fds=True,
                )
                self._cfg = {**cfg, "cmd": cmd}
                self._started_at = int(time.time())
                self._last_error = ""
                self._log = CellLog(log_path)
                self._exit_info = None
                return {"ok": True, "pid": self._proc.pid, "port": cfg.get("port")}
            except Exception as exc:
                self._last_error = str(exc)
                self._proc = None
                return {"ok": False, "error": str(exc)}

    def start_command(self, shell_command: str, cfg: dict[str, Any],
                      log_path: Path | None = None) -> dict[str, Any]:
        """Launch a generic command cell via bash. `shell_command` is a full
        shell line that sets $PORT and `exec`s the real process, so the tracked
        PID is the server itself, not bash. Managed exactly like a llama-server
        process so status()/stop() keep working unchanged."""
        with self._lock:
            if self._running_locked():
                return {"ok": False, "error": "a process is already running",
                        "port": self._cfg.get("port")}
            self._adopted_pid = None
            cmd = ["bash", "-lc", shell_command]
            if log_path:
                CellLog(log_path).rotate()
            try:
                log_fh = open(log_path, "w") if log_path else subprocess.DEVNULL
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT if log_path else subprocess.DEVNULL,
                    close_fds=True,
                )
                self._cfg = {**cfg, "cmd": cmd}
                self._started_at = int(time.time())
                self._last_error = ""
                self._log = CellLog(log_path)
                self._exit_info = None
                return {"ok": True, "pid": self._proc.pid, "port": cfg.get("port")}
            except Exception as exc:
                self._last_error = str(exc)
                self._proc = None
                return {"ok": False, "error": str(exc)}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            # Always clear any prior crash/error so a failed node can be dismissed
            # (stop/delete) without a full agent restart — otherwise status() keeps
            # reporting phase="error" and the cell can't be removed from the UI.
            self._last_error = ""
            self._exit_info = {}
            if self._adopted_pid:
                pid = self._adopted_pid
                self._adopted_pid = None
                self._cfg = {}
                self._started_at = 0
                if self.pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                        deadline = time.time() + 10
                        while time.time() < deadline and self.pid_alive(pid):
                            time.sleep(0.3)
                        if self.pid_alive(pid):
                            os.kill(pid, signal.SIGKILL)
                    except Exception as exc:
                        return {"ok": False, "error": str(exc)}
                return {"ok": True, "adopted": True}
            if not self._proc or self._proc.poll() is not None:
                self._proc = None
                self._cfg = {}
                self._started_at = 0
                return {"ok": True, "detail": "not running"}
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            self._proc = None
            self._cfg = {}
            self._started_at = 0
            return {"ok": True}

    def held_files(self) -> list[str]:
        """The model files this process holds while it runs — what a cache
        purge must leave alone. A process that does not run holds none."""
        if not self.status().get("running"):
            return []
        cfg = self._cfg
        return [cfg[k] for k in ("modelPath", "mmprojPath", "specPath") if cfg.get(k)]

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._adopted_pid:
                if self.pid_alive(self._adopted_pid):
                    return {
                        "running": True,
                        "pid": self._adopted_pid,
                        "adopted": True,
                        "startedAt": self._started_at,
                        "uptimeSec": int(time.time()) - self._started_at,
                        **{k: v for k, v in self._cfg.items() if k != "cmd"},
                    }
                # Died while adopted: no exit code is observable (not our child).
                err = self._log.crash_reason()
                self._exit_info = {"exitCode": None, "lastError": err, "crashed": True}
                self._adopted_pid = None
                return {"running": False, **self._exit_info}
            if not self._proc:
                st: dict[str, Any] = {"running": False}
                if self._exit_info:
                    st.update(self._exit_info)
                if self._last_error and not st.get("lastError"):
                    st["lastError"] = self._last_error
                return st
            rc = self._proc.poll()
            if rc is not None:
                # Process exited (e.g. crashed during model/clip load). Capture
                # the reason from the log so the admin can show it.
                err = self._last_error or self._log.crash_reason()
                self._exit_info = {"exitCode": rc, "lastError": err, "crashed": rc != 0}
                self._proc = None
                return {"running": False, **self._exit_info}
            return {
                "running": True,
                "pid": self._proc.pid,
                "startedAt": self._started_at,
                "uptimeSec": int(time.time()) - self._started_at,
                **{k: v for k, v in self._cfg.items() if k != "cmd"},
            }


class HostProcesses:
    """A process on this machine that the scout did not start — found by the
    command line it runs, or by the port it listens on and answers.

    This is how a cell outlives a scout restart: the registry remembers a pid
    and a marker, and these questions check that memory against the host.
    Each is one question to the OS; none of them keeps anything.
    """

    @staticmethod
    def marker_matches(marker: str, cmdline: str) -> bool:
        """The exec'd argv[0] may be a resolved binary path (python3 → .../MacOS/Python),
        so besides the exact substring also accept the marker's argument tail."""
        if not marker or not cmdline:
            return False
        if marker in cmdline:
            return True
        tail = " ".join(marker.split()[1:])
        return bool(tail) and tail in cmdline

    @staticmethod
    def cmdline(pid: int) -> str:
        try:
            out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                                 capture_output=True, text=True, timeout=5)
            return out.stdout.strip()
        except Exception:
            return ""

    @staticmethod
    def listener(port: int) -> int:
        """PID LISTENING on <port> (any local address), via `ss`; 0 if none. Lets us
        re-identify a cell by its port when the launch marker no longer matches: a
        wrapper that exec's into another program (run_whisper.sh → exec python)
        rewrites argv, so the marker is gone from ps though the port is still served."""
        want = str(int(port))
        try:
            out = subprocess.run(["ss", "-ltnpH"], capture_output=True,
                                 text=True, timeout=5).stdout
        except FileNotFoundError:
            # macOS has no ss; lsof answers the same question. Without this the
            # mac client silently returned 0 here — port-based re-adoption and
            # the stop-time port check both degraded to "nobody listening".
            try:
                out2 = subprocess.run(
                    ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
                    capture_output=True, text=True, timeout=5).stdout.strip()
                return int(out2.split()[0]) if out2 else 0
            except Exception:
                return 0
        except Exception:
            return 0
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[3].rsplit(":", 1)[-1] != want:
                continue  # parts[3] is the Local Address:Port column
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
        return 0

    @staticmethod
    def healthy(port: int, timeout: float = 2.0, attempts: int = 1,
                health_path: str = "/health") -> bool:
        """True if the server on <port> answers its health endpoint with 2xx —
        confirming a real, healthy cell serves the port before we adopt whatever
        pid owns it.

        The path is NOT always /health: the controller computes it per cell (a
        vLLM cell answers on /v1/models, a command cell can declare its own) and
        sends it with the start request. Probing a hardcoded /health would call a
        perfectly healthy vLLM cell dead.

        Retries because this runs at agent startup, which is exactly when the host
        is busiest — a single 2 s probe on a loaded box times out on a cell that is
        perfectly alive."""
        path = str(health_path or "/health").strip() or "/health"
        if not path.startswith("/"):
            path = "/" + path
        for i in range(max(1, int(attempts))):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{int(port)}{path}", timeout=timeout) as resp:
                    return 200 <= int(getattr(resp, "status", 200) or 200) < 300
            except Exception:
                if i + 1 < attempts:
                    time.sleep(1.0)
        return False
