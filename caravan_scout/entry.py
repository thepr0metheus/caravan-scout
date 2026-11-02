"""The machine's entry by OpenAI's API (2.27): one address for the models of this machine.

The owner, 2026-10-12 (visumap caravan-scout-openai, step 1): a model is called by its name, not by
the port of its cell. `GET /v1/models` lists the models of this machine — every cell the scout runs,
every one it keeps a start request for (autostart), and since 2.37 every cell the controller keeps
for the machine (KnownCells), a stopped one as stopped — and a request names one in `model`: the
scout hands it to a running cell of that model and passes the answer back as it comes, a stream as a
stream. Two cells of one model on one runner are two copies of it, and a request goes to the less busy
one; cells of one name on two runners are two models (2.39, MachineModels.groups).

The cells' own ports answer as before: the direct way stays as the spare one (the owner's decision 3).
The entry is open like the cells — no fleet token: everything it reaches is reachable on the cells'
ports already, and it starts, stops and changes nothing.

What a model does (`architecture`: text, images, sound) is not said here: that is the controller's
vocabulary (model_jobs), and a scout's guess at it would be a second copy of the rule.
"""
from __future__ import annotations

import http.client
import itertools
import json
import re
import threading
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.parse import unquote


class ModelName:
    """What a cell's model is called at the machine's entry: the name its operator gave it
    (`MODEL_NAME`, the owner's decision 2), else the cell's alias (`ALIAS` — what llama-server answers
    with), else one made of its model — a GGUF file's name without `.gguf` (and without the part a
    split file adds), a folder's own name; Whisper and Moonshine by their size or language. Nothing to
    go by — `cell-<port>`, a name that says it was not given one.

    Made of the cell's settings alone, so a model keeps its name while it is stopped: what the running
    server calls it is asked separately (ServedNames), only to hand it a request in its own words."""

    KEYS = ("MODEL_NAME", "ALIAS")
    #: What a split GGUF adds to each file's name: model-00001-of-00003.gguf is the model `model`.
    SPLIT = re.compile(r"-\d{5}-of-\d{5}$")

    @classmethod
    def of(cls, config: Any, model_path: str = "", port: int = 0) -> str:
        config = config if isinstance(config, dict) else {}
        for key in cls.KEYS:
            said = str(config.get(key) or "").strip()
            if said:
                return said
        runner = str(config.get("RUNNER") or "").strip().lower()
        for kind, key in (("whisper", "WHISPER_MODEL"), ("moonshine", "MOONSHINE_MODEL")):
            size = str(config.get(key) or "").strip()
            if runner == kind and size:
                return f"{kind}-{size}"
        made = cls.from_path(str(config.get("MODEL_FILE") or "").strip() or str(model_path or "").strip())
        return made or (f"cell-{int(port)}" if port else "")

    #: A folder of HF's cache: `models--<owner>--<repository>` is how HF lays its files out, not the model's name.
    HF_CACHE = re.compile(r"^models--[^/]+?--(.+)$")

    @classmethod
    def from_path(cls, path: str) -> str:
        """A model's name from where it lies: a GGUF file — its name without `.gguf`; a folder of HF's cache
        (`whisper/models--Systran--faster-whisper-large-v3-turbo`) — its repository's name (2.37: it was named
        after the runner's folder, or after the cache's whole folder name); a folder named under the models'
        root (`seamless-m4t-v2-large/facebook/FP32`) — its first part; an absolute folder — the part after
        `models`, else its last part."""
        path = path.strip().rstrip("/")
        if not path:
            return ""
        parts = PurePosixPath(path).parts
        if path.lower().endswith(".gguf"):
            return cls.SPLIT.sub("", PurePosixPath(path).name[:-5])
        for part in parts:
            cached = cls.HF_CACHE.match(part)
            if cached:
                return cached.group(1)
        if not path.startswith("/"):
            return parts[0]
        if "models" in parts[:-1]:
            return parts[parts.index("models") + 1]
        return parts[-1]


