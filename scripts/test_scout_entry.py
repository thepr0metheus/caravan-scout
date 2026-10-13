#!/usr/bin/env python3
"""The machine's entry by OpenAI's API (2.27): one address for the models of this machine.

The owner, 2026-10-12 (visumap caravan-scout-openai, step 1): a model is called by its name, not by
the port of its cell. Pinned here, by value:

  * the name of a cell's model: the operator's (MODEL_NAME), the alias, one made of its model, or
    `cell-<port>` — and that a start gives it at once and its record keeps it;
  * GET /v1/models: which cells are models, their states, two cells of one model as its copies;
  * which copy takes a request: the less busy one, equal ones in turn;
  * the request read once: `model` of a JSON body or of a form, and the same request naming another;
  * the names a running cell answers to — asked of it once per start;
  * the way through, against a real server on 127.0.0.1 standing for a cell: what reaches the cell
    and what does not, the answer as it came, a stream as a stream, the refusals in OpenAI's shape.

Run: python3 scripts/test_scout_entry.py
"""
import http.client
import io
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks, Served, make_scout, patched  # noqa: E402

from caravan_scout.entry import (Copies, EntryBody, EntryRequest, MachineEntry, MachineModels,  # noqa: E402
                                 ModelName, ServedNames)
from caravan_scout.http import ScoutHandler  # noqa: E402

CHECKS = Checks("scout entry")
check = CHECKS.check

BOUNDARY = "----caravan-test-7f3a"


class FakeProcess:
    """A cell's process as the entry reads it: running, or not and why."""

    def __init__(self, running=False, crashed=False):
        self.running, self.crashed = running, crashed

    def status(self):
        return {"running": self.running, **({"crashed": True} if self.crashed else {})}


class FakeProbe:
    """The cells' servers asked about their places (2.29): `total_slots` of /props and
    `requestsProcessing` of /metrics, by port; a port not named says nothing."""

    def __init__(self, slots=None, busy=None):
        self.slots, self.busy = dict(slots or {}), dict(busy or {})

    def slots_total(self, port):
        return self.slots.get(port)

    def metrics(self, port):
        return {"requestsProcessing": self.busy[port]} if port in self.busy else {}


def put(s, port, state="running", name="", record=None, phase=None):
    """A cell of the scout `s` as the entry sees it: its process, its start's phase, its name and record."""
    if not isinstance(s.cells.probe, FakeProbe):
        s.cells.probe = FakeProbe()
    cell = s.cells.at(port)
    cell.process = FakeProcess(running=state == "running", crashed=state == "crashed")
    if phase:
        cell.startup = {"phase": phase}
    cell.name = name
    if record is not None:
        s.state.setdefault("cells", {})[str(port)] = {"port": port, **record}
    return cell


