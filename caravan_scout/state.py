"""The scout's state.json: what it keeps across restarts."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class ScoutState(dict):
    """state.json as the document it is: the heartbeat's last outcome, when
    the scout started, and the `cells` registry that the next start re-adopts.

    A dict, because every reader and writer treats it as one and it is saved
    as JSON; plus the one lock all writers share and an atomic save.
    """

    #: What 1.x kept about the agents on its machine. Nothing reads it now.
    STALE_1X = ("assignments", "applyStatus")

    def __init__(self, path: Path):
        super().__init__(self.read_file(Path(path)))
        self.path = Path(path)
        self.lock = threading.Lock()
        self.setdefault("startedAt", int(time.time()))
        self.setdefault("heartbeat", {"state": "pending"})
        # A state.json written by 1.x still keeps the routes it applied to
        # agents and how that went; they are dropped once, and the log says so.
        stale = [key for key in self.STALE_1X if key in self]
        for key in stale:
            self.pop(key)
        if stale:
            print(f"[state] dropped what 1.x kept about agents: {', '.join(stale)}")
            self.save()

    @staticmethod
    def read_file(path: Path) -> dict[str, Any]:
        """The file's contents, or {} — for a missing file and for an
        unreadable one alike: a bad state is a bad start, not a dead scout."""
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
            except Exception:
                pass
        return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
