#!/usr/bin/env python3
"""The machine second by second, for the board's charts (2.8).

The controller draws its own machine from a sample a second; a scout's
machine was drawn from its reports — a GPU reading kept ten seconds — and
the machine the controller runs on will be a scout's too. Telemetry samples
the cards and the processor every second while a board watches (the
controller asks `since` about once a second), every ten seconds otherwise,
and keeps ten minutes.

Pinned by value with a fake machine, a fake /proc/stat and a clock the test
moves: what a sample holds, the processor share and its stand-in, the
cadence, what is kept, what an ask returns, the route and the launcher.

Run: python3 scripts/test_scout_telemetry.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import ROOT, TMP, Checks, Served, make_scout, patched  # noqa: E402

from caravan_scout.telemetry import Telemetry  # noqa: E402

CHECKS = Checks("scout telemetry")
check = CHECKS.check


class Machine:
    def __init__(self):
        self.gpus = [{"index": "0", "memoryUsedMiB": "20480", "memoryTotalMiB": "24576", "utilizationGpuPct": "37",
                      "powerDrawW": "212.40", "temperatureC": "61"}]
        self.cpu = {"loadPct": 12.5, "ram": {"usedGb": 18.2, "totalGb": 62.7}}

    def nvidia_gpus(self):
        return [dict(g) for g in self.gpus]

    def cpu_ram(self):
        return dict(self.cpu)


class Clock:
    def __init__(self):
        self.now = 1_790_000_000.0

    def __call__(self):
        return self.now


def rig(stat=True):
    clock = Clock()
    tel = Telemetry(Machine(), clock=clock)
    path = TMP / f"stat-{id(tel)}"
    tel.PROC_STAT = path if stat else TMP / "no-such-proc-stat"
    return tel, clock, path


def stat(path, busy, idle):
    path.write_text(f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 1 1 1\n", encoding="utf-8")


def test_a_sample():
    CHECKS.section("что в одном замере:")
    tel, clock, path = rig()
    stat(path, 1000, 9000)
    first = tel.sample()
    check(first == {"t": 1_790_000_000, "gpus": [{"index": 0, "memUsedMiB": 20480.0, "memTotalMiB": 24576.0,
                                                  "utilPct": 37.0, "powerW": 212.4, "tempC": 61.0}],
                    "cpuPct": None, "ram": {"usedGb": 18.2, "totalGb": 62.7}},
          f"карты — числами, с номером; память; процессор первого замера неизвестен — не с чем сравнить (got {first})")
    stat(path, 1300, 9700)
    clock.now += 1
    check(tel.sample()["cpuPct"] == 30.0, "процессор — доля занятого времени между замерами (/proc/stat), как у контроллера")
    tel, clock, _ = rig(stat=False)
    check(tel.sample()["cpuPct"] == 12.5, "negative: /proc/stat нет (macOS) — средняя загрузка за минуту, как в отчёте скаута")
    tel, clock, path = rig()
    tel.machine.gpus = []
    stat(path, 1, 1)
    check(tel.sample()["gpus"] == [], "negative: карт нет — пустой список, а не выдуманная карта")
    tel.machine.gpus = [{"index": "0", "memoryUsedMiB": "[N/A]", "utilizationGpuPct": ""}]
    got = tel.sample()["gpus"][0]
    check(got["memUsedMiB"] is None and got["utilPct"] is None, "boundary: [N/A] и пусто — None, а не 0")


def test_what_is_kept():
    CHECKS.section("что хранится:")
    tel, clock, path = rig()
    stat(path, 1, 1)
    tel.sample()
    tel.sample()
    check(len(tel.since()["samples"]) == 1, "два замера в одну секунду — один, последний")
    for _ in range(700):
        clock.now += 1
        tel.sample()
    rows = tel.since()["samples"]
    check(rows[0]["t"] == int(clock.now) - 600 and rows[-1]["t"] == int(clock.now),
          "хранятся 10 минут — старее уходит")
    got = tel.since(int(clock.now) - 3)
    check([r["t"] for r in got["samples"]] == [int(clock.now) - 2, int(clock.now) - 1, int(clock.now)]
          and got["retentionSeconds"] == 600 and got["watchedSeconds"] == 1.0 and got["idleSeconds"] == 10.0,
          "since — только новее; ответ говорит шаг и глубину")
    check(len(tel.since("мусор")["samples"]) == 601, "negative: since не числом — вся история, а не ошибка")


def test_the_cadence():
    CHECKS.section("шаг: смотрят — раз в секунду, нет — раз в 10 с:")
    tel, clock, path = rig()
    stat(path, 1, 1)
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds
        if len(slept) == 3:
            tel.since(0)
        if len(slept) >= 6:
            raise KeyboardInterrupt

    try:
        tel.run(sleep=sleep)
    except KeyboardInterrupt:
        pass
    check(slept == [10.0, 10.0, 10.0, 1.0, 1.0, 1.0],
          f"доску не смотрят — раз в 10 с; спросили (во время третьего сна) — дальше раз в секунду (got {slept})")
    clock.now += 31
    check(not tel.watched(), "30 с без вопросов — снова не смотрят")

    def broken():
        raise RuntimeError("nvidia-smi hung")
    tel.machine.nvidia_gpus = broken
    after = []

    def once(seconds):
        after.append(seconds)
        raise KeyboardInterrupt
    try:
        tel.run(sleep=once)
    except KeyboardInterrupt:
        pass
    check(after == [10.0], "negative: замер упал — цикл не кончился, ждёт следующего")


def test_the_route():
    CHECKS.section("путь и лаунчер:")
    s = make_scout()
    s.telemetry.PROC_STAT = TMP / "no-such-proc-stat"
    with patched(s.telemetry, machine=Machine()):
        s.telemetry.sample()
        with Served(s) as srv:
            code, body = srv.get(f"/api/telemetry?since={int(s.telemetry.now()) - 5}")
            empty = srv.get(f"/api/telemetry?since={int(s.telemetry.now()) + 5}")
    check(code == 200 and len(body["samples"]) == 1 and body["samples"][0]["gpus"][0]["index"] == 0,
          "GET /api/telemetry?since= — новые замеры")
    check(empty[0] == 200 and empty[1]["samples"] == [], "negative: новых нет — пустой список")
    app = (ROOT / "caravan_scout" / "app.py").read_text(encoding="utf-8")
    check("threading.Thread(target=agent.telemetry.run, daemon=True).start()" in app
          and app.find("agent.telemetry.run") < app.find("serve_forever()"),
          "лаунчер запускает замеры в своём потоке до того, как откроется порт")


def test_both_reports_say_it():
    CHECKS.section("оба отчёта говорят, что машина снимается:")
    s = make_scout({"controllerUrl": "http://10.0.0.1:7990", "listenPort": 8092})
    quiet = {"gpus": lambda: [], "compute_apps": lambda: [], "cpu_ram": lambda: {}, "address": lambda: "10.0.0.5"}
    with patched(s.machine, **quiet), patched(s.builds, binary_version=lambda: "", binary_mtime=lambda: "",
                                              status_slim=lambda: {}, binary_built_at=lambda: 0):
        public, beat = s.report.public(), s.report.heartbeat()
    want = {"watchedSeconds": 1.0, "idleSeconds": 10.0, "retentionSeconds": 600}
    check(public.get("telemetry") == want and beat.get("telemetry") == want,
          "/api/state и пульс — одно поле: контроллер спрашивает замеры только у скаута, который так сказал "
          "(у старшего — не спрашивает и не гадает по 404)")


for fn in (test_a_sample, test_what_is_kept, test_the_cadence, test_the_route, test_both_reports_say_it):
    fn()

sys.exit(CHECKS.finish())
