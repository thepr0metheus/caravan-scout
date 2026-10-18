#!/usr/bin/env python3
"""A start the card cannot hold is refused before it runs (2.7).

vLLM reserves GPU_MEMORY_UTILIZATION × the card the moment it starts; when
that much is not free it dies in a crash loop a minute later. The controller
refused such a start on its own machine; a scout's cell had no check. The
controller now sends what a start reserves (`vram` {device, reserveMiB, who,
why, lower} — the runner's rule is the controller's), and the scout checks
its own free memory at launch — also at boot, where two autostart cells may
want one card.

Pinned by value with a fake nvidia-smi: the refusal and its words, who holds
the card (the cells and the processes ON THAT CARD, by what nvidia-smi says,
2.33), a card too small for the reserve, another card with room, what passes,
what is not checked at all, and both ways a start arrives (HTTP and autostart).

Run: python3 scripts/test_scout_vram.py
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks, FakeRun, Served, make_scout, patched  # noqa: E402

from caravan_scout.starts import CellStart  # noqa: E402

CHECKS = Checks("scout vram")
check = CHECKS.check

VRAM = {"device": 0, "reserveMiB": 29491, "who": "vLLM", "why": "utilization 0.90 × 32.0 GiB",
        "lower": "GPU_MEMORY_UTILIZATION"}
START = {"cellKind": "command", "port": 22012, "command": "$HOME/vllm-venv/bin/vllm serve org/model",
         "shellLine": "set -euo pipefail; exec true", "healthPath": "/v1/models",
         "config": {"RUNNER": "vllm", "VLLM_MODEL": "org/model", "PORT": 22012}, "vram": VRAM}
SMI = ("nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,memory.free,"
       "utilization.gpu,temperature.gpu,power.draw,uuid", "--format=csv,noheader,nounits")
APPS = ("nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory", "--format=csv,noheader,nounits")


def smi(*free_by_card, totals=None, apps=None):
    """A fake nvidia-smi: the cards (their free memory, their total — 32 GiB each unless `totals` says) and, when
    `apps` is given, who holds what, [(card, pid, executable, MiB)]; without it the compute-apps query fails."""
    totals = totals or [32768] * len(free_by_card)
    rows = "".join(f"{i}, RTX, {totals[i]}, {totals[i] - f}, {f}, 3, 40, 30.1, GPU-{i}\n" for i, f in enumerate(free_by_card))
    table = {SMI: (0, rows)}
    if apps is not None:
        table[APPS] = (0, "".join(f"GPU-{card}, {pid}, /usr/bin/{name}, {mib}\n" for card, pid, name, mib in apps))
    return FakeRun(table)


class Running:
    def __init__(self, pid=7):
        self.pid = pid

    def status(self):
        return {"running": True, "pid": self.pid}


class Stopped:
    """A cell that is not running, with the pid its record still names."""

    def __init__(self, pid):
        self.pid = pid

    def status(self):
        return {"running": False, "pid": self.pid}


class Tree:
    """The processes a process started: {pid: [children]}, as the scout reads them from /proc."""

    def __init__(self, kids=None):
        self.kids = kids or {}

    def descendants(self, pid):
        return list(self.kids.get(pid, []))


def scout_with(cells, kids=None, stopped=None):
    """A scout whose cells run: {port: pid}, those that do not: {port: the pid their record names}, and the processes
    the running ones started: {pid: [children]}. A new one for every question: the machine keeps what nvidia-smi said
    for 5 s (who holds what) and 10 s (the cards)."""
    s = make_scout()
    for port, pid in cells.items():
        s.cells.at(port).process = Running(pid)
    for port, pid in (stopped or {}).items():
        s.cells.at(port).process = Stopped(pid)
    s.cells.processes = Tree(kids)
    return s


def refused(scout, run, payload=None):
    with patched(subprocess, run=run):
        return CellStart.of(scout.cells, dict(payload or START)).short_of_vram(22012)


def test_the_refusal():
    CHECKS.section("не помещается — отказ до запуска:")
    s = make_scout()
    s.cells.at(22010).process = Running()
    s.cells.at(22024).process = Running()
    got = refused(s, smi(4000))
    check(got == {"ok": False, "error": "vLLM wants 28.8 GiB reserved (utilization 0.90 × 32.0 GiB) but only 3.9 GiB "
                                        "VRAM is free on GPU 0 — lower GPU_MEMORY_UTILIZATION"},
          f"резерв больше свободного — отказ словами проверки контроллера, с картой; nvidia-smi не сказал, кто что держит, — "
          f"ячейки не названы (раньше называлась каждая работающая, в том числе стоящая на другой карте) (got {got})")
    got = refused(make_scout(), smi(4000))
    check(got["error"].endswith("VRAM is free on GPU 0 — lower GPU_MEMORY_UTILIZATION"),
          "никто не держит карту — совет один: убавить долю")
    got = refused(make_scout(), smi(32000, 1000), {**START, "vram": {**VRAM, "device": 1, "reserveMiB": 20000}})
    check(got is not None and "on GPU 1" in got["error"], "смотрится та карта, что названа в резерве, а не первая")


def test_who_is_in_the_way():
    CHECKS.section("кто мешает — те, кто держит память на ЭТОЙ карте:")
    want = "vLLM wants 28.8 GiB reserved (utilization 0.90 × 32.0 GiB) but only 3.9 GiB VRAM is free on GPU 0"
    # Card 0 is nearly full: :22010 holds 5.9 GiB with its own process, :22024 holds 21.5 GiB through a child of its server,
    # ollama (no cell) holds 100 MiB of a context. :22011 holds memory on card 1 only. Card 1 has 30 GiB free.
    apps = [(0, 11, "llama-server", 6000), (0, 12, "python3", 2000), (0, 120, "python3", 20000), (1, 13, "llama-server", 1000), (0, 999, "ollama", 100)]
    got = refused(scout_with({22010: 11, 22024: 12, 22011: 13}, {12: [120]}), smi(4000, 31000, apps=apps))
    check(got["error"] == want + " — stop :22024 (21.5 GiB), :22010 (5.9 GiB) or lower GPU_MEMORY_UTILIZATION, or pick GPU 1 (30.3 GiB free)",
          "названы только держатели этой карты, с тем, сколько кто держит, самый большой первым; ячейка :22024 держит и сама, и через дочерний "
          f"процесс своего сервера — оба куска сложены (2 + 20 ГиБ-кусков = 21.5); :22011 с другой карты и 100 МиБ чужого контекста не названы; "
          f"рядом — карта, где место есть (got {got})")

    got = refused(scout_with({22011: 13}, stopped={22010: 999}), smi(4000, 31000, apps=[(0, 999, "ollama", 6300), (1, 13, "llama-server", 1000)]))
    check(got["error"] == want + " — stop ollama (6.2 GiB) or lower GPU_MEMORY_UTILIZATION, or pick GPU 1 (30.3 GiB free)",
          f"то, что не ячейка скаута, названо по исполняемому файлу — как его зовёт доска; negative: остановленная ячейка, чей старый pid "
          f"совпал с чужим процессом, держателем не называется (got {got})")

    got = refused(scout_with({22012: 14, 22010: 11}), smi(4000, apps=[(0, 14, "python3", 5000), (0, 11, "llama-server", 3000)]))
    check(got["error"] == want + " — stop :22010 (2.9 GiB) or lower GPU_MEMORY_UTILIZATION",
          f"negative: ячейка, которую запускают, уже держит свой кусок (перезапуск) — её не называют, ей это не помеха; "
          f"negative: рядом нет карты с местом — совета «другая карта» нет (got {got})")

    got = refused(scout_with({22010: 11}), smi(4000, 4000, apps=[(0, 11, "llama-server", 3000)]))
    check("or pick GPU" not in got["error"], "negative: на другой карте места тоже нет — на неё не посылают")
    got = refused(scout_with({22010: 11}), smi(4000, 29491, apps=[(0, 11, "llama-server", 3000)]))
    check(got["error"].endswith("or pick GPU 1 (28.8 GiB free)"),
          f"boundary: на другой карте свободно ровно столько, сколько нужно, — места хватает, на неё посылают (получено {got['error'][-45:]})")
    got = refused(scout_with({22010: 11}), smi(4000, 29490, apps=[(0, 11, "llama-server", 3000)]))
    check("or pick GPU" not in got["error"], "boundary: на другой карте не хватает одного МиБ — не посылают")
    got = refused(scout_with({22010: 11, 22011: 12, 22013: 13}), smi(4000, apps=[(0, 11, "llama-server", 256), (0, 12, "llama-server", 255), (0, 13, "llama-server", 1000)]))
    check(got["error"].endswith("— stop :22013 (1.0 GiB), :22010 (0.2 GiB) or lower GPU_MEMORY_UTILIZATION"),
          f"boundary: держатель в 256 МиБ назван, в 255 — нет (получено {got['error'][-80:]})")
    got = refused(scout_with({22010: 11}), smi(4000, 30000, 31000, apps=[(0, 11, "llama-server", 3000)]))
    check(got["error"].endswith("or pick GPU 2 (30.3 GiB free)"),
          f"из карт с местом названа та, где его больше (получено {got['error'][-60:]})")

    got = refused(scout_with({22010: 11}), smi(31000, 3000, apps=[(1, 11, "llama-server", 1000)]),
                  {**START, "vram": {**VRAM, "device": 1}})
    check(got["error"] == "vLLM wants 28.8 GiB reserved (utilization 0.90 × 32.0 GiB) but only 2.9 GiB VRAM is free on GPU 1 "
                          "— stop :22010 (1.0 GiB) or lower GPU_MEMORY_UTILIZATION, or pick GPU 0 (30.3 GiB free)",
          f"карта 1: её держатели и совет про карту 0 — смотрится та карта, что названа в резерве (got {got})")


def test_a_card_too_small():
    CHECKS.section("карта мала для резерва — совет не «остановить ячейки»:")
    got = refused(scout_with({22010: 11}), smi(24000, 30000, totals=[24576, 32768], apps=[(0, 11, "llama-server", 500)]))
    check(got["error"] == "vLLM wants 28.8 GiB reserved (utilization 0.90 × 32.0 GiB) but GPU 0 has only 24.0 GiB in all — "
                          "lower GPU_MEMORY_UTILIZATION, or pick GPU 1 (29.3 GiB free)",
          f"резерв больше всей карты (24 ГиБ при 28.8): остановка ячеек не поможет — сказано «карта мала», ячейки не названы, "
          f"названа карта, где место есть (got {got})")
    got = refused(scout_with({22010: 11}), smi(24000, 24000, totals=[24576, 24576]))
    check(got["error"].endswith("has only 24.0 GiB in all — lower GPU_MEMORY_UTILIZATION"),
          "negative: ни на одной карте места нет — совета про другую карту нет")
    got = refused(scout_with({22010: 11}), smi(24000, totals=[24576]))
    check(got["error"].endswith("has only 24.0 GiB in all — lower GPU_MEMORY_UTILIZATION"), "у машины одна карта — то же, без совета про другую")
    got = refused(scout_with({22010: 11}), smi(1000, totals=[29491]))
    check("in all" not in got["error"] and "only 1.0 GiB VRAM is free on GPU 0" in got["error"],
          f"boundary: резерв ровно равен всей карте — карта не мала, она занята: остановить ячейки поможет (got {got['error'][:110]})")


def test_what_passes():
    CHECKS.section("что проходит:")
    check(refused(make_scout(), smi(31000)) is None, "negative: помещается — старт идёт")
    run = smi(100)
    check(refused(make_scout(), run, {k: v for k, v in START.items() if k != "vram"}) is None and run.calls == [],
          "negative: старт ничего не резервирует (llama.cpp, whisper, контроллер старше) — nvidia-smi даже не спрашивают")
    check(refused(make_scout(), FakeRun({})) is None,
          "negative: nvidia-smi нет — не мешаем, как и проверка контроллера")
    check(refused(make_scout(), smi(100), {**START, "vram": {**VRAM, "device": 3}}) is None,
          "negative: такой карты у машины нет — не выдумываем, пропускаем")
    check(refused(make_scout(), smi(100), {**START, "vram": {**VRAM, "reserveMiB": "много"}}) is None,
          "negative: резерв не числом — пропускаем, а не падаем")


def test_both_ways_in():
    CHECKS.section("оба пути старта:")
    s = make_scout()
    spawned = []
    with patched(subprocess, run=smi(4000), Popen=lambda *a, **k: spawned.append(a)), Served(s) as srv:
        code, body = srv.post("/api/llama-node/start", START)
    check(code == 400 and body.get("error", "").startswith("vLLM wants 28.8 GiB reserved") and spawned == [],
          "POST /api/llama-node/start — 400 с причиной, процесс не запускался")
    s = make_scout()
    s.autostart.set(22012, True, dict(START))
    out = io.StringIO()
    with patched(s.machine, boot_id=lambda: "boot-1"), patched(subprocess, run=smi(4000)), \
            contextlib.redirect_stdout(out):
        started = s.autostart.start_all()
    check(started == [] and "[autostart] :22012 did not start: vLLM wants 28.8 GiB reserved" in out.getvalue(),
          "автозапуск при загрузке: не помещается — не запущена, причина в журнале скаута")


for fn in (test_the_refusal, test_who_is_in_the_way, test_a_card_too_small, test_what_passes, test_both_ways_in):
    fn()

sys.exit(CHECKS.finish())
