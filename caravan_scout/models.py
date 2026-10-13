"""ModelFetcher: the model cache on this machine — files downloaded from the
controller, verified, and purged."""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from caravan_scout.errors import AppError


class DownloadedFiles:
    """The files this scout downloaded into its model cache — the only files
    it ever deletes there.

    The cache dir is a setting, and a setting can name a folder the scout
    shares: the model library on the controller's own machine, a NAS mount.
    Deleting by pattern (every *.gguf but the active one) there deletes the
    library. So a download is written down as it starts (its .tmp) and when
    it lands, and the purge, the old-model cleanup and the corruption retry
    delete only what is written down. Files that were in the cache before
    this record existed are not in it and stay — remove them by hand.
    """

    NAME = ".caravan-downloads.json"

    def __init__(self, cache_dir: Callable[[], Path]):
        self.cache_dir = cache_dir      # a callable: the setting can change
        self._lock = threading.Lock()

    def _file(self) -> Path:
        return self.cache_dir() / self.NAME

    def _read(self) -> set[str]:
        try:
            data = json.loads(self._file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {str(x) for x in data} if isinstance(data, list) else set()

    def _write(self, names: set[str]) -> None:
        target = self._file()
        if not names:               # nothing downloaded: no bookkeeping file either
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".new")
        tmp.write_text(json.dumps(sorted(names), indent=1), encoding="utf-8")
        tmp.replace(target)

    def _key(self, path: Any) -> str | None:
        """The path inside the cache, or None — outside it nothing is ours."""
        try:
            return str(Path(path).resolve().relative_to(self.cache_dir().resolve()))
        except (ValueError, OSError, RuntimeError):
            return None

    def add(self, path: Any) -> None:
        key = self._key(path)
        if key is None:
            return
        with self._lock:
            names = self._read()
            if key not in names:
                self._write(names | {key})

    def forget(self, path: Any) -> None:
        key = self._key(path)
        if key is None:
            return
        with self._lock:
            names = self._read()
            if key in names:
                self._write(names - {key})

    def has(self, path: Any) -> bool:
        key = self._key(path)
        return key is not None and key in self._read()

    def paths(self) -> list[Path]:
        base = self.cache_dir()
        return [base / name for name in sorted(self._read())]


class SplitGguf:
    """A GGUF in parts — `<stem>-00001-of-00002.gguf` and the rest beside it.

    An engine loads the first part and opens the others next to it by name, so
    a cell names only the first. The scout fetched only what a cell named: the
    second part never arrived, and the engine stopped at it — a 68 GB Strata
    model comes in two (lama-caravan 1.3.512). One rule for what the parts are,
    read by the fetch that brings them and by the purge that keeps them while a
    cell runs on them."""

    PART = re.compile(r"^(?P<stem>.+)-(?P<index>\d{5})(?P<of>-of-)(?P<count>\d{5})(?P<ext>\.gguf)$", re.IGNORECASE)

    def __init__(self, path: Any):
        self.path = str(path)

    def parts(self) -> list[str]:
        """Every part, the first first, when this path names the first of
        several; this path alone otherwise — a later part named by itself is
        a file like any other."""
        head, sep, name = self.path.rpartition("/")
        match = self.PART.match(name)
        if not match or int(match["index"]) != 1 or int(match["count"]) < 2:
            return [self.path]
        count = int(match["count"])
        # The name's own spelling throughout: the first part is the path given.
        return [f"{head}{sep}{match['stem']}-{i:05d}{match['of']}{match['count']}{match['ext']}"
                for i in range(1, count + 1)]


