#!/usr/bin/env python3
"""Snapshot: what the scout's model cache and cell-asset sync do today,
pinned by value.

Scope: caravan_scout/models.py (where the cache lives, the corruption
patterns, the download with its retries and truncation guard, cleanup and
purge, the cache listing, the multi-file download) and
caravan_scout/cell_assets.py (which launcher a command names, which runner
owns it, and how $HOME's copies are brought up to the controller's).

The code is about to be rewritten into classes; these pins are what the
rewrite is checked against. A pin marked `as-is:` holds behaviour that is a
defect or an ugliness, kept on purpose so that changing it is a decision and
not an accident.

Nothing real is touched (see _scout_harness.py): the network, signals and the
clock are fakes named in each test; files live in temp dirs.

Why functions: the test_* functions are the harness's contract — a list run
in order into one Checks ledger. The helpers left as functions (attempt,
env, quiet, served, lay, sync…) hold no state; each wraps one call.
Everything that keeps state — the fakes and the Rig — is a class.

The fakes repeat the ones in test_scout_cells.py. Their home is
_scout_harness.py; they are copied only because the harness was outside
this change — move them there, do not let the two copies drift.

Run: PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_scout_models.py
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks, patched, FakeRun, make_scout, BLOCKED, TMP  # noqa: E402,F401

# Path.home() and the cell-asset sync resolve through $HOME; no pin may
# write into the real one.
HOME = TMP / "home"
HOME.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(HOME)

from caravan_scout.cell_assets import CellAssets  # noqa: E402
from caravan_scout.errors import AppError  # noqa: E402

CHECKS = Checks("test_scout_models")
check = CHECKS.check

NOW = 1_790_000_000
CONTROLLER = "http://10.0.0.5:7990"
MODEL = "models/org/model-q4.gguf"
MMPROJ = "models/org/mmproj-f16.gguf"
SPEC = "models/org/draft-q8.gguf"
BODIES = {MODEL: b"GGUF model-q4", MMPROJ: b"GGUF mmproj", SPEC: b"GGUF draft"}
MIB = 1 << 20
REFUSED = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


# ── fakes (same shape as in test_scout_cells.py) ──────────────────────────

def attempt(fn):
    """(result, None) or (None, exception): a pin whose call raises reports
    FAIL and the file goes on."""
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001
        return None, exc


def err_is(err, status, text):
    return isinstance(err, AppError) and err.status == status and str(err) == text


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


@contextlib.contextmanager
def env(**values):
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
    """urllib.request.urlopen over a routing table: the longest matching URL
    substring picks a list of answers taken in order (the last repeats). An
    answer is a FakeResponse, an exception, or a callable(url) returning
    either. Every call is written down: url, headers, timeout."""

    def __init__(self, routes=None):
        self.routes = {k: (list(v) if isinstance(v, list) else [v]) for k, v in (routes or {}).items()}
        self.calls = []

    def __call__(self, req, timeout=None, **_kw):
        url = getattr(req, "full_url", req)
        headers = dict(req.header_items()) if hasattr(req, "header_items") else {}
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
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


class FakeKill:
    """os.kill over a set of live pids: signal 0 answers ProcessLookupError
    for the dead."""

    def __init__(self, alive=()):
        self.alive = set(alive)
        self.calls = []

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))
        if pid not in self.alive:
            raise ProcessLookupError(3, "No such process")
        if sig in (signal.SIGTERM, signal.SIGKILL):
            self.alive.discard(pid)


class Rig:
    """The fakes of one pin: the clock stands at NOW and sleeps are written
    down, the network and signals answer from fakes, the journal is captured.
    Checks are made after the block."""

    def __init__(self, web=None, kill=None):
        self.web = web or FakeUrlopen()
        self.kill = kill or FakeKill()
        self.sleeps = []
        self.journal = ""
        self._stack = self._buf = None

    def __enter__(self):
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(patched(time, time=lambda: float(NOW), sleep=self.sleeps.append))
        self._stack.enter_context(patched(urllib.request, urlopen=self.web))
        self._stack.enter_context(patched(os, kill=self.kill))
        self._buf = self._stack.enter_context(quiet())
        return self

    def __exit__(self, *exc):
        self.journal += self._buf.getvalue()
        self._stack.close()
        return False


def served(body, length=..., on_read=None, chunks=None):
    """A factory for one download response: Content-Length is the body's by
    default, a lie when given, absent with None."""
    def answer(_url):
        headers = {}
        size = len(body) if length is ... else length
        if size is not None:
            headers["Content-Length"] = str(size)
        return FakeResponse(body, headers=headers, chunks=chunks, on_read=on_read)
    return answer


def models_served(bodies=None, on_request=None):
    bodies = BODIES if bodies is None else bodies

    def answer(url):
        raw = urllib.parse.unquote(url.split("path=", 1)[1])
        if on_request:
            on_request(raw)
        body = bodies[raw]
        return FakeResponse(body, headers={"Content-Length": str(len(body))})
    return FakeUrlopen({"/api/models/download?path=": answer})


def lay(root, files):
    """Write {relative path: size or bytes} under root."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else b"x" * content)


def tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else []


def fetched(s, *rels):
    """Write down files as downloaded by this scout — the only ones it may delete."""
    for rel in rels:
        s.models.downloaded.add(s.models.cache_dir() / rel)


def written_down(s):
    cache = s.models.cache_dir()
    return [str(p.relative_to(cache)) for p in s.models.downloaded.paths()]


RECORD = ".caravan-downloads.json"


def sha(data):
    return hashlib.sha256(data).hexdigest()


# ── models.py ─────────────────────────────────────────────────────────────

def test_model_cache_dir():
    base = TMP / "mcache-a"
    check(make_scout({"modelsBasePath": str(base)}).models.cache_dir() == base, "modelsBasePath задан — кэш там")
    check(make_scout({"modelsBasePath": "~/models-x"}).models.cache_dir() == HOME / "models-x",
          "~ в modelsBasePath раскрыт")
    for raw in ("", "   "):
        check(make_scout({"modelsBasePath": raw}).models.cache_dir() == HOME / ".llama-model-cache",
              f"negative: пустой modelsBasePath ({raw!r}) — ~/.llama-model-cache")


