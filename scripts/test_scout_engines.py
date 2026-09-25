#!/usr/bin/env python3
"""Snapshot of the engines next to the cells (2.12): caravan_scout/engines.py.

Ollama and LM Studio on the scout's machine — found by their usual ports and
by the names of the processes that listen, read through their own APIs (GET
only), their processes and memory — and the report that carries them. Every
answer comes from a fake: the engines' replies are a table of (port, path),
listeners and the process table are stand-ins, and the one pin that speaks
real HTTP opens the network door for its own server on an ephemeral port.

Run: python3 scripts/test_scout_engines.py
"""
import contextlib
import io
import json
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import BLOCKED, REAL, Checks, RealCallBlocked, Served, make_scout, patched  # noqa: E402

from caravan_scout.engines import EngineAsk, EngineCall, ForeignEngines, LmStudio, Ollama  # noqa: E402
from caravan_scout.errors import AppError  # noqa: E402

CHECKS = Checks("scout engines")
check = CHECKS.check


def same(actual, expected, msg):
    ok = actual == expected
    check(ok, msg)
    if not ok:
        print(f"        got:  {actual!r}\n        want: {expected!r}")


def dig(value, *path):
    for key in path:
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError):
            return None
    return value


class Answers:
    """An engine's replies by path: (status, payload); a path it does not
    know is a 404. `asked` keeps every path, in order."""

    def __init__(self, table=None, silent=False):
        self.table = dict(table or {})
        self.silent = silent
        self.asked = []

    def __call__(self, path):
        self.asked.append(path)
        if self.silent:
            return None, None
        return self.table.get(path, (404, None))


class Fleet:
    """The fake machine a ForeignEngines scans: engines' replies by port,
    what listens, the process table and the scout's own cells."""

    def __init__(self, engines=None, listeners=None, processes=None, cells=(), firewalls=None):
        self.engines = {int(k): v for k, v in (engines or {}).items()}
        self.listen = listeners
        self.procs = processes
        self.cell_ports = list(cells)
        self.firewalls = dict(firewalls or {})
        self.asked = []   # (port, host)
        self.fw_asked = []

    def firewall(self, port):
        self.fw_asked.append(int(port))
        return self.firewalls.get(int(port), {"state": "unknown"})

    def listeners(self):
        return self.listen if self.listen is not None else {"ok": False, "error": "ss: ss", "ports": []}

    def processes(self):
        self.proc_reads = getattr(self, "proc_reads", 0) + 1   # a full scan reads it; an act does not
        return self.procs

    def all(self):
        return [(port, object()) for port in self.cell_ports]

    def ask(self, port, host="127.0.0.1"):
        self.asked.append((int(port), host))
        return self.engines.get(int(port), Answers(silent=True))

    def scanner(self):
        return ForeignEngines(self, self, ask=self.ask)


# ── the engines' own words ──────────────────────────────────────────────────

OLLAMA_DETAILS = {"format": "gguf", "family": "qwen3", "parameter_size": "8.2B", "quantization_level": "Q4_K_M"}
OLLAMA_PS = {"models": [{"name": "qwen3:8b", "model": "qwen3:8b", "size": 6_591_830_464,
                         "size_vram": 5_333_539_264, "context_length": 4096,
                         "expires_at": "2026-09-22T17:00:00+00:00", "details": OLLAMA_DETAILS}]}
OLLAMA_TAGS = {"models": [
    {"name": "qwen3:8b", "model": "qwen3:8b", "size": 5_225_388_164, "details": OLLAMA_DETAILS},
    {"name": "nomic-embed-text:latest", "size": 274_302_450,
     "details": {"format": "gguf", "family": "nomic-bert", "parameter_size": "137M",
                 "quantization_level": "F16"}},
    {"name": "gpt-oss:120b-cloud", "size": 384, "remote_host": "https://ollama.com:443",
     "details": {"format": "", "family": "gptoss", "parameter_size": "116.8B", "quantization_level": "MXFP4"}},
    {"name": "", "size": 1}, "not a model"]}


def ollama(ps=(200, OLLAMA_PS), tags=(200, OLLAMA_TAGS), version=(200, {"version": "0.12.3"})):
    return Answers({"/api/version": version, "/api/ps": ps, "/api/tags": tags})


