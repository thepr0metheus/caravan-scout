#!/usr/bin/env python3
"""Снимок значениями: id машины на доске держится, имя следует за машиной (2.10).

id был hostname'ом, прочитанным при каждом старте: машина, переименованная
через hostnamectl, приходила новым хостом, а всё, что доска держит под
старым id, — ячейки с расписаниями и автозапуском, расписание питания,
запись клиента той же машины — оставалось у машины, которая больше не
отчитывается. Теперь id, которым скаут назвался, прибит в state.json, и
при следующем старте прибитый побеждает hostname: переименование меняет
только имя, которое показывает доска. Первый старт после обновления
прибивает тот id, которым скаут называется сейчас, — ни одна машина не
становится новым хостом от обновления. hostId в config.json — выбор
оператора: побеждает и становится прибитым.

Запуск: python3 scripts/test_scout_identity.py
"""
from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import TMP, Checks, patched  # noqa: E402

from caravan_scout.paths import DEFAULT_CONFIG  # noqa: E402
from caravan_scout.scout import Scout  # noqa: E402

CHECKS = Checks("scout identity")
check = CHECKS.check


def home(config=None, state=None):
    """A scout's folder: config.json without hostId and displayName unless
    given — what every machine of the fleet has — and state.json if given."""
    root = Path(tempfile.mkdtemp(prefix="scout-", dir=TMP))
    cfg = {"listenHost": "127.0.0.1", "listenPort": 18092, "controllerUrl": "",
           "modelsBasePath": str(root / "models"), **(config or {})}
    (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    if state is not None:
        (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return root


def start(root, hostname):
    """Start a scout there on a machine that calls itself `hostname`; what
    it logged, and its report's host block and pairing id, with the probes
    faked."""
    log = io.StringIO()
    with patched(socket, gethostname=lambda: hostname), contextlib.redirect_stdout(log):
        scout = Scout(root / "config.json", root / "state.json")
        with patched(scout.machine, gpus=lambda: [], compute_apps=lambda: [], address=lambda: "10.0.0.5",
                     cpu_ram=lambda: {}), \
                patched(scout.builds, binary_version=lambda: "", binary_mtime=lambda: ""), \
                patched(scout.cells, views=lambda: [], first_view=lambda: {"running": False, "phase": "idle"}):
            host = scout.report.public()["host"]
            pairing_id = scout.report.pairing()["hostId"]
    state = root / "state.json"
    pinned = json.loads(state.read_text(encoding="utf-8")).get("hostId") if state.exists() else None
    return {"host": host, "pairing": pairing_id, "pinned": pinned, "log": log.getvalue()}


def test_first_run_pins():
    CHECKS.section("первый старт:")
    root = home()
    got = start(root, "box-a.lan.example")
    check([got["host"]["id"], got["pinned"], got["pairing"]] == ["box-a", "box-a", "box-a"],
          f"id — короткий hostname, прибит в state.json, тот же в отчёте и на странице сопряжения (got {got})")
    check(got["host"]["name"] == "box-a" and got["host"]["hostname"] == "box-a.lan.example",
          "имя на доске — hostname без домена; hostname — как говорит система")
    check("pinned" in got["log"], f"в журнал скаута — одна строка о прибитом id (got {got['log']!r})")
    return root


def test_rename_keeps_the_id(root):
    CHECKS.section("переименование машины:")
    got = start(root, "box-b")
    check([got["host"]["id"], got["pinned"], got["pairing"]] == ["box-a", "box-a", "box-a"],
          f"negative: новый hostname не делает новый хост — id прежний (got {got})")
    check([got["host"]["name"], got["host"]["hostname"]] == ["box-b", "box-b"],
          f"имя на доске — новое, как машина называется теперь (got {got['host']})")
    check(got["log"] == "", f"negative: прибитый id не переписывается и не пишется в журнал снова (got {got['log']!r})")


def test_name_follows_without_a_restart():
    CHECKS.section("имя — живое:")
    root = home()
    with patched(socket, gethostname=lambda: "box-a"), contextlib.redirect_stdout(io.StringIO()):
        scout = Scout(root / "config.json", root / "state.json")
    with patched(socket, gethostname=lambda: "box-c"):
        now = [scout.identity.name(), scout.config.get("hostId")]
    check(now == ["box-c", "box-a"],
          f"hostnamectl без перезапуска скаута: имя — уже новое в следующем отчёте, id — прежний (got {now})")


def test_the_operator_chooses():
    CHECKS.section("выбор оператора в config.json:")
    root = home(state={"hostId": "box-a", "startedAt": 1})
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    (root / "config.json").write_text(json.dumps({**config, "hostId": "chosen", "displayName": "Chosen One"}),
                                      encoding="utf-8")
    got = start(root, "box-b")
    check([got["host"]["id"], got["pinned"], got["host"]["name"]] == ["chosen", "chosen", "Chosen One"],
          f"hostId файла побеждает прибитый и сам становится прибитым; displayName файла — имя (got {got})")
    check("'box-a' -> 'chosen'" in got["log"], f"смена id — словами в журнале (got {got['log']!r})")
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    again = start(root, "box-d")
    check([again["host"]["id"], again["host"]["name"]] == ["chosen", "box-d"],
          f"negative: hostId убран из файла — id остаётся выбранным (прибит), имя снова живое (got {again})")


def test_an_upgrade_changes_nothing():
    CHECKS.section("обновление со скаута 2.9:")
    # What 2.9 reported: DEFAULT_CONFIG's hostId, the hostname at the start.
    with patched(socket, gethostname=lambda: "box-u.lan"):
        before = socket.gethostname().split(".")[0]
    root = home(state={"startedAt": 1, "heartbeat": {"state": "ok"}, "cells": {}})
    got = start(root, "box-u.lan")
    check([got["host"]["id"], got["pinned"]] == [before, before],
          f"state.json без id — прибивается тот id, которым скаут звался до обновления: хост тот же (got {got})")
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    check([state.get("heartbeat"), state.get("cells")] == [{"state": "ok"}, {}],
          "остальное в state.json не тронуто")


def test_nothing_to_call_it():
    CHECKS.section("без имени:")
    root = home()
    got = start(root, "")
    check([got["host"]["id"], got["pinned"], got["log"]] == ["remote", None, ""],
          f"пустой hostname (ранняя загрузка) — «remote» на этот запуск, но НЕ прибит (got {got})")
    later = start(root, "box-e")
    check([later["host"]["id"], later["pinned"]] == ["box-e", "box-e"],
          f"negative: следующий старт с именем прибивает имя, а не «remote» (got {later})")
    check("hostId" in DEFAULT_CONFIG and "displayName" in DEFAULT_CONFIG,
          "умолчания конфига на месте: их читают тесты и старые вызовы")


root = test_first_run_pins()
test_rename_keeps_the_id(root)
test_name_follows_without_a_restart()
test_the_operator_chooses()
test_an_upgrade_changes_nothing()
test_nothing_to_call_it()

sys.exit(CHECKS.finish())