class KnownCells:
    """The cells the controller keeps for this machine (2.37), as its answer to a heartbeat says them: each
    configured cell's port, where its model lies, and the settings its model's name is made of — never the rest
    of a cell's settings. A cell this scout has not started since its own start is then a model of its machine
    too, stopped: on 2026-10-25 the entry named 8 of the 17 ports the controller's pools led to.

    Kept in state.json (`knownCells`) from the last answer that said them: a beat that fails, or a restart, does
    not make the machine's cells unknown, and an answer that does not say them (a controller older than 1.3.629)
    leaves the list as it was. None until a controller has said them: the list of models is then what it was
    before 2.37 — not knowing the cells is not "no cells". What the network says is checked: a port that is no
    port, a field that is no text is left out. Unpairing forgets them with the controller."""

    KEY = "knownCells"
    MAX = 500
    FIELDS = ModelName.KEYS + ("RUNNER", "WHISPER_MODEL", "MOONSHINE_MODEL", "MODEL_FILE")

    def __init__(self, state):
        self.state = state

    @classmethod
    def clean(cls, raw) -> dict[int, dict[str, Any]] | None:
        """{port: {config, modelPath}} of what a controller said, or None when it said no list."""
        if not isinstance(raw, list):
            return None
        out: dict[int, dict[str, Any]] = {}
        for item in raw[:cls.MAX]:
            port = item.get("port") if isinstance(item, dict) else None
            if not (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536):
                continue
            config = item.get("config") if isinstance(item.get("config"), dict) else {}
            named = {key: str(value)[:400] for key, value in config.items()
                     if key in cls.FIELDS and isinstance(value, (str, int)) and not isinstance(value, bool)}
            path = item.get("modelPath")
            out[port] = {"config": named, "modelPath": str(path)[:1000] if isinstance(path, str) else ""}
        return out

    def take(self, answer) -> None:
        """Keep the cells a controller's answer to a heartbeat says; an answer that says none changes nothing."""
        cells = self.clean(answer.get("cells") if isinstance(answer, dict) else None)
        if cells is None:
            return
        with self.state.lock:
            self.state[self.KEY] = [{"port": port, **cell} for port, cell in sorted(cells.items())]
            self.state.save()

    def forget(self) -> None:
        with self.state.lock:
            if self.state.pop(self.KEY, None) is not None:
                self.state.save()

    def ports(self) -> dict[int, dict[str, Any]] | None:
        with self.state.lock:
            raw = self.state.get(self.KEY)
        return self.clean(raw)

    def name(self, port: int) -> str:
        """What the controller's settings call the cell's model now, "" when it does not keep the cell."""
        cell = (self.ports() or {}).get(port)
        return ModelName.of(cell["config"], cell["modelPath"], port) if cell else ""