LMS_V1 = {"models": [
    {"type": "llm", "publisher": "google", "key": "google/gemma-3-4b", "display_name": "Gemma 3 4B",
     "architecture": "gemma3", "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
     "size_bytes": 3_340_000_000, "params_string": "4B", "max_context_length": 131072, "format": "gguf",
     "loaded_instances": [{"id": "google/gemma-3-4b", "config": {"context_length": 8192, "parallel": 4}},
                          {"id": "google/gemma-3-4b:2", "config": {"context_length": 4096}}]},
    {"type": "embedding", "key": "text-embedding-nomic", "format": "gguf", "size_bytes": 84_000_000,
     "quantization": "Q8_0", "loaded_instances": [],
     "config_note": "a model-level max is not said"},
    {"type": "llm", "key": "mlx-community/qwen3-4b", "format": "mlx", "size_bytes": 2_000_000_000,
     "loaded_instances": [{"id": "qwen-fast", "config": {"context_length": 16384, "max_context_length": 40960}}]},
    {"type": "llm", "display_name": "no key — no model"}]}

LMS_V0 = {"object": "list", "data": [
    {"id": "qwen2-vl-7b-instruct", "object": "model", "type": "vlm", "publisher": "mlx-community",
     "arch": "qwen2_vl", "compatibility_type": "mlx", "quantization": "4bit", "state": "loaded",
     "max_context_length": 32768, "loaded_context_length": 8192},
    {"id": "granite-3.0-2b-instruct", "object": "model", "type": "llm", "arch": "granite",
     "compatibility_type": "gguf", "quantization": "Q4_K_M", "state": "not-loaded", "max_context_length": 4096}]}


def test_ollama_read():
    CHECKS.section("Ollama — что он говорит о себе:")
    ask = ollama()
    seen = Ollama().read(ask)
    same((dig(seen, "state"), dig(seen, "version"), "installedKnown" in (seen or {})), ("ok", "0.12.3", False),
         "/api/version доказывает, что это Ollama; версия — как он её назвал")
    same(ask.asked, ["/api/version", "/api/ps", "/api/tags"], "спрашивает версию, загруженные и установленные — "
         "и больше ничего")
    same([m["name"] for m in dig(seen, "models") or []],
         ["qwen3:8b", "nomic-embed-text:latest", "gpt-oss:120b-cloud"],
         "модели — по списку установленных; без имени и не словарь — пропущены")
    same(dig(seen, "models", 0), {
        "name": "qwen3:8b", "type": "", "format": "gguf", "family": "qwen3", "params": "8.2B", "quant": "Q4_K_M",
        "fileBytes": 5_225_388_164, "remote": False, "loaded": True, "memBytes": 6_591_830_464,
        "vramBytes": 5_333_539_264, "contextLength": 4096, "maxContextLength": None,
        "expiresAt": "2026-09-22T17:00:00+00:00", "instances": None},
         "загруженная: файл из списка установленных, память и из неё VRAM, окно и срок выгрузки — из /api/ps")
    same({k: dig(seen, "models", 1, k) for k in ("loaded", "memBytes", "vramBytes", "contextLength", "fileBytes")},
         {"loaded": False, "memBytes": None, "vramBytes": None, "contextLength": None, "fileBytes": 274_302_450},
         "negative: не загружена — памяти и окна нет (None, а не 0), размер файла есть")
    same((dig(seen, "models", 2, "remote"), dig(seen, "models", 0, "remote")), (True, False),
         "облачная модель Ollama (remote_host) помечена: она работает не на этой машине")

    seen = Ollama().read(ollama(tags=(500, None)))
    same((dig(seen, "installedKnown"), [m["name"] for m in dig(seen, "models") or []],
          dig(seen, "models", 0, "fileBytes"), dig(seen, "models", 0, "memBytes")),
         (False, ["qwen3:8b"], None, 6_591_830_464),
         "negative: список установленных не ответил — installedKnown false, загруженная всё равно названа; "
         "размер из /api/ps — это память, а не файл: fileBytes нет")
    seen = Ollama().read(ollama(ps=(500, None)))
    same(sorted({dig(m, "loaded") for m in dig(seen, "models") or []}, key=str), [None],
         "negative: /api/ps не ответил — «загружена ли» неизвестно (None) у всех, а не «не загружена»")
    same(Ollama().read(ollama(version=(401, None))), {"state": "auth"}, "401 — хочет токен")
    same(Ollama().read(Answers(silent=True)), {"state": "unreachable"}, "никто не ответил — unreachable")
    same(Ollama().read(ollama(version=(404, None))), None,
         "negative: /api/version — 404 (так отвечает llama-server) — это не Ollama")
    same(Ollama().read(ollama(version=(200, {"build": 1}))), None,
         "negative: 200 без поля version — не Ollama, а не «Ollama без версии»")


