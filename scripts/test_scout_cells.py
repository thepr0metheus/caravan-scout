#!/usr/bin/env python3
"""Snapshot: what the scout's cells do today, pinned by value.

Scope: the cells of caravan_scout/cells.py — the table by port and startup
phases, the public node views, the cell registry, re-adoption after a scout
restart, stray reaping; caravan_scout/starts.py — cell artifacts and both
start paths: the llama cell (a background launch) and the command cell (on
the request thread); caravan_scout/builds.py — the llama.cpp update job;
caravan_scout/saved_configs.py — saved launch configs.

The code is about to be rewritten into classes; these pins are what the
rewrite is checked against. A pin marked `as-is:` holds behaviour that is a
defect or an ugliness, kept on purpose so that changing it is a decision and
not an accident.

Nothing real is touched (see _scout_harness.py). Processes, signals, the
network, threads and the clock are fakes, named in each test.

Why functions: the test_* functions are the harness's contract — a list run
in order into one Checks ledger. The helpers left as functions (attempt,
env, quiet, models_served, worker, start_*) hold no state; each wraps one
call. Everything that keeps state — the fakes and the Rig — is a class.

Run: PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_scout_cells.py
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks, patched, FakeRun, make_scout, BLOCKED, TMP  # noqa: E402,F401

# expanduser(), Path.home() and the cell-asset sync all resolve through $HOME;
# no pin may write into the real one.
HOME = TMP / "home"
HOME.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(HOME)

import caravan_scout.builds as builds  # noqa: E402
from caravan_scout.cell_assets import CellAssets  # noqa: E402
from caravan_scout.starts import CellArtifacts, LlamaLaunch  # noqa: E402
from caravan_scout.errors import AppError  # noqa: E402
from caravan_scout.paths import SERVER_CELLS_DIR  # noqa: E402

CHECKS = Checks("test_scout_cells")
check = CHECKS.check

NOW = 1_790_000_000
GMTIME = time.gmtime
CONTROLLER = "http://10.0.0.5:7990"
MODEL = "models/org/model-q4.gguf"
MMPROJ = "models/org/mmproj-f16.gguf"
SPEC = "models/org/draft-q8.gguf"
BODIES = {MODEL: b"GGUF model-q4", MMPROJ: b"GGUF mmproj", SPEC: b"GGUF draft"}
UFW_ALLOW = ("sudo", "-n", "ufw", "allow")
UPDATE_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "update-llama.sh"
# llama.cpp's own words for a truncated GGUF — what the auto-repair looks for.
CORRUPT = ("gguf_init_from_file_impl: tensor 'blk.0.attn_q.weight' data is not within "
           "the file bounds, model is corrupted or incomplete")

# A binary that exists: CellProcess.start refuses a missing one before it spawns.
LLAMA_BIN = TMP / "bin" / "llama-server"
LLAMA_BIN.parent.mkdir(parents=True, exist_ok=True)
LLAMA_BIN.write_text("#!/bin/sh\n", encoding="utf-8")
# A model outside the cache, the way a host-local store holds one.
ABS_MODEL = TMP / "store" / "org" / "model-q4.gguf"
ABS_MODEL.parent.mkdir(parents=True, exist_ok=True)
ABS_MODEL.write_bytes(b"GGUF store copy")

METRICS = (b"# HELP llamacpp:prompt_tokens_seconds Average prompt throughput\n"
           b"llamacpp:prompt_tokens_seconds 123.456\n"
           b"llamacpp:predicted_tokens_seconds 45.678\n"
           b"llamacpp:requests_processing 2\n"
           b"llamacpp:kv_cache_usage_ratio 0.25\n")
PROPS = json.dumps({"default_generation_settings": {"n_ctx": 8192}}).encode()


# ── fakes ─────────────────────────────────────────────────────────────────

def attempt(fn):
    """(result, None) or (None, exception): a pin whose call raises reports
    FAIL and the file goes on — one traceback must not hide every pin after it."""
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001
        return None, exc


def err_is(err, status, text):
    return isinstance(err, AppError) and err.status == status and str(err) == text


def URLError_(reason):
    return urllib.error.URLError(reason)


@contextlib.contextmanager
def quiet():
    """Capture the scout's journal lines; pins read them from the buffer."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


@contextlib.contextmanager
def env(**values):
    """Set (or, with None, remove) environment variables for a block."""
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeProc:
    """A spawned process that never ran: poll/wait/terminate answer from fields."""

    def __init__(self, pid=4242, lines=(), rc=0, running=True):
        self.pid = pid
        self.stdout = iter(lines)        # the update job reads its output here
        self.rc = rc
        self.running = running
        self.terminated = False

    def poll(self):
        return None if self.running else self.rc

    def wait(self, timeout=None):
        self.running = False
        return self.rc

    def terminate(self):
        self.terminated = True
        self.running = False

    def kill(self):
        self.running = False


class FakePopen:
    """subprocess.Popen answering from a script: each spawn takes the next
    entry — FakeProc fields, or an exception to raise. Every spawn is written
    down with its argv and keyword arguments."""

    def __init__(self, *script, on_call=None):
        self.script = list(script)
        self.on_call = on_call
        self.calls = []
        self.procs = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        if self.on_call:
            self.on_call(list(argv), kwargs)
        entry = self.script.pop(0) if self.script else {}
        if isinstance(entry, BaseException):
            raise entry
        proc = FakeProc(**entry)
        self.procs.append(proc)
        return proc


class FakeKill:
    """os.kill over a set of live pids. Signal 0 asks (ProcessLookupError when
    dead); SIGTERM ends a pid unless it is stubborn; SIGKILL always does; a
    forbidden pid answers EPERM, like another user's process."""

    def __init__(self, alive=(), stubborn=(), forbidden=()):
        self.alive = set(alive)
        self.stubborn = set(stubborn)
        self.forbidden = set(forbidden)
        self.calls = []

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))
        if pid in self.forbidden:
            raise PermissionError(1, "Operation not permitted")
        if pid not in self.alive:
            raise ProcessLookupError(3, "No such process")
        if sig == signal.SIGKILL or (sig == signal.SIGTERM and pid not in self.stubborn):
            self.alive.discard(pid)


class HeldThreads:
    """threading.Thread that never starts by itself: the pin runs a held
    target when it chooses, so "returns immediately" and "what the thread
    did" are both observable, deterministically."""

    def __init__(self):
        self.made = []
        registry = self.made

        class _Thread:
            def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, *, daemon=None):
                self.target, self.name, self.daemon = target, name, daemon
                self.args, self.kwargs = tuple(args), dict(kwargs or {})
                self.started = self.ran = False
                registry.append(self)

            def start(self):
                self.started = True

            def run_now(self):
                self.ran = True
                self.target(*self.args, **self.kwargs)

        self.Thread = _Thread


class FakeResponse:
    """What urlopen hands back: read() in chunks, headers, a status, and a
    context manager. `on_read` runs before every read."""

    def __init__(self, body=b"", headers=None, status=200, chunks=None, on_read=None):
        self.chunks = list(chunks) if chunks is not None else ([body] if body else [])
        self.headers = dict(headers or {})
        self.status = status
        self.on_read = on_read

    def read(self, n=-1):
        if self.on_read:
            self.on_read()
        if n is None or n < 0:
            data, self.chunks = b"".join(self.chunks), []
            return data
        return self.chunks.pop(0) if self.chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen:
    """urllib.request.urlopen over a routing table. The longest URL substring
    that matches picks a list of answers, taken in order (the last repeats).
    An answer is a FakeResponse, an exception to raise, or a callable(url)
    returning either. Every call is written down: url, headers, timeout."""

    def __init__(self, routes=None, events=None):
        self.routes = {k: (list(v) if isinstance(v, list) else [v]) for k, v in (routes or {}).items()}
        self.calls = []
        self.events = events

    def __call__(self, req, timeout=None, **_kw):
        url = getattr(req, "full_url", req)
        headers = dict(req.header_items()) if hasattr(req, "header_items") else {}
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self.events is not None:
            self.events.append(("urlopen", url))
        keys = [k for k in self.routes if k in url]
        if not keys:
            raise urllib.error.URLError(f"the fake has no route for {url}")
        answers = self.routes[max(keys, key=len)]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if callable(answer) and not isinstance(answer, (FakeResponse, BaseException)):
            answer = answer(url)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Rig:
    """The fakes of one pin, patched in together for a `with` block: the clock
    stands at NOW and sleeps are written down, threads are held, processes,
    signals and the network answer from fakes, and the scout's journal is
    captured. Checks are made AFTER the block — inside it they would be
    captured too."""

    def __init__(self, popen=None, run=None, web=None, kill=None, now=NOW):
        self.popen = popen or FakePopen()
        self.run = run or FakeRun()
        self.web = web or FakeUrlopen()
        self.kill = kill or FakeKill()
        self.threads = HeldThreads()
        self.now = now
        self.sleeps = []
        self.journal = ""
        self.res = self.err = None
        self._stack = self._buf = None

    def __enter__(self):
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(patched(time, time=lambda: float(self.now), sleep=self.sleeps.append))
        self._stack.enter_context(patched(threading, Thread=self.threads.Thread))
        self._stack.enter_context(patched(subprocess, Popen=self.popen, run=self.run))
        self._stack.enter_context(patched(urllib.request, urlopen=self.web))
        self._stack.enter_context(patched(os, kill=self.kill))
        self._buf = self._stack.enter_context(quiet())
        return self

    def __exit__(self, *exc):
        self.journal += self._buf.getvalue()
        self._stack.close()
        return False


def models_served(bodies=None, on_request=None):
    """The controller's /api/models/download: serves `bodies` by path, with a
    Content-Length. `on_request(path)` runs when a download is asked for."""
    bodies = BODIES if bodies is None else bodies

    def answer(url):
        raw = urllib.parse.unquote(url.split("path=", 1)[1])
        if on_request:
            on_request(raw)
        body = bodies[raw]
        return FakeResponse(body, headers={"Content-Length": str(len(body))})
    return FakeUrlopen({"/api/models/download?path=": answer})


def disk_cells(s):
    """The cell registry as it is ON DISK — what the next agent start re-adopts."""
    if not s.state.path.exists():
        return {}
    return json.loads(s.state.path.read_text(encoding="utf-8")).get("cells") or {}


def ss_line(port, pid, proc="python3", addr="0.0.0.0"):
    return f'LISTEN 0 4096 {addr}:{port} 0.0.0.0:* users:(("{proc}",pid={pid},fd=3))\n'


def llama_args(port):
    return ["--model", "{{MODEL_PATH}}", "--port", str(port)]


def launch_inputs(t):
    """What a held thread's launch was handed, in the order the startup worker
    once took them as arguments: port, binary, config, model, mmproj, spec,
    cache flag, the controller's args. () when the thread runs no launch."""
    launch = getattr(t.target, "__self__", None)
    if not isinstance(launch, LlamaLaunch):
        return ()
    return (launch.port, launch.bin_path, launch.config, launch.model, launch.mmproj, launch.spec,
            launch.cache_models, launch.args)


def worker_scout(**config):
    return make_scout({"controllerUrl": CONTROLLER, "llamaServerBin": str(LLAMA_BIN), **config})


def worker(s, port, config, model=MODEL, mmproj="", spec="", cache=False, args=...,
           popen=None, web=None, kill=None, bin_path=None, make_cache_dir=True):
    """Run the llama startup worker synchronously under a Rig. The cache dir
    is made first unless asked not to: the worker opens its per-port log there."""
    if args is ...:
        args = llama_args(port)
    if make_cache_dir:
        s.models.cache_dir().mkdir(parents=True, exist_ok=True)
    rig = Rig(popen=popen or FakePopen({"pid": 4242}), web=web or models_served(), kill=kill)
    with rig:
        rig.res, rig.err = attempt(lambda: LlamaLaunch(
            s.cells, port, str(bin_path or LLAMA_BIN), config, model, mmproj, spec, cache, args).run())
    return rig


def start_llama(payload, config=None, prep=None, kill=None, run=None):
    """Cells.start of a llama cell under a Rig: the launch thread is held, not run."""
    s = make_scout({"llamaServerBin": str(LLAMA_BIN), **(config or {})})
    if prep:
        prep(s)
    rig = Rig(run=run or FakeRun({UFW_ALLOW: (0, "")}), kill=kill)
    with rig:
        rig.res, rig.err = attempt(lambda: s.cells.start(payload))
    rig.s = s
    return rig


def start_command(payload, config=None, prep=None, popen=None, web=None, kill=None,
                  home=None, make_cache_dir=True):
    """A command cell through Cells.start under a Rig."""
    s = make_scout(config or {})
    if make_cache_dir:
        s.models.cache_dir().mkdir(parents=True, exist_ok=True)
    if prep:
        prep(s)
    rig = Rig(popen=popen or FakePopen({"pid": 7070}), run=FakeRun({UFW_ALLOW: (0, "")}),
              web=web, kill=kill)
    with env(HOME=str(home or HOME)), rig:
        rig.res, rig.err = attempt(lambda: s.cells.start(payload))
    rig.s = s
    return rig


# ── slots (agent.py) ──────────────────────────────────────────────────────