def test_is_corruption_error():
    s = make_scout()
    for text in ("tensor 'blk.0' data is not within the file bounds", "model is CORRUPTED OR INCOMPLETE",
                 "gguf: Unexpected End Of File"):
        check(s.models.is_corruption_error(text) is True, f"признак порчи файла, без учёта регистра: {text!r}")
    for text in ("out of memory", "file bounds ok", "", None):
        check(s.models.is_corruption_error(text) is False, f"negative: не порча файла: {text!r}")


def test_ensure_model_local():
    s = make_scout({"controllerUrl": CONTROLLER})
    own = TMP / "store" / "org" / "model-q4.gguf"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_bytes(b"GGUF store copy")
    with Rig() as rig:
        got, err = attempt(lambda: s.models.ensure(str(own), report=False))
    check(got == own and rig.web.calls == [], "абсолютный существующий путь — берётся как есть, без загрузки")

    cache = s.models.cache_dir()
    lay(cache, {MODEL: b"GGUF cached"})
    with Rig() as rig:
        got, _ = attempt(lambda: s.models.ensure(MODEL, report=False, use_cache=True))
    check(got == cache / MODEL and rig.web.calls == [], "use_cache и копия в кэше — берётся копия, без загрузки")
    with Rig(web=models_served()) as rig:
        got, _ = attempt(lambda: s.models.ensure(MODEL, report=False, use_cache=False))
    check(got == cache / MODEL and len(rig.web.calls) == 1 and (cache / MODEL).read_bytes() == BODIES[MODEL],
          "negative: без use_cache копия в кэше не используется — модель скачана заново (по умолчанию так)")

    s = make_scout()
    with Rig() as rig:
        _, err = attempt(lambda: s.models.ensure(MODEL, report=False, use_cache=True))
    check(err_is(err, 404, f"model not found locally and controllerUrl not set: {MODEL}") and rig.web.calls == [],
          "модели нет, controllerUrl не задан — 404 с путём, в сеть не ходим")

    s = make_scout({"controllerUrl": CONTROLLER})
    elsewhere = TMP / "elsewhere" / "org" / "model-q4.gguf"
    with Rig(web=FakeUrlopen({"/api/models/download": served(b"GGUF from controller")})) as rig:
        got, _ = attempt(lambda: s.models.ensure(str(elsewhere), report=False))
    check(got == elsewhere and elsewhere.exists() and not str(elsewhere).startswith(str(s.models.cache_dir())),
          "as-is: ДЕФЕКТ — абсолютный путь, которого нет, скачивается прямо по этому пути — мимо кэша моделей")
    check(rig.web.calls[:1] and rig.web.calls[0]["url"] == f"{CONTROLLER}/api/models/download?path={elsewhere}",
          "as-is: и у контроллера просят этот абсолютный путь")


def test_ensure_model_download():
    s = make_scout({"controllerUrl": CONTROLLER + "/", "controllerToken": "tok-1"})
    cache = s.models.cache_dir()
    local = cache / MODEL
    tmp = cache / "models" / "org" / "model-q4.tmp"
    during = []
    body = BODIES[MODEL]
    web = FakeUrlopen({"/api/models/download": served(body, on_read=lambda: during.append(
        (tmp.exists(), local.exists(), dict(s.cells.startup(22001)), written_down(s))))})
    with Rig(web=web) as rig:
        got, err = attempt(lambda: s.models.ensure(MODEL, report=True, port=22001))
    check(err is None and got == local and local.read_bytes() == body, "модель скачана в кэш: <кэш>/<путь контроллера>")
    check(web.calls == [{"url": f"{CONTROLLER}/api/models/download?path=models/org/model-q4.gguf",
                         "headers": {"X-caravan-token": "tok-1"}, "timeout": 3600}],
          "запрос: /api/models/download?path=…, токен флота, таймаут 3600 с; хвостовой / у controllerUrl срезан")
    check(during[:1] and during[0][:2] == (True, False),
          "качается во временный файл <имя>.tmp — готовый файл появляется только целиком")
    check(not tmp.exists(), "negative: после загрузки временного файла нет")
    check(during[:1] and during[0][2] == {"phase": "downloading", "downloadedBytes": 0, "totalBytes": len(body),
                                          "downloadingFile": "model-q4.gguf"},
          "прогресс: фаза downloading, размер из Content-Length, имя файла")
    check(s.cells.startup(22001) == {"phase": "downloading", "downloadedBytes": len(body),
                                          "totalBytes": len(body), "downloadingFile": "model-q4.gguf"},
          "в конце — сколько скачано")
    check(f"[llama-node] download complete: model-q4.gguf — {len(body):,} bytes" in rig.journal,
          "журнал: загрузка завершена, байты")
    check(during[:1] and during[0][3] == ["models/org/model-q4.tmp"] and written_down(s) == [MODEL],
          "записано, что скачал скаут: пока идёт — .tmp, когда легла — сама модель; только это он потом удаляет")

    s = make_scout({"controllerUrl": CONTROLLER})
    with Rig(web=models_served()):
        attempt(lambda: s.models.ensure(MODEL, report=False, port=22002))
    check(22002 not in s.cells.by_port, "negative: report=False — запись старта не трогается, слот не создаётся")
    with Rig(web=FakeUrlopen({"/api/models/download": served(BODIES[MODEL])})) as rig:
        attempt(lambda: s.models.ensure(MODEL, report=True, report_label="model-q4.gguf (1/2)", port=22003))
    check(s.cells.startup(22003).get("downloadingFile") == "model-q4.gguf (1/2)",
          "report_label заменяет имя файла в прогрессе")

    s = make_scout({"controllerUrl": CONTROLLER})
    raw = "models/org/model&q4.gguf"
    with Rig(web=FakeUrlopen({"/api/models/download": served(b"GGUF amp")})) as rig:
        got, _ = attempt(lambda: s.models.ensure(raw, report=False))
    check(rig.web.calls[:1] and rig.web.calls[0]["url"] == f"{CONTROLLER}/api/models/download?path=models/org/model%26q4.gguf"
          and got == s.models.cache_dir() / raw,
          "путь в запросе экранирован (& не рвёт query), в кэше — исходное имя")

    s = make_scout({"controllerUrl": CONTROLLER})
    local = s.models.cache_dir() / MODEL
    lay(s.models.cache_dir(), {MODEL: b"GGUF old copy"})
    seen = []
    web = FakeUrlopen({"/api/models/download": served(b"GGUF new copy", on_read=lambda: seen.append(local.read_bytes()))})
    with Rig(web=web):
        attempt(lambda: s.models.ensure(MODEL, report=False))
    check(seen[:1] == [b"GGUF old copy"] and local.read_bytes() == b"GGUF new copy",
          "перекачка поверх копии: пока идёт загрузка, старая копия цела; заменяется целиком")


