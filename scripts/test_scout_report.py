#!/usr/bin/env python3
"""Snapshot of what the scout knows about itself and tells the controller.

Its config and state files, the pairing that points it at a controller, the
report (/api/state and the heartbeat), the heartbeat loop's pace, the address
it reports, and the live numbers it reads off a running llama-server.

Pinned before the rewrite into classes. The agent keys the report used to
carry (agents, candidates, assignments, applyStatus) went with the scout's
knowledge of agents (2.0); their absence is pinned.

Run: python3 scripts/test_scout_report.py
"""
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks, FakeRun, make_scout, patched  # noqa: E402

import urllib.request  # noqa: E402
from caravan_scout import __version__  # noqa: E402
from caravan_scout.errors import AppError  # noqa: E402

CHECKS = Checks("scout report")
check = CHECKS.check


class FakeResponse:
    """What urlopen hands back: a context manager with read() and headers."""

    def __init__(self, body=b"", headers=None, status=200):
        self._body = body
        self.headers = headers or {}
        self.status = status

    def read(self, *_a):
        body, self._body = self._body, b""
        return body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def raises(fn, *args):
    """The AppError a call raises, as (status, message), or None."""
    try:
        fn(*args)
    except AppError as exc:
        return exc.status, str(exc)
    return None


def test_config():
    CHECKS.section("config.json:")
    scout = make_scout({"listenPort": "18093", "heartbeatIntervalSeconds": 0, "llamaServerBin": "/opt/l"})
    cfg = scout.config
    check(cfg["listenPort"] == 18093 and cfg["heartbeatIntervalSeconds"] == 60,
          "порт — число; интервал 0 — это «не задан» → 60")
    check(make_scout({"heartbeatIntervalSeconds": "0"}).config["heartbeatIntervalSeconds"] == 2,
          "as-is: строка «0» непуста, значит задана — и становится нижней границей 2 с, а не 60")
    scout = make_scout({"heartbeatIntervalSeconds": 1})
    check(scout.config["heartbeatIntervalSeconds"] == 2, "boundary: интервал не короче 2 с")
    check(scout.config["llamaNodeDefaultPort"] == 8180 and scout.config["cleanOldModels"] is False,
          "умолчания доливаются из DEFAULT_CONFIG")
    from caravan_scout.paths import DEFAULT_CONFIG
    check(not {"agents", "registryUrl", "applyCommand", "openclawConfigPath", "openclawAgentId"} & set(DEFAULT_CONFIG),
          "negative: в умолчаниях нет ничего про агентов — ни списка, ни реестра, ни команды применения")
    bad = make_scout()
    bad.config.path.write_text("[1]", encoding="utf-8")
    check(raises(bad.config.load) == (400, "config must be a JSON object"),
          "negative: конфиг не объект — отказ, а не пустой конфиг")


def test_state():
    CHECKS.section("state.json:")
    scout = make_scout(state={"startedAt": 5, "cells": {"22001": {"port": 22001}}})
    check(scout.state["startedAt"] == 5 and scout.state["cells"] == {"22001": {"port": 22001}},
          "сохранённое состояние читается как было")
    check(scout.state["heartbeat"] == {"state": "pending"}
          and "assignments" not in scout.state and "applyStatus" not in scout.state,
          "недостающее доливается: пульс pending; назначений и их статуса в состоянии нет")
    import contextlib
    import io
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        old = make_scout(state={"startedAt": 5, "assignments": [{"agentId": "a1"}], "applyStatus": {"state": "ok"}})
    on_disk = json.loads(old.state.path.read_text(encoding="utf-8"))
    check("assignments" not in old.state and "applyStatus" not in on_disk and on_disk["startedAt"] == 5
          and "[state] dropped what 1.x kept about agents: assignments, applyStatus" in said.getvalue(),
          "state.json от 1.x теряет назначения и их статус один раз, с записью на диск и строкой в логе")
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        make_scout(state={"startedAt": 5})
    check("dropped" not in said.getvalue(), "negative: чистое состояние — ни правки, ни строки")
    for raw in ("{broken", "[1, 2]"):
        s = make_scout()
        s.state.path.write_text(raw, encoding="utf-8")
        check(s.state.read_file(s.state.path) == {}, f"negative: state.json {raw!r} — пустое состояние, а не падение")
    scout.state["x"] = "ü"
    scout.state.save()
    text = scout.state.path.read_text(encoding="utf-8")
    check(json.loads(text)["x"] == "ü" and '"ü"' in text and "\n  " in text
          and not scout.state.path.with_suffix(".tmp").exists(),
          "запись — через .tmp и замену, с отступом, без \\u-экранирования")


