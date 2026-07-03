"""LlamaNode process wrapper + _Slot (one managed server per port)."""
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


class LlamaNode:
    """Manages a local llama-server subprocess on this host's GPU.

    Lifecycle: start() → running → stop() or crash.
    Thread-safe: all state access is behind self._lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._adopted_pid: int | None = None  # re-attached process (not our child)
        self._cfg: dict[str, Any] = {}
        self._started_at: int = 0
        self._last_error: str = ""
        self._log_path: Path | None = None
        self._exit_info: dict[str, Any] | None = None

    @staticmethod
    def _read_log_error(log_path: Path | None) -> str:
        """Pull a concise crash reason from the tail of the llama-server log.

        Priority: corruption/OOM patterns first (most actionable), then any
        other error line, then last line as fallback.
        """
        if not log_path:
            return ""
        try:
            lines = [ln.rstrip() for ln in Path(log_path).read_text(
                encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        except Exception:
            return ""
        tail = lines[-80:]  # look further back than before
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
        for ln in reversed(tail):
            low = ln.lower()
            if any(p in low for p in priority):
                return ln[:300]
        # Fallback: any error/failure line
        markers = ("error", "abort", "failed", "invalid argument", "what()")
        for ln in reversed(tail):
            low = ln.lower()
            if any(m in low for m in markers) and "build:" not in low:
                return ln[:300]
        return (lines[-1][:300] if lines else "")

    @staticmethod
    def _rotate_log(log_path: Path | None, keep: int = 15) -> None:
        """Preserve the previous run's log instead of truncating it.

        llama-server's stdout/stderr is opened in "w" mode on every start, which
        wipes the log of a crashed run the moment the cell is relaunched (e.g. by
        an auto-restart or a route-agent redeploy). Before that happens, move an
        existing non-empty log aside to a timestamped backup
        (llama-server.<YYYYmmdd-HHMMSS>.log) so the crash can still be inspected.
        Keep only the most recent `keep` backups; never let logging block a start.
        """
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

    @staticmethod
    def _pid_alive(pid: int) -> bool:
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
            self._log_path = log_path
            self._exit_info = None
            return {"ok": True, "pid": int(pid), "adopted": True}

    def _running_locked(self) -> bool:
        if self._proc and self._proc.poll() is None:
            return True
        return bool(self._adopted_pid and self._pid_alive(self._adopted_pid))

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
                self._rotate_log(log_path)  # keep the crashed run's log, don't truncate it
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
                self._log_path = log_path
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
                self._rotate_log(log_path)
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
                self._log_path = log_path
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
                if self._pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                        deadline = time.time() + 10
                        while time.time() < deadline and self._pid_alive(pid):
                            time.sleep(0.3)
                        if self._pid_alive(pid):
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

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._adopted_pid:
                if self._pid_alive(self._adopted_pid):
                    return {
                        "running": True,
                        "pid": self._adopted_pid,
                        "adopted": True,
                        "startedAt": self._started_at,
                        "uptimeSec": int(time.time()) - self._started_at,
                        **{k: v for k, v in self._cfg.items() if k != "cmd"},
                    }
                # Died while adopted: no exit code is observable (not our child).
                err = self._read_log_error(self._log_path)
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
                err = self._last_error or self._read_log_error(self._log_path)
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


class _Slot:
    """One managed server process on this host, addressed by its port. A client
    can hold several at once (e.g. a translator + a whisper cell) — each keeps
    its own LlamaNode process, async startup progress and cache flag."""

    def __init__(self):
        self.node = LlamaNode()
        self.startup: dict[str, Any] = {"phase": "idle"}
        self.lock = threading.Lock()
        self.cache_models = False


