#!/usr/bin/env python3
"""Watchdog: a crashed cell comes back, as systemd brings the controller's (2.5).

The controller's cells are systemd units with Restart=on-failure: a crash is
followed by the same start 10 s later, at most 3 times in 10 minutes, and the
board shows how many times (💥). A scout's cell stayed down, with the reason
only. The Watchdog relaunches the cell the same way (CellProcess.relaunch —
the launch the cell's record keeps, also after a scout restart) and keeps the
crash note the board shows.

Pinned by value with a fake process and a clock the test moves: when it
restarts and when it gives up, what a clean exit and a failed relaunch do,
what a start by hand clears, what the card reads, and the relaunch itself.

Run: python3 scripts/test_scout_watchdog.py
"""
import contextlib
import io
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import ROOT, TMP, Checks, make_scout, patched  # noqa: E402

from caravan_scout.process import CellProcess  # noqa: E402
from caravan_scout.starts import CellStart  # noqa: E402
from caravan_scout.watchdog import Watchdog  # noqa: E402

CHECKS = Checks("scout watchdog")
check = CHECKS.check


class FakeProcess:
    """A cell's process whose life the test writes: running, crashed or
    stopped cleanly; relaunch() is written down and makes it run."""

    def __init__(self, relaunch_ok=True):
        self.state = {"running": True, "pid": 11}
        self.relaunches = 0
        self.relaunch_ok = relaunch_ok
        self.log = ""

    def changed_since(self, started_at):
        return []

    def crash(self, reason="CUDA error: out of memory", code=1, log=""):
        self.state = {"running": False, "exitCode": code, "lastError": reason, "crashed": code != 0}
        self.log = log

    def log_tail(self):
        return self.log

    def status(self):
        return dict(self.state)

    def relaunch(self):
        self.relaunches += 1
        if not self.relaunch_ok:
            return {"ok": False, "error": "llama-server binary not found"}
        self.state = {"running": True, "pid": 100 + self.relaunches}
        self.log = "a new run"
        return {"ok": True, "pid": 100 + self.relaunches}

    def held_files(self):
        return []


class Told:
    """The machine's CrashSuspect, written down: the words of each crash."""

    def __init__(self):
        self.words = []

    def crashed(self, words):
        self.words.append(words)


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def rig(relaunch_ok=True):
    s = make_scout()
    # What listens on the machine, written by the test; None is "the machine will not say" (the rule of a cell that never gets ready
    # is then silent, and the older pins below are about the other rule).
    s.listening = {"ports": None}
    s.cells.machine.listening_ports = lambda: s.listening["ports"]
    cell = s.cells.at(22021)
    proc = FakeProcess(relaunch_ok)
    cell.process = proc
    clock = Clock()
    dog = Watchdog(s.cells, Told(), clock=clock)
    return s, cell, proc, clock, dog


def ticks(dog, out=None):
    with contextlib.redirect_stdout(out or io.StringIO()):
        dog.tick()


def test_restart_after_ten_seconds():
    CHECKS.section("упала — через 10 с тот же запуск:")
    s, cell, proc, clock, dog = rig()
    ticks(dog)
    check(proc.relaunches == 0 and cell.crash is None, "работает — ничего")
    proc.crash()
    out = io.StringIO()
    ticks(dog, out)
    clock.now += 9
    ticks(dog)
    check(proc.relaunches == 0 and cell.crash["count"] == 1 and cell.crash["due"] == 1_700_000_010.0,
          "упала — отмечено (1 раз), перезапуск назначен через 10 с и не раньше")
    check("[watchdog] :22021 crashed (CUDA error: out of memory) — restarting in 10 s" in out.getvalue(),
          "журнал: причина и когда")
    clock.now += 1
    ticks(dog)
    ticks(dog)
    check(proc.relaunches == 1 and cell.crash["due"] is None and cell.crash["count"] == 1,
          "на 10-й секунде — перезапуск, один; снова работает — счёт остаётся (с тех пор, как тронули руками)")