def test_lmstudio_read():
    CHECKS.section("LM Studio — что он говорит о себе:")
    ask = Answers({"/api/v1/models": (200, LMS_V1)})
    seen = LmStudio().read(ask)
    same((dig(seen, "state"), dig(seen, "api"), dig(seen, "version")), ("ok", "v1", ""),
         "родной API 0.4+ (/api/v1/models); версии он не говорит — пусто, не выдумано")
    same(ask.asked, ["/api/v1/models"], "одного списка достаточно — v0 не спрашивается")
    same([m["name"] for m in dig(seen, "models") or []],
         ["google/gemma-3-4b", "text-embedding-nomic", "mlx-community/qwen3-4b"],
         "имя модели — её key (им её зовёт запрос); без key — пропущена")
    same(dig(seen, "models", 0), {
        "name": "google/gemma-3-4b", "type": "llm", "format": "gguf", "family": "gemma3", "params": "4B",
        "quant": "Q4_K_M", "fileBytes": 3_340_000_000, "remote": False, "loaded": True, "memBytes": None,
        "vramBytes": None, "contextLength": 8192, "maxContextLength": 131072, "expiresAt": "", "instances": 2},
         "загружена двумя экземплярами: окно — первого, потолок окна — модели; памяти LM Studio не говорит — None")
    same({k: dig(seen, "models", 1, k) for k in ("loaded", "contextLength", "maxContextLength", "quant",
                                                 "instances", "type")},
         {"loaded": False, "contextLength": None, "maxContextLength": None, "quant": "Q8_0", "instances": 0,
          "type": "embedding"},
         "negative: не загружена — окна нет; квантование строкой тоже читается; эмбеддинги названы как есть")
    same((dig(seen, "models", 2, "contextLength"), dig(seen, "models", 2, "maxContextLength")), (16384, 40960),
         "boundary: потолок окна, сказанный только в конфиге экземпляра, — тоже потолок")

    ask = Answers({"/api/v0/models": (200, LMS_V0)})
    seen = LmStudio().read(ask)
    same((dig(seen, "api"), ask.asked), ("v0", ["/api/v1/models", "/api/v0/models"]),
         "0.3 без /api/v1 — старый /api/v0")
    same([(m["name"], m["loaded"], m["contextLength"], m["maxContextLength"], m["format"])
          for m in dig(seen, "models") or []],
         [("qwen2-vl-7b-instruct", True, 8192, 32768, "mlx"), ("granite-3.0-2b-instruct", False, None, 4096, "gguf")],
         "v0: загружена ли — из state, окно — только у загруженной")
    plain = Answers({"/api/v0/models": (200, {"data": [{"id": "some-model", "object": "model"}]})})
    same(LmStudio().read(plain), None,
         "negative: обычный OpenAI-список без state — не LM Studio (свой знак — state у каждой модели)")
    same(LmStudio().read(Answers({"/api/v1/models": (401, None)})), {"state": "auth"}, "401 — хочет токен")
    same(LmStudio().read(Answers(silent=True)), {"state": "unreachable"}, "никто не ответил — unreachable")
    same(LmStudio().read(Answers({"/api/v1/models": (200, {"data": []})})), None,
         "negative: ответ не того вида и v0 нет — не LM Studio")


LISTEN = {"ok": True, "ports": [
    {"port": 1234, "proc": "LM Studio", "pid": 613, "addrs": ["127.0.0.1"]},
    {"port": 11434, "proc": "", "pid": 0, "addrs": ["0.0.0.0", "[::]"]},
    {"port": 22001, "proc": "llama-server", "pid": 4242, "addrs": ["0.0.0.0"]},
    {"port": 41000, "proc": "ollama", "pid": 5100, "addrs": ["203.0.113.20"]},
    {"port": 8092, "proc": "python3", "pid": 900, "addrs": ["0.0.0.0"]}]}
PROCS = {1: {"ppid": 0, "rssKb": 10, "name": "systemd"},
         5100: {"ppid": 1, "rssKb": 204_800, "name": "ollama"},
         5151: {"ppid": 5100, "rssKb": 1_048_576, "name": "ollama"},
         5152: {"ppid": 5151, "rssKb": 1_024, "name": "cuda-helper"},
         613: {"ppid": 1, "rssKb": 409_600, "name": "LM Studio"},
         614: {"ppid": 613, "rssKb": 102_400, "name": "LM Studio Helper (GPU)"},
         700: {"ppid": 1, "rssKb": 51_200, "name": "llmster"},
         4242: {"ppid": 900, "rssKb": 2_097_152, "name": "llama-server"},
         900: {"ppid": 1, "rssKb": 30_000, "name": "python3"}}


