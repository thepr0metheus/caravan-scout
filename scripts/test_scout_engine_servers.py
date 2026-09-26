#!/usr/bin/env python3
"""Snapshot of the engines' servers turned on and off (2.16):
caravan_scout/engine_servers.py and its seams in engines.py.

How each kind learns to start its server again (Ollama from its process,
LM Studio from its command line), starts and stops it; the machine's side —
/proc read from a stand-in directory, spawn, signals — with every door to
the host stood in for; what the scan's views gain (who runs a server, a stop
for this scout's user only, a stopped engine that can be started); the
start and stop as acts from the board, waited on; and the start at boot.

Run: python3 scripts/test_scout_engine_servers.py
"""
import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import BLOCKED, TMP, Checks, RealCallBlocked, Served, make_scout, patched  # noqa: E402

from caravan_scout.engine_servers import EngineProcs, EngineServers  # noqa: E402
from caravan_scout.engines import ForeignEngines, LmsCli, LmStudio, Ollama  # noqa: E402
from caravan_scout.errors import AppError  # noqa: E402

CHECKS = Checks("scout engine servers")
check = CHECKS.check


def same(actual, expected, msg):
    ok = actual == expected
    check(ok, msg)
    if not ok:
        print(f"        got:  {actual!r}\n        want: {expected!r}")


def refusal(fn):
    try:
        fn()
    except AppError as exc:
        return (exc.status, str(exc))
    return None


