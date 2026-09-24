#!/usr/bin/env python3
"""Snapshot of the scout's HTTP surface — the contract the controller calls.

Every path the handler answers, by value: what it returns, which method of
the scout it reaches, and what it refuses. The scout's own methods are stubbed
per pin, so this file pins the ROUTING and the envelopes; what the methods
compute is pinned in the other test_scout_* files.

Pinned before the scout is rewritten into classes (docs/architecture.md):
the rewrite must keep this surface, byte for byte where the controller reads
it. The agent paths (/api/agent-config, /api/routing/apply) went with the
scout's knowledge of agents (2.0) and answer 404.

Run: python3 scripts/test_scout_http.py
"""
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks, FakeRun, RealCallBlocked, Served, make_scout, patched  # noqa: E402

from caravan_scout import __version__  # noqa: E402

CHECKS = Checks("scout http")
check = CHECKS.check

TOKEN = "t0k3n-for-tests"
AUTH = {"X-Caravan-Token": TOKEN}


def test_open_paths():
    CHECKS.section("открытые пути — страница сопряжения и здоровье:")
    scout = make_scout()
    with Served(scout) as srv:
        for path in ("/", "/index.html"):
            status, body = srv.get(path)
            check(status == 200 and isinstance(body, str) and 'id="selfAddr"' in body
                  and "/api/controller-url" not in body and "<form" not in body,
                  f"{path} — справочная страница: адрес машины для контроллера, формы сопряжения нет (2.1)")
        headers = {k.lower(): v for k, v in srv.last_headers.items()}
        check(headers.get("content-type") == "text/html; charset=utf-8" and headers.get("cache-control") == "no-cache",
              "страница — HTML в UTF-8 и не кэшируется: браузер не покажет старую после обновления скаута")
        srv.get("/api/health")
        headers = {k.lower(): v for k, v in srv.last_headers.items()}
        check(headers.get("content-type") == "application/json; charset=utf-8" and "cache-control" not in headers,
              "negative: ответы API — JSON, без запрета кэша")
        status, body = srv.get("/index.htm")
        check((status, body) == (404, {"error": "not found"}), "negative: похожий путь — 404, а не страница")
        status, body = srv.get("/api/health")
        check(status == 200 and {k: body[k] for k in ("ok", "service", "version", "tokenRequired")}
              == {"ok": True, "service": "caravan-scout", "version": __version__, "tokenRequired": False}
              and isinstance(body.get("time"), int),
              "здоровье: ok, имя службы, версия пакета, токен не требуется, время")
    closed = make_scout({"controllerToken": TOKEN})
    with Served(closed) as srv:
        status, body = srv.get("/api/health")
        check(status == 200 and body["tokenRequired"] is True,
              "с токеном здоровье открыто и говорит, что токен нужен")
        status, page = srv.get("/")
        check(status == 200 and 'id="selfAddr"' in page, "страница скаута открыта и с токеном")


def test_the_token_gate():
    CHECKS.section("токен флота закрывает всё остальное:")
    scout = make_scout({"controllerToken": TOKEN})
    with patched(scout.report, public=lambda: {"state": "stub"}), Served(scout) as srv:
        denied = (401, {"error": "fleet token required (X-Caravan-Token)"})
        check(srv.get("/api/state") == denied, "negative: без заголовка — 401 и имя заголовка")
        check(srv.get("/api/state", headers={"X-Caravan-Token": "wrong"}) == denied,
              "negative: чужой токен — 401")
        check(srv.get("/api/state", headers=AUTH) == (200, {"state": "stub"}), "свой токен — ответ")
        check(srv.get("/api/nope") == denied,
              "as-is: неизвестный путь без токена — 401, а не 404: ворота стоят перед маршрутами")
        check(srv.post("/api/heartbeat") == denied, "negative: POST тоже закрыт")
    open_scout = make_scout()
    with patched(open_scout.report, public=lambda: {"state": "stub"}), Served(open_scout) as srv:
        check(srv.get("/api/state") == (200, {"state": "stub"}),
              "negative: токена в конфиге нет — сеть доверенная, всё открыто")