def test_token():
    CHECKS.section("токен флота:")
    check(make_scout({"controllerToken": "  abc  "}).config.headers() == {"X-Caravan-Token": "abc"},
          "токен обрезается и едет заголовком X-Caravan-Token")
    check(make_scout().config.headers() == {}, "negative: токена нет — заголовка нет")


def test_pairing():
    CHECKS.section("сопряжение с контроллером:")
    scout = make_scout({"extra": 1})
    beats = []
    with patched(scout.heartbeat, once=lambda: beats.append(1) or {"ok": True}):
        try:
            out = scout.heartbeat.pair(" 10.0.0.1:7990/ ", " sekret ")
        except AppError as exc:
            out = {"controllerUrl": f"refused: {exc}", "heartbeat": {}}
    raw = json.loads(scout.config.path.read_text(encoding="utf-8"))
    check(out["controllerUrl"] == "http://10.0.0.1:7990" and scout.config["controllerUrl"] == "http://10.0.0.1:7990",
          f"адрес без схемы получает http://, хвостовой / снят (got {out['controllerUrl']})")
    check(raw.get("controllerUrl") == "http://10.0.0.1:7990" and raw.get("controllerToken") == "sekret" and raw["extra"] == 1
          and "llamaNodeDefaultPort" not in raw,
          "в файл пишется только адрес и токен поверх того, что там было — не умолчания")
    check(beats == [1] and out["heartbeat"].get("state") == "ok" and out["heartbeat"].get("result") == {"ok": True}
          and scout.state["heartbeat"]["state"] == "ok",
          "сразу один пульс, и его итог сохранён в состояние")
    scout2 = make_scout({"controllerToken": "old"})

    def fail():
        raise OSError("refused")
    with patched(scout2.heartbeat, once=fail):
        out = scout2.heartbeat.pair("http://10.0.0.2:7990", "")
    check(out["heartbeat"] == {"state": "error", "lastAt": out["heartbeat"]["lastAt"], "error": "refused"}
          and scout2.config["controllerToken"] == "old"
          and json.loads(scout2.config.path.read_text(encoding="utf-8"))["controllerToken"] == "old",
          "negative: пульс не прошёл — сопряжение всё равно записано, ошибка названа; пустой токен старый не трогает")
    for bad in ("", "ftp://x", "http://", "http:///"):
        refusing = make_scout()
        with patched(refusing.heartbeat, once=lambda: {"ok": True}):
            got = raises(refusing.heartbeat.pair, bad)
        check(got == (400, "controller url must look like http://host:7990"),
              f"negative: {bad!r} — отказ с примером адреса контроллера на его порту 7990 (got {got})")
    odd = make_scout()
    with patched(odd.heartbeat, once=lambda: {"ok": True}):
        outcome = raises(odd.heartbeat.pair, "http://")
    check(outcome == (400, "controller url must look like http://host:7990")
          and odd.config.get("controllerUrl") == "",
          "defect-history: «http://» — отказ; раньше снятие хвостового / давало «http:», "
          "та получала второй http:// и проходила хостом «http»")