def test_slot_plumbing():
    s = make_scout()
    a = s.cells.at(22001)
    check(s.cells.at("22001") is a, "слот адресуется портом: '22001' и 22001 — один и тот же объект")
    b = s.cells.at(22002)
    check(b is not a, "negative: другой порт — другой слот")
    check(a.startup == {"phase": "idle"} and a.cache_models is False,
          "новый слот: фаза idle, кэш моделей выключен")
    snap = s.cells.all()
    check(snap == [(22001, a), (22002, b)], "снимок слотов — пары (порт, слот) в порядке создания")
    s.cells.at(22003)
    check(snap == [(22001, a), (22002, b)],
          "negative: снимок — копия: слот, созданный позже, в него не попадает")

    check(s.cells.holds(22001, a) is True, "holds: живой слот — текущий")
    s.cells.drop("22001")
    check(22001 not in s.cells.by_port, "drop снимает слот (порт строкой тоже)")
    check(s.cells.holds(22001, a) is False, "negative: снятый слот больше не текущий — жетон отмены сработал")
    check(22001 not in s.cells.by_port, "negative: holds не создаёт слот заново как побочный эффект")
    fresh = s.cells.at(22001)
    check(fresh is not a and s.cells.holds(22001, a) is False,
          "ячейка, пересозданная на том же порту, — новый объект; старый слот не текущий")
    _, err = attempt(lambda: s.cells.drop(29999))
    check(err is None, "boundary: снятие несуществующего слота — без ошибки")

    s.cells.report(22002, phase="resolving", modelPath=MODEL)
    s.cells.report(22002, downloadedBytes=5)
    check(s.cells.startup(22002) == {"phase": "resolving", "modelPath": MODEL, "downloadedBytes": 5},
          "report дополняет запись старта, а не заменяет её")
    got = s.cells.startup(22002)
    got["phase"] = "tampered"
    check(s.cells.startup(22002).get("phase") == "resolving",
          "negative: startup отдаёт копию — правка снаружи не течёт в слот")
    s.cells.startup(22004)
    check(22004 in s.cells.by_port,
          "as-is: чтение фазы порта без слота создаёт слот — поэтому holds обязан обходить at")


def test_node_public_views():
    s = make_scout()
    idle = s.cells.view(s.cells.at(22011))
    check(idle == {"running": False, "port": 22011}, "простой слот: running=False и порт")
    check("phase" not in idle,
          "as-is: у простаивающего слота фазы нет вовсе, а вид без слотов говорит «idle» — одно состояние, две формы")

    s.cells.report(22012, phase="downloading", modelPath=MODEL, downloadedBytes=64,
                         totalBytes=128, downloadingFile="model-q4.gguf (1/2)", startedAt=NOW)
    check(s.cells.view(s.cells.at(22012)) == {
        "running": False, "phase": "downloading", "modelPath": MODEL, "port": 22012,
        "downloadedBytes": 64, "totalBytes": 128, "downloadingFile": "model-q4.gguf (1/2)",
        "startedAt": NOW}, "фаза downloading: байты, файл и время старта видны до подъёма сервера")
    for port, phase in ((22013, "resolving"), (22014, "loading")):
        s.cells.report(port, phase=phase)
        check(s.cells.view(s.cells.at(port)) == {
            "running": False, "phase": phase, "modelPath": "", "port": port, "downloadedBytes": 0,
            "totalBytes": 0, "downloadingFile": "", "startedAt": None},
            f"фаза {phase} без данных: нули, пустые строки и startedAt=None — не выдуманное время")

    s.cells.report(22015, phase="error", error="model not available")
    check(s.cells.view(s.cells.at(22015)) == {
        "running": False, "port": 22015, "phase": "error", "lastError": "model not available"},
        "фаза error: причина из записи старта")
    sl = s.cells.at(22016)
    with Rig(popen=FakePopen(OSError("exec format error"))):
        sl.process.start(str(LLAMA_BIN), [], {"port": 22016})
    s.cells.report(22016, phase="error", error="")
    check(s.cells.view(sl) == {
        "running": False, "port": 22016, "phase": "error", "lastError": "exec format error"},
        "negative: пустая причина старта — берётся последняя ошибка самого процесса")

    log = TMP / "logs" / "llama-server.22017.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("0.00.100.000 I load: model type 7B\n"
                   "0.00.200.000 E llama_model_load: error loading model: out of memory\n"
                   "0.00.300.000 I main: exiting\n", encoding="utf-8")
    sl = s.cells.at(22017)
    sl.process.adopt(5151, {"port": 22017}, log_path=log, started_at=NOW - 10)
    s.cells.report(22017, phase="running", modelPath=MODEL)
    with Rig(kill=FakeKill()):
        view = s.cells.view(sl)
    check(view == {"running": False, "exitCode": None,
                   "lastError": "0.00.200.000 E llama_model_load: error loading model: out of memory",
                   "crashed": True, "phase": "error", "port": 22017, "modelPath": MODEL},
          "усыновлённая ячейка умерла — фаза error с причиной из её лога, хотя старт записал running")
    sl = s.cells.at(22018)
    sl.process.adopt(5252, {"port": 22018}, started_at=NOW)
    with Rig(kill=FakeKill()):
        view = s.cells.view(sl)
    check(view == {"running": False, "exitCode": None, "lastError": "exited (code None)", "crashed": True,
                   "phase": "error", "port": 22018, "modelPath": ""},
          "as-is: код выхода усыновлённой ячейки неизвестен, а причина гласит «exited (code None)» — None напечатан как код")

    sl = s.cells.at(22019)
    with Rig(popen=FakePopen({"running": False, "rc": 1, "pid": 6060})):
        sl.process.start(str(LLAMA_BIN), ["--port", "22019"], {"port": 22019})
    check(s.cells.view(sl) == {"running": False, "exitCode": 1, "lastError": "exited (code 1)",
                                        "crashed": True, "phase": "error", "port": 22019, "modelPath": ""},
          "упавший дочерний процесс без строки в логе — «exited (code 1)»")
    sl = s.cells.at(22020)
    with Rig(popen=FakePopen({"running": False, "rc": 0, "pid": 6161})):
        sl.process.start(str(LLAMA_BIN), ["--port", "22020"], {"port": 22020})
    s.cells.report(22020, phase="running")
    check(s.cells.view(sl) == {"running": False, "exitCode": 0, "lastError": "", "crashed": False,
                                        "port": 22020},
          "as-is: выход с кодом 0 — не авария, но и фазы нет вовсе: записанная running молча пропала")

    sl = s.cells.at(22021)
    sl.process.adopt(5353, {"modelPath": "/m/model-q4.gguf", "port": 22021, "cmd": ["/opt/llama-server"]},
                  started_at=NOW - 500)
    s.cells.report(22021, phase="error", error="old failure")
    web = FakeUrlopen({"/metrics": lambda url: FakeResponse(METRICS), "/props": lambda url: FakeResponse(PROPS)})
    run = FakeRun({("sudo", "-n", "ufw", "status"): (0, "Status: inactive\n")})
    with Rig(web=web, run=run, kill=FakeKill(alive={5353})):
        view = s.cells.view(sl)
    check(view == {"running": True, "pid": 5353, "adopted": True, "startedAt": NOW - 500, "uptimeSec": 500,
                   "modelPath": "/m/model-q4.gguf", "port": 22021, "phase": "running",
                   "promptTps": 123.46, "genTps": 45.68, "requestsProcessing": 2, "ctxMax": 8192,
                   "ctxUsed": 2048, "firewall": {"state": "open", "allowedFrom": []}},
          "живой процесс: фаза running, метрики, окно контекста и файрвол влиты в вид")
    check("cmd" not in view, "negative: командная строка процесса (cmd) в вид не попадает")
    check(view.get("phase") == "running", "negative: живой процесс сильнее записанной ошибки старта")
    check([c["url"] for c in web.calls] == ["http://127.0.0.1:22021/metrics", "http://127.0.0.1:22021/props"],
          "метрики и /props берутся с порта процесса")
    sl = s.cells.at(22022)
    sl.process.adopt(5454, {"modelPath": "/m/x.gguf"}, started_at=NOW)
    web = FakeUrlopen({"/metrics": URLError_("down"), "/props": URLError_("down")})
    with Rig(web=web, kill=FakeKill(alive={5454})):
        view = s.cells.view(sl)
    check(view.get("port") == 22022 and web.calls[:1] == [{"url": "http://127.0.0.1:22022/metrics",
                                                          "headers": {}, "timeout": 1}],
          "negative: без порта в cfg процесса — порт слота")
    check(view.get("firewall") == {"state": "unknown"} and "promptTps" not in view,
          "negative: метрик нет и ufw не ответил — метрик в виде нет, файрвол «unknown», а не выдуманные нули")


def test_nodes_public_list():
    s = make_scout()
    check(s.cells.views() == [], "нет слотов — пустой список")
    check(s.cells.first_view() == {"running": False, "phase": "idle"},
          "negative: одиночный вид без слотов — явное idle, а не пустота")
    s.cells.report(22025, phase="loading")
    s.cells.report(22023, phase="error", error="boom")
    nodes = s.cells.views()
    check([n.get("port") for n in nodes] == [22025, 22023],
          "список — в порядке создания слотов, а не по номеру порта")
    check([n.get("phase") for n in nodes] == ["loading", "error"], "у каждого слота своя фаза")
    check(s.cells.first_view() == nodes[0], "одиночный вид для старых контроллеров — первый слот")


# ── llama.cpp update job ──────────────────────────────────────────────────

def run_update(body, config=None, env_over=None, script=None, run_thread=True):
    s = make_scout(config)
    rig = Rig(popen=FakePopen(*(script or [{"lines": [], "rc": 0}])))
    with env(**{"LLAMA_BUILDS_KEEP": None, **(env_over or {})}), rig:
        rig.res, rig.err = attempt(lambda: s.builds.start_update(body))
        rig.before_run = list(rig.popen.calls)
        if run_thread:
            for t in rig.threads.made:
                t.run_now()
    rig.s = s
    return rig


def spawned_argv(rig):
    return rig.popen.calls[0]["argv"] if rig.popen.calls else None


def test_update_status_views():
    s = make_scout()
    fresh = {"running": False, "startedAt": 0, "tag": "", "lines": [], "done": False, "rc": None, "error": ""}
    check(s.builds.status() == fresh, "задание обновления до первого запуска: всё пусто, rc=None")
    check(s.builds.status_slim() == {"running": False, "done": False, "rc": None, "startedAt": 0,
                                           "tag": "", "lastLine": ""},
          "slim-статус (едет в каждом heartbeat) без строк: lastLine пустая")
    job = s.builds.job()
    check(s.builds.job() is job, "задание одно на агента: повторный вызов отдаёт тот же объект")
    job["lines"].extend(f"line {i}" for i in range(250))
    st = s.builds.status()
    check(st["lines"] == [f"line {i}" for i in range(50, 250)], "статус отдаёт последние 200 строк")
    st["lines"].append("tampered")
    check(len(job["lines"]) == 250, "negative: строки статуса — копия, правка снаружи не трогает задание")
    slim = s.builds.status_slim()
    check(slim.get("lastLine") == "line 249" and "lines" not in slim and "error" not in slim,
          "slim: только последняя строка, без списка строк и без error")


def test_update_start_commands():
    r = run_update({}, run_thread=False)
    check(r.res == {"running": True, "startedAt": NOW, "tag": "", "lines": [], "done": False, "rc": None,
                    "error": ""},
          "старт обновления отвечает сразу: running, время старта, строки пусты")
    t = r.threads.made[0] if r.threads.made else None
    check(t is not None and t.started and not t.ran and t.name == "llama-update" and t.daemon is True,
          "сборка идёт в фоновом daemon-потоке llama-update")
    check(r.before_run == [], "negative: в самом запросе процесс сборки не запускается")

    r = run_update({})
    check(spawned_argv(r) == ["bash", str(UPDATE_SCRIPT), "--force", "--no-restart"],
          "без тега: последний релиз — bash update-llama.sh --force --no-restart")
    r = run_update({"tag": " b9947 "})
    check(spawned_argv(r) == ["bash", str(UPDATE_SCRIPT), "--force", "--no-restart", "--llama-tag", "b9947"]
          and r.s.builds.status()["tag"] == "b9947",
          "тег релиза — --llama-tag, пробелы срезаны, тег записан в задание")
    r = run_update({"tag": "0123abcd"})
    check(spawned_argv(r) == ["bash", str(UPDATE_SCRIPT), "--force", "--no-restart", "--llama-tag", "0123abcd"],
          "коммит идёт тем же --llama-tag (checkout -f принимает и то, и другое)")
    r = run_update({"restoreId": "20260915-100000-b9947", "tag": "b1"})
    check(spawned_argv(r) == ["bash", str(UPDATE_SCRIPT), "--restore", "20260915-100000-b9947"],
          "restoreId — восстановление архивной сборки: --restore <id>, без --force и без тега")
    check(r.s.builds.status()["tag"] == "restore:20260915-100000-b9947",
          "negative: тег из тела при восстановлении не используется — в задании restore:<id>")
    r = run_update(None)
    check(spawned_argv(r) == ["bash", str(UPDATE_SCRIPT), "--force", "--no-restart"],
          "boundary: тело None — как пустое")

    r = run_update({}, env_over={"PATH": "/usr/bin:/bin:/opt/tools"})
    spawn = r.popen.calls[0] if r.popen.calls else {}
    penv = spawn.get("env") or {}
    check(penv.get("PATH") == "/usr/local/cuda/bin:/usr/bin:/bin:/opt/tools",
          "PATH сборки начинается с /usr/local/cuda/bin — nvcc находится без правки профиля")
    check(penv.get("LLAMA_BUILDS_KEEP") == "2", "клиент хранит 2 сборки по умолчанию")
    check("LLAMA_BUILDS_KEEP" not in os.environ, "negative: окружение самого агента не тронуто — правится копия")
    check(spawn.get("stdout") is subprocess.PIPE and spawn.get("stderr") is subprocess.STDOUT
          and spawn.get("text") is True, "вывод сборки — одним текстовым потоком (stderr слит в stdout)")
    r = run_update({}, config={"llamaBuildsKeep": 5})
    check((r.popen.calls[0].get("env") or {}).get("LLAMA_BUILDS_KEEP") == "5",
          "llamaBuildsKeep из config.json меняет глубину архива")
    r = run_update({}, config={"llamaBuildsKeep": 5}, env_over={"LLAMA_BUILDS_KEEP": "7"})
    check((r.popen.calls[0].get("env") or {}).get("LLAMA_BUILDS_KEEP") == "7",
          "boundary: LLAMA_BUILDS_KEEP из окружения процесса сильнее llamaBuildsKeep из config.json")
    r = run_update({}, env_over={"PATH": None})
    check((r.popen.calls[0].get("env") or {}).get("PATH") == "/usr/local/cuda/bin:/usr/bin:/bin",
          "boundary: без PATH у агента — /usr/local/cuda/bin:/usr/bin:/bin")

    s = make_scout()
    fake_module = TMP / "no-scripts" / "caravan_scout" / "builds.py"
    rig = Rig()
    with patched(builds, __file__=str(fake_module)), rig:
        _, err = attempt(lambda: s.builds.start_update({}))
    missing = TMP.resolve() / "no-scripts" / "scripts" / "update-llama.sh"
    check(err_is(err, 500, f"update script not found: {missing}"),
          "нет скрипта обновления рядом с пакетом — 500 с путём, где его искали")
    check(rig.threads.made == [] and s.builds.status()["running"] is False,
          "negative: без скрипта задание не стартует")