def test_ensure_model_progress_throttle():
    chunk = b"\0" * (8 * MIB)

    def progress(pieces):
        """downloadedBytes as the board sees it before every read, and at the end."""
        s = make_scout({"controllerUrl": CONTROLLER})
        seen = []
        answer = served(b"", length=pieces * 8 * MIB, chunks=[chunk] * pieces,
                        on_read=lambda: seen.append(s.cells.startup(22004).get("downloadedBytes")))
        with Rig(web=FakeUrlopen({"/api/models/download": answer})):
            _, err = attempt(lambda: s.models.ensure(MODEL, report=True, port=22004))
        (s.models.cache_dir() / MODEL).unlink(missing_ok=True)
        return err, seen, s.cells.startup(22004).get("downloadedBytes")

    err, seen, final = progress(5)
    check(err is None and seen == [0, 0, 0, 0, 32 * MIB, 32 * MIB],
          "прогресс пишется не на каждый кусок, а раз в 32 МиБ — меньше возни с замком")
    check(final == 40 * MIB, "в конце — всё скачанное")
    err, seen, final = progress(3)
    check(err is None and seen == [0, 0, 0, 0] and final == 24 * MIB,
          "negative: меньше 32 МиБ — промежуточных отчётов нет, только итог")


def test_ensure_model_truncation():
    s = make_scout({"controllerUrl": CONTROLLER})
    cache = s.models.cache_dir()
    with Rig(web=FakeUrlopen({"/api/models/download": served(b"x" * 60, length=100)})) as rig:
        _, err = attempt(lambda: s.models.ensure(MODEL, report=False))
    why = "incomplete download: received 60 of 100 bytes (60.0%) — connection closed prematurely"
    check(err_is(err, 500, f"model download failed: {why}"),
          "обрыв: байт меньше Content-Length — ошибка «incomplete download», а не молча обрезанная модель")
    check(len(rig.web.calls) == 4 and rig.sleeps == [5, 15, 30], "обрыв считается временным: 4 попытки с паузами 5, 15, 30 с")
    check(tree(cache) == ["models", "models/org"], "negative: ни обрезанной модели, ни .tmp в кэше не осталось")
    check("download error (attempt 4/4): model-q4.gguf" in rig.journal, "журнал считает попытки: 4/4")

    s = make_scout({"controllerUrl": CONTROLLER})
    with Rig(web=FakeUrlopen({"/api/models/download": served(b"x" * 60, length=None)})):
        got, err = attempt(lambda: s.models.ensure(MODEL, report=False))
    check(err is None and got.read_bytes() == b"x" * 60,
          "boundary: без Content-Length обрыв не распознать — принято сколько пришло")

    s = make_scout({"controllerUrl": CONTROLLER})
    lay(s.models.cache_dir(), {MODEL: b"GGUF old copy"})
    with Rig(web=FakeUrlopen({"/api/models/download": served(b"x" * 60, length=100)})):
        attempt(lambda: s.models.ensure(MODEL, report=False))
    check((s.models.cache_dir() / MODEL).read_bytes() == b"GGUF old copy",
          "negative: неудачная перекачка не портит прежнюю копию")


def test_ensure_model_retries():
    s = make_scout({"controllerUrl": CONTROLLER})
    web = FakeUrlopen({"/api/models/download": [REFUSED, urllib.error.URLError(TimeoutError("timed out")),
                                                served(BODIES[MODEL])]})
    with Rig(web=web) as rig:
        got, err = attempt(lambda: s.models.ensure(MODEL, report=False))
    check(err is None and got == s.models.cache_dir() / MODEL and len(web.calls) == 3,
          "контроллер перезапускается (refused, timed out) — повтор, третья попытка удалась")
    check(rig.sleeps == [5, 15], "паузы между попытками — 5, затем 15 с")
    check("download transient error (attempt 1)" in rig.journal and "retrying in 5s" in rig.journal,
          "журнал: временная ошибка и через сколько повтор")

    s = make_scout({"controllerUrl": CONTROLLER})
    with Rig(web=FakeUrlopen({"/api/models/download": REFUSED})) as rig:
        _, err = attempt(lambda: s.models.ensure(MODEL, report=False))
    check(err_is(err, 500, "model download failed: <urlopen error [Errno 111] Connection refused>")
          and len(rig.web.calls) == 4 and rig.sleeps == [5, 15, 30],
          "контроллер так и не ответил — 4 попытки (паузы 5, 15, 30), потом 500 с последней ошибкой")

    s = make_scout({"controllerUrl": CONTROLLER})
    not_found = urllib.error.HTTPError(f"{CONTROLLER}/api/models/download", 404, "Not Found", {}, None)
    with Rig(web=FakeUrlopen({"/api/models/download": not_found})) as rig:
        _, err = attempt(lambda: s.models.ensure(MODEL, report=False))
    check(err_is(err, 500, "model download failed: HTTP Error 404: Not Found")
          and len(rig.web.calls) == 1 and rig.sleeps == [],
          "negative: не временная ошибка (404) — без повторов")

    # Every real caller passes report=True: the retry branch reports on the
    # cell's own port. It called without one and raised TypeError on the first
    # blip — the promised 5/15/30 s never came.
    s = make_scout({"controllerUrl": CONTROLLER})
    with Rig(web=FakeUrlopen({"/api/models/download": REFUSED})) as rig:
        _, err = attempt(lambda: s.models.ensure(MODEL, report=True, port=22005))
    check(err_is(err, 500, "model download failed: <urlopen error [Errno 111] Connection refused>")
          and len(rig.web.calls) == 4 and rig.sleeps == [5, 15, 30],
          "defect-history: с report=True повторы идут — 4 попытки с паузами 5, 15, 30 с, а не TypeError на первой")
    check(s.cells.startup(22005).get("downloadingFile") == "model-q4.gguf (retry 3 in 30s…)",
          "на карточке ячейки своего порта — какой повтор и через сколько")
    check(tree(s.models.cache_dir()) == ["models", "models/org"], "временный файл убран после последней попытки")
    s = make_scout({"controllerUrl": CONTROLLER})
    with Rig(web=FakeUrlopen({"/api/models/download": REFUSED})) as rig:
        _, err = attempt(lambda: s.models.download_all(MODEL, "", "", use_cache=False, port=22006))
    check(err_is(err, 500, "model download failed: <urlopen error [Errno 111] Connection refused>")
          and len(rig.web.calls) == 4,
          "negative: _download_all_model_files тоже повторяет, а не падает TypeError")


