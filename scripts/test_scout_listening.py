#!/usr/bin/env python3
"""Whether a running cell's port answers yet — said by the machine (2.7).

vLLM installs and loads for minutes before it listens; the controller saw a
running process and a silent port, and a silent port from the controller's
side looks the same as a firewall — the board drew the cell "running". The
scout asks its own OS which ports listen (`ss`, `lsof` on macOS) and says it
per running cell (`listening`); a cell that does not listen yet says its last
log lines (`startingTail`), where the start is.

Pinned by value with fake ss/lsof: the ports read on Linux and on macOS, the
unknown answer, the cache, and what a view carries in each case.

Run: python3 scripts/test_scout_listening.py
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import TMP, Checks, FakeRun, make_scout, patched  # noqa: E402

from caravan_scout.machine import Machine  # noqa: E402

CHECKS = Checks("scout listening")
check = CHECKS.check

SS = ("LISTEN 0 4096 0.0.0.0:8092 0.0.0.0:*\n"
      "LISTEN 0 511 *:22012 *:*\n"
      "LISTEN 0 128 [::]:22001 [::]:*\n"
      "LISTEN 0 128 127.0.0.53%lo:53 0.0.0.0:*\n")
LSOF = ("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
        "Python 501 me 5u IPv4 0x1 0t0 TCP *:8092 (LISTEN)\n"
        "whisper 777 me 7u IPv4 0x2 0t0 TCP 127.0.0.1:22024 (LISTEN)\n")


def test_the_ports():
    CHECKS.section("какие порты слушают:")
    with patched(subprocess, run=FakeRun({("ss",): (0, SS)})):
        got = Machine.listening_now()
    check(got == {8092, 22012, 22001, 53}, f"Linux: ss — IPv4, IPv6 и с интерфейсом (got {got})")
    with patched(subprocess, run=FakeRun({("lsof",): (0, LSOF)})):
        got = Machine.listening_now()
    check(got == {8092, 22024}, f"macOS: ss нет — lsof, заголовок не порт (got {got})")
    with patched(subprocess, run=FakeRun({})):
        check(Machine.listening_now() is None,
              "negative: ни ss, ни lsof — None: «не знаю», а не «ничего не слушает»")


def test_asked_once_per_two_seconds():
    CHECKS.section("спрашивается раз в 2 с на все ячейки:")
    s = make_scout()
    run = FakeRun({("ss",): (0, SS)})
    clock = [1000.0]
    with patched(subprocess, run=run), patched(time, time=lambda: clock[0]):
        s.machine.listening_ports()
        s.machine.listening_ports()
        clock[0] += 2.5
        s.machine.listening_ports()
    check(len(run.calls) == 2, f"второй вопрос в те же 2 с — из памяти; после — снова (got {len(run.calls)})")


class Running:
    def __init__(self, tail=""):
        self.tail = tail

    def status(self):
        return {"running": True, "pid": 9, "port": 22013}

    def log_tail(self):
        return self.tail


def view(listening, tail="[caravan] provisioning vLLM venv\nCollecting vllm==0.24.0"):
    s = make_scout()
    cell = s.cells.at(22013)
    cell.process = Running(tail)
    with patched(s.cells.probe, metrics=lambda port: {}), \
            patched(s.machine, firewall=lambda port: {}, listening_ports=lambda: listening):
        return s.cells.view(cell)


def test_the_view():
    CHECKS.section("что говорит работающая ячейка:")
    got = view({8092})
    check(got.get("listening") is False and got.get("startingTail") == "[caravan] provisioning vLLM venv\nCollecting vllm==0.24.0",
          "процесс жив, порт ещё не слушает — так и сказано, с последними строками лога: где сейчас старт")
    got = view({8092, 22013})
    check(got.get("listening") is True and "startingTail" not in got,
          "negative: слушает — строк лога нет, отчёт не пухнет")
    got = view(None)
    check("listening" not in got and "startingTail" not in got,
          "negative: машина не говорит, что слушает, — поля нет вовсе, а не «не слушает»")


for fn in (test_the_ports, test_asked_once_per_two_seconds, test_the_view):
    fn()

sys.exit(CHECKS.finish())
