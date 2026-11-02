"""CacheKeeper: how much the machine's model cache keeps, which models, the trim that deletes the rest, and where the cache is.

The owner, 2026-10-18, when the disk of a machine was 94 % full of model copies and a start died with "No space left on device":
keep in the cache only the last models, from 1 to 5, 1 until said otherwise, each with all its files; what is older goes by itself.
The owner, 2026-10-26: say it in GB, not in models (CacheBudget), and let the cache be in a folder of his choosing — an HDD, not
the SSD's writes (move_to).

What it rests on: the scout deletes only what it downloaded (DownloadedFiles, the ledger in the cache folder: the folder is a
setting, and a setting can name a library). So the trim reaches the files in the ledger and no others; the files that were in the
cache before the ledger, or came by another road, are FOREIGN to it — counted and shown, never deleted by themselves, and
deleted only when the operator asks with the figure he was shown (delete_foreign).

What "the last models" means: the models of the last starts that got as far as a running process (ModelUses), newest first. A model
a cell uses now stays whatever its age — a running cell holds its files, and a cell that is still starting has asked for its own
(its start is in the log from its first moment, before its files are copied). Nothing is deleted while the log is empty: a scout
that has just learned this knows nothing about what was used last, and "unknown" is not "nothing".
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from caravan_scout.errors import AppError
from caravan_scout.models import ModelFetcher, SplitGguf


class CacheKeep:
    """The setting: how many of the last started models the cache keeps. It lives in state.json, the scout's own memory — the board
    changes it over HTTP, and config.json stays the file the operator wrote."""

    MIN, MAX, DEFAULT = 1, 5, 1
    KEY = "cacheKeep"

    def __init__(self, state):
        self.state = state

    @classmethod
    def valid(cls, raw: Any) -> bool:
        return isinstance(raw, int) and not isinstance(raw, bool) and cls.MIN <= raw <= cls.MAX

    def value(self) -> int:
        with self.state.lock:
            raw = self.state.get(self.KEY)
        return raw if self.valid(raw) else self.DEFAULT

    def set(self, raw: Any) -> int:
        """Keep `raw` models. A whole number from 1 to 5 — a string, a bool, 0 and 6 are refused, not rounded into one."""
        if not self.valid(raw):
            raise AppError(f"keep must be a whole number from {self.MIN} to {self.MAX}", 400)
        with self.state.lock:
            self.state[self.KEY] = raw
            self.state.save()
        return raw


class CacheBudget:
    """The setting: how much the cache's models may take in all, in bytes — the owner, 2026-10-26: "let me choose how many GB the
    models in the cache may take, not how many models". In state.json beside CacheKeep. None until the operator chooses one: the
    cache then keeps the last CacheKeep models, as before — a size nobody chose is not made up for him."""

    KEY = "cacheBudgetBytes"
    #: A gibibyte at least: a smaller budget keeps no model a cell could run. The top is a bound against a typo, not a rule.
    MIN, MAX = 1 << 30, 1 << 50

    def __init__(self, state):
        self.state = state

    @classmethod
    def valid(cls, raw: Any) -> bool:
        return isinstance(raw, int) and not isinstance(raw, bool) and cls.MIN <= raw <= cls.MAX

    def value(self) -> int | None:
        with self.state.lock:
            raw = self.state.get(self.KEY)
        return raw if self.valid(raw) else None

    def set(self, raw: Any) -> int | None:
        """Keep at most `raw` bytes of models — a whole number from 1 GiB up (400 for a string, a bool, a fraction, less); None
        takes the budget off, and the cache keeps the last CacheKeep models again."""
        if raw is not None and not self.valid(raw):
            raise AppError(f"budgetBytes must be a whole number of bytes from {self.MIN} (1 GiB) to {self.MAX}, or null", 400)
        with self.state.lock:
            if raw is None:
                self.state.pop(self.KEY, None)
            else:
                self.state[self.KEY] = raw
            self.state.save()
        return raw


class ModelUses:
    """What was started, newest first: for each model its files as the controller named them (the first is the model itself), the
    port, when, and whether the start got as far as a running process (`ok`)."""

    KEY = "modelUse"
    LIMIT = 20

    def __init__(self, state):
        self.state = state

    @staticmethod
    def sound(entry: Any) -> bool:
        return (isinstance(entry, dict) and isinstance(entry.get("files"), list) and bool(entry["files"])
                and all(isinstance(f, str) and f for f in entry["files"]))

    def _read(self) -> list[dict[str, Any]]:
        raw = self.state.get(self.KEY)
        return [e for e in raw if self.sound(e)] if isinstance(raw, list) else []

    def entries(self) -> list[dict[str, Any]]:
        with self.state.lock:
            return [dict(e) for e in self._read()]

    def begin(self, port: Any, files: list[str]) -> None:
        """A start asks for these files. The entry stands at the front from this moment, not `ok` yet — except that a model which
        has run before keeps the word it earned: a start that fails does not unmake the model that worked."""
        names = [str(f).strip() for f in files if str(f or "").strip()]
        if not names:
            return
        with self.state.lock:
            current = self._read()
            before = next((e for e in current if e["files"][0] == names[0]), None)
            entry = {"files": names, "port": int(port), "at": int(time.time()), "ok": bool(before and before.get("ok"))}
            rest = [e for e in current if e["files"][0] != names[0]]
            self.state[self.KEY] = [entry, *rest][: self.LIMIT]
            self.state.save()

    def commit(self, port: Any) -> None:
        """The start of this port got as far as a running process: its newest entry is a model that ran."""
        with self.state.lock:
            for entry in self._read():
                if entry.get("port") == int(port):
                    entry["ok"] = True
                    self.state.save()
                    return

    def newest(self, count: int) -> list[dict[str, Any]]:
        return [e for e in self.entries() if e.get("ok")][: max(0, int(count))]


class CacheKeeper:
    """The cache's keeper: records the starts, trims what is beyond the setting, and says what the cache holds.

    `held` gives the files the running cells hold, `live` the ports whose cell is starting or running: the cells are another part's
    business, and the keeper asks them at the moment it deletes.
    """

    #: Files of the cache folder that are no model and no business of this report.
    NOT_MODELS = (".log", ".tmp", ".part")
    #: How long the figures of facts() are reused: the heartbeat and the controller's polls both ask.
    MEMO_SECONDS = 20

    def __init__(self, state, models: ModelFetcher, held: Callable[[], list[str]], live: Callable[[], set[int]],
                 loading: Callable[[], set[int]] = lambda: set()):
        self.keep = CacheKeep(state)
        self.budget = CacheBudget(state)
        self.uses = ModelUses(state)
        self.models = models
        self.held = held
        self.live = live
        # The ports whose start is still bringing its files: the folder does not move under them (move_to).
        self.loading = loading
        # A trim or a deletion can take a while (a folder of a hundred files): one at a time, and the report of the cache — asked by
        # every heartbeat — never waits for it.
        self._lock = threading.Lock()
        self._memo_lock = threading.Lock()
        self._memo: tuple[float, dict[str, Any]] | None = None

    # ── what was started ────────────────────────────────────────────────────────

    def begin(self, port: Any, files: list[str]) -> None:
        self.uses.begin(port, files)

    def commit(self, port: Any) -> dict[str, Any]:
        """The start ran: record it, and delete what is beyond the setting now that the new model is in."""
        self.uses.commit(port)
        return self.trim()

    # ── what stays ──────────────────────────────────────────────────────────────

    def files_of(self, entry: dict[str, Any]) -> set[Path]:
        """Where the entry's files are on this machine: in the cache, under the name the controller gave, every part of a split GGUF."""
        cache = self.models.cache_dir()
        out: set[Path] = set()
        for raw in entry["files"]:
            for name in SplitGguf(raw).parts():
                path = Path(name).expanduser()
                out.add((path if path.is_absolute() else cache / path).resolve())
        return out

    @staticmethod
    def size_of(paths: set[Path]) -> int:
        """What these files take on the disk — a folder model by the files under it; a file that is not there takes nothing."""
        total = 0
        for path in paths:
            try:
                if path.is_dir():
                    for root, _dirs, files in os.walk(path):
                        for name in files:
                            total += (Path(root) / name).lstat().st_size
                elif path.exists():
                    total += path.lstat().st_size
            except OSError:
                continue
        return total

    def protected(self) -> set[Path]:
        """What is not deleted: the files of every cell that is starting or running and of every running process, and then the
        models that ran, newest first — with a budget (CacheBudget) as many as fit in it together with those, a model that does
        not fit making room for an older one that does; without one the last `keep` (CacheKeep)."""
        uses = self.uses.entries()
        live = self.live()
        out = {Path(p).resolve() for p in self.held() if p}
        for entry in uses:
            if entry.get("port") in live:
                out |= self.files_of(entry)
        ran = [e for e in uses if e.get("ok")]
        budget = self.budget.value()
        if budget is None:
            for entry in ran[: self.keep.value()]:
                out |= self.files_of(entry)
            return out
        total = self.size_of(out)
        for entry in ran:
            files = self.files_of(entry) - out
            size = self.size_of(files)
            if files and total + size <= budget:
                out |= files
                total += size
        return out

    def rule(self) -> str:
        """What the cache keeps, in words for the scout's log."""
        budget = self.budget.value()
        return f"keeps {budget / (1 << 30):g} GiB of models" if budget is not None else f"keeps the last {self.keep.value()} model(s)"

    def trim(self) -> dict[str, Any]:
        """Delete the cached models beyond the setting — only what this scout downloaded. `waiting`: the log knows no model that
        ran, so nothing is known about what was used last and nothing is deleted."""
        with self._lock:
            if not self.uses.newest(1):
                return {"removed": 0, "freedBytes": 0, "waiting": True}
            result = self.models.purge(keep=sorted(self.protected()))
            self.forget_figures()
            if result["removed"]:
                print(f"[llama-node] cache {self.rule()}: removed {result['removed']} file(s), freed {result['freedBytes']:,} bytes")
            return {**result, "waiting": False}

    def set_keep(self, raw: Any) -> dict[str, Any]:
        """The operator's number: kept, and the cache trimmed to it at once."""
        keep = self.keep.set(raw)
        return {"keep": keep, **self.trim()}

    def set_budget(self, raw: Any) -> dict[str, Any]:
        """The operator's size: kept, and the cache trimmed to it at once."""
        budget = self.budget.set(raw)
        return {"budgetBytes": budget, **self.trim()}

    # ── where the cache is ──────────────────────────────────────────────────────

    def move_to(self, raw: Any) -> dict[str, Any]:
        """Move the cache to the folder `raw` names — the owner, 2026-10-26: an HDD instead of the SSD's writes. What this scout
        downloaded into the folder it leaves is deleted first — every model of it is the controller's copy, and a start brings
        its model into the new folder by itself — then the cache is there. The owner's rule: not while a start is bringing its
        files (409, the cell named); a running cell has its weights loaded, and its open files outlive their names. Refused
        (400): no absolute path, the folder it is now, a folder inside the old one or holding it, one that cannot be made or
        written to. Returns {dir, removed, freedBytes}."""
        if self.models.folder is None:
            raise AppError("this scout keeps no chosen cache folder", 500)
        text = str(raw or "").strip()
        if not text:
            raise AppError("path is required: the folder the cache is to be in", 400)
        new = Path(text).expanduser()
        if not new.is_absolute():
            raise AppError(f"the cache folder must be an absolute path: {text}", 400)
        new = new.resolve()
        old = self.models.cache_dir().resolve()
        if new == old:
            raise AppError(f"the cache is in {new} already", 400)
        if new.is_relative_to(old) or old.is_relative_to(new):
            raise AppError(f"the new folder may not lie inside the old one or hold it: {new} and {old}", 400)
        try:
            new.mkdir(parents=True, exist_ok=True)
            probe = new / ".caravan-write-probe"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            raise AppError(f"the cache cannot be in {new}: {exc.strerror or exc}", 400)
        with self._lock:
            loading = sorted(self.loading())
            if loading:
                raise AppError("the cache does not move while a cell is bringing its model: "
                               + ", ".join(f":{port}" for port in loading) + " — try again when it runs", 409)
            result = self.models.clear()
            self.prune_empty()
            self.models.folder.set(new)
            self.forget_figures()
        print(f"[llama-node] the cache moved from {old} to {new}: removed {result['removed']} file(s) this scout downloaded "
              f"there, freed {result['freedBytes']:,} bytes")
        return {"dir": str(new), **result}

    # ── what is there that is not this scout's ──────────────────────────────────

    def foreign_files(self) -> list[tuple[Path, int]]:
        """(file, bytes) for every file of the cache folder that this scout did not download: not in its ledger, not under a folder
        of it. Hidden names (the scouts' scratch, a library's own cache) and logs are not models and are left out; a link is
        counted as a link."""
        cache = self.models.cache_dir()
        if not cache.is_dir():
            return []
        mine = self.models.downloaded.names()
        found: list[tuple[Path, int]] = []
        for root, dirs, files in os.walk(cache):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name.startswith(".") or name.endswith(self.NOT_MODELS):
                    continue
                path = Path(root) / name
                rel = path.relative_to(cache).as_posix()
                if self.models.downloaded.covers(mine, rel):
                    continue
                try:
                    found.append((path, path.lstat().st_size))
                except OSError:
                    continue
        return found

    def delete_foreign(self, expect_bytes: Any) -> dict[str, Any]:
        """Delete the foreign files — only when the figure the operator was shown is still the figure (409 otherwise: something
        came or went since he looked), and never what a cell holds or is starting on."""
        try:
            expect = int(expect_bytes)
        except (TypeError, ValueError):
            raise AppError("expectBytes is required: the size the operator was shown", 400)
        found = self.foreign_files()
        total = sum(size for _path, size in found)
        if expect != total:
            raise AppError(f"the cache changed since it was shown: {total:,} bytes of files this scout did not download, "
                           f"not {expect:,} — nothing was deleted", 409)
        with self._lock:
            protected = self.protected()
            removed = freed = 0
            for path, size in found:
                if path.resolve() in protected or self.models.in_flight(path):
                    continue
                try:
                    path.unlink()
                except OSError:
                    continue
                removed += 1
                freed += size
            self.prune_empty()
            self.forget_figures()
        if removed:
            print(f"[llama-node] cache: removed {removed} file(s) this scout did not download, freed {freed:,} bytes")
        return {"removed": removed, "freedBytes": freed}

    def prune_empty(self) -> None:
        """The folders a deletion left empty, bottom up; the cache folder itself and the hidden ones stay."""
        cache = self.models.cache_dir()
        for root, dirs, _files in os.walk(cache, topdown=False):
            path = Path(root)
            if path == cache or any(part.startswith(".") for part in path.relative_to(cache).parts):
                continue
            try:
                path.rmdir()
            except OSError:
                pass

    # ── what is said of the cache ───────────────────────────────────────────────

    def facts(self) -> dict[str, Any]:
        """The setting and the figures the board shows: what the cache keeps, how much of it is this scout's and how much is not,
        what the disk has left. The figures are reused for a few seconds; the setting is read each time."""
        now = time.monotonic()
        with self._memo_lock:
            memo = self._memo
        if memo is None or now - memo[0] > self.MEMO_SECONDS:
            memo = (now, self.measure())
            with self._memo_lock:
                self._memo = memo
        return {"keep": self.keep.value(), "budgetBytes": self.budget.value(), "dir": str(self.models.cache_dir()), **memo[1]}

    def forget_figures(self) -> None:
        """What was deleted changes the figures: the next report measures again."""
        with self._memo_lock:
            self._memo = None

    def measure(self) -> dict[str, Any]:
        cache = self.models.cache_dir()
        foreign = self.foreign_files()
        folders: dict[str, list[int]] = {}
        for path, size in foreign:
            top = path.relative_to(cache).parts[0]
            row = folders.setdefault(top, [0, 0])
            row[0] += 1
            row[1] += size
        biggest = sorted(folders.items(), key=lambda kv: -kv[1][1])[:8]
        return {
            "managedBytes": self.models.footprint(),
            "foreign": {"files": len(foreign), "bytes": sum(size for _path, size in foreign),
                        "folders": [{"folder": name, "files": row[0], "bytes": row[1]} for name, row in biggest]},
            "disk": self.disk(cache),
        }

    @staticmethod
    def disk(cache: Path) -> dict[str, int] | None:
        """The cache folder's disk: its size and what is free. The nearest folder that exists answers for one not made yet;
        None when the system will not say."""
        probe = cache
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(probe)
        except OSError:
            return None
        return {"totalBytes": int(usage.total), "freeBytes": int(usage.free)}