def test_gives_up_after_three():
    CHECKS.section("три раза за 10 минут — дальше не поднимает:")
    s, cell, proc, clock, dog = rig()
    out = io.StringIO()
    for _ in range(3):
        proc.crash("GGML_ASSERT failed")
        ticks(dog, out)
        clock.now += 10
        ticks(dog, out)
        clock.now += 30
    check(proc.relaunches == 3 and cell.crash["count"] == 3, "три падения — три перезапуска")
    proc.crash("GGML_ASSERT failed")
    ticks(dog, out)
    clock.now += 60
    ticks(dog, out)
    check(proc.relaunches == 3 and cell.crash.get("gaveUp") is True and cell.crash["count"] == 4,
          "четвёртое за 10 минут — не поднимает (как StartLimitBurst=3 у systemd), лежит и говорит почему")
    check("[watchdog] :22021 crashed 4 times in 10 minutes — not restarting it: GGML_ASSERT failed" in out.getvalue(),
          "журнал говорит, почему больше не поднимает")
    view = s.cells.view(cell)
    check(view.get("crash") == {"count": 4, "at": Watchdog.when(clock.now - 60), "reason": "GGML_ASSERT failed",
                                "gaveUp": True} and view.get("phase") == "error",
          "карточка читает: сколько раз, когда последний, причина, и что больше не поднимается; фаза error")
    check(view.get("lastError") == "crashed 4 times in 10 minutes — not restarting it: GGML_ASSERT failed",
          "и ошибка на карточке говорит, что сторож сдался и почему — не просто последняя строка процесса")
    s, cell, proc, clock, dog = rig()
    for _ in range(5):
        proc.crash()
        ticks(dog)
        clock.now += 10
        ticks(dog)
        clock.now += 300
    check(proc.relaunches == 5 and not cell.crash.get("gaveUp"),
          "negative: падения раз в 5 минут — окно 10 минут сдвигается, поднимает каждый раз")


def test_the_last_lines():
    CHECKS.section("последние строки лога — в заметке:")
    s, cell, proc, clock, dog = rig()
    proc.crash(log="E load: tensor data is not within the file bounds\nE main: exiting")
    ticks(dog)
    check(cell.crash.get("tail") == "E load: tensor data is not within the file bounds\nE main: exiting",
          "падение — заметка берёт последние строки лога упавшего запуска")
    clock.now += 10
    ticks(dog)
    with patched(s.cells.probe, metrics=lambda port: {}), \
            patched(s.cells.machine, firewall=lambda port: {}, listening_ports=lambda: {22021}, processes=lambda: None):
        view = s.cells.view(cell)
    check(proc.relaunches == 1 and (view.get("crash") or {}).get("tail") == "E load: tensor data is not within the file bounds\nE main: exiting",
          "перезапуск отодвинул лог, а строки в заметке — всё ещё того падения; карточка их читает")
    s, cell, proc, clock, dog = rig()
    proc.crash(log="")
    ticks(dog)
    check("tail" not in (s.cells.view(cell).get("crash") or {"tail": "no note at all"}),
          "negative: лог пуст — поля нет, а не пустая строка под 💥")


def test_the_suspect_is_told():
    CHECKS.section("о падении узнаёт подозрение на сборку:")
    s, cell, proc, clock, dog = rig()
    proc.crash("CUDA error: an illegal memory access", log="E ggml_cuda: CUDA error\nE main: exiting")
    ticks(dog)
    clock.now += 10
    ticks(dog)
    ticks(dog)
    check(dog.suspect.words == ["CUDA error: an illegal memory access\nexited (code 1)\nE ggml_cuda: CUDA error\nE main: exiting"],
          "каждое падение — один раз, словами причины, того, как кончился процесс, и последних строк: по ним решают, "
          "смерть ли это движка")
    s, cell, proc, clock, dog = rig(relaunch_ok=False)
    proc.crash()
    for _ in range(4):
        ticks(dog)
        clock.now += 10
    check(len(dog.suspect.words) == 1,
          "negative: неудачный перезапуск — не новое падение движка: сборка тут ни при чём")


def test_a_signal_by_its_name():
    CHECKS.section("сигнал — по имени:")
    s, cell, proc, clock, dog = rig()
    proc.crash("", code=-11)
    ticks(dog)
    check(cell.crash["reason"] == "died of SIGSEGV" and "died of SIGSEGV" in dog.suspect.words[0],
          "лог молчит, процесс убит сигналом 11 — причина «died of SIGSEGV», а не «exited (code -11)»; "
          "подозрение на сборку её видит")
    s, cell, proc, clock, dog = rig()
    proc.crash("E ggml: something went wrong", code=-6)
    ticks(dog)
    check(cell.crash["reason"] == "E ggml: something went wrong" and "died of SIGABRT" in dog.suspect.words[0],
          "в логе есть строка — она причина на карточке, а сигнал всё равно доходит до подозрения")
    check(Watchdog.how({"exitCode": 1}) == "exited (code 1)" and Watchdog.how({"exitCode": -999}) == "exited (code -999)",
          "negative: обычный код — как был; неизвестный номер сигнала — числом, без выдумок")
    check(Watchdog.how({"exitCode": None}) == "ended with an unknown exit code (adopted after a scout restart)",
          "defect-history: код усыновлённой ячейки неизвестен — так и сказано словами, а не «exited (code None)»")