def test_cleanup_old_models():
    s = make_scout()
    cache = s.models.cache_dir()
    lay(cache, {MODEL: 10, "models/org/old-q4.gguf": 5, "models/other/stale.gguf": 7,
                "models/org/partial.tmp": 3, "notes.json": 2, "models/lib/library.gguf": 9})
    fetched(s, MODEL, "models/org/old-q4.gguf", "models/other/stale.gguf", "models/org/partial.tmp")
    outside = TMP / "outside-cache" / "x.gguf"
    lay(outside.parent, {"x.gguf": 1})
    with Rig() as rig:
        _, err = attempt(lambda: s.models.cleanup_old(str(cache / MODEL)))
    check(err is None and tree(cache) == [RECORD, "models", "models/lib", "models/lib/library.gguf", "models/org",
                                          "models/org/model-q4.gguf", "models/org/partial.tmp", "notes.json"],
          "удалены скачанные скаутом .gguf, кроме оставленной модели; опустевшие папки убраны; не-.gguf не тронуты")
    check((cache / "models/lib/library.gguf").exists(),
          "defect-history: .gguf, который скаут не скачивал, цел — раньше удалялся любой .gguf в папке, кроме "
          "активного, а папка кэша — настройка и может указывать на библиотеку моделей")
    check(written_down(s) == [MODEL, "models/org/partial.tmp"], "удалённые вычеркнуты из записи о скачанном")
    check(outside.exists(), "negative: файлы вне кэша не трогаются")
    check("[llama-node] cleanOldModels: removed 2 file(s)" in rig.journal, "журнал считает удалённые файлы")

    s = make_scout()
    cache = s.models.cache_dir()
    lay(cache, {"a.gguf": 1, MODEL: 1})
    fetched(s, "a.gguf", MODEL)
    with Rig():
        attempt(lambda: s.models.cleanup_old(["", str(cache / "models" / "org" / ".." / "org" / "model-q4.gguf")]))
    check(tree(cache) == [RECORD, "models", "models/org", "models/org/model-q4.gguf"],
          "список путей: пустые пропущены, путь сравнивается после resolve (../ не мешает)")
    with Rig():
        attempt(lambda: s.models.cleanup_old([]))
    check(tree(cache) == [] and cache.is_dir(),
          "boundary: удалено всё — сама папка кэша остаётся, а пустая запись о скачанном уходит вместе с файлами")
    s = make_scout()
    _, err = attempt(lambda: s.models.cleanup_old([MODEL]))
    check(err is None, "negative: папки кэша нет — тихо ничего")


def test_purge_model_cache():
    def fresh():
        s = make_scout()
        lay(s.models.cache_dir(), {MODEL: 10, MMPROJ: 5, "models/org/partial.tmp": 3,
                                   "models/other/stale.gguf": 7, "notes.json": 2,
                                   "models/lib/library.gguf": 9, "models/lib/copying.tmp": 4})
        fetched(s, MODEL, MMPROJ, "models/org/partial.tmp", "models/other/stale.gguf")
        return s, s.models.cache_dir()

    s, cache = fresh()
    with Rig() as rig:
        res, _ = attempt(lambda: s.models.purge(keep=[str(cache / MODEL)]))
    check(res == {"removed": 3, "freedBytes": 15}, "очистка кэша: сколько файлов удалено и сколько байт освобождено")
    check(tree(cache) == [RECORD, "models", "models/lib", "models/lib/copying.tmp", "models/lib/library.gguf",
                          "models/org", "models/org/model-q4.gguf", "notes.json"],
          "удалены скачанные скаутом .gguf и .tmp, кроме оставленных; опустевшие папки убраны; прочие файлы целы")
    check("[llama-node] purge cache: removed 3 file(s), freed 15 bytes" in rig.journal, "журнал: сколько и сколько байт")
    s, cache = fresh()
    with Rig():
        res, _ = attempt(lambda: s.models.purge())
    check(res == {"removed": 4, "freedBytes": 25}
          and tree(cache) == ["models", "models/lib", "models/lib/copying.tmp", "models/lib/library.gguf", "notes.json"],
          "negative: без keep — удалено всё скачанное скаутом, и только оно")
    check((cache / "models/lib/library.gguf").exists() and (cache / "models/lib/copying.tmp").exists(),
          "defect-history: чужие .gguf и .tmp в папке кэша целы — очистка при остановке (кэш выключен) удаляла "
          "всё по шаблону, и папка-библиотека теряла бы модели")
    s, cache = fresh()
    with Rig():
        res, _ = attempt(lambda: s.models.purge(keep=str(cache / MMPROJ)))
    check(res == {"removed": 3, "freedBytes": 20} and (cache / MMPROJ).exists(), "keep строкой — тоже работает")
    check(make_scout().models.purge() == {"removed": 0, "freedBytes": 0}, "boundary: папки кэша нет — нули")