class ModelFetcher:
    """The model cache: where downloaded files live, how they arrive from the
    controller, and how they go.

    It knows the config (the cache dir, the controller and its token) and
    reports a download's progress through `report(port, **fields)` — the
    startup state of the cell being started — and nothing else about cells.
    Which files a RUNNING cell holds is the scout's to say (purge_safely).
    """

    def __init__(self, config, report: Callable[..., None]):
        self.config = config
        self.report = report
        self.downloaded = DownloadedFiles(self.cache_dir)
        # One fetch per folder at a time: two cells of one model starting
        # together would write the same "<folder>.part".
        self._folders: dict[str, threading.Lock] = {}
        self._folders_lock = threading.Lock()
        # What is being written right now — a file's .tmp, a folder's .part.
        # A purge leaves these alone: with no cache, every stop purges, and a
        # stop on one port used to delete the .tmp a start on another was
        # still filling, failing a download an hour in.
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()

    def _folder_lock(self, raw: str) -> threading.Lock:
        with self._folders_lock:
            return self._folders.setdefault(raw, threading.Lock())

    def _flying(self, path: Path, on: bool) -> None:
        key = str(path.resolve())
        with self._in_flight_lock:
            if on:
                self._in_flight.add(key)
            else:
                self._in_flight.discard(key)

    def in_flight(self, path: Path) -> bool:
        with self._in_flight_lock:
            return str(path.resolve()) in self._in_flight

    #: Where each cell keeps what it makes from its model — the folder of
    #: CARAVAN_CELL_SCRATCH. It lives as the cell's models live: kept with
    #: them, emptied at the start and removed at the stop without them.
    SCRATCH = ".cells"

    def scratch(self, port: int) -> Path:
        return self.cache_dir() / self.SCRATCH / str(int(port))

    def clear_scratch(self, port: int) -> bool:
        """Remove the cell's scratch folder; True when there was one."""
        path = self.scratch(port)
        if not path.is_dir():
            return False
        shutil.rmtree(path, ignore_errors=True)
        return True

    def cache_dir(self) -> Path:
        base = str(self.config.get("modelsBasePath") or "").strip()
        if base:
            return Path(base).expanduser()
        return Path.home() / ".llama-model-cache"

    # Error patterns that indicate a cached file is truncated / corrupted.
    CORRUPT_PATTERNS = (
        "not within the file bounds",
        "corrupted or incomplete",
        "unexpected end of file",
    )

    @classmethod
    def is_corruption_error(cls, text: str) -> bool:
        low = (text or "").lower()
        return any(p in low for p in cls.CORRUPT_PATTERNS)

    # How long a look at a hinted path may take: a library is an NFS mount, and
    # a dead NFS server makes stat() wait instead of failing.
    PROBE_SECONDS = 3.0

    @staticmethod
    def _stat(path: Path, want_dir: bool) -> Any:
        """True/False for a folder; the size (0 when absent) for a file."""
        if want_dir:
            return path.is_dir()
        return path.stat().st_size if path.is_file() else 0

    def look(self, path: Path, want_dir: bool) -> Any:
        """_stat with a deadline: None when the path did not answer in time."""
        box: dict[str, Any] = {}

        def probe() -> None:
            try:
                box["hit"] = self._stat(path, want_dir)
            except OSError:
                box["hit"] = None

        worker = threading.Thread(target=probe, daemon=True)
        worker.start()
        worker.join(self.PROBE_SECONDS)
        if worker.is_alive():
            print(f"[llama-node] {path} did not answer in {self.PROBE_SECONDS:g} s — not reading it in place")
            return None
        return box.get("hit")

    def in_place(self, raw: str, hint: Any) -> Path | None:
        """The file where the controller reads it, when this machine has the
        same one at that path — the scout on the controller's own machine, a
        library mounted at the same path. Read there: no copy, not written
        down as downloaded, never deleted. None when the hint names nothing
        here, or another file (its size differs)."""
        if not isinstance(hint, dict) or not str(hint.get("path") or "").strip():
            return None
        path = Path(str(hint["path"])).expanduser()
        if not path.is_absolute():
            return None
        want_dir = bool(hint.get("dir"))
        hit = self.look(path, want_dir)
        if not hit:
            return None
        size = int(hint.get("size") or 0)
        if not want_dir and size and hit != size:
            print(f"[llama-node] {raw}: {path} is here, but it is not the controller's file "
                  f"({hit:,} bytes, not {size:,}) — not reading it in place")
            return None
        return path

    @staticmethod
    def refuse_unreachable(raw: str, hint: Any) -> None:
        """A download from the controller cannot bring a file of a library it
        reads besides its models folder: say what this machine lacks instead of
        failing on a 404. A folder it brings (ensure_folder)."""
        if not isinstance(hint, dict):
            return
        where = str(hint.get("path") or raw)
        if hint.get("library"):
            raise AppError(f"the model is in the library {hint['library']} ({where}), which this machine "
                           f"does not have there — mount the library at the same path, or bring the model "
                           f"back to the controller", 409)

    def ensure(self, model_path_raw: str, report: bool = True,
                      report_label: str = "", use_cache: bool = False,
                      port: int = 0, hint: Any = None) -> Path:
        """Return a local Path to the model file.

        When `hint` — where the controller reads it — names the same file on
        this machine, read it there (in_place).
        If model_path_raw is absolute and exists — use it directly.
        If use_cache and a local copy exists — reuse it. Otherwise (the default)
        re-download from the admin into the working dir; with caching off the
        files are also purged on stop, so no GGUF persists on client disks.

        report=True streams download progress into the llama startup state.
        report_label is the short filename shown in the UI during download
        (defaults to the basename of model_path_raw).

        The first part of a split GGUF brings the other parts (SplitGguf):
        into the cache beside it, or — when the first is read where it lies —
        found beside it there, refused (409) when one is missing.
        """
        first = self._ensure_file(model_path_raw, report, report_label, use_cache, port, hint)
        rest = SplitGguf(model_path_raw).parts()[1:]
        if not rest or first.is_dir():
            return first
        if not self.in_cache(first):
            missing = [p for p in rest if not (first.parent / Path(p).name).is_file()]
            if missing:
                raise AppError(f"{first.name} is read at {first.parent}, but its part "
                               f"{Path(missing[0]).name} is not there", 409)
            return first
        count = len(rest) + 1
        for index, part in enumerate(rest, start=2):
            self._ensure_file(part, report, f"{Path(part).name} ({index}/{count})", use_cache, port, None)
        return first

    def in_cache(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.cache_dir().resolve())
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    def _ensure_file(self, model_path_raw: str, report: bool, report_label: str, use_cache: bool,
                     port: int, hint: Any) -> Path:
        here = self.in_place(model_path_raw, hint)
        if here is not None:
            print(f"[llama-node] {Path(model_path_raw).name}: read in place — {here}")
            return here
        mp = Path(model_path_raw).expanduser()
        if mp.is_absolute() and mp.exists():
            return mp

        if isinstance(hint, dict) and hint.get("dir"):
            return self.ensure_folder(model_path_raw, report=report, use_cache=use_cache,
                                      port=port, hint=hint)
        cache_dir = self.cache_dir()
        local = cache_dir / model_path_raw
        if use_cache and local.exists():
            return local
        self.refuse_unreachable(model_path_raw, hint)
        self.controller(model_path_raw)
        label = report_label or Path(model_path_raw).name
        local.parent.mkdir(parents=True, exist_ok=True)
        return self._download(model_path_raw, local, local.with_suffix(".tmp"), label, report, port)

    #: Written last into a folder this scout rebuilt: its listing, so a folder
    #: without it is one whose fetch did not finish.
    FOLDER_MARK = ".caravan-folder.json"

    def ensure_folder(self, raw: str, report: bool = True, use_cache: bool = False,
                      port: int = 0, hint: Any = None) -> Path:
        """A model that is a folder — a checkpoint, a whisper cache, a robot
        policy — rebuilt in the cache through the controller: its listing
        (GET /api/models/folder), then each file through the download a file
        takes, then each link inside it. The scout reads the caravan's models
        only through the controller (the owner, 2026-10-12: no scout gets rights
        to the share they live on); a folder used to be refused here outright.

        The files land in "<folder>.part" and the folder takes its name only
        when every file and link is in place, with FOLDER_MARK written last: a
        fetch cut short is resumed (a file already of its size is kept) and
        never read as a model. A folder this scout did not download is never
        replaced."""
        with self._folder_lock(raw):
            return self._ensure_folder(raw, report, use_cache, port, hint)

    def _ensure_folder(self, raw: str, report: bool, use_cache: bool, port: int, hint: Any) -> Path:
        local = self.cache_dir() / raw
        if use_cache and (local / self.FOLDER_MARK).is_file():
            return local
        self.refuse_unreachable(raw, hint)
        listing = self.folder_listing(raw)
        part = local.with_name(local.name + ".part")
        part.mkdir(parents=True, exist_ok=True)
        self.downloaded.add(part)
        self._flying(part, True)
        try:
            return self._fill_folder(raw, local, part, listing, report, port)
        finally:
            self._flying(part, False)

    def _fill_folder(self, raw: str, local: Path, part: Path, listing: dict[str, Any],
                     report: bool, port: int) -> Path:
        files, links = listing["files"], listing["links"]
        for idx, item in enumerate(files):
            dest = self.inside(part, item["path"])
            if dest.is_file() and dest.stat().st_size == item["size"]:
                continue        # fetched before the fetch was cut short
            dest.parent.mkdir(parents=True, exist_ok=True)
            label = f"{Path(raw).name}/{item['path']} ({idx + 1}/{len(files)})"
            self._download(f"{raw}/{item['path']}", dest, dest.with_name(dest.name + ".tmp"),
                           label, report, port, record=False)
        for item in links:
            at = self.inside(part, item["path"])
            target = str(item["target"])
            if Path(target).is_absolute():
                raise AppError(f"the controller listed a link out of the folder {raw}: {item['path']} -> {target}", 502)
            self.inside(part, (Path(item["path"]).parent / target).as_posix())
            at.parent.mkdir(parents=True, exist_ok=True)
            if at.is_symlink() or at.exists():
                at.unlink()
            at.symlink_to(target)
        (part / self.FOLDER_MARK).write_text(json.dumps(listing, indent=1), encoding="utf-8")
        if local.exists() or local.is_symlink():
            if not self.downloaded.has(local):
                raise AppError(f"{local} is there and was not downloaded by this scout: not replaced", 409)
            shutil.rmtree(local)
        part.rename(local)
        self.downloaded.add(local)
        self.downloaded.forget(part)
        print(f"[llama-node] folder complete: {raw} — {len(files)} files, {len(links)} links, "
              f"{listing['size']:,} bytes")
        return local

    def folder_listing(self, raw: str) -> dict[str, Any]:
        """The controller's listing of a model folder: {files, links, size}."""
        controller = self.controller(raw)
        url = f"{controller}/api/models/folder?path={urllib.parse.quote(raw)}"
        try:
            req = urllib.request.Request(url, headers=self.config.headers())
            with urllib.request.urlopen(req, timeout=60) as resp:
                listing = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AppError(f"the controller does not list the model folder {raw}: HTTP {exc.code}", 502)
        except (OSError, ValueError) as exc:
            raise AppError(f"the controller does not list the model folder {raw}: {exc}", 502)
        if not isinstance(listing, dict) or not isinstance(listing.get("files"), list) \
                or not isinstance(listing.get("links"), list):
            raise AppError(f"the controller's listing of {raw} is not a folder listing", 502)
        return listing

    @staticmethod
    def inside(root: Path, rel: str) -> Path:
        """root/rel, refused when rel leaves root: the listing comes over the
        network, and a path like ../../.ssh/authorized_keys is written nowhere."""
        text = str(rel or "")
        if not text or Path(text).is_absolute():
            raise AppError(f"the controller listed a path that is not inside the folder: {text!r}", 502)
        path = (root / text).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            raise AppError(f"the controller listed a path that is not inside the folder: {text!r}", 502)
        return root / text

    def controller(self, what: str) -> str:
        """The controller's address, or the refusal that names what could not
        be fetched without it."""
        controller = str(self.config.get("controllerUrl") or "").rstrip("/")
        if not controller:
            raise AppError(f"model not found locally and controllerUrl not set: {what}", 404)
        return controller

    def _download(self, model_path_raw: str, local: Path, tmp: Path, label: str,
                  report: bool, port: int, record: bool = True) -> Path:
        """One file from the controller into `local`, through `tmp`. `record`:
        written down as this scout's download — a folder's files are not, the
        folder is (ensure_folder)."""
        controller = self.controller(model_path_raw)
        url = f"{controller}/api/models/download?path={urllib.parse.quote(model_path_raw)}"
        self._flying(tmp, True)
        try:
            return self._fetch(url, model_path_raw, local, tmp, label, report, port, record)
        finally:
            self._flying(tmp, False)

    def _fetch(self, url: str, model_path_raw: str, local: Path, tmp: Path, label: str,
               report: bool, port: int, record: bool) -> Path:
        # Transient errors (controller restarting during deploy) — retry with backoff.
        _RETRY_DELAYS = (5, 15, 30)  # seconds between attempts; 4 attempts total
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt, _ in enumerate((*_RETRY_DELAYS, None)):
            try:
                req = urllib.request.Request(url, headers=self.config.headers())
                if record:
                    self.downloaded.add(tmp)
                with urllib.request.urlopen(req, timeout=3600) as resp, open(tmp, "wb") as fh:
                    total = int(resp.headers.get("Content-Length") or 0)
                    if report:
                        self.report(port, phase="downloading", downloadedBytes=0,
                                                totalBytes=total, downloadingFile=label)
                    done = 0
                    last_report = 0
                    while True:
                        chunk = resp.read(1 << 20)  # 1 MiB
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        # Throttle progress updates to ~every 32 MiB to limit lock churn.
                        if report and done - last_report >= (32 << 20):
                            last_report = done
                            self.report(port, downloadedBytes=done)
                    if report:
                        self.report(port, downloadedBytes=done)
                # Guard against silent truncation: server closes TCP without error
                # but before sending all bytes (network blip, restart mid-stream).
                if total and done != total:
                    raise IOError(
                        f"incomplete download: received {done:,} of {total:,} bytes "
                        f"({done / total * 100:.1f}%) — connection closed prematurely"
                    )
                print(f"[llama-node] download complete: {label} — {done:,} bytes")
                tmp.replace(local)
                if record:
                    self.downloaded.add(local)
                    self.downloaded.forget(tmp)
                return local  # success
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                if record:
                    self.downloaded.forget(tmp)
                last_exc = exc
                err_str = str(exc).lower()
                print(f"[llama-node] download error (attempt {attempt + 1}/{len(_RETRY_DELAYS) + 1}): {label} — {exc}")
                # Only retry on transient connectivity errors (controller restart, etc.)
                is_transient = ("connection refused" in err_str or
                                "connection reset" in err_str or
                                "timed out" in err_str or
                                "temporarily unavailable" in err_str or
                                "incomplete download" in err_str or
                                "errno 111" in err_str or
                                "errno 104" in err_str)
                if not is_transient or attempt >= len(_RETRY_DELAYS):
                    break
                delay = _RETRY_DELAYS[attempt]
                print(f"[llama-node] download transient error (attempt {attempt + 1}): {exc} — retrying in {delay}s…")
                if report:
                    # The port was missing here: the call raised TypeError on
                    # the first blip, so a download that should have waited 5,
                    # 15 and 30 s for a restarting controller failed at once.
                    self.report(
                        port, downloadingFile=f"{label} (retry {attempt + 1} in {delay}s…)")
                time.sleep(delay)
        raise AppError(f"model download failed: {last_exc}", 500)

    def cleanup_old(self, keep_paths: Any) -> None:
        """Delete the .gguf files this scout downloaded, except the kept ones
        (model + mmproj + spec draft).

        Called after a successful llama-server start when cleanOldModels is
        on (off by default). Only files the scout downloaded (DownloadedFiles)
        — it used to be every .gguf under the cache dir.
        """
        if isinstance(keep_paths, (str, Path)):
            keep_paths = [keep_paths]
        keep_resolved = {Path(p).resolve() for p in keep_paths if p}
        deleted = [p.name for p in self._delete_downloaded((".gguf",), keep_resolved)[0]]
        if deleted:
            print(f"[llama-node] cleanOldModels: removed {len(deleted)} file(s): {deleted}")

    def _delete_downloaded(self, suffixes: tuple, keep_resolved: set,
                           folders: bool = False) -> tuple[list[Path], int]:
        """Delete the downloaded files with these suffixes that are not kept,
        and the folders they leave empty; with `folders`, the downloaded model
        folders too (a checkpoint, a whisper cache) — a folder holding a kept
        path is kept. What is being written now is never deleted (in_flight).
        Returns (deleted, freed bytes)."""
        cache_dir = self.cache_dir()
        deleted, freed = [], 0
        for p in self.downloaded.paths():
            if not p.exists():
                if folders or p.suffix in suffixes:
                    self.downloaded.forget(p)
                continue
            is_folder = p.is_dir() and not p.is_symlink()
            if not is_folder and p.suffix not in suffixes:
                continue
            if is_folder and not folders:
                continue
            resolved = p.resolve()
            if resolved in keep_resolved or self.in_flight(p):
                continue
            if is_folder and any(k == resolved or resolved in k.parents for k in keep_resolved):
                continue
            try:
                size = self._size(p)
                if is_folder:
                    shutil.rmtree(p)
                else:
                    p.unlink()
                self.downloaded.forget(p)
                deleted.append(p)
                freed += size
                parent = p.parent
                while parent != cache_dir and parent.is_dir():
                    try:
                        parent.rmdir()  # only succeeds if empty
                        parent = parent.parent
                    except OSError:
                        break
            except Exception:
                pass
        return deleted, freed

    @staticmethod
    def _size(path: Path) -> int:
        """Bytes a file or a folder takes — a link counted as a link, not as
        what it points at (a Hugging Face cache links its snapshots to blobs)."""
        if not path.is_dir():
            return path.stat().st_size
        return sum(f.lstat().st_size for f in path.rglob("*") if f.is_symlink() or f.is_file())

    def purge(self, keep: Any = None) -> dict[str, Any]:
        """Delete the .gguf/.tmp files and the model folders this scout
        downloaded (except `keep`).

        Called on stop when caching is off, and on demand via the purge-cache
        endpoint. Folders since 2.31: a folder model (2.30) was downloaded on
        every machine that ran it and never left. Returns {removed, freedBytes}."""
        if isinstance(keep, (str, Path)):
            keep = [keep]
        keep_resolved = {Path(p).resolve() for p in (keep or []) if p}
        deleted, freed = self._delete_downloaded((".gguf", ".tmp"), keep_resolved, folders=True)
        if deleted:
            print(f"[llama-node] purge cache: removed {len(deleted)} file(s), freed {freed} bytes")
        return {"removed": len(deleted), "freedBytes": freed}

    def listing(self) -> list[dict[str, Any]]:
        """Return .gguf files currently stored in the model cache dir."""
        cache_dir = self.cache_dir()
        if not cache_dir.is_dir():
            return []
        result = []
        for p in sorted(cache_dir.rglob("*.gguf")):
            try:
                result.append({
                    "path": str(p.relative_to(cache_dir)),
                    "sizeBytes": p.stat().st_size,
                })
            except Exception:
                pass
        return result

    def download_all(self, model_path_raw: str, mmproj_raw: str,
                                   spec_raw: str, use_cache: bool, port: int = 0,
                                   hints: Any = None) -> tuple:
        """Download model + aux files, reporting progress for all of them.

        Returns (mp, mmproj_abs, spec_abs) as strings/Paths.
        Raises AppError on any download failure.
        """
        # Count how many files we'll download so the label can show "1/N"
        files = [(model_path_raw, True)]  # (path, is_primary)
        if mmproj_raw:
            files.append((mmproj_raw, False))
        if spec_raw:
            files.append((spec_raw, False))
        n = len(files)
        results: list[str] = []
        for idx, (raw, _) in enumerate(files):
            short = Path(raw).name
            label = f"{short} ({idx + 1}/{n})" if n > 1 else short
            local = self.ensure(raw, report=True, report_label=label,
                                       use_cache=use_cache, port=port,
                                       hint=(hints or {}).get(raw) if isinstance(hints, dict) else None)
            results.append(str(local))
        # In the order they were downloaded: the model, then mmproj if any,
        # then the draft if any. Positions counted by hand read results[2]
        # for a draft without an mmproj — an IndexError, so such a cell never
        # started, after downloading both files.
        rest = iter(results[1:])
        mp = results[0]
        mmproj_abs = next(rest) if mmproj_raw else ""
        spec_abs = next(rest) if spec_raw else ""
        return mp, mmproj_abs, spec_abs

