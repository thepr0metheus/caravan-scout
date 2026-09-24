"""A fresh llama.cpp build and crashing cells: the scout says so, and the
board offers a rollback."""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable


class CrashSuspect:
    """What the controller's banner says about its own machine — model cells
    crash after a recent llama.cpp build, roll back? — said for this one.

    The watchdog tells it of each crash, with the crash's words (crashed()).
    Only the words of an engine's death count, the controller's words: CUDA
    error, GGML_ABORT, SIGSEGV, SIGABRT, a core dump. A cell that will not
    start for want of a model or a port is not the build's fault. With the
    binary younger than 6 hours, 3 such crashes in 15 minutes make an
    incident. It is kept in state.json under this build (commit and binary
    time), so the banner is there whenever the board is opened, until the
    operator dismisses it for this build or the build changes — a restore or
    an update, and a new build starts clean. The rollback is the operator's:
    the board asks and confirms; nothing here restores anything.

    Crash times are counted in memory: a scout restart forgets those before
    it. An incident already raised stays.
    """

    MARKERS = re.compile(r"CUDA error|GGML_ABORT|SIGSEGV|SIGABRT|Aborted \(core dumped\)")
    MIN_CRASHES = 3
    WINDOW_SEC = 15 * 60
    FRESH_SEC = 6 * 3600

    def __init__(self, state, builds, clock: Callable[[], float] = time.time):
        self.state = state
        self.builds = builds
        self.clock = clock
        self._crashes: list[float] = []
        self._lock = threading.Lock()

    def build(self, version: str | None = None) -> tuple[str, int, str]:
        """This build: its commit (from `llama-server --version`), when its
        binary was made, and the key an incident and a dismissal are kept
        under. `version` when the caller has just read it."""
        if version is None:
            version = self.builds.binary_version()
        found = re.search(r"\(([0-9a-f]{6,40})\)", version or "")
        commit = found.group(1) if found else ""
        built_at = self.builds.binary_built_at()
        return commit, built_at, f"{commit}:{built_at}"

    def crashed(self, words: str) -> None:
        """A cell died saying `words` (its reason and last lines)."""
        if not self.MARKERS.search(words or ""):
            return
        now = self.clock()
        with self._lock:
            self._crashes = [t for t in self._crashes if now - t < self.WINDOW_SEC] + [now]
            count = len(self._crashes)
        commit, built_at, key = self.build()
        if count < self.MIN_CRASHES or now - built_at >= self.FRESH_SEC:  # no binary: built_at 0, never fresh
            return
        with self.state.lock:
            if self.state.get("llamaSuspectDismissed") == key:
                return
            before = self.state.get("llamaSuspect") or {}
            first = before.get("firstSeenAt") if before.get("key") == key else None
            self.state["llamaSuspect"] = {"key": key, "commit": commit, "builtAt": built_at,
                                          "firstSeenAt": int(first or now), "lastSeenAt": int(now),
                                          "crashes15m": count}
            self.state.save()
        print(f"[suspect] {count} crashes in 15 minutes on a llama.cpp build "
              f"{int((now - built_at) // 60)} minutes old ({commit or 'commit unknown'}) — the board offers a rollback")

    def verdict(self, version: str | None = None) -> dict[str, Any]:
        """The banner's word, for both reports: {"suspect": False}, or the
        incident with the build to roll back to (None when the archive holds
        no other)."""
        commit, built_at, key = self.build(version)
        with self.state.lock:
            incident = self.state.get("llamaSuspect")
            if incident and incident.get("key") != key:
                # Restored or updated since: a new build starts clean.
                self.state.pop("llamaSuspect")
                self.state.save()
                incident = None
        # A dismissal takes the incident away, and none comes back for that build (crashed()).
        if not incident:
            return {"suspect": False}
        return {"suspect": True, "crashes15m": incident.get("crashes15m", 0), "builtAt": built_at,
                "currentCommit": commit, "firstSeenAt": incident.get("firstSeenAt"),
                "lastSeenAt": incident.get("lastSeenAt"), "restoreCandidate": self.candidate(commit)}

    def candidate(self, commit: str) -> dict[str, Any] | None:
        """The newest archived build of another commit, or None."""
        for build in self.builds.archive().get("builds", []):
            other = str(build.get("commit") or "")
            if other and commit and not other.startswith(commit) and not commit.startswith(other):
                return {k: build[k] for k in ("id", "commit", "version", "builtAt", "sizeMb") if k in build}
        return None

    def dismiss(self) -> dict[str, Any]:
        """The operator hid the banner — for this build only."""
        _commit, _built_at, key = self.build()
        with self.state.lock:
            self.state["llamaSuspectDismissed"] = key
            self.state.pop("llamaSuspect", None)
            self.state.save()
        return {"ok": True, "dismissed": key}
