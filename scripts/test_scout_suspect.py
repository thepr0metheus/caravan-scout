#!/usr/bin/env python3
"""A fresh llama.cpp build and crashing cells: the board offers a rollback (2.6).

The controller watches its own machine: with the llama-server binary younger
than 6 hours, 3 engine crashes in 15 minutes raise a banner that offers the
previous archived build, kept up until dismissed for that build or until the
build changes. A scout's machine had nothing of the kind. Its CrashSuspect
does the same from what its watchdog sees, and both reports carry the
verdict (llamaSuspect) for the controller to draw.

Pinned by value with a clock the test moves and a build the test names: what
counts, what raises the incident and what does not, that it stays, what a
new build and a dismissal do, the build offered, the route, and that the
words and the numbers are the controller's.

Run: python3 scripts/test_scout_suspect.py
"""
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import ROOT, Checks, Served, make_scout, patched  # noqa: E402

from caravan_scout.scout import Scout  # noqa: E402
from caravan_scout.suspect import CrashSuspect  # noqa: E402

CHECKS = Checks("scout crash suspect")
check = CHECKS.check

NOW = 1_790_000_000.0
BUILT = int(NOW) - 3600
CURRENT = {"id": "20260924-090000-abc1234", "commit": "abc1234", "version": "version: 9947 (abc1234)",
           "builtAt": BUILT, "sizeMb": 88}
EARLIER = {"id": "20260920-090000-def5678", "commit": "def5678", "version": "version: 9900 (def5678)",
           "builtAt": int(NOW) - 400_000, "sizeMb": 87}
ENGINE = "CUDA error: an illegal memory access was encountered"


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class Build:
    """The machine's llama.cpp, as the test names it."""

    def __init__(self, version="version: 9947 (abc1234)", built_at=BUILT, builds=(CURRENT, EARLIER)):
        self.version, self.built_at, self.builds = version, built_at, list(builds)

    def binary_version(self):
        return self.version

    def binary_built_at(self):
        return self.built_at

    def archive(self):
        return {"ok": True, "builds": [dict(b) for b in self.builds]}


def rig(build=None, scout=None):
    s = scout or make_scout()
    clock = Clock()
    return s, CrashSuspect(s.state, build or Build(), clock=clock), clock


def crash(suspect, clock, words=ENGINE, times=1, every=60):
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(times):
            suspect.crashed(words)
            clock.now += every


def test_three_in_fifteen_minutes():
    CHECKS.section("три смерти движка за 15 минут на свежей сборке:")
    s, suspect, clock = rig()
    crash(suspect, clock, times=2)
    check(suspect.verdict() == {"suspect": False}, "две — ещё нет")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        suspect.crashed(ENGINE)
    got = suspect.verdict()
    check(got == {"suspect": True, "crashes15m": 3, "builtAt": BUILT, "currentCommit": "abc1234",
                  "firstSeenAt": int(NOW) + 120, "lastSeenAt": int(NOW) + 120,
                  "restoreCandidate": {k: EARLIER[k] for k in ("id", "commit", "version", "builtAt", "sizeMb")}},
          f"третья — баннер: сколько, какая сборка, когда впервые и последний раз, и сборка до неё (got {got})")
    check("[suspect] 3 crashes in 15 minutes on a llama.cpp build 62 minutes old (abc1234)" in out.getvalue(),
          "журнал скаута говорит, что доска предложит откат")
    clock.now += 60
    crash(suspect, clock)
    got = suspect.verdict()
    check(got["crashes15m"] == 4 and got["firstSeenAt"] == int(NOW) + 120 and got["lastSeenAt"] == int(NOW) + 180,
          "дальше — счёт растёт, «впервые» остаётся, «последний раз» движется")


def test_what_does_not_raise_it():
    CHECKS.section("что баннера не поднимает:")
    s, suspect, clock = rig()
    crash(suspect, clock, times=3, every=500)
    check(suspect.verdict() == {"suspect": False},
          "negative: три за 17 минут (раз в 500 с) — окно 15 минут сдвигается, в нём две")
    s, suspect, clock = rig()
    crash(suspect, clock, times=3, every=440)
    check(suspect.verdict().get("suspect") is True, "boundary: три за 14,7 минуты — все в окне")
    s, suspect, clock = rig()
    crash(suspect, clock, words="model file not found: /m/a.gguf", times=3)
    crash(suspect, clock, words="bind: address already in use", times=3)
    check(suspect.verdict() == {"suspect": False},
          "negative: ячейка не стартует без модели или порта — сборка ни при чём, не считается")
    for words in ("GGML_ABORT(\"fatal\")", "SIGSEGV", "SIGABRT", "Aborted (core dumped)", "CUDA error 700"):
        s, suspect, clock = rig()
        crash(suspect, clock, words=f"E main: {words}", times=3)
        check(suspect.verdict().get("suspect") is True, f"слова смерти движка «{words}» — считаются")
    s, suspect, clock = rig(Build(built_at=int(NOW) - 6 * 3600))
    crash(suspect, clock, times=3)
    check(suspect.verdict() == {"suspect": False}, "negative: сборке 6 часов — уже не свежая")
    s, suspect, clock = rig(Build(version="", built_at=0))
    crash(suspect, clock, times=3)
    check(suspect.verdict() == {"suspect": False} and "llamaSuspect" not in s.state,
          "negative: бинаря нет — нечего подозревать")


