#!/usr/bin/env python3
"""Snapshot: a cell the scout started is not lost when the scout is killed (2.32).

What happened, 2026-10-14. Strata's start loaded 37 GB into a 60 GB machine; in the middle of it systemd-oomd killed
the SCOUT (the page-scanning cgroup it picks is not always the one that is short of memory). The scout came back in
3 s, read its registry, found the cell's pid alive but its command line no longer the one the record remembered (a
launcher that `exec`s rewrites argv; the record's marker even carried a literal $HOME), found nothing listening on the
port yet (a cell that loads does not listen for a minute) — and forgot the cell. It went on loading outside the
registry: the board saw no cell, Stop was off, and Start ran a SECOND copy beside it, which loaded 37 GB again before
the first one's card memory made it die. The first stayed, holding 31 GB of the card, until the scout was restarted
by hand and found it by its port.

What the scout does now, pinned here by value:
 - every question the OS answers about a cell's mark (HostProcesses.port_of / tagged_cells / carrier / started_at);
 - the start-up adoption finds a cell by the mark in its environment, also while it loads, also when the recorded pid
   was clobbered; it forgets only a port nothing runs;
 - Reconcile holds a cell the scout lost track of while it runs, after two passes in a row;
 - a start that finds its cell running holds it instead of starting a second copy; a port held by a process no scout
   started is refused with its pid and command line;
 - a start that promises memory (`ram`) is refused when the machine cannot hold it — also while the first cell that
   promised is still loading (MemoryLedger);
 - Stop reaches a cell by its mark and leaves nothing of it; the watchdog does not start a third copy.

Nothing real is touched (see _scout_harness.py): the host's processes, signals and /proc are fakes named in each pin.

Run: PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_scout_lost_cell.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks, patched, make_scout, TMP  # noqa: E402

# expanduser(), Path.home() and the cell-asset sync all resolve through $HOME; no pin may write into the real one.
HOME = TMP / "home"
HOME.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(HOME)

from caravan_scout.ledger import MemoryLedger  # noqa: E402
from caravan_scout.process import HostProcesses  # noqa: E402
from caravan_scout.reconcile import Reconcile  # noqa: E402
from caravan_scout.starts import CommandLaunch  # noqa: E402
from caravan_scout.watchdog import Watchdog  # noqa: E402

CHECKS = Checks("test_scout_lost_cell")
check = CHECKS.check

NOW = 1_790_000_000
SERVER = 849104     # the cell's server: what the record remembers
ENGINE = 849118     # the engine under it, a child that carries the mark too
PORT = 22009
#: What the controller's command line for the cell said, as the record keeps it as a marker: $HOME and quotes no
#: argv can ever match.
MARKER = 'bash $HOME/run_strata.sh "22009" "${LLAMA_MODELS_DIR:-$HOME/llama.cpp/models}"/Qwen3.8-Flash/IQ2_XS/q.gguf'
SERVER_ARGV = "/opt/strata/.venv/bin/python /opt/strata/serve/server.py --engine strata --port 22009"
ENGINE_ARGV = "/opt/strata/engine/strata --serve --pack /cache/.cells/22009/pack"


# ── fakes ─────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def quiet():
    """The scout's journal lines, captured; pins read them from the buffer."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


class FakeProc:
    """A spawned process that never ran: poll/wait/terminate answer from fields."""

    def __init__(self, pid=4242, rc=0, running=True):
        self.pid, self.rc, self.running, self.terminated = pid, rc, running, False

    def poll(self):
        return None if self.running else self.rc

    def wait(self, timeout=None):
        self.running = False
        return self.rc

    def terminate(self):
        self.terminated, self.running = True, False

    def kill(self):
        self.running = False