class MachineModels:
    """The models of this machine, as `GET /v1/models` lists them: each cell the scout runs, starts or
    keeps a start request for, and each one the controller keeps for the machine (2.37, KnownCells),
    grouped by its model's name and its runner — two cells of one model on one runner are its copies
    (`groups`, 2.39).

    A cell's state is its process's word and its start's phase: running, starting (being fetched or
    loaded), failed, stopped. A cell the table holds with nothing behind it — no process, no start, no
    record, no kept request, not one the controller keeps: a port only asked about — is no model, and is
    left out.

    A running copy also says its places (2.29): how many requests its server takes at once (`slots`,
    /props) and how many it works on now (`busy`, /metrics — the count Copies picks by). A copy whose
    server does not say has None: a model's sum is known only when every running copy says its part.
    The controller queues a model's requests by them, as it queues a cell's by its /slots."""

    STATES = ("running", "starting", "failed", "stopped")
    #: The runner of a cell whose settings name none — the controller's default too (lama-caravan runner_id).
    DEFAULT_RUNNER = "llama-server"

    def __init__(self, cells, autostart, host_id: Callable[[], str], known: KnownCells | None = None):
        self.cells = cells
        self.autostart = autostart
        self.host_id = host_id
        self.known = known

    @classmethod
    def runner(cls, config) -> str:
        """The runner a cell's settings name (`RUNNER`), in lower case; settings that name none — the default."""
        said = str(config.get("RUNNER") or "").strip().lower() if isinstance(config, dict) else ""
        return said or cls.DEFAULT_RUNNER

    def state(self, cell) -> str:
        status = cell.process.status()
        with cell.lock:
            phase = str((cell.startup or {}).get("phase") or "")
        if status.get("running"):
            return "running"
        if phase in ("resolving", "downloading", "loading"):
            return "starting"
        if phase == "error" or status.get("crashed"):
            return "failed"
        return "stopped"

    def cells_now(self) -> list[dict[str, Any]]:
        """Each cell: its port, its model's name, its runner, its state and when it started — sorted by port.

        The name, from the freshest word first: the start under way or last made here (Cell.name), the
        record of a cell running since before this scout started, the kept start request, and — for a
        cell started before names were kept (2.26 and older) — its model's path in the record. A stopped
        cell the controller keeps (2.37) is called what its settings call it now: they are what its next
        start runs, and an operator who renamed a stopped model finds it under the new name at once. A
        running, starting or failed one keeps the name it was started under, the one its server answers to.

        The runner (2.39): what the freshest settings the scout has say — the controller's, when it keeps the cell
        (its next start runs them), else the kept start request's, else the record's (a prism start keeps its runner
        there). Settings that name no runner say llama-server: an absent RUNNER of the controller's is its default,
        not a word to look for elsewhere."""
        kept = self.autostart.requests()
        known = (self.known.ports() if self.known else None) or {}
        found: dict[int, dict[str, Any]] = {}
        for port, cell in self.cells.all():
            record = self.cells.records.get(port)
            request = kept.get(str(port)) or {}
            state = self.state(cell)
            told = known.get(port)
            if state == "stopped" and not record and not request and not told:
                continue
            if state == "stopped" and told:
                name = ModelName.of(told["config"], told["modelPath"], port)
            else:
                name = cell.name or str(record.get("name") or "") or ModelName.of(
                    request.get("config"), request.get("modelPath") or (record.get("cfg") or {}).get("modelPath") or "", port)
            runner = self.runner(told["config"] if told else request.get("config") if request
                                 else {"RUNNER": (record.get("cfg") or {}).get("runner")})
            found[port] = {"port": port, "name": name, "runner": runner, "state": state, "since": record.get("startedAt"),
                           **self.places(port, state)}
        for key, request in kept.items():
            port = int(key)
            if port not in found:
                told = known.get(port)
                name = (ModelName.of(told["config"], told["modelPath"], port) if told
                        else ModelName.of(request.get("config"), request.get("modelPath"), port))
                found[port] = {"port": port, "name": name, "runner": self.runner(told["config"] if told else request.get("config")),
                               "state": "stopped", "since": None, "slots": None, "busy": None}
        for port, told in known.items():
            if port not in found:
                found[port] = {"port": port, "name": ModelName.of(told["config"], told["modelPath"], port),
                               "runner": self.runner(told["config"]), "state": "stopped", "since": None, "slots": None, "busy": None}
        return [found[port] for port in sorted(found)]

    def places(self, port, state) -> dict[str, Any]:
        """A running copy's places — {slots, busy}, each None when its server does not say; none
        asked of a copy that does not run."""
        if state != "running":
            return {"slots": None, "busy": None}
        busy = self.cells.probe.metrics(port).get("requestsProcessing")
        return {"slots": self.cells.probe.slots_total(port),
                "busy": busy if isinstance(busy, int) and not isinstance(busy, bool) and busy >= 0 else None}

    @staticmethod
    def summed(copies, key):
        """The running copies' sum of `key`: 0 when none runs, None when one of them does not say."""
        values = [c[key] for c in copies if c["state"] == "running"]
        return sum(values) if all(isinstance(v, int) for v in values) else None

    def groups(self) -> dict[str, list[dict[str, Any]]]:
        """The models by the id the entry lists them under, each with its copies, in the order of their first ports.

        The id is the model's name. Cells of one name on two runners are two models (2.39), each called by the name and
        its runner (`minicpm-o-4_5-q4_k_m@minicpm-omni`), as LM Studio tells a model's variants by `@`: a request for one
        of them must not land on a server of another kind, and which of the two the bare name meant nobody said. Until
        2.39 they were copies of one model (a llama.cpp chat on :22011 and MiniCPM-o's voice on :22024, 2026-10-26),
        and a chat request could go to the voice. Which runner owns the bare name is not decided by the order of the
        ports either: a cell moved to another port would have taken the name away."""
        by_name: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for cell in self.cells_now():
            by_name.setdefault(cell["name"], {}).setdefault(cell["runner"], []).append(cell)
        out: dict[str, list[dict[str, Any]]] = {}
        for name, runners in by_name.items():
            for runner, copies in runners.items():
                out[name if len(runners) == 1 else f"{name}@{runner}"] = copies
        return out

    def listing(self) -> list[dict[str, Any]]:
        """The models in OpenAI's shape, each with `caravan`: its state — the best of its copies' —, its
        places (`slots`, `busy`: the running copies' sums, 0 when none runs, None when one does not say),
        and each copy's port, state and places. `created` is when its first copy started; 0 for one
        never started."""
        out = []
        for name, copies in self.groups().items():
            best = min((c["state"] for c in copies), key=self.STATES.index)
            started = [int(c["since"]) for c in copies if isinstance(c.get("since"), (int, float)) and c["since"] > 0]
            out.append({"id": name, "object": "model", "created": min(started) if started else 0,
                        "owned_by": self.host_id(),
                        "caravan": {"state": best, "slots": self.summed(copies, "slots"), "busy": self.summed(copies, "busy"),
                                    "copies": [{"port": c["port"], "state": c["state"], "slots": c["slots"], "busy": c["busy"]}
                                               for c in copies]}})
        return out

    def copies(self, name: str) -> list[dict[str, Any]]:
        """The copies of the model the entry lists as `name`; none for a name it does not list."""
        return self.groups().get(name, [])