@contextlib.contextmanager
def _stub_probes(scout, gpus=None, nodes=None):
    """The report's inputs, faked: every probe the report makes of the host."""
    with patched(
        scout.machine,
        gpus=lambda: gpus if gpus is not None else [{"index": "0", "name": "RTX"}],
        compute_apps=lambda: [{"gpuUuid": "u0", "pid": 11, "usedMiB": 900}],
        address=lambda: "10.0.0.5",
        cpu_ram=lambda: {"loadPct": 3.0},
    ), patched(
        scout.builds,
        binary_version=lambda: "version: 9947 (abc1234)",
        binary_mtime=lambda: "2026-09-01T10:00:00",
    ), patched(
        scout.cells,
        views=lambda: nodes if nodes is not None else [{"port": 22001, "phase": "running"}],
        first_view=lambda: (nodes or [{"port": 22001, "phase": "running"}])[0] if nodes != [] else
        {"running": False, "phase": "idle"},
    ):
        yield scout


def test_public_state():
    CHECKS.section("отчёт /api/state:")
    scout = make_scout({"controllerUrl": "http://10.0.0.1:7990"})
    beat = {"state": "ok", "lastAt": 1_699_999_990, "result": {"ok": True}}
    scout.state["heartbeat"] = beat
    with _stub_probes(scout), \
            patched(socket, gethostname=lambda: "box-a.lan"), patched(time, time=lambda: 1_700_000_000):
        state = scout.report.public()
    check(state.get("heartbeat") == beat,
          "итог последнего пульса — как записан в состоянии, с ответом контроллера (путь закрыт токеном)")
    fresh = make_scout()
    with _stub_probes(fresh):
        check(fresh.report.public().get("heartbeat") == {"state": "pending"},
              "negative: до первого пульса — «pending», а не пусто")
    check(sorted(state) == sorted(["service", "scoutVersion", "llamaBinaryVersion", "llamaBinaryMtime", "llamaUpdate",
                                   "llamaSuspect", "host", "controllerUrl", "gpus", "computeApps", "cpu", "platform",
                                   "heartbeat", "llamaNode", "llamaNodes", "autostart", "time"]),
          "ровно эти поля — только машина: ни агентов, ни найденных VM, ни назначений")
    check(state["host"] == {"id": "box-a", "name": "Box A", "hostname": "box-a.lan", "ip": "10.0.0.5"},
          "машина: id и имя из конфига, hostname системы, адрес — тот, что видит контроллер")
    check((state["service"], state.get("scoutVersion"), state["platform"], state["time"])
          == ("caravan-scout", __version__, sys.platform, 1_700_000_000),
          "служба, версия скаута (scoutVersion), платформа, время")
    check(state.get("llamaUpdate") == {"running": False, "done": False, "rc": None, "startedAt": 0, "tag": "",
                                       "lastLine": ""},
          "статус обновления llama.cpp — короткий, без строк журнала")
    check(state["gpus"] == [{"index": "0", "name": "RTX"}] and state["cpu"] == {"loadPct": 3.0}
          and state["computeApps"] == [{"gpuUuid": "u0", "pid": 11, "usedMiB": 900}]
          and state["llamaNodes"] == [{"port": 22001, "phase": "running"}],
          "видеокарты, процессор, процессы на картах и ячейки — как их отдали пробы")
    check(not {"agents", "candidates", "assignments", "applyStatus"} & set(state),
          "negative: об агентах отчёт молчит (2.0) — контроллер их и не читает")


