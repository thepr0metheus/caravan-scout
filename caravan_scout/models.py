"""ModelsMixin: model cache — download from the controller, verify, purge."""
from __future__ import annotations

import json
import os
import re
import signal
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from caravan_scout.errors import AppError


class ModelsMixin:
    def _model_cache_dir(self) -> Path:
        base = str(self.config.get("modelsBasePath") or "").strip()
        if base:
            return Path(base).expanduser()
        return Path.home() / ".llama-model-cache"

    # Error patterns that indicate a cached file is truncated / corrupted.
    _CORRUPT_PATTERNS = (
        "not within the file bounds",
        "corrupted or incomplete",
        "unexpected end of file",
    )

    @classmethod
    def _is_corruption_error(cls, text: str) -> bool:
        low = (text or "").lower()
        return any(p in low for p in cls._CORRUPT_PATTERNS)

    def _ensure_model(self, model_path_raw: str, report: bool = True,
                      report_label: str = "", use_cache: bool = False,
                      port: int = 0) -> Path:
        """Return a local Path to the model file.

        If model_path_raw is absolute and exists — use it directly.
        If use_cache and a local copy exists — reuse it. Otherwise (the default)
        re-download from the admin into the working dir; with caching off the
        files are also purged on stop, so no GGUF persists on client disks.

        report=True streams download progress into the llama startup state.
        report_label is the short filename shown in the UI during download
        (defaults to the basename of model_path_raw).
        """
        mp = Path(model_path_raw).expanduser()
        if mp.is_absolute() and mp.exists():
            return mp

        cache_dir = self._model_cache_dir()
        local = cache_dir / model_path_raw
        if use_cache and local.exists():
            return local

        # Download from admin
        controller = str(self.config.get("controllerUrl") or "").rstrip("/")
        if not controller:
            raise AppError(f"model not found locally and controllerUrl not set: {model_path_raw}", 404)

        import urllib.parse
        label = report_label or Path(model_path_raw).name
        url = f"{controller}/api/models/download?path={urllib.parse.quote(model_path_raw)}"
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(".tmp")

        # Transient errors (controller restarting during deploy) — retry with backoff.
        _RETRY_DELAYS = (5, 15, 30)  # seconds between attempts; 4 attempts total
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt, _ in enumerate((*_RETRY_DELAYS, None)):
            try:
                req = urllib.request.Request(url, headers=self.controller_headers())
                with urllib.request.urlopen(req, timeout=3600) as resp, open(tmp, "wb") as fh:
                    total = int(resp.headers.get("Content-Length") or 0)
                    if report:
                        self._set_llama_startup(port, phase="downloading", downloadedBytes=0,
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
                            self._set_llama_startup(port, downloadedBytes=done)
                    if report:
                        self._set_llama_startup(port, downloadedBytes=done)
                # Guard against silent truncation: server closes TCP without error
                # but before sending all bytes (network blip, restart mid-stream).
                if total and done != total:
                    raise IOError(
                        f"incomplete download: received {done:,} of {total:,} bytes "
                        f"({done / total * 100:.1f}%) — connection closed prematurely"
                    )
                print(f"[llama-node] download complete: {label} — {done:,} bytes")
                tmp.replace(local)
                return local  # success
            except Exception as exc:
                tmp.unlink(missing_ok=True)
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
                    self._set_llama_startup(
                        downloadingFile=f"{label} (retry {attempt + 1} in {delay}s…)")
                time.sleep(delay)
        raise AppError(f"model download failed: {last_exc}", 500)

    @staticmethod
    def _truthy(value: Any) -> bool:
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _cleanup_old_models(self, keep_paths: Any) -> None:
        """Delete .gguf files from modelsBasePath except the kept ones (model +
        mmproj + spec draft).

        Called after a successful llama-server start when cleanOldModels=true.
        Only touches files inside our own model cache dir — never touches files
        the user placed elsewhere.
        """
        cache_dir = self._model_cache_dir()
        if not cache_dir.is_dir():
            return
        if isinstance(keep_paths, (str, Path)):
            keep_paths = [keep_paths]
        keep_resolved = {Path(p).resolve() for p in keep_paths if p}
        deleted = []
        for p in cache_dir.rglob("*.gguf"):
            if p.resolve() in keep_resolved:
                continue
            try:
                p.unlink()
                deleted.append(p.name)
                # Remove empty parent dirs up to cache_dir
                parent = p.parent
                while parent != cache_dir and parent.is_dir():
                    try:
                        parent.rmdir()  # only succeeds if empty
                        parent = parent.parent
                    except OSError:
                        break
            except Exception:
                pass
        if deleted:
            print(f"[llama-node] cleanOldModels: removed {len(deleted)} file(s): {deleted}")

    def _purge_model_cache(self, keep: Any = None) -> dict[str, Any]:
        """Delete downloaded .gguf/.tmp from the model cache dir (except `keep`).

        Called on stop when caching is off, and on demand via the purge-cache
        endpoint. Returns {removed, freedBytes}."""
        cache_dir = self._model_cache_dir()
        if not cache_dir.is_dir():
            return {"removed": 0, "freedBytes": 0}
        if isinstance(keep, (str, Path)):
            keep = [keep]
        keep_resolved = {Path(p).resolve() for p in (keep or []) if p}
        deleted, freed = [], 0
        for pattern in ("*.gguf", "*.tmp"):
            for p in cache_dir.rglob(pattern):
                if p.resolve() in keep_resolved:
                    continue
                try:
                    sz = p.stat().st_size
                    p.unlink()
                    deleted.append(p.name)
                    freed += sz
                    parent = p.parent
                    while parent != cache_dir and parent.is_dir():
                        try:
                            parent.rmdir()
                            parent = parent.parent
                        except OSError:
                            break
                except Exception:
                    pass
        if deleted:
            print(f"[llama-node] purge cache: removed {len(deleted)} file(s), freed {freed} bytes")
        return {"removed": len(deleted), "freedBytes": freed}

    def purge_model_cache_safe(self) -> dict[str, Any]:
        """On-demand cache purge. Keeps the currently running model's files so a
        live server isn't broken."""
        keep = []
        for _p, slot in self._slots_snapshot():
            if slot.node.status().get("running"):
                cfg = slot.node._cfg if hasattr(slot.node, "_cfg") else {}
                for k in ("modelPath", "mmprojPath", "specPath"):
                    if cfg.get(k):
                        keep.append(cfg[k])
        return self._purge_model_cache(keep=keep)

    def list_cached_models(self) -> list[dict[str, Any]]:
        """Return .gguf files currently stored in the model cache dir."""
        cache_dir = self._model_cache_dir()
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

    def _download_all_model_files(self, model_path_raw: str, mmproj_raw: str,
                                   spec_raw: str, use_cache: bool, port: int = 0) -> tuple:
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
            local = self._ensure_model(raw, report=True, report_label=label,
                                       use_cache=use_cache, port=port)
            results.append(str(local))
        mp = results[0]
        mmproj_abs = results[1] if mmproj_raw else ""
        spec_abs = results[2] if spec_raw else (results[1] if spec_raw and not mmproj_raw else "")
        # Correct the spec index when mmproj is absent
        if spec_raw and not mmproj_raw:
            spec_abs = results[1]
        elif spec_raw and mmproj_raw:
            spec_abs = results[2]
        return mp, mmproj_abs, spec_abs

