"""The llama.cpp build on this machine: the binary the cells run, the job
that builds or restores another, and the archive of earlier builds."""
from __future__ import annotations

import datetime
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from caravan_scout.errors import AppError
from caravan_scout.job import BackgroundJob


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
        self._job = BackgroundJob("llama-update", "a llama.cpp update is already running")

    def job(self) -> dict[str, Any]:
        """The job itself, live — its one record, for the job's own thread."""
        return self._job.record

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

    def binary_built_at(self) -> int:
        """When the llama-server binary was built or replaced (epoch s), 0 when there is none."""
        bin_path = str(self.config.get("llamaServerBin") or "").strip()
        if not bin_path or not os.path.isfile(bin_path):
            return 0
        try:
            return int(os.path.getmtime(bin_path))
        except OSError:
            return 0

    def binary_mtime(self) -> str:
        """Return ISO-8601 mtime of the llama-server binary (date it was built/replaced)."""
        built_at = self.binary_built_at()
        return datetime.datetime.fromtimestamp(built_at).strftime("%Y-%m-%dT%H:%M:%S") if built_at else ""

    def status(self) -> dict:
        return self._job.status()

    def status_slim(self) -> dict:
        return self._job.status_slim()

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
        env = dict(os.environ)
        env["PATH"] = "/usr/local/cuda/bin:" + env.get("PATH", "/usr/bin:/bin")
        # Clients keep a SHORT archive (default 2: current + one-step undo) —
        # client snapshots are big and a client rollback is never urgent: cells
        # keep serving their old binary through any rebuild. config.json
        # `llamaBuildsKeep` overrides.
        env.setdefault("LLAMA_BUILDS_KEEP",
                       str(int(self.config.get("llamaBuildsKeep") or 2)))

        return self._job.start(cmd, tag, env)
