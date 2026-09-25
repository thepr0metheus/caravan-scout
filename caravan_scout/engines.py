"""Model engines on this machine that are not its cells: Ollama and LM Studio,
found by their ports and process names and read through their own HTTP APIs.

Read only. Nothing here loads, unloads or stops anything, and the one verb it
has is GET: an engine that someone runs by hand next to the caravan is theirs,
and a scout that changed it while looking would be a surprise on their
machine. Driving an engine from the board is a later step of its own
(docs/foreign-engines.md in the controller's repository).
"""
from __future__ import annotations

import ipaddress
import json
import threading
import time
import urllib.error
import urllib.request
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

    @staticmethod
    def row(name: str, **fields: Any) -> dict[str, Any]:
        """One model in the shape every kind reports: what a request names it
        by, what the engine says of its file, and — when it is loaded — what
        it holds and the window it serves. A field the engine does not say is
        None, never a zero."""
        return {"name": name, "type": "", "format": "", "family": "", "params": "", "quant": "",
                "fileBytes": None, "remote": False, "loaded": None, "memBytes": None, "vramBytes": None,
                "contextLength": None, "maxContextLength": None, "expiresAt": "", "instances": None,
                **fields}


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

    def read(self, ask):
        status, body = ask("/api/v1/models")
        if status is None:
            return {"state": "unreachable"}
        if status in (401, 403):
            return {"state": "auth"}
        if status == 200 and isinstance(body, dict) and isinstance(body.get("models"), list):
            return {"state": "ok", "version": "", "api": "v1",
                    "models": [self._v1(m) for m in body["models"] if isinstance(m, dict) and self.text(m.get("key"))]}
        status, body = ask("/api/v0/models")
        rows = body.get("data") if status == 200 and isinstance(body, dict) else None
        # Its own mark: every entry says whether it is loaded. A plain
        # OpenAI-style list of models has no `state`.
        if isinstance(rows, list) and rows and all(isinstance(m, dict) and "state" in m for m in rows):
            return {"state": "ok", "version": "", "api": "v0",
                    "models": [self._v0(m) for m in rows if self.text(m.get("id"))]}
        return None

    def _v1(self, m):
        instances = [i for i in (m.get("loaded_instances") or []) if isinstance(i, dict)]
        config = instances[0].get("config") if instances and isinstance(instances[0].get("config"), dict) else {}
        quant = m.get("quantization")
        return self.row(self.text(m.get("key"), 200), type=self.text(m.get("type"), 20), format=self.text(m.get("format"), 20),
                        family=self.text(m.get("architecture"), 40), params=self.text(m.get("params_string"), 20),
                        quant=self.text(quant.get("name") if isinstance(quant, dict) else quant, 20),
                        fileBytes=self.number(m.get("size_bytes")), loaded=bool(instances),
                        contextLength=self.number(config.get("context_length")) if instances else None,
                        maxContextLength=self.number(m.get("max_context_length") or config.get("max_context_length")),
                        instances=len(instances))

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

    def __init__(self, machine, cells, ask: Callable[..., Any] = EngineAsk):
        self.machine = machine
        self.cells = cells
        self.ask = ask
        self._views: list[dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def views(self) -> list[dict[str, Any]] | None:
        """The last scan: a list, or None before the first one."""
        with self._lock:
            return self._views

    def refresh(self) -> list[dict[str, Any]]:
        views = self.scan()
        with self._lock:
            self._views = views
        return views

    def run(self, sleep: Callable[[float], None] = time.sleep) -> None:
        """The loop the scout runs it in (a daemon thread)."""
        while True:
            try:
                self.refresh()
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
        for kind in self.KINDS:
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
        # A kind that did not answer says no models: None, not [] — an engine
        # that wants a token has models too.
        return {"kind": kind.id, "label": kind.label, "port": int(port), "listen": self.scope(listener),
                "version": "", "models": None, **seen, "pids": sorted(pids),
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