def test_pairing_page_reads_an_open_path():
    CHECKS.section("страница сопряжения при токене:")
    scout = make_scout({"controllerToken": TOKEN, "controllerUrl": "http://10.0.0.1:7990"})
    scout.state["heartbeat"] = {"state": "ok", "lastAt": 5, "result": {"host": {"id": "box-a"}}}
    views = {"views": lambda: [{"port": 22001, "running": True}, {"port": 22002}]}
    machine = {"gpus": lambda: [{"name": "NVIDIA GeForce RTX 3090"}, {"model": "M"}, {}],
               "address": lambda: "10.0.0.5"}
    import socket
    with patched(scout.cells, **views), patched(scout.machine, **machine), \
            patched(socket, gethostname=lambda: "box-a.lan"), Served(scout) as srv:
        _status, page = srv.get("/")
        status, body = srv.get("/api/pairing")
        state_status, _ = srv.get("/api/state")
    check('fetch("/api/pairing")' in page and 'fetch("/api/state")' not in page,
          "defect-history: страница читает /api/pairing — /api/state закрыт токеном, и при токене она была пустой")
    check(status == 200 and body == {
        "service": "caravan-scout", "version": __version__, "hostId": "box-a", "hostname": "box-a.lan",
        "ip": "10.0.0.5", "port": 18092, "platform": sys.platform,
        "gpus": ["NVIDIA GeForce RTX 3090", "M", "GPU"], "cells": {"running": 1, "total": 2},
        "controllerUrl": "http://10.0.0.1:7990", "tokenRequired": True,
        "heartbeat": {"state": "ok", "lastAt": 5}},
          "открыт и при токене: машина, карты, ячейки, контроллер и пульс — ровно то, что показывает страница")
    check("result" not in body.get("heartbeat", {}) and state_status == 401,
          "negative: ответа контроллера на пульс в нём нет, а /api/state по-прежнему закрыт")
    check("Model servers" in page and "Add scout" in page and "8090" not in page,
          "страница говорит, где добавить машину на контроллере: Model servers → ＋ Add scout")


def test_get_routes():
    CHECKS.section("GET — какой путь зовёт какой метод:")
    scout = make_scout()
    machine = {"nvidia_smi": lambda: {"kind": "nvidia-smi", "ok": True},
               "listeners": lambda: {"ok": True, "ports": [{"port": 22, "proc": "", "pid": 0}]}}
    builds = {"status": lambda: {"running": False}, "archive": lambda: {"ok": True, "builds": []}}
    with patched(scout.machine, **machine), patched(scout.cells, views=lambda: [{"port": 22001}]), \
            patched(scout.builds, **builds), \
            patched(scout.configs, listing=lambda: [{"filename": "llama-node.bak.x.json"}]), \
            patched(scout.models, listing=lambda: [{"path": "m.gguf", "sizeBytes": 1}]), Served(scout) as srv:
        expected = {
            "/api/llama-node/status": {"ok": True, "nodes": [{"port": 22001}]},
            "/api/monitor/nvidia-smi": {"kind": "nvidia-smi", "ok": True},
            "/api/host/listeners": {"ok": True, "ports": [{"port": 22, "proc": "", "pid": 0}]},
            "/api/llama-node/configs": {"ok": True, "configs": [{"filename": "llama-node.bak.x.json"}]},
            "/api/llama-node/update-status": {"running": False},
            "/api/llama-node/builds": {"ok": True, "builds": []},
            "/api/llama-node/list-cache": {"ok": True, "models": [{"path": "m.gguf", "sizeBytes": 1}]},
        }
        for path, want in expected.items():
            check(srv.get(path) == (200, want), f"{path} — конверт метода")
        check(srv.get("/api/agent-config?id=a1") == (404, {"error": "not found"}),
              "negative: конфига агента скаут больше не отдаёт — об агентах он не знает (2.0)")
        check(srv.get("/api/state?x=1") == (404, {"error": "not found"}),
              "as-is: пути сравниваются вместе с query — /api/state?x=1 не находится")
        check(srv.get("/api/llama-node/statuses") == (404, {"error": "not found"}),
              "negative: неизвестный путь — 404 с телом {error}")