def test_killed_from_outside_says_so():
    CHECKS.section("убита снаружи — так и сказано:")
    hint = Watchdog.KILLED
    check(hint.startswith(" (killed from outside") and "systemd-oomd" in hint and "OOM killer" in hint,
          "подсказка названа словами, а не прочитана из константы: убийцы по имени (systemd-oomd, убийца памяти ядра) — пустая подсказка "
          "оставила бы одно «died of SIGKILL», с которым владелец не знает, что делать")
    s, cell, proc, clock, dog = rig()
    proc.crash("[strata] still starting (461 s) - please wait ...", code=-9)
    ticks(dog)
    check(cell.crash["reason"] == "died of SIGKILL" + hint + " — its last words: [strata] still starting (461 s) - please wait ..."
          and "died of SIGKILL" in dog.suspect.words[0],
          "defect-history (2026-10-18): SIGKILL — процесс сам не ловит её, и последняя строка лога лишь последнее, что он сказал; причина на карточке "
          "начинается с сигнала и подсказки (systemd-oomd или убийца памяти ядра), строка лога идёт после; подозрение на сборку видит «died of SIGKILL»")
    s, cell, proc, clock, dog = rig()
    proc.crash("", code=-9)
    ticks(dog)
    check(cell.crash["reason"] == "died of SIGKILL" + hint, "лог молчит — одна подсказка, без «its last words» после неё")
    s, cell, proc, clock, dog = rig()
    proc.crash("E ggml: something went wrong", code=-6)
    ticks(dog)
    check(cell.crash["reason"] == "E ggml: something went wrong",
          "negative: SIGABRT, SIGSEGV — процесс сам себя прервал и сказал почему: причина — строка лога, как была")
    s, cell, proc, clock, dog = rig()
    proc.crash("E ggml: bad", code=1)
    ticks(dog)
    check(cell.crash["reason"] == "E ggml: bad", "negative: обычный код выхода — строка лога, как была")
    s, cell, proc, clock, dog = rig()
    proc.crash("[strata] still starting (9 s) - please wait ...", code=None)
    ticks(dog)
    check(cell.crash["reason"] == "[strata] still starting (9 s) - please wait ...",
          "negative: код неизвестен (усыновлённая ячейка) — не выдумываем, что это был SIGKILL")
    long = Watchdog.reason({"exitCode": -9, "lastError": "x" * 400})
    check(len(long) == 300 and long.startswith("died of SIGKILL"), "boundary: причина не длиннее 300 знаков")