def test_scan():
    CHECKS.section("поиск движков на машине:")
    fleet = Fleet(engines={11434: ollama(), 1234: Answers({"/api/v1/models": (200, LMS_V1)}),
                           22001: ollama(), 41000: ollama()},
                  listeners=json.loads(json.dumps(LISTEN)), processes={k: dict(v) for k, v in PROCS.items()},
                  cells=[22001])
    views = fleet.scanner().scan()
    same([(v["kind"], v["port"], v["state"]) for v in views],
         [("ollama", 11434, "ok"), ("ollama", 41000, "ok"), ("lmstudio", 1234, "ok")],
         "Ollama на своём порту (владелец не виден — служба чужого пользователя) и на порту процесса ollama; "
         "LM Studio на своём")
    same(sorted(p for p, _h in fleet.asked), [1234, 11434, 41000],
         "negative: порт своей ячейки не спрашивается, хоть и отвечает как Ollama; чужие порты (8092) — тоже")
    same(dict(fleet.asked), {11434: "127.0.0.1", 41000: "203.0.113.20", 1234: "127.0.0.1"},
         "спрашивается по адресу, на котором слушает: 0.0.0.0 и 127.0.0.1 — через 127.0.0.1, "
         "привязанный к одному адресу — по нему")
    same([(v["kind"], v["listen"]) for v in views],
         [("ollama", "network"), ("ollama", "network"), ("lmstudio", "loopback")],
         "где принимает соединения: вся сеть или только эта машина")
    same({v["port"]: (v["pids"], v["ramBytes"]) for v in views},
         {11434: ([5100, 5151, 5152], (204_800 + 1_048_576 + 1_024) * 1024),
          41000: ([5100, 5151, 5152], (204_800 + 1_048_576 + 1_024) * 1024),
          1234: ([613, 614, 700], (409_600 + 102_400 + 51_200) * 1024)},
         "процессы движка: по имени и все их потомки (раннер, помощник) — as-is: два Ollama одной машины "
         "делят процессы своего имени; RAM — сумма RSS")
    same(sorted(views[2]), ["api", "controls", "firewall", "kind", "label", "listen", "models", "pids", "port",
                            "ramBytes", "state", "version"],
         "вид движка: вид, имя, порт, где слушает, файрвол, что с ним можно делать, версия, модели, процессы, память")

    print("файрвол у порта движка (2.13):")
    fw = Fleet(engines={11434: ollama(), 41000: ollama(), 1234: Answers({"/api/v1/models": (200, LMS_V1)})},
               listeners=json.loads(json.dumps(LISTEN)), processes={},
               firewalls={11434: {"state": "blocked", "allowedFrom": []},
                          41000: {"state": "restricted", "allowedFrom": ["10.0.0.0/24"]}})
    views = fw.scanner().scan()
    same({v["port"]: v["firewall"] for v in views},
         {11434: {"state": "blocked", "allowedFrom": []}, 41000: {"state": "restricted", "allowedFrom": ["10.0.0.0/24"]},
          1234: None},
         "слушает сеть — сказано, кого ufw пускает на его порт, как у ячейки: никого, или только этих")
    same(sorted(fw.fw_asked), [11434, 41000],
         "negative: движок только на 127.0.0.1 — ufw не спрашивается: снаружи к нему и так не попасть")

    for name, answer, proc, port, shown in (
            ("хочет токен, процесс его", {"/api/v1/models": (401, None)}, "LM Studio", 1234, True),
            ("хочет токен, процесс не виден", {"/api/v1/models": (401, None)}, "", 1234, False),
            ("молчит на своём порту, процесс его", None, "LM Studio", 1234, True),
            ("молчит на своём порту, процесс не виден", None, "", 1234, False),
            ("молчит на чужом порту, процесс его", None, "LM Studio", 5000, False)):
        fleet = Fleet(engines={port: Answers(answer) if answer else Answers(silent=True)},
                      listeners={"ok": True, "ports": [{"port": port, "proc": proc, "pid": 613 if proc else 0,
                                                        "addrs": ["127.0.0.1"]}]},
                      processes={})
        views = fleet.scanner().scan()
        same(bool(views), shown, ("" if shown else "negative: ") + f"{name} — "
             + ("показан" if shown else "не показан: это была бы догадка"))
        if views:
            same((views[0]["state"], views[0]["models"], views[0]["version"]),
                 ("auth" if answer else "unreachable", None, ""),
                 f"{name}: моделей не назвал — None («не знаю»), а не [] («нет моделей»)")

    blind = Fleet(engines={11434: ollama()}, listeners=None, processes=None)
    views = blind.scanner().scan()
    same((blind.asked, [(v["port"], v["listen"], v["pids"], v["ramBytes"]) for v in views]),
         ([(11434, "127.0.0.1"), (1234, "127.0.0.1")], [(11434, "", [], None)]),
         "ОС не сказала, кто слушает, — обычные порты спрашиваются вслепую; где слушает — «» (не знаю), "
         "памяти нет — None, а не 0")
    ours = Fleet(engines={11434: ollama()}, processes={}, cells=[11434],
                 listeners={"ok": True, "ports": [{"port": 11434, "proc": "llama-server", "pid": 4242,
                                                   "addrs": ["0.0.0.0"]}]})
    same((ours.scanner().scan(), ours.asked), ([], []),
         "negative: своя ячейка на обычном порту Ollama не спрашивается и не становится чужим движком")
    busy = Fleet(engines={1234: Answers({"/api/v1/models": (200, LMS_V1)})}, listeners=None, processes=None,
                 cells=[1234])
    same((busy.scanner().scan(), [p for p, _h in busy.asked]), ([], [11434]),
         "negative: вслепую тоже — порт своей ячейки не спрашивается")