class Copies:
    """Which copy of a model takes a request: the running copy with the fewest requests in work, by its
    server's own count (/metrics, kept ~2 s); a count it does not say is taken as none. A tie goes to
    the next copy in turn, so equal copies share the work. One copy is not asked about at all."""

    def __init__(self, load: Callable[[int], Any]):
        self.load = load
        self._turn = itertools.count()
        self._lock = threading.Lock()

    def pick(self, ports: list[int]) -> int | None:
        if len(ports) < 2:
            return ports[0] if ports else None
        with self._lock:
            start = next(self._turn) % len(ports)
        ordered = ports[start:] + ports[:start]

        def busy(port):
            value = self.load(port)
            return int(value) if isinstance(value, (int, float)) and value > 0 else 0
        return min(ordered, key=busy)


class EntryBody:
    """The body of a request to the machine's entry, read whole before it goes on: by its length, or
    chunk by chunk when the client sends it so (Transfer-Encoding: chunked — curl uploading a pipe, a
    client streaming a file). Never past `limit`: the scout holds it in memory."""

    class TooLong(Exception):
        """The body is over the limit: refused, not read further."""

    class Malformed(Exception):
        """The body cannot be read as it says it is sent."""

    LINE = 1024

    def __init__(self, limit: int) -> None:
        self.limit = limit

    def read(self, headers, rfile) -> bytes:
        if "chunked" in str(headers.get("Transfer-Encoding") or "").lower():
            return self.chunks(rfile)
        try:
            length = int(headers.get("Content-Length") or 0)
        except ValueError:
            raise self.Malformed("Content-Length is not a number") from None
        if length < 0:
            raise self.Malformed("Content-Length is negative")
        if length > self.limit:
            raise self.TooLong()
        return rfile.read(length) if length else b""

    def chunks(self, rfile) -> bytes:
        parts: list[bytes] = []
        size = 0
        while True:
            line = rfile.readline(self.LINE)
            try:
                count = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise self.Malformed("a chunk's size is not a number") from None
            if count < 0:
                raise self.Malformed("a chunk's size is negative")
            if count == 0:
                while rfile.readline(self.LINE).strip():   # the trailers, up to the blank line
                    pass
                return b"".join(parts)
            size += count
            if size > self.limit:
                raise self.TooLong()
            data = rfile.read(count)
            if len(data) < count:
                raise self.Malformed("the body ended inside a chunk")
            parts.append(data)
            rfile.readline(self.LINE)   # the line end after the chunk