def test_a_cell_that_never_gets_ready_is_not_restarted_forever():
    CHECKS.section("ячейка, что не доходит до готовности, не перезапускается вечно:")

    def die(s, proc, clock, dog, ready=False, minutes=5):
        """One run that lasts `minutes` and dies: it listens first when `ready`."""
        proc.state = {"running": True, "pid": 11}
        s.listening["ports"] = {22021} if ready else set()
        ticks(dog)
        clock.now += minutes * 60
        proc.crash("[strata] still starting (300 s) - please wait ...", code=-9)
        ticks(dog)
        clock.now += Watchdog.RESTART_SEC
        ticks(dog)

    s, cell, proc, clock, dog = rig()
    for _ in range(4):
        die(s, proc, clock, dog)
    check(cell.crash["gaveUp"] is True and cell.crash["neverReady"] is True and cell.crash["count"] == 4 and proc.relaunches == 3,
          "defect-history (2026-10-18): четыре запуска по пять минут, каждый убит, ни один не открыл порт — окно «3 за 10 минут» не набирается, "
          "но после трёх перезапусков, что не прижились, ячейка остаётся лежать (раньше круг шёл, пока владелец не нажал Stop)")
    check(cell.crash["reason"].startswith("died of SIGKILL"), "причина — настоящая, а не строка прогресса")
    got = s.cells.view(cell)
    check(got.get("lastError", "").startswith("never ready in 4 starts — not restarting it: died of SIGKILL"),
          "карточка говорит «never ready in 4 starts», а не «in 10 minutes», которых не было")
    public = Watchdog.public(cell.crash)
    check(public["gaveUp"] is True and public["neverReady"] is True, "контроллер читает оба признака")

    s, cell, proc, clock, dog = rig()
    for ready in (False, False, True, False, False, False):
        die(s, proc, clock, dog, ready=ready)
    check(cell.crash.get("gaveUp") is not True and proc.relaunches == 6,
          "negative: запуск, что дошёл до открытого порта, обнуляет счёт «не прижилось»: три провала, один живой, ещё три — не сдаётся")
    die(s, proc, clock, dog, ready=False)
    check(cell.crash.get("gaveUp") is True and cell.crash["neverReady"] is True,
          "четвёртый провал подряд после живого запуска — сдаётся")

    s, cell, proc, clock, dog = rig()
    for _ in range(6):
        proc.state = {"running": True, "pid": 11}
        ticks(dog)
        clock.now += 300
        proc.crash("x", code=-9)
        ticks(dog)
        clock.now += Watchdog.RESTART_SEC
        ticks(dog)
    check(cell.crash.get("gaveUp") is not True and proc.relaunches == 6,
          "negative: машина не говорит, что слушает (None) — правило не включается, а не считает каждый запуск негодным")

    s, cell, proc, clock, dog = rig()
    s.listening["ports"] = {22021}
    ticks(dog)
    check(cell.listened is True, "запуск, чей порт слушает, отмечен: он дошёл до готовности")
    proc.crash("x", code=-9)
    ticks(dog)
    check(cell.listened is None and cell.crash["unready"] == 0,
          "упала — признак снят. Запуск, что начат вручную после «сдался» (перезапуска нет), не должен наследовать «слушал» мёртвого: "
          "иначе он, не открыв порт, сошёл бы за готовый и обнулил счёт")
    proc.state = {"running": True, "pid": 50}
    ticks(dog)
    check(cell.listened is True and proc.relaunches == 0,
          "в десять секунд до перезапуска владелец сам запустил ячейку: она слушает — отмечено")
    proc.crash("x", code=-9)
    clock.now += Watchdog.RESTART_SEC
    ticks(dog)
    check(proc.relaunches == 1 and cell.listened is None,
          "перезапуск начинает новый запуск с чистым признаком: слушание того, что умерло перед ним, к нему не относится")

    s, cell, proc, clock, dog = rig()
    s.listening["ports"] = set()
    for _ in range(4):
        proc.crash("x", code=1)
        ticks(dog)
        clock.now += Watchdog.RESTART_SEC
        ticks(dog)
    check(cell.crash["gaveUp"] is True and cell.crash["neverReady"] is False
          and s.cells.view(cell)["lastError"].startswith("crashed 4 times in 10 minutes — not restarting it: x"),
          "negative: четыре падения за минуты — прежнее правило окна и его слова («in 10 minutes»), признак neverReady не ставится")


def test_the_card_knows_these_words():
    CHECKS.section("карточка контроллера узнаёт эти слова:")
    repo = ROOT.parent / "lama-caravan"
    if not repo.is_dir():
        print("  (репозитория контроллера рядом нет — сверка пропущена)")
        return
    js = repo / "static" / "js" / "topology-nodes.js"
    # The repo is here, so a missing file is a moved fact, not a missing
    # controller: say so instead of skipping (the memory limits' check skipped
    # like that for a night after the controller's unit went).
    check(js.exists(), f"у контроллера есть {js.relative_to(repo)} — слова есть с чем сверять")
    if not js.exists():
        return
    found = re.search(r"const SCOUT_EXIT_WORDS = /(.+)/;", js.read_text(encoding="utf-8"))
    words = re.compile(found.group(1)) if found else None
    said = [Watchdog.how({"exitCode": code}) for code in (-11, -6, 1, 137, -999, None)]
    check(words is not None and all(words.match(w) for w in said),
          f"всё, что говорит Watchdog.how, карточка называет «причины нет в логе», а не «Model loading failed» ({said})")
    check(words is not None and not words.match("E llama_model_load: error loading model"),
          "negative: строка лога — не эти слова")


def test_what_is_not_a_crash():
    CHECKS.section("что не падение:")
    s, cell, proc, clock, dog = rig()
    proc.crash("", code=0)
    ticks(dog)
    clock.now += 60
    ticks(dog)
    check(proc.relaunches == 0 and cell.crash is None, "negative: вышла с кодом 0 — не падение, не поднимает")
    s, cell, proc, clock, dog = rig(relaunch_ok=False)
    proc.crash()
    out = io.StringIO()
    for _ in range(6):
        ticks(dog, out)
        clock.now += 10
    check(proc.relaunches == 3 and cell.crash.get("gaveUp") is True
          and cell.crash["reason"] == "llama-server binary not found",
          "перезапуск не удался — это тоже падение: ещё попытки, после трёх — лежит с настоящей причиной")