def test_scope_and_host():
    CHECKS.section("где слушает и как до него достучаться:")
    cases = [(["127.0.0.1"], "loopback", "127.0.0.1"), (["[::1]", "127.0.0.1"], "loopback", "127.0.0.1"),
             (["[::1]"], "loopback", "[::1]"), (["0.0.0.0"], "network", "127.0.0.1"),
             (["*"], "network", "127.0.0.1"), (["[::]"], "network", "127.0.0.1"),
             (["203.0.113.20"], "network", "203.0.113.20"), (["[fe80::1%eth0]"], "network", "[fe80::1]"),
             (["127.0.0.53%lo"], "loopback", "127.0.0.1"), ([], "", "127.0.0.1"),
             (["127.0.0.1", "203.0.113.20"], "network", "127.0.0.1")]
    got = [(ForeignEngines.scope({"addrs": a}), ForeignEngines.ask_host({"addrs": a})) for a, _s, _h in cases]
    same(got, [(s, h) for _a, s, h in cases],
         "loopback — только если ВСЕ адреса петлевые; спрашивать — по петле, когда можно, иначе по своему адресу")
    same((ForeignEngines.scope(None), ForeignEngines.ask_host(None)), ("", "127.0.0.1"),
         "negative: слушателя не знаем — «» (не «loopback»), спрашиваем по петле")


def test_views_and_loop():
    CHECKS.section("последний поиск и его поток:")
    fleet = Fleet(engines={11434: ollama()}, listeners={"ok": True, "ports": []}, processes={})
    engines = fleet.scanner()
    same(engines.views(), None, "до первого поиска — None: «не смотрели»")
    same(engines.refresh(), [], "смотрели, никого — [] (обычный порт не слушает, спрашивать нечего)")
    same((engines.views(), fleet.asked), ([], []), "negative: не слушает — и не спрашивается")

    fleet.listen = {"ok": True, "ports": [{"port": 11434, "proc": "ollama", "pid": 5100, "addrs": ["0.0.0.0"]}]}
    naps = []

    def sleep(sec):
        naps.append(sec)
        if len(naps) == 1:
            fleet.procs = "broken"   # the next scan raises inside
        if len(naps) >= 2:
            raise KeyboardInterrupt

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            engines.run(sleep=sleep)
        except KeyboardInterrupt:
            pass
    same(([v["port"] for v in engines.views() or []], naps), ([11434], [10.0, 10.0]),
         "поток: поиск каждые 10 с; упавший поиск не рвёт цикл и не стирает прошлый ответ")
    check("[engines] scan failed" in out.getvalue(), "упавший поиск назван в журнале")


class _EngineServer(BaseHTTPRequestHandler):
    methods = []

    def log_message(self, *_a):
        pass

    def _answer(self):
        type(self).methods.append((self.command, self.path))
        if self.path == "/json":
            body, status, ctype = b'{"version": "0.12.3"}', 200, "application/json"
        elif self.path == "/locked":
            body, status, ctype = b'{"error": "token"}', 401, "application/json"
        elif self.path == "/slow":
            time.sleep(1.0)
            body, status, ctype = b"{}", 200, "application/json"
        else:
            body, status, ctype = b"Ollama is running", 200, "text/plain"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer
    do_POST = _answer


def test_engine_ask():
    CHECKS.section("запрос к движку по HTTP — только GET:")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EngineServer)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    try:
        with patched(urllib.request, urlopen=REAL["urlopen"]):
            ask = EngineAsk(port)
            got = [ask("/json"), ask("/locked"), ask("/text"), EngineAsk(closed)("/json"),
                   EngineAsk(port, timeout=0.2)("/slow")]
    finally:
        server.shutdown()
        server.server_close()
    same(got, [(200, {"version": "0.12.3"}), (401, None), (200, None), (None, None), (None, None)],
         "200 — статус и JSON; 401 — статус (так просят токен); не JSON — статус без тела; "
         "закрытый порт и таймаут — None: никто не ответил")
    same({m for m, _p in _EngineServer.methods}, {"GET"}, "в движок уходят только GET — скаут ничего не меняет")
    same((EngineAsk.TIMEOUT, EngineAsk(1).host), (1.5, "127.0.0.1"), "таймаут 1.5 с, по умолчанию — петля")