class FakeHost:
    """The host's processes, as a scout asks about them: `ps -p` answers from `procs` ({pid: command line}, a dead
    process has none), `ss -ltnpH` from `listening` ({port: pid}), signals end processes the way FakeKill in
    test_scout_cells.py does — SIGTERM and SIGKILL, unless the pid is `stubborn` for SIGTERM. Every call is written
    down."""

    def __init__(self, procs=None, listening=None, stubborn=()):
        self.procs = dict(procs or {})
        self.listening = dict(listening or {})
        self.stubborn = set(stubborn)
        self.calls = []     # subprocess.run argv
        self.signals = []   # (pid, signal)
        self.spawned = []   # Popen argv

    def run(self, cmd, *args, **kwargs):
        argv = list(cmd)
        self.calls.append(argv)
        if argv[:2] == ["ps", "-p"]:
            line = self.procs.get(int(argv[2]), "")
            return subprocess.CompletedProcess(argv, 0 if line else 1, stdout=line + "\n" if line else "", stderr="")
        if argv[:2] == ["ss", "-ltnpH"]:
            out = "".join(f'LISTEN 0 4096 0.0.0.0:{port} 0.0.0.0:* users:(("python3",pid={pid},fd=3))\n'
                          for port, pid in self.listening.items())
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        raise FileNotFoundError(argv[0])      # a host without sudo, lsof, pgrep

    def kill(self, pid, sig):
        self.signals.append((pid, sig))
        if sig == 0:
            if pid not in self.procs:
                raise ProcessLookupError(3, "No such process")
            return
        if pid not in self.procs:
            raise ProcessLookupError(3, "No such process")
        if sig == signal.SIGKILL or pid not in self.stubborn:
            self.procs.pop(pid, None)
            self.listening = {p: q for p, q in self.listening.items() if q != pid}

    def popen(self, argv, **kwargs):
        self.spawned.append(list(argv))
        return FakeProc(pid=7070)


class HeldThreads:
    """threading.Thread that never starts by itself: a pin runs a held target when it chooses."""

    def __init__(self):
        self.made = []
        registry = self.made

        class _Thread:
            def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, *, daemon=None):
                self.target, self.name, self.daemon = target, name, daemon
                self.args, self.kwargs, self.started = tuple(args), dict(kwargs or {}), False
                registry.append(self)

            def start(self):
                self.started = True

        self.Thread = _Thread


class Rig:
    """The fakes of one pin, patched in for a `with` block: the clock stands at NOW and sleeps are written down,
    threads are held, the host answers from `host`, the journal is captured. Checks are made AFTER the block."""

    def __init__(self, host=None, proc=None, meminfo=None, now=NOW):
        self.host = host or FakeHost()
        self.proc = proc
        self.meminfo = meminfo
        self.now = now
        self.sleeps = []
        self.threads = HeldThreads()
        self.journal = ""
        self._stack = self._buf = None

    def __enter__(self):
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(patched(time, time=lambda: float(self.now), sleep=self.sleep))
        self._stack.enter_context(patched(threading, Thread=self.threads.Thread))
        self._stack.enter_context(patched(subprocess, run=self.host.run, Popen=self.host.popen))
        self._stack.enter_context(patched(os, kill=self.host.kill))
        self._stack.enter_context(patched(urllib.request, urlopen=lambda *a, **k: (_ for _ in ()).throw(OSError("quiet"))))
        # The machine's /proc: the pins' own (or none at all).
        self._stack.enter_context(patched(HostProcesses, PROC=self.proc or TMP / "no-proc-here"))
        self._stack.enter_context(patched(MemoryLedger, MEMINFO=self.meminfo or TMP / "no-meminfo-here"))
        self._buf = self._stack.enter_context(quiet())
        return self

    def sleep(self, seconds):
        """A sleep written down, and the clock moved by it: a wait for a process to go ends at its deadline, as it does."""
        self.sleeps.append(seconds)
        self.now += seconds

    def __exit__(self, *exc):
        self.journal += self._buf.getvalue()
        self._stack.close()
        return False


