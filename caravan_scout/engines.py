"""Model engines on this machine that are not its cells: Ollama and LM Studio,
found by their ports and process names and read through their own HTTP APIs.

Looking is read only: the scan's one verb is GET, and an engine someone runs
by hand next to the caravan is never changed by being looked at. Changing it
is a separate, explicit act: the operator unloads a model (2.14), deletes or
downloads one (2.17), starts or stops the engine's server (2.16), from the
board, and only then does the scout POST to it — to an engine it found, about
a model the engine listed. No model is loaded from here (2.18): a cell in the
engine loads its model when it starts.
"""
from __future__ import annotations

import errno
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


class EngineAsk:
    """GET on one engine's port: (status, JSON payload).

    The status is None when nothing answered — refused, timed out; the payload
    is None when the answer is not JSON. An HTTP error is an answer: its code
    is the status (401 is how an engine says it wants a token).
    """

    TIMEOUT = 1.5
    #: No engine's model list is near this; a stuck stream is cut here.
    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float | None = None):
        self.port = int(port)
        self.host = host
        self.timeout = self.TIMEOUT if timeout is None else timeout

    def __call__(self, path: str) -> tuple[int | None, Any]:
        url = f"http://{self.host}:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read(self.MAX_BYTES)
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except Exception:
            return None, None
        try:
            return status, json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return status, None