def test_purge_models_safely():
    def fresh():
        s = make_scout()
        cache = s.models.cache_dir()
        lay(cache, {MODEL: 10, MMPROJ: 5, SPEC: 4, "models/other/stale.gguf": 7, "models/org/partial.tmp": 3})
        fetched(s, MODEL, MMPROJ, SPEC, "models/other/stale.gguf", "models/org/partial.tmp")
        return s, cache

    s, cache = fresh()
    s.cells.at(22001).process.adopt(5151, {"modelPath": str(cache / MODEL), "mmprojPath": str(cache / MMPROJ),
                                     "specPath": str(cache / SPEC), "port": 22001})
    s.cells.at(22002).process.adopt(5252, {"modelPath": str(cache / "models/other/stale.gguf"), "port": 22002})
    with Rig(kill=FakeKill(alive={5151})):
        res, _ = attempt(s.cells.purge_models_safely)
    check(res == {"removed": 2, "freedBytes": 10}, "безопасная очистка: удалены только файлы, которые никто не держит")
    check((cache / MODEL).exists() and (cache / MMPROJ).exists() and (cache / SPEC).exists(),
          "модель, mmproj и черновая модель работающей ячейки целы — живой сервер не ломается")
    check(not (cache / "models/other/stale.gguf").exists(),
          "negative: модель остановившейся ячейки удалена — её процесс мёртв")

    s, cache = fresh()
    with Rig():
        res, _ = attempt(s.cells.purge_models_safely)
    check(res == {"removed": 5, "freedBytes": 29}, "negative: работающих ячеек нет — удаляется всё")


def test_in_place():
    lib = TMP / "lib-in-place"
    lay(lib, {"org/model-q4.gguf": BODIES[MODEL], "seam/FP32/weights.bin": 3})
    model = lib / "org" / "model-q4.gguf"
    hint = {"path": str(model), "size": len(BODIES[MODEL])}
    cached = lambda s: s.models.cache_dir() / MODEL  # noqa: E731

    s = make_scout({"controllerUrl": CONTROLLER})
    with Rig(web=models_served()) as rig:
        got, err = attempt(lambda: s.models.ensure(MODEL, report=True, port=22301, hint=hint))
    check(err is None and got == model and rig.web.calls == [] and written_down(s) == []
          and f"[llama-node] model-q4.gguf: read in place — {model}" in rig.journal,
          "тот же файл по пути контроллера (скаут на его машине, библиотека по тому же пути) — читается на "
          "месте: без закачки и без записи в скачанное, значит, никогда не удаляется")

    s = make_scout({"controllerUrl": CONTROLLER})
    with Rig(web=models_served()) as rig:
        got, _ = attempt(lambda: s.models.ensure(MODEL, report=False, hint={**hint, "size": 999}))
    check(got == cached(s) and len(rig.web.calls) == 1
          and f"[llama-node] {MODEL}: {model} is here, but it is not the controller's file "
              f"({len(BODIES[MODEL]):,} bytes, not 999) — not reading it in place" in rig.journal,
          "negative: по тому пути другой файл (размер не тот) — не читается, модель качается как раньше")

    s = make_scout({"controllerUrl": CONTROLLER})
    with Rig(web=models_served()) as rig:
        got, _ = attempt(lambda: s.models.ensure(MODEL, report=False,
                                                 hint={"path": str(TMP / "elsewhere" / "m.gguf"), "size": 13}))
    check(got == cached(s) and len(rig.web.calls) == 1,
          "negative: файла по пути контроллера здесь нет (другая машина) — кэш и закачка, как без подсказки")

    seam = lib / "seam" / "FP32"
    s = make_scout({"controllerUrl": CONTROLLER})
    with Rig(web=models_served()) as rig:
        got, err = attempt(lambda: s.models.ensure("seam/FP32", report=False, hint={"path": str(seam), "dir": True}))
    check(err is None and got == seam and rig.web.calls == [], "модель-папка (seamless) на месте — читается там")

    gone = TMP / "no-such-lib" / "org" / "model-q4.gguf"
    with Rig(web=models_served()) as rig:
        _, err = attempt(lambda: s.models.ensure(MODEL, report=False,
                                                 hint={"path": str(gone), "library": "NAS", "size": 13}))
    check(err_is(err, 409, f"the model is in the library NAS ({gone}), which this machine does not have there — "
                           f"mount the library at the same path, or bring the model back to the controller")
          and rig.web.calls == [],
          "модели из библиотеки здесь нет — отказ 409 с именем библиотеки и путём, без закачки, которая могла "
          "ответить только 404")
    s = make_scout({"controllerUrl": CONTROLLER})
    lay(s.models.cache_dir(), {MODEL: BODIES[MODEL]})
    with Rig(web=models_served()) as rig:
        got, err = attempt(lambda: s.models.ensure(MODEL, report=False, use_cache=True,
                                                   hint={"path": str(gone), "library": "NAS", "size": 13}))
    check(err is None and got == cached(s) and rig.web.calls == [],
          "negative: библиотеки здесь нет, но модель уже в кэше скаута (скачана раньше) — берётся из кэша, "
          "а не отказ")
    folder = lib / "seam" / "gone"
    with Rig(web=models_served()) as rig:
        _, err = attempt(lambda: s.models.ensure("seam/gone", report=False, hint={"path": str(folder), "dir": True}))
    check(err_is(err, 409, f"the model is a folder ({folder}) and this machine does not have it there — a folder "
                           f"cannot be downloaded; put it there, or mount the library that holds it")
          and rig.web.calls == [],
          "модели-папки здесь нет — отказ 409: папку не скачать")

    s = make_scout({"controllerUrl": CONTROLLER})
    release = threading.Event()

    def hung(_path, _want_dir):
        release.wait(2)
        return len(BODIES[MODEL])
    with patched(s.models, _stat=hung, PROBE_SECONDS=0.05), Rig(web=models_served()) as rig:
        got, _ = attempt(lambda: s.models.ensure(MODEL, report=False, hint=hint))
    release.set()
    check(got == cached(s) and len(rig.web.calls) == 1
          and f"[llama-node] {model} did not answer in 0.05 s — not reading it in place" in rig.journal,
          "путь не ответил вовремя (мёртвый NFS заставляет stat ждать) — его не ждут: закачка, как без подсказки")

    s = make_scout({"controllerUrl": CONTROLLER})
    with Rig(web=models_served()) as rig:
        got, err = attempt(lambda: s.models.download_all(MODEL, MMPROJ, "", use_cache=False, port=22303,
                                                         hints={MODEL: hint}))
    check(err is None and got == (str(model), str(s.models.cache_dir() / MMPROJ), "")
          and [urllib.parse.unquote(c["url"].split("path=", 1)[1]) for c in rig.web.calls] == [MMPROJ],
          "подсказка по каждому файлу: модель читается на месте, mmproj без подсказки качается")