class EntryRequest:
    """A request to the machine's entry, read once: the model it names — `model` of a JSON body, or the
    form field `model` of a multipart one (a recording) — and the same request naming another model."""

    BOUNDARY = re.compile(r'boundary="?([^";]+)"?', re.I)
    #: The form field `model` by its own name: a file's `filename="model"` is not it.
    FIELD = re.compile(rb'(?<![\w-])name="model"(?=;|\s|$)')

    def __init__(self, body: bytes, content_type: str) -> None:
        self.body = body
        self.content_type = str(content_type or "")
        self.form = self.content_type.lower().startswith("multipart/form-data")
        self.payload = None if self.form else self.json_object()
        self.span = self.field_span() if self.form else None

    def json_object(self) -> dict[str, Any] | None:
        """The body as a JSON object; any other body (no body, not JSON, a list) — None."""
        try:
            payload = json.loads(self.body.decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def field_span(self) -> tuple[int, int] | None:
        """Where the value of the form field `model` lies in the body, (start, end); no such field — None."""
        match = self.BOUNDARY.search(self.content_type)
        if not match:
            return None
        delimiter = b"--" + match.group(1).encode("latin-1")
        at = self.body.find(delimiter)
        while at != -1:
            head = at + len(delimiter)
            end = self.body.find(b"\r\n" + delimiter, head)
            if end == -1:
                return None
            split = self.body.find(b"\r\n\r\n", head, end)
            if split != -1 and self.FIELD.search(self.body[head:split]):
                return split + 4, end
            at = end + 2
        return None

    @property
    def model(self) -> str:
        if self.payload is not None:
            return str(self.payload.get("model") or "").strip()
        if self.span is not None:
            return self.body[self.span[0]:self.span[1]].decode("utf-8", "replace").strip()
        return ""

    def naming(self, name: str) -> bytes:
        """The body with `model` set to `name`: the same bytes when it already says so, or names no
        model to change. A JSON body is written again (its other fields as they were), a form's field
        is replaced in place — a recording's bytes are not touched."""
        if not name or not self.model or name == self.model:
            return self.body
        if self.payload is not None:
            return json.dumps({**self.payload, "model": name}).encode("utf-8")
        start, end = self.span
        return self.body[:start] + name.encode("utf-8") + self.body[end:]


class ServedNames:
    """The names a running cell's server answers to: its own `GET /v1/models` — each model's `id`, and
    the `aliases` llama-server lists — asked once per start of the cell (`since`, when its record says
    it started). vLLM refuses a request for a name it does not serve; llama-server and the caravan's own
    cells take any. A server that answers without a list (404 — a cell of the caravan's own) names
    nothing: an empty tuple, kept. One that does not answer, or fails (503 while it loads), is not
    known: None, not kept — asked again next time."""

    TIMEOUT = 2

    def __init__(self, connect: Callable[[int, float], Any]):
        self.connect = connect
        self._kept: dict[int, tuple[Any, tuple[str, ...]]] = {}

    def of(self, port: int, since: Any) -> tuple[str, ...] | None:
        hit = self._kept.get(int(port))
        if hit is not None and hit[0] == since:
            return hit[1]
        try:
            conn = self.connect(port, self.TIMEOUT)
            try:
                conn.request("GET", "/v1/models")
                answer = conn.getresponse()
                status, raw = answer.status, answer.read()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — a server that does not answer names nothing we know
            return None
        if status >= 500:
            return None
        names = self.parse(raw) if status == 200 else ()
        self._kept[int(port)] = (since, names)
        return names

    @staticmethod
    def parse(raw: bytes) -> tuple[str, ...]:
        """`{data: [{id, aliases?}]}` → the ids, then the aliases, each once, in the order the server says them."""
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "null")
        except ValueError:
            return ()
        items = payload.get("data") if isinstance(payload, dict) else None
        names: list[str] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
            for name in [item.get("id"), *aliases]:
                if isinstance(name, str) and name.strip() and name.strip() not in names:
                    names.append(name.strip())
        return tuple(names)


