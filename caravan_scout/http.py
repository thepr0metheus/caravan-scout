"""The scout's HTTP surface on :8092: which path does what, and who may ask."""
from __future__ import annotations

import hmac
import json
import subprocess
import time
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable
from urllib.parse import parse_qs

from caravan_scout import __version__ as APP_VERSION
from caravan_scout.api_spec import ApiSpec
from caravan_scout.errors import AppError
from caravan_scout.routes import Route
from caravan_scout.webui import PairingPage


class Power:
    """Reboot or power off this machine when the controller asks.

    Cells are not stopped first — systemd takes them down with the machine
    and autostart brings back what should come back.

    poweroff is the one-way door: nothing on the board can switch this box
    on again, so it is separated from reboot by its own path rather than a
    flag in a body. A path cannot be reached by accident the way a mistyped
    field can, and an old scout answers 404 to it instead of silently doing
    the wrong one of the two.
    """

    def issue(self, action: str) -> tuple[dict[str, Any], int]:
        print(f"[host] {action} requested by the controller")
        try:
            r = subprocess.run(["sudo", "-n", "systemctl", action],
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
                hint = (f" — passwordless sudo for `systemctl {action}` is required"
                        if "password" in err.lower() else "")
                return {"ok": False, "error": f"{action} refused: {err}{hint}"}, 500
        except subprocess.TimeoutExpired:
            pass   # the box is already going down; that is success
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{action} failed: {exc}"}, 500
        return {"ok": True, "detail": f"{action} issued"}, 200


class Api:
    """What each path does, as one list of rows (routes.py): one row per path.

    The handler's tables are cut from that list, and /openapi.json and the
    endpoint tables of docs/http-api.md are written from it, so the scout, its
    description and its documentation are the same list.

    The scout's page, /api/pairing, /api/health and /openapi.json are open. Everything else
    stands behind the fleet token when config.json has one: the controller
    sends it as X-Caravan-Token, the pairing form may put it in the body. No
    token configured means open (a trusted LAN). The gate stands BEFORE the
    routes, so an unknown path without a token is 401, not 404 — as it was.

    A path matches whole, query string and all. A POST route gets the body
    as a function and reads it only if it needs one: a body that does not
    parse fails only the routes that read it.
    """

    def __init__(self, scout):
        s = self.scout = scout
        self.power = Power()
        self.routes: list[Route] = self.table(s)
        self.spec = ApiSpec(self.routes, APP_VERSION)
        # The handler's own tables, one per way a path is answered, all cut from
        # the one list above. A path with no handler here (the pairing form's) is
        # answered by ScoutHandler itself.
        by_kind = {kind: {r.path: r.handler for r in self.routes if r.kind == kind and r.handler is not None}
                   for kind in (Route.PAGE, Route.PUBLIC, Route.GET, Route.QUERY, Route.POST)}
        self.pages: dict[str, Callable[[], bytes]] = by_kind[Route.PAGE]
        self.open_get: dict[str, Callable[[], Any]] = by_kind[Route.PUBLIC]
        self.get: dict[str, Callable[[], Any]] = by_kind[Route.GET]
        # Asked with a query: path -> fn(the query's first values).
        self.get_query: dict[str, Callable[[dict[str, str]], Any]] = by_kind[Route.QUERY]
        # path -> fn(read_body) -> (payload, status)
        self.post: dict[str, Callable[[Callable[[], dict]], tuple[Any, int]]] = by_kind[Route.POST]

    #: An engine's server itself (2.16), for both of its routes.
    ENGINE_SERVER = ("Only what its view's `controls` offer. Answered at once with `{ok, engines}`, the engine "
                     "carrying `serverAction`; the start waits up to 60 s for the server to answer, the stop up "
                     "to 20 s for it to go quiet, then the engines are scanned again; what failed stays as "
                     "`serverError`. Ollama: `ollama serve` with the binary and the `OLLAMA_*` / device variables "
                     "of the run it was learned from, in a session of its own (a scout restart leaves it "
                     "running), output in `engine-logs/ollama.log` next to the state; stopped with SIGTERM, and "
                     "SIGKILL for it and its children after 10 s. LM Studio: `lms daemon up` + `lms server start "
                     "-p <port> --bind <address>`; stopped with `lms daemon down` (`lms server stop` where no "
                     "daemon runs). A server started here starts again when the machine boots (once per boot), "
                     "until it is stopped here.")

    #: The power of the machine, for both of its routes.
    POWER = ("`sudo -n systemctl reboot` or `poweroff`; needs passwordless sudo for it and says so when it is "
             "missing. Two paths, not a flag: poweroff cannot be undone from the board, and an old scout answers "
             "404 to a path it does not have instead of doing the wrong one of the two.")

    def table(self, s) -> list[Route]:
        """The whole surface, one row per path: what answers it, what it is for, who may ask."""
        power = self.power
        rows = [
            # The page and the paths that stay open with a token.
            *[Route("GET", path, lambda: PairingPage.body(), "The scout's page: this machine's address for the controller's board",
                    "HTML, read-only since 2.1: the machine, whether a controller has paired it, and the address:port "
                    "to enter on the controller's board.", tag="meta", page=True) for path in ("/", "/index.html")],
            # Open like the page itself: see Report.pairing().
            Route("GET", "/api/pairing", lambda: s.report.pairing(),
                  "What the page shows: this machine and whether a controller has paired it",
                  "Host id, name and IP, the scout's port, platform, GPU names, cells running and total, "
                  "`controllerUrl`, `tokenRequired` and the heartbeat `{state, lastAt, error}` (`state` is `unpaired` "
                  "until a controller adds the scout) — never the controller's reply. The controller reads it first "
                  "when the operator adds the scout.", tag="pairing", public=True),
            Route("GET", "/api/health", lambda: {"ok": True, "service": "caravan-scout", "version": APP_VERSION,
                                                 "tokenRequired": bool(s.config.token()), "time": int(time.time())},
                  "Liveness: service, version, whether a fleet token is required, time",
                  "`{ok, service, version, tokenRequired, time}`.", tag="meta", public=True),
            Route("GET", "/openapi.json", lambda: self.spec.document(), "This description of the API, as OpenAPI 3.1",
                  "Written from the table the scout answers by, so it cannot drift from what the scout does; the "
                  "endpoint tables of docs/http-api.md are written from the same table. `info.version` is the "
                  "version /api/health names.", tag="meta", public=True),
            Route("GET", "/api/state", lambda: s.report.public(),
                  "The machine as a whole: hardware, cells, engines, llama.cpp build, driver",
                  "The machine: host identity/IP, GPUs, CPU/RAM, compute apps (`[{gpuUuid, pid, name, usedMiB}]` — "
                  "`name` is the process's executable, \"\" when nvidia-smi cannot name it, 2.12+), `engines` — the "
                  "model engines on this machine that are not its cells (2.12+, below), heartbeat status, llama.cpp "
                  "build and update status (`llamaUpdate`), the scout's own version (`scoutVersion`), per-cell "
                  "`llamaNodes` (a running cell's `promptTps`, `genTps` and `requestsProcessing` from its /metrics — "
                  "llama.cpp/Prism speeds from gauges or token/compute-time counters, retained while idle (2.23.2+); "
                  "a vLLM cell's also `requestsWaiting`, and its rates from its token counters, 2.7+; `listening` — "
                  "whether a running cell's port listens on this machine yet, and while it does not, "
                  "`startingTail`, the last lines of its log, 2.7+; `launchDiskNewer` — the roles (`model`, "
                  "`mmproj`, `draft`) of the files a running cell holds that changed on disk after it started, so a "
                  "restart would pick them up (a folder by its newest file), 2.11+; a cell that crashed since it "
                  "was last started by hand carries `crash: {count, at, reason, tail?, gaveUp?}`, 2.5+; `tail` — "
                  "the last 8 lines of the crashed run's log, keys scrubbed, 2.6+), `autostart` — the ports that "
                  "start with the machine, stopped ones too (2.4+), `driver` — the NVIDIA driver as the next boot "
                  "will meet it (2.19+, below), and `llamaSuspect` — `{suspect: false}` or the incident of cells "
                  "crashing after a fresh llama.cpp build: `crashes15m`, `builtAt`, `currentCommit`, `firstSeenAt`, "
                  "`lastSeenAt`, `restoreCandidate` (the archived build to offer, or null) (2.6+). The heartbeat "
                  "pushes the same facts under the same names.", tag="machine"),
            Route("GET", "/api/llama-node/status", lambda: {"ok": True, "nodes": s.cells.views()},
                  "Every server cell of this machine with its state", "`{ok, nodes: [...]}`.", tag="cells"),
            Route("GET", "/api/monitor/nvidia-smi", lambda: s.machine.nvidia_smi(),
                  "A raw nvidia-smi snapshot for the controller's monitor drawer", tag="machine"),
            # What is listening on this box, so the controller's port picker
            # stops offering numbers something else already owns. The
            # controller can only see its OWN host; a client squatter was
            # invisible until now.
            Route("GET", "/api/host/listeners", lambda: s.machine.listeners(),
                  "TCP ports listening on this machine, and what owns each",
                  "`{ok, ports: [{port, proc, pid, addrs}]}` — the controller's port picker. The owning process "
                  "where the OS says; `addrs` (2.12+): the addresses the port is bound on (`127.0.0.1` only — this "
                  "machine alone reaches it). `ss` on Linux, `lsof` where there is none (macOS, 2.12+); a tool that "
                  "fails is `{ok: false, error}`, not an empty machine.", tag="machine"),
            Route("GET", "/api/llama-node/configs", lambda: {"ok": True, "configs": s.configs.listing()},
                  "Launch configs saved on this machine", "Stored in `llama-node-configs/`.", tag="cells"),
            Route("GET", "/api/llama-node/update-status", lambda: s.builds.status(),
                  "The llama.cpp update or restore job: running, result, last 200 lines",
                  "Running or done, the return code, the last 200 lines.", tag="llama.cpp"),
            Route("GET", "/api/llama-node/builds", lambda: s.builds.archive(),
                  "Archived llama.cpp builds on this machine, newest first", tag="llama.cpp"),
            Route("GET", "/api/llama-node/list-cache", lambda: {"ok": True, "models": s.models.listing()},
                  "What the local model cache holds", tag="cells"),
            # vLLM in this machine's venv: version, history, the install job.
            Route("GET", "/api/vllm", lambda: s.vllm.info(),
                  "vLLM in this machine's venv: version, history, the install job",
                  "(2.9+) In `~/vllm-venv`: `{ok, installed, version, venv, history: [{version, seenAt}], job}` — the "
                  "version read from its dist-info folder, the versions the venv has had (newest first, five kept: "
                  "the rollback candidates) and the install job, briefly.", tag="vllm"),
            Route("GET", "/api/vllm/update-status", lambda: s.vllm.job.status(),
                  "The vLLM install job: running, result, last 200 lines", "(2.9+)", tag="vllm"),
            # What is new since `since` for the board's charts; asking marks
            # the machine watched (Telemetry).
            Route("GET", "/api/telemetry", lambda q: s.telemetry.since(q.get("since")),
                  "The machine second by second: samples newer than a moment, for the board's charts",
                  "(2.8+) Samples newer than `since` (epoch seconds; 0 or missing returns all that are kept) — "
                  "`{t, gpus: [{index, memUsedMiB, memTotalMiB, utilPct, powerW, tempC}], cpuPct, ram}`. Ten minutes "
                  "are kept, one sample a second while the machine is watched and one in ten seconds otherwise; "
                  "each ask marks the machine watched for 30 s.", tag="machine", query=("since",)),
            # The pairing form may carry the token in the body, so ScoutHandler reads the body
            # before the gate on this one path — and a token the scout does not hold yet may pass
            # it: see Heartbeat.takes_new_token.
            Route("POST", "/api/controller-url", None,
                  "Pair this machine with a controller: its address and the fleet token",
                  "The controller calls it when the operator adds the scout on its board: `{url, token?}` → "
                  "validates (`http[s]://`, scheme optional in the form), writes `controllerUrl` into `config.json` "
                  "(atomic, preserves the rest of the file), updates the running scout and fires one heartbeat right "
                  "away. Returns `{ok, controllerUrl, heartbeat}`; `heartbeat.state` is `error` if the controller "
                  "did not answer (the URL is still saved). The fleet token may come in the body (`token`) instead "
                  "of the header, since the pairing form has no header. A token regenerated on the controller "
                  "passes without the one the scout holds when the address is the same controller's and that "
                  "controller accepts a heartbeat carrying it; a different address still needs the token the "
                  "scout holds.", tag="pairing", body=("url", "token")),
            Route("POST", "/api/heartbeat", lambda body: (s.heartbeat.once(), 200), "Send one heartbeat to the controller now",
                  "The controller calls it when the Topology page opens.", tag="pairing"),
            # The controller lets go of this machine (its board's ✕).
            Route("POST", "/api/unpair", lambda body: (s.heartbeat.unpair(), 200),
                  "Let go of the controller: its address and the fleet token leave config.json",
                  "The ✕ on the machine's node on the board: `controllerUrl` and `controllerToken` leave "
                  "`config.json`, the heartbeat stops (`state: unpaired`); the cells keep running. Token-gated like "
                  "everything else, so only the scout's own controller can let go.", tag="pairing"),
            Route("POST", "/api/llama-node/start", self._start, "Start a server cell: llama.cpp, vLLM or a command cell",
                  "A llama cell needs the controller-built `args` (with `{{MODEL_PATH}}`-style placeholders), "
                  "`port`, model file references to download and `cacheModels`. A command cell "
                  "(`cellKind=command`) needs `shellLine` — the complete `bash -lc` sentence — plus `healthPath`, "
                  "which is stored with the cell so re-adoption after a scout restart probes the right endpoint (a "
                  "vLLM cell answers on `/v1/models`, not `/health`). Missing `args` or `shellLine` is refused with "
                  "a version hint: this scout never assembles its own. `inPlace` (optional, 2.3+) maps each model "
                  "path the cell is sent to where the controller reads that file — `{path, size}` for a file, "
                  "`{path, dir: true}` for a folder, `library` when it is a library's: a file this machine has at "
                  "that path with that size is read there (no copy, never deleted); a library's file or a folder it "
                  "lacks is refused (409) naming what is missing, since a download cannot bring it. `vram` "
                  "(optional, 2.7+) — `{device, reserveMiB, who, why, lower}`, what the cell reserves on a card the "
                  "moment it starts (the controller's rule: vLLM takes utilization × the card): a command cell "
                  "whose card has less free right now is refused before it runs, naming the running cells that "
                  "hold it. `env` (optional, 2.8.1+) — `{NAME: value}` a llama cell's server starts with, over the "
                  "scout's own environment: a CPU-only cell (`N_GPU_LAYERS` 0) gets `CUDA_VISIBLE_DEVICES=\"\"`, "
                  "the environment the controller's start.sh exports; it is written into the cell's start.sh and "
                  "kept for a restart after a crash. A name that is not a shell variable name, or a value that is "
                  "not a string, is refused (400) before anything starts. Since 2.20 a command cell takes `env` "
                  "too (it was dropped), and `env` may not name `CARAVAN_SCOUT_CELL` (400): the scout sets that "
                  "marker itself, last. Async: returns `{status: \"starting\", phase: \"resolving\"}` immediately; "
                  "progress is visible in `/api/llama-node/status` and the fast heartbeats.", tag="cells",
                  body=("port", "args", "cellKind", "shellLine", "healthPath", "cacheModels", "inPlace", "vram", "env"),
                  body_more=True),
            Route("POST", "/api/llama-node/autostart", self._autostart,
                  "Set whether a cell starts with the machine",
                  "(2.4+) `{port, enabled, payload}`. On keeps `payload` — the very request `/api/llama-node/start` "
                  "takes — and the scout starts the cell when its machine boots: on the first scout start of a "
                  "boot only (the machine's boot id), so a scout update never brings back a cell the operator "
                  "stopped. A start of an autostart cell refreshes the kept request. Off drops it. Answers `{ok, "
                  "port, autostart: [ports]}`; 400 for a bad port or an on without a request.", tag="cells",
                  body=("port", "enabled", "payload")),
            Route("POST", "/api/host/reboot", lambda body: power.issue("reboot"), "Reboot this machine", self.POWER, tag="machine"),
            Route("POST", "/api/host/poweroff", lambda body: power.issue("poweroff"),
                  "Power this machine off — nothing on the board can switch it back on", self.POWER, tag="machine"),
            Route("POST", "/api/llama-node/stop", lambda body: (s.cells.stop(body().get("port")), 200),
                  "Stop one cell by its port, or every cell",
                  "`{port}`, or no port for ALL cells. Drops the cell from the fleet view and the registry; purges "
                  "cached models unless the cell had `cacheModels` (safe purge — never evicts a model a sibling cell "
                  "still serves). A port still held by a process the scout lost track of is reclaimed only when it "
                  "is recognisably ours (its marker or the llama-server binary); an unrecognised holder is named "
                  "and left alone.", tag="cells", body=("port",)),
            Route("POST", "/api/llama-node/update", lambda body: (s.builds.start_update(body()), 200),
                  "Start the llama.cpp update job (empty tag: the latest release)",
                  "`{tag?}` — empty is the latest release; a commit works too. With `restoreId` in the body the same "
                  "job restores an archived build instead of building. 409 while one runs.", tag="llama.cpp",
                  body=("tag", "restoreId")),
            Route("POST", "/api/vllm/update", lambda body: (s.vllm.start_update(body()), 200),
                  "Install another vLLM version into the machine's venv",
                  "(2.9+) Into `~/vllm-venv`: `{version?}` — empty is the latest release (`pip install --upgrade "
                  "vllm`), a version pins it (`vllm==X`; a rollback is an older pin). The current version goes into "
                  "the history first. Its own background job, apart from the llama.cpp build's: 409 while one runs; "
                  "400 for a version that is not one, or when the venv does not exist yet (the first vLLM cell start "
                  "provisions it). Running cells keep their vLLM until restarted.", tag="vllm", body=("version",)),
            Route("POST", "/api/llama-node/restore", self._restore, "Restore an archived llama.cpp build",
                  "`{id}` — `restoreId` is read as an alias, and `id` wins when both are given. 400 \"build id is "
                  "required\" without one: the update job would read an empty id as no restore and build the latest "
                  "release instead. The same job as the update, so 409 while one runs.", tag="llama.cpp",
                  body=("id", "restoreId")),
            # The operator hid the "fresh build, crashing cells" banner: for this build.
            Route("POST", "/api/llama-node/suspect-dismiss", lambda body: (s.suspect.dismiss(), 200),
                  "Hide the \"fresh build, crashing cells\" banner for the current build",
                  "(2.6+) A new build can raise it again.", tag="llama.cpp"),
            Route("POST", "/api/llama-node/purge-cache", lambda body: ({"ok": True, **s.cells.purge_models_safely()}, 200),
                  "Clear the model cache, never evicting a model a running cell serves", tag="cells"),
            Route("POST", "/api/llama-node/configs/delete", self._delete_config,
                  "Delete a saved launch config by its filename", tag="cells", body=("filename",)),
            # A model of an engine next to the cells (Ollama, LM Studio)
            # unloaded from the board (2.14): {kind, port, model}; answered at
            # once, the act runs on its own. No load (2.18): a cell loads it.
            Route("POST", "/api/engines/unload", lambda body: self._engine("unload", body),
                  "Unload a model from Ollama or LM Studio",
                  "(2.14+) `{kind, port, model}` — only an engine the last scan found and a model it listed and "
                  "holds loaded (else 400/404/409 with the reason), one act per model at a time. Answered at once "
                  "with `{ok, engines}`, the model carrying `action: {op, since}`; the act runs on a thread of its "
                  "own, then the engines are scanned again. Ollama: `/api/generate` with `keep_alive: 0`. LM Studio "
                  "0.4+: `/api/v1/models/unload` for every loaded instance. A refusal stays on the model as "
                  "`actionError: {op, error, at}` — the engine's own words — until the next act on it. There is no "
                  "load (2.18): a cell in the engine loads its model when it starts.", tag="engines",
                  body=("kind", "port", "model")),
            # A model deleted from an engine (2.17): {kind, port, model};
            # answered at once, the act runs on its own. No download (2.22):
            # a model reaches an engine by that engine's own tools.
            Route("POST", "/api/engines/delete", lambda body: self._engine("delete", body),
                  "Delete a model from Ollama's disk",
                  "(2.17) `{kind, port, model}` — Ollama only (`DELETE /api/delete`), on a thread of its own with "
                  "`action: {op: \"delete\"}` on the model while it runs; a loaded model is refused (409: unload it "
                  "first). LM Studio has no verb for it and does not say which files are a model's, so it does not "
                  "offer it. Nothing is downloaded into an engine (2.22).", tag="engines", body=("kind", "port", "model")),
            # The engine's server itself started or stopped (2.16): {kind,
            # port}; answered at once, the start or stop runs on its own.
            Route("POST", "/api/engines/start", lambda body: self._server("start", body),
                  "Start an engine's server (Ollama, LM Studio)",
                  "(2.16) `{kind, port}` — a known server that is not running: a view of its own, "
                  "`state: \"stopped\"`, `models: null`. " + self.ENGINE_SERVER, tag="engines", body=("kind", "port")),
            Route("POST", "/api/engines/stop", lambda body: self._server("stop", body),
                  "Stop an engine's server (Ollama, LM Studio)",
                  "(2.16) `{kind, port}` — a server this scout's user runs, when the scout knows how to start it "
                  "again. " + self.ENGINE_SERVER, tag="engines", body=("kind", "port")),
        ]
        return rows

    def _start(self, body) -> tuple[Any, int]:
        payload = body()
        result = self.scout.cells.start(payload)
        if result.get("ok"):
            self.scout.autostart.refresh(result.get("port"), payload)
        return result, 200 if result.get("ok") else 400

    def _engine(self, op, body) -> tuple[Any, int]:
        b = body()
        return self.scout.engines.act(op, str(b.get("kind") or ""), b.get("port"), str(b.get("model") or "")), 200

    def _server(self, op, body) -> tuple[Any, int]:
        b = body()
        return self.scout.engines.serve(op, str(b.get("kind") or ""), b.get("port")), 200

    def _autostart(self, body) -> tuple[Any, int]:
        b = body()
        return self.scout.autostart.set(b.get("port"), b.get("enabled"), b.get("payload")), 200

    def _restore(self, body) -> tuple[Any, int]:
        b = body()
        return self.scout.builds.restore(b.get("id") or b.get("restoreId")), 200

    def _delete_config(self, body) -> tuple[Any, int]:
        self.scout.configs.delete(str(body().get("filename") or ""))
        return {"ok": True}, 200

    def handler(self) -> type[BaseHTTPRequestHandler]:
        """The request handler class for this scout's server."""
        return type("Handler", (ScoutHandler,), {"api": self})


class ScoutHandler(BaseHTTPRequestHandler):
    """One request to the scout, answered from its Api's tables."""

    api: Api
    server_version = f"caravan-scout/{APP_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_page(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise AppError("body must be a JSON object")
        return payload

    def token_ok(self, body: dict[str, Any] | None = None) -> bool:
        """When a controllerToken is configured, every API call must carry
        it (controller does via X-Caravan-Token; the pairing form may put
        it into the body instead). No token configured = open (LAN mode)."""
        expected = self.api.scout.config.token()
        if not expected:
            return True
        got = self.headers.get("X-Caravan-Token") or ""
        if not got and isinstance(body, dict):
            got = str(body.get("token") or "")
        return hmac.compare_digest(got, expected)

    def refuse(self) -> None:
        self.send_json({"error": "fleet token required (X-Caravan-Token)"}, 401)

    def do_GET(self) -> None:
        api = self.api
        try:
            if self.path in api.pages:
                self.send_page(api.pages[self.path]())
                return
            if self.path in api.open_get:
                self.send_json(api.open_get[self.path]())
                return
            if not self.token_ok():
                self.refuse()
                return
            path, _, query = self.path.partition("?")
            if path in api.get_query:
                self.send_json(api.get_query[path]({k: v[0] for k, v in parse_qs(query).items()}))
                return
            route = api.get.get(self.path)
            if route is None:
                self.send_json({"error": "not found"}, 404)
                return
            self.send_json(route())
        except AppError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:
        api = self.api
        try:
            if self.path == "/api/controller-url":
                # The pairing form may carry the token in the body, so the
                # body is read before the gate on this one path — and a token
                # the scout does not hold yet may pass it: see
                # Heartbeat.takes_new_token.
                body = self.read_body()
                url, token = str(body.get("url") or ""), str(body.get("token") or "")
                if not self.token_ok(body) and not api.scout.heartbeat.takes_new_token(url, token):
                    self.refuse()
                    return
                self.send_json(api.scout.heartbeat.pair(url, token))
                return
            if not self.token_ok():
                self.refuse()
                return
            route = api.post.get(self.path)
            if route is None:
                self.send_json({"error": "not found"}, 404)
                return
            payload, status = route(self.read_body)
            self.send_json(payload, status)
        except AppError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)
