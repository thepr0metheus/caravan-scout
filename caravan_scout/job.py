"""A long job the scout runs in the background — a llama.cpp build, a vLLM
install — and the one record of how it goes."""
from __future__ import annotations

import re
import subprocess
import threading
import time
from typing import Any

from caravan_scout.errors import AppError


class BackgroundJob:
    """One job at a time, its output kept in a ring buffer.

    The job's own thread writes the record under the lock; the board reads
    the slim status (every heartbeat, for a build) and whoever asks reads the
    whole one. A second start while one runs is refused with `busy`, and the
    running job is left as it was.
    """

    #: Colour codes only — an erase-line sequence stays as it came.
    ANSI = re.compile(r"\x1b\[[0-9;]*m")
    #: The ring: past this many lines the oldest DROP_LINES go.
    KEEP_LINES = 500
    DROP_LINES = 100
    #: How many of the last lines a status carries.
    STATUS_LINES = 200

    def __init__(self, thread_name: str, busy: str):
        self.thread_name = thread_name
        self.busy = busy
        self.record: dict[str, Any] = {"running": False, "startedAt": 0, "tag": "", "lines": [],
                                       "done": False, "rc": None, "error": ""}
        self.lock = threading.Lock()

    def status(self) -> dict:
        with self.lock:
            snap = {k: v for k, v in self.record.items() if k != "lines"}
            snap["lines"] = list(self.record["lines"])[-self.STATUS_LINES:]
            return snap

    def status_slim(self) -> dict:
        job = self.record
        with self.lock:
            return {"running": job["running"], "done": job["done"], "rc": job["rc"],
                    "startedAt": job["startedAt"], "tag": job["tag"],
                    "lastLine": (job["lines"][-1] if job["lines"] else "")}

    def start(self, cmd: list[str], tag: str, env: dict[str, str]) -> dict:
        """Run `cmd` in the background under `tag`; the status right away."""
        with self.lock:
            if self.record["running"]:
                raise AppError(self.busy, 409)
            self.record.update({"running": True, "startedAt": int(time.time()), "tag": tag,
                                "lines": [], "done": False, "rc": None, "error": ""})
        threading.Thread(target=self._run, args=(cmd, env), daemon=True, name=self.thread_name).start()
        return self.status()

    def _run(self, cmd: list[str], env: dict[str, str]) -> None:
        job = self.record
        rc, error = -1, ""
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, env=env)
            for line in proc.stdout:
                clean = self.ANSI.sub("", line.rstrip())
                with self.lock:
                    job["lines"].append(clean)
                    if len(job["lines"]) > self.KEEP_LINES:
                        del job["lines"][:self.DROP_LINES]
            rc = proc.wait()
        except Exception as exc:
            error = str(exc)
        finally:
            with self.lock:
                job.update({"running": False, "done": True, "rc": rc, "error": error})
