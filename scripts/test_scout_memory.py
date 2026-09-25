#!/usr/bin/env python3
"""Memory limits: a scout's cell is capped like a cell of the controller (2.6).

The controller's cells are systemd units with MemoryHigh=70%, MemoryMax=80%
and MemorySwapMax=2G: a model that eats the RAM dies alone. A scout's cell
had no limit, and one runaway model could take the machine — with the scout,
the controller's routes and every other cell on it. On Linux with a user
systemd the scout now launches each cell in its own transient scope with the
same limits; elsewhere the cell runs as before, and the journal says so.

Pinned by value: the command a launch runs (start, command cell, relaunch
after a crash — once, not twice), what the probe answers and why, that it is
asked once, and that the limits are the controller's.

Run: python3 scripts/test_scout_memory.py
"""
import contextlib
import io
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import ROOT, TMP, Checks, FakeRun, patched  # noqa: E402

from caravan_scout import process as process_mod  # noqa: E402
from caravan_scout.process import CellProcess, MemoryScope  # noqa: E402

CHECKS = Checks("scout memory limits")
check = CHECKS.check

SCOPE = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
         "-p", "MemoryHigh=70%", "-p", "MemoryMax=80%", "-p", "MemorySwapMax=2G", "--"]


class Proc:
    pid = 4321

    def poll(self):
        return None


class Spawns:
    """subprocess.Popen, written down."""

    def __init__(self):
        self.argv = []

    def __call__(self, argv, **kw):
        self.argv.append(list(argv))
        return Proc()


def test_the_command():
    CHECKS.section("команда запуска:")
    with patched(MemoryScope, _usable=True):
        got = MemoryScope.wrap(["/x/llama-server", "--port", "22021"])
    check(got == [*SCOPE, "/x/llama-server", "--port", "22021"],
          "есть лимиты — команда уходит в свою область systemd с тремя лимитами, после «--» — как была")
    bare = ["/x/llama-server", "--port", "22021"]
    with patched(MemoryScope, _usable=False):
        got = MemoryScope.wrap(bare)
    check(got == bare and got is not bare, "negative: лимитов нет — команда как была (копия, не та же)")


def test_every_launch_goes_through_it():
    CHECKS.section("все три запуска:")
    binary = TMP / "bin" / "llama-server"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("")
    log = TMP / "logs" / "llama-server.22021.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    spawns = Spawns()
    with patched(MemoryScope, _usable=True), patched(subprocess, Popen=spawns):
        p = CellProcess()
        started = p.start(str(binary), ["--port", "22021"], {"port": 22021}, log_path=log)
        kept = p.launch_spec()
        p._proc = None
        again = p.relaunch()
        c = CellProcess()
        c.start_command("exec bash ~/run_whisper.sh $PORT", {"port": 22024})
    bare = [str(binary), "--port", "22021"]
    check(started.get("ok") is True and spawns.argv[0] == [*SCOPE, *bare],
          "llama-server — в области с лимитами")
    check(kept["argv"] == bare and p._cfg.get("cmd") == bare,
          "запись запуска и cmd на карточке — сама команда, без обёртки: обёртку добавляет каждый запуск")
    check(again.get("ok") is True and spawns.argv[1] == [*SCOPE, *bare],
          "перезапуск после падения — в области один раз; negative: обёртка из записи дала бы вторую поверх")
    check(spawns.argv[2] == [*SCOPE, "bash", "-lc", "exec bash ~/run_whisper.sh $PORT"],
          "ячейка-команда — тоже")
    spawns = Spawns()
    with patched(MemoryScope, _usable=False), patched(subprocess, Popen=spawns):
        CellProcess().start(str(binary), ["--port", "22021"], {"port": 22021})
    check(spawns.argv == [bare], "negative: лимитов нет — запуск как раньше")


def probe(platform="linux", which="/usr/bin/systemd-run", run=None):
    run = run or FakeRun({})
    with patched(sys, platform=platform), patched(shutil, which=lambda name: which), \
            patched(subprocess, run=run):
        return MemoryScope.probe(), run.calls