def test_report_carries_scan():
    CHECKS.section("отчёт несёт последний поиск:")
    scout = make_scout()
    fleet = Fleet(engines={11434: ollama()}, processes={5100: {"ppid": 1, "rssKb": 100, "name": "ollama"}},
                  listeners={"ok": True, "ports": [{"port": 11434, "proc": "ollama", "pid": 5100,
                                                    "addrs": ["0.0.0.0"]}]})
    quiet = {"gpus": lambda: [], "compute_apps": lambda: [], "cpu_ram": lambda: {}, "address": lambda: "10.0.0.5",
             "listeners": fleet.listeners, "processes": fleet.processes, "firewall": fleet.firewall}
    with patched(scout.machine, **quiet), patched(scout.engines, ask=fleet.ask), \
            patched(socket, gethostname=lambda: "box-a.lan"), contextlib.redirect_stdout(io.StringIO()):
        before = (scout.report.public()["engines"], scout.report.heartbeat()["engines"])
        scout.engines.refresh()
        after = (scout.report.public()["engines"], scout.report.heartbeat()["engines"])
    same(before, (None, None), "до поиска — None в обоих отчётах")
    same([v["port"] for v in after[0]], [11434], "после — найденный движок")
    same(after[0], after[1], "пульс и /api/state говорят одно и то же одним именем — engines")


