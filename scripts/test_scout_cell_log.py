#!/usr/bin/env python3
"""A cell's log for the board's window (2.36): the last lines of a run, and the runs there are to read.

The board's cell window had only what the scout reported of a failure — a reason and eight lines. The owner's window
asks for the log itself: the last lines of the present run, and of the runs before it, which rotate keeps. What is read
leaves the machine, so keys are scrubbed out of it as out of a crash's tail, and only the cell's own log is read: a name
that is not one of its runs reads nothing.

Pinned by value, in a temp dir: how many lines a view asks for, the lines (blank ones left out, keys scrubbed, the cut
first line of a long file left out), the runs (newest first, another file of the folder not among them), a run by its
name, where a port's log is found (the process's, the record's, the cache's), the answers for a port with no log and a
name that is not a run, and the route behind the fleet token.

Run: python3 scripts/test_scout_cell_log.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import TMP, Checks, Served, make_scout, patched  # noqa: E402

from caravan_scout.process import CellLog  # noqa: E402

CHECKS = Checks("scout cell log")
check = CHECKS.check
TOKEN = "fleet-token-for-the-log-test"


def folder(name):
    path = TMP / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def write(path, text, mtime=None):
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_how_many_lines():
    CHECKS.section("сколько строк просит вид:")
    count = CellLog.view_count
    check([count(None), count(""), count("abc"), count([1])] == [300, 300, 300, 300],
          "не число — 300 строк, сколько вид берёт, когда не сказано")
    check([count("50"), count(" 7 "), count(1), count(2000)] == [50, 7, 1, 2000], "число от 1 до 2000 — столько и берётся")
    check([count("0"), count(-5), count("99999")] == [1, 1, 2000],
          "negative: меньше одной — одна, больше 2000 — 2000: журнал целиком за раз не читается")


def test_the_lines():
    CHECKS.section("строки запуска:")
    d = folder("lines")
    log = CellLog(write(d / "llama-server.22020.log",
                        "start\n\n   \nload model a.gguf\nkey sk-abcdef1234567890 in a line\nsrv  listening on 0.0.0.0:22020   \n"))
    check(log.lines() == ["start", "load model a.gguf", "key sk-… in a line", "srv  listening on 0.0.0.0:22020"],
          "все строки по порядку, пустые выпали, хвостовые пробелы срезаны, ключ вычищен как в хвосте сбоя")
    check(log.lines("2") == ["key sk-… in a line", "srv  listening on 0.0.0.0:22020"], "просили две — последние две")
    check(CellLog(None).lines() == [] and CellLog(d / "no-such.log").lines() == [],
          "negative: процесс без журнала и журнал, которого нет, — пусто, без ошибки")
    with patched(CellLog, VIEW_BYTES=24):
        cut = CellLog(write(d / "long.log", "first line of the file\nsecond line here\nthird line\n")).lines()
    check(cut == ["third line"],
          "длинный файл читается с конца: первая прочитанная строка обрезана посередине — её нет, а не показан обрубок")


def test_the_runs():
    CHECKS.section("запуски — этот и прежние:")
    d = folder("runs")
    present = write(d / "llama-server.22020.log", "now\n", mtime=1_790_000_300)
    write(d / "llama-server.22020.20261024-204735.log", "before\n", mtime=1_790_000_200)
    write(d / "llama-server.22020.20261024-204735-1.log", "two in one second\n", mtime=1_790_000_250)
    write(d / "llama-server.22020.notes.log", "not a run\n", mtime=1_790_000_400)
    write(d / "llama-server.220201.20261024-204735.log", "another port\n", mtime=1_790_000_500)
    write(d / "command-cell.22020.log", "another kind\n", mtime=1_790_000_600)
    log = CellLog(present)
    runs = log.runs()
    check([r["name"] for r in runs] == ["llama-server.22020.log", "llama-server.22020.20261024-204735-1.log",
                                        "llama-server.22020.20261024-204735.log"],
          "этот журнал и те, что rotate отложил, — новые первыми")
    check(runs[0] == {"name": "llama-server.22020.log", "bytes": 4, "modifiedAt": 1_790_000_300}, "у каждого — имя, размер и время")
    check(all(r["name"] not in ("llama-server.22020.notes.log", "llama-server.220201.20261024-204735.log",
                                "command-cell.22020.log") for r in runs),
          "negative: другой файл папки, другой порт и другой вид ячейки — не её запуски")
    check(log.run("llama-server.22020.20261024-204735.log").lines() == ["before"], "прежний запуск читается по имени")
    check(log.run("llama-server.22020.notes.log") is None and log.run("../../etc/passwd") is None and log.run("") is None,
          "negative: имя не из её запусков, путь наружу и пустое имя — ничего не читается")
    present.unlink()
    check([r["name"] for r in CellLog(present).runs()][0] == "llama-server.22020.20261024-204735-1.log",
          "журнала этого запуска нет — остаются прежние")
    check(CellLog(None).runs() == [], "negative: процесс без журнала — запусков нет")


def test_where_a_port_logs():
    CHECKS.section("где журнал ячейки на порту:")
    scout = make_scout()
    cache = scout.cells.models.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    check(scout.cells.log("abc") == {"ok": False, "error": "port must be a number"}, "negative: порт не число — так и сказано")
    check(scout.cells.log(22031) == {"ok": False, "error": "no log of a cell on port 22031 here"},
          "negative: журнала нет — ошибка со словами, а не пустой журнал")
    check(22031 not in scout.cells.by_port, "negative: вопрос о журнале не заводит ячейку на порту")
    write(cache / "command-cell.22031.log", "whisper up\nserving\n")
    got = scout.cells.log("22031", "1")
    check(got == {"ok": True, "port": 22031, "run": "command-cell.22031.log", "lines": ["serving"],
                  "runs": got["runs"]} and [r["name"] for r in got["runs"]] == ["command-cell.22031.log"],
          "ячейка, о которой скаут не знает, — журнал её имени в кэше моделей")
    named = write(folder("record") / "elsewhere.log", "from the record\n")
    scout.cells.records.add(22032, "llama", 4242, "llama-server --port 22032", {"port": 22032}, named, False)
    check(scout.cells.log(22032).get("lines") == ["from the record"], "после перезапуска скаута — журнал, который назвала запись")
    held = write(folder("process") / "llama-server.22033.log", "held by the process\n")
    cell = scout.cells.at(22033)
    with patched(cell.process, _log=CellLog(held)):
        check(scout.cells.log(22033).get("lines") == ["held by the process"], "у процесса, который скаут держит, — его журнал")
    write(cache / "llama-server.22034.log", "now\n", mtime=1_790_000_300)
    write(cache / "llama-server.22034.20261024-204735.log", "the crash before\n", mtime=1_790_000_200)
    check(scout.cells.log(22034, run="llama-server.22034.20261024-204735.log").get("lines") == ["the crash before"],
          "прежний запуск — по имени из runs")
    check(scout.cells.log(22034, run="llama-server.22034.notes.log")
          == {"ok": False, "error": "'llama-server.22034.notes.log' is not a log of the cell on port 22034"},
          "negative: имя не из её запусков — отказ со словами")


def test_the_route():
    CHECKS.section("маршрут:")
    scout = make_scout({"controllerToken": TOKEN})
    cache = scout.cells.models.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    write(cache / "llama-server.22035.log", "a\nb\nc\n")
    with Served(scout) as srv:
        auth = {"X-Caravan-Token": TOKEN}
        code, body = srv.get("/api/llama-node/log?port=22035&lines=2", headers=auth)
        check(code == 200 and body["ok"] is True and body["lines"] == ["b", "c"] and body["run"] == "llama-server.22035.log",
              "GET /api/llama-node/log?port=&lines= — последние строки и имя запуска")
        code, body = srv.get("/api/llama-node/log?port=22036", headers=auth)
        check(code == 200 and body == {"ok": False, "error": "no log of a cell on port 22036 here"},
              "порт без журнала — ответ с ошибкой, как у остальных чтений скаута")
        check(srv.get("/api/llama-node/log?port=22035")[0] == 401, "negative: без токена флота журнал не отдаётся")


if __name__ == "__main__":
    test_how_many_lines()
    test_the_lines()
    test_the_runs()
    test_where_a_port_logs()
    test_the_route()
    sys.exit(CHECKS.finish())
