#!/usr/bin/env python3
"""Snapshot of one managed cell process: CellLog and CellProcess (caravan_scout/process.py), and the Cell
that holds one (caravan_scout/cells.py).

How a crash reason is read out of a cell's log — level-aware for llama.cpp,
plain for command cells — how the previous run's log is kept aside, and how a
process is started, adopted after a scout restart, stopped and reported.

Pinned by value before the scout is rewritten into classes. Nothing real is
started or signalled: Popen, os.kill, the clock, sleep and strftime are fakes
and every log is a file in a temp dir, so stop()'s 10 s SIGTERM wait costs
nothing.

The tests are plain functions, one scenario each, run once in order — the
shape of the other snapshots here. What holds state is an object: the Checks
ledger and the fakes.

Run: python3 scripts/test_scout_node.py
"""
import calendar
import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import BLOCKED, TMP, Checks, RealCallBlocked, patched  # noqa: E402

from caravan_scout.cells import Cell  # noqa: E402
from caravan_scout.process import CellLog, CellProcess  # noqa: E402

CHECKS = Checks("scout node")
check = CHECKS.check

T0 = 1_790_000_000.0
REAL_STRFTIME = time.strftime
STAMP_AT = calendar.timegm((2026, 9, 24, 12, 0, 0))  # the second every rotation in these pins happens in
STAMP = "20260924-120000"


# ── helpers ──────────────────────────────────────────────────────────────────

def same(actual, expected, msg):
    """check(actual == expected) that prints both values when it fails."""
    ok = actual == expected
    check(ok, msg)
    if not ok:
        print(f"        got:  {actual!r}\n        want: {expected!r}")


class Raised:
    """What a call raised, standing in for its value: a crash under a mutant
    then fails the pin that made the call instead of ending the file."""

    def __init__(self, exc):
        self.exc = exc

    def __repr__(self):
        return f"Raised({self.exc!r})"


def outcome(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — a crash is an answer to pin, not the end of the file
        return Raised(exc)


def dig(value, *path):
    """value[p0][p1]…, or None when that shape is not there."""
    for key in path:
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError):
            return None
    return value


class FakeClock:
    """time.time and time.sleep for a pin: time moves only when the code sleeps
    or the pin moves it."""

    def __init__(self, now=T0):
        self.now = now
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def fixed_strftime(fmt, t=None):
    """time.strftime frozen at STAMP_AT (UTC), so a backup's name is known in advance."""
    return REAL_STRFTIME(fmt, time.gmtime(STAMP_AT) if t is None else t)


class FakeProc:
    """Our own child process: running until the pin, terminate() or kill() ends it.
    `stubborn` ignores SIGTERM, so wait(10) times out and kill() is needed."""

    def __init__(self, pid=5151, stubborn=False, terminate_error=None):
        self.pid = pid
        self.rc = None
        self.stubborn = stubborn
        self.terminate_error = terminate_error
        self.calls = []

    def poll(self):
        return self.rc

    def terminate(self):
        self.calls.append("terminate")
        if self.terminate_error:
            raise self.terminate_error
        if not self.stubborn:
            self.rc = -signal.SIGTERM

    def kill(self):
        self.calls.append("kill")
        self.rc = -signal.SIGKILL

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if self.rc is None:
            raise subprocess.TimeoutExpired("llama-server", timeout)
        return self.rc


class FakePopen:
    """subprocess.Popen that hands out one FakeProc (or raises) and keeps every call."""

    def __init__(self, proc=None, error=None):
        self.proc = proc if proc is not None else FakeProc()
        self.error = error
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if self.error:
            raise self.error
        return self.proc

    def close_logs(self):
        """Close the log files start() opened for the child (the parent keeps no handle)."""
        for _, kwargs in self.calls:
            if hasattr(kwargs.get("stdout"), "close"):
                kwargs["stdout"].close()


class FakePid:
    """os.kill for one process that is not our child. Signal 0 answers whether
    it lives; SIGTERM ends it `term_takes` seconds later on the fake clock
    (None: it ignores SIGTERM); SIGKILL ends it at once. Every call is kept."""

    def __init__(self, clock, pid=4242, alive=True, term_takes=0.0, probe_error=None, signal_error=None):
        self.clock, self.pid = clock, pid
        self.dies_at = None if alive else float("-inf")
        self.term_takes = term_takes
        self.probe_error, self.signal_error = probe_error, signal_error
        self.calls = []

    @property
    def probes(self):
        return sum(1 for _, sig in self.calls if sig == 0)

    @property
    def sent(self):
        return [sig for _, sig in self.calls if sig != 0]

    def _end_at(self, when):
        self.dies_at = when if self.dies_at is None else min(self.dies_at, when)

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))
        if pid != self.pid:
            raise ProcessLookupError(3, "No such process")
        if sig == 0:
            if self.probe_error:
                raise self.probe_error
            if self.dies_at is not None and self.clock.now >= self.dies_at:
                raise ProcessLookupError(3, "No such process")
            return
        if self.signal_error:
            raise self.signal_error
        if sig == signal.SIGKILL:
            self._end_at(self.clock.now)
        elif sig == signal.SIGTERM and self.term_takes is not None:
            self._end_at(self.clock.now + self.term_takes)


