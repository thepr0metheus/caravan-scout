#!/usr/bin/env python3
"""Снимок значениями: какие файлы запуска ячейки изменились на диске после её старта (2.11).

Работающий процесс держит открытые файлы (их inode), а не имена: файл,
заменённый под ним, доходит до ячейки только после перезапуска, и доска
говорит об этом чипом ⟳. Контроллер мерил это для своих ячеек по времени
старта юнита; с его шага 6.9 все ячейки — скаутов, и файлы лежат на машине
скаута. Скаут говорит роли: модель, проектор, черновая модель.

Запуск: python3 scripts/test_scout_launch_files.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import TMP, Checks, make_scout, patched  # noqa: E402

from caravan_scout.process import CellProcess  # noqa: E402

CHECKS = Checks("scout launch files")
check = CHECKS.check

START = 1_700_000_000


def files(**mtimes):
    """Files in a temp folder with the given mtimes (None: not created)."""
    root = Path(tempfile.mkdtemp(prefix="launch-", dir=TMP))
    out = {}
    for name, mtime in mtimes.items():
        path = root / name
        if mtime is not None:
            path.write_bytes(b"x")
            os.utime(path, (mtime, mtime))
        out[name] = str(path)
    return root, out


def held(model="", mmproj="", spec=""):
    proc = CellProcess()
    proc._cfg = {"modelPath": model, "mmprojPath": mmproj, "specPath": spec, "port": 22031}
    return proc


def test_files():
    CHECKS.section("файлы:")
    _root, f = files(model=START + 10, mmproj=START - 10, draft=START + 5)
    check(held(f["model"], f["mmproj"], f["draft"]).changed_since(START) == ["model", "draft"],
          "изменились после старта модель и черновая — они и названы, по ролям и в их порядке")
    check(held(f["mmproj"]).changed_since(START) == [],
          "negative: файл старше старта — не изменился")
    check(held(f["model"]).changed_since(START + 10) == [],
          "boundary: mtime ровно в секунду старта — не «после»")
    _root, g = files(gone=None)
    check(held(g["gone"]).changed_since(START) == [],
          "negative: файл, который не прочитать, — не «изменился»: молчание, а не догадка")
    check([held(f["model"]).changed_since(0), held(f["model"]).changed_since(None)] == [[], []],
          "negative: времени старта нет — нечего сравнивать")


def test_a_checkpoint_folder():
    CHECKS.section("папка (чекпойнт):")
    root = Path(tempfile.mkdtemp(prefix="ckpt-", dir=TMP))
    for name, mtime in (("a.safetensors", START - 50), ("b.safetensors", START + 30)):
        (root / name).write_bytes(b"x")
        os.utime(root / name, (mtime, mtime))
    os.utime(root, (START - 100, START - 100))
    check(held(str(root)).changed_since(START) == ["model"],
          "папка изменилась, когда изменился любой файл в ней — по самому новому, а не по дате папки")
    os.utime(root / "b.safetensors", (START - 1, START - 1))
    check(held(str(root)).changed_since(START) == [],
          "negative: все файлы старше старта — папка не изменилась")
    empty = Path(tempfile.mkdtemp(prefix="empty-", dir=TMP))
    os.utime(empty, (START + 1, START + 1))
    check(held(str(empty)).changed_since(START) == ["model"], "boundary: пустая папка — по своей дате")


def test_on_the_card():
    CHECKS.section("в отчёте работающей ячейки:")
    _root, f = files(model=START + 10)
    scout = make_scout()
    proc = held(f["model"])
    with patched(proc, status=lambda: {"running": True, "pid": 7, "startedAt": START}), \
            patched(scout.cells.probe, metrics=lambda port: {}), \
            patched(scout.machine, firewall=lambda port: {}, listening_ports=lambda: None):
        cell = scout.cells.at(22031)
        cell.process = proc
        view = scout.cells.view(cell)
    check(view.get("launchDiskNewer") == ["model"],
          f"работающая ячейка говорит, какие её файлы новее старта (got {view.get('launchDiskNewer')})")
    stopped = scout.cells.at(22032)
    with patched(scout.cells.probe, metrics=lambda port: {}):
        quiet = scout.cells.view(stopped)
    check("launchDiskNewer" not in quiet, "negative: у стоящей ячейки поля нет — она ничего не держит")


test_files()
test_a_checkpoint_folder()
test_on_the_card()

sys.exit(CHECKS.finish())
