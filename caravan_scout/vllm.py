"""vLLM on this machine: the version in its venv, the versions it had, and
the job that installs another."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from caravan_scout.errors import AppError
from caravan_scout.job import BackgroundJob


class VllmVenv:
    """The vLLM venv a vLLM cell runs from, and its updates.

    The venv is `~/vllm-venv` (the scout passes it): the controller's start
    line provisions and runs vLLM there ($HOME/vllm-venv), so this is the
    only place to look.
    vLLM is a pip package, so PyPI itself is the archive — an update and a
    rollback are the same `pip install vllm==X` with another pin. What is
    kept is a short history of the versions this venv has had, newest first:
    the rollback candidates. The controller's System panel used to do this
    for its own machine only; a machine with a scout had no way to move its
    vLLM at all.

    Running cells keep the vLLM they started with until they are restarted.
    """

    #: How many versions the history keeps.
    KEEP = 5
    #: A version pip is asked for: PEP 440 characters only.
    VERSION = re.compile(r"[0-9][0-9A-Za-z.+!-]*")

    def __init__(self, venv: Path, history: Path):
        self.venv = venv
        self.history_file = history
        self.job = BackgroundJob("vllm-update", "a vLLM install is already running")

    def pip(self) -> Path:
        return self.venv / "bin" / "pip"

    def version(self) -> str:
        """The installed vLLM version, from its dist-info folder (what pip
        itself reads, without starting pip); "" when there is none."""
        found = sorted(self.venv.glob("lib/python*/site-packages/vllm-*.dist-info"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not found:
            return ""
        return found[0].name[len("vllm-"):-len(".dist-info")]

    def history(self) -> list[dict[str, Any]]:
        try:
            rows = json.loads(self.history_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [r for r in rows if isinstance(r, dict) and r.get("version")] if isinstance(rows, list) else []

    def remember(self, version: str) -> None:
        """Put `version` first in the history (once), keeping KEEP."""
        if not version:
            return
        rows = [r for r in self.history() if r.get("version") != version]
        rows.insert(0, {"version": version, "seenAt": int(time.time())})
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            self.history_file.write_text(json.dumps(rows[:self.KEEP], indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            # A read of the version must not fail on this; the history just
            # does not grow, and the journal says why.
            print(f"[vllm] history not written ({self.history_file}): {exc}")

    def info(self) -> dict[str, Any]:
        """What the System panel shows for this machine: the version, the
        history to roll back to, and the job."""
        current = self.version()
        self.remember(current)
        return {"ok": True, "installed": bool(current), "version": current, "venv": str(self.venv),
                "history": self.history(), "job": self.job.status_slim()}

    def start_update(self, body: dict | None) -> dict:
        """POST /api/vllm/update {version?} — empty installs the latest
        release; a version pins it (a rollback is an older pin). Refused
        before anything runs: no venv yet (the first vLLM start provisions
        it), or a version that is not one."""
        if not self.pip().is_file():
            raise AppError("vLLM is not installed on this machine yet — start a vLLM cell once to create "
                           f"its venv ({self.venv})", 400)
        version = str((body or {}).get("version") or "").strip()
        if version and not self.VERSION.fullmatch(version):
            raise AppError(f"not a vLLM version: {version!r}", 400)
        self.remember(self.version())   # the rollback candidate
        cmd = [str(self.pip()), "install", *([f"vllm=={version}"] if version else ["--upgrade", "vllm"])]
        return self.job.start(cmd, f"vllm:{version or 'latest'}", dict(os.environ))