def test_list_cached_models():
    s = make_scout()
    check(s.models.listing() == [], "папки кэша нет — пустой список")
    lay(s.models.cache_dir(), {MODEL: 10, MMPROJ: 5, SPEC: 4, "models/other/stale.gguf": 7,
                               "models/org/partial.tmp": 3, "notes.json": 2})
    check(s.models.listing() == [{"path": SPEC, "sizeBytes": 4}, {"path": MMPROJ, "sizeBytes": 5},
                                     {"path": MODEL, "sizeBytes": 10},
                                     {"path": "models/other/stale.gguf", "sizeBytes": 7}],
          "список кэша: только .gguf, путь относительно кэша, размер, по алфавиту пути")


def test_download_all_model_files():
    def download(mmproj, spec, use_cache=False, prep=None):
        s = make_scout({"controllerUrl": CONTROLLER})
        if prep:
            prep(s)
        labels = []   # the label on the board when each file's first bytes arrive

        def labelled(url):
            raw = urllib.parse.unquote(url.split("path=", 1)[1])
            noted = []

            def note():
                if not noted:
                    noted.append(True)
                    labels.append(s.cells.startup(22007).get("downloadingFile"))
            body = BODIES[raw]
            return FakeResponse(body, headers={"Content-Length": str(len(body))}, on_read=note)
        web = FakeUrlopen({"/api/models/download?path=": labelled})
        with Rig(web=web):
            res, err = attempt(lambda: s.models.download_all(MODEL, mmproj, spec, use_cache=use_cache,
                                                                   port=22007))
        return SimpleNamespace(s=s, res=res, err=err, labels=labels, web=web, cache=s.models.cache_dir())

    d = download("", "")
    check(d.res == (str(d.cache / MODEL), "", "") and d.labels == ["model-q4.gguf"],
          "одна модель: подпись без «(1/1)», mmproj и spec пусты")
    check(isinstance(d.res[0], str), "пути возвращаются строками")
    d = download(MMPROJ, "")
    check(d.res == (str(d.cache / MODEL), str(d.cache / MMPROJ), "")
          and d.labels == ["model-q4.gguf (1/2)", "mmproj-f16.gguf (2/2)"],
          "модель + mmproj: подписи «(1/2)», «(2/2)»; spec пуст")
    d = download("", SPEC)
    check(d.labels == ["model-q4.gguf (1/2)", "draft-q8.gguf (2/2)"]
          and (d.cache / MODEL).exists() and (d.cache / SPEC).exists(),
          "модель + spec без mmproj: оба файла скачаны с подписями «(1/2)», «(2/2)»")
    check(d.err is None and d.res == (str(d.cache / MODEL), "", str(d.cache / SPEC)),
          "defect-history: spec без mmproj — путь черновой модели на месте spec, mmproj пуст; "
          "раньше IndexError, и такая ячейка не стартовала никогда")
    d = download(MMPROJ, SPEC)
    check(d.res == (str(d.cache / MODEL), str(d.cache / MMPROJ), str(d.cache / SPEC))
          and d.labels == ["model-q4.gguf (1/3)", "mmproj-f16.gguf (2/3)", "draft-q8.gguf (3/3)"],
          "все три: подписи «(1/3)…(3/3)», каждый путь на своём месте")

    d = download(MMPROJ, "", use_cache=True, prep=lambda s: lay(s.models.cache_dir(), {MODEL: b"c", MMPROJ: b"c"}))
    check(d.res == (str(d.cache / MODEL), str(d.cache / MMPROJ), "") and d.web.calls == [],
          "use_cache передан каждому файлу: закэшированные не качаются")

    s = make_scout({"controllerUrl": CONTROLLER})
    with Rig(web=models_served({MODEL: BODIES[MODEL]})):
        res, err = attempt(lambda: s.models.download_all(MODEL, MMPROJ, "", use_cache=False, port=22008))
    check(isinstance(err, AppError) and err.status == 500 and (s.models.cache_dir() / MODEL).exists(),
          "negative: второй файл не скачался — ошибка, а не частичный результат (первый уже лежит в кэше)")


# ── CellAssets (cell_assets.py) ───────────────────────────────────────────

WHISPER_SH = b"#!/bin/bash\nexec python3 whisper_server.py \"$@\"\n"
WHISPER_PY = b"print('whisper server')\n"
MANIFEST = {"runners": {"whisper": ["run_whisper.sh", "whisper_server.py"]},
            "assets": {"run_whisper.sh": {"sha256": sha(WHISPER_SH)},
                       "whisper_server.py": {"sha256": sha(WHISPER_PY)}}}
_homes = iter(range(1000))


def fresh_home():
    home = TMP / f"asset-home-{next(_homes)}"
    home.mkdir(parents=True)
    return home


def sync(command, manifest=None, files=None, controller=CONTROLLER, home=..., headers=None,
         manifest_answer=None, **kw):
    """CellAssets.sync against a fake controller: `manifest` is served as
    JSON (or `manifest_answer` instead), `files` maps a name to its bytes or
    an exception."""
    home = fresh_home() if home is ... else home
    files = {"run_whisper.sh": WHISPER_SH, "whisper_server.py": WHISPER_PY} if files is None else files
    lines = []

    def file_answer(url):
        name = urllib.parse.unquote(url.split("name=", 1)[1])
        got = files[name]
        if isinstance(got, BaseException):
            raise got
        return FakeResponse(got)
    web = FakeUrlopen({
        "/api/cell-assets": manifest_answer if manifest_answer is not None else (
            lambda url: FakeResponse(json.dumps(MANIFEST if manifest is None else manifest).encode())),
        "/api/cell-assets/file?name=": file_answer})
    with patched(urllib.request, urlopen=web):
        out, err = attempt(lambda: CellAssets(
            controller, {"X-Caravan-Token": "tok-1"} if headers is None else headers,
            home=None if home is None else str(home), log=lines.append, **kw).sync(command))
    fetched = [urllib.parse.unquote(c["url"].split("name=", 1)[1]) for c in web.calls if "name=" in c["url"]]
    return SimpleNamespace(out=out, err=err, lines=lines, web=web, home=home, fetched=fetched)


