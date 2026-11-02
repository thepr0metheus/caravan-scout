#!/usr/bin/env python3
"""Snapshot: how many models the model cache keeps, and the trim that deletes the rest (2.34).

The owner, 2026-10-18, when the disk of a machine was 94 % full of model copies and a start died with "No space left on device":
keep in the cache only the last models, from 1 to 5, 1 until said otherwise, each with all its files; what is older goes by itself.

What is pinned, by value (caravan_scout/cache_keeper.py):
  - the setting (CacheKeep): 1 to 5, 1 by default, kept in state.json, anything else refused — not rounded into a number;
  - the log of starts (ModelUses): newest first, a model that ran keeps its word when it is started again, a start that never
    reached a running process is not "the last model", junk in state.json is skipped;
  - the trim (CacheKeeper.trim): the last `keep` models that ran stay with all their files (parts, projector, draft layer, a folder),
    so does everything a running or starting cell uses whatever its age, so does a file being written; only what this scout
    downloaded is ever deleted (the files it did not are FOREIGN, shown and counted, never deleted by themselves); and nothing is
    deleted while no model is known to have run — "unknown" is not "nothing";
  - the foreign files: counted without the hidden scratch and the logs, deleted only with the figure the operator was shown;
  - the figures the reports carry (facts) and the three routes the board calls.

Nothing real is touched (see _scout_harness.py): files live in temp dirs, the host's doors are shut.

Run: PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_scout_cache_keeper.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks, RealCallBlocked, Served, make_scout, patched  # noqa: E402

from caravan_scout import cache_keeper  # noqa: E402
from caravan_scout.cache_keeper import CacheKeep, ModelUses  # noqa: E402
from caravan_scout.errors import AppError  # noqa: E402
from caravan_scout.state import ScoutState  # noqa: E402

CHECKS = Checks("scout cache keeper")
check = CHECKS.check

A, B, C = "org/a/q4/a-Q4.gguf", "org/b/q4/b-Q4.gguf", "org/c/q4/c-Q4.gguf"
C_MM = "org/c/mmproj-c.gguf"
S1, S2 = "org/s/q4/s-00001-of-00002.gguf", "org/s/q4/s-00002-of-00002.gguf"
FOLDER = "fold/x"
FOREIGN = "org/f/foreign.gguf"


def lay(root: Path, files: dict) -> None:
    for rel, size in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)


def raises(fn):
    try:
        fn()
    except AppError as exc:
        return exc.status, str(exc.message if hasattr(exc, "message") else exc)
    return None


def shelf(setting=None):
    """A scout whose cache holds three models that ran (A oldest, C newest with its projector), a split model, a folder model and a
    file this scout did not download."""
    s = make_scout()
    cache = s.models.cache_dir()
    lay(cache, {A: 10, B: 20, C: 30, C_MM: 5, S1: 7, S2: 8, FOREIGN: 9, f"{FOLDER}/FP32/weights.bin": 40, f"{FOLDER}/FP32/config.json": 2})
    for rel in (A, B, C, C_MM, S1, S2):
        s.models.downloaded.add(cache / rel)
    s.models.downloaded.add(cache / FOLDER)
    keeper = s.cells.keeper
    keeper.held, keeper.live = (lambda: []), (lambda: set())
    if setting:
        keeper.keep.set(setting)
    return s, keeper, cache


def ran(keeper, port, *files):
    """A start of these files that got as far as a running process."""
    keeper.begin(port, list(files))
    keeper.uses.commit(port)


def left(cache: Path):
    return sorted(str(p.relative_to(cache)) for p in cache.rglob("*") if p.is_file() and not p.name.startswith("."))


# ── the setting ───────────────────────────────────────────────────────────────

def test_setting():
    CHECKS.section("the setting:")
    s = make_scout()
    keep = s.cells.keeper.keep
    check(keep.value() == 1, "until set, the cache keeps the last 1 model")
    check(keep.set(3) == 3 and keep.value() == 3, "set(3): kept")
    check(json.loads(s.state.path.read_text(encoding="utf-8")).get("cacheKeep") == 3
          and CacheKeep(ScoutState(s.state.path)).value() == 3,
          "it is written to state.json, the scout's own memory (not config.json) — a scout that starts again reads 3")
    for bad in (0, 6, -1, 100, "3", 2.5, True, False, None, [], {}):
        err = raises(lambda: keep.set(bad))
        check(err == (400, "keep must be a whole number from 1 to 5") and keep.value() == 3,
              f"negative: {bad!r} is refused with 400 and the setting stays 3 — not rounded, not clamped")
    check(keep.set(1) == 1 and keep.set(5) == 5, "boundary: 1 and 5 are the two ends, both valid")
    for stored in (9, 0, "2", True, 2.0, None, [1]):
        broken = make_scout(state={"cacheKeep": stored})
        check(broken.cells.keeper.keep.value() == 1, f"negative: {stored!r} in state.json reads as the default 1 — a hand-edited file does not break the trim")
    check(make_scout(state={"cacheKeep": 4}).cells.keeper.keep.value() == 4, "a valid value in state.json is the setting")
    check("cleanOldModels" not in make_scout().config, "the old flag cleanOldModels is gone from the defaults: the setting replaces it")


# ── the log of starts ─────────────────────────────────────────────────────────

def test_uses():
    CHECKS.section("the log of starts:")
    s = make_scout()
    uses = s.cells.keeper.uses
    uses.begin(22001, [A, "", "  ", C_MM])
    first = uses.entries()[0]
    check(first["files"] == [A, C_MM] and first["port"] == 22001 and first["ok"] is False and isinstance(first["at"], int),
          "a start is told before its files are copied: the model and its projector, blanks left out, not yet ok")
    uses.begin(22002, [B])
    check([e["files"][0] for e in uses.entries()] == [B, A], "newest first")
    uses.commit(22001)
    check([(e["files"][0], e["ok"]) for e in uses.entries()] == [(B, False), (A, True)], "commit marks the entry of that port: the model ran")
    uses.begin(22003, [A])
    check([(e["files"][0], e["port"], e["ok"]) for e in uses.entries()] == [(A, 22003, True), (B, 22002, False)],
          "the same model started again comes to the front with its new port and KEEPS its word (a model that ran is not unmade by a start that may fail)")
    uses.commit(99999)
    check(len(uses.entries()) == 2, "negative: committing a port nobody started changes nothing")
    uses.begin(22009, ["", "  "])
    check(len(uses.entries()) == 2, "negative: a start that names no file is no entry")
    for n in range(30):
        uses.begin(23000 + n, [f"m{n}.gguf"])
    check(len(uses.entries()) == ModelUses.LIMIT == 20 and uses.entries()[0]["files"] == ["m29.gguf"],
          "boundary: twenty entries are kept, the oldest go")
    s.state["modelUse"] = [1, "x", {"files": []}, {"files": [3]}, {"files": ["a.gguf"], "port": 1, "ok": True}, {"nofiles": 1}]
    check([e["files"] for e in uses.entries()] == [["a.gguf"]], "negative: junk in state.json is skipped, a sound entry is read")
    check(uses.newest(1) == [{"files": ["a.gguf"], "port": 1, "ok": True}] and uses.newest(0) == [],
          "newest(n) counts only models that ran; none asked, none given")
    ran(s.cells.keeper, 22001, A)
    check(json.loads(s.state.path.read_text(encoding="utf-8"))["modelUse"][0]["files"] == [A], "the log is written to state.json: it outlives a restart")


# ── the trim ──────────────────────────────────────────────────────────────────

def test_trim():
    CHECKS.section("the trim:")
    s, keeper, cache = shelf()
    result = keeper.trim()
    check(result == {"removed": 0, "freedBytes": 0, "waiting": True} and len(left(cache)) == 9,
          "waiting: no model is known to have run — nothing is deleted, though the cache is full of downloads (unknown is not nothing)")
    keeper.begin(22005, [A])
    check(keeper.trim()["waiting"] is True and len(left(cache)) == 9,
          "negative: a start that has not reached a running process is not a model that ran — still waiting")

    s, keeper, cache = shelf()
    ran(keeper, 22001, A)
    ran(keeper, 22002, B)
    ran(keeper, 22003, C, C_MM)
    result = keeper.trim()
    check(left(cache) == sorted([C, C_MM, FOREIGN]), "keep 1: the newest model and its projector stay; the older ones, the split model and the folder go")
    check(result == {"removed": 5, "freedBytes": 10 + 20 + 7 + 8 + 40 + 2, "waiting": False},
          "the answer counts what went: 5 entries (A, B, both parts, the folder as ONE entry), 87 bytes — the folder's every file counted in the bytes")
    check((cache / FOREIGN).is_file(), "negative: the file this scout did not download stays, whatever its age — it is not in the ledger")
    check(not (cache / "org" / "a").exists() and not (cache / "fold").exists() and (cache / "org" / "c" / "q4").is_dir(),
          "the folders a deletion left empty go with it; the kept model's folders stay")
    check(keeper.trim() == {"removed": 0, "freedBytes": 0, "waiting": False}, "boundary: a second trim finds nothing to do")

    s, keeper, cache = shelf(setting=2)
    ran(keeper, 22001, A)
    ran(keeper, 22002, B)
    ran(keeper, 22003, C, C_MM)
    keeper.trim()
    check(B in left(cache) and C in left(cache) and A not in left(cache), "keep 2: the last two models stay")

    s, keeper, cache = shelf(setting=5)
    ran(keeper, 22001, A)
    ran(keeper, 22002, B)
    ran(keeper, 22003, C, C_MM)
    keeper.trim()
    check({A, B, C, C_MM} <= set(left(cache)) and S1 not in left(cache),
          "keep 5 with three models that ran: all three stay; the split model that never ran in this log is not one of them")

    s, keeper, cache = shelf()
    ran(keeper, 22001, S1)
    ran(keeper, 22002, C)
    keeper.set_keep(2)
    check(S1 in left(cache) and S2 in left(cache),
          "the first part of a split model brings the other parts: the engine opens them by name (SplitGguf)")

    s, keeper, cache = shelf()
    ran(keeper, 22001, FOLDER)
    ran(keeper, 22002, C)
    keeper.set_keep(1)
    check(not (cache / FOLDER).exists(), "a folder model that is not the last goes with its files")
    s, keeper, cache = shelf()
    ran(keeper, 22001, C)
    ran(keeper, 22002, FOLDER)
    keeper.trim()
    check((cache / FOLDER / "FP32" / "weights.bin").is_file() and C not in left(cache), "a folder model that is the last stays whole")

    s, keeper, cache = shelf()
    ran(keeper, 22001, A)
    ran(keeper, 22002, C)
    keeper.held = lambda: [str(cache / A)]
    keeper.trim()
    check(A in left(cache) and B not in left(cache),
          "a running process holds its file: it stays whatever its age (the last model, C, stays too) — a live server is not broken")
    keeper.held = lambda: []

    s, keeper, cache = shelf()
    ran(keeper, 22001, A)
    keeper.begin(22002, [B])
    ran(keeper, 22003, C, C_MM)
    keeper.live = lambda: {22002}
    keeper.trim()
    check(B in left(cache) and A not in left(cache),
          "a cell that is still starting has asked for its files from its first moment: B stays though it is not the newest and has not run")
    keeper.live = lambda: set()
    keeper.trim()
    check(B not in left(cache),
          "negative: the same start that failed (its port is not live now) is no model that ran: its copy goes, a half-copied file with it")

    s, keeper, cache = shelf()
    ran(keeper, 22001, A)
    ran(keeper, 22002, B)
    ran(keeper, 22003, C, C_MM)
    keeper.live = lambda: {22001}
    keeper.trim()
    check(A in left(cache) and B not in left(cache), "a model that ran and whose cell is alive stays beyond the setting")
    keeper.live = lambda: set()

    s, keeper, cache = shelf()
    ran(keeper, 22001, A)
    ran(keeper, 22002, C, C_MM)
    tmp = cache / "org" / "b" / "q4" / "b-Q4.gguf.tmp"
    lay(cache, {"org/b/q4/b-Q4.gguf.tmp": 3})
    s.models.downloaded.add(tmp)
    s.models._flying(tmp, True)
    keeper.trim()
    check(tmp.is_file(), "a file being written now is never deleted — a trim on one port does not break a download on another")
    s.models._flying(tmp, False)
    keeper.trim()
    check(not tmp.exists(), "when the download ends or fails, its .tmp goes with the next trim")

    s, keeper, cache = shelf(setting=3)
    ran(keeper, 22001, A)
    ran(keeper, 22002, B)
    ran(keeper, 22003, C, C_MM)
    result = keeper.set_keep(1)
    check(result["keep"] == 1 and result["waiting"] is False and A not in left(cache) and B not in left(cache) and C in left(cache),
          "set_keep(1) after 3: the number is kept and the cache is trimmed to it at once — the owner does not wait for the next start")
    before = left(cache)
    check(keeper.set_keep(5)["removed"] == 0 and left(cache) == before,
          "negative: raising the number when the cache holds only what was kept deletes nothing more")
    check(raises(lambda: keeper.set_keep(0)) == (400, "keep must be a whole number from 1 to 5") and keeper.keep.value() == 5,
          "negative: a refused number changes nothing and trims nothing")


# ── the files this scout did not download ─────────────────────────────────────

def test_foreign():
    CHECKS.section("the foreign files:")
    s, keeper, cache = shelf()
    lay(cache, {".cells/22001/scratch.bin": 50, "llama-server.22001.log": 4, "stray.tmp": 1, "half.part": 1, "org/f/readme.txt": 6,
                "other/zz.gguf": 12, ".hidden/x.gguf": 99})
    found = {p.relative_to(cache).as_posix(): size for p, size in keeper.foreign_files()}
    check(found == {FOREIGN: 9, "org/f/readme.txt": 6, "other/zz.gguf": 12},
          "foreign: the files in the folder that are not in the ledger and not under a ledger folder; the hidden scratch, the logs, .tmp and .part are not models")
    check(FOLDER + "/FP32/weights.bin" not in found and A not in found, "negative: what the ledger holds (a file, and a folder's every file) is not foreign")
    facts = keeper.facts()["foreign"]
    check(facts == {"files": 3, "bytes": 27, "folders": [{"folder": "org", "files": 2, "bytes": 15}, {"folder": "other", "files": 1, "bytes": 12}]},
          "facts: the count, the bytes and the top folders they lie in, the biggest first (the ledger's own files in the same folder are not counted)")
    check(make_scout().cells.keeper.foreign_files() == [] and keeper.facts()["keep"] == 1,
          "boundary: a cache folder that is not there has no foreign files")
    many = make_scout()
    for n in range(12):
        lay(many.models.cache_dir(), {f"d{n:02d}/f.gguf": n + 1})
    check(len(many.cells.keeper.facts()["foreign"]["folders"]) == 8, "boundary: the folders are named eight at most — the count and the bytes are of all")

    CHECKS.section("deleting the foreign files:")
    s, keeper, cache = shelf()
    total = sum(size for _p, size in keeper.foreign_files())
    check(raises(lambda: keeper.delete_foreign(None)) == (400, "expectBytes is required: the size the operator was shown")
          and raises(lambda: keeper.delete_foreign("many")) == (400, "expectBytes is required: the size the operator was shown"),
          "negative: no figure, no deletion (400)")
    err = raises(lambda: keeper.delete_foreign(total + 1))
    check(err is not None and err[0] == 409 and "nothing was deleted" in err[1] and (cache / FOREIGN).is_file(),
          "negative: the figure is not the size now — something came or went since the operator looked: 409 and nothing is deleted")
    keeper.held = lambda: [str(cache / FOREIGN)]
    check(keeper.delete_foreign(total) == {"removed": 0, "freedBytes": 0} and (cache / FOREIGN).is_file(),
          "negative: a file a running cell holds stays even when it is foreign and the figure is right")
    keeper.held = lambda: []
    check(keeper.delete_foreign(total) == {"removed": 1, "freedBytes": 9} and not (cache / FOREIGN).exists(),
          "with the figure he was shown: the foreign files go")
    check((cache / A).is_file() and (cache / FOLDER / "FP32" / "weights.bin").is_file() and not (cache / "org" / "f").exists() and cache.is_dir(),
          "negative: what the ledger holds is not touched; the folders the deletion emptied go; the cache folder itself stays")
    lay(cache, {".cells/22001/x": 1})
    keeper.delete_foreign(0)
    check((cache / ".cells" / "22001" / "x").is_file(), "negative: the hidden scratch folder is never pruned")
    (cache / ".cells" / "22002").mkdir()
    lay(cache, {"org/zz/lonely.gguf": 4})
    keeper.delete_foreign(keeper.facts()["foreign"]["bytes"])
    check((cache / ".cells" / "22002").is_dir() and not (cache / "org" / "zz").exists(),
          "negative: an EMPTY hidden folder (a cell's scratch not yet filled) is not pruned either, while an empty ordinary one is")
    only = make_scout()
    lay(only.models.cache_dir(), {"stray.gguf": 6})
    only.cells.keeper.delete_foreign(6)
    check(only.models.cache_dir().is_dir() and left(only.models.cache_dir()) == [],
          "boundary: the last file of the cache folder goes — the folder itself stays, empty, for the next download")


# ── what the reports say ──────────────────────────────────────────────────────

def test_facts():
    CHECKS.section("the figures:")
    s, keeper, cache = shelf(setting=2)
    facts = keeper.facts()
    check(sorted(facts) == ["budgetBytes", "dir", "disk", "foreign", "keep", "managedBytes"],
          "exactly these: the two settings (2.38: the budget beside the count), the folder, what this scout holds, what it did not "
          "download, the disk")
    check(facts["budgetBytes"] is None and facts["dir"] == str(cache), "no budget until set — null, not a size made up; the folder is the cache's")
    check(facts["keep"] == 2 and facts["managedBytes"] == 10 + 20 + 30 + 5 + 7 + 8 + 40 + 2, "managedBytes is what the ledger names and the disk still holds, a folder counted whole")
    disk = facts["disk"]
    check(isinstance(disk["totalBytes"], int) and isinstance(disk["freeBytes"], int) and 0 < disk["freeBytes"] <= disk["totalBytes"],
          "the disk of the cache folder: its size and what is free, whole numbers")
    lay(cache, {"org/f/new.gguf": 100})
    check(keeper.facts()["foreign"]["bytes"] == 9 and keeper.facts()["foreign"]["files"] == 1,
          "the figures are reused for a few seconds: the heartbeat and the controller's polls both ask")
    keeper.keep.set(4)
    check(keeper.facts()["keep"] == 4 and keeper.facts()["foreign"]["bytes"] == 9,
          "negative: the setting is never reused — a number just set is the number said")
    now = [1000.0]
    with patched(cache_keeper.time, monotonic=lambda: now[0]):
        keeper.forget_figures()
        keeper.facts()
        lay(cache, {"org/f/late.gguf": 50})
        now[0] += cache_keeper.CacheKeeper.MEMO_SECONDS - 1
        before = keeper.facts()["foreign"]["bytes"]
        now[0] += 2
        after = keeper.facts()["foreign"]["bytes"]
    check(before == 109 and after == 159,
          "the figures are reused for MEMO_SECONDS (a file that came a moment ago is not seen yet) and measured again after it")
    keeper.forget_figures()
    held_before = keeper.facts()["managedBytes"]          # measured now, on the real clock: reused for the next 20 s
    ran(keeper, 22001, C)
    keeper.trim()
    check(held_before == 10 + 20 + 30 + 5 + 7 + 8 + 42 and keeper.facts()["managedBytes"] == 30,
          "a trim makes the next report measure again: right after it the figures say what is held now (the one model that ran), "
          "not the 122 bytes of the minute before")
    with patched(cache_keeper.shutil, disk_usage=lambda p: (_ for _ in ()).throw(OSError("no"))):
        keeper.forget_figures()
        check(keeper.facts()["disk"] is None, "negative: a disk the system will not size says None, not zero bytes free")
    gone = make_scout()
    check(gone.cells.keeper.facts()["managedBytes"] == 0 and gone.cells.keeper.facts()["disk"]["freeBytes"] > 0,
          "boundary: a cache folder that is not made yet holds nothing; the disk is its nearest folder's")


# ── the routes the board calls ────────────────────────────────────────────────

def test_routes():
    CHECKS.section("the routes:")
    s, keeper, cache = shelf()
    ran(keeper, 22001, A)
    ran(keeper, 22002, C, C_MM)
    with Served(s) as srv:
        status, body = srv.get("/api/llama-node/cache")
        check(status == 200 and body["ok"] is True and body["keep"] == 1 and body["foreign"]["bytes"] == 9 and body["managedBytes"] > 0,
              "GET /api/llama-node/cache: the setting and the figures, ok")
        check(srv.post("/api/llama-node/cache/keep", {"keep": 2}) == (200, {"ok": True, "keep": 2, "removed": 4, "freedBytes": 20 + 7 + 8 + 42, "waiting": False})
              and A in left(cache) and B not in left(cache),
              "POST .../cache/keep 2: the number kept and the cache trimmed to it — the two models that ran stay, what never ran (B, the split model, the folder) goes")
        status, body = srv.post("/api/llama-node/cache/keep", {"keep": 1})
        check(status == 200 and body == {"ok": True, "keep": 1, "removed": 1, "freedBytes": 10, "waiting": False} and A not in left(cache),
              "lowering it trims at once: the answer says how much went")
        for bad in ({"keep": 0}, {"keep": 6}, {"keep": "2"}, {"keep": True}, {}):
            check(srv.post("/api/llama-node/cache/keep", bad) == (400, {"error": "keep must be a whole number from 1 to 5"}),
                  f"negative: {bad!r} — 400 with the reason, and the setting stays {keeper.keep.value()}")
        check(srv.post("/api/llama-node/cache/delete-foreign", {}) == (400, {"error": "expectBytes is required: the size the operator was shown"}),
              "POST .../delete-foreign without the figure: 400")
        status, body = srv.post("/api/llama-node/cache/delete-foreign", {"expectBytes": 1})
        check(status == 409 and "nothing was deleted" in body["error"] and (cache / FOREIGN).is_file(), "a figure that is not the size now: 409, nothing deleted")
        check(srv.post("/api/llama-node/cache/delete-foreign", {"expectBytes": 9}) == (200, {"ok": True, "removed": 1, "freedBytes": 9})
              and not (cache / FOREIGN).exists(), "the figure he was shown: 200 and the files gone")
    s, keeper, cache = shelf()
    hdd = cache.parent / "hdd"
    with Served(s) as srv:
        check(srv.post("/api/llama-node/cache/budget", {"budgetBytes": 2 << 30}) == (200, {"ok": True, "budgetBytes": 2 << 30, "removed": 0,
                                                                                         "freedBytes": 0, "waiting": True}),
              "POST .../cache/budget (2.38): the size kept, the trim waiting while no model is known to have run")
        status, body = srv.post("/api/llama-node/cache/budget", {"budgetBytes": "2"})
        check(status == 400 and "budgetBytes must be a whole number of bytes" in body["error"], "negative: a size that is no whole number — 400")
        status, body = srv.post("/api/llama-node/cache/dir", {"path": str(hdd)})
        check(status == 200 and body["ok"] is True and body["dir"] == str(hdd.resolve()) and srv.get("/api/llama-node/cache")[1]["dir"] == str(hdd.resolve()),
              "POST .../cache/dir (2.38): the cache moved, and GET .../cache says the new folder")
        check(srv.post("/api/llama-node/cache/dir", {})[0] == 400, "negative: no path — 400")
    closed = make_scout({"controllerToken": "tok"})
    with Served(closed) as srv:
        check(srv.get("/api/llama-node/cache")[0] == 401 and srv.post("/api/llama-node/cache/keep", {"keep": 2})[0] == 401
              and srv.post("/api/llama-node/cache/delete-foreign", {"expectBytes": 0})[0] == 401
              and srv.post("/api/llama-node/cache/budget", {"budgetBytes": 2 << 30})[0] == 401
              and srv.post("/api/llama-node/cache/dir", {"path": "/tmp/x"})[0] == 401,
              "negative: with a fleet token the five routes are closed like the rest")


# ── the budget in bytes (2.38) ────────────────────────────────────────────────

def test_budget():
    CHECKS.section("the budget (2.38, the owner: in GB, not in models):")
    s = make_scout()
    budget = s.cells.keeper.budget
    check(budget.value() is None, "until set there is no budget: the cache keeps the last `keep` models, as before")
    five = 5 << 30
    check(budget.set(five) == five and budget.value() == five
          and json.loads(s.state.path.read_text(encoding="utf-8")).get("cacheBudgetBytes") == five,
          "set(5 GiB): kept, in state.json beside the count")
    for bad in (0, (1 << 30) - 1, (1 << 50) + 1, "5", 2.5, True, [], {}):
        err = raises(lambda: budget.set(bad))
        check(err is not None and err[0] == 400 and "budgetBytes must be a whole number of bytes" in err[1] and budget.value() == five,
              f"negative: {bad!r} is refused with 400 and the budget stays 5 GiB — not rounded, not clamped")
    check(budget.set(1 << 30) == 1 << 30 and budget.set(None) is None and budget.value() is None
          and "cacheBudgetBytes" not in json.loads(s.state.path.read_text(encoding="utf-8")),
          "boundary: 1 GiB is the least; None takes the budget off, and state.json no longer holds it")
    with patched(cache_keeper.CacheBudget, MIN=1):
        err = raises(lambda: budget.set(True))
    check(err is not None and err[0] == 400, "negative: a flag is no size, even where 1 byte would be one (True is 1 to Python)")
    for stored in ("5", True, 12, -1, 2.0):
        check(make_scout(state={"cacheBudgetBytes": stored}).cells.keeper.budget.value() is None,
              f"negative: {stored!r} in state.json is no budget — a hand-edited file does not trim the cache")

    def budgeted(size, live=()):
        s, keeper, cache = shelf()
        ran(keeper, 22001, A)                 # 10 bytes, the oldest
        ran(keeper, 22002, B)                 # 20
        ran(keeper, 22003, C, C_MM)           # 35, the newest
        keeper.live = lambda: set(live)
        with patched(cache_keeper.CacheBudget, MIN=1):
            result = keeper.set_budget(size)
        return keeper, cache, result
    keeper, cache, result = budgeted(60)
    check(sorted(set(left(cache)) - {FOREIGN}) == sorted([B, C, C_MM]) and result == {"budgetBytes": 60, "removed": 4, "freedBytes": 10 + 7 + 8 + 42,
                                                                                       "waiting": False},
          "60 bytes: the newest that fit stay together — C with its projector (35) and B (20); A (10) would make 65, and goes")
    keeper, cache, _result = budgeted(55)
    check(sorted(set(left(cache)) - {FOREIGN}) == sorted([B, C, C_MM]),
          "boundary: 55 bytes holds C and B exactly — a model that fits to the byte stays")
    keeper, cache, _result = budgeted(50)
    check(sorted(set(left(cache)) - {FOREIGN}) == sorted([A, C, C_MM]),
          "50 bytes: B does not fit beside C, and makes room for the older A, which does")
    keeper, cache, _result = budgeted(30)
    check(sorted(set(left(cache)) - {FOREIGN}) == sorted([A, B]),
          "30 bytes: the newest model alone is bigger than the budget and goes; the older ones that fit stay")
    keeper, cache, _result = budgeted(40, live=(22002,))
    check(sorted(set(left(cache)) - {FOREIGN}) == sorted([A, B]),
          "a cell that runs keeps its files first and they count: B (20) runs, C (35) no longer fits beside it, A (10) does")
    keeper, cache, _result = budgeted(5, live=(22003,))
    check(sorted(set(left(cache)) - {FOREIGN}) == sorted([C, C_MM]),
          "negative: a running cell's files stay even when they alone are past the budget — a trim never pulls a model from under a cell")
    check((cache / FOREIGN).is_file(), "negative: the file this scout did not download stays, budget or not")
    s, keeper, cache = shelf(setting=1)
    ran(keeper, 22001, A)
    ran(keeper, 22002, B)
    with patched(cache_keeper.CacheBudget, MIN=1):
        keeper.set_budget(100)
        keeper.set_budget(None)
    check(sorted(set(left(cache)) - {FOREIGN}) == [B], "the budget taken off: the count rules again (keep 1 — the newest stays)")


# ── where the cache is (2.38) ─────────────────────────────────────────────────

def test_move():
    CHECKS.section("the cache's folder (2.38, the owner: an HDD instead of the SSD's writes):")
    s, keeper, cache = shelf()
    lay(cache, {"w/ggml-base.bin": 11})
    s.models.downloaded.add(cache / "w/ggml-base.bin")
    hdd = cache.parent / "hdd" / "models"
    for path, words in (("", "path is required"), ("relative/dir", "must be an absolute path"),
                        (str(cache), "is in"), (str(cache / "inside"), "may not lie inside the old one or hold it"),
                        (str(cache.parent), "may not lie inside the old one or hold it")):
        err = raises(lambda: keeper.move_to(path))
        check(err is not None and err[0] == 400 and words in err[1] and keeper.models.cache_dir() == cache,
              f"negative: {path or 'nothing'!r} is refused with 400 ({words}) and the cache stays where it is")
    blocker = cache.parent / "a-file"
    blocker.write_bytes(b"x")
    err = raises(lambda: keeper.move_to(str(blocker / "sub")))
    check(err is not None and err[0] == 400 and "the cache cannot be in" in err[1], "negative: a folder that cannot be made — 400 in the system's words")
    keeper.loading = lambda: {22005, 22003}
    err = raises(lambda: keeper.move_to(str(hdd)))
    check(err == (409, "the cache does not move while a cell is bringing its model: :22003, :22005 — try again when it runs")
          and keeper.models.cache_dir() == cache and (cache / A).is_file(),
          "the owner's rule: not while a start brings its files — 409 names the cells, nothing deleted, the folder stays")
    keeper.loading = lambda: set()
    keeper.held = lambda: [str(cache / C)]
    result = keeper.move_to(str(hdd))
    check(result == {"dir": str(hdd.resolve()), "removed": 8, "freedBytes": 10 + 20 + 30 + 5 + 7 + 8 + 42 + 11},
          "the move: what this scout downloaded into the old folder is deleted — every kind (a whisper .bin too) and the folder model whole")
    check(left(cache) == [FOREIGN] and not (cache / ".caravan-downloads.json").exists(),
          "negative: the file this scout did not download stays in the old folder; the ledger of the old folder goes with its files")
    check(not (cache / C).exists(), "a running cell's model goes too: its weights are loaded, and its open file outlives its name")
    check(keeper.models.cache_dir() == hdd.resolve() and hdd.is_dir() and list(hdd.iterdir()) == []
          and json.loads(s.state.path.read_text(encoding="utf-8")).get("cacheDir") == str(hdd.resolve()),
          "the cache is in the new folder now — made, empty (no probe left), and kept in state.json")
    keeper.forget_figures()
    facts = keeper.facts()
    check(facts["dir"] == str(hdd.resolve()) and facts["managedBytes"] == 0 and facts["foreign"]["files"] == 0,
          "the figures are the new folder's: nothing held yet, nothing foreign — the next start brings its model here")
    fresh = make_scout(state={"cacheDir": str(hdd.resolve())})
    check(fresh.models.cache_dir() == hdd.resolve(), "a scout that starts again reads the folder from state.json, before config.json's")
    plain = make_scout(state={"cacheDir": "relative/path"})
    check(plain.models.cache_dir() == Path(plain.config["modelsBasePath"]).expanduser(),
          "negative: a folder in state.json that is no absolute path is no setting — config.json's modelsBasePath rules")


TESTS = (test_setting, test_uses, test_trim, test_foreign, test_facts, test_budget, test_move, test_routes)

for test in TESTS:
    try:
        test()
    except (Exception, RealCallBlocked) as exc:  # noqa: BLE001 — a crash is a red pin; the rest still runs
        check(False, f"{test.__name__} crashed: {exc!r}")

sys.exit(CHECKS.finish())
