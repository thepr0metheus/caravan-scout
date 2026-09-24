#!/usr/bin/env python3
"""Autostart: the cells a machine starts by itself when it boots (2.4).

The controller starts its own cells at boot with `systemctl enable`; a scout's
cells had nothing, and a reboot left them down. The scout keeps the start
request of each autostart cell and starts them on the first scout start of a
boot — not on every scout start: an update restarts the scout too, and a cell
the operator stopped would come back.

Pinned by value: what is kept, when it is refreshed, what a boot starts and
what a restart in the same boot does not, the route, the machine's boot id on
Linux and macOS, and the field both reports carry.

Run: python3 scripts/test_scout_autostart.py
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import TMP, Checks, FakeRun, Served, make_scout, patched  # noqa: E402

from caravan_scout.errors import AppError  # noqa: E402
from caravan_scout.machine import Machine  # noqa: E402

CHECKS = Checks("scout autostart")
check = CHECKS.check

LLAMA = {"modelPath": "models/org/model-q4.gguf", "port": 22001, "args": ["--port", "22001"],
         "config": {"PORT": 22001}}
WHISPER = {"cellKind": "command", "port": 22024, "shellLine": "exec bash ~/run_whisper.sh $PORT",
           "config": {"PORT": 22024, "RUNNER": "whisper"}}


def refusal(fn):
    try:
        fn()
    except AppError as exc:
        return exc.status, str(exc)
    return None


def test_turning_it_on_and_off():
    CHECKS.section("включить и выключить:")
    s = make_scout()
    got = s.autostart.set("22024", True, WHISPER)
    s.autostart.set(22001, True, {**LLAMA, "port": 9})
    check(got == {"ok": True, "port": 22024, "autostart": [22024]} and s.autostart.ports() == [22001, 22024],
          "включён — порт в списке; список по возрастанию")
    check(s.state["autostart"]["22001"]["payload"] == {**LLAMA, "port": 22001},
          "хранится запрос старта ячейки, и порт в нём — порт ячейки")
    check(refusal(lambda: s.autostart.set(22030, True)) == (400, "the start request is required to turn autostart on")
          and refusal(lambda: s.autostart.set(22030, True, {})) == (400, "the start request is required to turn "
                                                                        "autostart on"),
          "negative: включить без запроса старта — отказ 400: скауту нечем будет её запустить")
    for bad in ("", "abc", 0, 70000, None):
        check(refusal(lambda b=bad: s.autostart.set(b, True, LLAMA)) == (400, "port must be a number from 1 to 65535"),
              f"negative: порт {bad!r} — отказ 400")
    got = s.autostart.set(22024, False)
    check(got == {"ok": True, "port": 22024, "autostart": [22001]} and "22024" not in s.state["autostart"],
          "выключен — ушёл из списка и из state")
    check(s.autostart.set(22099, False) == {"ok": True, "port": 22099, "autostart": [22001]},
          "boundary: выключить невключённый — не ошибка")


def test_refresh_on_start():
    CHECKS.section("запрос обновляется при старте:")
    s = make_scout()
    s.autostart.set(22001, True, LLAMA)
    newer = {**LLAMA, "args": ["--port", "22001", "--ctx-size", "8192"]}
    s.autostart.refresh(22001, newer)
    check(s.state["autostart"]["22001"]["payload"] == newer,
          "ячейка с автозапуском запущена с новыми настройками — следующая загрузка поднимет её такой")
    s.autostart.refresh(22024, WHISPER)
    s.autostart.refresh(None, WHISPER)
    check(s.autostart.ports() == [22001], "negative: старт ячейки без автозапуска его не включает")


def test_what_a_boot_starts():
    CHECKS.section("что поднимает загрузка:")
    s = make_scout()
    s.autostart.set(22001, True, LLAMA)
    s.autostart.set(22024, True, WHISPER)
    s.autostart.set(22030, True, {**LLAMA, "port": 22030})
    started = []

    def start(payload):
        started.append(payload["port"])
        if payload["port"] == 22030:
            raise RuntimeError("no such model")
        return {"ok": True, "port": payload["port"]}
    s.cells.at(22024).process.status = lambda: {"running": True}
    out = io.StringIO()
    with patched(s.machine, boot_id=lambda: "boot-1"), patched(s.cells, start=start), contextlib.redirect_stdout(out):
        first = s.autostart.start_all()
        again = s.autostart.start_all()
    check(first == [22001] and started == [22001, 22030],
          "первый старт скаута в этой загрузке поднимает ячейки с автозапуском; работающую (усыновлённую) не трогает")
    check("[autostart] :22030 did not start: no such model" in out.getvalue()
          and "[autostart] :22024 is running already" in out.getvalue(),
          "неудача одной ячейки не мешает другим и названа в журнале")
    check(again == [] and started == [22001, 22030] and s.state["autostartBoot"] == "boot-1",
          "defect-history (как у systemd enable): рестарт скаута в той же загрузке ничего не поднимает — ячейку, "
          "которую оператор остановил, обновление скаута не вернёт")
    with patched(s.machine, boot_id=lambda: "boot-2"), patched(s.cells, start=start), \
            contextlib.redirect_stdout(io.StringIO()):
        next_boot = s.autostart.start_all()
    check(next_boot == [22001], "новая загрузка — снова поднимает")
    out = io.StringIO()
    s2 = make_scout()
    s2.autostart.set(22001, True, LLAMA)
    with patched(s2.machine, boot_id=lambda: ""), patched(s2.cells, start=start), contextlib.redirect_stdout(out):
        unknown = s2.autostart.start_all()
    check(unknown == [] and "this machine does not say which boot it is" in out.getvalue(),
          "negative: машина не говорит, какая это загрузка, — ничего не поднимается, и сказано почему: "
          "внезапный старт хуже пропущенного")


def test_routes():
    CHECKS.section("путь и старт:")
    s = make_scout()
    with patched(s.cells, start=lambda body: {"ok": True, "port": body.get("port")}), Served(s) as srv:
        on = srv.post("/api/llama-node/autostart", {"port": 22001, "enabled": True, "payload": LLAMA})
        bad = srv.post("/api/llama-node/autostart", {"port": 22001, "enabled": True})
        newer = {**LLAMA, "args": ["--port", "22001", "-c", "4096"]}
        srv.post("/api/llama-node/start", newer)
        kept = s.state["autostart"]["22001"]["payload"]
        off = srv.post("/api/llama-node/autostart", {"port": 22001, "enabled": False})
        srv.post("/api/llama-node/start", LLAMA)
    check(on == (200, {"ok": True, "port": 22001, "autostart": [22001]}),
          "POST /api/llama-node/autostart включает с запросом старта")
    check(bad == (400, {"error": "the start request is required to turn autostart on"}),
          "negative: без запроса — 400 с причиной")
    check(kept == newer, "старт через HTTP ячейки с автозапуском обновляет хранимый запрос")
    check(off == (200, {"ok": True, "port": 22001, "autostart": []}) and "22001" not in (s.state.get("autostart") or {}),
          "выключает; старт после выключения автозапуск не включает")


def test_boot_id():
    CHECKS.section("какая это загрузка:")
    linux = TMP / "boot_id"
    linux.write_text("6f1c2b3a-0000-4000-8000-000000000001\n", encoding="utf-8")
    with patched(Machine, BOOT_ID=str(linux)), patched(subprocess, run=FakeRun({})):
        got = Machine.boot_id()
    check(got == "6f1c2b3a-0000-4000-8000-000000000001", "Linux: boot_id ядра, без перевода строки")
    mac = FakeRun({("sysctl", "-n", "kern.boottime"): (0, "{ sec = 1727000000, usec = 5 } Tue Sep 24 09:00:00 2026\n")})
    with patched(Machine, BOOT_ID=str(TMP / "no-boot-id")), patched(subprocess, run=mac):
        got = Machine.boot_id()
    check(got == "{ sec = 1727000000, usec = 5 } Tue Sep 24 09:00:00 2026" and mac.calls == [["sysctl", "-n", "kern.boottime"]],
          "macOS: время загрузки из sysctl")
    with patched(Machine, BOOT_ID=str(TMP / "no-boot-id")), patched(subprocess, run=FakeRun({})):
        got = Machine.boot_id()
    check(got == "", "negative: ни того ни другого — пусто")


def test_both_reports_say_it():
    CHECKS.section("оба отчёта называют порты с автозапуском:")
    s = make_scout({"controllerUrl": "http://10.0.0.1:7990", "listenPort": 8092})
    s.autostart.set(22024, True, WHISPER)
    s.autostart.set(22001, True, LLAMA)
    quiet = {"gpus": lambda: [], "compute_apps": lambda: [], "cpu_ram": lambda: {}, "address": lambda: "10.0.0.5"}
    with patched(s.machine, **quiet), patched(s.builds, binary_version=lambda: "", binary_mtime=lambda: "",
                                              status_slim=lambda: {}):
        public, beat = s.report.public(), s.report.heartbeat()
    check(public.get("autostart") == [22001, 22024] and beat.get("autostart") == [22001, 22024],
          "/api/state и пульс — одно поле под одним именем, остановленные ячейки тоже: иначе опрос и пульс "
          "стирали бы его друг у друга, а ↟ на остановленной ячейке не узнать")


def test_the_launcher_runs_it():
    CHECKS.section("лаунчер поднимает автозапуск:")
    app = (Path(__file__).resolve().parent.parent / "caravan_scout" / "app.py").read_text(encoding="utf-8")
    adopt, start = app.find("agent.cells.adopt_survivors()"), app.find("target=agent.autostart.start_all")
    serve = app.find("serve_forever()")
    check(0 <= adopt < start < serve and "threading.Thread(target=agent.autostart.start_all, daemon=True).start()" in app,
          "после усыновления выживших и до того, как откроется порт, — в отдельном потоке: медленный старт ячейки "
          "не держит порт скаута закрытым; negative: без вызова ячейки после перезагрузки не поднимутся")


for fn in (test_the_launcher_runs_it, test_turning_it_on_and_off, test_refresh_on_start, test_what_a_boot_starts, test_routes, test_boot_id,
           test_both_reports_say_it):
    fn()

sys.exit(CHECKS.finish())
