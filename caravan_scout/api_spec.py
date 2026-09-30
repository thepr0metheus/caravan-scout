"""The scout's API described as OpenAPI 3.1, from the table it answers by."""
from __future__ import annotations

import re
from typing import Any

from caravan_scout.routes import Route


class ApiSpec:
    """What /openapi.json says: every path of the scout, what it is for, who may ask.

    It is written from the rows of the scout's own table (routes.py), not
    beside it: a path the scout answers is in the description, and one it does
    not answer is not. That is level 1 of the home rule (visumap, "Описание API
    приложения"): paths and methods, what each does, the query and body fields
    the scout's own code reads, the content types, who may call. The types of
    fields and the shapes of answers are not described yet, and the document
    says so in words — an empty schema would read as "no body", and that is
    untrue.
    """

    OPENAPI = "3.1.0"
    NOT_DESCRIBED = "Not described yet."
    TOKEN_HEADER = "X-Caravan-Token"
    JSON, HTML = "application/json", "text/html"
    SCHEME = "fleetToken"
    ABOUT = ("The API of caravan-scout, the sidecar of a machine in a LAMA CARAVAN fleet: the controller drives "
             "the machine's cells, engines and llama.cpp build through it. Written from the table the scout "
             "answers by, so a path is here exactly when the scout answers it. Types of fields and the shapes "
             "of answers are not described yet.")
    SECURITY = ("The fleet token the controller hands a scout when the operator adds it on the board "
                "(`controllerToken` in config.json), sent as X-Caravan-Token. A scout that holds none is open, as a "
                "trusted LAN is; the page, /api/pairing, /api/health and this document are open either way.")

    def __init__(self, routes: list[Route], version: str) -> None:
        self.routes = routes
        self.version = version
        self._document: dict[str, Any] | None = None

    def document(self) -> dict[str, Any]:
        """The description, built once: the table does not change while the scout runs."""
        if self._document is None:
            self._document = self.build()
        return self._document

    def build(self) -> dict[str, Any]:
        paths: dict[str, dict[str, Any]] = {}
        for route in sorted(self.routes, key=lambda r: (r.path, r.method)):
            paths.setdefault(route.path, {})[route.method.lower()] = self.operation(route)
        return {
            "openapi": self.OPENAPI,
            "info": {"title": "caravan-scout", "version": self.version, "description": self.ABOUT},
            "tags": [{"name": tag} for tag in sorted({r.tag for r in self.routes})],
            "paths": paths,
            "components": {
                "securitySchemes": {self.SCHEME: {"type": "apiKey", "in": "header", "name": self.TOKEN_HEADER,
                                                  "description": self.SECURITY}},
                "schemas": {"Error": {"type": "object", "required": ["error"],
                                      "properties": {"error": {"type": "string"}},
                                      "description": "A refusal or a failure: the status is the scout's own "
                                                     "(400 a bad request, 404, 409…), or 500 for what it did not expect."}},
            },
        }

    def operation(self, route: Route) -> dict[str, Any]:
        op: dict[str, Any] = {"operationId": self.operation_id(route), "summary": route.summary, "tags": [route.tag]}
        if route.description:
            op["description"] = route.description
        op["security"] = [] if route.needs_no_token else [{self.SCHEME: []}]
        if route.query:
            op["parameters"] = [{"name": name, "in": "query", "required": False,
                                 "schema": {"description": self.NOT_DESCRIBED}} for name in route.query]
        if route.method == "POST" and route.kind == Route.OWN:
            # Answered by the handler itself: it reads the body ahead of the token
            # gate, and the fleet token may ride in the body instead of the header.
            op["x-caravan-token-in-body"] = True
        if route.reads_a_body:
            schema: dict[str, Any] = {"type": "object",
                                      "properties": {name: {"description": self.NOT_DESCRIBED} for name in route.body}}
            content: dict[str, Any] = {self.JSON: {"schema": schema}}
            op["requestBody"] = {"required": False, "content": content}
            if route.body_more:
                op["x-caravan-fields-complete"] = False
        op["responses"] = self.responses(route)
        return op

    def responses(self, route: Route) -> dict[str, Any]:
        if route.page:
            ok: dict[str, Any] = {"description": "The page.", "content": {self.HTML: {"schema": {"type": "string"}}}}
        else:
            ok = {"description": "The answer. " + self.NOT_DESCRIBED,
                  "content": {self.JSON: {"schema": {"description": self.NOT_DESCRIBED}}}}
        failure = {"description": "A refusal or a failure: `{error}`.",
                   "content": {self.JSON: {"schema": {"$ref": "#/components/schemas/Error"}}}}
        out: dict[str, Any] = {"200": ok}
        if not route.needs_no_token:
            out["401"] = {"description": "The scout holds a fleet token and none, or another, came in "
                                         "X-Caravan-Token: `{error: \"fleet token required (X-Caravan-Token)\"}`.",
                          "content": {self.JSON: {"schema": {"$ref": "#/components/schemas/Error"}}}}
        out["default"] = failure
        return out

    @staticmethod
    def operation_id(route: Route) -> str:
        """getApiLlamaNodeStatus, postApiHeartbeat, getRoot — unique because a path and a method are."""
        words = re.findall(r"[A-Za-z0-9]+", route.path) or ["root"]
        return route.method.lower() + "".join(w[:1].upper() + w[1:] for w in words)


class ApiReference:
    """The endpoint tables of docs/http-api.md, written from the same rows.

    The page was written by hand and it drifted: it still described a load
    the scout dropped in 2.18 and a download it dropped in 2.22. Between the
    markers it is rendered from the table, and a test fails when the page and
    the table disagree; anything else on the page stays as written.
    """

    ACCESS = {Route.PAGE: "open", Route.PUBLIC: "open", Route.GET: "fleet token", Route.QUERY: "fleet token",
              Route.POST: "fleet token", Route.OWN: "fleet token, in the header or the body"}
    START, END = "<!-- api-reference:{name}:start -->", "<!-- api-reference:{name}:end -->"
    HEAD = "| Path | Access | Purpose |\n|---|---|---|\n"

    def __init__(self, routes: list[Route]) -> None:
        self.routes = routes

    @staticmethod
    def cell(text: str) -> str:
        return " ".join(text.split()).replace("|", "\\|")

    def table(self, method: str) -> str:
        rows = []
        for route in self.routes:
            if route.method != method:
                continue
            path = route.path + ("?" + "&".join(f"{q}=" for q in route.query) if route.query else "")
            purpose = route.summary + (". " + route.description if route.description else "")
            rows.append(f"| `{path}` | {self.ACCESS[route.kind]} | {self.cell(purpose)} |\n")
        return self.HEAD + "".join(rows)

    def splice(self, text: str) -> str:
        """The page with each table between its markers written again; a page without the markers is an error."""
        for method in ("GET", "POST"):
            name = method.lower()
            start, end = self.START.format(name=name), self.END.format(name=name)
            if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
                raise ValueError(f"docs/http-api.md needs exactly one {start} … {end} pair")
            head, rest = text.split(start, 1)
            _old, tail = rest.split(end, 1)
            text = head + start + "\n" + self.table(method) + end + tail
        return text