class EngineCall:
    """POST to one engine's port: (status, JSON payload, the engine's own
    words). The only place the scout changes an engine, and only when the
    operator asked. The status is None when nothing answered.

    An engine busy with another request may answer late, so the timeout
    is minutes, not seconds; the call runs on a thread of its own
    (ForeignEngines.act)."""

    TIMEOUT = 300.0
    MAX_BYTES = 1024 * 1024

    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float | None = None):
        self.port = int(port)
        self.host = host
        self.timeout = self.TIMEOUT if timeout is None else timeout

    def __call__(self, path: str, body: dict[str, Any], method: str = "POST") -> tuple[int | None, Any, str]:
        request = urllib.request.Request(f"http://{self.host}:{self.port}{path}", method=method,
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read(self.MAX_BYTES)
        except urllib.error.HTTPError as exc:
            try:
                status, raw = exc.code, exc.read(self.MAX_BYTES)
            except Exception:
                status, raw = exc.code, b""
        except Exception as exc:
            return None, None, f"no answer: {exc}"[:300]
        text = raw.decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        # Its words are for a refusal; an answer that did what was asked says none.
        return status, payload, "" if 200 <= status < 300 else self.words(payload, text)

    @staticmethod
    def words(payload: Any, text: str) -> str:
        """What the engine said went wrong, as it said it: its `error` (a text
        or {message}), else the raw answer."""
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            error = error.get("message") or json.dumps(error)
        return str(error or text or "").strip()[:300]


class EngineStream:
    """POST to one engine's port, its answer read line by line as it comes:
    a download says its progress that way (Ollama's /api/pull). Each JSON
    line goes to `on_line`; the call returns (status, the engine's words),
    the words only for a refusal. The timeout is between two lines, not for
    the whole download."""

    TIMEOUT = 300.0

    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float | None = None):
        self.port = int(port)
        self.host = host
        self.timeout = self.TIMEOUT if timeout is None else timeout

    def __call__(self, path: str, body: dict[str, Any], on_line: Callable[[Any], None]) -> tuple[int | None, str]:
        request = urllib.request.Request(f"http://{self.host}:{self.port}{path}", method="POST",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        continue
                    on_line(payload)
                return resp.status, ""
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read(EngineCall.MAX_BYTES).decode("utf-8", "replace")
            except Exception:
                text = ""
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            return exc.code, EngineCall.words(payload, text)
        except Exception as exc:
            return None, f"no answer: {exc}"[:300]


class LmsCli:
    """LM Studio's own command line, `lms`, where every LM Studio — the app
    and its windowless daemon — puts it: ~/.lmstudio/bin/lms.

    Its REST API unloads; the command line starts and stops its daemon and
    server, and says how long a loaded model is held (`lms ps`, 2.15). It
    talks to the LM Studio of the user it runs as, which is the scout's.

    (argv, timeout) -> (exit code, what it printed, colours stripped); the
    code is None when it did not run or did not finish.
    """

    #: A question is a moment (0.14 s measured); it is asked inside a scan
    #: or a request, which must not hang on it.
    QUICK = 10.0
    #: Starting or stopping its daemon and server takes longer.
    SLOW = 300.0
    ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
    #: LM Studio's daemon copies `lms` over itself as it starts (seen
    #: 2026-09-25: `lms daemon up`, then `lms server start` failed with
    #: "Text file busy"). A command that meets the copy is tried again, this
    #: often and this many seconds apart — five seconds in all.
    BUSY_TRIES = 20
    BUSY_PAUSE = 0.25

    def __init__(self, home: str | Path | None = None, run: Callable[..., Any] | None = None,
                 pause: Callable[[float], None] | None = None):
        self._home = home
        self._run = run or self.run_process
        self._pause = pause or (lambda sec: time.sleep(sec))

    @property
    def path(self) -> Path:
        # The home looked up when asked, not when the scout was built.
        return Path(self._home or Path.home()) / ".lmstudio" / "bin" / "lms"

    def available(self) -> bool:
        return os.access(self.path, os.X_OK)

    @staticmethod
    def said(text: str) -> str:
        """What a failed command said, without its progress lines and the
        advice after them: "Model not found — No model found that matches
        model key "x"."."""
        lines = [line.strip() for line in str(text or "").splitlines()
                 if line.strip() and not line.strip().startswith("Loading ")]
        return " — ".join(lines[:2])[:300]

    def __call__(self, args: list[str], timeout: float | None = None) -> tuple[int | None, str]:
        if not self.available():
            return None, f"no lms at {self.path}"
        code, text = self._run([str(self.path), *args], self.QUICK if timeout is None else timeout)
        return code, self.ANSI.sub("", text or "").replace("\r", "\n")

    def run_process(self, argv: list[str], timeout: float) -> tuple[int | None, str]:
        for attempt in range(self.BUSY_TRIES):
            try:
                done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return None, f"lms did not finish in {timeout:.0f} s"
            except OSError as exc:
                if exc.errno == errno.ETXTBSY and attempt + 1 < self.BUSY_TRIES:
                    self._pause(self.BUSY_PAUSE)
                    continue
                return None, f"lms did not run: {exc}"
            return done.returncode, (done.stdout or "") + (done.stderr or "")
        return None, "lms did not run"


class EngineKind:
    """One kind of engine: the port it takes when nobody changes it, what its
    processes are called, and how its API is read.

    The kinds are a table (ForeignEngines.KINDS). A new engine is a new kind;
    nothing outside a kind asks which one it is.
    """

    id = ""
    label = ""
    default_port = 0
    #: Its processes by the start of their name, lower case: the server, its
    #: runners, its helpers.
    process_prefixes: tuple[str, ...] = ()

    @staticmethod
    def number(value: Any) -> int | None:
        """A count or a size as the engine said it; None when it did not."""
        if isinstance(value, bool):
            return None
        try:
            return int(value) if value is not None and str(value).strip() != "" else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def text(value: Any, limit: int = 120) -> str:
        return str(value or "").strip()[:limit]

    def is_its_process(self, name: Any) -> bool:
        low = str(name or "").strip().lower()
        return bool(low) and any(low.startswith(p) for p in self.process_prefixes)

    def read(self, ask: Callable[[str], tuple[int | None, Any]]) -> dict[str, Any] | None:
        """What the engine behind `ask` says: {"state": "ok", "version",
        "models", …}; {"state": "auth"} when it wants a token;
        {"state": "unreachable"} when nothing answered. None when what
        answers is not this kind."""
        raise NotImplementedError

    def controls(self, seen: dict[str, Any]) -> list[str]:
        """What the operator can do to this engine from the board, as it
        answered: nothing to an engine that did not answer or wants a token."""
        return []

    def pull(self, reach, name: str, progress: Callable[[int | None, int | None], None]) -> str:
        """Download the model `name` into the engine (2.17): "" when it did,
        else the engine's own words; `progress(doneBytes, totalBytes)` as it
        goes. `reach` has call, ask, stream and pause for this engine."""
        raise NotImplementedError

    def delete(self, call, name: str) -> str:
        """Remove the model `name` from the engine's disk: "" or its words.
        A kind that cannot says so — and does not offer it (`controls`)."""
        return f"{self.label} cannot delete a model from here"

    def unload(self, call, ask, model: str) -> str:
        """Unload `model`: "" when it did, else the engine's own words."""
        raise NotImplementedError

    # ── the server itself (2.16): learned from its run, started, stopped ──

    def recipe(self, info: dict[str, Any] | None, view: dict[str, Any]) -> dict[str, Any] | None:
        """How to start this kind's server again, learned from the one
        running now: its process as the machine says it (`info`: exe, args,
        env — None where /proc does not say) and its view. None when it
        cannot be told."""
        return None

    def installed(self, home: Path) -> dict[str, Any] | None:
        """A recipe for a server never seen running here, from where this
        kind puts itself in the user's home; None when it is not there. A
        system-wide install is not offered: its server is the system's."""
        return None

    def start_server(self, recipe: dict[str, Any], procs) -> str:
        """Start the server the recipe says: "" when it was started, else
        why not. Whether it answers is the caller's to wait for."""
        raise NotImplementedError

    def stop_server(self, recipe: dict[str, Any], pid: int | None, procs) -> str:
        """Stop the server running as `pid`: "" when it was told to stop,
        else why not."""
        raise NotImplementedError

    @staticmethod
    def refused(status: int | None, words: str) -> str:
        """"" for a 2xx; the reason for anything else, never empty."""
        if status is not None and 200 <= status < 300:
            return ""
        return words or (f"http {status}" if status else "no answer")

    @staticmethod
    def row(name: str, **fields: Any) -> dict[str, Any]:
        """One model in the shape every kind reports: what a request names it
        by, what the engine says of its file, and — when it is loaded — what
        it holds and the window it serves. A field the engine does not say is
        None, never a zero."""
        return {"name": name, "type": "", "format": "", "family": "", "params": "", "quant": "",
                "fileBytes": None, "remote": False, "loaded": None, "memBytes": None, "vramBytes": None,
                "contextLength": None, "maxContextLength": None, "expiresAt": "", "staysLoaded": None,
                "instances": None, **fields}


class Ollama(EngineKind):
    """Ollama: `ollama serve`, its runner processes, port 11434.

    /api/version proves it is Ollama. /api/ps says what is loaded — the
    memory a model holds and how much of it is on the cards, the window it
    was loaded with, when keep_alive unloads it; /api/tags says what is
    installed. A tag with `remote_host` is a model Ollama runs on its own
    cloud, not on this machine.
    """

    id = "ollama"
    label = "Ollama"
    default_port = 11434
    process_prefixes = ("ollama",)

    def controls(self, seen):
        return ["unload", "delete", "pull"] if seen.get("state") == "ok" else []

    def pull(self, reach, name, progress):
        """/api/pull, streamed: each layer says its size and how much of it
        has come; the progress is their sum. A failure is a line of its own
        ({"error": …}) — even after a 200."""
        layers: dict[str, tuple[int, int]] = {}
        failed: list[str] = []

        def on_line(line):
            if not isinstance(line, dict):
                return
            if line.get("error"):
                failed.append(self.text(line.get("error"), 300))
                return
            total = self.number(line.get("total"))
            if line.get("digest") and total:
                layers[str(line["digest"])] = (self.number(line.get("completed")) or 0, total)
                progress(sum(c for c, _t in layers.values()), sum(t for _c, t in layers.values()))

        status, words = reach.stream("/api/pull", {"model": name, "stream": True}, on_line)
        return failed[-1] if failed else self.refused(status, words)

    def delete(self, call, name):
        status, _payload, words = call("/api/delete", {"model": name}, method="DELETE")
        return self.refused(status, words)

    def unload(self, call, ask, model):
        status, _payload, words = call("/api/generate", {"model": model, "keep_alive": 0})
        return self.refused(status, words)

    #: What configures a server, by name: kept from the run it was learned from.
    ENV_KEPT = ("OLLAMA_", "CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES")
    #: Where Ollama's own archive puts it in a home, and pip-style installs.
    HOME_BINARIES = ("ollama/bin/ollama", ".local/bin/ollama", "bin/ollama")
    #: How long a server told to stop gets before it is killed.
    STOP_GRACE = 10.0

    def recipe(self, info, view):
        """`ollama serve` as it runs: its binary, and the OLLAMA_* and device
        variables it was started with (what makes it "the one set up")."""
        exe = str((info or {}).get("exe") or "")
        args = [str(a) for a in (info or {}).get("args") or []]
        if Path(exe).name != "ollama" or args[1:2] != ["serve"]:
            return None
        env = {k: str(v) for k, v in ((info or {}).get("env") or {}).items() if str(k).startswith(self.ENV_KEPT)}
        return {"exe": exe, "args": ["serve"], "env": env}

    def installed(self, home):
        for relative in self.HOME_BINARIES:
            exe = Path(home) / relative
            if os.access(exe, os.X_OK):
                return {"exe": str(exe), "args": ["serve"], "env": {}}
        return None

    def start_server(self, recipe, procs):
        """`ollama serve`, detached from the scout (a scout restart leaves it
        running, as it leaves its cells), its output in a log of its own."""
        exe = str(recipe.get("exe") or "")
        if not os.access(exe, os.X_OK):
            return f"{exe or 'ollama'} is not there to run"
        return procs.spawn([exe, *[str(a) for a in recipe.get("args") or ["serve"]]],
                           {str(k): str(v) for k, v in (recipe.get("env") or {}).items()}, self.id)

    def stop_server(self, recipe, pid, procs):
        """SIGTERM: Ollama unloads its models and stops its runners; one that
        does not go in STOP_GRACE seconds is killed, runners and all."""
        if not pid:
            return "nothing listens on its port"
        return procs.terminate(int(pid), self.STOP_GRACE)

    def read(self, ask):
        status, version = ask("/api/version")
        if status is None:
            return {"state": "unreachable"}
        if status in (401, 403):
            return {"state": "auth"}
        if status != 200 or not isinstance(version, dict) or not isinstance(version.get("version"), str):
            return None
        ps_status, ps = ask("/api/ps")
        tags_status, tags = ask("/api/tags")
        loaded_known = ps_status == 200 and isinstance(ps, dict) and isinstance(ps.get("models"), list)
        loaded = {}
        for m in (ps.get("models") if loaded_known else None) or []:
            if isinstance(m, dict) and self.text(m.get("name")):
                loaded[self.text(m.get("name"))] = m
        installed_known = tags_status == 200 and isinstance(tags, dict) and isinstance(tags.get("models"), list)
        models = []
        for m in (tags.get("models") if installed_known else None) or []:
            name = self.text(m.get("name")) if isinstance(m, dict) else ""
            if not name:
                continue
            models.append(self._model(name, m, loaded.pop(name, None), loaded_known))
        # Loaded but not installed: a tag list that did not answer, or a model
        # removed while it ran. Still on the card, so still said.
        models.extend(self._model(name, m, m, True) for name, m in loaded.items())
        return {"state": "ok", "version": self.text(version["version"], 40), "models": models,
                **({} if installed_known else {"installedKnown": False})}

    def _model(self, name, tag, running, loaded_known):
        details = tag.get("details") if isinstance(tag.get("details"), dict) else {}
        fields = {"format": self.text(details.get("format"), 20), "family": self.text(details.get("family"), 40),
                  "params": self.text(details.get("parameter_size"), 20),
                  "quant": self.text(details.get("quantization_level"), 20),
                  "remote": bool(tag.get("remote_host")),
                  "loaded": (running is not None) if loaded_known else None}
        if running is not None:
            fields.update(memBytes=self.number(running.get("size")), vramBytes=self.number(running.get("size_vram")),
                          contextLength=self.number(running.get("context_length")),
                          expiresAt=self.text(running.get("expires_at"), 40))
        if running is not tag:
            fields["fileBytes"] = self.number(tag.get("size"))
        return self.row(name, **fields)


class LmStudio(EngineKind):
    """LM Studio: the app or its windowless daemon (llmster), port 1234.

    Its native API (0.4+) lists every model with the instances loaded from
    it; 0.3's older /api/v0 says only loaded or not. Neither says the memory
    a model holds: that is the process's (nvidia-smi, ps). It may be set to
    want a token, and then says 401 to everything.
    """

    id = "lmstudio"
    label = "LM Studio"
    default_port = 1234
    process_prefixes = ("lm studio", "lm-studio", "lmstudio", "llmster")

    def __init__(self, cli: LmsCli | None = None):
        self.cli = cli or LmsCli()
        # Most of `lms` wakes LM Studio up when it is not running ("Waking up
        # LM Studio service..." — `lms ps` does, seen 2026-09-25): a read
        # that met a stop half-way started the daemon again. Every sequence
        # of commands holds this lock, and one that must not wake it asks
        # `lms server status` (which does not) first.
        self.lock = threading.Lock()

    def running(self) -> bool:
        """Whether its server runs now, as `lms server status` says — the
        one question that does not wake LM Studio. Called with the lock held."""
        code, text = self.cli(["server", "status"])
        return code == 0 and "is running" in text

    #: Seconds between two looks at a download's progress.
    POLL = 2.0

    def controls(self, seen):
        """Only its native API (0.4+) unloads and downloads; 0.3's /api/v0
        has no such verbs, and is read, not driven. A model is not
        deleted from here: LM Studio has no verb for it, and which files are a
        model's it does not say (a catalog name is not a path; some ship
        inside the app) — a guess would delete the wrong ones."""
        return ["unload", "pull"] if seen.get("state") == "ok" and seen.get("api") == "v1" else []

    def pull(self, reach, name, progress):
        """/api/v1/models/download starts a job; its status is asked every
        POLL seconds until it completes or fails."""
        status, payload, words = reach.call("/api/v1/models/download", {"model": name})
        if status is None or not 200 <= status < 300:
            return self.refused(status, words)
        payload = payload if isinstance(payload, dict) else {}
        if payload.get("status") in ("already_downloaded", "completed"):
            return ""
        job = self.text(payload.get("job_id"), 120)
        if not job:
            return f"LM Studio started no download ({payload.get('status') or 'no status'})"
        total = self.number(payload.get("total_size_bytes"))
        while True:
            reach.pause(self.POLL)
            code, body = reach.ask(f"/api/v1/models/download/status/{job}")
            if code is None:
                return "LM Studio stopped answering during the download"
            if code != 200 or not isinstance(body, dict):
                return self.refused(code, "the download's status did not answer")
            total = self.number(body.get("total_size_bytes")) or total
            progress(self.number(body.get("downloaded_bytes")), total)
            state = body.get("status")
            if state == "completed":
                return ""
            if state == "failed":
                return "LM Studio says the download failed"
            if state == "paused":
                return "the download was paused in LM Studio"

    def recipe(self, info, view):
        """Its own command line starts and stops it; what is learned is where
        it listens — the port, and whether only this machine may call."""
        if not self.cli.available():
            return None
        return {"port": int(view.get("port") or self.default_port),
                "bind": "127.0.0.1" if view.get("listen") == "loopback" else "0.0.0.0"}

    def installed(self, home):
        """Installed where its command line is — the app and its windowless
        daemon alike; its server on its usual port, this machine only (LM
        Studio's own default)."""
        if not LmsCli(home=home).available():
            return None
        return {"port": self.default_port, "bind": "127.0.0.1"}

    def start_server(self, recipe, procs):
        """`lms daemon up`, then `lms server start` on the learned port and
        address. The command line returns when the server has started."""
        with self.lock:
            code, text = self.cli(["daemon", "up"], timeout=LmsCli.SLOW)
            if code != 0:
                return LmsCli.said(text) or f"lms daemon up exited with {code}"
            code, text = self.cli(["server", "start", "-p", str(int(recipe.get("port") or self.default_port)),
                                   "--bind", str(recipe.get("bind") or "127.0.0.1")], timeout=LmsCli.SLOW)
        return "" if code == 0 else (LmsCli.said(text) or f"lms server start exited with {code}")

    def stop_server(self, recipe, pid, procs):
        """`lms daemon down` — the daemon holds the loaded models, and only
        its going frees their memory; where there is no daemon (the app
        runs the server), `lms server stop`."""
        with self.lock:
            code, text = self.cli(["daemon", "down"], timeout=LmsCli.SLOW)
            if code == 0:
                return ""
            code, text = self.cli(["server", "stop"], timeout=LmsCli.SLOW)
        return "" if code == 0 else (LmsCli.said(text) or f"lms server stop exited with {code}")

    def unload(self, call, ask, model):
        """Every loaded instance of the model, by the ids the engine gives
        now — asked afresh, not taken from the last scan."""
        status, body = ask("/api/v1/models")
        if status != 200 or not isinstance(body, dict):
            return self.refused(status, "the model list did not answer")
        ids = [str(i.get("id")) for m in body.get("models") or [] if isinstance(m, dict) and m.get("key") == model
               for i in m.get("loaded_instances") or [] if isinstance(i, dict) and i.get("id")]
        if not ids:
            return f"{model} is not loaded"
        for instance in ids:
            status, _payload, words = call("/api/v1/models/unload", {"instance_id": instance})
            reason = self.refused(status, words)
            if reason:
                return reason
        return ""

    def read(self, ask):
        status, body = ask("/api/v1/models")
        if status is None:
            return {"state": "unreachable"}
        if status in (401, 403):
            return {"state": "auth"}
        if status == 200 and isinstance(body, dict) and isinstance(body.get("models"), list):
            rows = [m for m in body["models"] if isinstance(m, dict) and self.text(m.get("key"))]
            limits = self.idle_limits() if any(m.get("loaded_instances") for m in rows) else {}
            return {"state": "ok", "version": "", "api": "v1",
                    "models": [self._v1(m, limits) for m in rows]}
        status, body = ask("/api/v0/models")
        rows = body.get("data") if status == 200 and isinstance(body, dict) else None
        # Its own mark: every entry says whether it is loaded. A plain
        # OpenAI-style list of models has no `state`.
        if isinstance(rows, list) and rows and all(isinstance(m, dict) and "state" in m for m in rows):
            return {"state": "ok", "version": "", "api": "v0",
                    "models": [self._v0(m) for m in rows if self.text(m.get("id"))]}
        return None

    def idle_limits(self) -> dict[str, dict[str, Any]]:
        """{instance id: {ttlMs, lastUsedTime}} of what is loaded, from `lms
        ps --json` — the REST list does not say an idle limit. {} when the
        command line is not there or does not answer, or its server is not
        running — `lms ps` would wake LM Studio up: then nobody knows."""
        with self.lock:
            if not self.running():
                return {}
            code, text = self.cli(["ps", "--json"])
        try:
            rows = json.loads(text) if code == 0 else None
        except ValueError:
            rows = None
        return {str(r.get("identifier")): r for r in rows or [] if isinstance(r, dict) and r.get("identifier")}

    def _v1(self, m, limits=None):
        instances = [i for i in (m.get("loaded_instances") or []) if isinstance(i, dict)]
        config = instances[0].get("config") if instances and isinstance(instances[0].get("config"), dict) else {}
        quant = m.get("quantization")
        # The first instance's idle limit, as the window is the first one's:
        # when it is let go (lastUsedTime + ttlMs), or that it stays until
        # unloaded (ttlMs null). Neither when `lms ps` did not say.
        limit = (limits or {}).get(str(instances[0].get("id"))) if instances else None
        ttl = self.number(limit.get("ttlMs")) if limit else None
        last = self.number(limit.get("lastUsedTime")) if limit else None
        expires = ""
        if ttl and last:
            expires = datetime.fromtimestamp((last + ttl) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
        return self.row(self.text(m.get("key"), 200), type=self.text(m.get("type"), 20), format=self.text(m.get("format"), 20),
                        family=self.text(m.get("architecture"), 40), params=self.text(m.get("params_string"), 20),
                        quant=self.text(quant.get("name") if isinstance(quant, dict) else quant, 20),
                        fileBytes=self.number(m.get("size_bytes")), loaded=bool(instances),
                        contextLength=self.number(config.get("context_length")) if instances else None,
                        maxContextLength=self.number(m.get("max_context_length") or config.get("max_context_length")),
                        instances=len(instances), expiresAt=expires,
                        staysLoaded=(limit.get("ttlMs") is None) if limit else None)

    def _v0(self, m):
        loaded = str(m.get("state") or "") == "loaded"
        return self.row(self.text(m.get("id"), 200), type=self.text(m.get("type"), 20),
                        format=self.text(m.get("compatibility_type"), 20), family=self.text(m.get("arch"), 40),
                        quant=self.text(m.get("quantization"), 20), loaded=loaded,
                        contextLength=self.number(m.get("loaded_context_length")) if loaded else None,
                        maxContextLength=self.number(m.get("max_context_length")))


class ForeignEngines:
    """The engines on this machine that are not its cells, as the report says
    them (`engines`, scout 2.12+), rescanned every 10 s by a thread of their
    own.

    Found where a kind listens by default, or where a process of its name
    listens. The ports of this scout's cells are never asked: llama-server
    answers some of the same paths, and a cell must not turn into a foreign
    engine. An answer proves the kind; a port that says nothing, or wants a
    token, is shown only when its process is the kind's own and — for
    silence — it is the kind's usual port: anything else there would be a
    guess.

    A scan never runs inside a report: a hung engine would hold /api/state,
    which the controller asks with a 2 s timeout, and the whole machine would
    go stale on the board for one engine's sake. Before the first scan the
    report says None — "not looked yet" — and after it a list, [] being
    "looked, none here".
    """

    KINDS: tuple[EngineKind, ...] = (Ollama(), LmStudio())
    PERIOD = 10.0
    #: Addresses a port listens on that take connections from anywhere.
    WILDCARDS = ("*", "0.0.0.0", "::", "[::]")

    #: Seconds a started server has to answer, and a stopped one to go quiet.
    START_WAIT = 60.0
    STOP_WAIT = 20.0

    def __init__(self, machine, cells, ask: Callable[..., Any] = EngineAsk, call: Callable[..., Any] = EngineCall,
                 clock: Callable[[], float] | None = None, spawn: Callable[..., Any] | None = None,
                 kinds: tuple[EngineKind, ...] | None = None, servers=None,
                 pause: Callable[[float], None] | None = None, stream: Callable[..., Any] = EngineStream):
        self.machine = machine
        self.cells = cells
        self.ask = ask
        self.call = call
        self.stream = stream
        # The servers themselves (2.16, engine_servers.py): who runs each,
        # and starting and stopping them. None — looking and acting on models only.
        self.servers = servers
        # How a start or stop waits between looks; a test does not wait.
        self.pause = pause or (lambda sec: time.sleep(sec))
        # The kinds it looks for; a test hands in kinds with their command
        # line stood in for.
        self.kinds = kinds or self.KINDS
        # time.time looked up at each reading, so a patched clock is seen.
        self.clock = clock or (lambda: time.time())
        # How an action runs off the request: a daemon thread; a test runs it
        # in place.
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._views: list[dict[str, Any]] | None = None
        # (kind, port, model) -> {op, since}: an action under way.
        self._actions: dict[tuple[str, int, str], dict[str, Any]] = {}
        # (kind, port, model) -> {op, error, at}: the last one that failed,
        # until the next action on that model.
        self._failures: dict[tuple[str, int, str], dict[str, Any]] = {}
        # (kind, port) -> {model, since, doneBytes, totalBytes}: the download
        # under way on that engine (2.17), one at a time; and the last one
        # that failed, until the next download there.
        self._downloads: dict[tuple[str, int], dict[str, Any]] = {}
        self._download_errors: dict[tuple[str, int], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def views(self) -> list[dict[str, Any]] | None:
        """The last scan, with what is being done to each model and what
        failed last: a list, or None before the first scan."""
        with self._lock:
            if self._views is None:
                return None
            return [self.with_actions(v) for v in self._views]

    def with_actions(self, view: dict[str, Any]) -> dict[str, Any]:
        """A copy of one engine's view, each model carrying `action` while an
        action on it runs and `actionError` when the last one failed; the
        engine itself `serverAction` and `serverError` — its start or stop
        (2.16), keyed by no model."""
        server = (view["kind"], int(view["port"]), "")
        engine = (view["kind"], int(view["port"]))
        view = {**view, **({"serverAction": dict(self._actions[server])} if server in self._actions else {}),
                **({"serverError": dict(self._failures[server])} if server in self._failures else {}),
                **({"downloading": dict(self._downloads[engine])} if engine in self._downloads else {}),
                **({"downloadError": dict(self._download_errors[engine])} if engine in self._download_errors else {})}
        if not isinstance(view.get("models"), list):
            return dict(view)
        rows = []
        for model in view["models"]:
            key = (view["kind"], int(view["port"]), model.get("name"))
            row = dict(model)
            if key in self._actions:
                row["action"] = dict(self._actions[key])
            if key in self._failures:
                row["actionError"] = dict(self._failures[key])
            rows.append(row)
        return {**view, "models": rows}

    def act(self, op: str, kind_id: str, port: Any, model: str) -> dict[str, Any]:
        """Unload a model the engine holds, or delete one from its disk — on a
        thread of its own; answered at once, with the engines as they are now
        (the model marked as being acted on).

        Only an engine the last scan found, only an act it offers
        (`controls`), only a model it listed — the scout is no proxy to an
        arbitrary port — and one act per model at a time. No load: a cell in
        the engine loads its model when it starts (2.18)."""
        from caravan_scout.errors import AppError
        if op not in ("unload", "delete"):
            raise AppError(f"unknown engine action {op!r}", 400)
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise AppError("port must be a number", 400)
        kind = next((k for k in self.kinds if k.id == kind_id), None)
        key = (kind_id, port, model)
        with self._lock:
            self._target(op, kind, key)
            self._actions[key] = {"op": op, "since": int(self.clock())}
            self._failures.pop(key, None)
        host = self.ask_host(self.listener_of(port))
        self.spawn(lambda: self._run(kind, op, key, host))
        return {"ok": True, "engines": self.views()}

    def _target(self, op: str, kind: EngineKind | None, key: tuple[str, int, str]) -> dict[str, Any]:
        """The model row an act is about, as the last scan saw it — or the
        refusal that says why there is none to act on. Called with the lock
        held."""
        from caravan_scout.errors import AppError
        kind_id, port, model = key
        view = next((v for v in self._views or [] if v.get("kind") == kind_id and v.get("port") == port), None)
        if kind is None or view is None:
            raise AppError(f"no {kind_id or 'engine'} on port {port} here", 404)
        if op not in (view.get("controls") or []):
            raise AppError(f"{view.get('label')} on port {port} cannot {op} from here", 409)
        row = next((m for m in view.get("models") or [] if m.get("name") == model), None)
        if row is None:
            raise AppError(f"{view.get('label')} lists no model {model!r}", 404)
        if op == "unload" and row.get("loaded") is not True:
            raise AppError(f"{model} is not loaded", 409)
        if op == "delete" and row.get("loaded") is True:
            raise AppError(f"{model} is loaded — unload it first", 409)
        if key in self._actions:
            doing = {"unload": "unloaded", "delete": "deleted"}[self._actions[key]["op"]]
            raise AppError(f"{model} is being {doing} already", 409)
        return dict(row)

    #: A model's name as a download takes it: a catalog name, a tag, a link —
    #: one word, no spaces or control characters.
    MODEL_NAME = re.compile(r"^[^\s\x00-\x1f\x7f]{1,300}$")

    def pull(self, kind_id: str, port: Any, model: str) -> dict[str, Any]:
        """Download a model into an engine (2.17) — on a thread of its own:
        a download takes minutes to hours. Answered at once, the engine
        carrying `downloading` {model, since, doneBytes, totalBytes}, which
        its thread keeps up to date; one download per engine at a time. What
        failed stays as `downloadError` until the next download there."""
        from caravan_scout.errors import AppError
        model = str(model or "").strip()
        if not self.MODEL_NAME.match(model):
            raise AppError("model must be one name, such as qwen3:8b", 400)
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise AppError("port must be a number", 400)
        kind = next((k for k in self.kinds if k.id == kind_id), None)
        key = (kind_id, port)
        with self._lock:
            view = next((v for v in self._views or [] if v.get("kind") == kind_id and v.get("port") == port), None)
            if kind is None or view is None:
                raise AppError(f"no {kind_id or 'engine'} on port {port} here", 404)
            if "pull" not in (view.get("controls") or []):
                raise AppError(f"{view.get('label')} on port {port} cannot download from here", 409)
            if key in self._downloads:
                raise AppError(f"{view.get('label')} is downloading {self._downloads[key]['model']} already", 409)
            self._downloads[key] = {"model": model, "since": int(self.clock()), "doneBytes": None, "totalBytes": None}
            self._download_errors.pop(key, None)
        host = self.ask_host(self.listener_of(port))
        self.spawn(lambda: self._pull_run(kind, key, host, model))
        return {"ok": True, "engines": self.views()}

    def _pull_run(self, kind, key, host, model) -> None:
        kind_id, port = key

        def progress(done, total):
            with self._lock:
                if key in self._downloads:
                    self._downloads[key].update(doneBytes=done, totalBytes=total)

        reach = SimpleNamespace(call=self.call(port, host), ask=self.ask(port, host),
                                stream=self.stream(port, host), pause=self.pause)
        try:
            reason = kind.pull(reach, model, progress)
        except Exception as exc:  # noqa: BLE001 — a download that crashed failed; it must not stay "under way"
            reason = f"{type(exc).__name__}: {exc}"[:300]
        with self._lock:
            self._downloads.pop(key, None)
            if reason:
                self._download_errors[key] = {"model": model, "error": reason, "at": int(self.clock())}
        print(f"[engines] download {model} into {kind_id}:{port}: {reason or 'done'}")
        try:
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            print(f"[engines] scan after the download failed: {exc}")

    def serve(self, op: str, kind_id: str, port: Any) -> dict[str, Any]:
        """Start an engine's server, or stop it (2.16) — on a thread of its
        own: a start waits for the server to answer, a stop for it to go
        quiet. Answered at once with the engines as they are now, the engine
        marked `serverAction`. Only what its view offers (`controls`): a
        stop for a server this scout's user runs, a start for a known one
        that is not running."""
        from caravan_scout.errors import AppError
        if op not in ("start", "stop"):
            raise AppError(f"unknown server action {op!r}", 400)
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise AppError("port must be a number", 400)
        kind = next((k for k in self.kinds if k.id == kind_id), None)
        key = (kind_id, port, "")
        with self._lock:
            view = next((v for v in self._views or [] if v.get("kind") == kind_id and v.get("port") == port), None)
            if kind is None or view is None or self.servers is None:
                raise AppError(f"no {kind_id or 'engine'} on port {port} here", 404)
            if op not in (view.get("controls") or []):
                raise AppError(f"{view.get('label')} on port {port} cannot {op} from here", 409)
            if key in self._actions:
                doing = {"start": "started", "stop": "stopped"}[self._actions[key]["op"]]
                raise AppError(f"{view.get('label')} is being {doing} already", 409)
            self._actions[key] = {"op": op, "since": int(self.clock())}
            self._failures.pop(key, None)
        listener = self.listener_of(port)
        pid = int((listener or {}).get("pid") or 0) or None
        host = self.ask_host(listener)
        self.spawn(lambda: self._serve_run(kind, op, key, pid, host))
        return {"ok": True, "engines": self.views()}

    def _serve_run(self, kind, op, key, pid, host) -> None:
        kind_id, port, _none = key
        try:
            reason = self.servers.start(kind, port) if op == "start" else self.servers.stop(kind, pid)
            if not reason:
                reason = self._await(kind, port, host, answering=(op == "start"))
        except Exception as exc:  # noqa: BLE001 — a start that crashed failed; it must not stay "under way"
            reason = f"{type(exc).__name__}: {exc}"[:300]
        with self._lock:
            self._actions.pop(key, None)
            if reason:
                self._failures[key] = {"op": op, "error": reason, "at": int(self.clock())}
        print(f"[engines] {op} {kind_id}:{port}: {reason or 'done'}")
        try:
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            print(f"[engines] scan after {op} failed: {exc}")

    def _await(self, kind, port, host, answering: bool) -> str:
        """Wait until the engine answers on its port (a start) or stops
        answering (a stop): "" when it did, else what did not happen."""
        wait = self.START_WAIT if answering else self.STOP_WAIT
        deadline = self.clock() + wait
        while True:
            seen = kind.read(self.ask(port, host))
            up = bool(seen) and seen.get("state") in ("ok", "auth")
            if up == answering:
                return ""
            if self.clock() >= deadline:
                return (f"it did not answer on port {port} in {wait:.0f} s" if answering
                        else f"it still answers on port {port} after {wait:.0f} s")
            self.pause(1.0)

    def start_at_boot(self) -> list[str]:
        """The servers started from here and not stopped since, started when
        the machine has booted (EngineServers.due_at_boot). Returns the kinds
        asked to start."""
        if self.servers is None:
            return []
        started = []
        for kind_id in self.servers.due_at_boot(self.machine.boot_id()):
            view = next((v for v in self.views() or [] if v.get("kind") == kind_id), None)
            if view is None or "start" not in (view.get("controls") or []):
                print(f"[engines] {kind_id} is running already or cannot start here — not started at boot")
                continue
            try:
                self.serve("start", kind_id, view["port"])
                started.append(kind_id)
            except Exception as exc:  # noqa: BLE001 — one server's failure is not the others'
                print(f"[engines] {kind_id} did not start at boot: {exc}")
        return started

    def listener_of(self, port: int) -> dict[str, Any] | None:
        """Who listens on `port` now, as the OS says — to reach the engine at
        the address it is bound to; None when the OS will not say."""
        heard = self.machine.listeners()
        rows = heard.get("ports") if isinstance(heard, dict) and heard.get("ok") else []
        return next((r for r in rows or [] if isinstance(r, dict) and r.get("port") == port), None)

    def _run(self, kind, op, key, host) -> None:
        kind_id, port, model = key
        try:
            call = self.call(port, host)
            ask = self.ask(port, host)
            if op == "delete":
                reason = kind.delete(call, model)
            else:
                reason = kind.unload(call, ask, model)
        except Exception as exc:  # noqa: BLE001 — an act that crashed failed; it must not stay "under way"
            reason = f"{type(exc).__name__}: {exc}"[:300]
        with self._lock:
            self._actions.pop(key, None)
            if reason:
                self._failures[key] = {"op": op, "error": reason, "at": int(self.clock())}
        print(f"[engines] {op} {model} on {kind_id}:{port}: {reason or 'done'}")
        try:
            self.refresh()   # the loaded state now, not in ten seconds
        except Exception as exc:  # noqa: BLE001
            print(f"[engines] scan after {op} failed: {exc}")

    def refresh(self) -> list[dict[str, Any]]:
        views = self.scan()
        with self._lock:
            self._views = views
        return views

    def run(self, sleep: Callable[[float], None] = time.sleep) -> None:
        """The loop the scout runs it in (a daemon thread). After the first
        scan — when it is known what runs — the servers due at boot start."""
        first = True
        while True:
            try:
                self.refresh()
                if first:
                    first = False
                    self.start_at_boot()
            except Exception as exc:  # noqa: BLE001 — one bad scan must not end the watch
                print(f"[engines] scan failed: {exc}")
            sleep(self.PERIOD)

    def scan(self) -> list[dict[str, Any]]:
        heard = self.machine.listeners()
        # None: the OS would not say what listens — each kind's usual port is
        # asked blind then, and where it listens stays unknown.
        rows = heard.get("ports") if isinstance(heard, dict) and heard.get("ok") else None
        own = {int(port) for port, _cell in self.cells.all()}
        procs = self.machine.processes()
        found = []
        for kind in self.kinds:
            for port, listener in self.candidates(kind, rows, own):
                seen = kind.read(self.ask(port, self.ask_host(listener)))
                if seen is None:
                    continue
                by_name = bool(listener) and kind.is_its_process(listener.get("proc"))
                if seen["state"] == "auth" and not by_name:
                    continue
                if seen["state"] == "unreachable" and not (by_name and port == kind.default_port):
                    continue
                found.append(self.view(kind, port, listener, seen, procs))
        if self.servers is not None:
            found = self.servers.annotate(self.kinds, found, rows)
        return found

    def candidates(self, kind, rows, own) -> list[tuple[int, dict[str, Any] | None]]:
        """(port, its listener) worth asking as `kind`: its usual port and the
        ports its processes listen on; never a cell's."""
        if rows is None:
            return [] if kind.default_port in own else [(kind.default_port, None)]
        return [(int(r["port"]), r) for r in rows
                if isinstance(r, dict) and str(r.get("port", "")).isdigit() and int(r["port"]) not in own
                and (int(r["port"]) == kind.default_port or kind.is_its_process(r.get("proc")))]

    @classmethod
    def bare(cls, addr: Any) -> str:
        """An address as ss or lsof write it, without brackets or interface."""
        return str(addr or "").strip().strip("[]").split("%", 1)[0]

    @classmethod
    def loopback(cls, addr: Any) -> bool:
        bare = cls.bare(addr)
        if bare.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(bare).is_loopback
        except ValueError:
            return False

    @classmethod
    def scope(cls, listener: dict[str, Any] | None) -> str:
        """Where the engine takes connections from: "loopback" — this machine
        only, so the controller's proxy on another machine cannot reach it —
        or "network"; "" when the OS did not say."""
        addrs = (listener or {}).get("addrs") or []
        if not addrs:
            return ""
        return "loopback" if all(cls.loopback(a) for a in addrs) else "network"

    @classmethod
    def ask_host(cls, listener: dict[str, Any] | None) -> str:
        """The address to ask the engine at from this machine: loopback where
        it listens there or everywhere, else the one address it is bound to."""
        addrs = [str(a) for a in ((listener or {}).get("addrs") or [])]
        if not addrs or any(a in cls.WILDCARDS or cls.loopback(a) and cls.bare(a) != "::1" for a in addrs):
            return "127.0.0.1"
        if any(cls.bare(a) == "::1" for a in addrs):
            return "[::1]"
        bare = cls.bare(addrs[0])
        return f"[{bare}]" if ":" in bare else bare

    def view(self, kind, port, listener, seen, procs) -> dict[str, Any]:
        pids = self.pids(kind, listener, procs)
        scope = self.scope(listener)
        # A kind that did not answer says no models: None, not [] — an engine
        # that wants a token has models too.
        return {"kind": kind.id, "label": kind.label, "port": int(port), "listen": scope,
                "version": "", "models": None, **seen, "pids": sorted(pids),
                # What the board may do to it: unload a model (2.14), delete
                # one or download one (2.17). No load (2.18): a cell loads it.
                "controls": kind.controls(seen),
                # Who ufw lets reach its port (2.13), as a cell's port says it:
                # an engine open to the network is still closed to the
                # controller's proxy when no rule lets it in. Asked only when
                # it listens beyond this machine — on 127.0.0.1 no rule matters.
                "firewall": self.machine.firewall(port) if scope != "loopback" else None,
                # The memory its processes hold (RSS); None when ps will not say.
                "ramBytes": (None if procs is None else sum(int(procs[p]["rssKb"]) for p in pids) * 1024)}

    @staticmethod
    def pids(kind, listener, procs) -> set[int]:
        """The engine's processes: the one on its port, those of its name, and
        everything they started — Ollama's runners, LM Studio's helpers. The
        cards' memory is attributed through these (the controller joins them
        with computeApps). Two engines of one kind share what their name
        claims."""
        roots = {int(listener["pid"])} if listener and int(listener.get("pid") or 0) > 0 else set()
        if procs is None:
            return roots
        roots |= {pid for pid, p in procs.items() if kind.is_its_process(p.get("name"))}
        mine = set()
        for pid in procs:
            cur, hops = pid, 0
            while cur in procs and hops < 64:
                if cur in roots:
                    mine.add(pid)
                    break
                cur, hops = procs[cur]["ppid"], hops + 1
        return mine