def test_update_job_run():
    out = ["\x1b[1;32m-- Build files written\x1b[0m\n", "[ 50%] Building CXX object ggml.c.o   \n",
           "\x1b[2Kprogress 3/10\n"]
    r = run_update({"tag": "b9947"}, script=[{"lines": out, "rc": 0}])
    check(r.s.builds.status() == {
        "running": False, "startedAt": NOW, "tag": "b9947",
        "lines": ["-- Build files written", "[ 50%] Building CXX object ggml.c.o", "\x1b[2Kprogress 3/10"],
        "done": True, "rc": 0, "error": ""},
        "после сборки: строки без цветовых ANSI-кодов и хвостовых пробелов, done, rc=0")
    check(r.s.builds.status()["lines"][-1].startswith("\x1b[2K"),
          "boundary: срезаются только цветовые коды (…m); стирание строки \\x1b[2K остаётся как есть")
    check(r.s.builds.status_slim() == {"running": False, "done": True, "rc": 0, "startedAt": NOW,
                                             "tag": "b9947", "lastLine": "\x1b[2Kprogress 3/10"},
          "slim-статус после сборки: rc и последняя строка")
    r = run_update({}, script=[{"lines": ["cmake failed\n"], "rc": 2}])
    st = r.s.builds.status()
    check((st["rc"], st["error"], st["done"], st["running"]) == (2, "", True, False),
          "negative: неудачная сборка — rc скрипта, error пуст, done")
    r = run_update({}, script=[FileNotFoundError(2, "No such file or directory", "bash")])
    st = r.s.builds.status()
    check((st["rc"], st["error"], st["done"], st["running"]) ==
          (-1, "[Errno 2] No such file or directory: 'bash'", True, False),
          "процесс не запустился — rc=-1 и текст ошибки, задание снято с running")

    r = run_update({}, script=[{"lines": [f"l{i}\n" for i in range(501)], "rc": 0}])
    check(r.s.builds.job()["lines"] == [f"l{i}" for i in range(100, 501)],
          "кольцевой буфер: на 501-й строке выброшены первые 100")
    r = run_update({}, script=[{"lines": [f"l{i}\n" for i in range(500)], "rc": 0}])
    check(r.s.builds.job()["lines"] == [f"l{i}" for i in range(500)],
          "boundary: 500 строк хранятся целиком")


def test_update_conflict():
    s = make_scout()
    rig = Rig(popen=FakePopen({"lines": ["building\n"], "rc": 0}, {"lines": [], "rc": 0}))
    with env(LLAMA_BUILDS_KEEP=None), rig:
        attempt(lambda: s.builds.start_update({"tag": "b1"}))
        s.builds.job()["lines"].append("building…")
        second, err = attempt(lambda: s.builds.start_update({"tag": "b2"}))
    check(err_is(err, 409, "a llama.cpp update is already running"), "второе обновление, пока идёт первое, — 409")
    st = s.builds.status()
    check(st["tag"] == "b1" and st["lines"] == ["building…"] and st["running"] is True,
          "negative: отказ 409 не сбрасывает идущее задание")
    check(len(rig.threads.made) == 1, "второй поток сборки не создан")
    with env(LLAMA_BUILDS_KEEP=None), rig:
        rig.threads.made[0].run_now()
        third, err3 = attempt(lambda: s.builds.start_update({"tag": "b3"}))
    check(err3 is None and third == {"running": True, "startedAt": NOW, "tag": "b3", "lines": [],
                                     "done": False, "rc": None, "error": ""},
          "negative: после завершения новое обновление принимается и обнуляет строки, rc и done")


