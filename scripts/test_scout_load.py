#!/usr/bin/env python3
"""How far a loading cell has read its model, how fast, and of how much (2.43).

The owner, 2026-10-26, shown the copy of a model with its speed: "and at what speed it loads into memory — do it, I want it to
work". A copy is the scout's own and it counts its bytes; a load is the cell's process reading its files. The kernel counts that:
/proc/<pid>/io says what a process read by calls (rchar — a model read into memory, from disk or from the page cache) and what it
fetched from storage (read_bytes — the pages of a mapped model, llama.cpp's default, count only there). The scout says the larger
of the two for each process of the cell, summed; how fast over the last seconds (CopyPace, kept per run); and what its model files
weigh, measured when its start had them — what the board counts the reading against.

Pinned here, each with its negative: the counts of the processes (the larger of the two for each, summed; one that says nothing
left out; none that says anything — None, not 0); the weight of the files (a file, every part of a split GGUF, a folder, a file
that is not there left out, nothing measured — None); the pace of a run (none before two seconds, a new process starts again,
nothing read — None). The view of a running cell that carries the three is pinned in test_scout_cells.py, with its rig.

Run: python3 scripts/test_scout_load.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import TMP, Checks, make_scout, patched  # noqa: E402

from caravan_scout.cells import RunPace  # noqa: E402
from caravan_scout.models import ModelBytes  # noqa: E402
from caravan_scout.process import HostProcesses  # noqa: E402

CHECKS = Checks("cell load")
check = CHECKS.check
GIB = 2 ** 30


def proc_with(rows):
    """A stand-in /proc: {pid: io text}."""
    root = TMP / f"proc-{len(list(TMP.glob('proc-*')))}"
    for pid, text in rows.items():
        (root / str(pid)).mkdir(parents=True, exist_ok=True)
        (root / str(pid) / "io").write_text(text, encoding="ascii")
    return root


def io(rchar, read_bytes):
    return f"rchar: {rchar}\nwchar: 9\nsyscr: 1\nsyscw: 1\nread_bytes: {read_bytes}\nwrite_bytes: 0\ncancelled_write_bytes: 0\n"


def test_bytes_read():
    CHECKS.section("what a cell's processes have read:")
    proc = proc_with({100: io(5 * GIB, 1 * GIB), 101: io(3 * 2 ** 20, 40 * GIB), 102: "rchar: x\nread_bytes: y\n"})
    with patched(HostProcesses, PROC=proc):
        both = HostProcesses.bytes_read([100, 101])
        alone = HostProcesses.bytes_read([101])
        silent = HostProcesses.bytes_read([102, 999, None, "x"])
        none = HostProcesses.bytes_read([])
    check(both == 5 * GIB + 40 * GIB,
          "for each process the larger of its two counts, summed: a model read by calls (rchar 5 GiB) and one mapped and paged "
          f"in from disk (read_bytes 40 GiB) (got {both})")
    check(alone == 40 * GIB, "negative: a mapped load's few megabytes of calls do not hide what it fetched from storage")
    check(silent is None and none is None,
          "negative: processes whose /proc says nothing (gone, junk, not a pid) and no process at all — None, not a 0 that "
          "would read as nothing loaded")


def test_model_bytes():
    CHECKS.section("what a start's model files weigh here:")
    root = TMP / "models"
    (root / "split").mkdir(parents=True, exist_ok=True)
    for i, size in ((1, 100), (2, 250), (3, 50)):
        (root / "split" / f"big-0000{i}-of-00003.gguf").write_bytes(b"\0" * size)
    (root / "one.gguf").write_bytes(b"\0" * 70)
    (root / "folder" / "sub").mkdir(parents=True, exist_ok=True)
    (root / "folder" / "a.safetensors").write_bytes(b"\0" * 30)
    (root / "folder" / "sub" / "b.json").write_bytes(b"\0" * 5)
    first = root / "split" / "big-00001-of-00003.gguf"
    check(ModelBytes.of([first]) == 400, "the first part of a split GGUF weighs every part: 100 + 250 + 50")
    check(ModelBytes.of([root / "one.gguf", "", None, root / "folder"]) == 105,
          "a file, a folder with everything under it, and empty names left out: 70 + 30 + 5")
    check(ModelBytes.of([root / "one.gguf", root / "missing.gguf"]) == 70, "a file that is not there is left out")
    check(ModelBytes.of([root / "missing.gguf"]) is None and ModelBytes.of([]) is None,
          "negative: nothing measured — None, not a 0 to divide by")


def test_run_pace():
    CHECKS.section("the pace of one run:")
    now = [0.0]
    pace = RunPace(clock=lambda: now[0])

    def at(t, pid, done):
        now[0] = t
        return pace.read(pid, done)

    check([at(0, 7, 0), at(1, 7, 300 * 2 ** 20)] == [None, None], "less than two seconds of the run — no pace yet")
    check(at(2, 7, 600 * 2 ** 20) == 300 * 2 ** 20, "two seconds: 300 MiB/s")
    check(at(3, 8, 900 * 2 ** 20) is None and at(5, 8, 1500 * 2 ** 20) == 300 * 2 ** 20,
          "a new process of the cell starts the window again: the old run's bytes are not its pace")
    check(at(6, 8, None) is None, "negative: nothing read said — no pace")


for test in (test_bytes_read, test_model_bytes, test_run_pace):
    test()
sys.exit(CHECKS.finish())
