"""Shared ground for the scout's snapshot tests.

A snapshot pins what the scout DOES, by value, before any of it is rewritten.
Everything here serves one rule: nothing real is touched. A scout under test
lives in a temp dir, and every door to the host — a process, a signal, the
network — is shut by default. A pin that needs one opens it with `patched`,
naming exactly what it fakes.

Why shut and not just "remember to stub": the scout can reboot and power off
its host (POST /api/host/reboot runs `sudo -n systemctl reboot`), kill
processes by pid, and download gigabytes. A snapshot that forgot one stub
would do that for real on whatever machine ran it — a laptop, CI, the
controller. With the doors shut, a forgotten stub is a red test that names
the call.

Import this module before anything from caravan_scout: the scout reads some
paths from the environment at import time.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="scout-snapshot-"))
os.environ["LAMA_CARAVAN_SERVER_CELLS_DIR"] = str(TMP / "server-cells")
os.environ["LLAMA_BUILDS_DIR"] = str(TMP / "llama-builds")
sys.path.insert(0, str(ROOT))


class RealCallBlocked(BaseException):
    """A snapshot reached for the host: a process, a signal or the network.

    BaseException on purpose: the scout wraps its probes in `except Exception`,
    and a refusal it could catch would read as "no GPU", "nothing listening" —
    a forgotten stub drawn as a normal answer. Every refusal is also written
    down in BLOCKED, and Checks.finish fails on any, in case something
    swallowed even this.
    """


BLOCKED = []


def _blocked(what):
    def refuse(*args, **_kwargs):
        BLOCKED.append(f"{what}{args[:1]!r}")
        raise RealCallBlocked(f"{what} in a snapshot: {args[:1]!r} — fake it with patched()")
    return refuse


# The doors, shut. Kept so a pin can open one deliberately (it never should).
REAL = {"run": subprocess.run, "Popen": subprocess.Popen, "kill": os.kill,
        "urlopen": urllib.request.urlopen}
subprocess.run = _blocked("subprocess.run")
subprocess.Popen = _blocked("subprocess.Popen")
os.kill = _blocked("os.kill")
urllib.request.urlopen = _blocked("urllib.request.urlopen")


_THREAD_HOOK = threading.excepthook


def _door_in_a_thread(args):
    """A server thread that hit a shut door dies on a BaseException, and the
    default hook prints its whole traceback — a mutant's red run then reads
    as a crash. One line is enough: BLOCKED already holds the call and fails
    the file. Anything else keeps the default."""
    if issubclass(args.exc_type, RealCallBlocked):
        print(f"  (a server thread stopped at a shut door: {args.exc_value})")
        return
    _THREAD_HOOK(args)


threading.excepthook = _door_in_a_thread


class Checks:
    """The pass/fail ledger of one test file. Messages are the claims, in
    Russian like every guard's output here; a claim's negative twin says
    `negative:`, a pin of behaviour kept on purpose though ugly says `as-is:`."""

    def __init__(self, name):
        self.name = name
        self.failures = []
        self.count = 0

    def check(self, cond, msg):
        self.count += 1
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            self.failures.append(msg)

    def section(self, title):
        print(title)

    def finish(self):
        for call in BLOCKED:
            self.failures.append(f"a real call reached the host: {call}")
        if self.failures:
            print(f"\nFAILED ({len(self.failures)}):")
            for f in self.failures:
                print("  - " + f)
            return 1
        print(f"\n{self.name} OK: {self.count} checks")
        return 0


@contextlib.contextmanager
def patched(obj, **attrs):
    """Set attributes on `obj` for the duration of the block, then put back."""
    missing = object()
    saved = {k: getattr(obj, k, missing) for k in attrs}
    try:
        for k, v in attrs.items():
            setattr(obj, k, v)
        yield obj
    finally:
        for k, v in saved.items():
            if v is missing:
                delattr(obj, k)
            else:
                setattr(obj, k, v)


class FakeRun:
    """A stand-in for subprocess.run: answers the commands it knows, records
    every call, and raises FileNotFoundError for the rest — like a host where
    the tool is not installed.

    `table` maps a command prefix (tuple of argv items) to (returncode, stdout)
    or to an exception instance to raise.
    """

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []

    def __call__(self, cmd, *args, **kwargs):
        argv = tuple(cmd) if isinstance(cmd, (list, tuple)) else (cmd,)
        self.calls.append(list(argv))
        best = None
        for prefix, answer in self.table.items():
            if argv[:len(prefix)] == tuple(prefix) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, answer)
        if best is None:
            raise FileNotFoundError(argv[0])
        answer = best[1]
        if isinstance(answer, BaseException):
            raise answer
        rc, out = answer
        return subprocess.CompletedProcess(list(argv), rc, stdout=out, stderr="" if rc == 0 else out)


def make_scout(config=None, state=None):
    """A scout in its own temp dir: config.json and state.json written there,
    models cached there. The config names the host explicitly — the default is
    the machine's hostname, which a snapshot must not depend on."""
    from caravan_scout.scout import Scout
    home = Path(tempfile.mkdtemp(prefix="scout-", dir=TMP))
    cfg = {"hostId": "box-a", "displayName": "Box A", "listenHost": "127.0.0.1",
           "listenPort": 18092, "controllerUrl": "", "modelsBasePath": str(home / "models")}
    cfg.update(config or {})
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    if state is not None:
        (home / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return Scout(home / "config.json", home / "state.json")


class Served:
    """The scout's real HTTP handler on 127.0.0.1, an ephemeral port, for the
    length of a `with` block. Requests go through http.client, which the shut
    urlopen door does not touch."""

    def __init__(self, scout):
        from caravan_scout.http import Api
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Api(scout).handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        hdrs = dict(headers or {})
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")
            hdrs["Content-Length"] = str(len(data))
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
        ctype = resp.getheader("Content-Type") or ""
        self.last_headers = dict(resp.getheaders())
        conn.close()
        try:
            payload = json.loads(text) if "json" in ctype else text
        except ValueError:
            payload = text
        return resp.status, payload

    def get(self, path, headers=None):
        return self.request("GET", path, headers=headers)

    def post(self, path, body=None, headers=None, raw=None):
        return self.request("POST", path, body=body, headers=headers, raw=raw)


# The memory-limit probe launches systemd-run: a door to the host like the
# others, and on a Linux runner it would be reached. A snapshot sees a host
# whose cells get no limits; a pin about the limits sets the answer itself.
from caravan_scout.process import MemoryScope  # noqa: E402

MemoryScope._usable = False
