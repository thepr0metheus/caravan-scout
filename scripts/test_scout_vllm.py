#!/usr/bin/env python3
"""vLLM on a scout's machine: its version, its history, its updates (2.9).

The controller's System panel updated and rolled back vLLM in the venv of its
own machine only. The controller's machine runs its cells through its scout
now, and a vLLM cell of any scout runs from that machine's ~/vllm-venv — so
the scout answers for it: which version is installed (read from the
dist-info folder, as pip reads it), which versions the venv had (the
rollback candidates), and a background pip job, apart from the llama.cpp
build's, that installs another.

Pinned by value with a venv in a temp folder, a fake thread and a fake
pip: the version, the history, what a start runs and what it refuses, the
job, the routes and where the scout keeps it all.

Run: python3 scripts/test_scout_vllm.py
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import TMP, Checks, Served, make_scout, patched  # noqa: E402

from caravan_scout.errors import AppError  # noqa: E402
from caravan_scout.vllm import VllmVenv  # noqa: E402

CHECKS = Checks("scout vllm")
check = CHECKS.check
NOW = 1_790_000_000


def venv_with(*dist_infos, pip=True):
    """A venv folder holding these dist-info folders (name, mtime)."""
    root = Path(os.path.realpath(TMP)) / f"venv-{time.monotonic_ns()}"
    site = root / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    for name, mtime in dist_infos:
        (site / name).mkdir()
        os.utime(site / name, (mtime, mtime))
    if pip:
        (root / "bin").mkdir()
        (root / "bin" / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
    return root


def fresh(venv=None, history=None):
    venv = venv or venv_with(("vllm-0.24.0.dist-info", NOW))
    history = history or venv.parent / f"history-{time.monotonic_ns()}.json"
    return VllmVenv(venv, history)


class Threads:
    """threading.Thread written down, not run until asked."""

    def __init__(self):
        self.made = []

    def __call__(self, target=None, args=(), daemon=None, name=None, **_kw):
        outer = self

        class T:
            def __init__(self):
                self.target, self.args, self.daemon, self.name = target, args, daemon, name
                self.started = False
                outer.made.append(self)

            def start(self):
                self.started = True

            def run_now(self):
                self.target(*self.args)
        return T()


class Pip:
    """subprocess.Popen for pip: prints `lines`, exits with `rc`."""

    def __init__(self, lines=(), rc=0):
        self.lines, self.rc = list(lines), rc
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append({"argv": list(argv), **kw})
        outer = self

        class P:
            stdout = iter(outer.lines)

            def wait(self):
                return outer.rc
        return P()


def attempt(fn):
    try:
        return fn(), None
    except AppError as exc:
        return None, exc


def test_version():
    CHECKS.section("версия vLLM в venv:")
    check(fresh().version() == "0.24.0", "по папке dist-info: vllm-0.24.0.dist-info — 0.24.0")
    two = fresh(venv_with(("vllm-0.23.1.dist-info", NOW - 100), ("vllm-0.24.0.dist-info", NOW)))
    check(two.version() == "0.24.0", "boundary: две папки (прерванная установка) — версия новейшей")
    check(fresh(venv_with(("vllm_flash_attn-2.6.2.dist-info", NOW))).version() == "",
          "negative: соседний пакет с тем же началом имени — не vLLM")
    check(fresh(Path(os.path.realpath(TMP)) / "no-venv").version() == "", "negative: venv нет — пусто")


def test_history():
    CHECKS.section("история версий (кандидаты на откат):")
    v = fresh()
    check(v.history() == [], "файла нет — история пуста")
    v.remember("")
    check(not v.history_file.exists(), "negative: пустая версия не записывается")
    with patched(time, time=lambda: NOW):
        for ver in ("0.22.0", "0.23.1", "0.24.0", "0.23.1", "0.21.0", "0.20.0", "0.19.0"):
            v.remember(ver)
    check([r["version"] for r in v.history()] == ["0.19.0", "0.20.0", "0.21.0", "0.23.1", "0.24.0"],
          "новейшая первой, повтор поднимается наверх, а не дублируется, хранится 5")
    check(v.history()[0] == {"version": "0.19.0", "seenAt": NOW}, "у записи — версия и когда её видели")
    short = fresh()
    for ver in ("0.22.0", "0.23.1", "0.22.0"):
        short.remember(ver)
    check([r["version"] for r in short.history()] == ["0.22.0", "0.23.1"],
          "повтор — одна запись, поднятая наверх, а не вторая")
    v.history_file.write_text("{not json", encoding="utf-8")
    check(v.history() == [], "negative: испорченный файл — история пуста, без исключения")
    v.history_file.write_text(json.dumps([{"version": "0.24.0"}, "junk", {"seenAt": 1}]), encoding="utf-8")
    check(v.history() == [{"version": "0.24.0"}], "negative: записи без версии и не-записи отброшены")
    blocked = Path(os.path.realpath(TMP)) / f"a-file-{time.monotonic_ns()}"
    blocked.write_text("x", encoding="utf-8")
    stuck = fresh(history=blocked / "history.json")
    printed = []
    import builtins
    with patched(builtins, print=lambda *a, **k: printed.append(" ".join(map(str, a)))):
        stuck.remember("0.24.0")
    check(printed and printed[0].startswith(f"[vllm] history not written ({blocked / 'history.json'})"),
          "историю не записать — чтение версии не падает, журнал говорит почему")


def test_info():
    CHECKS.section("что видит панель:")
    v = fresh()
    with patched(time, time=lambda: NOW):
        got = v.info()
    check(got == {"ok": True, "installed": True, "version": "0.24.0", "venv": str(v.venv),
                  "history": [{"version": "0.24.0", "seenAt": NOW}],
                  "job": {"running": False, "done": False, "rc": None, "startedAt": 0, "tag": "", "lastLine": ""}},
          "версия, venv, история с текущей первой и задание (коротко)")
    none = fresh(Path(os.path.realpath(TMP)) / "no-venv-2")
    got = none.info()
    check(got["installed"] is False and got["version"] == "" and got["history"] == [],
          "negative: vLLM нет — не установлен, история не пополняется пустой версией")


def test_update():
    CHECKS.section("обновление и откат:")
    v, threads = fresh(), Threads()
    with patched(threading, Thread=threads), patched(time, time=lambda: NOW):
        got, err = attempt(lambda: v.start_update({"version": " 0.23.1 "}))
    t = threads.made[0] if threads.made else None
    check(err is None and got == {"running": True, "startedAt": NOW, "tag": "vllm:0.23.1", "lines": [],
                                  "done": False, "rc": None, "error": ""},
          "откат — версия закреплена, задание сразу running с тегом vllm:<версия>")
    check(t is not None and t.started and t.daemon is True and t.name == "vllm-update",
          "установка идёт в фоновом daemon-потоке vllm-update, не в запросе")
    check([r["version"] for r in v.history()] == ["0.24.0"],
          "текущая версия записана в историю до установки — к ней можно вернуться")
    pip = Pip(["\x1b[32mCollecting vllm==0.23.1\x1b[0m\n", "Successfully installed vllm-0.23.1\n"])
    with patched(subprocess, Popen=pip):
        t.run_now()
    check(pip.calls and pip.calls[0]["argv"] == [str(v.venv / "bin" / "pip"), "install", "vllm==0.23.1"],
          "pip из venv машины: install vllm==<версия>")
    check(v.job.status()["lines"] == ["Collecting vllm==0.23.1", "Successfully installed vllm-0.23.1"]
          and v.job.status()["rc"] == 0 and v.job.status()["done"] is True,
          "вывод pip — в строки задания без цветовых кодов, rc и done")

    v, threads = fresh(), Threads()
    with patched(threading, Thread=threads):
        got, err = attempt(lambda: v.start_update({}))
    pip = Pip()
    with patched(subprocess, Popen=pip):
        threads.made[0].run_now()
    check(got["tag"] == "vllm:latest" and pip.calls[0]["argv"] == [str(v.venv / "bin" / "pip"), "install",
                                                                    "--upgrade", "vllm"],
          "без версии — последняя: install --upgrade vllm, тег vllm:latest")
    with patched(threading, Thread=Threads()):
        got2, err2 = attempt(lambda: fresh().start_update(None))
    check(err2 is None and got2["tag"] == "vllm:latest", "boundary: тело None — как пустое")

    v, threads = fresh(), Threads()
    with patched(threading, Thread=threads):
        attempt(lambda: v.start_update({"version": "0.23.1"}))
        again, err = attempt(lambda: v.start_update({"version": "0.22.0"}))
    check(err is not None and err.status == 409 and str(err) == "a vLLM install is already running"
          and len(threads.made) == 1 and v.job.status()["tag"] == "vllm:0.23.1",
          "второй старт, пока идёт первый, — 409; идущее задание не тронуто")

    for why, version in (("слово", "latest"), ("команда в версии", "0.24.0; rm -rf ~"), ("ключ pip", "-e git+x"),
                         ("пробел внутри", "0.24 .0")):
        v, threads = fresh(), Threads()
        with patched(threading, Thread=threads):
            got, err = attempt(lambda: v.start_update({"version": version}))
        check(err is not None and err.status == 400 and str(err) == f"not a vLLM version: {version.strip()!r}"
              and threads.made == [] and v.history() == [],
              f"negative: {why} — 400 до всего: ни задания, ни записи в историю")
    bare, th = fresh(venv_with(pip=False)), Threads()
    with patched(threading, Thread=th):
        got, err = attempt(lambda: bare.start_update({"version": "0.24.0"}))
    check(err is not None and err.status == 400 and str(err) == (
        "vLLM is not installed on this machine yet — start a vLLM cell once to create its venv "
        f"({bare.venv})") and th.made == [],
          "negative: venv ещё нет — 400 с подсказкой: первый старт vLLM-ячейки его создаёт")


def test_scout_and_routes():
    CHECKS.section("скаут и его маршруты:")
    s = make_scout()
    check(s.vllm.venv == Path.home() / "vllm-venv",
          "venv — ~/vllm-venv: тот, что создаёт и запускает строка vLLM-ячейки ($HOME/vllm-venv)")
    check(s.vllm.history_file == s.state.path.parent / "vllm-versions.json",
          "история — рядом с state.json, как сохранённые конфиги")
    s.vllm = fresh()
    with Served(s) as srv:
        code, body = srv.get("/api/vllm")
        code2, body2 = srv.get("/api/vllm/update-status")
        code3, body3 = srv.post("/api/vllm/update", {"version": "latest"})
    check(code == 200 and body.get("version") == "0.24.0" and body.get("installed") is True,
          "GET /api/vllm — версия и история машины")
    check(code2 == 200 and body2.get("lines") == [] and body2.get("running") is False,
          "GET /api/vllm/update-status — задание целиком, со строками")
    check(code3 == 400 and "not a vLLM version" in str(body3.get("error")),
          "POST /api/vllm/update — отказ приходит кодом и словами")


for test in (test_version, test_history, test_info, test_update, test_scout_and_routes):
    test()
sys.exit(CHECKS.finish())