def test_launcher_and_runner():
    stem = CellAssets.launcher_stem
    check(stem('bash $HOME/run_moonshine.sh "$PORT" en') == "moonshine", "лаунчер run_<stem>.sh найден в строке")
    check(stem("python3 -m http.server $PORT") == "" and stem(None) == "",
          "negative: строка без лаунчера (или None) — пусто")
    check(stem("bash ~/RUN_WHISPER.SH") == "" and stem("bash xrun_whisper.sh") == "",
          "boundary: только строчные и только с границы слова (xrun_… — не лаунчер)")
    rfc = CellAssets.runner_for_command
    check([rfc("bash ~/run_moonshine.sh $PORT"), rfc("bash ~/run_whisper.sh $PORT"), rfc("bash ~/run_tts.sh $PORT"),
           rfc("bash ~/run_transcribe.sh $PORT")] == ["moonshine", "whisper", "custom", "transcribe"],
          "запасная таблица: moonshine, whisper, tts → custom, transcribe")
    check(rfc("bash ~/run_newthing.sh") == "" and rfc("python3 srv.py") == "",
          "negative: незнакомый лаунчер или его отсутствие — пусто, а не догадка")
    rfm = CellAssets.runner_from_manifest
    manifest = {"runners": {"cosyvoice": ["run_tts.sh", "tts_server.py"], "broken": None}}
    check(rfm("tts", manifest) == "cosyvoice", "владелец лаунчера — по манифесту контроллера")
    check(rfm("whisper", manifest) == "" and rfm("tts", {}) == "",
          "negative: манифест не знает лаунчер (или без runners) — пусто")
    f = TMP / "digest-me.sh"
    f.write_bytes(WHISPER_SH)
    check(CellAssets.digest(str(f)) == sha(WHISPER_SH), "digest — sha256 содержимого")
    check(CellAssets.digest(str(TMP / "no-such-file")) == "", "negative: нет файла — пустая строка")


def test_sync_nothing_to_do():
    r = sync("python3 -m http.server $PORT")
    check(r.out == {} and r.lines == [] and r.web.calls == [],
          "команда без лаунчера — синхронизировать нечего: ни запросов, ни строк в журнале")
    r = sync("bash $HOME/run_whisper.sh $PORT", controller="")
    check(r.out == {} and r.lines == ["cell-assets: no controllerUrl — keeping local copies"] and r.web.calls == [],
          "нет controllerUrl — остаёмся на локальных копиях и говорим об этом в журнале")
    r = sync("bash $HOME/run_whisper.sh $PORT", manifest_answer=urllib.error.URLError("connection refused"))
    check(r.out == {} and r.lines == ["cell-assets: manifest unavailable (<urlopen error connection refused>) — "
                                      "keeping local copies"] and len(r.web.calls) == 1,
          "манифест недоступен — локальные копии, причина в журнале, файлы не запрашиваются")
    r = sync("bash $HOME/run_whisper.sh $PORT", manifest_answer=lambda url: FakeResponse(b"<html>"))
    check(r.out == {} and r.lines == ["cell-assets: manifest unavailable (Expecting value: line 1 column 1 (char 0)) "
                                      "— keeping local copies"],
          "negative: манифест не JSON — то же, а не исключение")
    r = sync("bash $HOME/run_newthing.sh $PORT")
    check(r.out == {} and r.lines == ["cell-assets: run_newthing.sh belongs to no runner the controller publishes — "
                                      "keeping local copies (upgrade the controller, or this cell runs whatever is "
                                      "already in $HOME)"] and r.fetched == [],
          "лаунчер ничей (ни в манифесте, ни в таблице) — громко в журнал, локальные копии")
    r = sync("bash $HOME/run_whisper.sh $PORT", manifest={"runners": {}, "assets": {}})
    check(r.out == {} and r.lines == [] and r.fetched == [],
          "as-is: раннер знаком только таблице, а манифест не перечисляет его файлы — ничего не синхронизировано и "
          "в журнале ни строки")


def test_sync_runner_resolution():
    tts_sh, tts_py = b"#!/bin/bash\nexec python3 tts_server.py\n", b"print('tts')\n"
    manifest = {"runners": {"cosyvoice": ["run_tts.sh", "tts_server.py"], "custom": ["run_custom.sh"]},
                "assets": {"run_tts.sh": {"sha256": sha(tts_sh)}, "tts_server.py": {"sha256": sha(tts_py)},
                           "run_custom.sh": {"sha256": sha(b"custom")}}}
    r = sync("bash $HOME/run_tts.sh $PORT", manifest=manifest,
             files={"run_tts.sh": tts_sh, "tts_server.py": tts_py, "run_custom.sh": b"custom"})
    check(r.out == {"run_tts.sh": "updated", "tts_server.py": "updated"} and r.fetched == ["run_tts.sh", "tts_server.py"],
          "владелец лаунчера берётся из манифеста контроллера (cosyvoice), а не из своей таблицы (custom)")
    check("run_custom.sh" not in r.fetched, "negative: файлы раннера из запасной таблицы не качаются")
    r = sync("bash $HOME/run_whisper.sh $PORT",
             manifest={"runners": {"whisper": ["whisper_server.py"]},
                       "assets": {"whisper_server.py": {"sha256": sha(WHISPER_PY)}}})
    check(r.out == {"whisper_server.py": "updated"} and r.fetched == ["whisper_server.py"],
          "манифест не знает лаунчер — раннер из запасной таблицы, файлы по списку манифеста")


