"""ModelFetcher: the model cache on this machine — files downloaded from the
controller, verified, and purged."""
from __future__ import annotations

import json
import threading
import time
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
        """A download from the controller cannot bring a library's file or a
        folder: say what this machine lacks instead of failing on a 404."""
        if not isinstance(hint, dict):
            return
        where = str(hint.get("path") or raw)
        if hint.get("library"):
            raise AppError(f"the model is in the library {hint['library']} ({where}), which this machine "
                           f"does not have there — mount the library at the same path, or bring the model "
                           f"back to the controller", 409)
        if hint.get("dir"):
            raise AppError(f"the model is a folder ({where}) and this machine does not have it there — a "
                           f"folder cannot be downloaded; put it there, or mount the library that holds it", 409)

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
        """
        here = self.in_place(model_path_raw, hint)
        if here is not None:
            print(f"[llama-node] {Path(model_path_raw).name}: read in place — {here}")
            return here
        mp = Path(model_path_raw).expanduser()
        if mp.is_absolute() and mp.exists():
            return mp

        cache_dir = self.cache_dir()
        local = cache_dir / model_path_raw
        if use_cache and local.exists():
            return local
        self.refuse_unreachable(model_path_raw, hint)

        # Download from admin
        controller = str(self.config.get("controllerUrl") or "").rstrip("/")
        if not controller:
            raise AppError(f"model not found locally and controllerUrl not set: {model_path_raw}", 404)

        label = report_label or Path(model_path_raw).name
        url = f"{controller}/api/models/download?path={urllib.parse.quote(model_path_raw)}"
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(".tmp")

        # Transient errors (controller restarting during deploy) — retry with backoff.
        _RETRY_DELAYS = (5, 15, 30)  # seconds between attempts; 4 attempts total
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt, _ in enumerate((*_RETRY_DELAYS, None)):
            try:
                req = urllib.request.Request(url, headers=self.config.headers())
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
                self.downloaded.add(local)
                self.downloaded.forget(tmp)
                return local  # success
            except Exception as exc:
                tmp.unlink(missing_ok=True)
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

    def _delete_downloaded(self, suffixes: tuple, keep_resolved: set) -> tuple[list[Path], int]:
        """Delete the downloaded files with these suffixes that are not kept,
        and the folders they leave empty. Returns (deleted, freed bytes)."""
        cache_dir = self.cache_dir()
        deleted, freed = [], 0
        for p in self.downloaded.paths():
            if p.suffix not in suffixes:
                continue
            if not p.exists():
                self.downloaded.forget(p)
                continue
            if p.resolve() in keep_resolved:
                continue
            try:
                size = p.stat().st_size
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

    def purge(self, keep: Any = None) -> dict[str, Any]:
        """Delete the .gguf/.tmp files this scout downloaded (except `keep`).

        Called on stop when caching is off, and on demand via the purge-cache
        endpoint. Returns {removed, freedBytes}."""
        if isinstance(keep, (str, Path)):
            keep = [keep]
        keep_resolved = {Path(p).resolve() for p in (keep or []) if p}
        deleted, freed = self._delete_downloaded((".gguf", ".tmp"), keep_resolved)
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