def test_the_probe():
    CHECKS.section("проба:")
    run = FakeRun({("systemd-run",): (0, "26203131904\n")})
    (ok, why), calls = probe(run=run)
    check(ok is True and why == "cells run in their own scope: MemoryHigh=70% MemoryMax=80% MemorySwapMax=2G "
                                "(MemoryMax 26.2 GB)",
          f"область даёт memory.max числом — лимиты есть, журнал называет их и потолок в ГБ (got {why!r})")
    check(calls == [[*SCOPE, "sh", "-c", MemoryScope.READ_LIMIT]],
          "проба запускает то же, что запуск ячейки, и читает memory.max своей же области")
    (ok, why), calls = probe(run=FakeRun({("systemd-run",): (0, "max\n")}))
    check(ok is False and why == "cells run without memory limits: a user scope gets none here (memory.max = max)",
          "negative (почему проба читает memory.max): systemd принял MemoryMax, а потолка нет — "
          "контроллер памяти не отдан пользователю; лимитов нет, и сказано почему")
    (ok, why), calls = probe(run=FakeRun({("systemd-run",): (1, "Failed to connect to bus: No medium found\n")}))
    check(ok is False and why == "cells run without memory limits: a user scope gets none here "
                                "(Failed to connect to bus: No medium found)",
          "negative: нет пользовательского systemd (скаут запущен руками из ssh) — лимитов нет, причина из systemd-run")
    (ok, why), calls = probe(run=FakeRun({("systemd-run",): subprocess.TimeoutExpired("systemd-run", 10)}))
    check(ok is False and why.startswith("cells run without memory limits: systemd-run --user did not answer ("),
          "negative: systemd-run не ответил за 10 с — лимитов нет")
    (ok, why), calls = probe(which=None)
    check(ok is False and why == "cells run without memory limits: systemd-run is not installed" and calls == [],
          "negative: systemd-run нет — не запускается ничего")
    (ok, why), calls = probe(platform="darwin")
    check(ok is False and why == "cells run without memory limits: the limits come from systemd, and this is not Linux"
          and calls == [],
          "negative: macOS — лимитов нет, так и сказано")


def test_asked_once():
    CHECKS.section("спрашивается один раз:")
    asked = []

    def answer():
        asked.append(1)
        return True, "cells run in their own scope: …"
    out = io.StringIO()
    with patched(MemoryScope, _usable=None, probe=staticmethod(answer)), contextlib.redirect_stdout(out):
        first, second = MemoryScope.usable(), MemoryScope.usable()
    check(first is True and second is True and len(asked) == 1,
          "первый запуск спрашивает, остальные берут ответ — проба не ходит в systemd на каждый старт")
    check(out.getvalue() == "[cells] cells run in their own scope: …\n",
          "ответ — одной строкой в журнал скаута, и только раз")


def test_the_limits():
    # The values came from the controller's cell unit, and this test compared
    # the two. The unit went in the controller's step 6.9 — and the comparison
    # then said "no controller next to us, skipped" with the controller right
    # there: an absence drawn as a pass. The scout is the values' one home now.
    CHECKS.section("лимиты — факт скаута (2.9.1):")
    check(MemoryScope.LIMITS == ("MemoryHigh=70%", "MemoryMax=80%", "MemorySwapMax=2G"),
          f"MemoryHigh 70 %, MemoryMax 80 %, своп 2 ГБ — те, что были у юнита ячеек контроллера (got {MemoryScope.LIMITS})")
    repo = ROOT.parent / "lama-caravan"
    if not repo.is_dir():
        print("  (репозитория контроллера рядом нет — его сторона не проверена)")
        return
    check(not (repo / "systemd" / "lama-cell@.service").exists(),
          "negative: юнита lama-cell@ у контроллера нет (ушёл в его шаге 6.9) — лимиты живут в одном месте, у скаута")


for fn in (test_the_command, test_every_launch_goes_through_it, test_the_probe, test_asked_once,
           test_the_limits):
    fn()

sys.exit(CHECKS.finish())