def test_builds_list():
    root = TMP / "llama-builds"
    shutil.rmtree(root, ignore_errors=True)
    s = make_scout()
    check(s.builds.archive() == {"ok": True, "builds": []}, "нет папки архива — пустой список, а не ошибка")

    def build(where, name, meta=None, raw=None):
        d = where / name
        d.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            (d / "meta.json").write_text(raw, encoding="utf-8")
        elif meta is not None:
            (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    build(root, "20260901-100000-b9900", {"tag": "b9900", "commit": "aaa111"})
    build(root, "20260915-100000-b9947", {"tag": "b9947", "id": "spoofed"})
    build(root, "20260920-100000-b9990")
    build(root, "20260921-100000-b9991", raw="{broken")
    (root / "README.txt").write_text("not a build", encoding="utf-8")
    got = s.builds.archive()
    check(got == {"ok": True, "builds": [{"tag": "b9947", "id": "20260915-100000-b9947"},
                                         {"tag": "b9900", "commit": "aaa111", "id": "20260901-100000-b9900"}]},
          "архив сборок: новейшие первыми (по имени папки), meta.json + id")
    ids = [b.get("id") for b in got.get("builds", [])]
    check("20260920-100000-b9990" not in ids and "20260921-100000-b9991" not in ids and "README.txt" not in ids,
          "negative: папка без meta.json, битый meta.json и просто файл пропущены — список не падает")
    check(ids[:1] == ["20260915-100000-b9947"], "id — имя папки, даже если meta.json назвал другой")

    home_root = HOME / ".local" / "share" / "lama-caravan" / "llama-builds"
    build(home_root, "20260101-000000-b1", {"tag": "b1"})
    with env(LLAMA_BUILDS_DIR=None):
        got = s.builds.archive()
    shutil.rmtree(HOME / ".local", ignore_errors=True)
    check(got == {"ok": True, "builds": [{"tag": "b1", "id": "20260101-000000-b1"}]},
          "negative: без LLAMA_BUILDS_DIR архив ищется в ~/.local/share/lama-caravan/llama-builds")

    odd = TMP / "llama-builds-odd"
    build(odd, "20260930-000000-b2", raw="[1, 2]")
    with env(LLAMA_BUILDS_DIR=str(odd)):
        _, err = attempt(s.builds.archive)
    check(isinstance(err, TypeError),
          "as-is: ДЕФЕКТ — meta.json со списком вместо объекта роняет ВЕСЬ список сборок (TypeError), а не пропускается как битый")


# ── cell artifacts and registry ───────────────────────────────────────────

def test_cell_artifacts():
    s = make_scout()
    port = 22031
    d = SERVER_CELLS_DIR / str(port)
    shutil.rmtree(d, ignore_errors=True)
    check(CellArtifacts.dir_of("22031") == d, "папка ячейки — SERVER_CELLS_DIR/<port>")
    d.mkdir(parents=True)
    # Somebody is reading the old launcher and cell.json right now: an atomic
    # replace leaves their copies whole instead of half-written.
    (d / "start.sh").write_text("old launcher\n", encoding="utf-8")
    (d / "cell.json").write_text("{\"old\": true}\n", encoding="utf-8")
    os.link(d / "start.sh", d / "start.sh.reader")
    os.link(d / "cell.json", d / "cell.json.reader")
    args = ["--model", "/models/org/model q4.gguf", "--alias", "it's", "--port", port]
    config = {"MODEL_FILE": "models/org/model q4.gguf", "ALIAS": "модель"}
    runtime = {"modelPath": "/models/org/model q4.gguf", "port": port}
    with Rig():
        out = CellArtifacts(s.config).write(port, "~/llama.cpp/build/bin/llama-server", args, config, runtime)
    bin_abs = str(HOME / "llama.cpp" / "build" / "bin" / "llama-server")
    script = (d / "start.sh").read_text(encoding="utf-8")
    check(script == ("#!/usr/bin/env bash\nset -euo pipefail\n\nexec " + shlex.quote(bin_abs)
                     + " --model '/models/org/model q4.gguf' --alias 'it'\"'\"'s' --port 22031 \"$@\"\n"),
          "start.sh: bash со строгим режимом, exec бинаря (~ раскрыт) с аргументами в shlex-кавычках и \"$@\"")
    check((d / "start.sh").stat().st_mode & 0o777 == 0o755, "start.sh исполняемый (0755)")
    cell = json.loads((d / "cell.json").read_text(encoding="utf-8"))
    check(cell == {"hostId": "box-a", "port": port, "config": config, "runtime": runtime,
                   "cmd": [bin_abs, "--model", "/models/org/model q4.gguf", "--alias", "it's", "--port", "22031"],
                   "generatedAt": NOW, "startScript": str(d / "start.sh")},
          "cell.json: хост, порт, конфиг формы, runtime, argv строками, время и путь start.sh")
    raw = (d / "cell.json").read_text(encoding="utf-8")
    check("модель" in raw and raw.endswith("}\n") and raw.startswith("{\n  \"hostId\""),
          "cell.json читаем глазами: не-ASCII как есть, отступ 2, перевод строки в конце")
    check(out == {"dir": str(d), "startScript": str(d / "start.sh"), "cellJson": str(d / "cell.json"),
                  "generatedAt": NOW}, "ответ — где лежат артефакты и когда созданы")
    check((d / "start.sh.reader").read_text(encoding="utf-8") == "old launcher\n"
          and (d / "start.sh").stat().st_ino != (d / "start.sh.reader").stat().st_ino,
          "start.sh заменён атомарно: читатель старого файла видит его целым")
    check((d / "cell.json.reader").read_text(encoding="utf-8") == "{\"old\": true}\n"
          and (d / "cell.json").stat().st_ino != (d / "cell.json.reader").stat().st_ino,
          "cell.json заменён атомарно")
    check(sorted(p.name for p in d.iterdir()) == ["cell.json", "cell.json.reader", "start.sh", "start.sh.reader"],
          "negative: временных .tmp не осталось")


def test_registry():
    s = make_scout()
    cfg = {"modelPath": "/m/model-q4.gguf", "port": 22041, "cmd": ["/opt/llama-server", "-m", "x"]}
    with Rig():
        s.cells.records.add("22041", "llama", "4242", "/opt/llama/bin/llama-server", cfg,
                         Path("/logs/llama-server.22041.log"), 1)
    check(disk_cells(s).get("22041") == {
        "port": 22041, "kind": "llama", "pid": 4242, "marker": "/opt/llama/bin/llama-server",
        "cfg": {"modelPath": "/m/model-q4.gguf", "port": 22041}, "log": "/logs/llama-server.22041.log",
        "cacheModels": True, "healthPath": "/health", "startedAt": NOW},
        "запись реестра на диске: порт и pid числами, cfg без cmd, лог строкой, healthPath по умолчанию /health")
    check("cmd" in cfg, "negative: cfg вызывающего не тронут — cmd вырезан из копии")

    with Rig():
        s.cells.records.add(22042, "command", 5, "x" * 250, {}, None, False, health_path="")
        s.cells.records.add(22043, "command", 6, "y" * 200, None, None, False, health_path="/v1/models")
    cells_on_disk = disk_cells(s)
    check(cells_on_disk.get("22042", {}).get("marker") == "x" * 200, "маркер обрезан до 200 символов")
    check(cells_on_disk.get("22043", {}).get("marker") == "y" * 200, "boundary: маркер ровно в 200 символов цел")
    check(cells_on_disk.get("22042", {}).get("healthPath") == "/health"
          and cells_on_disk.get("22043", {}).get("healthPath") == "/v1/models",
          "пустой healthPath — /health; заданный (vLLM: /v1/models) сохранён для переусыновления")
    check(cells_on_disk.get("22042", {}).get("log") == "" and cells_on_disk.get("22043", {}).get("cfg") == {},
          "без лога — пустая строка; cfg None — пустой объект")

    with Rig():
        s.cells.records.add(22041, "llama", 4343, "m2", {"port": 22041}, None, False)
    check(disk_cells(s).get("22041", {}).get("pid") == 4343, "повторная запись того же порта заменяет прежнюю")

    s.cells.records.forget("22042")
    check(set(disk_cells(s)) == {"22041", "22043"}, "снятие с реестра переписывает state.json без этой ячейки")
    s.state.path.unlink()
    s.cells.records.forget(22099)
    check(not s.state.path.exists(), "negative: снятие неизвестного порта ничего не пишет на диск")


def test_marker_matches():
    m = make_scout().cells.processes.marker_matches
    check(m("/opt/llama/bin/llama-server", "/opt/llama/bin/llama-server --model /m/x.gguf --port 22001") is True,
          "маркер — подстрока командной строки процесса")
    mac_python = ("/opt/homebrew/Cellar/python@3.12/3.12.4/Frameworks/Python.framework/Versions/3.12/"
                  "Resources/Python.app/Contents/MacOS/Python whisper_server.py --port 22005")
    check(m("python3 whisper_server.py --port 22005", mac_python) is True,
          "argv[0] превратился в полный путь (python3 → …/MacOS/Python) — совпадает хвост маркера")
    check(m("python3 whisper_server.py --port 22005", "/usr/bin/python3 whisper_server.py --port 22006") is False,
          "negative: другой порт в хвосте — не та ячейка")
    check(m("", "anything") is False and m("python3 x.py", "") is False, "negative: пустой маркер или пустая строка — нет")
    check(m("python3", "/usr/bin/Python whisper.py") is False,
          "boundary: маркер из одного слова без совпадения — хвоста нет, не совпадает")


def test_pid_cmdline():
    s = make_scout()
    run = FakeRun({("ps", "-p", "4242", "-o", "command="): (0, "/opt/llama/bin/llama-server --port 22001\n")})
    with Rig(run=run):
        got = s.cells.processes.cmdline(4242)
    check(got == "/opt/llama/bin/llama-server --port 22001", "командная строка процесса из ps, без перевода строки")
    check(run.calls == [["ps", "-p", "4242", "-o", "command="]], "ps -p <pid> -o command=")
    with Rig(run=FakeRun({("ps",): (1, "")})):
        gone = s.cells.processes.cmdline(9999)
    with Rig(run=FakeRun()):
        no_ps = s.cells.processes.cmdline(4242)
    with Rig(run=FakeRun({("ps",): subprocess.TimeoutExpired(["ps"], 5)})):
        slow = s.cells.processes.cmdline(4242)
    check((gone, no_ps, slow) == ("", "", ""), "negative: нет процесса, нет ps, ps завис — пустая строка")


def test_port_listener_pid():
    s = make_scout()
    ss = (ss_line(122001, 1111, "other") + ss_line(2200, 2222, "other")
          + "LISTEN 0 4096 [::]:22001 [::]:*\n"
          + ss_line(22001, 4242, "llama-server")
          + 'LISTEN 0 4096 [::]:22005 [::]:* users:(("python3",pid=5151,fd=4))\n'
          + "garbage\n")

    def listener(port, table):
        run = FakeRun(table)
        with Rig(run=run):
            pid = s.cells.processes.listener(port)
        return pid, run.calls

    pid, calls = listener(22001, {("ss", "-ltnpH"): (0, ss)})
    check(pid == 4242, "pid слушателя порта из ss (строка без pid пропущена, ищем дальше)")
    check(calls == [["ss", "-ltnpH"]], "ss -ltnpH")
    check(listener(2200, {("ss", "-ltnpH"): (0, ss)})[0] == 2222,
          "negative: порт 2200 не путается с 22001 и 122001 — сравнивается весь номер")
    check(listener(22005, {("ss", "-ltnpH"): (0, ss)})[0] == 5151, "IPv6-слушатель [::]:<port> тоже находится")
    check(listener(22009, {("ss", "-ltnpH"): (0, ss)})[0] == 0, "negative: никто не слушает — 0")
    pid, calls = listener(22001, {("lsof",): (0, "4242\n4243\n")})
    check(pid == 4242 and calls == [["ss", "-ltnpH"], ["lsof", "-nP", "-iTCP:22001", "-sTCP:LISTEN", "-t"]],
          "macOS без ss — lsof -nP -iTCP:<port> -sTCP:LISTEN -t, первый pid")
    check(listener(22001, {("lsof",): (1, "")})[0] == 0, "negative: lsof молчит — 0")
    check(listener(22001, {})[0] == 0, "negative: ни ss, ни lsof — 0")
    pid, calls = listener(22001, {("ss", "-ltnpH"): subprocess.TimeoutExpired(["ss"], 5),
                                  ("lsof",): (0, "4242\n")})
    check(pid == 0 and calls == [["ss", "-ltnpH"]], "boundary: ss завис — 0, к lsof не переходим")


def test_port_health_ok():
    s = make_scout()

    def probe(answers, **kw):
        web = FakeUrlopen({"http://127.0.0.1:": answers})
        with Rig(web=web) as rig:
            ok = s.cells.processes.healthy(22051, **kw)
        return ok, [c["url"] for c in web.calls], [c["timeout"] for c in web.calls], rig.sleeps

    ok, urls, timeouts, sleeps = probe([FakeResponse(status=200)])
    check((ok, urls, timeouts, sleeps) == (True, ["http://127.0.0.1:22051/health"], [2.0], []),
          "по умолчанию: GET /health, таймаут 2 с, 200 — здоров")
    check(probe([FakeResponse(status=200)], health_path="v1/models")[1] == ["http://127.0.0.1:22051/v1/models"],
          "путь без ведущего слэша дополняется им: v1/models → /v1/models")
    for raw in ("", "   ", None):
        check(probe([FakeResponse(status=200)], health_path=raw)[1] == ["http://127.0.0.1:22051/health"],
              f"negative: пустой путь {raw!r} — /health, а не корень сервера")
    check(probe([FakeResponse(status=204)])[0] is True, "204 — тоже здоров: годится любой 2xx")
    ok, urls, _, sleeps = probe([FakeResponse(status=301)], attempts=3)
    check((ok, len(urls), sleeps) == (False, 1, []), "boundary: не-2xx без исключения — сразу нездоров, без повторов")
    ok, urls, timeouts, sleeps = probe(
        [URLError_("refused"), urllib.error.HTTPError("u", 503, "Unavailable", {}, None), FakeResponse(status=200)],
        attempts=3, timeout=4.0)
    check((ok, len(urls), timeouts, sleeps) == (True, 3, [4.0, 4.0, 4.0], [1.0, 1.0]),
          "повторы: две неудачи (в т.ч. 503), третья здорова — пауза 1 с между попытками")
    ok, urls, _, sleeps = probe([URLError_("timed out")], attempts=3, timeout=4.0)
    check((ok, len(urls), sleeps) == (False, 3, [1.0, 1.0]), "все три неудачны — нездоров; после последней паузы нет")
    ok, urls, _, sleeps = probe([URLError_("refused")])
    check((ok, len(urls), sleeps) == (False, 1, []), "negative: одна попытка по умолчанию — без повторов и пауз")
    check(len(probe([URLError_("refused")], attempts=0)[1]) == 1, "boundary: attempts=0 — всё равно одна попытка")
    bare = FakeResponse()
    del bare.status
    check(probe([bare])[0] is True, "boundary: ответ без status читается как 200")


# ── re-adoption after an agent restart, stray reaping ────────────────────

def test_adopt_by_marker():
    port = 22061
    log = TMP / "logs" / f"llama-server.{port}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("0.00.100.000 I load: model type 7B\n"
                   "0.00.200.000 E llama_model_load: error loading model: out of memory\n", encoding="utf-8")
    rec = {"port": port, "kind": "llama", "pid": 4242, "marker": str(LLAMA_BIN),
           "cfg": {"modelPath": "/m/model-q4.gguf", "port": port}, "log": str(log), "cacheModels": True,
           "healthPath": "/health", "startedAt": NOW - 3600}
    s = make_scout({"llamaServerBin": str(LLAMA_BIN)}, {"cells": {str(port): rec}})
    cached = s.models.cache_dir() / MODEL
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"GGUF")
    run = FakeRun({("ps", "-p", "4242"): (0, f"{LLAMA_BIN} --model /m/model-q4.gguf --port {port}\n"),
                   ("pgrep", "-f", str(LLAMA_BIN)): (0, "4242\n")})
    kill = FakeKill(alive={4242})
    rig = Rig(run=run, kill=kill)
    with rig:
        s.cells.adopt_survivors()
        reaper_kills = list(kill.calls)
        slot = s.cells.by_port.get(port)
        st = slot.process.status() if slot else None
    check(st == {"running": True, "pid": 4242, "adopted": True, "startedAt": NOW - 3600, "uptimeSec": 3600,
                 "modelPath": "/m/model-q4.gguf", "port": port},
          "маркер совпал — тот же pid усыновлён с прежним временем старта: деплой не убивает инференс")
    check(s.cells.startup(port) == {"phase": "running", "error": ""}, "фаза усыновлённой ячейки — running")
    check(slot is not None and slot.cache_models is True, "флаг кэша моделей восстановлен из реестра")
    check(run.calls == [["ps", "-p", "4242", "-o", "command="], ["pgrep", "-f", str(LLAMA_BIN)]],
          "negative: при совпавшем маркере порт не опрашивается (ss не вызывался)")
    check(reaper_kills == [] and rig.sleeps == [], "negative: усыновлённый pid не жнётся")
    check(cached.exists(), "negative: хоть одна ячейка усыновлена — кэш моделей не чистится")
    check(disk_cells(s).get(str(port), {}).get("pid") == 4242, "реестр на диске прежний — pid тот же")
    check(f"[llama-node] adopted running cell :{port} (pid 4242)" in rig.journal, "журнал называет ячейку и pid")
    kill.alive.discard(4242)
    with Rig(kill=kill):
        dead = slot.process.status() if slot else None
    check(dead == {"running": False, "exitCode": None,
                   "lastError": "0.00.200.000 E llama_model_load: error loading model: out of memory",
                   "crashed": True},
          "путь лога вернулся из реестра: умерший процесс объясняется строкой из своего лога")


def test_adopt_by_port():
    port = 22062
    rec = {"port": port, "kind": "command", "pid": 4343, "marker": f"bash run_whisper.sh {port}",
           "cfg": {"modelPath": "", "port": port, "cellKind": "command", "command": "bash ~/run_whisper.sh $PORT"},
           "log": "", "cacheModels": True, "healthPath": "/health", "startedAt": NOW - 60}
    s = make_scout({}, {"cells": {str(port): rec}})
    run = FakeRun({("ps", "-p", "4343"): (1, ""), ("ss", "-ltnpH"): (0, ss_line(port, 5151))})
    web = FakeUrlopen({f"http://127.0.0.1:{port}/": FakeResponse(status=200)})
    rig = Rig(run=run, web=web, kill=FakeKill(alive={5151}))
    with rig:
        s.cells.adopt_survivors()
        slot = s.cells.by_port.get(port)
        st = slot.process.status() if slot else {}
    check(st.get("pid") == 5151 and st.get("adopted") is True,
          "маркер пропал (exec-цепочка переписала argv) — усыновлён тот, кто здорово слушает порт ячейки")
    check(disk_cells(s).get(str(port), {}).get("pid") == 5151,
          "реестр на диске переписан на найденный pid — честный для следующего старта")
    check([c["url"] for c in web.calls] == [f"http://127.0.0.1:{port}/health"]
          and [c["timeout"] for c in web.calls] == [4.0],
          "здоровье проверено по записанному healthPath, таймаут 4 с")
    check(rig.sleeps == [] and f"[llama-node] adopted running cell :{port} (pid 5151)" in rig.journal,
          "здоров с первой попытки — без пауз; журнал называет новый pid")
    check(run.calls == [["ps", "-p", "4343", "-o", "command="], ["ss", "-ltnpH"]],
          "без llamaServerBin жатва не зовёт pgrep")

    port = 22066
    rec = {"port": port, "kind": "command", "pid": 1, "marker": "python3 server.py", "cfg": {}, "log": "",
           "cacheModels": False, "healthPath": "/health", "startedAt": NOW}
    s = make_scout({}, {"cells": {str(port): rec}})
    run = FakeRun({("ss", "-ltnpH"): (0, ss_line(port, 6262))})
    with Rig(run=run, web=FakeUrlopen({"http://127.0.0.1:": FakeResponse(status=200)}), kill=FakeKill(alive={6262})):
        s.cells.adopt_survivors()
        slot = s.cells.by_port.get(port)
        st = slot.process.status() if slot else {}
    check(not any(c[0] == "ps" for c in run.calls) and st.get("pid") == 6262,
          "boundary: записанный pid 1 у ps не спрашиваем — сразу по порту")


def test_adopt_nothing_listening():
    port = 22063
    keep = 22068
    recs = {str(port): {"port": port, "kind": "command", "pid": 4444, "marker": f"python3 server.py --port {port}",
                        "cfg": {}, "log": "", "cacheModels": False, "healthPath": "/health", "startedAt": NOW},
            str(keep): {"port": keep, "kind": "command", "pid": 4545, "marker": "python3 keep.py",
                        "cfg": {}, "log": "", "cacheModels": False, "healthPath": "/health", "startedAt": NOW}}
    s = make_scout({}, {"cells": recs})
    run = FakeRun({("ps", "-p", "4444"): (0, ""), ("ps", "-p", "4545"): (0, "/usr/bin/python3 keep.py\n"),
                   ("ss", "-ltnpH"): (0, ss_line(22999, 7777))})
    web = FakeUrlopen()
    with Rig(run=run, web=web, kill=FakeKill(alive={4545})):
        s.cells.adopt_survivors()
    check(str(port) not in disk_cells(s), "никто не слушает порт — ячейка правда ушла: снята с реестра на диске")
    check(port not in s.cells.by_port, "negative: для ушедшей ячейки слот не создан — на доске её нет")
    check(str(keep) in disk_cells(s) and keep in s.cells.by_port, "negative: соседняя живая ячейка осталась и усыновлена")
    check(web.calls == [], "здоровье не проверялось: проверять некого")