def test_by_hand():
    CHECKS.section("руками:")
    s, cell, proc, clock, dog = rig()
    proc.crash()
    ticks(dog)
    check(cell.crash["count"] == 1, "было падение")
    class FakeStart:
        def __init__(self, port):
            self._port = port

        def port(self):
            return self._port

        def run(self):
            return {"ok": True, "port": self._port}
    with patched(CellStart, of=lambda cells, payload: FakeStart(payload["port"])):
        s.cells.start({"port": 22021, "config": {"PORT": 22021}})
        s.cells.start({"port": 22099, "config": {"PORT": 22099}})
    check(cell.crash is None, "старт руками обнуляет счёт — «с тех пор, как тронули руками», как NRestarts у systemd")
    check(22099 not in s.cells.by_port,
          "negative: старт на порту, где ячейки нет, слота ради счёта не заводит")
    s.cells.drop(22021)
    check(22021 not in s.cells.by_port, "стоп руками уносит ячейку — и её счёт с ней")


def test_relaunch_itself():
    CHECKS.section("сам перезапуск:")
    log = TMP / "logs" / "llama-server.22021.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    spawned = []

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def popen(argv, **kw):
        spawned.append((list(argv), kw))
        return Proc()
    p = CellProcess()
    p._cfg = {"port": 22021}
    p._launch = {"argv": ["/x/llama-server", "--port", "22021"], "extraEnv": {"LLAMA_MODELS_DIR": "/m"},
                 "log": str(log)}
    with patched(subprocess, Popen=popen):
        got = p.relaunch()
        again = p.relaunch()
    argv, kw = spawned[0]
    env = kw.get("env") or {}
    check(got == {"ok": True, "pid": 4321} and argv == ["/x/llama-server", "--port", "22021"]
          and env.get("CARAVAN_SCOUT_CELL") == "22021" and env.get("LLAMA_MODELS_DIR") == "/m"
          and getattr(kw.get("stdout"), "name", None) == str(log),
          "тот же argv, метка скаута с портом, окружение сверх обычного и тот же лог")
    check(again == {"ok": False, "error": "the cell is running"} and len(spawned) == 1,
          "negative: работает — второй раз не запускает")
    gone = TMP / "logs-gone" / "llama-server.22022.log"
    shutil.rmtree(gone.parent, ignore_errors=True)
    q = CellProcess()
    q._cfg = {"port": 22022}
    q._launch = {"argv": ["/x/llama-server", "--port", "22022"], "extraEnv": {}, "log": str(gone)}
    with patched(subprocess, Popen=popen):
        got = q.relaunch()
    check(got == {"ok": True, "pid": 4321} and gone.is_file()
          and getattr(spawned[-1][1].get("stdout"), "name", None) == str(gone),
          "папку лога убрали между запусками — перезапуск создаёт её снова, а не падает на открытии лога")
    bare = CellProcess()
    check(bare.relaunch() == {"ok": False, "error": "no launch to repeat — it was started before this scout kept one"},
          "negative: нечего повторить (запущена скаутом старше 2.5) — отказ с причиной")
    adopted = CellProcess()
    adopted.adopt(5555, {"port": 22021}, log_path=log, started_at=1,
                  launch={"argv": ["bash", "-lc", "exec x"], "extraEnv": {}, "log": str(log)})
    check(adopted.launch_spec() == {"argv": ["bash", "-lc", "exec x"], "extraEnv": {}, "log": str(log)},
          "усыновлённая после рестарта скаута ячейка помнит запуск из записи — её тоже можно поднять")


def test_the_launcher_runs_it():
    CHECKS.section("лаунчер держит сторожа:")
    app = (Path(__file__).resolve().parent.parent / "caravan_scout" / "app.py").read_text(encoding="utf-8")
    check("threading.Thread(target=agent.watchdog.run, daemon=True).start()" in app
          and app.find("agent.watchdog.run") < app.find("serve_forever()"),
          "сторож запущен в своём потоке до того, как откроется порт; negative: без него упавшая ячейка лежит")


for fn in (test_restart_after_ten_seconds, test_gives_up_after_three, test_the_last_lines, test_the_suspect_is_told,
           test_a_signal_by_its_name, test_killed_from_outside_says_so, test_a_cell_that_never_gets_ready_is_not_restarted_forever,
           test_the_card_knows_these_words, test_what_is_not_a_crash,
           test_by_hand,
           test_relaunch_itself, test_the_launcher_runs_it):
    fn()

sys.exit(CHECKS.finish())