def fake_proc(name, tree):
    """A /proc of its own: {pid: {"mark": port or None, "ppid": n, "children": [pids], "ticks": n}} written as the
    files the scout reads — environ, stat, task/<pid>/children — plus /proc/stat's boot time."""
    root = TMP / "proc" / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "stat").write_text("cpu  1 2 3\nbtime 1789000000\n", encoding="utf-8")
    for pid, info in tree.items():
        folder = root / str(pid)
        (folder / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
        env = b"PATH=/bin\0HOME=/h\0"
        if info.get("mark") is not None:
            env += f"CARAVAN_SCOUT_CELL={info['mark']}\0".encode()
        (folder / "environ").write_bytes(env)
        # pid (comm) state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt utime stime cutime cstime
        # priority nice threads itrealvalue starttime — comm may hold spaces and brackets, as a real one does.
        (folder / "stat").write_text(
            f"{pid} (py (srv) x) S {info.get('ppid', 1)} 0 0 0 0 0 0 0 0 0 0 0 0 0 20 0 1 0 {info.get('ticks', 100)} 0\n",
            encoding="utf-8")
        (folder / "task" / str(pid) / "children").write_text(
            " ".join(str(c) for c in info.get("children", [])) + (" " if info.get("children") else ""), encoding="utf-8")
    return root


def meminfo(name, total_mib, available_mib):
    path = TMP / f"meminfo-{name}"
    path.write_text(f"MemTotal: {int(total_mib * 1024)} kB\nMemFree: 1 kB\nMemAvailable: {int(available_mib * 1024)} kB\n",
                    encoding="utf-8")
    return path


def disk_cells(s):
    """The cell registry as it is ON DISK — what the next scout start re-adopts."""
    if not s.state.path.exists():
        return {}
    return json.loads(s.state.path.read_text(encoding="utf-8")).get("cells") or {}


def record(**fields):
    base = {"port": PORT, "kind": "command", "pid": SERVER, "marker": MARKER,
            "cfg": {"modelPath": "/cache/q.gguf", "port": PORT, "cellKind": "command"}, "log": "",
            "cacheModels": True, "healthPath": "/health", "startedAt": NOW - 46}
    base.update(fields)
    return base


STRATA_TREE = {SERVER: {"mark": PORT, "ppid": 1599, "children": [ENGINE], "ticks": 250_000},
               ENGINE: {"mark": PORT, "ppid": SERVER}, 1599: {"ppid": 1}, 4000: {"mark": 22001, "ppid": 1599}}


# ── what the OS says of a cell's mark ─────────────────────────────────────

def test_port_of():
    proc = fake_proc("port-of", {SERVER: {"mark": PORT}, 600: {}})
    (proc / "610").mkdir(parents=True, exist_ok=True)
    (proc / "610" / "environ").write_bytes(b"CARAVAN_SCOUT_CELLS=22009\0CARAVAN_SCOUT_CELL=nope\0")
    (proc / "620").mkdir(parents=True, exist_ok=True)
    (proc / "620" / "environ").write_bytes(b"A=1\0CARAVAN_SCOUT_CELL=22001\0")
    (proc / "630" / "environ").mkdir(parents=True, exist_ok=True)     # unreadable as a file
    with patched(HostProcesses, PROC=proc):
        got = [HostProcesses.port_of(p) for p in (SERVER, 620, 600, 610, 9999, 630, "x", None)]
    check(got == [PORT, 22001, 0, 0, 0, 0, 0, 0],
          "the port a scout started a process for is what its environment says of it; no mark, a look-alike variable, "
          "a mark that is no number, no such process, an unreadable file, a pid that is no number — 0, never an error")
    with patched(HostProcesses, PROC=TMP / "no-proc-here"):
        check(HostProcesses.port_of(SERVER) == 0, "negative: a machine with no /proc says 0 (macOS: the older questions stay)")


def test_tagged_cells():
    proc = fake_proc("tagged", STRATA_TREE)
    (proc / "notapid").mkdir(exist_ok=True)
    with patched(HostProcesses, PROC=proc):
        got = HostProcesses.tagged_cells()
    check(got == {PORT: [SERVER], 22001: [4000]},
          "a cell is a tree: the server and the engine under it both carry the mark, and the root is the one whose parent "
          "does not — one pid per cell")
    check(1599 not in {pid for roots in got.values() for pid in roots} and ENGINE not in got.get(PORT, []),
          "negative: a process with no mark is no cell, a child of a marked process is not a second root")
    tree = {10: {"mark": 22050, "ppid": 1}, 11: {"mark": 22050, "ppid": 1}, os.getpid(): {"mark": 22051, "ppid": 1}}
    with patched(HostProcesses, PROC=fake_proc("two-roots", tree)):
        two = HostProcesses.tagged_cells()
    check(two == {22050: [10, 11]},
          "boundary: two processes of one port with no parent among them are two roots — both are named, sorted; the "
          "scout's own process is never a cell")
    with patched(HostProcesses, PROC=TMP / "no-proc-here"):
        check(HostProcesses.tagged_cells() == {}, "negative: no /proc — no cells found this way")


def test_carrier_and_started_at():
    proc = fake_proc("carrier", STRATA_TREE)
    with patched(HostProcesses, PROC=proc):
        got = (HostProcesses.carrier(PORT), HostProcesses.carrier(PORT, SERVER), HostProcesses.carrier(PORT, ENGINE),
               HostProcesses.carrier(PORT, 4000), HostProcesses.carrier(22777), HostProcesses.carrier(PORT, "x"))
    check(got == (SERVER, SERVER, ENGINE, SERVER, 0, SERVER),
          "the process that runs a port: the one asked for when it carries the mark of that port (also a child), else "
          "the cell's root; a port nothing runs — 0; a pid of another cell is not taken for this one")
    with patched(HostProcesses, PROC=proc), patched(os, sysconf=lambda name: 100):
        first, none = HostProcesses.started_at(SERVER), HostProcesses.started_at(31337)
    check(first == 1789000000 + 250_000 // 100 and none == 0,
          "when a process started: the boot's time plus its start ticks over the clock rate; a process gone — 0")


# ── the start-up adoption ─────────────────────────────────────────────────

def test_adopt_a_cell_that_loads():
    s = make_scout({}, {"cells": {str(PORT): record(ramMiB=38000)}})
    host = FakeHost({SERVER: SERVER_ARGV, ENGINE: ENGINE_ARGV})
    rig = Rig(host, proc=fake_proc("adopt-loads", STRATA_TREE))
    with rig:
        s.cells.adopt_survivors()
        cell = s.cells.by_port.get(PORT)
        status = cell.process.status() if cell else {}
    check(status.get("running") is True and status.get("pid") == SERVER and status.get("adopted") is True,
          "defect-history (2026-10-14): the pid lives, its argv is not the record's marker, nothing listens yet — the "
          "cell is adopted by the mark in its environment, not forgotten")
    check(PORT in {int(k) for k in disk_cells(s)} and disk_cells(s)[str(PORT)]["pid"] == SERVER,
          "negative: the record stays in the registry, with the same pid")
    check(host.calls == [["ps", "-p", str(SERVER), "-o", "command="]],
          "the mark decides: the port is not asked of `ss`, nobody is health-probed, nothing is reaped")
    check(f"[llama-node] adopted running cell :{PORT} (pid {SERVER})" in rig.journal, "the journal names the cell and its pid")
    check(cell is not None and cell.ram_mib == 38000, "the memory it promised comes back from the record — the ledger sees it")
    check(host.signals == [(SERVER, 0)] or host.signals == [], "negative: nobody is signalled on the way")


def test_adopt_after_the_pid_was_clobbered():
    s = make_scout({}, {"cells": {str(PORT): record(pid=850361)}})      # a failed second start's pid, dead
    host = FakeHost({SERVER: SERVER_ARGV, ENGINE: ENGINE_ARGV})
    rig = Rig(host, proc=fake_proc("adopt-clobbered", STRATA_TREE))
    with rig:
        s.cells.adopt_survivors()
        status = s.cells.by_port[PORT].process.status() if PORT in s.cells.by_port else {}
    check(status.get("pid") == SERVER and disk_cells(s).get(str(PORT), {}).get("pid") == SERVER,
          "the recorded pid is dead, nothing listens, the mark is on another process — that is the cell: adopted, and "
          "the registry on disk is made honest for the next start")
    check(f"[llama-node] :{PORT} does not listen yet, but pid {SERVER} still runs it (it carries this scout's mark for the "
          f"port) — adopting it while it loads" in rig.journal, "the journal says why a silent port is no reason to forget it")


def test_adopt_forgets_only_what_is_gone():
    s = make_scout({}, {"cells": {str(PORT): record()}})
    rig = Rig(FakeHost({}), proc=fake_proc("adopt-gone", {1599: {"ppid": 1}}))
    with rig:
        s.cells.adopt_survivors()
    check(str(PORT) not in disk_cells(s) and PORT not in s.cells.by_port,
          "negative: no process runs the port or carries its mark — the cell is really gone, the record is dropped")
    check(f"[llama-node] :{PORT} is gone: no process serves it or carries its mark — forgetting it" in rig.journal,
          "and the journal says so: a cell that vanishes from the registry without a word was the defect")


# ── Reconcile ─────────────────────────────────────────────────────────────

def test_reconcile():
    s = make_scout({}, {"cells": {}})
    host = FakeHost({SERVER: SERVER_ARGV, ENGINE: ENGINE_ARGV, 4000: "/opt/llama/llama-server --port 22001"})
    rec = Reconcile(s.cells)
    rig = Rig(host, proc=fake_proc("reconcile", STRATA_TREE))
    with rig:
        first = rec.tick()
        held_after_first = {p: c.process.status().get("running") for p, c in s.cells.all()}
        second = rec.tick()
        held_after_second = {p: c.process.status().get("running") for p, c in s.cells.all()}
        third = rec.tick()
    check(first == [] and held_after_first == {}, "a process seen once is not taken: a start may have made it a moment ago")
    check(sorted(second) == [(22001, 4000), (PORT, SERVER)] and held_after_second == {PORT: True, 22001: True},
          "seen by two passes in a row, a cell no cell of this scout holds is held again — both of them, each by its root")
    check(third == [], "negative: a cell that is held is not taken again")
    check(f"[reconcile] :{PORT} runs as pid {SERVER} outside this scout's cells — holding it again" in rig.journal
          and f"[llama-node] adopted running cell :{PORT} (pid {SERVER})" in rig.journal,
          "the journal says what was found, and the adoption says the rest")
    rec_disk = disk_cells(s).get(str(PORT), {})
    check(rec_disk.get("pid") == SERVER and rec_disk.get("kind") == "command" and rec_disk.get("port") == PORT,
          "a cell with no record gets one from what the machine says of its process: the pid, its kind, its port")
    check(rec_disk.get("startedAt") == 1789000000 + 250_000 // 100,
          "…and its start time from /proc, not the moment it was found: the board's uptime is the cell's own")

    s = make_scout({}, {"cells": {}})
    rec = Reconcile(s.cells)
    with Rig(host, proc=fake_proc("reconcile-gone", {SERVER: {"mark": PORT, "ppid": 1}})):
        rec.tick()
    with Rig(host, proc=fake_proc("reconcile-gone-2", {1599: {"ppid": 1}})):
        rec.tick()
    with Rig(host, proc=fake_proc("reconcile-gone-3", {SERVER: {"mark": PORT, "ppid": 1}})):
        again = rec.tick()
    check(again == [] and s.cells.by_port == {},
          "boundary: a process that vanished between two passes starts counting again — two in a row, not two in all")

    s = make_scout({}, {"cells": {}})
    rec = Reconcile(s.cells)
    tree = {SERVER: {"mark": PORT, "ppid": 1}}
    with Rig(host, proc=fake_proc("reconcile-tracked", tree)):
        s.cells.at(PORT).process.adopt(SERVER, {"port": PORT}, started_at=NOW)
        a, b = rec.tick(), rec.tick()
        s.cells.drop(PORT)
        s.cells.at(PORT)
        s.cells.report(PORT, phase="loading")
        c, d = rec.tick(), rec.tick()
    check(a == b == c == d == [],
          "negative: a port whose cell runs, or whose start is under way (loading), is never taken from its start")

    s = make_scout({}, {"cells": {}})
    rec = Reconcile(s.cells)
    waits, passes = [], []
    boom = [True]

    def tick():
        passes.append(1)
        if boom[0]:
            boom[0] = False
            raise RuntimeError("/proc is odd")
        if len(passes) >= 3:
            raise KeyboardInterrupt
    rec.tick = tick

    def sleep(seconds):
        waits.append(seconds)
    with quiet() as buf:
        try:
            rec.run(sleep=sleep)
        except KeyboardInterrupt:
            pass
    check(waits == [Reconcile.INTERVAL, Reconcile.INTERVAL] and len(passes) == 3 and "[reconcile] pass failed: /proc is odd" in buf.getvalue(),
          "the loop passes every INTERVAL; a pass that fails is said in the journal and the watch goes on")


# ── a start ───────────────────────────────────────────────────────────────

COMMAND = {"cellKind": "command", "command": "bash $HOME/run_strata.sh $PORT", "shellLine": "export PORT=$PORT; exec bash x",
           "config": {"PORT": PORT}, "cacheModels": True}


def test_a_start_finds_its_cell():
    s = make_scout({}, {"cells": {str(PORT): record(pid=850361)}})
    host = FakeHost({SERVER: SERVER_ARGV, ENGINE: ENGINE_ARGV})
    rig = Rig(host, proc=fake_proc("start-finds", STRATA_TREE))
    with rig:
        answer = s.cells.start(dict(COMMAND))
        status = s.cells.by_port[PORT].process.status()
    check(answer.get("ok") is False and answer.get("adopted") is True and answer.get("pid") == SERVER
          and f"a server is already running on port {PORT} (pid {SERVER}" in answer.get("error", ""),
          "defect-history: a start for a port whose cell runs unheld answers that it runs there — and says its pid")
    check(host.spawned == [] and rig.threads.made == [], "no second copy: nothing is spawned, no launch thread is made")
    check(status.get("running") is True and status.get("pid") == SERVER and status.get("adopted") is True,
          "the cell is held at once, so the board shows it and has a Stop for it")
    check(not any(c[:1] == ["sudo"] for c in host.calls), "negative: the firewall is not opened for a refused start")


def test_a_start_refuses_a_stranger_on_its_port():
    s = make_scout({}, {"cells": {}})
    host = FakeHost({7777: "/usr/bin/python3 -m http.server 22009"}, listening={PORT: 7777})
    rig = Rig(host, proc=fake_proc("start-stranger", {7777: {"mark": None, "ppid": 1}}))
    with rig:
        answer = s.cells.start(dict(COMMAND))
    check(answer.get("ok") is False and answer.get("listenerPid") == 7777
          and answer.get("error") == f"port {PORT} is held by pid 7777 (/usr/bin/python3 -m http.server 22009), "
                                     f"which no scout started — stop it first, or pick another port",
          "a process no scout started holds the port: the start is refused with its pid and its command line, before the "
          "cell loads a model and fails to bind")
    check(host.spawned == [] and rig.threads.made == [] and PORT not in {p for p, c in s.cells.all() if c.process.status().get("running")},
          "negative: nothing was launched and nothing was taken")
    check(not any(c[:1] == ["sudo"] for c in host.calls), "negative: the port is not opened in the firewall")


# ── memory ────────────────────────────────────────────────────────────────

def test_the_ledger():
    s = make_scout({}, {"cells": {}})
    want = {"reserveMiB": 38000, "who": "Strata", "why": "its weights stay in memory: 37.1 GiB"}
    with Rig(meminfo=meminfo("ample", 61952, 52000)):
        fits = MemoryLedger(s.cells).short(PORT, want)
    check(fits is None, "the memory is there: available 50.8 GiB covers 37.1 GiB and the 2 GiB the system keeps — no refusal")
    with Rig(meminfo=meminfo("short", 61952, 30000)):
        refused = MemoryLedger(s.cells).short(PORT, want)
    check(refused == {"ok": False, "error": "Strata wants 37.1 GiB of memory (its weights stay in memory: 37.1 GiB) but only "
                                           "29.3 GiB is available (and 2 GiB stays for the system)"},
          "less is available than it keeps resident: refused, with the numbers said")
    with Rig(meminfo=meminfo("edge", 61952, 38000 + MemoryLedger.HEADROOM_MIB)):
        edge = MemoryLedger(s.cells).short(PORT, want)
    with Rig(meminfo=meminfo("edge-1", 61952, 38000 + MemoryLedger.HEADROOM_MIB - 1)):
        below = MemoryLedger(s.cells).short(PORT, want)
    check(edge is None and below is not None, "boundary: exactly what it needs plus the system's share is enough; a MiB less is not")
    # A cell that promised 38000 and is still loading: its memory has not come, its promise has.
    first = s.cells.at(22010)
    first.ram_mib = 38000
    s.cells.report(22010, phase="loading")
    with Rig(meminfo=meminfo("loading", 61952, 52000)):
        second = MemoryLedger(s.cells).short(PORT, want)
        same_port = MemoryLedger(s.cells).short(22010, want)
    check(second is not None and second["error"].startswith("Strata wants 37.1 GiB of memory (its weights stay in memory: 37.1 GiB), "
                                                           "and with what the cells running here promised (37.1 GiB) that is more "
                                                           "than 90% of the machine's 60.5 GiB — stop :22010 first"),
          "defect-history: a second cell that keeps its weights in RAM, started while the first still LOADS — the kernel "
          "still says 50 GiB are available, and the promise is what refuses it, naming the cell to stop")
    check(same_port is None, "negative: a cell's own promise does not count against itself (a start of the same port)")
    s.cells.report(22010, phase="idle")
    with Rig(meminfo=meminfo("idle", 61952, 52000)):
        check(MemoryLedger(s.cells).short(PORT, want) is None, "negative: a cell that is not up and not starting promises nothing")
    with Rig():
        nothing = MemoryLedger(s.cells).short(PORT, want)
    check(nothing is None and MemoryLedger(s.cells).short(PORT, {"reserveMiB": 0}) is None
          and MemoryLedger(s.cells).short(PORT, {"reserveMiB": "many"}) is None,
          "negative: a machine that does not say its memory, a promise of nothing, a promise that is no number — no refusal")


def test_a_start_that_promises_memory():
    payload = {**COMMAND, "ram": {"reserveMiB": 38000, "who": "Strata", "why": "its weights stay in memory"}}
    s = make_scout({}, {"cells": {}})
    s.models.cache_dir().mkdir(parents=True, exist_ok=True)
    host = FakeHost({})
    rig = Rig(host, meminfo=meminfo("start-low", 61952, 20000))
    with rig:
        refused = s.cells.start(dict(payload))
    check(refused.get("ok") is False and "Strata wants 37.1 GiB of memory" in refused.get("error", "") and rig.threads.made == [],
          "a command cell that keeps its weights in RAM is refused at the request when the machine lacks it — no download, "
          "no launch")
    check(s.cells.by_port[PORT].ram_mib == 0 if PORT in s.cells.by_port else True,
          "negative: a refused start leaves no promise behind")
    s = make_scout({}, {"cells": {}})
    s.models.cache_dir().mkdir(parents=True, exist_ok=True)
    host = FakeHost({})
    rig = Rig(host, meminfo=meminfo("start-ok", 61952, 52000))
    with rig:
        accepted = s.cells.start(dict(payload))
        held = [t for t in rig.threads.made if isinstance(getattr(t.target, "__self__", None), CommandLaunch)]
        launched = held[0].target() if held else None
    check(accepted == {"ok": True, "status": "starting", "phase": "resolving", "port": PORT} and s.cells.by_port[PORT].ram_mib == 38000,
          "accepted: the request answers starting at once, and the cell holds its promise from that moment")
    check(launched is not None and launched.get("ok") is True and disk_cells(s).get(str(PORT), {}).get("ramMiB") == 38000,
          "the record keeps the promise: a restart finds it again")
    s2 = make_scout({}, {"cells": {str(PORT): disk_cells(s)[str(PORT)]}})
    with Rig(FakeHost({disk_cells(s)[str(PORT)]["pid"]: SERVER_ARGV}), proc=fake_proc("promise", {7070: {"mark": PORT, "ppid": 1}})):
        s2.cells.adopt_survivors()
    check(s2.cells.by_port[PORT].ram_mib == 38000, "an adopted cell is still known as the one that promised 37 GiB")
    with Rig(FakeHost({7070: SERVER_ARGV}), meminfo=meminfo("two-strata", 61952, 52000)):
        second = s2.cells.start({**payload, "config": {"PORT": 22011}})
    check(second.get("ok") is False and "that is more than 90% of the machine's 60.5 GiB — stop :22009 first" in second.get("error", ""),
          "a second Strata on another port, beside the first that runs: refused, and the cell to stop is named")


# ── stop ──────────────────────────────────────────────────────────────────

def test_stop_reaches_a_cell_by_its_mark():
    s = make_scout({}, {"cells": {}})
    host = FakeHost({SERVER: SERVER_ARGV, ENGINE: ENGINE_ARGV}, listening={PORT: SERVER})
    rig = Rig(host, proc=fake_proc("stop-mark", STRATA_TREE))
    with rig:
        result = s.cells.stop(PORT)
    check(result == {"ok": True, "reclaimed": True, "pid": SERVER},
          "defect-history: Stop for a cell no registry holds: the process that carries the scout's mark is the cell's — "
          "terminated, not 'an unrecognized process'")
    check(host.signals[:2] == [(SERVER, signal.SIGTERM), (ENGINE, signal.SIGTERM)] and host.procs == {},
          "the whole tree goes, the server and the engine under it — the engine held 31 GB of the card")
    s = make_scout({}, {"cells": {}})
    host = FakeHost({SERVER: SERVER_ARGV, ENGINE: ENGINE_ARGV})          # still loading: nothing listens yet
    with Rig(host, proc=fake_proc("stop-loading", STRATA_TREE)):
        loading = s.cells.stop(PORT)
    check(loading == {"ok": True, "reclaimed": True, "pid": SERVER} and host.procs == {},
          "defect-history: Stop for a cell that LOADS — nothing listens on its port, so the port could not name it; the mark "
          "does, and the server and the engine under it go")
    s = make_scout({}, {"cells": {str(PORT): record(marker="python3 other.py")}})
    host = FakeHost({5151: "/usr/bin/some-dev-server --port 22009"}, listening={PORT: 5151})
    with Rig(host, proc=fake_proc("stop-stranger", {5151: {"mark": None, "ppid": 1}})):
        refused = s.cells.stop(PORT)
    check(refused == {"ok": False, "listenerPid": 5151,
                      "error": f"port {PORT} is held by an unrecognized process (pid 5151) — not killing it"}
          and host.signals == [] and str(PORT) in disk_cells(s),
          "negative: a process with no mark and no known command line is still never killed, and the record stays")


def test_stop_leaves_nothing_of_the_cell():
    s = make_scout({}, {"cells": {str(PORT): record()}})
    host = FakeHost({SERVER: SERVER_ARGV, ENGINE: ENGINE_ARGV})
    tree = {SERVER: {"mark": PORT, "ppid": 1, "children": [ENGINE]}, ENGINE: {"mark": PORT, "ppid": SERVER}}
    rig = Rig(host, proc=fake_proc("stop-sweep", tree))
    with rig:
        s.cells.adopt_survivors()
        host.stubborn = set()
        # The server dies to its SIGTERM; the engine under it stays — as when the server cannot pass the signal on.
        original = host.kill

        def kill(pid, sig):
            if pid == ENGINE and sig == signal.SIGTERM and not host.engine_got_term:
                host.engine_got_term = True
                host.signals.append((pid, sig))
                return
            original(pid, sig)
        host.engine_got_term = False
        with patched(os, kill=kill):
            result = s.cells.stop(PORT)
    check(result == {"ok": True, "adopted": True}, "the stop of an adopted cell answers as it always did")
    check(ENGINE not in host.procs and SERVER not in host.procs and (ENGINE, signal.SIGTERM) in host.signals,
          "what still carries the cell's mark after its stop goes too: a stop that ends with the card still held is no stop")
    check("[llama-node] :22009 stopped, but [%d] still carry its mark — terminating them" % ENGINE in rig.journal
          or f"[llama-node] :{PORT} stopped, but" in rig.journal,
          "the journal says the sweep was needed")


# ── the watchdog ──────────────────────────────────────────────────────────

def test_the_watchdog_holds_the_lost_one():
    s = make_scout({}, {"cells": {}})
    host = FakeHost({SERVER: SERVER_ARGV, ENGINE: ENGINE_ARGV})
    dog = Watchdog(s.cells, suspect=None, clock=lambda: NOW)
    cell = s.cells.at(PORT)
    cell.crash = {"count": 1, "restarts": [], "due": NOW - 1, "reason": "exited (code 1)"}
    relaunches = []
    cell.process.relaunch = lambda: relaunches.append(1) or {"ok": True, "pid": 1}
    with Rig(host, proc=fake_proc("watchdog", STRATA_TREE)) as rig:
        dog.tick()
        status = cell.process.status()
    check(relaunches == [] and status.get("running") is True and status.get("pid") == SERVER and cell.crash is None,
          "a crash is due for a restart, but a process of this scout's still runs the port: that one is held, the crash "
          "note (a second copy's) is cleared, and no third copy is launched")
    check(f"[watchdog] :{PORT} not restarted: pid {SERVER} still runs it — holding that one" in rig.journal,
          "the journal says why the restart did not happen")


TESTS = (test_port_of, test_tagged_cells, test_carrier_and_started_at, test_adopt_a_cell_that_loads,
         test_adopt_after_the_pid_was_clobbered, test_adopt_forgets_only_what_is_gone, test_reconcile,
         test_a_start_finds_its_cell, test_a_start_refuses_a_stranger_on_its_port, test_the_ledger,
         test_a_start_that_promises_memory, test_stop_reaches_a_cell_by_its_mark, test_stop_leaves_nothing_of_the_cell,
         test_the_watchdog_holds_the_lost_one)

for test in TESTS:
    CHECKS.section(test.__name__[5:].replace("_", " ") + ":")
    try:
        test()
    except BaseException as exc:  # noqa: BLE001 — a crash is a red pin; the rest still runs
        check(False, f"{test.__name__} crashed: {exc!r}")

sys.exit(CHECKS.finish())