def form(fields, file_bytes=b"RIFF\x00\x01model\r\n--not-the-boundary\x02"):
    """A multipart body as a client sends a recording: the fields, then the file."""
    out = b""
    for name, value in fields:
        out += (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
    out += (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"model\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + file_bytes + f"\r\n--{BOUNDARY}--\r\n".encode()
    return out


FORM_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


class FakeCell:
    """A cell's server on 127.0.0.1, an ephemeral port: what it was sent, and how it answers.

    `names` is what its /v1/models lists — None: it has no such path (404), as the caravan's own cells;
    `mode` is how it answers a POST: json, refused (400), stream (one event, then waits to be let go),
    broken (one event, then the connection drops)."""

    def __init__(self, names=None, mode="json"):
        self.names, self.mode = names, mode
        self.seen = []
        self.asked_names = 0
        self.gate = threading.Event()
        self.second_sent = False
        cell = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version, sys_version = "fake-cell", ""

            def log_message(self, *args):
                pass

            def send(self, status, payload, extra=()):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in extra:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/v1/models" and cell.names is not None:
                    cell.asked_names += 1
                    self.send(200, {"object": "list", "data": cell.names})
                else:
                    self.send(404, {"error": "not found"})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                cell.seen.append({"path": self.path, "headers": list(self.headers.items()), "body": body})
                getattr(cell, "answer_" + cell.mode)(self, body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def answer_json(h, body):
        h.send(200, {"object": "chat.completion", "got": len(body)},
               (("X-Cell", "yes"), ("Keep-Alive", "timeout=5"), ("Connection", "keep-alive")))

    @staticmethod
    def answer_refused(h, body):
        h.send(400, {"error": {"message": "bad request", "type": "invalid_request_error"}})

    @staticmethod
    def chunk(h, data):
        h.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        h.wfile.flush()

    def answer_stream(self, h, body):
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        self.chunk(h, b"data: one\n\n")
        self.gate.wait(3)
        self.second_sent = True
        self.chunk(h, b"data: [DONE]\n\n")
        h.wfile.write(b"0\r\n\r\n")

    def answer_broken(self, h, body):
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        self.chunk(h, b"data: one\n\n")
        h.close_connection = True   # no last chunk: the cell is gone mid-answer


def closed_port():
    """A port nothing listens on: bound, read, let go."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wire(s, ports, loads=None):
    """The scout's entry, its cells' ports pointed at local servers: {cell port: real port}."""
    connect = (lambda port, timeout: http.client.HTTPConnection("127.0.0.1", ports[port], timeout=timeout))
    s.entry = MachineEntry(s.entry.models, Copies(lambda port: (loads or {}).get(port)), connect=connect)
    return s.entry


def call(srv, method, path, body=b"", headers=None):
    """A request to the served scout: (status, headers as pairs, body bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    conn.request(method, path, body=body, headers=dict(headers or {}))
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, resp.getheaders(), data


def header(pairs, name):
    return [v for k, v in pairs if k.lower() == name.lower()]


def test_model_name():
    CHECKS.section("имя модели ячейки:")
    cases = [
        ({"MODEL_NAME": "gemma", "ALIAS": "g4", "MODEL_FILE": "models/x.gguf"}, "", 22001, "gemma",
         "имя оператора (MODEL_NAME) — первое"),
        ({"MODEL_NAME": "  ", "ALIAS": " qwen3-8b ", "MODEL_FILE": "models/x.gguf"}, "", 22001, "qwen3-8b",
         "пустое имя оператора не в счёт — алиас ячейки, без пробелов"),
        ({"RUNNER": "whisper", "WHISPER_MODEL": "large-v3"}, "", 22024, "whisper-large-v3", "Whisper — по размеру"),
        ({"RUNNER": "Moonshine", "MOONSHINE_MODEL": "ru"}, "", 22025, "moonshine-ru", "Moonshine — по языку"),
        ({"RUNNER": "whisper", "MODEL_FILE": "models/w/ggml-base.gguf"}, "", 22024, "ggml-base",
         "negative: Whisper без размера — по файлу модели"),
        ({"WHISPER_MODEL": "large-v3", "MODEL_FILE": "models/a/b.gguf"}, "", 22024, "b",
         "negative: размер Whisper у ячейки другого раннера — не в счёт"),
        ({"MODEL_FILE": "models/org/model-q4.gguf"}, "", 22001, "model-q4", "GGUF — имя файла без .gguf"),
        ({"MODEL_FILE": "models/org/Big-Q8_0-00001-of-00003.gguf"}, "", 22001, "Big-Q8_0",
         "GGUF из частей — без «-00001-of-00003»: части — одна модель"),
        ({"MODEL_FILE": "/srv/m/Ws.GGUF"}, "", 22001, "Ws", "boundary: .GGUF заглавными — тоже GGUF"),
        ({}, "/home/u/llama.cpp/models/Qwen3-8B/Qwen/BF16", 22030, "Qwen3-8B",
         "папка внутри models — её первая часть после models"),
        ({}, "seamless-m4t-v2-large/facebook/FP32", 22003, "seamless-m4t-v2-large",
         "папка от корня моделей — её первая часть"),
        ({}, "/opt/weights/tts-ru/", 22009, "tts-ru", "папка вне models — её последняя часть"),
        ({"MODEL_FILE": "models/a.gguf"}, "/other/b.gguf", 22001, "a", "MODEL_FILE раньше пути из запроса"),
        ({}, "", 22003, "cell-22003", "не из чего — cell-<порт>: имя говорит, что его не дали"),
        (None, "", 0, "", "boundary: ни настроек, ни порта — пусто, не выдумано"),
    ]
    for config, path, port, want, claim in cases:
        got = ModelName.of(config, path, port)
        check(got == want, f"{claim} (got {got!r})")


def test_listing():
    CHECKS.section("список моделей машины:")
    s = make_scout()
    models = s.entry.models
    check(s.entry.listing() == {"object": "list", "data": []}, "клеток нет — список пуст, в форме OpenAI")
    s.cells.at(22099)
    check(models.cells_now() == [], "negative: порт, о котором только спросили (ни процесса, ни записи), — не модель")

    put(s, 22002, name="gemma", record={"startedAt": 900})
    put(s, 22001, record={"name": "gemma", "startedAt": 1000})
    put(s, 22005, state="stopped", phase="loading", name="qwen")
    put(s, 22006, state="crashed", record={"name": "broken-one", "startedAt": 500})
    put(s, 22007, state="stopped", phase="error", record={"name": "broken-two", "startedAt": 400})
    put(s, 22008, state="stopped", record={"startedAt": 300, "cfg": {"modelPath": "/x/models/org/old-q8.gguf"}})
    put(s, 22009, state="stopped", phase="loading", name="renamed", record={"name": "old-name", "startedAt": 200})
    s.autostart.set(22003, True, {"modelPath": "", "config": {"PORT": 22003, "ALIAS": "gigaam"}})
    s.autostart.set(22008, True, {"modelPath": "models/org/kept.gguf", "config": {"PORT": 22008}})
    got = {c["port"]: (c["name"], c["state"], c["since"]) for c in models.cells_now()}
    check(got == {22001: ("gemma", "running", 1000), 22002: ("gemma", "running", 900), 22003: ("gigaam", "stopped", None),
                  22005: ("qwen", "starting", None), 22006: ("broken-one", "failed", 500),
                  22007: ("broken-two", "failed", 400), 22008: ("kept", "stopped", 300),
                  22009: ("renamed", "starting", 200)},
          f"ячейки: работает, запускается, упала (процесс или фаза), остановлена — с запросом автозапуска (got {got})")
    check([c["port"] for c in models.cells_now()] == sorted(got), "по возрастанию порта")
    check(got[22009][0] == "renamed" and got[22008][0] == "kept",
          "имя идущего старта раньше записи прошлого запуска (ячейку запускают заново под другим именем); у ячейки "
          "без имени в записи — сохранённый запрос раньше пути в записи (2.26 и старше)")

    data = s.entry.listing()["data"]
    check([m["id"] for m in data] == ["gemma", "gigaam", "qwen", "broken-one", "broken-two", "kept", "renamed"],
          "модели по первому порту каждой")
    gemma = data[0]
    check(gemma == {"id": "gemma", "object": "model", "created": 900, "owned_by": "box-a",
                    "caravan": {"state": "running", "slots": None, "busy": None,
                                "copies": [{"port": 22001, "state": "running", "slots": None, "busy": None},
                                           {"port": 22002, "state": "running", "slots": None, "busy": None}]}},
          "две ячейки одной модели — её копии; created — старт первой, owned_by — имя машины; места копий, "
          "которые не сказали о себе, — None («не знаю»), и у модели тоже")
    check(data[1]["created"] == 0 and data[1]["caravan"]["state"] == "stopped",
          "ни разу не запущенная — created 0, состояние stopped")

    put(s, 22002, state="crashed", name="gemma")
    put(s, 22001, state="stopped", phase="loading", record={"name": "gemma", "startedAt": 1000})
    check(s.entry.listing()["data"][0]["caravan"]["state"] == "starting",
          "состояние модели — лучшее из копий: запускается лучше, чем упала")
    check(models.copies("gemma") == [{"port": 22001, "name": "gemma", "state": "starting", "since": 1000, "slots": None, "busy": None},
                                     {"port": 22002, "name": "gemma", "state": "failed", "since": 900, "slots": None, "busy": None}]
          and models.copies("nope") == [], "копии модели по имени; negative: чужое имя — ни одной")


def test_places():
    CHECKS.section("места модели (2.29):")
    s = make_scout()
    put(s, 22001, name="gemma", record={"startedAt": 1})
    put(s, 22002, name="gemma", record={"startedAt": 2})
    put(s, 22003, state="stopped", phase="loading", name="gemma")
    put(s, 22005, name="ear", record={"startedAt": 3})
    put(s, 22006, state="stopped", name="off", record={"startedAt": 4})
    s.cells.probe = FakeProbe(slots={22001: 4, 22002: 2, 22005: 1}, busy={22001: 1, 22002: 0})
    by_id = {m["id"]: m["caravan"] for m in s.entry.listing()["data"]}
    check((by_id["gemma"]["slots"], by_id["gemma"]["busy"]) == (6, 1)
          and by_id["gemma"]["copies"] == [{"port": 22001, "state": "running", "slots": 4, "busy": 1},
                                           {"port": 22002, "state": "running", "slots": 2, "busy": 0},
                                           {"port": 22003, "state": "starting", "slots": None, "busy": None}],
          "места модели — сумма работающих копий: 4 + 2 места, 1 занято; запускающаяся копия мест не даёт и их не "
          "спрашивают")
    check((by_id["ear"]["slots"], by_id["ear"]["busy"]) == (1, None),
          "сервер сказал число мест, но не занятость — занятость модели None, а не 0")
    check((by_id["off"]["slots"], by_id["off"]["busy"]) == (0, 0),
          "ни одна копия не работает — 0 мест и 0 занятых: это измерено, а не выдумано")
    s.cells.probe = FakeProbe(slots={22001: 4}, busy={22001: 1, 22002: 0})
    check(s.entry.listing()["data"][0]["caravan"]["slots"] is None,
          "negative: одна из работающих копий не сказала число мест — у модели None, не 4")
    check(s.entry.get("/v1/models/gemma")[0]["caravan"]["busy"] == 1, "GET /v1/models/<имя> несёт те же места")
    probe = s.cells.probe

    class Counting(FakeProbe):
        asked = []

        def slots_total(self, port):
            self.asked.append(port)
            return super().slots_total(port)
    s.cells.probe = Counting(slots=probe.slots, busy=probe.busy)
    s.entry.listing()
    check(sorted(Counting.asked) == [22001, 22002, 22005],
          "negative: у остановленной и запускающейся копии места не спрашивают")


def test_copies():
    CHECKS.section("какая копия берёт запрос:")
    asked = []

    def never(port):
        asked.append(port)
        return 0
    copies = Copies(never)
    check(copies.pick([]) is None and copies.pick([22001]) == 22001 and asked == [],
          "нет копий — никто; одна — она, и о нагрузке не спрошено")
    loads = {22001: 3, 22002: 1}
    copies = Copies(loads.get)
    check([copies.pick([22001, 22002]) for _ in range(4)] == [22002] * 4, "копия с меньшим числом запросов в работе")
    copies = Copies(lambda port: 0)
    check([copies.pick([22001, 22002]) for _ in range(4)] == [22001, 22002, 22001, 22002],
          "равные — по очереди: работа делится")
    for odd in (None, -2, "5", float("nan")):
        copies = Copies({22001: odd, 22002: 1}.get)
        check(copies.pick([22001, 22002]) == 22001, f"boundary: {odd!r} — не число запросов, считается нулём")


def test_request():
    CHECKS.section("запрос — какая модель в нём названа:")
    req = EntryRequest(b'{"model": " gemma ", "messages": [], "stream": true}', "application/json")
    check(req.model == "gemma", "JSON — поле model, без пробелов")
    for body, why in ((b'{"messages": []}', "без model"), (b"[1, 2]", "не объект"), (b"{oops", "не JSON"),
                      (b"", "без тела"), (b"\xff\xfe", "не UTF-8")):
        check(EntryRequest(body, "application/json").model == "", f"negative: тело {why} — модель не названа")
    check(EntryRequest(b'{"model": "gemma"}', "application/x-www-form-urlencoded").model == "gemma",
          "тип тела не JSON (curl -d) — всё равно читается как JSON")

    body = form([("language", "ru"), ("model", "whisper-large-v3"), ("response_format", "json")])
    req = EntryRequest(body, FORM_TYPE)
    check(req.model == "whisper-large-v3", "форма с записью — поле model")
    check(EntryRequest(form([("language", "ru")]), FORM_TYPE).model == "",
          "negative: файл с filename=\"model\" — не поле model")
    check(EntryRequest(body, f'multipart/form-data; boundary="{BOUNDARY}"').model == "whisper-large-v3",
          "граница формы в кавычках — тоже")
    check(EntryRequest(body, "multipart/form-data").model == "", "negative: форма без границы — модель не названа")

    req = EntryRequest(b'{"stream": true, "model": "gemma", "messages": [{"role": "user", "content": "\\u043f"}]}',
                       "application/json")
    renamed = req.naming("gemma-served")
    check(json.loads(renamed) == {"stream": True, "model": "gemma-served", "messages": [{"role": "user", "content": "п"}]}
          and list(json.loads(renamed)) == ["stream", "model", "messages"],
          "другое имя — JSON записан заново: model заменён, остальные поля и их порядок как были")
    check(req.naming("gemma") is req.body and req.naming("") is req.body,
          "то же имя или пустое — те же байты, не копия")
    plain = EntryRequest(b'{"messages": []}', "application/json")
    check(plain.naming("x") is plain.body, "negative: тело без model — не трогается")

    req = EntryRequest(body, FORM_TYPE)
    renamed = req.naming("large-v3")
    check(renamed == form([("language", "ru"), ("model", "large-v3"), ("response_format", "json")]),
          "форма: заменено только значение поля model — запись и остальные поля байт в байт")


def test_body():
    CHECKS.section("тело запроса — по длине или частями:")
    body = EntryBody(100)
    check(body.read({"Content-Length": "5"}, io.BytesIO(b"hello, and more")) == b"hello"
          and body.read({}, io.BytesIO(b"x")) == b"", "по длине — ровно столько; без длины — пусто")
    chunked = b"4;ext=1\r\nWiki\r\n6\r\npedia \r\nE\r\n in\r\n\r\nchunks.\r\n0\r\nX-Trailer: t\r\n\r\nNEXT"
    rfile = io.BytesIO(chunked)
    check(body.read({"Transfer-Encoding": "chunked"}, rfile) == b"Wikipedia  in\r\n\r\nchunks." and rfile.read() == b"NEXT",
          "частями (curl из канала) — части склеены, расширения и хвост пропущены, лишнего не прочитано")

    def refused(headers, raw, limit=100):
        try:
            EntryBody(limit).read(headers, io.BytesIO(raw))
        except (EntryBody.TooLong, EntryBody.Malformed) as exc:
            return type(exc).__name__, str(exc)
        return None
    check(refused({"Content-Length": "101"}, b"") == ("TooLong", ""), "длина больше предела — отказ, тело не читается")
    check(refused({"Content-Length": "100"}, b"x" * 100) is None, "boundary: ровно предел — читается")
    check(refused({"Transfer-Encoding": "chunked"}, b"40\r\n" + b"x" * 64 + b"\r\n40\r\n" + b"x" * 64, 100)
          == ("TooLong", ""), "частями больше предела — отказ на той части, что его переходит")
    for headers, raw, why in (({"Content-Length": "abc"}, b"", "Content-Length is not a number"),
                              ({"Content-Length": "-1"}, b"", "Content-Length is negative"),
                              ({"Transfer-Encoding": "chunked"}, b"zz\r\n", "a chunk's size is not a number"),
                              ({"Transfer-Encoding": "chunked"}, b"-5\r\n", "a chunk's size is negative"),
                              ({"Transfer-Encoding": "chunked"}, b"10\r\nshort", "the body ended inside a chunk"),
                              ({"Transfer-Encoding": "chunked"}, b"", "a chunk's size is not a number")):
        check(refused(headers, raw) == ("Malformed", why), f"negative: {why} — тело не читается")
    check(ScoutHandler.ENTRY_BODY_MAX == 256 << 20, "предел — 256 МиБ: два часа записи WAV 16 кГц")


def test_served_names():
    CHECKS.section("имена, на которые отвечает ячейка:")
    check(ServedNames.parse(b'{"data": [{"id": "model-q4.gguf", "aliases": ["gemma", "g"]}, {"id": "gemma"}, {"x": 1}]}')
          == ("model-q4.gguf", "gemma", "g"),
          "id и алиасы llama-server, каждое один раз, по порядку сервера")
    for raw, why in ((b"[]", "не объект"), (b'{"data": {}}', "data не список"), (b"<html>", "не JSON"),
                     (b'{"data": [{"id": "  "}]}', "пустое id")):
        check(ServedNames.parse(raw) == (), f"negative: {why} — имён нет")

    answers, calls = [], []

    class Conn:
        def __init__(self, port, timeout):
            calls.append((port, timeout))

        def request(self, method, path):
            self.asked = (method, path)

        def getresponse(self):
            status, raw = answers.pop(0)
            if isinstance(status, BaseException):
                raise status

            class Answer:
                pass
            a = Answer()
            a.status, a.read = status, (lambda: raw)
            return a

        def close(self):
            pass

    names = ServedNames(Conn)
    answers[:] = [(200, b'{"data": [{"id": "qwen3-8b"}]}')]
    check(names.of(22001, 1000) == ("qwen3-8b",) and calls == [(22001, 2)], "ответ 200 — имена; спрошено с таймаутом 2 с")
    check(names.of(22001, 1000) == ("qwen3-8b",) and len(calls) == 1, "второй раз за тот же запуск — не спрашивает")
    answers[:] = [(200, b'{"data": [{"id": "other"}]}')]
    check(names.of(22001, 2000) == ("other",) and len(calls) == 2, "ячейку запустили заново — спрошено заново")
    answers[:] = [(404, b"")]
    check(names.of(22003, 1) == () and names.of(22003, 1) == () and len(calls) == 3,
          "404 (ячейка каравана) — имён нет, и это запомнено")
    answers[:] = [(503, b""), (200, b'{"data": [{"id": "late"}]}')]
    check(names.of(22004, 1) is None and names.of(22004, 1) == ("late",) and len(calls) == 5,
          "503 (загружается) — неизвестно, не запомнено: спрошено снова")
    answers[:] = [(ConnectionRefusedError("refused"), b"")]
    check(names.of(22005, 1) is None, "negative: не ответила — неизвестно, а не «имён нет»")


def scout_with(cells, config=None):
    s = make_scout(config)
    for port, kwargs in cells.items():
        put(s, port, **kwargs)
    return s


def test_refusals():
    CHECKS.section("отказы входа — в форме OpenAI:")
    s = scout_with({22001: {"name": "gemma", "record": {"startedAt": 1}},
                    22003: {"state": "stopped", "phase": "loading", "name": "qwen"},
                    22004: {"state": "crashed", "name": "qwen", "record": {"startedAt": 2}}},
                   config={"controllerToken": "t0k3n"})
    with Served(s) as srv:
        status, payload = srv.get("/v1/models")
        check(status == 200 and [m["id"] for m in payload["data"]] == ["gemma", "qwen"],
              "GET /v1/models — без токена флота, хотя скаут его держит: вход открыт, как ячейки")
        check(srv.get("/api/state")[0] == 401, "negative: остальное API скаута — под токеном, как было")
        check(srv.get("/v1/models?x=1")[0] == 200, "запрос в пути списку не мешает")
        check(srv.get("/v1/models/qwen") == (200, payload["data"][1]), "GET /v1/models/<имя> — модель, как в списке")
        check(srv.get("/v1/models/no%20such") == (404, {"error": {
            "message": "The model `no such` does not exist on this machine", "type": "invalid_request_error",
            "param": "model", "code": "model_not_found"}}), "чужое имя — 404 model_not_found; имя из пути раскодировано")
        check(srv.get("/v1/files")[0] == 404 and srv.get("/v1/files")[1]["error"]["param"] is None,
              "negative: другой GET под /v1 — 404 в форме OpenAI")
        status, payload = srv.post("/v1/chat/completions", {"messages": []})
        check((status, payload) == (400, {"error": {"message": "You must provide a model parameter",
                                                    "type": "invalid_request_error", "param": "model", "code": None}}),
              "без model — 400, как отвечает OpenAI")
        status, payload = srv.post("/v1/chat/completions", {"model": "llama-70b"})
        check(status == 404 and payload["error"]["code"] == "model_not_found", "чужая модель — 404 model_not_found")
        status, payload = srv.post("/v1/chat/completions", {"model": "qwen"})
        check(status == 503 and payload["error"] == {
            "message": "The model `qwen` is not running on this machine (:22003 starting, :22004 failed)",
            "type": "server_error", "param": "model", "code": "model_not_running"},
            "модель без работающей копии — 503 model_not_running, с состоянием каждой копии")

        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
        conn.putrequest("POST", "/v1/audio/transcriptions")
        conn.putheader("Content-Length", str((256 << 20) + 1))
        conn.endheaders()
        resp = conn.getresponse()
        answer = json.loads(resp.read() or b"{}")
        conn.close()
        check(resp.status == 413 and answer["error"]["message"] == "The request body is over 268435456 bytes",
              "тело больше 256 МиБ — 413, не читая его")
        status, payload = srv.post("/v1/chat/completions", raw=b"zz\r\n{}\r\n0\r\n\r\n",
                                   headers={"Transfer-Encoding": "chunked"})
        check(status == 400 and payload["error"]["message"] == "The request body cannot be read: a chunk's size is not a number",
              "тело частями с кривым размером части — 400, со словами почему")

    s = scout_with({22001: {"name": "gemma", "record": {"startedAt": 1}}})
    wire(s, {22001: closed_port()})
    with Served(s) as srv:
        status, payload = srv.post("/v1/chat/completions", {"model": "gemma"})
        check(status == 502 and payload["error"]["code"] == "cell_unreachable"
              and payload["error"]["message"].startswith("The cell of `gemma` on :22001 did not answer:"),
              "ячейка не отвечает — 502 cell_unreachable, с портом")


def test_through():
    CHECKS.section("запрос насквозь — до ячейки и обратно:")
    cell = FakeCell(names=[{"id": "model-q4.gguf", "aliases": ["gemma"]}])
    s = scout_with({22001: {"name": "gemma", "record": {"startedAt": 1}}})
    wire(s, {22001: cell.port})
    body = b'{"model": "gemma", "messages": [{"role": "user", "content": "hi"}]}'
    with Served(s) as srv:
        status, headers, data = call(srv, "POST", "/v1/chat/completions?trace=1", body, {
            "Content-Type": "application/json", "Authorization": "Bearer sk-client", "X-Request-Id": "r-1",
            "Connection": "close, X-Hop", "X-Hop": "1", "Expect": "100-continue"})
    seen = cell.seen[0] if cell.seen else {}
    sent = {k.lower(): v for k, v in seen.get("headers", [])}
    check(status == 200 and json.loads(data) == {"object": "chat.completion", "got": len(body)},
          "ответ ячейки — клиенту как есть")
    check(seen.get("path") == "/v1/chat/completions?trace=1" and seen.get("body") == body,
          "ячейке — тот же путь с запросом и те же байты тела: имя ячейке знакомо (алиас llama-server)")
    check(sent.get("x-request-id") == "r-1" and sent.get("content-type") == "application/json"
          and sent.get("content-length") == str(len(body)) and sent.get("host") == f"127.0.0.1:{cell.port}",
          "заголовки клиента дошли; длина — тела, что ушло; Host — ячейки")
    check("authorization" not in sent and "x-hop" not in sent and "expect" not in sent,
          "negative: ключ клиента, заголовок одного перехода (назван в Connection) и Expect ячейке не переданы")
    check(header(headers, "X-Cell") == ["yes"] and header(headers, "Content-Length") == [str(len(data))]
          and header(headers, "Keep-Alive") == [] and header(headers, "Server") != ["fake-cell"]
          and len(header(headers, "Server")) == 1 and len(header(headers, "Date")) == 1,
          "заголовки ячейки — клиенту, длина ответа сохранена; negative: Keep-Alive, Server и Date ячейки не "
          "передаются — у ответа один Server и один Date, скаута")

    cell.mode = "refused"
    with Served(s) as srv:
        status, _headers, data = call(srv, "POST", "/v1/chat/completions", body, {"Content-Type": "application/json"})
    check(status == 400 and json.loads(data)["error"]["message"] == "bad request",
          "отказ ячейки — клиенту как есть: её статус и её слова")
    cell.close()

    vllm = FakeCell(names=[{"id": "qwen3-8b"}])
    s = scout_with({22030: {"name": "Qwen3-8B", "record": {"startedAt": 7}}})
    wire(s, {22030: vllm.port})
    with Served(s) as srv:
        call(srv, "POST", "/v1/chat/completions", b'{"model": "Qwen3-8B", "stream": false}',
             {"Content-Type": "application/json"})
        call(srv, "POST", "/v1/chat/completions", b'{"model": "Qwen3-8B"}', {"Content-Type": "application/json"})
    got = [json.loads(x["body"]) for x in vllm.seen]
    check(got == [{"model": "qwen3-8b", "stream": False}, {"model": "qwen3-8b"}],
          "ячейка отвечает на другое имя (vLLM: --served-model-name) — model передан её словами")
    check(vllm.asked_names == 1, "имена ячейки спрошены один раз за её запуск")
    vllm.close()

    stt = FakeCell(names=[{"id": "large-v3"}])
    s = scout_with({22024: {"name": "whisper-large-v3", "record": {"startedAt": 3}}})
    wire(s, {22024: stt.port})
    recording = bytes(range(256)) * 64
    with Served(s) as srv:
        status, _h, _d = call(srv, "POST", "/v1/audio/transcriptions",
                              form([("model", "whisper-large-v3"), ("language", "ru")], recording),
                              {"Content-Type": FORM_TYPE})
    check(status == 200 and stt.seen and stt.seen[0]["body"] == form([("model", "large-v3"), ("language", "ru")], recording),
          "запись формой: поле model — словами ячейки, запись и остальные поля — байт в байт")
    stt.close()

    own = FakeCell(names=None)
    s = scout_with({22003: {"name": "gigaam", "record": {"startedAt": 4}}})
    wire(s, {22003: own.port})
    with Served(s) as srv:
        call(srv, "POST", "/v1/audio/transcriptions", form([("model", "gigaam")]), {"Content-Type": FORM_TYPE})
    check(own.seen and own.seen[0]["body"] == form([("model", "gigaam")]),
          "negative: ячейка каравана имён не называет (404) — запрос уходит как пришёл")
    with Served(s) as srv:
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
        conn.request("POST", "/v1/audio/transcriptions", body=iter([form([("model", "gigaam")])[:40],
                                                                     form([("model", "gigaam")])[40:]]),
                     headers={"Content-Type": FORM_TYPE}, encode_chunked=True)
        resp = conn.getresponse()
        resp.read()
        conn.close()
    sent = {k.lower(): v for k, v in own.seen[-1]["headers"]} if own.seen else {}
    check(resp.status == 200 and len(own.seen) == 2 and own.seen[-1]["body"] == form([("model", "gigaam")])
          and "transfer-encoding" not in sent and sent.get("content-length") == str(len(form([("model", "gigaam")]))),
          "тело частями (chunked) — дошло до ячейки целым, с длиной, без chunked")
    own.close()

    one, two = FakeCell(), FakeCell()
    s = scout_with({22001: {"name": "gemma", "record": {"startedAt": 1}},
                    22002: {"name": "gemma", "record": {"startedAt": 2}},
                    22005: {"name": "gemma", "state": "stopped", "phase": "loading"}})
    wire(s, {22001: one.port, 22002: two.port}, loads={22001: 4, 22002: 0})
    with Served(s) as srv:
        for _ in range(3):
            call(srv, "POST", "/v1/embeddings", b'{"model": "gemma", "input": "x"}', {"Content-Type": "application/json"})
    check(len(one.seen) == 0 and len(two.seen) == 3,
          "две работающие копии — запросы у менее занятой; negative: запускающаяся копия запросов не берёт")
    one.close()
    two.close()


def test_stream():
    CHECKS.section("поток — как поток:")
    cell = FakeCell(names=[{"id": "gemma"}], mode="stream")
    s = scout_with({22001: {"name": "gemma", "record": {"startedAt": 1}}})
    wire(s, {22001: cell.port})
    with Served(s) as srv:
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model": "gemma", "stream": true}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        first = resp.read1(65536)
        second_sent_before = cell.second_sent
        cell.gate.set()
        rest = resp.read()
        conn.close()
    check(resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
          and resp.getheader("Content-Length") is None and resp.getheader("Transfer-Encoding") is None,
          "поток: тип ячейки, без длины и без chunked к клиенту — ответ до закрытия соединения")
    check(first == b"data: one\n\n" and not second_sent_before,
          "первое событие пришло клиенту, пока ячейка ещё держит второе: скаут не копит ответ")
    check(rest == b"data: [DONE]\n\n", "остальное — до конца, без служебных байтов chunked")
    cell.close()

    broken = FakeCell(names=[{"id": "gemma"}], mode="broken")
    s = scout_with({22001: {"name": "gemma", "record": {"startedAt": 1}}})
    wire(s, {22001: broken.port})
    with Served(s) as srv:
        status, _h, data = call(srv, "POST", "/v1/chat/completions", b'{"model": "gemma", "stream": true}',
                                {"Content-Type": "application/json"})
        alive = srv.get("/v1/models")[0]
    check(status == 200 and data == b"data: one\n\n",
          "ячейка оборвалась посреди ответа — ответ кончается там, где оборвался; negative: второго ответа "
          "(500 с ошибкой) в теле нет")
    check(alive == 200, "скаут после обрыва отвечает дальше")
    broken.close()


def test_wiring():
    CHECKS.section("вход в самом скауте:")
    s = make_scout()
    check(isinstance(s.entry, MachineEntry) and isinstance(s.entry.models, MachineModels)
          and s.entry.models.host_id() == "box-a", "у скаута есть вход; owned_by — его имя машины")
    with patched(s.cells.probe, metrics=lambda port: {"requestsProcessing": {22001: 5, 22002: 0}[port]}):
        check(s.entry.copies.pick([22001, 22002]) == 22002,
              "нагрузка копии — из /metrics ячейки (requestsProcessing), как у отчёта скаута")
    check(MachineEntry.passed([("Connection", "close, X-Hop"), ("X-Hop", "1"), ("TE", "trailers"),
                               ("Content-Type", "a"), ("authorization", "k")], frozenset({"content-type"})) == [],
          "заголовки одного перехода, названные в Connection, Authorization и `also` — без учёта регистра")


if __name__ == "__main__":
    for test in (test_model_name, test_listing, test_places, test_copies, test_request, test_body, test_served_names, test_refusals,
                 test_through, test_stream, test_wiring):
        try:
            test()
        except Exception as exc:  # noqa: BLE001 — a crash is a red line, not the end of the file
            CHECKS.check(False, f"{test.__name__} упал: {exc!r}")
    sys.exit(CHECKS.finish())