def test_heartbeat_payload():
    CHECKS.section("пульс:")
    scout = make_scout({"controllerUrl": "http://10.0.0.1:7990", "listenPort": 18099})
    with _stub_probes(scout), \
            patched(socket, gethostname=lambda: "box-a.lan"), patched(time, time=lambda: 1_700_000_000):
        try:
            payload = scout.report.heartbeat()
        except Exception as exc:  # noqa: BLE001 — a crash is a red pin, not a stopped run
            payload = {"__raised__": repr(exc), "agentUrl": None}
    check(sorted(payload) == sorted(["host", "gpus", "computeApps", "cpu", "platform", "llamaNode", "llamaNodes",
                                     "llamaBinaryVersion", "llamaBinaryMtime", "llamaUpdate", "llamaSuspect",
                                     "scoutVersion", "autostart", "agentUrl", "time"]),
          "ровно эти поля — только машина")
    check(payload["agentUrl"] == "http://10.0.0.5:18099", "адрес скаута — его IP и порт, на котором он слушает")
    check(payload.get("llamaUpdate") == {"running": False, "done": False, "rc": None, "startedAt": 0, "tag": "",
                                         "lastLine": ""},
          "defect-history: статус обновления llama.cpp едет и в пульсе — пульс заменял запись хоста без него, "
          "и «сборка идёт» мигало на доске")
    check(payload.get("scoutVersion") == __version__ and "version" not in payload,
          "скаут называет свою версию одним именем, scoutVersion — тем же, что в /api/state")


def test_heartbeat_once():
    CHECKS.section("один пульс:")
    check(raises(make_scout().heartbeat.once) == (400, "controllerUrl is required"),
          "negative: без адреса контроллера — отказ")
    scout = make_scout({"controllerUrl": "http://10.0.0.1:7990/", "controllerToken": "tok"})
    sent = []

    def urlopen(req, timeout=None):
        sent.append((req.full_url, req.get_method(), dict(req.header_items()), json.loads(req.data), timeout))
        return FakeResponse(b'{"ok": true, "host": {"id": "box-a"}}')
    with patched(scout.report, heartbeat=lambda: {"p": 1}), patched(urllib.request, urlopen=urlopen):
        out = scout.heartbeat.once()
    url, method, headers, body, timeout = sent[0]
    check(url == "http://10.0.0.1:7990/api/topology/client-heartbeat" and method == "POST" and body == {"p": 1}
          and headers.get("X-caravan-token") == "tok" and timeout == 5,
          "POST на /api/topology/client-heartbeat с токеном, таймаут 5 с")
    check(out == {"ok": True, "host": {"id": "box-a"}}, "ответ контроллера возвращается как есть")
    with patched(scout.report, heartbeat=lambda: {}), \
            patched(urllib.request, urlopen=lambda req, timeout=None: FakeResponse(b"")):
        check(scout.heartbeat.once() == {"ok": True}, "boundary: пустой ответ — {ok: true}")


class Stop(BaseException):
    pass


def test_heartbeat_pace():
    CHECKS.section("темп пульса:")
    for nodes, want, why in (([{"phase": "downloading"}], 5, "ячейка качает модель — пульс раз в 5 с"),
                             ([{"phase": "warming"}], 5, "boundary: прогрев — тоже 5 с"),
                             ([{"phase": "running"}], 77, "negative: всё запущено — интервал из конфига"),
                             ([], 77, "negative: ячеек нет — интервал из конфига")):
        scout = make_scout({"heartbeatIntervalSeconds": 77})
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            raise Stop()
        with patched(scout.heartbeat, once=lambda: {"ok": True}), patched(scout.cells, views=lambda n=nodes: n), \
                patched(time, sleep=sleep):
            try:
                scout.heartbeat.loop()
            except Stop:
                pass
        check(slept == [want], f"{why} (got {slept})")
    scout = make_scout({"controllerUrl": "http://10.0.0.1:7990"})

    def fail():
        raise OSError("down")

    def stop(_s):
        raise Stop()
    with patched(scout.heartbeat, once=fail), patched(scout.cells, views=lambda: []), patched(time, sleep=stop):
        try:
            scout.heartbeat.loop()
        except Stop:
            pass
    check(scout.state["heartbeat"]["state"] == "error" and scout.state["heartbeat"]["error"] == "down"
          and json.loads(scout.state.path.read_text())["heartbeat"]["state"] == "error",
          "negative: пульс не прошёл — ошибка записана в состояние, петля живёт дальше")
    idle, beats = make_scout(), []
    with patched(idle.heartbeat, once=lambda: beats.append(1) or {"ok": True}), \
            patched(idle.cells, views=lambda: []), patched(time, sleep=stop):
        try:
            idle.heartbeat.loop()
        except Stop:
            pass
    check(beats == [] and idle.state["heartbeat"] == {"state": "unpaired"},
          "скаут, которого никто не сопряг, в пустоту не стучит: в состоянии «unpaired», а не ошибка каждую минуту")