def test_errors_become_envelopes():
    CHECKS.section("ошибки — статус и {error}:")
    from caravan_scout.errors import AppError
    scout = make_scout()

    def app_error():
        raise AppError("no such thing", 404)

    def crash():
        raise ValueError("boom")
    with patched(scout.machine, listeners=app_error, nvidia_smi=crash), Served(scout) as srv:
        check(srv.get("/api/host/listeners") == (404, {"error": "no such thing"}),
              "AppError — его статус и текст")
        check(srv.get("/api/monitor/nvidia-smi") == (500, {"error": "boom"}),
              "negative: любое другое исключение — 500 и его текст, а не обрыв соединения")
        check(srv.post("/api/llama-node/start", raw=b"[1, 2]") == (400, {"error": "body must be a JSON object"}),
              "тело не объект — 400")
        check(srv.post("/api/llama-node/start", raw=b"{not json")[0] == 500,
              "as-is: битый JSON — 500, а не 400")


def test_post_routes():
    CHECKS.section("POST — какой путь зовёт какой метод:")
    scout = make_scout()
    seen = {}
    beat = {
        "pair": lambda url, token="": seen.setdefault("pair", (url, token)) and {"ok": True, "controllerUrl": url},
        "once": lambda: {"ok": True, "host": {"id": "box-a"}},
    }
    with patched(scout.heartbeat, **beat), patched(scout.cells, purge_models_safely=lambda: {"removed": 2, "freedBytes": 10},
                                          start=lambda body: {"ok": body.get("port") == 22001, "echo": body}), \
            patched(scout.builds, start_update=lambda body: {"started": body}), \
            patched(scout.configs, delete=lambda name: seen.setdefault("deleted", name)), Served(scout) as srv:
        check(srv.post("/api/controller-url", {"url": "10.0.0.1:7990", "token": "x"})
              == (200, {"ok": True, "controllerUrl": "10.0.0.1:7990"}) and seen["pair"] == ("10.0.0.1:7990", "x"),
              "сопряжение передаёт адрес и токен из тела как есть")
        check(srv.post("/api/routing/apply", {"assignments": []}) == (404, {"error": "not found"}),
              "negative: назначений агентам скаут не применяет — путь ушёл (2.0)")
        check(srv.post("/api/heartbeat") == (200, {"ok": True, "host": {"id": "box-a"}}),
              "/api/heartbeat — один пульс сейчас, его ответ")
        check(srv.post("/api/llama-node/start", {"port": 22001}) == (200, {"ok": True, "echo": {"port": 22001}}),
              "старт ячейки: ok — 200")
        check(srv.post("/api/llama-node/start", {"port": 22002}) == (400, {"ok": False, "echo": {"port": 22002}}),
              "negative: старт не удался — тот же конверт, но 400")
        check(srv.post("/api/llama-node/update", {"tag": "b1"}) == (200, {"started": {"tag": "b1"}}),
              "обновление llama.cpp получает тело как есть")
        check(srv.post("/api/llama-node/restore", {"id": "20260901-1"}) == (200, {"started": {"restoreId": "20260901-1"}}),
              "откат: id из тела становится restoreId")
        check(srv.post("/api/llama-node/restore", {"restoreId": "20260901-2"}) == (200, {"started": {"restoreId": "20260901-2"}}),
              "boundary: откат понимает и restoreId")
        check(srv.post("/api/llama-node/purge-cache") == (200, {"ok": True, "removed": 2, "freedBytes": 10}),
              "чистка кэша: ok поверх ответа метода")
        check(srv.post("/api/llama-node/configs/delete", {"filename": "llama-node.bak.1.json"}) == (200, {"ok": True})
              and seen["deleted"] == "llama-node.bak.1.json", "удаление сохранённого конфига по имени")
        check(srv.post("/api/llama-node/nope") == (404, {"error": "not found"}), "negative: неизвестный POST — 404")
        check(srv.post("/api/heartbeat", raw=b"{not json") == (200, {"ok": True, "host": {"id": "box-a"}})
              and srv.post("/api/llama-node/purge-cache", raw=b"[1]")[0] == 200,
              "тело, которое путь не читает, ему не мешает, даже если это не JSON")
        check(srv.post("/api/llama-node/update", raw=b"[1]") == (400, {"error": "body must be a JSON object"}),
              "negative: путь, который читает тело, отказывает на теле не-объекте")