class Calls:
    """An engine's answers to POSTs by path — (status, payload, its words) —
    and every call made, path and body."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.made = []

    def __call__(self, path, body):
        self.made.append((path, body))
        return self.table.get(path, (200, {"done": True}, ""))


def refusal(fn):
    try:
        fn()
    except AppError as exc:
        return (exc.status, str(exc))
    return None


def test_kind_controls():
    print("что можно сделать с движком (2.14):")
    same([Ollama().controls({"state": s}) for s in ("ok", "auth", "unreachable")], [["load", "unload"], [], []],
         "Ollama отвечает — загрузить и выгрузить; хочет токен или молчит — ничего")
    same([LmStudio().controls({"state": "ok", "api": a}) for a in ("v1", "v0")], [["load", "unload"], []],
         "negative: LM Studio 0.3 (только /api/v0) читается, но не водится — у старого API нет таких глаголов")

    c = Calls()
    same((Ollama().load(c, None, "qwen3:8b", 4096), c.made),
         ("", [("/api/generate", {"model": "qwen3:8b", "keep_alive": -1, "options": {"num_ctx": 4096}})]),
         "Ollama: загрузка — /api/generate без запроса, keep_alive -1 (держать, пока не выгрузят), окно — num_ctx")
    c = Calls()
    Ollama().load(c, None, "m", None)
    same(c.made[0][1], {"model": "m", "keep_alive": -1}, "окно не задано — своё окно Ollama, без options")
    c = Calls()
    same((Ollama().unload(c, None, "m"), c.made), ("", [("/api/generate", {"model": "m", "keep_alive": 0})]),
         "выгрузка — keep_alive 0")
    same(Ollama().load(Calls({"/api/generate": (500, {"error": "model requires more system memory"},
                                                "model requires more system memory")}), None, "m", None),
         "model requires more system memory", "отказ — словами самого движка")
    same(Ollama().load(Calls({"/api/generate": (None, None, "no answer: refused")}), None, "m", None),
         "no answer: refused", "negative: не ответил — так и сказано, а не «загружено»")
    same(Ollama().load(Calls({"/api/generate": (500, None, "")}), None, "m", None), "http 500",
         "boundary: отказ без слов — хотя бы код, а не пустая строка «успех»")

    c = Calls()
    same((LmStudio().load(c, None, "google/gemma-3-4b", 8192), c.made),
         ("", [("/api/v1/models/load", {"model": "google/gemma-3-4b", "context_length": 8192})]),
         "LM Studio: /api/v1/models/load с окном")
    c = Calls()
    same((LmStudio().unload(c, Answers({"/api/v1/models": (200, LMS_V1)}), "google/gemma-3-4b"), c.made),
         ("", [("/api/v1/models/unload", {"instance_id": "google/gemma-3-4b"}),
               ("/api/v1/models/unload", {"instance_id": "google/gemma-3-4b:2"})]),
         "выгрузка — каждый экземпляр по id, спрошенным заново, а не из прошлого поиска")
    same(LmStudio().unload(Calls(), Answers({"/api/v1/models": (200, LMS_V1)}), "text-embedding-nomic"),
         "text-embedding-nomic is not loaded", "negative: экземпляров нет — так и сказано")
    same(LmStudio().unload(Calls(), Answers(silent=True), "x"), "the model list did not answer",
         "negative: список моделей не ответил — выгружать нечего по чьим-то словам")
    c = Calls({"/api/v1/models/unload": (404, {"error": {"message": "no such instance"}}, "no such instance")})
    same((LmStudio().unload(c, Answers({"/api/v1/models": (200, LMS_V1)}), "google/gemma-3-4b"), len(c.made)),
         ("no such instance", 1), "первый отказ останавливает выгрузку и назван словами движка")


def act_rig(listen=("127.0.0.1",), engine=None):
    fleet = Fleet(engines={11434: engine or ollama()}, processes={},
                  listeners={"ok": True, "ports": [{"port": 11434, "proc": "ollama", "pid": 5100,
                                                    "addrs": list(listen)}]})
    posts, queued, made = {}, [], []

    def call(port, host="127.0.0.1"):
        made.append((port, host))
        return posts.setdefault(port, Calls())

    engines = ForeignEngines(fleet, fleet, ask=fleet.ask, call=call, clock=lambda: 1000.0, spawn=queued.append)
    with contextlib.redirect_stdout(io.StringIO()):
        engines.refresh()
    return engines, fleet, posts, queued, made


def model_row(views, name):
    for v in views or []:
        for m in v.get("models") or []:
            if m.get("name") == name:
                return m
    return None


def test_act():
    print("загрузить и выгрузить с доски (2.14):")
    engines, fleet, posts, queued, made = act_rig()
    got = engines.act("load", "ollama", 11434, "nomic-embed-text:latest", "2048")
    same((got["ok"], model_row(got["engines"], "nomic-embed-text:latest").get("action")),
         (True, {"op": "load", "since": 1000}), "ответ сразу: модель помечена «грузится», с какого часа")
    same(len(queued), 1, "само действие — вне запроса (своя нить): загрузка идёт секунды и минуты")
    same(refusal(lambda: engines.act("load", "ollama", 11434, "nomic-embed-text:latest")),
         (409, "nomic-embed-text:latest is being loaded already"), "negative: второе действие над той же моделью — 409")
    scans_before = fleet.proc_reads
    with contextlib.redirect_stdout(io.StringIO()) as out:
        queued.pop()()
    same(posts[11434].made, [("/api/generate", {"model": "nomic-embed-text:latest", "keep_alive": -1,
                                                "options": {"num_ctx": 2048}})],
         "движку ушла загрузка с окном из запроса")
    same((model_row(engines.views(), "nomic-embed-text:latest").get("action"), fleet.proc_reads == scans_before + 1,
          "load nomic-embed-text:latest on ollama:11434: done" in out.getvalue()),
         (None, True, True), "после — метка снята, движок опрошен сразу (не через 10 с), в журнале сказано")

    posts[11434] = Calls({"/api/generate": (500, {"error": "boom"}, "boom")})
    engines.act("unload", "ollama", 11434, "qwen3:8b")
    with contextlib.redirect_stdout(io.StringIO()):
        queued.pop()()
    same(model_row(engines.views(), "qwen3:8b").get("actionError"), {"op": "unload", "error": "boom", "at": 1000},
         "отказ движка остаётся у модели словами — до следующего действия над ней")
    posts[11434] = Calls()
    engines.act("unload", "ollama", 11434, "qwen3:8b")
    same(model_row(engines.views(), "qwen3:8b").get("actionError"), None,
         "следующее действие снимает прошлую ошибку")

    def crash(*_a):
        raise RuntimeError("kaboom")

    queued.clear()
    with patched(Ollama, unload=crash):
        engines, *_rest = act_rig()
        engines.act("unload", "ollama", 11434, "qwen3:8b")
        with contextlib.redirect_stdout(io.StringIO()):
            _rest[2].pop()()
    row = model_row(engines.views(), "qwen3:8b")
    same((row.get("action"), row.get("actionError", {}).get("error")), (None, "RuntimeError: kaboom"),
         "negative: действие упало — не висит «идёт» вечно, упавшее названо ошибкой")

    engines, fleet, posts, queued, made = act_rig()
    for args, want in (
            (("stop", "ollama", 11434, "qwen3:8b"), (400, "unknown engine action 'stop'")),
            (("load", "ollama", 9999, "qwen3:8b"), (404, "no ollama on port 9999 here")),
            (("load", "vllm", 11434, "qwen3:8b"), (404, "no vllm on port 11434 here")),
            (("load", "ollama", "x", "qwen3:8b"), (400, "port must be a number")),
            (("load", "ollama", 11434, "ghost:1"), (404, "Ollama lists no model 'ghost:1'")),
            (("load", "ollama", 11434, "gpt-oss:120b-cloud"), (400, "gpt-oss:120b-cloud runs on the engine's cloud, not on this machine")),
            (("load", "ollama", 11434, "qwen3:8b"), (409, "qwen3:8b is loaded already")),
            (("unload", "ollama", 11434, "nomic-embed-text:latest"), (409, "nomic-embed-text:latest is not loaded")),
            (("load", "ollama", 11434, "nomic-embed-text:latest", "big"), (400, "contextLength must be a number of tokens")),
            (("load", "ollama", 11434, "nomic-embed-text:latest", 100), (400, "contextLength must be 256…1048576"))):
        same(refusal(lambda: engines.act(*args)), want, f"negative: {args[0]} {args[2]}:{args[3]} — {want[1]}")
    same(queued, [], "negative: ни один отказ ничего не запустил")
    engines.act("load", "ollama", 11434, "nomic-embed-text:latest", "")
    same(made[-1:] if made else made, [], "boundary: пустое окно — окно движка; движок ещё не зовётся до нити")
    engines, fleet, posts, queued, made = act_rig(listen=("203.0.113.20",))
    engines.act("load", "ollama", 11434, "nomic-embed-text:latest")
    with contextlib.redirect_stdout(io.StringIO()):
        queued.pop()()
    same(made, [(11434, "203.0.113.20")], "движок, привязанный к одному адресу, зовётся по нему, как при поиске")
    silent, *_r = act_rig(engine=Answers({"/api/version": (401, None)}))
    same(refusal(lambda: silent.act("load", "ollama", 11434, "x")), None if False else (409, "Ollama on port 11434 cannot load from here"),
         "negative: движок хочет токен — водить его нельзя (controls пуст)")


def test_engine_call():
    print("POST движку — по-настоящему:")
    class _Engine(BaseHTTPRequestHandler):
        seen = []

        def log_message(self, *_a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            type(self).seen.append((self.path, body, self.headers.get("Content-Type")))
            status, payload = (200, {"done": True}) if self.path == "/ok" else (500, {"error": {"message": "no VRAM"}})
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    try:
        with patched(urllib.request, urlopen=REAL["urlopen"]):
            call = EngineCall(server.server_address[1], timeout=5)
            got = [call("/ok", {"model": "m"}), call("/bad", {"model": "m"})]
            dead = EngineCall(closed, timeout=1)("/ok", {})
    finally:
        server.shutdown()
        server.server_close()
    same(got, [(200, {"done": True}, ""), (500, {"error": {"message": "no VRAM"}}, "no VRAM")],
         "200 — тело; отказ — код и слова движка из error.message")
    same(_Engine.seen, [("/ok", {"model": "m"}, "application/json"), ("/bad", {"model": "m"}, "application/json")],
         "POST с JSON-телом")
    same((dead[0], dead[1], dead[2].startswith("no answer:")), (None, None, True), "negative: порт молчит — «no answer»")
    same(EngineCall.TIMEOUT, 300.0, "boundary: первая загрузка — минуты, не секунды")


def test_http_routes():
    print("пути скаута для действий над движком:")
    scout = make_scout()
    got_args = []

    def act(*args):
        got_args.append(args)
        if args[2] == 9999:
            raise AppError("no ollama on port 9999 here", 404)
        return {"ok": True, "engines": []}

    with patched(scout.engines, act=act), Served(scout) as srv:
        a = srv.post("/api/engines/load", {"kind": "ollama", "port": 11434, "model": "m", "contextLength": 4096})
        b = srv.post("/api/engines/unload", {"kind": "ollama", "port": 11434, "model": "m"})
        c = srv.post("/api/engines/load", {"kind": "ollama", "port": 9999, "model": "m"})
    same((a, b, c), ((200, {"ok": True, "engines": []}), (200, {"ok": True, "engines": []}),
                     (404, {"error": "no ollama on port 9999 here"})),
         "load/unload отвечают сразу; отказ — своим кодом и словами")
    same(got_args, [("load", "ollama", 11434, "m", 4096), ("unload", "ollama", 11434, "m", None),
                    ("load", "ollama", 9999, "m", None)], "вид, порт, модель и окно доходят как есть")


TESTS = (test_ollama_read, test_lmstudio_read, test_scan, test_scope_and_host, test_views_and_loop,
         test_engine_ask, test_report_carries_scan, test_kind_controls, test_act, test_engine_call, test_http_routes)

for test in TESTS:
    blocked_before = len(BLOCKED)
    try:
        test()
    except (Exception, RealCallBlocked) as exc:  # noqa: BLE001 — a crash is a red pin; the rest still runs
        check(False, f"{test.__name__} упал: {exc!r}")
    if len(BLOCKED) > blocked_before:
        check(False, f"{test.__name__} дотянулся до хоста: {BLOCKED[blocked_before:]}")

sys.exit(CHECKS.finish())