@contextlib.contextmanager
def faked(clock, popen=None, kill=None):
    """The node's doors for one pin: the clock always; Popen and os.kill only
    when given — otherwise the harness keeps them shut."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(time, time=clock.time, sleep=clock.sleep, strftime=fixed_strftime))
        if popen is not None:
            stack.enter_context(patched(subprocess, Popen=popen))
        if kill is not None:
            stack.enter_context(patched(os, kill=kill))
        yield


@contextlib.contextmanager
def env(**values):
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def fresh_dir():
    return Path(tempfile.mkdtemp(prefix="node-", dir=TMP))


class Workspace:
    """A fresh dir with an empty llama-server binary and model file — start()
    only checks that they exist; the fake Popen runs nothing — and a logs/
    dir of its own."""

    def __init__(self):
        self.dir = fresh_dir()
        self.bin = self.dir / "llama-server"
        self.model = self.dir / "model.gguf"
        self.logs = self.dir / "logs"
        self.log = self.logs / "llama-server.22001.log"
        self.bin.write_text("")
        self.model.write_text("")
        self.logs.mkdir()


def log_with(*lines, name="llama-server.22001.log"):
    """A cell log holding exactly these lines, in a directory of its own."""
    path = fresh_dir() / name
    path.write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")
    return path


def lv(level, text, stamp="0.00.100.000"):
    """One llama.cpp log line: '<time> <LEVEL> <subsystem: text>'."""
    return f"{stamp} {level} {text}"


def read(path):
    return outcome(lambda: CellLog(path).crash_reason())


def rotate(path, **kwargs):
    with patched(time, strftime=fixed_strftime):
        return outcome(lambda: CellLog(path).rotate(**kwargs))


def names(directory):
    return sorted(p.name for p in directory.iterdir())


def texts(directory):
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(directory.iterdir())}


def aged(path, mtime, text="old run\n"):
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


# ── CellLog.crash_reason ─────────────────────────────────────────────────────

GENERIC_LAST = "main: exiting due to model loading error"
CALM_LAST = "cleanup complete"
# One priority pattern per line, and none of the others, so each pin guards one entry.
PRIORITY_CASES = [
    ("not within the file bounds",
     "llama_model_load: tensor 'blk.0.ffn_down.weight' data is not within the file bounds"),
    ("corrupted or incomplete", "gguf_init_from_file_impl: model is corrupted or incomplete"),
    ("unexpected end of file", "gguf_init_from_file: unexpected end of file while reading tensor info"),
    ("out of memory", "ggml_backend_cuda_buffer_type_alloc_buffer: allocating 20480.00 MiB on device 0: "
                      "cudaMalloc failed: out of memory"),
    ("failed to allocate", "ggml_gallocr_reserve_n: failed to allocate CUDA0 buffer of size 8589934592"),
    ("not enough memory", "ggml_metal_init: not enough memory for the compute buffer"),
    ("mismatch between text model", "clip_init: mismatch between text model (n_embd = 4096) and mmproj (n_embd = 3584)"),
    ("wrong mmproj", "hint: this is probably a wrong mmproj for this model"),
    ("mtmd_init_from_file", "mtmd_init_from_file: cannot load the projector"),
    ("no such file", "main: cannot stat /models/model.gguf: No such file or directory"),
    ("failed to open", "llama_model_load_from_file_impl: failed to open /models/model.gguf"),
]
MARKER_CASES = [
    ("error", "whisper: error reading audio chunk 3"),
    ("abort", "Aborted (core dumped)"),
    ("failed", "bind: failed on 0.0.0.0:22005, port in use"),
    ("invalid argument", "llama-server: invalid argument: --flash-attn maybe"),
    ("what()", "caught exception, what() = std::bad_alloc"),
]
CATASTROPHE_CASES = [
    ("terminate called", "terminate called without an active exception"),
    ("traceback", "Traceback (most recent call last):"),
    ("segmentation fault", "Segmentation fault (core dumped)"),
    ("killed", "Killed"),
]
W_CATASTROPHE_CASES = [
    ("cuda error", lv("W", "ggml_cuda_compute_forward: CUDA error 700 at ggml-cuda.cu:88", "0.00.150.000")),
    ("what():", lv("W", "  what():  std::bad_alloc", "0.00.150.000")),
]


def test_read_log_error_absent():
    CHECKS.section("причина из лога — лога нет:")
    same(read(None), "", "лога нет (None) — пусто")
    same(read(fresh_dir() / "never-written.log"), "", "negative: файла нет — пусто, без исключения")
    same(read(fresh_dir()), "", "boundary: путь — каталог — пусто")
    same(read(log_with()), "", "boundary: пустой лог — пусто")
    same(read(log_with("", "   ", "\t")), "", "boundary: одни пустые строки — пусто")


def test_read_log_error_priority():
    CHECKS.section("причина из лога — приоритетные шаблоны:")
    for pattern, line in PRIORITY_CASES:
        same(read(log_with(line, GENERIC_LAST)), line, f"«{pattern}» важнее более поздней общей ошибки")
    cuda = "ggml_cuda_host_malloc: cudaErrorMemoryAllocation at ggml-cuda.cu:512"
    same(read(log_with(cuda, GENERIC_LAST)), GENERIC_LAST,
         "as-is: настоящее cudaErrorMemoryAllocation не приоритетно — в списке опечатка «cudaerroromemoryallocation»")
    older, newer = PRIORITY_CASES[-1][1], PRIORITY_CASES[3][1]
    same(read(log_with(older, "srv: retrying with --no-mmap", newer, GENERIC_LAST)), newer,
         "из двух приоритетных строк — более поздняя")
    # Not level-filtered on purpose: the corrupted-download auto-repair reads this very value.
    corrupt_w = lv("W", "gguf_init_from_file_impl: model is corrupted or incomplete", "0.00.300.000")
    same(read(log_with(lv("I", "main: loading model"), corrupt_w, lv("I", "srv: shutting down", "0.00.400.000"))),
         corrupt_w, "приоритет не смотрит на уровень: W с «corrupted or incomplete» — причина (её ждёт авто-ремонт)")


def test_read_log_error_markers():
    CHECKS.section("причина из лога — строки-ошибки:")
    for marker, line in MARKER_CASES:
        same(read(log_with(line, CALM_LAST)), line, f"строка с «{marker}» важнее последней спокойной строки")
    same(read(log_with("step 1 failed", "step 2 failed", CALM_LAST)), "step 2 failed",
         "из двух строк-ошибок — более поздняя")
    build = "build: 6543 (0a1b2c3) with GNU 13.3.0 -Werror for x86_64-linux-gnu"
    same(read(log_with(build, "main: loading model")), "main: loading model",
         "строка «build:» — не причина, даже с «error» во флагах сборки")
    flags = "flags: 6543 (0a1b2c3) with GNU 13.3.0 -Werror for x86_64-linux-gnu"
    same(read(log_with(flags, "main: loading model")), flags, "negative: та же строка без «build:» — причина")


def test_read_log_error_levels():
    CHECKS.section("причина из лога — уровни llama.cpp:")
    # The real defect: this benign W line was the last one and reached the card as "Model loading failed".
    warn = lv("W", "load: control-looking token: 128247 '</s>' was not control-type; this is probably a bug "
                   "in the model. its type will be overridden", "0.00.817.809")
    same(read(log_with(lv("I", "main: loading model", "0.00.817.700"), warn)), "",
         "лог llama.cpp только из I и W — пусто: безобидная последняя строка не выдаётся за причину")
    same(read(log_with("Loading checkpoint shards", "Listening on 0.0.0.0:22005")), "Listening on 0.0.0.0:22005",
         "negative: лог без уровней (command-ячейка) — последняя строка, как раньше")
    e_line = lv("E", "llama_model_load: error loading model architecture: unknown model architecture: 'foo'",
                "0.00.200.000")
    same(read(log_with(lv("I", "main: loading model"), e_line,
                       lv("I", "srv: load_model: failed, cleaning up", "0.00.300.000"),
                       lv("W", "common_init: failed to warm up, continuing", "0.00.400.000"))),
         e_line, "строки I и W с «failed» пропускаются — причина в строке E")
    bare = "llama-server: invalid argument: --bogus"
    same(read(log_with(lv("I", "main: loading model"), bare, lv("I", "srv: shutting down", "0.00.200.000"))),
         bare, "negative: строка без уровня в логе с уровнями (stderr рантайма) не пропускается")
    d_line = lv("D", "sched: failed to split graph", "0.00.200.000")
    same(read(log_with(lv("E", "main: error in step 1"), d_line)), d_line,
         "as-is: пропускаются только I и W — строка D с «failed» становится причиной")
    same(read(log_with(lv("I", "main: loading model"), "srv: listening on 0.0.0.0:22001")), "",
         "boundary: одна строка с уровнем делает весь лог «с уровнями» — последняя строка без уровня не причина")


def test_read_log_error_catastrophes():
    CHECKS.section("причина из лога — катастрофы:")
    for name, line in CATASTROPHE_CASES:
        same(read(log_with(lv("I", "main: loading model"), line, lv("I", "srv: shutting down", "0.00.200.000"))),
             line, f"«{name}» без уровня в логе llama.cpp — причина, хоть это и не строка-ошибка")
    for name, line in W_CATASTROPHE_CASES:
        same(read(log_with(lv("I", "main: loading model"), line, lv("I", "srv: shutting down", "0.00.200.000"))),
             line, f"as-is: катастрофы не смотрят на уровень — W с «{name}» становится причиной")
    same(read(log_with("loading model weights", "Segmentation fault (core dumped)", "cleanup done")),
         "Segmentation fault (core dumped)", "лог без уровней: катастрофа важнее последней строки")


def test_read_log_error_window():
    CHECKS.section("причина из лога — окно, обрезка, кодировка:")
    oom = "error: out of memory"
    same(read(log_with(oom, *[f"step {i}" for i in range(79)])), oom,
         "boundary: приоритетная строка 80-й с конца — видна")
    same(read(log_with(oom, *[f"step {i}" for i in range(80)])), "step 79",
         "boundary: 81-й с конца — за окном, остаётся последняя строка")
    padded = [x for i in range(79) for x in (f"step {i}", "", "   ")]
    same(read(log_with(oom, *padded)), oom, "пустые строки не занимают окно в 80 строк")
    same(read(log_with("warming up", "listening   ", "", "   ")), "listening",
         "последняя строка — последняя непустая, без хвостовых пробелов")
    tail = "x" * 400
    for what, lines, line in (("приоритетная строка", ["error: out of memory " + tail], "error: out of memory " + tail),
                              ("строка-ошибка", ["step failed " + tail, CALM_LAST], "step failed " + tail),
                              ("катастрофа", [lv("I", "main: loading model"), "Killed " + tail], "Killed " + tail),
                              ("последняя строка", ["listening " + tail], "listening " + tail)):
        same(read(log_with(*lines)), line[:300], f"boundary: {what} обрезается до 300 символов")
    raw = fresh_dir() / "llama-server.22001.log"
    raw.write_bytes(b"\xff\xfe error: bad header\n")
    same(read(raw), "�� error: bad header", "boundary: битые байты не роняют чтение — заменяются")


# ── CellLog.rotate ──────────────────────────────────────────────────────────

def test_rotate_log():
    CHECKS.section("лог прошлого запуска откладывается, а не затирается:")
    same(rotate(None), None, "лога нет (None) — ничего не делает")
    d = fresh_dir()
    same((rotate(d / "llama-server.22001.log"), names(d)), (None, []), "negative: файла нет — копий не появляется")
    d = fresh_dir()
    (d / "llama-server.22001.log").write_text("")
    rotate(d / "llama-server.22001.log")
    same(names(d), ["llama-server.22001.log"], "negative: пустой лог не откладывается")

    d = fresh_dir()
    (d / "llama-server.22001.log").write_text("error: out of memory\n")
    rotate(d / "llama-server.22001.log")
    same(texts(d), {f"llama-server.22001.{STAMP}.log": "error: out of memory\n"},
         "непустой лог уезжает целиком в llama-server.22001.<ГГГГммдд-ЧЧММСС>.log")

    d = fresh_dir()
    (d / f"llama-server.22001.{STAMP}.log").write_text("first\n")
    for text in ("second\n", "third\n"):
        (d / "llama-server.22001.log").write_text(text)
        rotate(d / "llama-server.22001.log")
    same(texts(d), {f"llama-server.22001.{STAMP}.log": "first\n",
                    f"llama-server.22001.{STAMP}-1.log": "second\n",
                    f"llama-server.22001.{STAMP}-2.log": "third\n"},
         "несколько стартов в одну секунду: -1, -2 — ни одна копия не затёрта")

    d = fresh_dir()
    old = [aged(d / f"llama-server.22001.20260901-0000{n:02d}.log", 1000 + n) for n in range(16)]
    aged(d / "llama-server.22001.log", 2000, "latest crash\n")
    rotate(d / "llama-server.22001.log")
    same(names(d), sorted([f"llama-server.22001.{STAMP}.log"] + [p.name for p in old[2:]]),
         "хранятся 15 самых свежих копий по времени записи; две старейшие удалены")

    d = fresh_dir()
    old = [aged(d / f"llama-server.22001.20260901-00000{n}.log", 1000 + n) for n in range(3)]
    aged(d / "llama-server.22001.log", 2000, "latest crash\n")
    rotate(d / "llama-server.22001.log", keep=2)
    same(names(d), sorted([f"llama-server.22001.{STAMP}.log", old[2].name]), "keep=2 — две самые свежие")

    d = fresh_dir()
    neighbours = [aged(d / "llama-server.22002.log", 100, "live neighbour\n"),
                  aged(d / "llama-server.22002.20260901-000000.log", 90),
                  aged(d / "command-cell.22005.20260901-000000.log", 80)]
    old = [aged(d / f"llama-server.22001.20260901-00000{n}.log", 1000 + n) for n in range(3)]
    rotate(d / "llama-server.22001.log", keep=1)
    same(names(d), sorted([p.name for p in neighbours] + [old[2].name]),
         "negative: логи соседних ячеек не трогаются; свои копии подрезаются и без живого лога")

    d = fresh_dir()
    aged(d / "llama-server.22002.log", 1000, "live cell on 22002\n")
    aged(d / "llama-server.22003.log", 2000, "live cell on 22003\n")
    aged(d / "llama-server.log", 3000, "legacy single log\n")
    rotate(d / "llama-server.log", keep=1)
    same(names(d), [f"llama-server.{STAMP}.log"],
         "as-is: у старого имени llama-server.log глоб llama-server.*.log цепляет живые логи ячеек и удаляет их")

    d = fresh_dir()
    (d / "llama-server.22001.log").write_text("keep me\n")

    def refuse_rename(self, target):
        raise PermissionError(13, "Permission denied")
    with patched(Path, rename=refuse_rename):
        got = rotate(d / "llama-server.22001.log")
    same((got, texts(d)), (None, {"llama-server.22001.log": "keep me\n"}),
         "сбой переименования проглатывается — старт не блокируется, лог на месте")

    d = fresh_dir()
    old = [aged(d / f"llama-server.22001.20260901-00000{n}.log", 1000 + n) for n in range(4)]
    real_unlink = Path.unlink

    def stuck_unlink(self, missing_ok=False):
        if self.name == old[2].name:
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, missing_ok=missing_ok)
    with patched(Path, unlink=stuck_unlink):
        rotate(d / "llama-server.22001.log", keep=1)
    same(names(d), sorted([old[3].name, old[2].name]), "одна копия не удалилась — остальные всё равно удаляются")


# ── CellProcess.pid_alive ────────────────────────────────────────────────────

def test_pid_alive():
    CHECKS.section("жив ли процесс по pid:")
    for error, expected, msg in ((None, True, "kill(pid, 0) прошёл — жив"),
                                 (ProcessLookupError(3, "No such process"), False,
                                  "negative: ProcessLookupError — мёртв"),
                                 (PermissionError(1, "Operation not permitted"), True,
                                  "PermissionError — жив, просто чужой"),
                                 (OSError(22, "Invalid argument"), False, "negative: любая другая ошибка — мёртв")):
        calls = []

        def kill(pid, sig, error=error):
            calls.append((pid, sig))
            if error:
                raise error
        with patched(os, kill=kill):
            got = outcome(CellProcess.pid_alive, "4242")
        same(got, expected, msg)
        if error is None:
            same(calls, [(4242, 0)], "проверка — сигнал 0 по числовому pid (строка «4242» приводится к числу)")
    calls = []
    with patched(os, kill=lambda pid, sig: calls.append((pid, sig))):
        got = outcome(CellProcess.pid_alive, "not-a-pid")
    same((got, calls), (False, []), "boundary: pid не число — мёртв, kill не зовётся")


# ── adopt ────────────────────────────────────────────────────────────────────

def test_adopt():
    CHECKS.section("усыновление ячейки, пережившей рестарт скаута:")
    clock = FakeClock()
    pid = FakePid(clock)
    node = CellProcess()
    cfg = {"port": 22001, "modelPath": "/models/model.gguf", "cmd": ["llama-server", "-m", "x"]}
    with faked(clock, kill=pid):
        res = outcome(node.adopt, "4242", cfg, started_at=int(T0) - 1000)
        cfg["port"] = 22999  # the caller's dict changes later; the node keeps its own copy
        clock.now = T0 + 0.9
        st = outcome(node.status)
    same(res, {"ok": True, "pid": 4242, "adopted": True}, "ok, pid числом, adopted")
    same(st, {"running": True, "pid": 4242, "adopted": True, "startedAt": int(T0) - 1000, "uptimeSec": 1000,
              "port": 22001, "modelPath": "/models/model.gguf"},
         "живой: running, adopted, uptime от переданного startedAt, cfg — своя копия и без cmd")
    same(pid.calls, [(4242, 0)], "жизнь проверяется сигналом 0 — больше ничего не посылается")

    clock = FakeClock(T0 + 0.7)
    node = CellProcess()
    with faked(clock, kill=FakePid(clock)):
        outcome(node.adopt, 4242, None)
        st = outcome(node.status)
    same(st, {"running": True, "pid": 4242, "adopted": True, "startedAt": int(T0), "uptimeSec": 0},
         "boundary: startedAt 0 — берётся «сейчас»; cfg None — пустой")

    ws, clock, proc = Workspace(), FakeClock(), FakeProc(pid=5151)
    node = CellProcess()
    with faked(clock, popen=FakePopen(proc), kill=FakePid(clock)):
        outcome(node.start, str(ws.bin), [], {"port": 22001})
        proc.rc = 1
        crashed = outcome(node.status)
        outcome(node.adopt, 4242, {"port": 22001}, started_at=int(T0))
        st = outcome(node.status)
    same((dig(crashed, "crashed"), st),
         (True, {"running": True, "pid": 4242, "adopted": True, "startedAt": int(T0), "uptimeSec": 0, "port": 22001}),
         "усыновление стирает прошлый крэш слота")

    # While the adopted process lives, status() never reads the old crash; it shows only on the
    # path where nothing overwrites it: the adopted process dies unseen and a restart fails first.
    ws, clock, proc = Workspace(), FakeClock(), FakeProc(pid=5151)
    node, pid = CellProcess(), FakePid(clock)
    with faked(clock, popen=FakePopen(proc), kill=pid):
        outcome(node.start, str(ws.bin), [], {"port": 22001})
        proc.rc = 1
        outcome(node.status)
        outcome(node.adopt, 4242, {"port": 22001}, started_at=int(T0))
        pid.dies_at = clock.now  # dies before anyone asks
        failed = outcome(node.start, str(ws.dir / "gone"), [], {"port": 22001})
        st = outcome(node.status)
    same((dig(failed, "ok"), st), (False, {"running": False}),
         "прошлый крэш не всплывает и после тихой смерти усыновлённого (as-is: сама смерть при этом теряется — "
         "неудачный старт стёр pid раньше, чем её заметили)")


def test_adopted_death():
    CHECKS.section("усыновлённый умер:")
    clock = FakeClock()
    pid = FakePid(clock, alive=False)
    reason = lv("E", "ggml_cuda_init: failed to initialize CUDA: out of memory", "0.00.200.000")
    log = log_with(lv("I", "main: loading model"), reason, lv("I", "srv: shutting down", "0.00.300.000"))
    node = CellProcess()
    with faked(clock, kill=pid):
        outcome(node.adopt, 4242, {"port": 22001}, log_path=log, started_at=int(T0))
        first = outcome(node.status)
        probes = pid.probes
        second = outcome(node.status)
    crash = {"running": False, "exitCode": None, "lastError": reason, "crashed": True}
    same(first, crash, "running false, кода выхода нет (не наш ребёнок), причина из лога, crashed")
    same(second, crash, "крэш липкий: следующий опрос показывает то же")
    same((probes, pid.probes), (1, 1), "после смерти pid забыт — чужой процесс с тем же pid не оживит ячейку")

    clock = FakeClock()
    node = CellProcess()
    with faked(clock, kill=FakePid(clock, alive=False)):
        outcome(node.adopt, 4242, {"port": 22001}, started_at=int(T0))
        st = outcome(node.status)
    same(st, {"running": False, "exitCode": None, "lastError": "", "crashed": True},
         "negative: без лога причина пустая, но смерть — всё равно крэш")


# ── start / start_command ────────────────────────────────────────────────────

def test_start():
    CHECKS.section("старт llama-server:")
    ws, clock, popen = Workspace(), FakeClock(), FakePopen(FakeProc(pid=5151))
    ws.log.write_text("previous run: error: out of memory\n")
    cfg = {"modelPath": str(ws.model), "port": 22001, "gpuLayers": 999, "ctxSize": 8192}
    node = CellProcess()
    with faked(clock, popen=popen):
        res = outcome(node.start, str(ws.bin), ["--model", str(ws.model), "--port", 22001, "-c", 8192], cfg,
                      log_path=ws.log)
        clock.now = T0 + 42.5
        st = outcome(node.status)
    popen.close_logs()
    same(res, {"ok": True, "pid": 5151, "port": 22001}, "ok, pid процесса, порт из cfg")
    same(dig(popen.calls, 0, 0), [str(ws.bin), "--model", str(ws.model), "--port", "22001", "-c", "8192"],
         "команда — бинарь и аргументы, каждый строкой")
    out = dig(popen.calls, 0, 1) or {}
    same((getattr(out.get("stdout"), "name", None), getattr(out.get("stdout"), "mode", None),
          out.get("stderr"), out.get("close_fds")), (str(ws.log), "w", subprocess.STDOUT, True),
         "вывод — в лог ячейки, открытый заново (w); stderr туда же; close_fds")
    same(st, {"running": True, "pid": 5151, "startedAt": int(T0), "uptimeSec": 42, **cfg},
         "status: running, pid, startedAt, uptime — и cfg без cmd")
    same(texts(ws.logs), {f"llama-server.22001.{STAMP}.log": "previous run: error: out of memory\n",
                          "llama-server.22001.log": ""},
         "лог упавшего запуска отложен в копию до открытия нового (было: затирался при авто-рестарте)")

    ws, popen = Workspace(), FakePopen(FakeProc(pid=5152))
    with faked(FakeClock(), popen=popen):
        res = outcome(CellProcess().start, str(ws.bin), [], {"port": 22002})
    out = dig(popen.calls, 0, 1) or {}
    same((res, out.get("stdout"), out.get("stderr")),
         ({"ok": True, "pid": 5152, "port": 22002}, subprocess.DEVNULL, subprocess.DEVNULL),
         "без лога — вывод в /dev/null")

    ws, popen = Workspace(), FakePopen()
    with faked(FakeClock(), popen=popen):
        res = outcome(CellProcess().start, str(ws.dir / "no-such-binary"), [], {"port": 22001})
    same((res, popen.calls),
         ({"ok": False, "error": f"llama-server binary not found: {ws.dir / 'no-such-binary'}"}, []),
         "negative: бинаря нет — отказ с путём, Popen не зовётся")

    ws, popen = Workspace(), FakePopen(FakeProc(pid=5153))
    with env(HOME=str(ws.dir)), faked(FakeClock(), popen=popen):
        res = outcome(CellProcess().start, "~/llama-server", [], {"port": 22001})
        missing = outcome(CellProcess().start, "~/nope", [], {"port": 22001})
    same((dig(res, "ok"), dig(popen.calls, 0, 0, 0)), (True, str(ws.bin)), "«~» в пути бинаря раскрывается")
    same(missing, {"ok": False, "error": f"llama-server binary not found: {ws.dir / 'nope'}"},
         "negative: и в отказе — раскрытый путь")

    ws, popen = Workspace(), FakePopen(FakeProc(pid=5154))
    with faked(FakeClock(), popen=popen):
        res = outcome(CellProcess().start, str(ws.bin), [], {"modelPath": str(ws.dir / "missing.gguf"), "port": 22001})
        blank = outcome(CellProcess().start, str(ws.bin), [], {"modelPath": "", "port": 22001})
    same(res, {"ok": False, "error": f"model file not found: {ws.dir / 'missing.gguf'}"},
         "negative: файла модели нет — отказ с путём")
    same((blank, len(popen.calls)), ({"ok": True, "pid": 5154, "port": 22001}, 1),
         "boundary: пустой modelPath не проверяется — старт идёт")

    ws, popen = Workspace(), FakePopen(FakeProc(pid=5151))
    node = CellProcess()
    with faked(FakeClock(), popen=popen):
        outcome(node.start, str(ws.bin), [], {"port": 22001})
        again = outcome(node.start, str(ws.bin), [], {"port": 22002})
        cmd_again = outcome(node.start_command, "exec true", {"port": 22003})
    same(again, {"ok": False, "error": "llama-server is already running", "port": 22001},
         "второй старт при живом процессе — отказ с портом идущего")
    same(cmd_again, {"ok": False, "error": "a process is already running", "port": 22001},
         "то же для command-старта — своим текстом")
    same(len(popen.calls), 1, "negative: второй процесс не запускается")

    clock = FakeClock()
    ws, popen = Workspace(), FakePopen()
    node = CellProcess()
    with faked(clock, popen=popen, kill=FakePid(clock)):
        outcome(node.adopt, 4242, {"port": 22001}, started_at=int(T0))
        res = outcome(node.start, str(ws.bin), [], {"port": 22009})
    same((res, popen.calls), ({"ok": False, "error": "llama-server is already running", "port": 22001}, []),
         "живой усыновлённый тоже держит слот")

    ws, clock = Workspace(), FakeClock()
    first, second = FakeProc(pid=5151), FakeProc(pid=6161)
    node = CellProcess()
    with faked(clock, popen=FakePopen(first)):
        outcome(node.start, str(ws.bin), [], {"port": 22001})
    first.rc = 1
    with faked(clock, popen=FakePopen(second)):
        res = outcome(node.start, str(ws.bin), [], {"port": 22001})
        st = outcome(node.status)
    same(res, {"ok": True, "pid": 6161, "port": 22001}, "упавший процесс слот не держит — перезапуск идёт")
    same(st, {"running": True, "pid": 6161, "startedAt": int(T0), "uptimeSec": 0, "port": 22001},
         "после перезапуска — новый процесс, без следов прошлого")

    ws, popen = Workspace(), FakePopen(error=OSError(8, "Exec format error"))
    node = CellProcess()
    with faked(FakeClock(), popen=popen):
        res = outcome(node.start, str(ws.bin), [], {"port": 22001})
        st = outcome(node.status)
    same(res, {"ok": False, "error": "[Errno 8] Exec format error"}, "Popen упал — отказ с текстом ошибки")
    same(st, {"running": False, "lastError": "[Errno 8] Exec format error"}, "status помнит ошибку запуска")

    ws, clock, proc = Workspace(), FakeClock(), FakeProc(pid=5151)
    node, popen = CellProcess(), FakePopen(proc)
    with faked(clock, popen=popen):
        outcome(node.start, str(ws.bin), [], {"port": 22001}, log_path=ws.log)
    popen.close_logs()
    ws.log.write_text("error: out of memory\n")  # what the dying server wrote
    proc.rc = 1
    with faked(clock, popen=FakePopen(error=OSError(8, "Exec format error"))):
        crashed = outcome(node.status)
        outcome(node.start, str(ws.bin), [], {"port": 22001}, log_path=ws.log)
        st = outcome(node.status)
    same(st, {"running": False, "exitCode": 1, "lastError": "error: out of memory", "crashed": True},
         "as-is: после крэша новый провал запуска прячется за старым — status показывает прошлую причину")
    same(crashed, st, "as-is: status после неудачного старта слово в слово тот же, что до него")

    ws, popen = Workspace(), FakePopen()
    lost = ws.dir / "no-such-dir" / "llama-server.22001.log"
    with faked(FakeClock(), popen=popen):
        res = outcome(CellProcess().start, str(ws.bin), [], {"port": 22001}, log_path=lost)
    same((res, popen.calls), ({"ok": False, "error": f"[Errno 2] No such file or directory: '{lost}'"}, []),
         "boundary: каталога лога нет — отказ ошибкой open, Popen не зовётся")


def test_start_command():
    CHECKS.section("старт command-ячейки:")
    ws, clock, popen = Workspace(), FakeClock(), FakePopen(FakeProc(pid=7171))
    log = ws.logs / "command-cell.22005.log"
    log.write_text("previous: Traceback (most recent call last):\n")
    line = "set -euo pipefail; export PORT=22005; exec python3 server.py --port $PORT"
    cfg = {"modelPath": str(ws.dir / "not-downloaded.gguf"), "port": 22005, "cellKind": "command",
           "command": "python3 server.py --port $PORT"}
    node = CellProcess()
    with faked(clock, popen=popen):
        res = outcome(node.start_command, line, cfg, log_path=log)
        clock.now = T0 + 3
        st = outcome(node.status)
    popen.close_logs()
    same(res, {"ok": True, "pid": 7171, "port": 22005}, "ok, pid, порт")
    same(dig(popen.calls, 0, 0), ["bash", "-lc", line],
         "команда — bash -lc со строкой целиком (login-shell: окружение пользователя)")
    out = dig(popen.calls, 0, 1) or {}
    same((getattr(out.get("stdout"), "name", None), out.get("stderr"), out.get("close_fds")),
         (str(log), subprocess.STDOUT, True), "вывод и stderr — в лог ячейки; close_fds")
    same(st, {"running": True, "pid": 7171, "startedAt": int(T0), "uptimeSec": 3, **cfg},
         "status как у llama-ячейки: cfg без cmd; путь модели не проверяется")
    same(texts(ws.logs), {f"command-cell.22005.{STAMP}.log": "previous: Traceback (most recent call last):\n",
                          "command-cell.22005.log": ""},
         "лог прошлого запуска отложен и здесь")

    popen = FakePopen(FakeProc(pid=7172))
    with faked(FakeClock(), popen=popen):
        res = outcome(CellProcess().start_command, "exec true", {"port": 22006})
    out = dig(popen.calls, 0, 1) or {}
    same((res, out.get("stdout"), out.get("stderr")),
         ({"ok": True, "pid": 7172, "port": 22006}, subprocess.DEVNULL, subprocess.DEVNULL),
         "без лога — вывод в /dev/null")

    node = CellProcess()
    with faked(FakeClock(), popen=FakePopen(error=FileNotFoundError(2, "No such file or directory", "bash"))):
        res = outcome(node.start_command, "exec true", {"port": 22006})
        st = outcome(node.status)
    same((res, st), ({"ok": False, "error": "[Errno 2] No such file or directory: 'bash'"},
                     {"running": False, "lastError": "[Errno 2] No such file or directory: 'bash'"}),
         "negative: Popen упал — отказ, и status помнит ошибку")


# ── stop ─────────────────────────────────────────────────────────────────────

def started(proc):
    """A node that started `proc` (our own child) and forgot the workspace."""
    ws, node = Workspace(), CellProcess()
    with faked(FakeClock(), popen=FakePopen(proc)):
        outcome(node.start, str(ws.bin), [], {"port": 22001})
    return node


def test_stop_own():
    CHECKS.section("стоп своего процесса:")
    proc = FakeProc()
    node = started(proc)
    with faked(FakeClock()):
        res, st = outcome(node.stop), outcome(node.status)
    same((res, proc.calls), ({"ok": True}, ["terminate", ("wait", 10)]), "SIGTERM и ожидание до 10 с — ушёл сам")
    same(st, {"running": False}, "после стопа — просто не запущен")

    proc = FakeProc(stubborn=True)
    node = started(proc)
    same((outcome(node.stop), proc.calls), ({"ok": True}, ["terminate", ("wait", 10), "kill", ("wait", 5)]),
         "не ушёл за 10 с — kill и ещё до 5 с")

    proc = FakeProc(terminate_error=OSError(1, "Operation not permitted"))
    node = started(proc)
    with faked(FakeClock()):
        res, st = outcome(node.stop), outcome(node.status)
    same(res, {"ok": False, "error": "[Errno 1] Operation not permitted"}, "negative: terminate упал — отказ с текстом")
    same(dig(st, "running"), True, "negative: процесс остаётся нашим и на виду")

    same(outcome(CellProcess().stop), {"ok": True, "detail": "not running"}, "negative: не запущен — ok, «not running»")
    proc = FakeProc()
    node = started(proc)
    proc.rc = 0
    same((outcome(node.stop), proc.calls), ({"ok": True, "detail": "not running"}, []),
         "negative: уже вышел — «not running», сигналов нет")

    ws, proc = Workspace(), FakeProc()
    node, popen = CellProcess(), FakePopen(proc)
    with faked(FakeClock(), popen=popen):
        outcome(node.start, str(ws.bin), [], {"port": 22001}, log_path=ws.log)
    popen.close_logs()
    ws.log.write_text("error: out of memory\n")
    proc.rc = 1
    with faked(FakeClock()):
        crashed, stopped, after = outcome(node.status), outcome(node.stop), outcome(node.status)
    same((dig(crashed, "crashed"), stopped, after), (True, {"ok": True, "detail": "not running"}, {"running": False}),
         "стоп снимает крэш — иначе упавшую ячейку не убрать с доски без рестарта скаута")


def test_stop_adopted():
    CHECKS.section("стоп усыновлённого процесса (не наш ребёнок — по pid):")

    def adopted(clock, pid):
        node = CellProcess()
        with faked(clock, kill=pid):
            outcome(node.adopt, 4242, {"port": 22001}, started_at=int(T0))
        return node

    clock = FakeClock()
    pid = FakePid(clock, term_takes=1.0)
    node = adopted(clock, pid)
    with faked(clock, kill=pid):
        res = outcome(node.stop)
        probes = pid.probes
        st = outcome(node.status)
    same(res, {"ok": True, "adopted": True}, "ok, adopted")
    same(pid.sent, [signal.SIGTERM], "SIGTERM; ушёл за 1 с — SIGKILL не нужен")
    same(clock.sleeps, [0.3] * 4, "ждёт шагами по 0.3 с, пока жив")
    same((st, pid.probes - probes), ({"running": False}, 0), "после стопа — не запущен, pid забыт и не проверяется")

    clock = FakeClock()
    pid = FakePid(clock, term_takes=None)
    node = adopted(clock, pid)
    with faked(clock, kill=pid):
        res = outcome(node.stop)
    same((res, pid.sent), ({"ok": True, "adopted": True}, [signal.SIGTERM, signal.SIGKILL]),
         "SIGTERM проигнорирован — SIGKILL")
    same(clock.sleeps, [0.3] * 34, "ждёт ровно 10 с (34 шага по 0.3 с), потом SIGKILL")

    clock = FakeClock()
    pid = FakePid(clock, alive=False)
    node = adopted(clock, pid)
    with faked(clock, kill=pid):
        res = outcome(node.stop)
    same((res, pid.sent, clock.sleeps), ({"ok": True, "adopted": True}, [], []),
         "negative: уже мёртв — ни сигналов, ни ожидания")

    clock = FakeClock()
    denied = PermissionError(1, "Operation not permitted")
    pid = FakePid(clock, probe_error=denied, signal_error=denied)
    node = adopted(clock, pid)
    with faked(clock, kill=pid):
        res = outcome(node.stop)
        st = outcome(node.status)
    same(res, {"ok": False, "error": "[Errno 1] Operation not permitted"},
         "negative: сигнал не дошёл (процесс чужого пользователя) — отказ с текстом")
    same(st, {"running": False},
         "as-is: pid забыт ещё до сигнала — живой процесс после отказа читается «не запущен»")


# ── status ───────────────────────────────────────────────────────────────────

def test_status():
    CHECKS.section("status своего процесса:")
    same(outcome(CellProcess().status), {"running": False}, "новый узел — {running: false} и больше ничего")

    reason = lv("E", "llama_model_load: error loading model: out of memory", "0.00.200.000")
    for rc, log_text, expected, msg in (
            (1, lv("I", "main: loading model") + "\n" + reason + "\n",
             {"running": False, "exitCode": 1, "lastError": reason, "crashed": True},
             "вышел с кодом 1 — running false, код, причина из лога, crashed"),
            (-9, "",
             {"running": False, "exitCode": -9, "lastError": "", "crashed": True},
             "negative: убит сигналом (код -9) — тоже крэш"),
            (0, "Listening on 0.0.0.0:22005\nshutting down\n",
             {"running": False, "exitCode": 0, "lastError": "shutting down", "crashed": False},
             "as-is: чистый выход (код 0) — не крэш, но lastError — последняя строка лога")):
        ws, clock, proc = Workspace(), FakeClock(), FakeProc(pid=5151)
        node, popen = CellProcess(), FakePopen(proc)
        with faked(clock, popen=popen):
            outcome(node.start, str(ws.bin), [], {"port": 22001}, log_path=ws.log)
        popen.close_logs()
        ws.log.write_text(log_text)
        proc.rc = rc
        with faked(clock):
            first, second = outcome(node.status), outcome(node.status)
        same(first, expected, msg)
        if rc == 1:
            same(second, expected, "выход липкий: следующий опрос показывает то же")


# ── Cell ─────────────────────────────────────────────────────────────────────

def test_slot_defaults():
    CHECKS.section("слот ячейки по умолчанию:")
    slot, other = Cell(22001), Cell(22002)
    check(isinstance(slot.process, CellProcess), "в слоте свой CellProcess")
    same(outcome(slot.process.status), {"running": False}, "процесс не запущен")
    same(slot.startup, {"phase": "idle"}, "фаза старта — idle")
    same(slot.cache_models, False, "кэш моделей выключен")
    check(slot.process is not other.process and slot.startup is not other.startup and slot.lock is not other.lock,
          "negative: у каждого слота свои process, startup и lock — ничего общего")
    got = (slot.lock.acquire(blocking=False), slot.lock.acquire(blocking=False))
    slot.lock.release()
    same(got, (True, False), "замок обычный, не реентерабельный: второй захват без ожидания не проходит")


TESTS = (test_read_log_error_absent, test_read_log_error_priority, test_read_log_error_markers,
         test_read_log_error_levels, test_read_log_error_catastrophes, test_read_log_error_window,
         test_rotate_log, test_pid_alive, test_adopt, test_adopted_death, test_start, test_start_command,
         test_stop_own, test_stop_adopted, test_status, test_slot_defaults)

for test in TESTS:
    blocked_before = len(BLOCKED)
    try:
        test()
    except (Exception, RealCallBlocked) as exc:  # noqa: BLE001 — a crash is a red pin; the rest still runs
        check(False, f"{test.__name__} упал: {exc!r}")
    if len(BLOCKED) > blocked_before:
        check(False, f"{test.__name__} дотянулся до хоста: {BLOCKED[blocked_before:]}")

sys.exit(CHECKS.finish())
