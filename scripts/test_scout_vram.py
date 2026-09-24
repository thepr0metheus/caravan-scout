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
the card, what passes, what is not checked at all, and both ways a start
arrives (HTTP and autostart).

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


def smi(*free_by_card):
    rows = "".join(f"{i}, RTX, 32768, {32768 - f}, {f}, 3, 40, 30.1, GPU-{i}\n" for i, f in enumerate(free_by_card))
    return FakeRun({SMI: (0, rows)})


class Running:
    def status(self):
        return {"running": True, "pid": 7}


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
                                        "VRAM is free on GPU 0 — stop :22010, :22024 or lower GPU_MEMORY_UTILIZATION"},
          f"резерв больше свободного — отказ словами проверки контроллера, с картой и работающими ячейками (got {got})")
    got = refused(make_scout(), smi(4000))
    check(got["error"].endswith("VRAM is free on GPU 0 — lower GPU_MEMORY_UTILIZATION"),
          "никто не держит карту — совет один: убавить долю")
    got = refused(make_scout(), smi(32000, 1000), {**START, "vram": {**VRAM, "device": 1, "reserveMiB": 20000}})
    check(got is not None and "on GPU 1" in got["error"], "смотрится та карта, что названа в резерве, а не первая")


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


for fn in (test_the_refusal, test_what_passes, test_both_ways_in):
    fn()

sys.exit(CHECKS.finish())