def test_sync_files():
    home = fresh_home()
    (home / "run_whisper.sh").write_bytes(WHISPER_SH)
    r = sync("bash $HOME/run_whisper.sh $PORT", home=home)
    check(r.out == {"run_whisper.sh": "current", "whisper_server.py": "updated"} and r.fetched == ["whisper_server.py"],
          "копия совпадает по sha256 — current, не качается; недостающий файл скачан")

    home = fresh_home()
    old = home / "run_whisper.sh"
    old.write_bytes(b"#!/bin/bash\necho old\n")
    os.link(old, home / "reader.sh")
    r = sync("bash $HOME/run_whisper.sh $PORT", home=home)
    check(r.out == {"run_whisper.sh": "updated", "whisper_server.py": "updated"}
          and old.read_bytes() == WHISPER_SH and (home / "whisper_server.py").read_bytes() == WHISPER_PY,
          "устаревшая копия заменена копией контроллера")
    check(old.stat().st_mode & 0o777 == 0o755, ".sh — исполняемый (0755)")
    check((home / "whisper_server.py").stat().st_mode & 0o111 == 0, "negative: .py без флага executable — не исполняемый")
    check((home / "reader.sh").read_bytes() == b"#!/bin/bash\necho old\n"
          and old.stat().st_ino != (home / "reader.sh").stat().st_ino
          and sorted(p.name for p in home.iterdir()) == ["reader.sh", "run_whisper.sh", "whisper_server.py"],
          "замена атомарная (через .new и rename): полузаписанного лаунчера не бывает, .new не остаётся")
    check("cell-assets: run_whisper.sh updated from controller" in r.lines, "журнал называет обновлённый файл")

    manifest = {"runners": MANIFEST["runners"],
                "assets": {**MANIFEST["assets"], "whisper_server.py": {"sha256": sha(WHISPER_PY), "executable": True}}}
    r = sync("bash $HOME/run_whisper.sh $PORT", manifest=manifest)
    check((r.home / "whisper_server.py").stat().st_mode & 0o777 == 0o755, "флаг executable в манифесте — 0755 и не для .sh")

    home = fresh_home()
    (home / "run_whisper.sh").write_bytes(b"#!/bin/bash\necho old\n")
    r = sync("bash $HOME/run_whisper.sh $PORT", home=home,
             files={"run_whisper.sh": b"#!/bin/bash\necho trunc", "whisper_server.py": WHISPER_PY})
    check(r.out.get("run_whisper.sh") == "kept: hash mismatch"
          and (home / "run_whisper.sh").read_bytes() == b"#!/bin/bash\necho old\n"
          and not (home / "run_whisper.sh.new").exists(),
          "пришло не то (sha256 не сходится) — рабочий лаунчер не затирается мусором")
    check("cell-assets: run_whisper.sh arrived with the wrong hash — keeping local copy" in r.lines,
          "журнал: не тот хэш, копия оставлена")

    home = fresh_home()
    (home / "run_whisper.sh").write_bytes(b"#!/bin/bash\necho old\n")
    r = sync("bash $HOME/run_whisper.sh $PORT", home=home,
             files={"run_whisper.sh": urllib.error.URLError("timed out"), "whisper_server.py": WHISPER_PY})
    check(r.out == {"run_whisper.sh": "kept: <urlopen error timed out>", "whisper_server.py": "updated"}
          and (home / "run_whisper.sh").read_bytes() == b"#!/bin/bash\necho old\n",
          "файл не скачался — локальная копия остаётся, остальные файлы идут дальше")
    check("cell-assets: run_whisper.sh not fetched (<urlopen error timed out>) — keeping local copy" in r.lines,
          "журнал: не скачан, причина")

    home = fresh_home()
    (home / "run_whisper.sh").write_bytes(WHISPER_SH)
    r = sync("bash $HOME/run_whisper.sh $PORT", home=home, manifest={"runners": MANIFEST["runners"], "assets": {}})
    check(r.out == {"run_whisper.sh": "updated", "whisper_server.py": "updated"} and len(r.fetched) == 2,
          "boundary: у файла нет sha256 в манифесте — качается всегда и принимается без проверки")


def test_sync_requests():
    r = sync("bash $HOME/run_whisper.sh $PORT", controller=CONTROLLER + "/", timeout=3)
    check(r.web.calls[:1] == [{"url": f"{CONTROLLER}/api/cell-assets", "headers": {"X-caravan-token": "tok-1"},
                               "timeout": 3}],
          "манифест: /api/cell-assets, токен флота, свой таймаут; хвостовой / у controllerUrl срезан")
    check([c["url"] for c in r.web.calls[1:]] == [f"{CONTROLLER}/api/cell-assets/file?name=run_whisper.sh",
                                                 f"{CONTROLLER}/api/cell-assets/file?name=whisper_server.py"]
          and all(c["headers"] == {"X-caravan-token": "tok-1"} for c in r.web.calls),
          "файлы: /api/cell-assets/file?name=…, с тем же токеном")
    r = sync("bash $HOME/run_whisper.sh $PORT")
    check({c["timeout"] for c in r.web.calls} == {10}, "таймаут по умолчанию — 10 с")
    plus = b"print('c++')\n"
    r = sync("bash $HOME/run_whisper.sh $PORT",
             manifest={"runners": {"whisper": ["c++_helper.py"]}, "assets": {"c++_helper.py": {"sha256": sha(plus)}}},
             files={"c++_helper.py": plus})
    check([c["url"] for c in r.web.calls[1:]] == [f"{CONTROLLER}/api/cell-assets/file?name=c%2B%2B_helper.py"],
          "имя файла в запросе экранировано")
    home = fresh_home()
    with env(HOME=str(home)):
        r = sync("bash $HOME/run_whisper.sh $PORT", home=None)
    check((home / "run_whisper.sh").exists(), "negative: без home — копии в $HOME")


def test_sync_malformed_manifest():
    r = sync("bash $HOME/run_whisper.sh $PORT", manifest=[1, 2])
    check(isinstance(r.err, AttributeError),
          "as-is: ДЕФЕКТ — «never raises» из докстроки неправда: манифест-JSON не объектом роняет sync (AttributeError)")
    r = sync("bash $HOME/run_whisper.sh $PORT", manifest={"runners": ["run_whisper.sh"]})
    check(isinstance(r.err, AttributeError), "as-is: ДЕФЕКТ — runners списком вместо объекта — тоже исключение")


TESTS = [
    test_model_cache_dir, test_is_corruption_error, test_ensure_model_local, test_ensure_model_download,
    test_ensure_model_progress_throttle, test_ensure_model_truncation, test_ensure_model_retries,
    test_cleanup_old_models, test_purge_model_cache, test_purge_models_safely, test_in_place, test_list_cached_models,
    test_download_all_model_files,
    test_launcher_and_runner, test_sync_nothing_to_do, test_sync_runner_resolution, test_sync_files,
    test_sync_requests, test_sync_malformed_manifest,
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