def test_it_stays():
    CHECKS.section("баннер держится:")
    s, suspect, clock = rig()
    crash(suspect, clock, times=3)
    clock.now += 5 * 3600
    check(suspect.verdict().get("suspect") is True,
          "падения ушли из окна, сборка уже не свежая — баннер тот же, пока его не убрали: доску открывают и через час")
    again = Scout(s.state.path.parent / "config.json", s.state.path)
    fresh = CrashSuspect(again.state, Build(), clock=clock)
    check(fresh.verdict().get("suspect") is True, "и после рестарта скаута — он в state.json")


def test_a_new_build():
    CHECKS.section("новая сборка — с чистого листа:")
    s, suspect, clock = rig()
    crash(suspect, clock, times=3)
    suspect.builds = Build(version="version: 9900 (def5678)", built_at=EARLIER["builtAt"])
    check(suspect.verdict() == {"suspect": False} and "llamaSuspect" not in s.state,
          "откатили (другой коммит и время бинаря) — баннера нет, запись снята")
    suspect.builds = Build(version="version: 9947 (abc1234)", built_at=BUILT + 60)
    crash(suspect, clock, times=3)
    check(suspect.verdict().get("suspect") is True, "пересобрали тот же коммит — новая сборка, новые падения считаются")


def test_dismissed():
    CHECKS.section("скрыть — для этой сборки:")
    s, suspect, clock = rig()
    crash(suspect, clock, times=3)
    got = suspect.dismiss()
    check(got == {"ok": True, "dismissed": f"abc1234:{BUILT}"} and suspect.verdict() == {"suspect": False},
          "скрыт — баннера нет")
    crash(suspect, clock, times=3)
    check(suspect.verdict() == {"suspect": False} and "llamaSuspect" not in s.state,
          "negative: эта сборка падает дальше — оператор уже сказал «знаю», не поднимается")
    suspect.builds = Build(built_at=BUILT + 60)
    crash(suspect, clock, times=3)
    check(suspect.verdict().get("suspect") is True, "новая сборка — снова может")


def test_the_build_offered():
    CHECKS.section("что предлагается откатить:")
    s, suspect, clock = rig(Build(builds=[CURRENT]))
    crash(suspect, clock, times=3)
    check(suspect.verdict()["restoreCandidate"] is None,
          "negative: в архиве только текущая — предложить нечего, баннер без кнопки")
    third = dict(EARLIER, id="20260922-090000-0a0a0a0", commit="0a0a0a0")
    s, suspect, clock = rig(Build(builds=[CURRENT, third, EARLIER]))
    crash(suspect, clock, times=3)
    check(suspect.verdict()["restoreCandidate"]["id"] == "20260922-090000-0a0a0a0",
          "самая свежая из других коммитов (архив — от новых к старым)")
    s, suspect, clock = rig(Build(version="version: 9947 (abc1234def)", builds=[dict(CURRENT), EARLIER]))
    crash(suspect, clock, times=3)
    check(suspect.verdict()["restoreCandidate"]["id"] == EARLIER["id"],
          "boundary: коммит той же сборки записан короче или длиннее — это она же, не кандидат")


def test_the_route():
    CHECKS.section("путь:")
    s = make_scout()
    with patched(s.builds, binary_version=lambda: "version: 9947 (abc1234)", binary_built_at=lambda: BUILT), \
            Served(s) as srv:
        got = srv.post("/api/llama-node/suspect-dismiss", {})
    check(got == (200, {"ok": True, "dismissed": f"abc1234:{BUILT}"}) and s.state["llamaSuspectDismissed"] == f"abc1234:{BUILT}",
          "POST /api/llama-node/suspect-dismiss скрывает для этой сборки")


def test_the_rule_is_ours():
    # The controller read the same words and numbers from its own cells'
    # journal, and this test compared the two. Its cells — and that rule — went
    # in its step 6.9 (their machine runs them through its scout), so the
    # banner comes from this rule alone.
    CHECKS.section("правило — скаута (2.9.1):")
    check(CrashSuspect.MARKERS.pattern == r"CUDA error|GGML_ABORT|SIGSEGV|SIGABRT|Aborted \(core dumped\)",
          "слова смерти движка: CUDA error, GGML_ABORT, SIGSEGV, SIGABRT, дамп ядра")
    check([CrashSuspect.MIN_CRASHES, CrashSuspect.FRESH_SEC // 3600, CrashSuspect.WINDOW_SEC // 60] == [3, 6, 15],
          "3 падения, 6 часов, 15 минут — правило баннера")
    repo = ROOT.parent / "lama-caravan"
    if not repo.is_dir():
        print("  (репозитория контроллера рядом нет — его сторона не проверена)")
        return
    own = [str(p.relative_to(repo)) for p in (repo / "caravan").rglob("*.py")
           if "LLAMA_SUSPECT_MIN_CRASHES" in p.read_text(encoding="utf-8")]
    check(own == [], f"negative: своего правила у контроллера нет (ушло с его ячейками в шаге 6.9) — одно правило, у скаута (got {own})")


for fn in (test_three_in_fifteen_minutes, test_what_does_not_raise_it, test_it_stays, test_a_new_build, test_dismissed,
           test_the_build_offered, test_the_route, test_the_rule_is_ours):
    fn()

sys.exit(CHECKS.finish())