def test_unpair():
    CHECKS.section("контроллер отпускает машину:")
    scout = make_scout({"controllerUrl": "http://10.0.0.1:7990", "controllerToken": "sekret", "extra": 1})
    out = scout.heartbeat.unpair()
    raw = json.loads(scout.config.path.read_text(encoding="utf-8"))
    check(out == {"ok": True} and "controllerUrl" not in raw and "controllerToken" not in raw and raw.get("extra") == 1,
          "адрес и токен контроллера ушли из config.json, остальное в файле на месте")
    check(scout.config.get("controllerUrl") == "" and scout.config.token() == ""
          and scout.state["heartbeat"] == {"state": "unpaired"},
          "и из работающего конфига: токена нет — скаут открыт до следующего сопряжения; пульс «unpaired»")
    again = make_scout()
    again.heartbeat.unpair()
    check(again.config.get("controllerUrl") == "" and again.state["heartbeat"] == {"state": "unpaired"},
          "negative: отпустить скаут, которого никто не держал, — не ошибка")


class FakeSocket:
    connected = []
    fail = False

    def __init__(self, *_a):
        pass

    def connect(self, addr):
        if FakeSocket.fail:
            raise OSError("no route")
        FakeSocket.connected.append(addr)

    def getsockname(self):
        return ("10.0.0.5", 50000)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_local_ip():
    CHECKS.section("адрес, который скаут называет:")
    FakeSocket.connected, FakeSocket.fail = [], False
    with patched(socket, socket=FakeSocket):
        ip = make_scout({"controllerUrl": "http://10.0.0.1:7990"}).machine.address()
        check(ip == "10.0.0.5" and FakeSocket.connected == [("10.0.0.1", 7990)],
              "адрес интерфейса, что смотрит на контроллер")
        make_scout({"controllerUrl": ""}).machine.address()
        check(FakeSocket.connected[-1] == ("8.8.8.8", 80), "negative: контроллер не задан — на 8.8.8.8:80")
        FakeSocket.fail = True
        check(make_scout().machine.address() == "127.0.0.1",
              "as-is: маршрута нет — 127.0.0.1, и контроллер получит адрес петли")
    FakeSocket.fail = False


def test_binary_version():
    CHECKS.section("версия и дата сборки llama-server:")
    check(make_scout().builds.binary_version() == "",
          "negative: бинарь не задан — пусто")
    binary = make_scout().config.path.parent / "llama-server"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    scout = make_scout({"llamaServerBin": str(binary)})
    run = FakeRun({(str(binary), "--version"): (0, "version: 9947 (abc1234)\nbuilt with cc\n")})
    with patched(subprocess, run=run):
        check(scout.builds.binary_version() == "version: 9947 (abc1234)", "первая строка --version")
    with patched(subprocess, run=FakeRun({(str(binary),): (1, "boom")})):
        check(scout.builds.binary_version() == "", "negative: --version упал — пусто")

    def answers(stdout, stderr):
        return lambda cmd, **_kw: subprocess.CompletedProcess(list(cmd), 0, stdout=stdout, stderr=stderr)
    with patched(subprocess, run=answers("", "version: 9947 (abc1234)\nbuilt with cc\n")):
        check(scout.builds.binary_version() == "version: 9947 (abc1234)",
              "llama-server печатает версию в stderr — берётся оттуда")
    with patched(subprocess, run=answers("from stdout\n", "from stderr\n")):
        check(scout.builds.binary_version() == "from stdout", "boundary: есть и stdout, и stderr — первым stdout")
    os.utime(binary, (1_700_000_000.7, 1_700_000_000.7))
    mtime = scout.builds.binary_mtime()
    check(len(mtime) == 19 and mtime[4] == "-" and mtime[10] == "T",
          "дата бинаря — ISO без часового пояса, по местному времени")
    built = scout.builds.binary_built_at()
    check(built == 1_700_000_000 and type(built) is int,
          "когда сделан бинарь — целые секунды: из них ключ сборки, под которым держится баннер подозрения")
    check(make_scout().builds.binary_built_at() == 0 and make_scout().builds.binary_mtime() == "",
          "negative: бинаря нет — 0 и пустая дата, не «1970-01-01»")
    folder = make_scout({"llamaServerBin": str(binary.parent)})
    check(folder.builds.binary_built_at() == 0,
          "negative: в конфиге папка вместо бинаря — её время не выдаётся за сборку")