def test_pairing_accepts_the_token_in_the_body():
    CHECKS.section("сопряжение при токене:")
    scout = make_scout({"controllerToken": TOKEN})
    with patched(scout.heartbeat, pair=lambda url, token="": {"ok": True, "controllerUrl": url}), \
            Served(scout) as srv:
        check(srv.post("/api/controller-url", {"url": "10.0.0.1:7990", "token": TOKEN})[0] == 200,
              "токен в теле формы — сопряжение проходит (у формы нет заголовка)")
        check(srv.post("/api/controller-url", {"url": "10.0.0.1:7990"})[0] == 401,
              "negative: без токена — 401")
        check(srv.post("/api/heartbeat", {"token": TOKEN})[0] == 401,
              "negative: токен в теле принимается только сопряжением")


class ControllerFake:
    """urllib.request.urlopen for the controller's heartbeat path: it accepts
    the tokens it is told to and answers 401 to any other, and writes every
    call down — the URL and the token it carried."""

    def __init__(self, accepts=()):
        self.accepts = set(accepts)
        self.calls = []

    def __call__(self, req, timeout=None):
        token = dict(req.header_items()).get("X-caravan-token")
        self.calls.append((req.full_url, token))
        if token in self.accepts:
            return io.BytesIO(b'{"ok": true}')
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))


def test_pairing_after_a_token_rotation():
    CHECKS.section("сопряжение после смены токена флота:")
    new_token, controller = "n3w-t0k3n-for-tests", "http://10.0.0.1:7990"
    beat_url = controller + "/api/topology/client-heartbeat"

    def rotated(accepts):
        scout = make_scout({"controllerToken": TOKEN, "controllerUrl": controller})
        return scout, ControllerFake(accepts)

    scout, fake = rotated({new_token})
    with patched(scout.report, heartbeat=lambda: {"p": 1}), patched(urllib.request, urlopen=fake), \
            Served(scout) as srv:
        status, body = srv.post("/api/controller-url", {"url": "10.0.0.1:7990/", "token": new_token})
    check(status == 200 and body.get("controllerUrl") == controller and body.get("heartbeat", {}).get("state") == "ok",
          "defect-history: контроллер принял новый токен — сопряжение проходит; раньше новый токен сверялся со "
          "старым, и страница отказывала")
    check(scout.config.token() == new_token
          and json.loads(scout.config.path.read_text(encoding="utf-8")).get("controllerToken") == new_token,
          "новый токен записан в config.json и взят в работу")
    check(fake.calls == [(beat_url, new_token), (beat_url, new_token)],
          "сначала пульс с новым токеном как доказательство, затем пульс сопряжения — тоже с новым")

    scout, fake = rotated(set())
    with patched(scout.report, heartbeat=lambda: {"p": 1}), patched(urllib.request, urlopen=fake), \
            Served(scout) as srv:
        refused = srv.post("/api/controller-url", {"url": controller, "token": new_token})
    check(refused == (401, {"error": "fleet token required (X-Caravan-Token)"}) and scout.config.token() == TOKEN
          and fake.calls == [(beat_url, new_token)],
          "negative: контроллер не принял токен — 401, в config.json прежний токен")

    for label, form in (("другой контроллер", {"url": "http://10.0.0.9:7990", "token": new_token}),
                        ("пустой токен", {"url": controller, "token": ""}),
                        ("не адрес", {"url": "http://", "token": new_token})):
        scout, fake = rotated({new_token})
        with patched(scout.report, heartbeat=lambda: {"p": 1}), patched(urllib.request, urlopen=fake), \
                Served(scout) as srv:
            got = srv.post("/api/controller-url", form)
        check(got[0] == 401 and fake.calls == [] and scout.config.token() == TOKEN,
              f"negative: {label} — 401 без единого запроса к контроллеру; перенаправить скаут можно только "
              f"с токеном, который он держит")


