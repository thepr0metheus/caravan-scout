"""The llama.cpp build on this machine: the binary the cells run, the job
that builds or restores another, and the archive of earlier builds."""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from caravan_scout.errors import AppError

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class LlamaBuilds:
    """llama.cpp on this machine: which build the binary is, and the update job.

    The job runs scripts/update-llama.sh (a synced copy of the controller's
    install-llama.sh: release-tag/commit checkout -f, stale-build-dir guard,
    probe-gated Blackwell workaround, cmake build) as a background thread and
    streams its output into a ring buffer. Running cells keep the OLD binary
    (they hold its inode) until restarted — deliberately never automatic.
    The slim status rides every heartbeat so the controller UI can show
    "building…" without extra calls. One job per scout: a second start while
    one runs is refused.
    """

    def __init__(self, config):
        self.config = config
        self._job: dict[str, Any] = {"running": False, "startedAt": 0, "tag": "", "lines": [],
                                     "done": False, "rc": None, "error": ""}
        self._lock = threading.Lock()

    def job(self) -> dict[str, Any]:
        """The job itself, live — its one record, for the job's own thread."""
        return self._job

    def binary_version(self) -> str:
        """Return the llama-server version string, e.g. 'version: 362 (3ac3c20)'."""
        bin_path = str(self.config.get("llamaServerBin") or "").strip()
        if not bin_path or not os.path.isfile(bin_path):
            return ""
        try:
            result = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=5)
            return (result.stdout or result.stderr or "").strip().splitlines()[0] if result.returncode == 0 else ""
        except Exception:
            return ""

    def binary_mtime(self) -> str:
        """Return ISO-8601 mtime of the llama-server binary (date it was built/replaced)."""
        bin_path = str(self.config.get("llamaServerBin") or "").strip()
        if not bin_path or not os.path.isfile(bin_path):
            return ""
        try:
            mtime = os.path.getmtime(bin_path)
            return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            return ""

    def status(self) -> dict:
        job = self._job
        with self._lock:
            snap = {k: v for k, v in job.items() if k != "lines"}
            snap["lines"] = list(job["lines"])[-200:]
            return snap

    def status_slim(self) -> dict:
        job = self._job
        with self._lock:
            return {"running": job["running"], "done": job["done"], "rc": job["rc"],
                    "startedAt": job["startedAt"], "tag": job["tag"],
                    "lastLine": (job["lines"][-1] if job["lines"] else "")}

    def archive(self) -> dict:
        """Archived build snapshots on THIS host (newest first) — the update
        script writes one per successful build and prunes to 5 by default."""
        root = Path(os.environ.get("LLAMA_BUILDS_DIR")
                    or Path.home() / ".local" / "share" / "lama-caravan" / "llama-builds")
        rows = []
        if root.is_dir():
            for entry in sorted(root.iterdir(), reverse=True):
                meta = entry / "meta.json"
                if not meta.is_file():
                    continue
                try:
                    row = json.loads(meta.read_text(encoding="utf-8"))
                except Exception:
                    continue
                row["id"] = entry.name
                rows.append(row)
        return {"ok": True, "builds": rows}

    def start_update(self, body: dict) -> dict:
        """POST /api/llama-node/update {tag?} — empty tag = latest release; a
        commit sha works too (checkout -f accepts either), which is how the
        controller converges a client onto its own build. With {restoreId} the
        same job restores an archived build instead of building."""
        job = self._job
        script = Path(__file__).resolve().parent.parent / "scripts" / "update-llama.sh"
        if not script.exists():
            raise AppError(f"update script not found: {script}", 500)
        tag = str((body or {}).get("tag") or "").strip()
        restore_id = str((body or {}).get("restoreId") or "").strip()
        if restore_id:
            cmd = ["bash", str(script), "--restore", restore_id]
            tag = f"restore:{restore_id}"
        else:
            cmd = ["bash", str(script), "--force", "--no-restart"]
            if tag:
                cmd += ["--llama-tag", tag]
        with self._lock:
            if job["running"]:
                raise AppError("a llama.cpp update is already running", 409)
            job.update({"running": True, "startedAt": int(time.time()), "tag": tag,
                        "lines": [], "done": False, "rc": None, "error": ""})
        env = dict(os.environ)
        env["PATH"] = "/usr/local/cuda/bin:" + env.get("PATH", "/usr/bin:/bin")
        # Clients keep a SHORT archive (default 2: current + one-step undo) —
        # client snapshots are big and a client rollback is never urgent: cells
        # keep serving their old binary through any rebuild. config.json
        # `llamaBuildsKeep` overrides.
        env.setdefault("LLAMA_BUILDS_KEEP",
                       str(int(self.config.get("llamaBuildsKeep") or 2)))

        def _run():
            rc, error = -1, ""
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, env=env)
                for line in proc.stdout:
                    clean = _ANSI_RE.sub("", line.rstrip())
                    with self._lock:
                        job["lines"].append(clean)
                        if len(job["lines"]) > 500:
                            del job["lines"][:100]
                rc = proc.wait()
            except Exception as exc:
                error = str(exc)
            finally:
                with self._lock:
                    job.update({"running": False, "done": True, "rc": rc, "error": error})

        threading.Thread(target=_run, daemon=True, name="llama-update").start()
        return self.status()