def test_live_numbers():
    CHECKS.section("живые числа запущенного llama-server:")
    scout = make_scout()
    metrics = (b"# HELP x\nllamacpp:prompt_tokens_seconds 120.456\nllamacpp:predicted_tokens_seconds 33.3333\n"
               b"llamacpp:requests_processing 2\nllamacpp:kv_cache_usage_ratio 0.25\ngarbage\n")
    props = json.dumps({"default_generation_settings": {"n_ctx": 8192}}).encode()
    asked = []

    def urlopen(url, timeout=None):
        asked.append(url)
        return FakeResponse(metrics if url.endswith("/metrics") else props)
    clock = [1000.0]
    with patched(urllib.request, urlopen=urlopen), patched(time, time=lambda: clock[0]):
        out = scout.cells.probe.metrics(22001)
        check(out == {"promptTps": 120.46, "genTps": 33.33, "requestsProcessing": 2, "ctxMax": 8192, "ctxUsed": 2048},
              "скорости, очередь, окно и занятость KV — из /metrics и /props")
        clock[0] += 1.5
        scout.cells.probe.metrics(22001)
        check(asked.count("http://127.0.0.1:22001/metrics") == 1, "метрики кэшируются 2 с")
        clock[0] += 1.0
        scout.cells.probe.metrics(22001)
        check(asked.count("http://127.0.0.1:22001/metrics") == 2, "boundary: через 2 с — спрашиваются снова")
        check(asked.count("http://127.0.0.1:22001/props") == 1, "окно /props кэшируется дольше — 30 с")

    def refuse(url, timeout=None):
        raise OSError("refused")
    fresh = make_scout()
    with patched(urllib.request, urlopen=refuse):
        check(fresh.cells.probe.metrics(22002) == {}, "negative: сервер не отвечает — пусто, без окна")
    with patched(urllib.request, urlopen=lambda url, timeout=None: FakeResponse(json.dumps({"n_ctx": 4096}).encode())):
        check(make_scout().cells.probe.ctx_max(22003) == 4096, "boundary: окно и из верхнего n_ctx старых сборок")


def test_report_sample():
    CHECKS.section("образец отчёта — один на обе стороны:")
    from report_sample import ReportSample
    from caravan_scout.report import Report
    sample = ReportSample()
    check(sample.current(),
          "docs/report-sample.json — ровно то, что скаут шлёт в пульсе и в /api/state; его копию читает контроллер "
          "(разошлось — python3 scripts/report_sample.py --write и копия в lama-caravan)")
    original = Report.heartbeat
    with patched(Report, heartbeat=lambda self: {**original(self), "renamedField": 1}):
        check(not sample.current(), "negative: поле в пульсе добавлено или переименовано — образец уже не совпадает")


for fn in (test_config, test_state, test_token, test_pairing, test_public_state, test_heartbeat_payload,
           test_heartbeat_once, test_heartbeat_pace, test_unpair, test_local_ip, test_binary_version,
           test_live_numbers, test_report_sample):
    fn()

sys.exit(CHECKS.finish())