def test_unpair_route():
    CHECKS.section("контроллер отпускает машину (/api/unpair):")
    scout = make_scout({"controllerToken": TOKEN, "controllerUrl": "http://10.0.0.1:7990"})
    with patched(scout.machine, gpus=lambda: [], address=lambda: "10.0.0.5"), Served(scout) as srv:
        denied = srv.post("/api/unpair")
        kept = scout.config.token()
        done = srv.post("/api/unpair", headers=AUTH)
        _status, after = srv.get("/api/pairing")
        reopened = srv.get("/api/state?probe")[0]
    check(denied == (401, {"error": "fleet token required (X-Caravan-Token)"}) and kept == TOKEN,
          "negative: отпустить машину может только её контроллер — без токена 401, всё на месте")
    check(done == (200, {"ok": True}), "со своим токеном — ok")
    check(after.get("controllerUrl") == "" and after.get("tokenRequired") is False
          and after.get("heartbeat") == {"state": "unpaired"} and reopened == 404,
          "после — ни контроллера, ни токена: скаут открыт и ждёт следующего сопряжения, пульс «unpaired»")


def test_host_power():
    CHECKS.section("перезагрузка и выключение:")
    scout = make_scout()
    with Served(scout) as srv:
        for action in ("reboot", "poweroff"):
            run = FakeRun({("sudo", "-n", "systemctl", action): (0, "")})
            with patched(subprocess, run=run):
                result = srv.post(f"/api/host/{action}")
            check(result == (200, {"ok": True, "detail": f"{action} issued"})
                  and run.calls == [["sudo", "-n", "systemctl", action]],
                  f"{action}: ровно одна команда sudo -n systemctl {action}")
        run = FakeRun({("sudo",): (1, "sudo: a password is required")})
        with patched(subprocess, run=run):
            result = srv.post("/api/host/reboot")
        check(result == (500, {"ok": False, "error": "reboot refused: sudo: a password is required — "
                                                     "passwordless sudo for `systemctl reboot` is required"}),
              "negative: sudo просит пароль — 500 и подсказка, что нужен sudo без пароля")
        run = FakeRun({("sudo",): (1, "Failed to connect to bus")})
        with patched(subprocess, run=run):
            result = srv.post("/api/host/poweroff")
        check(result == (500, {"ok": False, "error": "poweroff refused: Failed to connect to bus"}),
              "negative: другая причина — 500 без подсказки про пароль")
        run = FakeRun({("sudo",): subprocess.TimeoutExpired(["sudo"], 10)})
        with patched(subprocess, run=run):
            result = srv.post("/api/host/reboot")
        check(result == (200, {"ok": True, "detail": "reboot issued"}),
              "boundary: команда не вернулась за 10 с — машина уже уходит, это успех")
        run = FakeRun({("sudo",): OSError("no sudo")})
        with patched(subprocess, run=run):
            result = srv.post("/api/host/reboot")
        check(result == (500, {"ok": False, "error": "reboot failed: no sudo"}), "negative: не запустилась — 500 failed")
        check(srv.post("/api/host/shutdown") == (404, {"error": "not found"}),
              "negative: выключение — только своим путём, похожий путь не срабатывает")