def test_adopt_quiet_health():
    port = 22064
    rec = {"port": port, "kind": "command", "pid": 4646, "marker": f"vllm serve org/model --port {port}",
           "cfg": {"port": port}, "log": "", "cacheModels": False, "healthPath": "/v1/models", "startedAt": NOW - 60}
    s = make_scout({}, {"cells": {str(port): rec}})
    run = FakeRun({("ps", "-p", "4646"): (0, ""), ("ss", "-ltnpH"): (0, ss_line(port, 6161))})
    web = FakeUrlopen({"http://127.0.0.1:": URLError_("timed out")})
    rig = Rig(run=run, web=web, kill=FakeKill(alive={6161}))
    with rig:
        s.cells.adopt_survivors()
        slot = s.cells.by_port.get(port)
        st = slot.process.status() if slot else {}
    check(st.get("pid") == 6161,
          "порт занят, но здоровье молчит — всё равно усыновлён: на загруженном хосте это таймаут, а не смерть")
    check(disk_cells(s).get(str(port), {}).get("pid") == 6161,
          "negative: запись не снята (в отличие от пустого порта), pid переписан на слушателя")
    check([c["url"] for c in web.calls] == [f"http://127.0.0.1:{port}/v1/models"] * 3,
          "три попытки по записанному healthPath (/v1/models у vLLM), а не по /health")
    check(rig.sleeps == [1.0, 1.0], "между попытками пауза 1 с, после последней — нет")
    check(f"[llama-node] :{port} holds the port but /v1/models stayed quiet — adopting anyway rather than "
          f"forgetting it" in rig.journal, "журнал объясняет, почему молчащая ячейка усыновлена")
    check(s.cells.startup(port) == {"phase": "running", "error": ""},
          "фаза записана running — дальше живость решает опрос процесса")


def test_adopt_bad_records():
    s = make_scout({}, {"cells": {"junk": {"port": "abc", "pid": 7},
                                  "22065": {"pid": 4747, "marker": "python3 srv.py", "cfg": {}}}})
    run = FakeRun({("ps", "-p", "4747"): (0, "/usr/bin/python3 srv.py --port 22065\n")})
    with Rig(run=run, kill=FakeKill(alive={4747})):
        _, err = attempt(s.cells.adopt_survivors)
    check(err is None and "junk" in s.state["cells"] and set(s.cells.by_port) == {22065},
          "битая запись (порт не число) пропущена молча: не снята и не усыновлена")
    check(22065 in s.cells.by_port, "negative: запись без поля port усыновлена на порт из ключа реестра")


def reap(pgrep_out, keep=None, bin_path=None, kill=None):
    s = make_scout({"llamaServerBin": str(LLAMA_BIN) if bin_path is None else bin_path})
    cached = s.models.cache_dir() / MODEL
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"GGUF")
    table = {} if pgrep_out is None else {("pgrep", "-f", str(LLAMA_BIN)): (0 if pgrep_out else 1, pgrep_out)}
    rig = Rig(run=FakeRun(table), kill=kill)
    with rig:
        rig.res, rig.err = attempt(lambda: s.cells.reap_strays(keep_pids=keep))
    rig.s, rig.cached = s, cached
    return rig


def test_reap_strays():
    own = os.getpid()
    kill = FakeKill(alive={111, 222, 4242, own}, stubborn={111})
    r = reap(f"111\n222\n{own}\n4242\nabc\n", keep={4242}, kill=kill)
    check(r.run.calls == [["pgrep", "-f", str(LLAMA_BIN)]], "бродяги ищутся pgrep -f по пути бинаря")
    check(kill.calls == [(111, signal.SIGTERM), (222, signal.SIGTERM), (111, 0), (111, signal.SIGKILL), (222, 0)],
          "TERM всем бродягам, пауза, затем KILL только выжившим")
    check(r.sleeps == [2], "между TERM и проверкой — 2 с")
    check(all(pid not in (own, 4242) for pid, _ in kill.calls),
          "negative: свой pid и усыновлённые не трогаются, мусор в выводе pgrep пропущен")
    check("[llama-node] reaping 2 stray llama-server(s): [111, 222]" in r.journal, "журнал называет бродяг")
    check(r.cached.exists(), "negative: есть усыновлённые — кэш моделей не чистится")

    r = reap("111\n", keep=None, kill=FakeKill(alive={111}))
    check(not r.cached.exists(), "бродяги убиты и никого не усыновили — кэш моделей вычищен")
    r = reap("4242\n", keep={4242}, kill=FakeKill(alive={4242}))
    check(r.kill.calls == [] and r.sleeps == [] and r.cached.exists(),
          "negative: бродяг нет (только усыновлённые) — ни сигналов, ни паузы, ни чистки")
    r = reap("", keep=None)
    check(r.kill.calls == [] and r.cached.exists(),
          "negative: бродяг нет и никого не усыновили — кэш всё равно не чистится")
    r = reap(None, bin_path="")
    check(r.run.calls == [] and r.kill.calls == [], "без llamaServerBin — ни pgrep, ни сигналов")
    r = reap(None)
    check(r.err is None and r.kill.calls == [] and r.cached.exists(), "pgrep нет на хосте — тихо ничего")
    kill = FakeKill(alive={333, 444}, forbidden={333})
    r = reap("333\n444\n", keep=None, kill=kill)
    check(r.err is None and kill.calls == [(333, signal.SIGTERM), (444, signal.SIGTERM), (333, 0), (444, 0)],
          "чужой процесс (EPERM) не роняет жатву — остальные обработаны")


# ── saved launch configs ──────────────────────────────────────────────────

def test_node_configs():
    s = make_scout()
    d = s.configs.dir
    check(s.configs.listing() == [], "папки бэкапов нет — пустой список")
    with Rig(), patched(time, localtime=GMTIME):
        s.configs.save("/models/org/model-q4.gguf", 22071, 99, 8192)
    name = "llama-node.bak.20260921-141320.json"
    check(sorted(p.name for p in d.iterdir()) == [name],
          "бэкап назван по времени сохранения; временного .tmp не осталось")
    saved = {"savedAt": "20260921-141320", "savedAtTs": NOW, "modelPath": "/models/org/model-q4.gguf",
             "modelName": "model-q4.gguf", "port": 22071, "gpuLayers": 99, "ctxSize": 8192}
    check(json.loads((d / name).read_text(encoding="utf-8")) == saved, "бэкап — параметры запуска и время")
    check(s.configs.listing() == [{**saved, "filename": name}], "список отдаёт содержимое и имя файла")
    with Rig(), patched(time, localtime=GMTIME):
        s.configs.save("/models/org/other-q8.gguf", 22072, 10, 4096)
    check(sorted(p.name for p in d.iterdir()) == [name]
          and json.loads((d / name).read_text(encoding="utf-8"))["modelName"] == "other-q8.gguf",
          "as-is: два сохранения в одну секунду — второе молча затирает первое")

    s = make_scout()
    d = s.configs.dir
    d.mkdir(parents=True)
    for day in range(1, 22):
        (d / f"llama-node.bak.202609{day:02d}-000000.json").write_text(json.dumps({"day": day}), encoding="utf-8")
    (d / "notes.json").write_text("{}", encoding="utf-8")
    rows = s.configs.listing()
    check([r["filename"] for r in rows] == [f"llama-node.bak.202609{day:02d}-000000.json" for day in range(21, 1, -1)],
          "новейшие первыми, не больше 20; чужие файлы не в списке")
    (d / "llama-node.bak.20260922-000000.json").write_text("{broken", encoding="utf-8")
    rows = s.configs.listing()
    check(len(rows) == 19 and rows[0]["filename"] == "llama-node.bak.20260921-000000.json",
          "as-is: битый файл среди 20 новейших съедает место — строк 19, хотя валидных старше хватает")


def test_delete_node_config():
    s = make_scout()
    d = s.configs.dir
    d.mkdir(parents=True)
    mine = "llama-node.bak.20260901-000000.json"
    (d / mine).write_text("{}", encoding="utf-8")
    outside = d.parent / "llama-node.bak.20260902-000000.json"
    outside.write_text("{}", encoding="utf-8")
    evil = d.parent / "evil.json"
    evil.write_text("{}", encoding="utf-8")
    (d / "llama-node.bak.x").mkdir()
    (d / "state.json").write_text("{}", encoding="utf-8")

    _, err = attempt(lambda: s.configs.delete("../llama-node.bak.20260902-000000.json"))
    check(err_is(err, 404, "backup not found: llama-node.bak.20260902-000000.json") and outside.exists(),
          "путь с ../ срезан до имени: ищется только в папке бэкапов, файл снаружи цел")
    _, err = attempt(lambda: s.configs.delete("llama-node.bak.x/../../evil.json"))
    check(err_is(err, 400, "invalid backup filename") and evil.exists(),
          "negative: ../ внутри имени не выводит за папку бэкапов — отказ 400, файл цел")
    _, err = attempt(lambda: s.configs.delete("state.json"))
    check(err_is(err, 400, "invalid backup filename") and (d / "state.json").exists(),
          "negative: чужое имя (не llama-node.bak.*.json) — отказ 400, файл цел")
    _, err = attempt(lambda: s.configs.delete("llama-node.bak.1.txt"))
    check(err_is(err, 400, "invalid backup filename"), "negative: не .json — отказ 400")
    _, err = attempt(lambda: s.configs.delete("llama-node.bak.20991231-000000.json"))
    check(err_is(err, 404, "backup not found: llama-node.bak.20991231-000000.json"), "нет такого бэкапа — 404")
    _, err = attempt(lambda: s.configs.delete(mine))
    check(err is None and not (d / mine).exists(), "существующий бэкап удалён")


# ── LlamaStart: validation, then the launch in the background ────────────

def test_llama_start_refusals():
    r = start_llama({"modelPath": MODEL}, config={"llamaServerBin": ""})
    check(err_is(r.err, 400, "llamaServerBin not configured in config.json — run install.sh first"),
          "нет llamaServerBin — отказ 400 с подсказкой")
    check(r.threads.made == [] and r.run.calls == [] and r.s.cells.by_port == {},
          "negative: отказ ничего не запускает, не открывает порт и не оставляет слота")
    r = start_llama({"config": {"PORT": 22081}})
    check(err_is(r.err, 400, "modelPath is required") and r.s.cells.by_port == {}, "нет модели — отказ 400, слота нет")
    r = start_llama({"modelPath": "   ", "config": {"PORT": 22081}})
    check(err_is(r.err, 400, "modelPath is required"), "boundary: модель из пробелов — тоже отказ")

    port = 22086

    def running(s):
        s.cells.at(port).process.adopt(5151, {"port": port})
    r = start_llama({"modelPath": MODEL, "config": {"MODEL_FILE": MODEL, "PORT": port}}, prep=running,
                    kill=FakeKill(alive={5151}))
    check(r.res == {"ok": False, "error": f"a server is already running on port {port}"},
          "на порту уже работает сервер — отказ (не исключение)")
    check(r.threads.made == [] and r.run.calls == [], "negative: при отказе воркер не запущен, ufw не тронут")
    for phase in ("resolving", "downloading", "loading"):
        r = start_llama({"modelPath": MODEL, "config": {"MODEL_FILE": MODEL, "PORT": port}},
                        prep=lambda s, ph=phase: s.cells.report(port, phase=ph))
        check(r.res == {"ok": False, "error": f"startup already in progress on port {port} ({phase})",
                        "phase": phase} and r.threads.made == [],
              f"старт уже идёт ({phase}) — второй отказан, с фазой")
    for phase in ("error", "idle", "running"):
        r = start_llama({"modelPath": MODEL, "config": {"MODEL_FILE": MODEL, "PORT": port}},
                        prep=lambda s, ph=phase: s.cells.report(port, phase=ph))
        check((r.res or {}).get("ok") is True and len(r.threads.made) == 1,
              f"negative: фаза {phase} без живого процесса не мешает новому старту")