def executable(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


class Cli:
    """LM Studio's command line, stood in for: answers by the command's
    first two words, records every command and how long it may take."""

    def __init__(self, answers=None, available=True):
        self.answers = dict(answers or {})
        self.is_there = available
        self.made = []

    def available(self):
        return self.is_there

    def __call__(self, args, timeout=None):
        self.made.append((list(args), timeout))
        return self.answers.get(" ".join(args[:2]), (0, ""))


class Procs:
    """The machine's side of a server, stood in for: processes by pid, the
    scout's uid, and every spawn and stop asked."""

    def __init__(self, infos=None, me=1000, spawn_says="", stop_says=""):
        self.infos = dict(infos or {})
        self.me = me
        self.spawned = []
        self.stopped = []
        self.spawn_says = spawn_says
        self.stop_says = stop_says

    def uid(self):
        return self.me

    def info(self, pid):
        return self.infos.get(int(pid))

    def spawn(self, argv, env, kind):
        self.spawned.append((list(argv), dict(env), kind))
        return self.spawn_says

    def terminate(self, pid, grace):
        self.stopped.append((pid, grace))
        return self.stop_says


class State(dict):
    """state.json as the scout keeps it: a dict with a lock and a save."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        import threading
        self.lock = threading.Lock()
        self.saves = 0

    def save(self):
        self.saves += 1


OLLAMA_INFO = {"uid": 1000, "exe": "/home/u/ollama/bin/ollama", "args": ["/home/u/ollama/bin/ollama", "serve"],
               "env": {"OLLAMA_HOST": "0.0.0.0:11434", "OLLAMA_MODELS": "/data/ollama", "CUDA_VISIBLE_DEVICES": "1",
                       "HOME": "/home/u", "API_TOKEN": "secret"}, "marked": False}


def test_kind_recipes():
    CHECKS.section("как вид движка учится запускать свой сервер:")
    same(Ollama().recipe(OLLAMA_INFO, {}),
         {"exe": "/home/u/ollama/bin/ollama", "args": ["serve"],
          "env": {"OLLAMA_HOST": "0.0.0.0:11434", "OLLAMA_MODELS": "/data/ollama", "CUDA_VISIBLE_DEVICES": "1"}},
         "Ollama: его бинарь и serve, из окружения — только OLLAMA_* и выбор карт (не HOME, не чужие секреты)")
    for info, why in (({**OLLAMA_INFO, "exe": "/home/u/ollama/lib/ollama/llama-server"}, "исполнитель, а не сервер"),
                      ({**OLLAMA_INFO, "args": ["/home/u/ollama/bin/ollama", "runner"]}, "не serve"),
                      ({**OLLAMA_INFO, "exe": ""}, "бинарь не назван (чужой процесс)"), (None, "процесс не назван")):
        same(Ollama().recipe(info, {}), None, f"negative: {why} — рецепта нет, а не выдуманный")
    with tempfile.TemporaryDirectory() as home:
        same(Ollama().installed(home), None, "negative: в домашней папке Ollama нет — не установлена")
        (Path(home) / "ollama/bin").mkdir(parents=True)
        (Path(home) / "ollama/bin/ollama").write_text("x")
        same(Ollama().installed(home), None, "negative: файл есть, но не исполняемый — не предлагается")
        executable(Path(home) / ".local/bin/ollama")
        executable(Path(home) / "ollama/bin/ollama")
        same(Ollama().installed(home), {"exe": f"{home}/ollama/bin/ollama", "args": ["serve"], "env": {}},
             "установлена в домашнюю папку — рецепт по её месту; архив Ollama (~/ollama) — первым")
    lms = LmStudio(cli=Cli())
    same([lms.recipe(None, {"port": 1234, "listen": "loopback"}), lms.recipe(None, {"port": 1235, "listen": "network"}),
          lms.recipe(None, {"port": 1234, "listen": ""})],
         [{"port": 1234, "bind": "127.0.0.1"}, {"port": 1235, "bind": "0.0.0.0"}, {"port": 1234, "bind": "0.0.0.0"}],
         "LM Studio: его командная строка; выучено, где он слушает — порт и только ли эта машина")
    same(LmStudio(cli=Cli(available=False)).recipe(None, {"port": 1234, "listen": "loopback"}), None,
         "negative: без lms запускать его нечем")
    with tempfile.TemporaryDirectory() as home:
        same(LmStudio(cli=Cli()).installed(home), None, "negative: lms в домашней папке нет — не установлен")
        executable(Path(home) / ".lmstudio/bin/lms")
        same(LmStudio(cli=Cli()).installed(home), {"port": 1234, "bind": "127.0.0.1"},
             "установлен — свой порт, только эта машина (его же умолчание)")


def test_kind_start_stop():
    CHECKS.section("пуск и остановка по виду движка:")
    with tempfile.TemporaryDirectory() as home:
        exe = executable(Path(home) / "ollama/bin/ollama")
        procs = Procs()
        same((Ollama().start_server({"exe": str(exe), "args": ["serve"], "env": {"OLLAMA_HOST": "127.0.0.1:11434"}},
                                    procs), procs.spawned),
             ("", [([str(exe), "serve"], {"OLLAMA_HOST": "127.0.0.1:11434"}, "ollama")]),
             "Ollama: serve своим бинарём и своим окружением")
        procs = Procs(spawn_says="ollama did not start: exec format error")
        same(Ollama().start_server({"exe": str(exe), "args": ["serve"], "env": {}}, procs),
             "ollama did not start: exec format error", "отказ запуска — его словами")
    procs = Procs()
    same((Ollama().start_server({"exe": "/nowhere/ollama", "args": ["serve"]}, procs), procs.spawned),
         ("/nowhere/ollama is not there to run", []), "negative: бинаря больше нет — так и сказано, ничего не запущено")
    procs = Procs()
    same((Ollama().stop_server({}, 5100, procs), procs.stopped), ("", [(5100, 10.0)]),
         "Ollama: остановка — SIGTERM его серверу, 10 с на то, чтобы уйти самому")
    same(Ollama().stop_server({}, None, Procs()), "nothing listens on its port",
         "negative: на порту никого — останавливать некого")

    cli = Cli()
    same((LmStudio(cli=cli).start_server({"port": 1235, "bind": "0.0.0.0"}, None), cli.made),
         ("", [(["daemon", "up"], LmsCli.SLOW), (["server", "start", "-p", "1235", "--bind", "0.0.0.0"], LmsCli.SLOW)]),
         "LM Studio: поднять демона, затем сервер на выученных порту и адресе; ждать — минуты")
    cli = Cli({"daemon up": (1, "Loading x\nllmster failed to start\nsee logs")})
    same((LmStudio(cli=cli).start_server({"port": 1234}, None), len(cli.made)),
         ("llmster failed to start — see logs", 1), "демон не поднялся — его словами, сервер не зовётся")
    cli = Cli({"server start": (1, "Port 1234 is in use")})
    same(LmStudio(cli=cli).start_server({"port": 1234}, None), "Port 1234 is in use", "сервер не поднялся — его словами")
    cli = Cli()
    same((LmStudio(cli=cli).stop_server({}, 7, None), cli.made), ("", [(["daemon", "down"], LmsCli.SLOW)]),
         "LM Studio: остановка — демона целиком: только так уходят загруженные модели и их память")
    cli = Cli({"daemon down": (1, "no daemon")})
    same((LmStudio(cli=cli).stop_server({}, 7, None), [m[0] for m in cli.made]),
         ("", [["daemon", "down"], ["server", "stop"]]), "демона нет (сервер держит приложение) — останавливается сервер")
    cli = Cli({"daemon down": (1, "no daemon"), "server stop": (2, "")})
    same(LmStudio(cli=cli).stop_server({}, 7, None), "lms server stop exited with 2",
         "negative: не остановилось ничего — сказано, а не «остановлен»")


def test_lms_lock():
    CHECKS.section("чтение не будит LM Studio посреди остановки:")
    import threading
    order, result, blocked, readers = [], [], [], []

    class RacingCli:
        up = True

        def available(self):
            return True

        def __call__(self, args, timeout=None):
            order.append(" ".join(args[:2]))
            if list(args[:2]) == ["server", "status"]:
                return (0, "The server is running on port 1234.") if self.up else (0, "The server is not running.")
            if list(args[:2]) == ["daemon", "down"]:
                reader = threading.Thread(target=lambda: result.append(kind.idle_limits()), daemon=True)
                reader.start()
                reader.join(0.3)
                blocked.append(reader.is_alive())
                readers.append(reader)
                self.up = False
                return 0, "Done."
            return 0, "[]"

    kind = LmStudio(cli=RacingCli())
    said = kind.stop_server({}, 7, None)
    readers[0].join(2)
    same((said, blocked, result, order), ("", [True], [{}], ["daemon down", "server status"]),
         "чтение пришло посреди остановки — ждёт её конца, видит «не работает» и lms ps не зовёт (он поднял бы LM Studio снова)")


def fake_proc(root, pid, uid_of_dir=None, cmdline=(), environ=None, exe=None, ppid=1):
    d = Path(root) / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in cmdline) + (b"\0" if cmdline else b""))
    if environ is not None:
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in environ.items()))
    if exe:
        (d / "exe").symlink_to(exe)
    (d / "stat").write_text(f"{pid} (x y) S {ppid} 1 1 0\n")
    return d


def test_procs():
    CHECKS.section("сторона машины (/proc, запуск, сигналы) — на подставном /proc:")
    with tempfile.TemporaryDirectory() as root:
        procs = EngineProcs(Path(root) / "logs", sleep=lambda s: None)
        procs.PROC = Path(root) / "proc"
        fake_proc(procs.PROC, 5100, cmdline=["/home/u/ollama/bin/ollama", "serve"],
                  environ={"OLLAMA_HOST": "127.0.0.1:11434", EngineProcs.ENGINE_ENV: "ollama"}, exe="/home/u/ollama/bin/ollama")
        fake_proc(procs.PROC, 5151, cmdline=["runner"], ppid=5100)
        fake_proc(procs.PROC, 5152, cmdline=["helper"], ppid=5151)
        fake_proc(procs.PROC, 6000, cmdline=["other"], ppid=1)
        info = procs.info(5100)
        same({k: info[k] for k in ("exe", "args", "marked")} | {"host": info["env"].get("OLLAMA_HOST")},
             {"exe": "/home/u/ollama/bin/ollama", "args": ["/home/u/ollama/bin/ollama", "serve"], "marked": True,
              "host": "127.0.0.1:11434"}, "процесс: бинарь, аргументы, окружение; метка «запущен этим скаутом»")
        same((procs.info(5151)["env"], procs.info(5151)["marked"], procs.info(5151)["exe"]), ({}, False, ""),
             "negative: окружение и бинарь не прочитаны (чужой пользователь) — пусто, а не выдумано")
        same((procs.info(4242), procs.info("x"), procs.info(None)), (None, None, None),
             "negative: процесса нет или pid не число — None")
        same(sorted(procs.children(5100)), [5151, 5152], "дети — всё дерево под процессом, а не только прямые")

        popens = []

        class FakePopen:
            def __init__(self, argv, **kw):
                popens.append((argv, kw))

        with patched(subprocess, Popen=FakePopen), patched(os, environ={"PATH": "/usr/bin", EngineProcs.CELL_ENV: "22001"}):
            said = procs.spawn(["/home/u/ollama/bin/ollama", "serve"], {"OLLAMA_HOST": "0.0.0.0:11434"}, "ollama")
        argv, kw = popens[0]
        same((said, argv, kw["start_new_session"], kw["close_fds"], kw["stdin"] is subprocess.DEVNULL,
              kw["env"], Path(kw["stdout"].name).name),
             ("", ["/home/u/ollama/bin/ollama", "serve"], True, True, True,
              {"PATH": "/usr/bin", "OLLAMA_HOST": "0.0.0.0:11434", EngineProcs.ENGINE_ENV: "ollama"}, "ollama.log"),
             "запуск: своя сессия (рестарт скаута его не заденет), журнал ollama.log, окружение скаута + рецепт + своя "
             "метка; метки ячейки нет — уборщик ячеек его не тронет")

        class Refuses:
            def __init__(self, argv, **kw):
                raise OSError("exec format error")

        with patched(subprocess, Popen=Refuses):
            same(procs.spawn(["/x/ollama", "serve"], {}, "ollama"), "ollama did not start: exec format error",
                 "negative: не запустился — его словами")

        sent, alive = [], {5100: [True, True, False]}

        def kill(pid, sig):
            sent.append((pid, sig))
            if sig == 0:
                seq = alive.get(pid, [False])
                if not (seq.pop(0) if len(seq) > 1 else seq[0]):
                    raise ProcessLookupError(pid)

        with patched(os, kill=kill, waitpid=lambda pid, flags: (_ for _ in ()).throw(ChildProcessError())):
            same((procs.terminate(5100, 10.0), sent[0]), ("", (5100, signal.SIGTERM)),
                 "остановка: SIGTERM, ушёл сам — всё")
            sent.clear()
            clock = iter(range(0, 1000, 5))
            procs.clock = lambda: next(clock)
            alive[5100] = [True]
            same((procs.terminate(5100, 10.0), [s for s in sent if s[1] == signal.SIGKILL]),
                 ("process 5100 would not stop", [(5100, signal.SIGKILL), (5151, signal.SIGKILL), (5152, signal.SIGKILL)]),
                 "не ушёл за 10 с — SIGKILL ему и всем его детям (они держат память карт); не ушёл и так — сказано")

        def gone(pid, sig):
            raise ProcessLookupError(pid)

        def denied(pid, sig):
            raise PermissionError(pid)

        with patched(os, kill=gone):
            same(procs.terminate(5100, 10.0), "", "boundary: уже ушёл до сигнала — остановлен")
        with patched(os, kill=denied):
            same(procs.terminate(6000, 10.0), "not allowed to stop process 6000",
                 "negative: чужой процесс — не позволено, так и сказано")
        with patched(os, waitpid=lambda pid, flags: (pid, 0), kill=lambda pid, sig: None):
            same(procs.alive(5100), False, "свой ребёнок, который вышел, прибран — не «жив» как зомби")


def views(ollama_state="ok", lms=True):
    rows = [{"kind": "ollama", "label": "Ollama", "port": 11434, "listen": "loopback", "state": ollama_state,
             "version": "0.34.4", "models": [], "pids": [5100], "controls": ["unload", "delete", "pull"],
             "firewall": None, "ramBytes": 1}]
    if lms:
        rows.append({"kind": "lmstudio", "label": "LM Studio", "port": 1234, "listen": "loopback", "state": "ok",
                     "version": "", "api": "v1", "models": [], "pids": [700], "controls": ["unload", "pull"],
                     "firewall": None, "ramBytes": 1})
    return rows


LISTEN = [{"port": 11434, "pid": 5100}, {"port": 1234, "pid": 700}]


def test_annotate():
    CHECKS.section("что вид движка узнаёт о своём сервере:")
    kinds = (Ollama(), LmStudio(cli=Cli()))
    state = State()
    servers = EngineServers(state, Procs({5100: OLLAMA_INFO, 700: {"uid": 1000, "exe": "", "args": [], "env": {}}}),
                            home=TMP / "empty-home")
    out = servers.annotate(kinds, views(), LISTEN)
    same([(v["kind"], v["runBy"], v["controls"], v["autostart"]) for v in out],
         [("ollama", "user", ["unload", "delete", "pull", "stop"], False), ("lmstudio", "user", ["unload", "pull", "stop"], False)],
         "сервер пользователя скаута — «user» и его можно остановить с доски")
    same((state["engineServers"]["ollama"], state["engineServers"]["lmstudio"], state.saves),
         ({"recipe": {"exe": "/home/u/ollama/bin/ollama", "args": ["serve"],
                      "env": {"OLLAMA_HOST": "0.0.0.0:11434", "OLLAMA_MODELS": "/data/ollama", "CUDA_VISIBLE_DEVICES": "1"}},
           "port": 11434}, {"recipe": {"port": 1234, "bind": "127.0.0.1"}, "port": 1234}, 2),
         "как его запускать снова — запомнено в state.json (и сохранено)")
    servers.annotate(kinds, views(), LISTEN)
    same(state.saves, 2, "то же ещё раз — не сохраняется заново")

    out = EngineServers(State(), Procs({700: {"uid": 1000, "exe": "", "args": [], "env": {}}}),
                        home=TMP / "empty-home").annotate((Ollama(), LmStudio(cli=Cli(available=False))),
                                                          views()[1:], LISTEN)
    same((out[0]["runBy"], out[0]["controls"]), ("user", ["unload", "pull"]),
         "negative: как запустить снова — не выучить (LM Studio без lms): и остановить с доски нельзя — в одну сторону")
    state = State()
    servers = EngineServers(state, Procs({5100: {**OLLAMA_INFO, "uid": 999}}), home=TMP / "empty-home")
    out = servers.annotate(kinds, views(lms=False), LISTEN)
    same((out[0]["runBy"], out[0]["controls"], "engineServers" in state), ("other", ["unload", "delete", "pull"], False),
         "negative: сервер другого пользователя (системная служба) — «other», остановить с доски нельзя, рецепт не учится")
    out = EngineServers(State(), Procs({}), home=TMP / "empty-home").annotate(kinds, views(lms=False), LISTEN)
    same((out[0]["runBy"], out[0]["controls"]), ("", ["unload", "delete", "pull"]),
         "negative: машина не назвала процесс — «» (не знаю), не «user»")
    out = EngineServers(State(), Procs({5100: OLLAMA_INFO}, me=None), home=TMP / "empty-home").annotate(
        kinds, views(lms=False), LISTEN)
    same(out[0]["runBy"], "", "negative: свой uid неизвестен — и чей процесс, не сказать")

    state = State({"engineServers": {"ollama": {"recipe": {"exe": "/home/u/ollama/bin/ollama", "args": ["serve"], "env": {}},
                                                "port": 11434, "autostart": True}}})
    servers = EngineServers(state, Procs({5100: OLLAMA_INFO}), home=TMP / "empty-home")
    out = servers.annotate(kinds, views(ollama_state="unreachable", lms=False), LISTEN)
    same((out[0]["controls"], out[0]["autostart"], state["engineServers"]["ollama"]["recipe"]["env"]),
         (["unload", "delete", "pull", "stop"], True, {}),
         "молчащий сервер (unreachable) остановить можно — рецепт известен; учится рецепт только у отвечающего")
    out = servers.annotate(kinds, [], [])
    same(out, [{"kind": "ollama", "label": "Ollama", "port": 11434, "listen": "", "state": "stopped", "version": "",
                "models": None, "pids": [], "controls": ["start"], "firewall": None, "ramBytes": None,
                "runBy": "", "autostart": True}],
         "известный сервер не запущен — движок «stopped», модели неизвестны (None), можно только запустить")

    home = Path(tempfile.mkdtemp(dir=TMP))
    executable(home / "ollama/bin/ollama")
    out = EngineServers(State(), Procs(), home=home).annotate((Ollama(), LmStudio(cli=Cli(available=False))), [], [])
    same([(v["kind"], v["state"], v["controls"], v["port"]) for v in out], [("ollama", "stopped", ["start"], 11434)],
         "не виден, но установлен в домашнюю папку — тоже «stopped» (запустить); LM Studio без lms — нет")
    same(EngineServers(State(), Procs(), home=TMP / "empty-home").annotate(kinds, [], []), [],
         "negative: не виден и не установлен — ничего, а не «остановлен»")


def test_servers_start_stop():
    CHECKS.section("пуск и остановка — что помнится после:")
    home = Path(tempfile.mkdtemp(dir=TMP))
    exe = executable(home / "ollama/bin/ollama")
    state = State()
    procs = Procs()
    servers = EngineServers(state, procs, home=home)
    same((servers.start(Ollama(), 11434), procs.spawned[0][0], state["engineServers"]["ollama"]),
         ("", [str(exe), "serve"], {"recipe": {"exe": str(exe), "args": ["serve"], "env": {}}, "port": 11434,
                                    "autostart": True}),
         "запущен с доски — запомнен и будет подниматься вместе с машиной")
    same((servers.stop(Ollama(), 5100), procs.stopped, state["engineServers"]["ollama"]["autostart"]),
         ("", [(5100, 10.0)], False), "остановлен с доски — больше с машиной не поднимается")
    state = State()
    servers = EngineServers(state, Procs(spawn_says="ollama did not start: boom"), home=home)
    same((servers.start(Ollama(), 11434), "engineServers" in state), ("ollama did not start: boom", False),
         "negative: не запустился — ничего не запомнено как «поднимать с машиной»")
    same(EngineServers(State(), Procs(), home=TMP / "empty-home").start(Ollama(), 11434),
         "how to start Ollama here is not known", "negative: рецепта нет — так и сказано")
    state = State()
    servers = EngineServers(state, Procs(stop_says="process 5100 would not stop"), home=home)
    same((servers.stop(Ollama(), 5100), state.get("engineServers", {}).get("ollama", {}).get("autostart")),
         ("process 5100 would not stop", None), "negative: не остановился — автозапуск не снят")

    state = State({"engineServers": {"ollama": {"recipe": {"exe": "x"}, "autostart": True},
                                     "lmstudio": {"recipe": {"port": 1234}, "autostart": False},
                                     "vllm": {"autostart": True}}})
    servers = EngineServers(state, Procs(), home=home)
    same([servers.due_at_boot(None), servers.due_at_boot(""), "engineServersBoot" in state], [[], [], False],
         "negative: машина не говорит, какая это загрузка, — ничего не поднимается")
    same([servers.due_at_boot("b1"), servers.due_at_boot("b1"), servers.due_at_boot("b2")],
         [["ollama"], [], ["ollama"]],
         "поднимается только запущенное с доски и не остановленное, один раз за загрузку машины (рестарт скаута — нет)")


class Machine:
    """The fake machine a ForeignEngines scans, as far as servers go."""

    def __init__(self, engines, listen, boot="b1"):
        self.engines = engines
        self.listen = listen
        self.boot = boot

    def listeners(self):
        return {"ok": True, "ports": [dict(r) for r in self.listen]}

    def processes(self):
        return {}

    def firewall(self, port):
        return {"state": "unknown"}

    def nvidia_gpus(self):
        return []

    def boot_id(self):
        return self.boot

    def all(self):
        return []

    def ask(self, port, host="127.0.0.1"):
        table = self.engines.get(int(port))
        return table if table is not None else (lambda path: (None, None))


def ollama_answers(up=True):
    def ask(path):
        if not up["now"] if isinstance(up, dict) else not up:
            return None, None
        return {"/api/version": (200, {"version": "0.34.4"}), "/api/ps": (200, {"models": []}),
                "/api/tags": (200, {"models": []})}.get(path, (404, None))
    return ask


def rig(up, procs, state=None, boot="b1", home=None):
    machine = Machine({11434: ollama_answers(up)}, [{"port": 11434, "pid": 5100, "addrs": ["127.0.0.1"], "proc": "ollama"}]
                      if (up["now"] if isinstance(up, dict) else up) else [], boot=boot)
    queued, clock = [], {"t": 1000.0}
    engines = ForeignEngines(machine, machine, ask=machine.ask, clock=lambda: clock["t"], spawn=queued.append,
                             kinds=(Ollama(), LmStudio(cli=Cli(available=False))),
                             servers=EngineServers(state if state is not None else State(), procs,
                                                   home=home or TMP / "empty-home"),
                             pause=lambda sec: clock.__setitem__("t", clock["t"] + sec))
    with contextlib.redirect_stdout(io.StringIO()):
        engines.refresh()
    return engines, machine, queued, clock


def test_serve():
    CHECKS.section("пуск и остановка с доски (2.16):")
    home = Path(tempfile.mkdtemp(dir=TMP))
    exe = executable(home / "ollama/bin/ollama")
    up = {"now": True}
    procs = Procs({5100: {**OLLAMA_INFO, "exe": str(exe), "args": [str(exe), "serve"]}})
    engines, machine, queued, clock = rig(up, procs)
    view = engines.views()[0]
    same((view["runBy"], view["controls"]), ("user", ["unload", "delete", "pull", "stop"]),
         "работает у пользователя скаута — можно остановить")
    got = engines.serve("stop", "ollama", 11434)
    same((got["ok"], got["engines"][0].get("serverAction"), len(queued)), (True, {"op": "stop", "since": 1000}, 1),
         "ответ сразу: движок помечен «останавливается»; сама остановка — в своей нити")
    same(refusal(lambda: engines.serve("stop", "ollama", 11434)), (409, "Ollama is being stopped already"),
         "negative: второй раз, пока идёт, — 409")

    def stop_then_quiet(pid, grace):
        procs.stopped.append((pid, grace))
        up["now"] = False
        machine.listen.clear()
        return ""

    procs.terminate = stop_then_quiet
    with contextlib.redirect_stdout(io.StringIO()) as out:
        queued.pop()()
    after = engines.views()
    same((procs.stopped, after[0]["state"], after[0]["controls"], after[0].get("serverAction"), after[0].get("serverError"),
          "stop ollama:11434: done" in out.getvalue()),
         ([(5100, 10.0)], "stopped", ["start"], None, None, True),
         "остановка: SIGTERM серверу на его порту; дождались тишины — движок «stopped», можно запустить")

    def spawn_then_up(argv, env, kind):
        procs.spawned.append((argv, env, kind))
        up["now"] = True
        machine.listen.append({"port": 11434, "pid": 5100, "addrs": ["127.0.0.1"], "proc": "ollama"})
        return ""

    procs.spawn = spawn_then_up
    engines.serve("start", "ollama", 11434)
    with contextlib.redirect_stdout(io.StringIO()):
        queued.pop()()
    after = engines.views()[0]
    same((procs.spawned[0][:2], after["state"], after["autostart"], after.get("serverError")),
         (([str(exe), "serve"], {"OLLAMA_HOST": "0.0.0.0:11434", "OLLAMA_MODELS": "/data/ollama",
                                 "CUDA_VISIBLE_DEVICES": "1"}), "ok", True, None),
         "запуск: по выученному рецепту; дождались ответа — движок работает и поднимается с машиной")

    up["now"] = False
    machine.listen.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        engines.refresh()
    procs.spawn = lambda argv, env, kind: ""
    engines.serve("start", "ollama", 11434)
    t0 = clock["t"]
    with contextlib.redirect_stdout(io.StringIO()):
        queued.pop()()
    same((engines.views()[0].get("serverError"), clock["t"] - t0 >= ForeignEngines.START_WAIT),
         ({"op": "start", "error": "it did not answer on port 11434 in 60 s", "at": clock["t"]}, True),
         "negative: запустился, но за 60 с не ответил — ошибка на движке, а не «работает»")

    procs.spawn = lambda argv, env, kind: (_ for _ in ()).throw(RuntimeError("kaboom"))
    engines.serve("start", "ollama", 11434)
    with contextlib.redirect_stdout(io.StringIO()):
        queued.pop()()
    view = engines.views()[0]
    same((view.get("serverAction"), view.get("serverError", {}).get("error")), (None, "RuntimeError: kaboom"),
         "negative: пуск упал — не висит «запускается» вечно, упавшее названо")

    for args, want in ((("pause", "ollama", 11434), (400, "unknown server action 'pause'")),
                       (("start", "ollama", "x"), (400, "port must be a number")),
                       (("start", "vllm", 11434), (404, "no vllm on port 11434 here")),
                       (("stop", "ollama", 11434), (409, "Ollama on port 11434 cannot stop from here"))):
        same(refusal(lambda: engines.serve(*args)), want, f"negative: {args[0]} {args[1]}:{args[2]} — {want[1]}")
    bare = ForeignEngines(machine, machine, ask=machine.ask, kinds=(Ollama(),), spawn=queued.append)
    with contextlib.redirect_stdout(io.StringIO()):
        bare.refresh()
    same(refusal(lambda: bare.serve("start", "ollama", 11434)), (404, "no ollama on port 11434 here"),
         "negative: без знания о серверах (servers=None) — ни пуска, ни остановки")

    other = rig(True, Procs({5100: {**OLLAMA_INFO, "uid": 0}}))[0]
    same(refusal(lambda: other.serve("stop", "ollama", 11434)), (409, "Ollama on port 11434 cannot stop from here"),
         "negative: сервер другого пользователя остановить нельзя — 409, до сигнала не дошло")


def test_boot():
    CHECKS.section("поднять вместе с машиной:")
    state = State({"engineServers": {"ollama": {"recipe": {"exe": "/home/u/ollama/bin/ollama", "args": ["serve"], "env": {}},
                                                "port": 11434, "autostart": True}}})
    engines, machine, queued, clock = rig(False, Procs(), state=state)
    with contextlib.redirect_stdout(io.StringIO()):
        got = engines.start_at_boot()
    same((got, len(queued), engines.views()[0].get("serverAction")), (["ollama"], 1, {"op": "start", "since": 1000}),
         "первый старт скаута в загрузке: остановленный сервер, запущенный когда-то с доски, — запускается")
    with contextlib.redirect_stdout(io.StringIO()):
        same(engines.start_at_boot(), [], "negative: второй раз в ту же загрузку — нет")
    state = State({"engineServers": {"ollama": {"recipe": {"exe": "x", "args": ["serve"]}, "port": 11434, "autostart": True}}})
    engines, machine, queued, clock = rig(True, Procs({5100: OLLAMA_INFO}), state=state, boot="b9")
    with contextlib.redirect_stdout(io.StringIO()) as out:
        same((engines.start_at_boot(), queued), ([], []), "negative: уже работает (подняли руками раньше скаута) — не трогается")
    check("is running already or cannot start here" in out.getvalue(), "…и в журнале сказано почему")
    runs = []
    engines, machine, queued, clock = rig(False, Procs(), state=State(), boot="b1")
    with patched(engines, start_at_boot=lambda: runs.append("boot") or []), patched(engines, refresh=lambda: runs.append("scan")):
        try:
            engines.run(sleep=lambda s: (_ for _ in ()).throw(StopIteration()) if runs.count("scan") >= 2 else None)
        except StopIteration:
            pass
    same(runs, ["scan", "boot", "scan"], "цикл: подъём при загрузке — один раз, после первого скана (когда известно, что работает)")


def test_scout_and_http():
    CHECKS.section("скаут и его пути:")
    scout = make_scout()
    same(type(scout.engines.servers).__name__, "EngineServers", "скаут знает о серверах движков")
    got = []

    def serve(op, kind, port):
        got.append((op, kind, port))
        if port == 9:
            raise AppError("no ollama on port 9 here", 404)
        return {"ok": True, "engines": []}

    with patched(scout.engines, serve=serve), Served(scout) as srv:
        a = srv.post("/api/engines/start", {"kind": "ollama", "port": 11434})
        b = srv.post("/api/engines/stop", {"kind": "lmstudio", "port": 1234})
        c = srv.post("/api/engines/start", {"kind": "ollama", "port": 9})
    same((a, b, c), ((200, {"ok": True, "engines": []}), (200, {"ok": True, "engines": []}),
                     (404, {"error": "no ollama on port 9 here"})), "start/stop отвечают сразу; отказ — своим кодом и словами")
    same(got, [("start", "ollama", 11434), ("stop", "lmstudio", 1234), ("start", "ollama", 9)], "вид и порт доходят как есть")


TESTS = (test_kind_recipes, test_kind_start_stop, test_lms_lock, test_procs, test_annotate, test_servers_start_stop, test_serve,
         test_boot, test_scout_and_http)

for test in TESTS:
    blocked_before = len(BLOCKED)
    try:
        test()
    except (Exception, RealCallBlocked) as exc:  # noqa: BLE001 — a crash is a red pin; the rest still runs
        check(False, f"{test.__name__} упал: {exc!r}")
    if len(BLOCKED) > blocked_before:
        check(False, f"{test.__name__} дотянулся до хоста: {BLOCKED[blocked_before:]}")

sys.exit(CHECKS.finish())