def test_stop():
    CHECKS.section("остановка ячейки:")
    scout = make_scout({"llamaServerBin": "/opt/llama/bin/llama-server"})
    purged, kills = [], []
    alive = {}

    def listener(port):
        return {22001: 4242, 22002: 5151}.get(port, 0)

    def cmdline(pid):
        return alive.get(pid, "")

    def kill(pid, sig):
        kills.append((pid, sig))
        alive.pop(pid, None)
    with patched(scout.cells.processes, listener=listener, cmdline=cmdline), \
            patched(scout.cells, purge_models_safely=lambda: purged.append(1) or {"removed": 0, "freedBytes": 0}), \
            patched(os, kill=kill), Served(scout) as srv:
        scout.cells.records.add(22001, "llama", 4242, "/opt/llama/bin/llama-server", {"port": 22001}, None, False)
        alive[4242] = "/opt/llama/bin/llama-server --port 22001"
        result = srv.post("/api/llama-node/stop", {"port": 22001})
        check(result == (200, {"ok": True, "reclaimed": True, "pid": 4242}) and kills[:1] == [(4242, 15)],
              "узел ничего не держал, но порт слушает наш llama-server — SIGTERM и «reclaimed»")
        check("22001" not in (scout.state.get("cells") or {}) and 22001 not in dict(scout.cells.all()),
              "после остановки ячейка ушла из реестра и из слотов")
        check(purged == [1], "кэш без cacheModels чистится один раз, безопасной чисткой")

        scout.cells.records.add(22002, "command", 5151, "python3 other.py", {"port": 22002}, None, True)
        alive[5151] = "/usr/bin/some-dev-server --port 22002"
        kills.clear()
        result = srv.post("/api/llama-node/stop", {"port": 22002})
        check(result == (200, {"ok": False, "listenerPid": 5151,
                               "error": "port 22002 is held by an unrecognized process (pid 5151) — not killing it"})
              and kills == [],
              "negative: порт держит чужой процесс — его не убивают и говорят, кто держит")
        check("22002" in (scout.state.get("cells") or {}),
              "negative: неподтверждённая остановка не стирает запись реестра — ячейка не становится ничьей")

        result = srv.post("/api/llama-node/stop", {"port": 22003})
        check(result == (200, {"ok": True, "detail": "not running"}),
              "boundary: порт никто не слушает — «not running», это успех")


def test_stop_every_slot():
    CHECKS.section("остановка без порта — все слоты:")
    scout = make_scout()
    purged = []
    with patched(scout.cells.processes, listener=lambda port: 0), \
            patched(scout.cells, purge_models_safely=lambda: purged.append(1) or {"removed": 0, "freedBytes": 0}), \
            Served(scout) as srv:
        scout.cells.at(22001).cache_models = True
        scout.cells.at(22002).cache_models = True
        result = srv.post("/api/llama-node/stop", {})
        check(result == (200, {"ok": True, "results": [{"ok": True, "detail": "not running"},
                                                        {"ok": True, "detail": "not running"}]}),
              "без порта — остановка каждого слота и список результатов")
        check(scout.cells.all() == [], "все слоты сняты")
        check(purged == [], "negative: ячейки с кэшированием моделей — кэш при остановке не чистится")


TESTS = (test_open_paths, test_the_token_gate, test_pairing_page_reads_an_open_path, test_get_routes,
         test_errors_become_envelopes, test_post_routes, test_pairing_accepts_the_token_in_the_body,
         test_pairing_after_a_token_rotation, test_unpair_route, test_host_power, test_stop, test_stop_every_slot)

for test in TESTS:
    try:
        test()
    except (Exception, RealCallBlocked) as exc:  # noqa: BLE001 — a crash is a red pin; the rest still runs
        check(False, f"{test.__name__} упал: {exc!r}")

sys.exit(CHECKS.finish())