def test_llama_start_accepted():
    port = 22085
    payload = {"modelPath": MODEL, "cacheModels": True, "args": llama_args(port),
               "config": {"MODEL_FILE": MODEL, "PORT": port, "MMPROJ_FILE": f" {MMPROJ} ",
                          "SPEC_DRAFT_MODEL_FILE": SPEC}}
    r = start_llama(payload)
    check(r.res == {"ok": True, "status": "starting", "phase": "resolving", "port": port},
          "старт отвечает сразу: starting/resolving — загрузка и подъём уходят в фон")
    t = r.threads.made[0] if r.threads.made else None
    check(t is not None and t.started and not t.ran and t.daemon is True,
          "воркер запущен фоновым daemon-потоком и в запросе не выполнялся")
    check(t is not None and isinstance(getattr(t.target, "__self__", None), LlamaLaunch)
          and t.target.__name__ == "run", "цель потока — LlamaLaunch.run")
    check(t is not None and launch_inputs(t) == (
        port, str(LLAMA_BIN),
        {"MODEL_FILE": MODEL, "PORT": port, "MMPROJ_FILE": f" {MMPROJ} ", "SPEC_DRAFT_MODEL_FILE": SPEC,
         "HOST": "0.0.0.0"},
        MODEL, MMPROJ, SPEC, True, llama_args(port)),
        "аргументы воркера: порт, бинарь, config с HOST по умолчанию, пути (обрезанные), кэш, аргументы контроллера")
    check(r.run.calls == [["sudo", "-n", "ufw", "allow", str(port)]], "порт открыт в ufw: sudo -n ufw allow <port>")
    check(r.s.cells.views() == [{"running": False, "phase": "resolving", "modelPath": MODEL, "port": port,
                                        "downloadedBytes": 0, "totalBytes": 0, "downloadingFile": "",
                                        "startedAt": NOW}],
          "на доске сразу фаза resolving с моделью и временем старта")
    check(r.s.cells.by_port[port].cache_models is True if port in r.s.cells.by_port else False,
          "флаг кэша моделей из запроса записан в слот")

    r = start_llama({"modelPath": MODEL, "port": 22082, "gpuLayers": 20, "ctxSize": 8192})
    t = r.threads.made[0] if r.threads.made else None
    check(t is not None and launch_inputs(t) == (
        22082, str(LLAMA_BIN),
        {"MODEL_FILE": MODEL, "PORT": 22082, "N_GPU_LAYERS": 20, "CTX_SIZE": 8192, "HOST": "0.0.0.0"},
        MODEL, "", "", False, None),
        "старый вызов без config: config собран из modelPath/port/gpuLayers/ctxSize; без args — None")
    r = start_llama({"modelPath": MODEL, "gpuLayers": 20, "args": "--model x",
                     "config": {"MODEL_FILE": MODEL, "PORT": 22083, "HOST": "127.0.0.1"}})
    t = r.threads.made[0] if r.threads.made else None
    check(t is not None and launch_inputs(t)[2] == {"MODEL_FILE": MODEL, "PORT": 22083, "HOST": "127.0.0.1"}
          and launch_inputs(t)[7] is None,
          "negative: при непустом config старые поля не подмешиваются, свой HOST сохранён, args не списком — None")
    r = start_llama({"modelPath": "models/org/picked.gguf", "config": {"MODEL_FILE": MODEL, "PORT": 22084}})
    t = r.threads.made[0] if r.threads.made else None
    check(t is not None and launch_inputs(t)[3] == "models/org/picked.gguf" and launch_inputs(t)[2]["MODEL_FILE"] == MODEL,
          "boundary: modelPath запроса сильнее MODEL_FILE из config (а в config остаётся свой)")


def test_llama_start_port_order():
    def port_of(payload, config=None):
        r = start_llama({"modelPath": MODEL, **payload}, config=config)
        return (r.res or {}).get("port"), (launch_inputs(r.threads.made[0])[2].get("PORT") if r.threads.made else None)

    check(port_of({"port": 22002, "config": {"MODEL_FILE": MODEL, "PORT": "22001"}}) == (22001, 22001),
          "порт: config.PORT первым (строка приведена к числу и в самом config)")
    check(port_of({"port": 22002, "config": {"MODEL_FILE": MODEL}}) == (22002, 22002),
          "negative: без config.PORT — port запроса")
    check(port_of({"config": {"MODEL_FILE": MODEL}}, config={"llamaNodeDefaultPort": 22099}) == (22099, 22099),
          "без обоих — llamaNodeDefaultPort агента")
    check(port_of({"config": {"MODEL_FILE": MODEL}}, config={"llamaNodeDefaultPort": 0}) == (8180, 8180),
          "boundary: и его нет — 8180")


def test_llama_start_misc():
    r = start_llama({"modelPath": MODEL, "config": {"MODEL_FILE": MODEL, "PORT": 22087}}, run=FakeRun())
    check((r.res or {}).get("ok") is True and r.run.calls == [["sudo", "-n", "ufw", "allow", "22087"]],
          "ufw нет (или нет sudo) — старт всё равно идёт")
    r = start_llama({"modelPath": MODEL, "config": {"MODEL_FILE": MODEL, "PORT": 22088}},
                    config={"cacheModels": True})
    check(r.threads.made and launch_inputs(r.threads.made[0])[6] is True, "без cacheModels в запросе — берётся из config.json")
    r = start_llama({"modelPath": MODEL, "cacheModels": False, "config": {"MODEL_FILE": MODEL, "PORT": 22089}},
                    config={"cacheModels": True})
    check(r.threads.made and launch_inputs(r.threads.made[0])[6] is False, "negative: cacheModels запроса сильнее config.json")

    r = start_llama({"cellKind": "command", "command": "python3 srv.py", "config": {"PORT": 22090}},
                    config={"llamaServerBin": ""})
    check(isinstance(r.err, AppError) and "shellLine" in str(r.err),
          "cellKind=command уходит в командную ячейку — ей llamaServerBin не нужен")
    r = start_llama({"config": {"CELL_KIND": " Command ", "COMMAND": "python3 srv.py", "PORT": 22090}},
                    config={"llamaServerBin": ""})
    check(isinstance(r.err, AppError) and "shellLine" in str(r.err),
          "CELL_KIND из config тоже работает, без учёта регистра и пробелов")

    port = 22091

    def failed_before(s):
        s.cells.report(port, phase="error", error="model download failed",
                             downloadingFile="old-model.gguf (1/2)")
    r = start_llama({"modelPath": MODEL, "config": {"MODEL_FILE": MODEL, "PORT": port}}, prep=failed_before)
    view = (r.s.cells.views() or [{}])[0]
    check(view.get("phase") == "resolving" and view.get("downloadingFile") == "old-model.gguf (1/2)",
          "as-is: ДЕФЕКТ — имя файла из прошлого, упавшего старта показывается в новом старте как текущее")


def test_llama_start_then_worker():
    port = 22092
    s = make_scout({"controllerUrl": CONTROLLER, "llamaServerBin": str(LLAMA_BIN)})
    rig = Rig(popen=FakePopen({"pid": 4949}), run=FakeRun({UFW_ALLOW: (0, "")}), web=models_served())
    with rig:
        attempt(lambda: s.cells.start({"modelPath": MODEL, "args": llama_args(port),
                                            "config": {"MODEL_FILE": MODEL, "PORT": port}}))
        for t in rig.threads.made:
            t.run_now()
    check(disk_cells(s).get(str(port), {}).get("pid") == 4949 and s.cells.startup(port).get("phase") == "running",
          "весь путь: старт → фоновый воркер → модель скачана, процесс поднят, ячейка в реестре, фаза running")


# ── command cells ─────────────────────────────────────────────────────────

def test_command_cell_start():
    port = 22201
    cmd = "exec bash ~/run_whisper.sh $PORT --lang en"
    shell = f"set -euo pipefail; export PORT={port}; {cmd}"
    r = start_command({"cellKind": "command", "command": cmd, "shellLine": shell, "cacheModels": False,
                       "healthPath": "/v1/health", "config": {"PORT": port}})
    log = r.s.models.cache_dir() / f"command-cell.{port}.log"
    check(r.res == {"ok": True, "pid": 7070, "port": port}, "командная ячейка стартует и отвечает pid и портом")
    spawn = r.popen.calls[0] if r.popen.calls else {}
    check(spawn.get("argv") == ["bash", "-lc", shell], "запускается ровно строка контроллера через bash -lc")
    check(getattr(spawn.get("stdout"), "name", None) == str(log), "лог — свой для порта: command-cell.<port>.log")
    check(disk_cells(r.s).get(str(port)) == {
        "port": port, "kind": "command", "pid": 7070, "marker": f"bash run_whisper.sh {port} --lang en",
        "cfg": {"modelPath": "", "port": port, "cellKind": "command", "command": "bash ~/run_whisper.sh $PORT --lang en"},
        "log": str(log), "cacheModels": True, "healthPath": "/v1/health", "startedAt": NOW},
        "ячейка в реестре: маркер с раскрытым $PORT и без ~/, cfg с командой без exec, healthPath от контроллера")
    check(r.s.cells.startup(port) == {"phase": "running", "modelPath": "bash ~/run_whisper.sh $PORT --lang en",
                                           "downloadedBytes": 0, "totalBytes": 0, "error": "", "startedAt": NOW},
          "фаза running; в modelPath записи старта — сама команда")
    check(port in r.s.cells.by_port and r.s.cells.by_port[port].cache_models is True,
          "кэш моделей командной ячейки включён принудительно, хотя запрос просил false: её файлы не качаются заново")
    check(r.threads.made == [], "командная ячейка стартует в потоке запроса — фонового воркера нет")
    check(r.run.calls == [["sudo", "-n", "ufw", "allow", str(port)]], "порт открыт в ufw")

    r = start_command({"cellKind": "command", "shellLine": "exec python3 srv.py",
                       "config": {"PORT": 22202, "COMMAND": "python3 srv.py"}})
    check(disk_cells(r.s).get("22202", {}).get("cfg", {}).get("command") == "python3 srv.py",
          "без command в запросе — COMMAND из config")
    r = start_command({"cellKind": "command", "command": "python3 srv.py", "shellLine": "exec python3 srv.py",
                       "port": 22203, "config": {}}, config={"llamaNodeDefaultPort": 22299})
    check((r.res or {}).get("port") == 22203, "порт: config.PORT, затем port запроса")
    r = start_command({"cellKind": "command", "command": "python3 srv.py", "shellLine": "exec python3 srv.py",
                       "config": {}}, config={"llamaNodeDefaultPort": 22299})
    check((r.res or {}).get("port") == 22299, "negative: без обоих — llamaNodeDefaultPort агента")


def test_command_exec_stripping():
    cases = [("exec bash run.sh", "bash run.sh", "ведущий exec срезан — pid ячейки должен быть самим сервером"),
             ("  exec   python3 srv.py $PORT", "python3 srv.py $PORT", "exec с пробелами вокруг тоже срезан"),
             ("executor --serve $PORT", "executor --serve $PORT", "boundary: слово, начинающееся на exec, — не exec"),
             ("bash -c 'exec x'", "bash -c 'exec x'", "negative: exec в середине строки не трогается")]
    for i, (raw, want, msg) in enumerate(cases):
        port = 22210 + i
        r = start_command({"cellKind": "command", "command": raw, "shellLine": "exec true", "config": {"PORT": port}})
        check(disk_cells(r.s).get(str(port), {}).get("cfg", {}).get("command") == want, msg)


def test_command_cell_refusals():
    port = 22220
    r = start_command({"cellKind": "command", "command": "bash ~/run_whisper.sh $PORT", "config": {"PORT": port}})
    check(err_is(r.err, 400, "controller sent no shellLine for this command cell — it is older than this agent "
                             "(needs lama-caravan v1.3.115+)"),
          "без shellLine — отказ 400, а не строка, собранная заново из command")
    check(r.popen.calls == [] and disk_cells(r.s) == {}, "negative: без shellLine ничего не запущено и не записано")
    check(r.run.calls == [["sudo", "-n", "ufw", "allow", str(port)]],
          "as-is: ДЕФЕКТ — порт открыт в ufw ещё до отказа: файрвол открыт для ячейки, которая не стартовала")
    r = start_command({"cellKind": "command", "command": "   ", "shellLine": "exec true", "config": {"PORT": 22221}})
    check(err_is(r.err, 400, "command is required for a command cell") and r.run.calls == [],
          "пустая команда — отказ 400, ufw не тронут")

    port = 22222

    def running(s):
        s.cells.at(port).process.adopt(5151, {"port": port})
    r = start_command({"cellKind": "command", "command": "python3 srv.py", "shellLine": "exec python3 srv.py",
                       "config": {"PORT": port}}, prep=running, kill=FakeKill(alive={5151}))
    check(r.res == {"ok": False, "error": f"a server is already running on port {port}"} and r.popen.calls == [],
          "на порту уже работает процесс — отказ, второй не запущен")
    r = start_command({"cellKind": "command", "command": "python3 srv.py", "shellLine": "exec python3 srv.py",
                       "config": {"PORT": port}}, prep=lambda s: s.cells.report(port, phase="loading"))
    check(r.res == {"ok": False, "error": f"startup already in progress on port {port} (loading)", "phase": "loading"},
          "старт уже идёт — второй отказан")

    port = 22223
    r = start_command({"cellKind": "command", "command": "python3 srv.py", "shellLine": "exec python3 srv.py",
                       "config": {"PORT": port}}, popen=FakePopen(OSError("exec failed")))
    check(r.res == {"ok": False, "error": "exec failed"}, "процесс не запустился — ответ с причиной")
    st = r.s.cells.startup(port)
    check((st.get("phase"), st.get("error")) == ("error", "exec failed") and disk_cells(r.s) == {},
          "negative: не стартовавшая ячейка — фаза error, в реестр не пишется")

    port = 22224
    r = start_command({"cellKind": "command", "command": "python3 srv.py", "shellLine": "exec python3 srv.py",
                       "config": {"PORT": port}}, make_cache_dir=False)
    log = r.s.models.cache_dir() / f"command-cell.{port}.log"
    check(r.res == {"ok": False, "error": f"[Errno 2] No such file or directory: '{log}'"},
          "as-is: ДЕФЕКТ — на хосте без папки моделей командная ячейка не стартует: её лог открывается в "
          "несуществующей папке, а создаёт папку только загрузка модели")


