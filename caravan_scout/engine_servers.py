"""The servers of the engines next to the cells — Ollama, LM Studio — as
programs the board turns on and off (2.16).

Looking at an engine (engines.py) never touches its process. Starting and
stopping it is an explicit act from the board, and only for a server this
scout's user runs: another user's server — a system service — is named, and
left to the operator. A server stopped from here, or never seen running but
installed in the user's home, stays on the board as a stopped engine, so it
can be started again; one started from here starts again when the machine
boots, until it is stopped from here.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


class EngineProcs:
    """The machine's side of an engine's server: what a process is (Linux
    /proc), starting one detached from the scout, stopping one. Where /proc
    does not say, nothing is known of a process, and nothing is offered.
    A test stands in for all of it."""

    PROC = Path("/proc")
    #: The environment mark of a server this scout started. Not a cell's
    #: mark (CARAVAN_SCOUT_CELL): the stray-cell reaper must never take a
    #: server for a lost cell.
    ENGINE_ENV = "CARAVAN_SCOUT_ENGINE"
    CELL_ENV = "CARAVAN_SCOUT_CELL"

    def __init__(self, logs: Path, sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None):
        self.logs = Path(logs)
        # Looked up at each call, so a patched clock or sleep is seen.
        self.sleep = sleep or (lambda sec: time.sleep(sec))
        self.clock = clock or (lambda: time.time())

    @staticmethod
    def uid() -> int | None:
        return os.getuid() if hasattr(os, "getuid") else None

    def info(self, pid: Any) -> dict[str, Any] | None:
        """{uid, exe, args, env, marked} of a process; None when /proc does
        not say. The binary and environment only for the scout's own user —
        the machine keeps another user's to itself."""
        try:
            base = self.PROC / str(int(pid))
            uid = base.stat().st_uid
        except (TypeError, ValueError, OSError):
            return None
        info: dict[str, Any] = {"uid": uid, "exe": "", "args": [], "env": {}, "marked": False}
        try:
            info["args"] = [a.decode("utf-8", "replace") for a in (base / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            pass
        try:
            info["exe"] = os.readlink(base / "exe")
        except OSError:
            pass
        try:
            env = {}
            for item in (base / "environ").read_bytes().split(b"\0"):
                key, sep, value = item.decode("utf-8", "replace").partition("=")
                if sep:
                    env[key] = value
            info["env"] = env
            info["marked"] = self.ENGINE_ENV in env
        except OSError:
            pass
        return info

    def spawn(self, argv: list[str], env: dict[str, str], kind: str) -> str:
        """Start a server in a session of its own — a scout restart leaves it
        running, as it leaves the cells — its output appended to
        <logs>/<kind>.log, with the scout's environment and the recipe's on
        top, and this scout's mark. "" when it started, else why not."""
        base = {k: v for k, v in os.environ.items() if k != self.CELL_ENV}
        try:
            self.logs.mkdir(parents=True, exist_ok=True)
            with open(self.logs / f"{kind}.log", "ab") as log:
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 close_fds=True, start_new_session=True, cwd=str(Path.home()),
                                 env={**base, **env, self.ENGINE_ENV: kind})
        except OSError as exc:
            return f"{Path(str(argv[0])).name} did not start: {exc}"
        return ""

    def alive(self, pid: int) -> bool:
        """Whether `pid` runs. A server this scout started is its child: one
        that exited is reaped here, or it would linger as a zombie that
        signal 0 still finds."""
        try:
            done, _status = os.waitpid(int(pid), os.WNOHANG)
            if done:
                return False
        except ChildProcessError:
            pass
        except OSError:
            pass
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def children(self, pid: int) -> list[int]:
        """Every process under `pid` — a server's runners and helpers."""
        tree: dict[int, list[int]] = {}
        try:
            for entry in self.PROC.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    stat = (entry / "stat").read_text()
                    ppid = int(stat.rsplit(")", 1)[1].split()[1])
                except (OSError, ValueError, IndexError):
                    continue
                tree.setdefault(ppid, []).append(int(entry.name))
        except OSError:
            return []
        found, queue = [], [int(pid)]
        while queue:
            for child in tree.get(queue.pop(), []):
                if child not in found:
                    found.append(child)
                    queue.append(child)
        return found

    def terminate(self, pid: int, grace: float) -> str:
        """SIGTERM; one that outlives `grace` seconds is killed with all its
        children, or they would keep the cards' memory. "" when it is gone,
        else why not."""
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            return ""
        except PermissionError:
            return f"not allowed to stop process {pid}"
        deadline = self.clock() + grace
        while self.clock() < deadline:
            if not self.alive(pid):
                return ""
            self.sleep(0.25)
        for victim in [int(pid), *self.children(pid)]:
            try:
                os.kill(victim, signal.SIGKILL)
            except OSError:
                pass
        self.sleep(0.5)
        return "" if not self.alive(pid) else f"process {pid} would not stop"


class EngineServers:
    """The engines' servers the board can turn on and off, as this scout
    knows them — in state.json (`engineServers`): for each kind, how its
    server is started (learned from the run of this scout's user that it
    saw, or from where the kind installs itself in the home), its port, and
    whether it starts when the machine boots.

    What each engine's view gains (ForeignEngines.scan): `runBy` — "user"
    (this scout's user runs it), "other" (another user: a system service,
    the operator's to stop), "" (the machine does not say); `autostart`; and
    in `controls`, "stop" for a server this scout's user runs. A known
    server that is not running is a view of its own, `state: "stopped"`,
    whose only control is "start".
    """

    KEY = "engineServers"
    BOOT_KEY = "engineServersBoot"
    ACTIONS = ("start", "stop")

    def __init__(self, state, procs: EngineProcs, home: Path | None = None):
        self.state = state
        self.procs = procs
        # The home looked up when asked, not when the scout was built.
        self._home = home

    @property
    def home(self) -> Path:
        return Path(self._home or Path.home())

    def known(self) -> dict[str, dict[str, Any]]:
        rows = self.state.get(self.KEY)
        return rows if isinstance(rows, dict) else {}

    def remember(self, kind_id: str, **fields: Any) -> None:
        with self.state.lock:
            rows = self.state.setdefault(self.KEY, {})
            if not isinstance(rows, dict):
                rows = self.state[self.KEY] = {}
            before = dict(rows.get(kind_id) or {})
            rows[kind_id] = {**before, **fields}
            if rows[kind_id] != before:
                self.state.save()

    def annotate(self, kinds, views: list[dict[str, Any]], listeners: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """The scan's views with who runs each server and whether the board
        may stop it — learning, on the way, how to start it again — and the
        known servers that are not running, as stopped engines."""
        by_port = {int(r["port"]): r for r in listeners or [] if isinstance(r, dict) and str(r.get("port", "")).isdigit()}
        me = self.procs.uid()
        out = []
        running = set()
        for view in views:
            kind = next((k for k in kinds if k.id == view.get("kind")), None)
            running.add(view.get("kind"))
            pid = int((by_port.get(int(view.get("port") or 0)) or {}).get("pid") or 0)
            info = self.procs.info(pid) if pid > 0 else None
            run_by = "" if info is None or me is None else ("user" if info.get("uid") == me else "other")
            entry = self.known().get(str(view.get("kind"))) or {}
            if kind is not None and run_by == "user" and view.get("state") in ("ok", "auth"):
                recipe = kind.recipe(info, view)
                if recipe:
                    self.remember(kind.id, recipe=recipe, port=int(view["port"]))
            controls = list(view.get("controls") or [])
            if run_by == "user" and pid > 0 and (self.known().get(str(view.get("kind"))) or {}).get("recipe"):
                controls.append("stop")
            out.append({**view, "controls": controls, "runBy": run_by, "autostart": entry.get("autostart") is True})
        for kind in kinds:
            if kind.id in running:
                continue
            entry = self.known().get(kind.id) or {}
            recipe = entry.get("recipe") or kind.installed(self.home)
            if not recipe:
                continue
            port = int(entry.get("port") or recipe.get("port") or kind.default_port)
            out.append({"kind": kind.id, "label": kind.label, "port": port, "listen": "", "state": "stopped",
                        "version": "", "models": None, "pids": [], "controls": ["start"], "holds": False,
                        "firewall": None, "ramBytes": None, "runBy": "", "autostart": entry.get("autostart") is True})
        return out

    def recipe_for(self, kind) -> dict[str, Any] | None:
        entry = self.known().get(kind.id) or {}
        return entry.get("recipe") or kind.installed(self.home)

    def start(self, kind, port: int) -> str:
        """Start the server as the recipe says; it starts with the machine
        from now on. "" or why not — whether it answers is the caller's."""
        recipe = self.recipe_for(kind)
        if not recipe:
            return f"how to start {kind.label} here is not known"
        reason = kind.start_server(recipe, self.procs)
        if not reason:
            self.remember(kind.id, recipe=recipe, port=int(port), autostart=True)
        return reason

    def stop(self, kind, pid: int | None) -> str:
        """Stop the server; it no longer starts with the machine."""
        recipe = self.recipe_for(kind) or {}
        reason = kind.stop_server(recipe, pid, self.procs)
        if not reason:
            self.remember(kind.id, autostart=False)
        return reason

    def due_at_boot(self, boot: str | None) -> list[str]:
        """The kinds to start because the machine booted: those started from
        here and not stopped since — on the first scout start of a boot only
        (a scout restarted by an update must not bring back what the
        operator stopped by hand), and never where the machine will not say
        which boot it is."""
        if not boot:
            return []
        with self.state.lock:
            if self.state.get(self.BOOT_KEY) == boot:
                return []
            self.state[self.BOOT_KEY] = boot
            self.state.save()
        return [kind_id for kind_id, entry in self.known().items()
                if isinstance(entry, dict) and entry.get("autostart") is True and entry.get("recipe")]
