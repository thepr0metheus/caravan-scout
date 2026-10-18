"""A cell's process on this machine and the log it writes: how it is started
or adopted, how it stops, and what its log says when it dies."""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from caravan_scout.models import SplitGguf


class CellLog:
    """The log a cell's process writes.

    A new run moves the previous run's log aside instead of truncating it,
    and a dead process's log is read for the reason it died and for its last
    lines. A process started without a log has a CellLog of nothing: nothing
    is kept, nothing is read.

    What is read leaves the machine — the board shows it — so keys are
    scrubbed out of it first: the value goes, its name stays, and the line
    still reads. Only values that look like keys: llama.cpp logs
    "EOS token = 151645", and that number is not a secret.
    """

    # The last lines of a dead run, as many as the controller shows for its
    # own cells from their journal.
    TAIL_LINES = 8
    TAIL_CHARS = 1500
    #: What a debugger prints into a crashed server's log after the server's own message (llama.cpp's crash handler runs
    #: gdb): a frame ("#3  0x… in ggml_abort () from …"), a source file it cannot find ("warning: 360\t../csu/libc-start.c:
    #: No such file or directory."), the detach lines. None of it says why the server died, and its "abort", "error" and
    #: "no such file" were taken for the reason: a cell that died of "no kernel image" showed a libc source file on the
    #: board (2026-10-17).
    DEBUGGER_NOISE = re.compile(r"^\s*(?:#\d+\s+0x[0-9a-f]+\s|warning: \d+\s|\[Inferior \d+ \(process \d+\) detached\]"
                                r"|\[New LWP |\[Thread debugging |Using host libthread_db)")
    #: What a Python library prints AFTER the cause and which says nothing about it: pydantic ends every validation
    #: error with a pointer to its docs ("For further information visit https://errors.pydantic.dev/…"), and that line's
    #: "error" took the place of the cause the line above it states ("Value error, Unrecognized model in … model_type",
    #: "Please pass the argument `trust_remote_code=True`"): the board showed the pointer for two vLLM cells (2026-10-17).
    LIBRARY_FOOTERS = re.compile(r"^\s*(?:\([A-Za-z]+ pid=\d+\)\s+)?For further information visit https?://errors\.pydantic\.dev/")
    #: A line a program prints to say it is still alive: Strata's launcher says "[strata] still starting (461 s) - please wait ..."
    #: every 20 s while its engine loads. A process that was killed in the middle (systemd-oomd, 2026-10-18) leaves one of them
    #: last in its log, and that line was taken for the reason: the board said "still starting" of a cell that had died four times.
    PROGRESS_NOISE = re.compile(r"^\s*\[strata\]\s+still starting \(\d+ s\)")
    #: An exception line of a Python log: "(EngineCore pid=2087114) torch._inductor.exc.InductorError: AssertionError: …".
    EXCEPTION_LINE = re.compile(r"^\s*(?:\([A-Za-z]+ pid=\d+\)\s+)?(?:[A-Za-z_]\w*\.)*\w*(?:Error|Exception)\b:\s")
    #: An exception line that is not the cause itself. A wrapper says the cause stands ABOVE it: vLLM ends with
    #: "RuntimeError: Engine core initialization failed. See root cause above." — the cause is the exception line before
    #: (2026-10-17, a vLLM cell whose card said only that the engine core failed). A header says it stands BELOW it:
    #: pydantic's "ValidationError: 1 validation error for ModelConfig" is followed by the lines that say what is wrong.
    CAUSE_ELSEWHERE = re.compile(r"see root cause above|validation errors? for ", re.I)
    SECRETS = (
        # A key by its prefix, wherever it stands.
        (re.compile(r"\b(lcv1_|sk-|hf_|ghp_|glpat-)[A-Za-z0-9_\-]{6,}"), r"\1…"),
        (re.compile(r"(?i)\b(bearer)\s+[^\s\"',}]+"), r"\1 …"),
        # A value named as secret: whatever it is.
        (re.compile(r"(?i)\b((?:[a-z0-9]+_)*(?:api[_-]?key|password|passwd|secret))\b(\"?\s*[=:]\s*\"?|\s+)"
                    r"[^\s\"',}]+"),
         r"\1\2…"),
        # A token: only a key-shaped value — "EOS token = 151645" stays.
        (re.compile(r"(?i)\b((?:[a-z0-9]+_)*(?:token|authorization))\b(\"?\s*[=:]\s*\"?|\s+)"
                    r"[A-Za-z0-9_\-.~+/=]{12,}"),
         r"\1\2…"),
    )

    def __init__(self, path):
        self.path = path

    @classmethod
    def scrub(cls, text: str) -> str:
        """`text` without the keys in it."""
        for pattern, kept in cls.SECRETS:
            text = pattern.sub(kept, text)
        return text

    def tail(self) -> str:
        """The last lines the process wrote, keys scrubbed — what the card
        shows on hover. Empty when there is no log or nothing in it."""
        if not self.path:
            return ""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 64 * 1024))
                text = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()][-self.TAIL_LINES:]
        return self.scrub("\n".join(lines))[-self.TAIL_CHARS:]

    def crash_reason(self) -> str:
        """The reason the process died, keys scrubbed (see _reason)."""
        return self.scrub(self._reason())

    def _reason(self) -> str:
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
        # look further back than before; what only follows the cause (a debugger's lines, a library's footer) is no cause
        tail = [ln for ln in lines[-80:] if not (self.DEBUGGER_NOISE.match(ln) or self.LIBRARY_FOOTERS.match(ln) or self.PROGRESS_NOISE.match(ln))]
        # llama.cpp: "0.00.817.809 W load: …" — capture the level per line.
        _lvl = re.compile(r"^\s*[\d.]+\s+([IWED])\s")
        levels = {ln: (_lvl.match(ln).group(1) if _lvl.match(ln) else "") for ln in tail}
        levelled = any(levels.values())
        def _skip(ln):
            """True for a line that cannot be a crash reason on a levelled log."""
            return levelled and levels.get(ln) in ("I", "W")
        # High-priority: actionable patterns the UI can classify into friendly messages
        priority = (
            "invalid ggml type",
            "unsupported quantization",
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
            # The engine has no code for the card it runs on (a build for the RTX 5090 alone on an RTX 3090): the fix
            # is a build for that card, and the message says nothing else.
            "no kernel image",
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
        # A Python log has no levels: its cause is the last exception line — unless that line only says where the cause
        # stands (CAUSE_ELSEWHERE): then the one before it, or, for a header, the generic reading below.
        if not levelled:
            for ln in reversed(tail):
                if self.EXCEPTION_LINE.match(ln) and not self.CAUSE_ELSEWHERE.search(ln):
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
        return "" if levelled else (tail[-1][:300] if tail else "")

    def open_new(self):
        """The file this run writes to, opened fresh: its folder made first,
        and the previous run's log kept aside (rotate). DEVNULL without a path.

        The logs live in this scout's model cache, and only a download used
        to make that folder: a scout that reads every model in place had
        none, and its first cell died opening its log ("No such file or
        directory") before it ever ran."""
        if not self.path:
            return subprocess.DEVNULL
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.rotate()
        return open(self.path, "w")

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


class MemoryScope:
    """The memory limits a cell had from the controller's systemd unit
    (MemoryHigh 70 %, MemoryMax 80 %, swap 2 GB): a model that eats the RAM
    dies alone instead of taking the machine with it. That unit went in the
    controller's step 6.9 — its machine's cells run through its scout too —
    so this is the values' one home.

    On Linux with a user systemd a cell is launched in its own transient
    scope with those limits: `systemd-run --user --scope` registers the scope
    for itself and then execs the command, so the pid is the cell's, its
    environment (the scout's marker) is kept, and it lives outside the
    scout's cgroup. Elsewhere — macOS, no user manager, no memory controller
    for it — the cell runs as it did, without limits, and the journal says so
    once: a limit that is not there must not read as one that is.

    The probe launches what a cell would be launched with and reads the
    scope's own memory.max: systemd accepts MemoryMax even where the memory
    controller is not delegated to the user manager, and then nothing limits
    the cell.
    """

    LIMITS = ("MemoryHigh=70%", "MemoryMax=80%", "MemorySwapMax=2G")
    # Run inside the scope: the memory.max of the cgroup it runs in.
    READ_LIMIT = 'cat "/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)/memory.max"'
    _usable: bool | None = None

    @classmethod
    def usable(cls) -> bool:
        """Asked once per scout run; the answer goes to the journal."""
        if cls._usable is None:
            cls._usable, why = cls.probe()
            print(f"[cells] {why}", flush=True)
        return cls._usable

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        """Whether a cell launched here gets the limits, and why, for the journal."""
        if not sys.platform.startswith("linux"):
            return False, "cells run without memory limits: the limits come from systemd, and this is not Linux"
        if not shutil.which("systemd-run"):
            return False, "cells run without memory limits: systemd-run is not installed"
        try:
            done = subprocess.run(cls.command(["sh", "-c", cls.READ_LIMIT]),
                                  capture_output=True, text=True, timeout=10)
        except Exception as exc:
            return False, f"cells run without memory limits: systemd-run --user did not answer ({exc})"
        said = (done.stdout or "").strip()
        if done.returncode != 0 or not said.isdigit():
            errors = (done.stderr or "").strip().splitlines()
            why = errors[-1] if errors else (f"memory.max = {said}" if said else f"exit {done.returncode}")
            return False, f"cells run without memory limits: a user scope gets none here ({why})"
        return True, f"cells run in their own scope: {' '.join(cls.LIMITS)} (MemoryMax {int(said) / 1e9:.1f} GB)"

    @classmethod
    def command(cls, argv: list[str]) -> list[str]:
        """`argv` in a transient user scope with the limits."""
        limits = [arg for limit in cls.LIMITS for arg in ("-p", limit)]
        return ["systemd-run", "--user", "--scope", "--quiet", "--collect", *limits, "--", *argv]

    @classmethod
    def wrap(cls, argv: list[str]) -> list[str]:
        """The command as it is launched: in a limited scope when one can be made."""
        return cls.command(argv) if cls.usable() else list(argv)


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
        # How it was last launched — argv, the extra environment, the log —
        # so a crash can be followed by the same launch (Watchdog), also after
        # a scout restart: the record keeps it (CellRecords).
        self._launch: dict[str, Any] | None = None

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
              started_at: int = 0, launch: dict[str, Any] | None = None) -> dict[str, Any]:
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
            self._launch = dict(launch) if isinstance(launch, dict) and launch.get("argv") else None
            return {"ok": True, "pid": int(pid), "adopted": True}

    def _running_locked(self) -> bool:
        if self._proc and self._proc.poll() is None:
            return True
        return bool(self._adopted_pid and self.pid_alive(self._adopted_pid))

    def start(self, bin_path: str, args: list[str], cfg: dict[str, Any],
              log_path: Path | None = None,
              extra_env: dict[str, str] | None = None) -> dict[str, Any]:
        """Launch llama-server. `args` is the full token list after the binary
        (already includes --model/--host/--port). `cfg` is metadata surfaced by
        status() (modelPath, port, gpuLayers, ctxSize). `extra_env` is set on
        the process over the scout's own environment, and kept with the
        launch, so a restart after a crash starts it the same way."""
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
            try:
                log_fh = CellLog(log_path).open_new()
                self._proc = subprocess.Popen(
                    MemoryScope.wrap(cmd),
                    stdout=log_fh,
                    stderr=subprocess.STDOUT if log_path else subprocess.DEVNULL,
                    close_fds=True,
                    env=HostProcesses.cell_env(cfg.get("port"), extra_env),
                )
                self._cfg = {**cfg, "cmd": cmd}
                self._started_at = int(time.time())
                self._last_error = ""
                self._log = CellLog(log_path)
                self._exit_info = None
                self._launch = {"argv": [str(a) for a in cmd], "extraEnv": dict(extra_env or {}),
                                "log": str(log_path or "")}
                return {"ok": True, "pid": self._proc.pid, "port": cfg.get("port")}
            except Exception as exc:
                self._last_error = str(exc)
                self._proc = None
                return {"ok": False, "error": str(exc)}

    def start_command(self, shell_command: str, cfg: dict[str, Any],
                      log_path: Path | None = None,
                      extra_env: dict[str, str] | None = None) -> dict[str, Any]:
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
            try:
                log_fh = CellLog(log_path).open_new()
                self._proc = subprocess.Popen(
                    MemoryScope.wrap(cmd),
                    stdout=log_fh,
                    stderr=subprocess.STDOUT if log_path else subprocess.DEVNULL,
                    close_fds=True,
                    env=HostProcesses.cell_env(cfg.get("port"), extra_env),
                )
                self._cfg = {**cfg, "cmd": cmd}
                self._started_at = int(time.time())
                self._last_error = ""
                self._log = CellLog(log_path)
                self._exit_info = None
                self._launch = {"argv": list(cmd), "extraEnv": dict(extra_env or {}), "log": str(log_path or "")}
                return {"ok": True, "pid": self._proc.pid, "port": cfg.get("port")}
            except Exception as exc:
                self._last_error = str(exc)
                self._proc = None
                return {"ok": False, "error": str(exc)}

    def launch_spec(self) -> dict[str, Any] | None:
        """How this cell was last launched, or None when it never was here."""
        with self._lock:
            return dict(self._launch) if self._launch else None

    def relaunch(self) -> dict[str, Any]:
        """The same launch again, after a crash: same argv, environment and
        log (the crashed run's log is kept aside, as on every start)."""
        with self._lock:
            if self._running_locked():
                return {"ok": False, "error": "the cell is running"}
            spec = self._launch
            if not spec or not spec.get("argv"):
                return {"ok": False, "error": "no launch to repeat — it was started before this scout kept one"}
            log_path = Path(spec["log"]) if spec.get("log") else None
            try:
                log_fh = CellLog(log_path).open_new()
                self._proc = subprocess.Popen(
                    MemoryScope.wrap(list(spec["argv"])),
                    stdout=log_fh,
                    stderr=subprocess.STDOUT if log_path else subprocess.DEVNULL,
                    close_fds=True,
                    env=HostProcesses.cell_env(self._cfg.get("port"), spec.get("extraEnv")),
                )
            except Exception as exc:
                self._last_error = str(exc)
                self._proc = None
                return {"ok": False, "error": str(exc)}
            self._adopted_pid = None
            self._started_at = int(time.time())
            self._last_error = ""
            self._log = CellLog(log_path)
            self._exit_info = None
            return {"ok": True, "pid": self._proc.pid}

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

    def log_tail(self) -> str:
        """The last lines of this cell's log (CellLog.tail)."""
        with self._lock:
            log = self._log
        return log.tail()

    #: The files a launch holds, each under its role on the board's card.
    LAUNCH_FILES = (("modelPath", "model"), ("mmprojPath", "mmproj"), ("specPath", "draft"))

    def held_files(self) -> list[str]:
        """The model files this process holds while it runs — what a cache
        purge must leave alone: the launch files, the rest a command cell was
        sent (`files`), and every part of a split GGUF among them (SplitGguf:
        the engine opens them by name). A process that does not run holds none."""
        if not self.status().get("running"):
            return []
        cfg = self._cfg
        named = [cfg[k] for k, _role in self.LAUNCH_FILES if cfg.get(k)]
        named += [str(f) for f in cfg.get("files") or [] if f]
        return [part for path in named for part in SplitGguf(path).parts()]

    def changed_since(self, started_at: int) -> list[str]:
        """The roles of the launch files that changed on disk after the process
        started at `started_at` — the weights, the projector, the draft.

        A running process holds the files it opened (their inodes), not their
        names: a file replaced under it reaches the cell only on a restart, and
        the board says so (⟳). The controller measured this for its own cells
        from the unit's start time; since its step 6.9 every cell is a scout's,
        and this machine is where the files are. A file that cannot be read has
        not "changed"; a folder (a checkpoint) changed when a file in it did."""
        started = int(started_at or 0)
        if not started:
            return []
        cfg = self._cfg
        return [role for key, role in self.LAUNCH_FILES
                if cfg.get(key) and self.newest_mtime(cfg[key]) > started]

    @staticmethod
    def newest_mtime(path: str) -> int:
        """The file's mtime; for a folder, its newest file's (or its own, empty).
        0 when it cannot be read."""
        target = Path(str(path)).expanduser()
        try:
            if target.is_dir():
                times = [int(entry.stat().st_mtime) for entry in os.scandir(target) if entry.is_file()]
                return max(times) if times else int(target.stat().st_mtime)
            return int(target.stat().st_mtime)
        except OSError:
            return 0

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
                if self._cfg.get("runner") == "prism":
                    self._exit_info["runner"] = "prism"
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
                if self._cfg.get("runner") == "prism":
                    self._exit_info["runner"] = "prism"
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

    # Every cell this scout starts carries it in its environment, and keeps it
    # through `bash -lc … exec python`. A process without it was not started
    # by a scout: on a machine the scout shares — the controller's own cells,
    # a llama-server run by hand — it is never adopted and never killed.
    CELL_ENV = "CARAVAN_SCOUT_CELL"
    PROC = Path("/proc")            # a class attribute so a test can stand in for it

    @classmethod
    def cell_env(cls, port: Any, extra: dict[str, str] | None = None) -> dict[str, str]:
        """A cell's environment: this scout's own, what its start adds over it,
        and the marker last — nothing a start carries can take the marker away
        and leave the scout a cell it would no longer know as its own."""
        return {**os.environ, **dict(extra or {}), cls.CELL_ENV: str(port or "")}

    @classmethod
    def owned(cls, pid: int) -> bool | None:
        """Whether a scout started `pid`: True or False, or None when the
        machine will not say (then it counts as not ours for anything that
        kills, and as before for adopting)."""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        if pid <= 1:
            return None
        environ = cls.PROC / str(pid) / "environ"
        if cls.PROC.is_dir():
            try:
                return any(v.startswith(cls.CELL_ENV.encode() + b"=")
                           for v in environ.read_bytes().split(b"\0"))
            except FileNotFoundError:
                return False       # gone
            except OSError:
                return None
        try:                       # macOS: ps -E adds the environment to the command
            out = subprocess.run(["ps", "-E", "-ww", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, timeout=5)
        except Exception:
            return None
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return f" {cls.CELL_ENV}=" in out.stdout

    @classmethod
    def port_of(cls, pid: Any) -> int:
        """The port a scout started `pid` for — what CELL_ENV says in its
        environment — or 0: not a scout's, gone, unreadable, or a machine that
        keeps no /proc.

        This, not the command line, is how a cell is known again. A command
        cell's launcher is often an `exec` chain (bash → run_strata.sh →
        python server.py → the engine), each link rewriting argv, so the
        marker the record keeps from the controller's command line is not in
        what `ps` shows (2026-10-14: the marker of Strata's record carried a
        literal $HOME). The environment is kept through every exec and every
        child."""
        try:
            raw = (cls.PROC / str(int(pid)) / "environ").read_bytes()
        except (OSError, TypeError, ValueError):
            return 0
        prefix = cls.CELL_ENV.encode() + b"="
        for item in raw.split(b"\0"):
            if item.startswith(prefix):
                value = item[len(prefix):].decode("ascii", "ignore")
                return int(value) if value.isdigit() else 0
        return 0

    @classmethod
    def parent_of(cls, pid: Any) -> int:
        """The parent of `pid`, from /proc/<pid>/stat; 0 when it does not say."""
        try:
            fields = (cls.PROC / str(int(pid)) / "stat").read_text(errors="replace").rsplit(")", 1)[1].split()
            return int(fields[1])
        except (OSError, TypeError, ValueError, IndexError):
            return 0

    @classmethod
    def tagged_cells(cls) -> dict[int, list[int]]:
        """The cells of this machine that a scout started, found by the mark
        their processes carry: {port: [the pids at the root of the cell's
        tree]}. A cell is a tree — Strata's server and the engine under it
        both carry the mark — and its root is the one whose parent does not.
        {} where /proc does not say (macOS): the questions below then fall back
        to the port and the command line, as before."""
        if not cls.PROC.is_dir():
            return {}
        me = os.getpid()
        trees: dict[int, dict[int, int]] = {}
        try:
            names = [entry.name for entry in cls.PROC.iterdir() if entry.name.isdigit()]
        except OSError:
            return {}
        for name in names:
            pid = int(name)
            port = cls.port_of(pid) if pid != me else 0
            if port:
                trees.setdefault(port, {})[pid] = cls.parent_of(pid)
        return {port: sorted(pid for pid, parent in tree.items() if parent not in tree)
                for port, tree in trees.items()}

    @classmethod
    def started_at(cls, pid: Any) -> int:
        """When `pid` started, in epoch seconds, from /proc: the clock ticks of
        its start since the boot, and the boot's own time. 0 when /proc does not
        say — a cell found again without a record is then as old as its
        adoption."""
        try:
            ticks = int((cls.PROC / str(int(pid)) / "stat").read_text(errors="replace")
                        .rsplit(")", 1)[1].split()[19])
            booted = next(int(line.split()[1]) for line in (cls.PROC / "stat").read_text().splitlines()
                          if line.startswith("btime "))
            return booted + ticks // os.sysconf("SC_CLK_TCK")
        except (OSError, StopIteration, TypeError, ValueError, IndexError, AttributeError):
            return 0

    @classmethod
    def carrier(cls, port: Any, prefer: Any = 0) -> int:
        """The process a scout started for `port` that still runs, listening
        or not yet — a cell loading its model does not listen for a minute and
        more. `prefer` when it is one of them, else the first root; 0 when no
        process carries the mark of that port."""
        port = int(port)
        try:
            want = int(prefer or 0)
        except (TypeError, ValueError):
            want = 0
        if want > 1 and cls.port_of(want) == port:
            return want
        roots = cls.tagged_cells().get(port) or []
        return roots[0] if roots else 0

    #: How many processes a cell's tree may name: a server with its engine and
    #: helpers has a handful, and a runaway fork is not walked.
    TREE_LIMIT = 64

    @classmethod
    def descendants(cls, pid: Any) -> list[int]:
        """The processes `pid` started, and theirs, read from
        /proc/<pid>/task/<tid>/children; [] where /proc does not say (macOS,
        a kernel without the file, a process gone).

        A cell's server can hand the card to a child: Strata's server.py runs
        its engine as one. The card's memory was the child's, so the board
        found none for the cell and called it a CPU cell, with 31 GB of VRAM
        in use (2026-10-13). The controller binds the card to these too
        (lama-caravan reads them as the cell's `pids`)."""
        try:
            root = int(pid)
        except (TypeError, ValueError):
            return []
        if root <= 1:
            return []
        found: list[int] = []
        todo = [root]
        while todo and len(found) < cls.TREE_LIMIT:
            parent = todo.pop(0)
            tasks = cls.PROC / str(parent) / "task"
            try:
                files = sorted(tasks.glob("*/children"))
            except OSError:
                continue
            for children in files:
                try:
                    kids = [int(x) for x in children.read_text().split()]
                except (OSError, ValueError):
                    continue
                for kid in kids:
                    if kid != root and kid not in found and len(found) < cls.TREE_LIMIT:
                        found.append(kid)
                        todo.append(kid)
        return found

    @classmethod
    def resident(cls, pid: Any, table: dict[int, dict[str, Any]] | None) -> int | None:
        """The memory a cell's processes hold in RAM, in bytes: the RSS of the process `pid` and of everything it started (the server's engine,
        vLLM's workers), read from one `ps` table (`Machine.processes()`: pid -> {ppid, rssKb, name}) — the figure an engine's `ramBytes`
        is. None when there is no table (ps did not answer) or the table does not hold the process: "not told" is not "nothing".

        RSS counts the memory-mapped model files a process has touched: the weights of a CPU cell are in it, and a file that two cells map
        is in both of them. The tree is walked by parent pid, so it works where /proc does not (macOS), and is cut at TREE_LIMIT as
        `descendants` is."""
        try:
            root = int(pid)
        except (TypeError, ValueError):
            return None
        if not table or root not in table:
            return None
        children: dict[int, list[int]] = {}
        for child, row in table.items():
            children.setdefault(int(row.get("ppid") or 0), []).append(child)
        found: list[int] = []
        todo = [root]
        while todo and len(found) < cls.TREE_LIMIT + 1:
            current = todo.pop(0)
            if current in found:
                continue
            found.append(current)
            todo.extend(children.get(current, []))
        return sum(int(table[p].get("rssKb") or 0) for p in found) * 1024

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