def test_command_cell_model():
    asr = "models/org/asr-q8.gguf"
    command = f"python3 -m asr_server --model {asr} --port $PORT"

    def cached(s):
        f = s.models.cache_dir() / asr
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"GGUF cached asr")
    port = 22230
    r = start_command({"cellKind": "command", "command": command, "shellLine": "exec python3 -m asr_server",
                       "config": {"PORT": port, "MODEL_FILE": asr}}, prep=cached, web=FakeUrlopen())
    mp = str(r.s.models.cache_dir() / asr)
    check(disk_cells(r.s).get(str(port), {}).get("cfg", {}).get("modelPath") == mp,
          "MODEL_FILE найден в кэше — его путь в cfg: по нему чистка кэша не тронет файл работающей ячейки")
    check(r.web.calls == [], "negative: закэшированная модель не перекачивается")

    port = 22231
    web = models_served({asr: b"GGUF asr-q8"})
    r = start_command({"cellKind": "command", "command": command, "shellLine": "exec python3 -m asr_server",
                       "config": {"PORT": port, "MODEL_FILE": asr}}, config={"controllerUrl": CONTROLLER}, web=web)
    f = r.s.models.cache_dir() / asr
    check(f.exists() and f.read_bytes() == b"GGUF asr-q8"
          and disk_cells(r.s).get(str(port), {}).get("cfg", {}).get("modelPath") == str(f),
          "модели нет — скачана с контроллера до старта, путь в cfg")
    check(r.threads.made == [] and (r.res or {}).get("ok") is True,
          "as-is: ДЕФЕКТ — загрузка идёт в потоке запроса: ответ контроллеру ждёт весь файл "
          "(llama-путь ради этого ушёл в фон)")

    port = 22232
    r = start_command({"cellKind": "command", "command": command, "shellLine": "exec python3 -m asr_server",
                       "config": {"PORT": port, "MODEL_FILE": asr}})
    why = f"model not found locally and controllerUrl not set: {asr}"
    check(r.res == {"ok": False, "error": f"model not available: {why}"},
          "модель недоступна — отказ с причиной, а не ячейка, тихо упавшая в своём логе")
    st = r.s.cells.startup(port)
    check((st.get("phase"), st.get("error")) == ("error", why), "фаза error с той же причиной")
    check(r.popen.calls == [] and disk_cells(r.s) == {}, "negative: без модели не запущено и не записано")


def test_command_cell_markers():
    def marker(port, command):
        r = start_command({"cellKind": "command", "command": command, "shellLine": "exec true",
                           "config": {"PORT": port}})
        return disk_cells(r.s).get(str(port), {}).get("marker")

    check(marker(22240, "python3 srv.py --port $PORT " + "x" * 200)
          == "python3 srv.py --port 22240 " + "x" * 92, "boundary: маркер обрезан до 120 символов")
    check(marker(22241, "python3 srv.py --port ${PORT}") == "python3 srv.py --port ${PORT}",
          "as-is: ДЕФЕКТ — ${PORT} в маркере не раскрывается: ps его не покажет, переусыновление только по порту")
    check(marker(22242, "bash $HOME/run_moonshine.sh $PORT en") == "bash $HOME/run_moonshine.sh 22242 en",
          "as-is: ДЕФЕКТ — $HOME в маркере не раскрывается (так пишет контроллер): маркер не совпадёт с ps")


def test_command_cell_syncs_assets_first():
    port = 22250
    events = []
    home = TMP / "home-sync"
    home.mkdir(parents=True, exist_ok=True)
    launcher = b"#!/bin/bash\nexec python3 whisper_server.py \"$@\"\n"
    manifest = {"runners": {"whisper": ["run_whisper.sh"]},
                "assets": {"run_whisper.sh": {"sha256": hashlib.sha256(launcher).hexdigest()}}}
    web = FakeUrlopen({"/api/cell-assets": lambda url: FakeResponse(json.dumps(manifest).encode()),
                       "/api/cell-assets/file": lambda url: FakeResponse(launcher)}, events=events)
    popen = FakePopen({"pid": 7171}, on_call=lambda argv, kw: events.append(("popen", argv[0])))
    r = start_command({"cellKind": "command", "command": "bash $HOME/run_whisper.sh $PORT", "shellLine": "exec true",
                       "config": {"PORT": port}},
                      config={"controllerUrl": CONTROLLER + "/", "controllerToken": "tok-1"},
                      popen=popen, web=web, home=home)
    check([e[0] for e in events] == ["urlopen", "urlopen", "popen"],
          "ячейка сначала сверяет свои файлы с контроллером, потом стартует")
    check(web.calls[:1] == [{"url": f"{CONTROLLER}/api/cell-assets", "headers": {"X-caravan-token": "tok-1"},
                             "timeout": 10}], "манифест запрошен с токеном флота")
    check((home / "run_whisper.sh").exists() and (home / "run_whisper.sh").read_bytes() == launcher,
          "лаунчер лёг в $HOME")
    check(f"[llama-node] cell-assets :{port} — run_whisper.sh=updated" in r.journal, "журнал называет, что обновлено")

    def broken_sync(*_a, **_k):
        raise RuntimeError("boom")
    port = 22251
    with patched(CellAssets, sync=broken_sync):
        r = start_command({"cellKind": "command", "command": "bash $HOME/run_whisper.sh $PORT",
                           "shellLine": "exec true", "config": {"PORT": port}})
    check((r.res or {}).get("ok") is True and f"[llama-node] cell-assets :{port} skipped (boom)" in r.journal,
          "negative: сбой синхронизации не роняет старт — ячейка идёт на локальных копиях, причина в журнале")


def test_resolve_arg_paths():
    s = make_scout()
    args = ["--model", "{{MODEL_PATH}}", "--mmproj", "{{MMPROJ_PATH}}", "--model-draft", "{{SPEC_PATH}}",
            "--alias", "{{MODEL_PATH}}-x", "--port", "22001"]
    got = LlamaLaunch.resolve_paths(args, "/c/model-q4.gguf", "/c/mmproj-f16.gguf", "/c/draft-q8.gguf")
    check(got == ["--model", "/c/model-q4.gguf", "--mmproj", "/c/mmproj-f16.gguf", "--model-draft",
                  "/c/draft-q8.gguf", "--alias", "{{MODEL_PATH}}-x", "--port", "22001"],
          "плейсхолдеры контроллера заменены путями скачанных файлов")
    check(got[7] == "{{MODEL_PATH}}-x", "negative: плейсхолдер внутри токена не трогается — замена только целым аргументом")
    check(args[1] == "{{MODEL_PATH}}", "negative: список контроллера не изменён — возвращена копия")
    check(LlamaLaunch.resolve_paths(["--mmproj", "{{MMPROJ_PATH}}", "--model-draft", "{{SPEC_PATH}}"], "/c/m.gguf", "", None)
          == ["--mmproj", "", "--model-draft", ""],
          "boundary: нет mmproj/spec — плейсхолдер становится пустым аргументом, а не исчезает")


# ── the llama startup worker ──────────────────────────────────────────────

def test_worker_success():
    port = 22101
    s = worker_scout()
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port, "N_GPU_LAYERS": "auto", "CTX_SIZE": 8192})
    cache = s.models.cache_dir()
    mp = str(cache / MODEL)
    log = str(cache / f"llama-server.{port}.log")
    cd = SERVER_CELLS_DIR / str(port)
    cfg = {"modelPath": mp, "mmprojPath": "", "specPath": "", "specType": "", "port": port,
           "gpuLayers": 999, "ctxSize": 8192}
    artifact = {"dir": str(cd), "startScript": str(cd / "start.sh"), "cellJson": str(cd / "cell.json"),
                "generatedAt": NOW}
    check(r.err is None, "воркер отработал без исключения")
    spawn = r.popen.calls[0] if r.popen.calls else {}
    check(spawn.get("argv") == [str(LLAMA_BIN), "--model", mp, "--port", str(port)],
          "llama-server запущен с аргументами контроллера, плейсхолдер заменён путём скачанной модели")
    check(getattr(spawn.get("stdout"), "name", None) == log,
          "лог — свой для порта (llama-server.<port>.log): упавшая ячейка цитирует свой лог, а не соседа")
    check(disk_cells(s).get(str(port)) == {"port": port, "kind": "llama", "pid": 4242, "marker": str(LLAMA_BIN),
                                           "cfg": {**cfg, "artifact": artifact}, "log": log, "cacheModels": False,
                                           "healthPath": "/health", "startedAt": NOW},
          "успешный старт в реестре: маркер — путь бинаря, cfg с артефактом, лог порта")
    size = len(BODIES[MODEL])
    check(s.cells.startup(port) == {"phase": "running", "error": "", "downloadedBytes": size, "totalBytes": size,
                                         "downloadingFile": "model-q4.gguf"},
          "фаза running; прогресс загрузки остаётся в записи старта")
    cell = json.loads((cd / "cell.json").read_text(encoding="utf-8")) if (cd / "cell.json").exists() else {}
    check(cell.get("runtime") == cfg, "cell.json.runtime — cfg без артефакта: артефакт дописан после записи файла")
    check(cell.get("config") == {"MODEL_FILE": MODEL, "PORT": port, "N_GPU_LAYERS": "auto", "CTX_SIZE": 8192},
          "cell.json.config — конфиг формы как пришёл")
    start = (cd / "start.sh").read_text(encoding="utf-8") if (cd / "start.sh").exists() else ""
    check(f"exec {shlex.quote(str(LLAMA_BIN))} --model {shlex.quote(mp)} --port {port} \"$@\"" in start,
          "start.sh повторяет ту же командную строку")
    check([c["url"] for c in r.web.calls] == [f"{CONTROLLER}/api/models/download?path=models/org/model-q4.gguf"],
          "модель скачана с контроллера один раз")
    check(r.web.calls and r.web.calls[0]["timeout"] == 3600, "загрузка модели с таймаутом 3600 с")


def test_worker_download_error():
    port = 22102
    s = make_scout({"llamaServerBin": str(LLAMA_BIN)})
    shutil.rmtree(SERVER_CELLS_DIR / str(port), ignore_errors=True)
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port})
    check(s.cells.startup(port) == {"phase": "error",
                                         "error": f"model not found locally and controllerUrl not set: {MODEL}"},
          "модели нет и контроллер не задан — фаза error с причиной")
    check(r.err is None and r.popen.calls == [], "negative: без модели процесс не запускается")
    check(not (SERVER_CELLS_DIR / str(port)).exists() and disk_cells(s) == {},
          "negative: ни артефактов, ни записи в реестре")

    port = 22103
    s = worker_scout()
    refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port},
               web=FakeUrlopen({"/api/models/download": refused}))
    st = s.cells.startup(port)
    check(st.get("phase") == "error"
          and st.get("error") == "model download failed: <urlopen error [Errno 111] Connection refused>",
          "defect-history: контроллер так и не ответил — на доске причина загрузки, а не TypeError из ветки повтора")
    check(len(r.web.calls) == 4 and r.sleeps == [5, 15, 30],
          "defect-history: перед отказом — три повтора с паузами 5, 15, 30 с; раньше ни одного")

    port = 22109
    s = worker_scout()
    spec_args = llama_args(port) + ["--model-draft", "{{SPEC_PATH}}"]
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port, "SPEC_DRAFT_MODEL_FILE": SPEC}, spec=SPEC,
               args=spec_args)
    argv = r.popen.calls[0]["argv"] if r.popen.calls else []
    check(s.cells.startup(port).get("phase") == "running" and len(r.web.calls) == 2
          and argv[-2:] == ["--model-draft", str(s.models.cache_dir() / SPEC)],
          "defect-history: черновая модель без mmproj — оба файла скачаны, процесс запущен с путём черновика; "
          "раньше «list index out of range» и ячейка не стартовала")
    port = 22100
    s = worker_scout()
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port}, mmproj=MMPROJ, spec=SPEC,
               args=["--model", "{{MODEL_PATH}}", "--mmproj", "{{MMPROJ_PATH}}", "--model-draft", "{{SPEC_PATH}}"])
    cache = s.models.cache_dir()
    check((r.popen.calls[0]["argv"] if r.popen.calls else None) == [
        str(LLAMA_BIN), "--model", str(cache / MODEL), "--mmproj", str(cache / MMPROJ),
        "--model-draft", str(cache / SPEC)],
        "negative: модель + mmproj + черновая — все три скачаны и подставлены на свои места")


def test_worker_log_dir_missing():
    port = 22139
    s = make_scout({"llamaServerBin": str(LLAMA_BIN)})
    r = worker(s, port, {"MODEL_FILE": str(ABS_MODEL), "PORT": port}, model=str(ABS_MODEL), make_cache_dir=False)
    log = s.models.cache_dir() / f"llama-server.{port}.log"
    check(s.cells.startup(port).get("error") == f"[Errno 2] No such file or directory: '{log}'"
          and r.popen.calls == [],
          "as-is: ДЕФЕКТ — модель по абсолютному пути, папки кэша нет: лог открывается в несуществующей папке, "
          "ячейка не стартует")
    port = 22140
    s = make_scout({"llamaServerBin": str(LLAMA_BIN)})
    r = worker(s, port, {"MODEL_FILE": str(ABS_MODEL), "PORT": port}, model=str(ABS_MODEL))
    check(len(r.popen.calls) == 1 and s.cells.startup(port).get("phase") == "running",
          "negative: папка кэша есть — та же ячейка стартует")