class MachineEntry:
    """`GET /v1/models`, `GET /v1/models/<name>` and a request by `model` to a cell of this machine.

    Refusals speak OpenAI's shape — `{"error": {message, type, param, code}}`: no `model` — 400; a name
    no cell has — 404 `model_not_found`; a model with no running copy — 503 `model_not_running`, its
    copies' states in the message; a cell that does not answer — 502 `cell_unreachable`.

    The request goes to the cell as it came, but for what belongs to one hop (Connection, Host…) and
    the client's `Authorization` — the cells hold no keys, and a key is not theirs to see — and for
    `model`, given in the cell's own words when the cell answers to other names (ServedNames). The
    answer comes back as it came: with its length when it has one, else streamed until the cell ends
    it. Past its headers nothing can be refused any more: a cell that breaks off ends the answer where
    it got to."""

    #: Not passed on, either way: one hop's own (and what its Connection header names), the length —
    #: said again for the body that goes — and the client's key.
    HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
                     "transfer-encoding", "upgrade", "host", "content-length", "authorization"})
    #: Not passed on to the cell: the body is here whole, and nothing waits for a 100.
    TO_CELL = frozenset({"expect"})
    #: Not passed back: the scout's own server writes them.
    TO_CLIENT = frozenset({"date", "server"})
    #: Seconds a cell may stay silent mid-answer: a long generation or a recording's transcription.
    TIMEOUT = 600
    CHUNK = 65536

    def __init__(self, models: MachineModels, copies: Copies, connect: Callable[[int, float], Any] | None = None):
        self.models = models
        self.copies = copies
        self.connect = connect or (lambda port, timeout: http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout))
        self.names = ServedNames(self.connect)

    @staticmethod
    def refusal(message: str, kind: str, code: str | None, param: str | None = "model") -> dict[str, Any]:
        return {"error": {"message": message, "type": kind, "param": param, "code": code}}

    @classmethod
    def passed(cls, pairs, also: frozenset = frozenset()) -> list[tuple[str, str]]:
        """The headers that go on: all but one hop's own — HOP, and those its Connection header names — and `also`."""
        pairs = list(pairs)
        named = {token.strip().lower() for key, value in pairs if key.lower() == "connection" for token in value.split(",")}
        return [(key, value) for key, value in pairs if key.lower() not in cls.HOP | named | also]

    def listing(self) -> dict[str, Any]:
        return {"object": "list", "data": self.models.listing()}

    def get(self, path: str) -> tuple[dict[str, Any], int]:
        """A GET under /v1: the list, or one model by its name."""
        path = path.split("?", 1)[0].rstrip("/")
        if path == "/v1/models":
            return self.listing(), 200
        if path.startswith("/v1/models/"):
            name = unquote(path[len("/v1/models/"):])
            found = [m for m in self.models.listing() if m["id"] == name]
            if found:
                return found[0], 200
            return self.refusal(f"The model `{name}` does not exist on this machine", "invalid_request_error",
                                "model_not_found"), 404
        return self.refusal(f"No such path on this machine's entry: {path}", "invalid_request_error", None, None), 404

    def forward(self, handler, method: str, path: str, body: bytes) -> None:
        """Hand the request to a running copy of its model and relay the answer — or refuse."""
        request = EntryRequest(body, handler.headers.get("Content-Type", ""))
        name = request.model
        if not name:
            handler.send_json(self.refusal("You must provide a model parameter", "invalid_request_error", None), 400)
            return
        copies = self.models.copies(name)
        if not copies:
            handler.send_json(self.refusal(f"The model `{name}` does not exist on this machine", "invalid_request_error",
                                           "model_not_found"), 404)
            return
        running = {c["port"]: c for c in copies if c["state"] == "running"}
        port = self.copies.pick(list(running))
        if port is None:
            states = ", ".join(f":{c['port']} {c['state']}" for c in copies)
            handler.send_json(self.refusal(f"The model `{name}` is not running on this machine ({states})", "server_error",
                                           "model_not_running"), 503)
            return
        served = self.names.of(port, running[port]["since"])
        send = request.naming(served[0]) if served and name not in served else body
        headers = dict(self.passed(handler.headers.items(), self.TO_CELL))
        headers["Content-Length"] = str(len(send))
        conn = None
        try:
            conn = self.connect(port, self.TIMEOUT)
            conn.request(method, path, body=send, headers=headers)
            answer = conn.getresponse()
        except Exception as exc:  # noqa: BLE001 — said to the client in OpenAI's shape
            if conn is not None:
                conn.close()
            handler.send_json(self.refusal(f"The cell of `{name}` on :{port} did not answer: {exc}", "server_error",
                                           "cell_unreachable"), 502)
            return
        try:
            self.relay(handler, answer)
        finally:
            conn.close()

    def relay(self, handler, answer) -> None:
        """The cell's answer to the client as it comes: its status and headers, then its body — with its
        length when it said one, else chunk by chunk as the cell writes it (a stream), the connection
        closed at its end. A cell that breaks off, or a client that leaves, ends it where it got to."""
        handler.send_response(answer.status)
        for key, value in self.passed(answer.getheaders(), self.TO_CLIENT):
            handler.send_header(key, value)
        length = answer.getheader("Content-Length")
        if length is not None and not answer.getheader("Transfer-Encoding"):
            handler.send_header("Content-Length", length)
        else:
            handler.send_header("Connection", "close")
            handler.close_connection = True
        handler.end_headers()
        try:
            while True:
                chunk = answer.read1(self.CHUNK)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
        except (OSError, http.client.HTTPException):
            handler.close_connection = True
