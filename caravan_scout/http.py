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
from caravan_scout.errors import AppError
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
    """What each path does, as tables: one line per path.

    The scout's page, /api/pairing and /api/health are open. Everything else
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
        self.open_get: dict[str, Callable[[], Any]] = {
            # Open like the page itself: see Report.pairing().
            "/api/pairing": lambda: s.report.pairing(),
            "/api/health": lambda: {"ok": True, "service": "caravan-scout", "version": APP_VERSION,
                                    "tokenRequired": bool(s.config.token()), "time": int(time.time())},
        }
        self.get: dict[str, Callable[[], Any]] = {
            "/api/state": lambda: s.report.public(),
            "/api/llama-node/status": lambda: {"ok": True, "nodes": s.cells.views()},
            "/api/monitor/nvidia-smi": lambda: s.machine.nvidia_smi(),
            # What is listening on this box, so the controller's port picker
            # stops offering numbers something else already owns. The
            # controller can only see its OWN host; a client squatter was
            # invisible until now.
            "/api/host/listeners": lambda: s.machine.listeners(),
            "/api/llama-node/configs": lambda: {"ok": True, "configs": s.configs.listing()},
            "/api/llama-node/update-status": lambda: s.builds.status(),
            "/api/llama-node/builds": lambda: s.builds.archive(),
            "/api/llama-node/list-cache": lambda: {"ok": True, "models": s.models.listing()},
            # vLLM in this machine's venv: version, history, the install job.
            "/api/vllm": lambda: s.vllm.info(),
            "/api/vllm/update-status": lambda: s.vllm.job.status(),
        }
        # Asked with a query: path -> fn(the query's first values).
        self.get_query: dict[str, Callable[[dict[str, str]], Any]] = {
            # What is new since `since` for the board's charts; asking marks
            # the machine watched (Telemetry).
            "/api/telemetry": lambda q: s.telemetry.since(q.get("since")),
        }
        # path -> fn(read_body) -> (payload, status)
        self.post: dict[str, Callable[[Callable[[], dict]], tuple[Any, int]]] = {
            "/api/heartbeat": lambda body: (s.heartbeat.once(), 200),
            # The controller lets go of this machine (its board's ✕).
            "/api/unpair": lambda body: (s.heartbeat.unpair(), 200),
            "/api/llama-node/start": self._start,
            "/api/llama-node/autostart": self._autostart,
            "/api/host/reboot": lambda body: self.power.issue("reboot"),
            "/api/host/poweroff": lambda body: self.power.issue("poweroff"),
            "/api/llama-node/stop": lambda body: (s.cells.stop(body().get("port")), 200),
            "/api/llama-node/update": lambda body: (s.builds.start_update(body()), 200),
            "/api/vllm/update": lambda body: (s.vllm.start_update(body()), 200),
            "/api/llama-node/restore": self._restore,
            # The operator hid the "fresh build, crashing cells" banner: for this build.
            "/api/llama-node/suspect-dismiss": lambda body: (s.suspect.dismiss(), 200),
            "/api/llama-node/purge-cache": lambda body: ({"ok": True, **s.cells.purge_models_safely()}, 200),
            "/api/llama-node/configs/delete": self._delete_config,
            # A model of an engine next to the cells (Ollama, LM Studio) loaded
            # or unloaded from the board (2.14): {kind, port, model,
            # contextLength?}; answered at once, the act runs on its own.
            "/api/engines/load": lambda body: self._engine("load", body),
            "/api/engines/unload": lambda body: self._engine("unload", body),
            # A model downloaded into an engine, or deleted from it (2.17):
            # {kind, port, model}; answered at once, each runs on its own.
            "/api/engines/delete": lambda body: self._engine("delete", body),
            "/api/engines/pull": self._pull,
            # The engine's server itself started or stopped (2.16): {kind,
            # port}; answered at once, the start or stop runs on its own.
            "/api/engines/start": lambda body: self._server("start", body),
            "/api/engines/stop": lambda body: self._server("stop", body),
        }

    def _start(self, body) -> tuple[Any, int]:
        payload = body()
        result = self.scout.cells.start(payload)
        if result.get("ok"):
            self.scout.autostart.refresh(result.get("port"), payload)
        return result, 200 if result.get("ok") else 400

    def _engine(self, op, body) -> tuple[Any, int]:
        b = body()
        return self.scout.engines.act(op, str(b.get("kind") or ""), b.get("port"), str(b.get("model") or ""),
                                      b.get("contextLength"), force=b.get("force") is True, hold=b.get("hold")), 200

    def _pull(self, body) -> tuple[Any, int]:
        b = body()
        return self.scout.engines.pull(str(b.get("kind") or ""), b.get("port"), str(b.get("model") or "")), 200

    def _server(self, op, body) -> tuple[Any, int]:
        b = body()
        return self.scout.engines.serve(op, str(b.get("kind") or ""), b.get("port")), 200

    def _autostart(self, body) -> tuple[Any, int]:
        b = body()
        return self.scout.autostart.set(b.get("port"), b.get("enabled"), b.get("payload")), 200

    def _restore(self, body) -> tuple[Any, int]:
        b = body()
        return self.scout.builds.start_update({"restoreId": str(b.get("id") or b.get("restoreId") or "")}), 200

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
            if self.path in ("/", "/index.html"):
                self.send_page(PairingPage.body())
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