def test_worker_refuses_without_args():
    port = 22104
    s = worker_scout()
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port}, args=None)
    check(err_is(r.err, 400, "controller sent no args for this llama cell — it is older than this agent "
                             "(needs lama-caravan v1.3.115+)"),
          "без аргументов от контроллера — отказ 400, а не командная строка, собранная заново")
    check(r.popen.calls == [] and disk_cells(s) == {}, "negative: процесс без аргументов контроллера не запускается")
    check(s.cells.startup(port).get("phase") == "loading",
          "as-is: ДЕФЕКТ — отказ вылетает из потока воркера: фаза навсегда «loading», причина до доски не доходит")
    r = worker(worker_scout(), 22107, {"MODEL_FILE": MODEL, "PORT": 22107}, args=[])
    check(isinstance(r.err, AppError) and r.popen.calls == [], "boundary: пустой список аргументов — тот же отказ")

    port = 22108
    s = worker_scout()
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port, "CTX_SIZE": "32k"})
    check(isinstance(r.err, ValueError) and s.cells.startup(port).get("phase") == "loading"
          and r.popen.calls == [],
          "as-is: ДЕФЕКТ — CTX_SIZE не числом роняет поток воркера ValueError: фаза навсегда «loading»")


def test_worker_gpu_layers_and_spec():
    def registered(port, config):
        s = make_scout({"llamaServerBin": str(LLAMA_BIN)})
        worker(s, port, {"MODEL_FILE": str(ABS_MODEL), "PORT": port, **config}, model=str(ABS_MODEL))
        return disk_cells(s).get(str(port), {}).get("cfg", {})

    cases = [
        ("auto", 999, "N_GPU_LAYERS 'auto' → 999 («всё, что влезет»): старт не падает на int('auto')"),
        ("all", 999, "N_GPU_LAYERS 'all' → 999"),
        ("max", 999, "N_GPU_LAYERS 'max' → 999"),
        (" AUTO ", 999, "N_GPU_LAYERS ' AUTO ' → 999: регистр и пробелы не важны"),
        ("lots", 999, "N_GPU_LAYERS 'lots' (мусор) → 999, а не падение старта"),
        ("", 999, "boundary: N_GPU_LAYERS пусто → 999"),
        (None, 999, "boundary: без N_GPU_LAYERS → 999"),
        ("20", 20, "negative: N_GPU_LAYERS '20' строкой — число 20 сохранено"),
        (20, 20, "negative: N_GPU_LAYERS 20 числом — сохранено"),
        ("0", 0, "negative: N_GPU_LAYERS '0' строкой — 0 (только CPU) сохранён"),
    ]
    for i, (raw, want, msg) in enumerate(cases):
        config = {} if raw is None else {"N_GPU_LAYERS": raw}
        check(registered(22110 + i, config).get("gpuLayers") == want, msg)
    check(registered(22121, {"N_GPU_LAYERS": 0}).get("gpuLayers") == 999,
          "as-is: ДЕФЕКТ — N_GPU_LAYERS 0 числом записан как 999 (0 or '' — пусто), а строкой '0' — как 0")
    check(registered(22122, {}).get("ctxSize") == 4096, "без CTX_SIZE — 4096")

    for i, (raw, want, msg) in enumerate([
            ("mtp", "draft-mtp", "SPEC_TYPE 'mtp' → 'draft-mtp': бейдж MTP виден и у встроенного MTP (specPath пуст)"),
            ("MTP", "draft-mtp", "SPEC_TYPE 'MTP' → 'draft-mtp': регистр не важен"),
            ("draft-simple", "draft-simple", "negative: другой SPEC_TYPE ('draft-simple') — как есть"),
            (None, "", "negative: без SPEC_TYPE — пустая строка")]):
        config = {} if raw is None else {"SPEC_TYPE": raw}
        check(registered(22125 + i, config).get("specType") == want, msg)


def test_worker_cancelled_mid_download():
    port = 22105
    s = worker_scout()
    shutil.rmtree(SERVER_CELLS_DIR / str(port), ignore_errors=True)
    original = s.cells.at(port)

    def stop_arrives(_raw):
        # What http.py's /api/llama-node/stop does, in its order.
        s.cells.report(port, phase="idle", error="", downloadedBytes=0, totalBytes=0)
        s.cells.drop(port)
        s.cells.records.forget(port)
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port}, web=models_served(on_request=stop_arrives))
    check(r.err is None and r.popen.calls == [],
          "стоп во время загрузки: процесс не запускается — сервера, которым никто не владеет, нет")
    check(str(port) not in disk_cells(s), "negative: отменённая ячейка не вернулась в реестр")
    check(f"[llama-node] :{port} start cancelled — the cell was stopped mid-download" in r.journal,
          "журнал говорит, что старт отменён")
    ghost = s.cells.by_port.get(port)
    check(ghost is not None and ghost is not original,
          "as-is: ДЕФЕКТ — отменённый старт пересоздал слот порта (report до проверки отмены)")
    size = len(BODIES[MODEL])
    check(s.cells.views() == [{"running": False, "phase": "loading", "modelPath": "", "port": port,
                                      "downloadedBytes": size, "totalBytes": size,
                                      "downloadingFile": "model-q4.gguf", "startedAt": None}],
          "as-is: ДЕФЕКТ — остановленный порт висит на доске в фазе «loading»")
    check((SERVER_CELLS_DIR / str(port) / "start.sh").exists(),
          "as-is: артефакты отменённой ячейки (start.sh, cell.json) всё равно записаны")
    rig = Rig(run=FakeRun({UFW_ALLOW: (0, "")}))
    with rig:
        res, _ = attempt(lambda: s.cells.start({"modelPath": MODEL, "args": llama_args(port),
                                                     "config": {"MODEL_FILE": MODEL, "PORT": port}}))
    check(res == {"ok": False, "error": f"startup already in progress on port {port} (loading)", "phase": "loading"},
          "as-is: ДЕФЕКТ — новый старт на этом порту отказан «уже стартует», пока не придёт ещё один Stop")


def test_worker_stopped_during_start():
    port = 22106
    s = worker_scout()
    popen = FakePopen({"pid": 4343}, on_call=lambda argv, kw: s.cells.drop(port))
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port}, popen=popen)
    check(len(popen.procs) == 1 and popen.procs[0].terminated,
          "стоп пришёл во время старта: свежий процесс завершён, а не брошен сиротой")
    check(str(port) not in disk_cells(s), "negative: ячейка, снятая стопом, не возвращается в реестр")
    check(port not in s.cells.by_port, "стоп во время старта не оставляет призрачного слота")
    check(f"[llama-node] :{port} stopped during startup — terminating the fresh process" in r.journal,
          "журнал объясняет, почему свежий процесс завершён")


def test_worker_corruption_retry():
    port = 22130
    s = worker_scout()
    local = s.models.cache_dir() / MODEL
    seen = []

    def on_request(raw):
        seen.append((s.cells.startup(port), local.exists()))
    popen = FakePopen(OSError(CORRUPT), {"pid": 4545})
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port}, cache=True, popen=popen,
               web=models_served(on_request=on_request))
    check(len(seen) == 2 and len(popen.calls) == 2,
          "битый кэш (corrupted or incomplete): файлы удалены, модель скачана заново, старт повторён один раз")
    check(len(seen) == 2 and seen[1][1] is False and seen[1][0].get("phase") == "downloading"
          and seen[1][0].get("downloadingFile") == "re-downloading…" and seen[1][0].get("downloadedBytes") == 0,
          "перед перекачкой: файл удалён, на доске downloading «re-downloading…» с нуля")
    check(disk_cells(s).get(str(port), {}).get("pid") == 4545 and s.cells.startup(port).get("phase") == "running",
          "второй старт удался — в реестре второй процесс, фаза running")
    check("corruption detected in cached file(s), deleting and retrying" in r.journal
          and f"[llama-node]   deleted: {local}" in r.journal, "журнал называет удалённые файлы")

    port = 22131
    s = worker_scout()
    popen = FakePopen(OSError(CORRUPT), {"pid": 4646})
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port}, cache=False, popen=popen)
    check(len(r.web.calls) == 1 and len(popen.calls) == 1 and (s.models.cache_dir() / MODEL).exists(),
          "negative: без кэша моделей авто-ремонта нет — ни удаления, ни перекачки")
    check(s.cells.startup(port).get("error") == CORRUPT and s.cells.startup(port).get("phase") == "error",
          "negative: без кэша — фаза error с текстом ошибки старта")

    port = 22132
    s = worker_scout()
    popen = FakePopen(OSError("exec format error"), {"pid": 4747})
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port}, cache=True, popen=popen)
    check(len(r.web.calls) == 1 and len(popen.calls) == 1
          and s.cells.startup(port).get("error") == "exec format error",
          "negative: ошибка не про порчу файла — авто-ремонта нет даже с кэшем")


def test_worker_corruption_as_is():
    # The auto-repair trusts the port's log even when this start never reached
    # the process: the log it reads is the PREVIOUS run's.
    port = 22133
    s = worker_scout()
    cache = s.models.cache_dir()
    local = cache / MODEL
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"GOOD cached copy")
    (cache / f"llama-server.{port}.log").write_text(f"0.01.000.000 E {CORRUPT}\n", encoding="utf-8")
    missing_bin = TMP / "missing" / "llama-server"
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port}, cache=True, bin_path=missing_bin)
    check(len(r.web.calls) == 1 and local.read_bytes() == BODIES[MODEL],
          "as-is: ДЕФЕКТ — старт упал на отсутствующем бинаре, а лог ПРОШЛОГО запуска говорит «corrupted»: "
          "хорошая модель удалена и скачана заново")
    check(s.cells.startup(port).get("error") == f"llama-server binary not found: {missing_bin}",
          "итог — фаза error с настоящей причиной (нет бинаря)")

    port = 22134
    s = make_scout({"llamaServerBin": str(LLAMA_BIN)})
    own = TMP / "store-2" / "org" / "model-q4.gguf"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_bytes(b"GGUF user copy")
    r = worker(s, port, {"MODEL_FILE": str(own), "PORT": port}, model=str(own), cache=True,
               popen=FakePopen(OSError(CORRUPT)))
    check(not own.exists(),
          "as-is: ДЕФЕКТ — авто-ремонт удалил модель ВНЕ кэша (абсолютный путь хранилища), хотя чистка кэша "
          "обещает не трогать чужие файлы")
    check(s.cells.startup(port).get("error") == f"model not found locally and controllerUrl not set: {own}",
          "после удаления вернуть её неоткуда — фаза error")


def test_worker_cleanup_when_caching():
    port = 22135
    s = worker_scout(cleanOldModels=False)
    cache = s.models.cache_dir()
    old = cache / "models" / "org" / "old-model.gguf"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"GGUF old")
    r = worker(s, port, {"MODEL_FILE": MODEL, "PORT": port}, cache=True)
    check(r.err is None and not old.exists() and (cache / MODEL).exists(),
          "кэш моделей включён: после удачного старта в кэше остаётся только активная модель")
    check(not old.exists(),
          "as-is: cleanOldModels=false не читается — старые модели удаляются всё равно (докстрока обещает флаг)")

    port = 22136
    s = worker_scout()
    old = s.models.cache_dir() / "models" / "org" / "old-model.gguf"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"GGUF old")
    worker(s, port, {"MODEL_FILE": MODEL, "PORT": port}, cache=False)
    check(old.exists(), "negative: кэш выключен — чистки при старте нет (файлы уйдут при остановке)")

    port = 22137
    s = worker_scout()
    neighbour = s.models.cache_dir() / "models" / "org" / "neighbour.gguf"
    neighbour.parent.mkdir(parents=True, exist_ok=True)
    neighbour.write_bytes(b"GGUF neighbour")
    s.cells.at(22138).process.adopt(5858, {"modelPath": str(neighbour), "port": 22138})
    worker(s, port, {"MODEL_FILE": MODEL, "PORT": port}, cache=True, kill=FakeKill(alive={5858}))
    check(not neighbour.exists(),
          "as-is: ДЕФЕКТ — чистка при старте удалила модель СОСЕДНЕЙ работающей ячейки "
          "(purge_models_safely её бы сохранил)")


TESTS = [
    test_slot_plumbing, test_node_public_views, test_nodes_public_list,
    test_update_status_views, test_update_start_commands, test_update_job_run, test_update_conflict,
    test_builds_list, test_cell_artifacts, test_registry, test_marker_matches, test_pid_cmdline,
    test_port_listener_pid, test_port_health_ok,
    test_adopt_by_marker, test_adopt_by_port, test_adopt_nothing_listening, test_adopt_quiet_health,
    test_adopt_bad_records, test_reap_strays,
    test_node_configs, test_delete_node_config,
    test_llama_start_refusals, test_llama_start_accepted, test_llama_start_port_order, test_llama_start_misc,
    test_llama_start_then_worker,
    test_command_cell_start, test_command_exec_stripping, test_command_cell_refusals, test_command_cell_model,
    test_command_cell_markers, test_command_cell_syncs_assets_first,
    test_resolve_arg_paths,
    test_worker_success, test_worker_download_error, test_worker_log_dir_missing, test_worker_refuses_without_args,
    test_worker_gpu_layers_and_spec, test_worker_cancelled_mid_download, test_worker_stopped_during_start,
    test_worker_corruption_retry, test_worker_corruption_as_is, test_worker_cleanup_when_caching,
]


def main():
    for test in TESTS:
        CHECKS.section(test.__name__)
        try:
            test()
        except BaseException as exc:  # a crashed test is a red pin; the rest must still run
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            check(False, f"{test.__name__}: упал с исключением {type(exc).__name__}: {exc}")
    return CHECKS.finish()


if __name__ == "__main__":
    sys.exit(main())
