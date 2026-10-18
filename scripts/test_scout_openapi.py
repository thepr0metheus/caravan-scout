#!/usr/bin/env python3
"""GET /openapi.json: the scout describes itself, from the table it answers by.

The scout's paths are one list of rows (caravan_scout/routes.py). The handler
answers by it, /openapi.json is written from it, and the endpoint tables of
docs/http-api.md are rendered from it — so what the scout does and what it says
it does cannot part. Pinned here, by value:

  * the document: version, security, one operation per row, a summary on each,
    what is open and what is not, the query and body fields it names;
  * that it is served open, even when the scout holds a fleet token, and says the
    version /api/health says;
  * that the table and the handler are one list: a path is answered exactly when
    it is described;
  * that the page is current, and that the checks go red when it is not.

The home rule (visumap: apps-openapi.md) asks for level 1: paths, methods, what
each does, the fields it reads, who may call. Types and the shapes of answers
are said not to be described yet — never an empty schema, which would read as
"no body".

Run: python3 scripts/test_scout_openapi.py
"""
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks, Served, make_scout  # noqa: E402

from api_reference import ApiReferencePage  # noqa: E402
from caravan_scout import __version__  # noqa: E402
from caravan_scout.api_spec import ApiReference, ApiSpec  # noqa: E402
from caravan_scout.http import Api  # noqa: E402
from caravan_scout.routes import Route  # noqa: E402

CHECKS = Checks("scout openapi")
check = CHECKS.check

TOKEN = "t0k3n-for-tests"
MAX_SUMMARY = 90

# Open with a token configured: the page, the paths the controller reads before it is paired, and the
# machine's entry by OpenAI's API (2.27) — open like the cells it reaches.
ENTRY = {("get", "/v1/models"), ("get", "/v1/models/{model}"), ("post", "/v1/{path}")}
OPEN = {("get", "/"), ("get", "/index.html"), ("get", "/api/pairing"), ("get", "/api/health"),
        ("get", "/openapi.json")} | ENTRY

# The fields each POST route names, as the description says them. The routes that read no body are not here.
BODIES = {
    "/api/controller-url": ["url", "token"],
    "/api/llama-node/start": ["port", "args", "cellKind", "shellLine", "healthPath", "cacheModels", "inPlace", "vram", "ram", "env",
                              "modelFiles"],
    "/api/llama-node/autostart": ["port", "enabled", "payload"],
    "/api/llama-node/stop": ["port"],
    "/api/llama-node/update": ["tag", "restoreId"],
    "/api/vllm/update": ["version"],
    "/api/host/driver/install": ["package"],
    "/api/llama-node/restore": ["id", "restoreId"],
    "/api/llama-node/configs/delete": ["filename"],
    "/api/engines/unload": ["kind", "port", "model"],
    "/api/engines/delete": ["kind", "port", "model"],
    "/api/engines/start": ["kind", "port"],
    "/api/engines/stop": ["kind", "port"],
    "/api/llama-node/cache/keep": ["keep"],
    "/api/llama-node/cache/delete-foreign": ["expectBytes"],
    "/v1/{path}": ["model"],
}
NO_BODY = ["/api/heartbeat", "/api/unpair", "/api/host/reboot", "/api/host/poweroff",
           "/api/llama-node/suspect-dismiss", "/api/llama-node/purge-cache"]


def problems_in(document):
    """What is wrong with a description: a path that says nothing about itself."""
    out = []
    for path, ops in document["paths"].items():
        for method, op in ops.items():
            where = f"{method.upper()} {path}"
            summary = op.get("summary") or ""
            if not summary:
                out.append(f"{where}: no summary")
            elif len(summary) > MAX_SUMMARY or "\n" in summary:
                out.append(f"{where}: the summary is not one short line")
            if "UNSURE" in (summary + " " + (op.get("description") or "")).upper():
                out.append(f"{where}: a draft left marked UNSURE")
    return out


def bare_schemas(node, where="document"):
    """Schemas that say nothing at all: no type, no reference and no words."""
    out = []
    if isinstance(node, dict):
        if "schema" in node and isinstance(node["schema"], dict):
            s = node["schema"]
            if not (set(s) & {"type", "$ref", "description"}):
                out.append(where)
        for key, value in node.items():
            out.extend(bare_schemas(value, f"{where}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out.extend(bare_schemas(value, f"{where}[{i}]"))
    return out


def test_the_document():
    CHECKS.section("документ — что в нём сказано:")
    scout = make_scout()
    api = Api(scout)
    doc = api.spec.document()
    ops = [(method, path, op) for path, item in doc["paths"].items() for method, op in item.items()]
    check(doc["openapi"] == "3.1.0" and doc["info"]["title"] == "caravan-scout" and doc["info"]["version"] == __version__,
          "OpenAPI 3.1, имя «caravan-scout», версия — та, что у скаута")
    check(doc["components"]["securitySchemes"]["fleetToken"]
          == {"type": "apiKey", "in": "header", "name": "X-Caravan-Token", "description": ApiSpec.SECURITY},
          "вход: токен флота в заголовке X-Caravan-Token, и сказано, когда скаут открыт без него")
    check(len(ops) == len(api.routes) == 45, f"операций столько же, сколько строк в таблице путей — 45 (got {len(ops)})")
    check(list(doc["paths"]) == sorted(doc["paths"]), "пути в документе по алфавиту — он не зависит от порядка строк таблицы")
    ids = [op["operationId"] for _m, _p, op in ops]
    check(len(set(ids)) == len(ids) and "getApiLlamaNodeStatus" in ids and "postApiHeartbeat" in ids
          and "getRoot" in ids and "getIndexHtml" in ids and "getOpenapiJson" in ids,
          "у каждой операции свой operationId: getApiLlamaNodeStatus, postApiHeartbeat, getRoot…")
    check(problems_in(doc) == [], f"у каждого пути есть summary в одну короткую строку, черновиков нет (got {problems_in(doc)})")
    check(all(op["tags"] for _m, _p, op in ops) and [t["name"] for t in doc["tags"]] == sorted({t for _m, _p, op in ops for t in op["tags"]}),
          "у каждой операции есть тег, и все теги названы")

    opened = {(m, p) for m, p, op in ops if op["security"] == []}
    closed = {(m, p) for m, p, op in ops if op["security"] == [{"fleetToken": []}]}
    check(opened == OPEN, f"открыты ровно страница, /api/pairing, /api/health, /openapi.json и вход машины /v1 (got {sorted(opened)})")
    check(opened | closed == {(m, p) for m, p, _op in ops} and not (opened & closed),
          "negative: остальные — все под токеном, ни одна не «ни то ни сё»")
    check(all("401" in op["responses"] for m, p, op in ops if (m, p) not in OPEN)
          and not any("401" in op["responses"] for m, p, op in ops if (m, p) in OPEN),
          "401 назван у закрытых путей и не назван у открытых")

    params = {p: op.get("parameters") for m, p, op in ops if "parameters" in op}
    in_path = {p: [(x["name"], x["in"], x["required"], x["schema"]) for x in ps] for p, ps in params.items() if p != "/api/telemetry"}
    check(params["/api/telemetry"] == [{"name": "since", "in": "query", "required": False,
                                        "schema": {"description": ApiSpec.NOT_DESCRIBED}}],
          "параметр запроса один — since у /api/telemetry; больше никто запроса не читает")
    check(in_path == {"/v1/models/{model}": [("model", "path", True, {"type": "string"})],
                      "/v1/{path}": [("path", "path", True, {"type": "string"})]}
          and all(x["description"] for p in in_path for x in params[p]),
          "параметры пути — у входа машины (2.27): {model} и {path}, обязательные строки, каждый со словами; "
          "negative: у других путей шаблонов нет")
    posts = {p: op for m, p, op in ops if m == "post"}
    said = {p: list(op["requestBody"]["content"]["application/json"]["schema"]["properties"])
            for p, op in posts.items() if "requestBody" in op}
    check(said == BODIES, f"поля тела названы так же, как в таблице (got {said})")
    check(sorted(p for p, op in posts.items() if "requestBody" not in op) == sorted(NO_BODY),
          "negative: у путей, что тело не читают, тела в описании нет")
    check(all(list(op["requestBody"]["content"]["application/json"]["schema"]["properties"].values())
              == [{"description": ApiSpec.NOT_DESCRIBED}] * len(BODIES[p]) for p, op in posts.items() if "requestBody" in op),
          "тип поля не описан, и это сказано словами, а не пустой схемой")
    check(posts["/api/llama-node/stop"]["requestBody"]
          == {"required": False, "content": {"application/json": {"schema": {
              "type": "object", "properties": {"port": {"description": ApiSpec.NOT_DESCRIBED}}}}}},
          "тело — по значению целиком: не обязательно (путь без тела тоже работает), объект, поля без типов")
    incomplete = sorted(p for p, op in posts.items() if op.get("x-caravan-fields-complete") is False)
    check(incomplete == ["/api/llama-node/start", "/v1/{path}"],
          "«может читать больше» — у старта ячейки и у входа машины: тело уходит дальше целиком")
    forms = sorted(p for p, op in posts.items() if "requestBody" in op and "multipart/form-data" in op["requestBody"]["content"])
    check(forms == ["/v1/{path}"] and posts["/v1/{path}"]["requestBody"]["content"]["multipart/form-data"]
          == posts["/v1/{path}"]["requestBody"]["content"]["application/json"],
          "форму с записью принимает только вход машины, и её поле model — то же, что у JSON; negative: у остальных только JSON")
    check([p for p, op in posts.items() if op.get("x-caravan-token-in-body")] == ["/api/controller-url"],
          "только сопряжение принимает токен и в теле: форма страницы не шлёт заголовков")
    check(bare_schemas(doc) == [], f"negative: пустых схем нет — каждая называет тип, ссылку или «не описано» (got {bare_schemas(doc)})")
    pages = {p for m, p, op in ops if list(op["responses"]["200"]["content"]) == ["text/html"]}
    check(pages == {"/", "/index.html"}, "страница отдаётся как text/html, всё остальное — JSON")
    refs = {v["content"]["application/json"]["schema"]["$ref"] for m, p, op in ops if (m, p) not in ENTRY
            for k, v in op["responses"].items() if k in ("401", "default")}
    check(refs == {"#/components/schemas/Error"} and doc["components"]["schemas"]["Error"]["required"] == ["error"],
          "отказ назван один раз: {error}, и ссылка на него живая")
    entry_ops = {(m, p): op for m, p, op in ops if (m, p) in ENTRY}
    entry_refs = {op["responses"]["default"]["content"]["application/json"]["schema"]["$ref"] for op in entry_ops.values()}
    openai = doc["components"]["schemas"]["OpenAIError"]
    check(entry_refs == {"#/components/schemas/OpenAIError"} and openai["required"] == ["error"]
          and openai["properties"]["error"]["required"] == ["message", "type"]
          and sorted(openai["properties"]["error"]["properties"]) == ["code", "message", "param", "type"]
          and all(sorted(op["responses"]) == ["200", "default"] for op in entry_ops.values()),
          "вход машины отказывает в форме OpenAI — {error: {message, type, param, code}}, своя схема; 401 у него нет")
    check(list(entry_ops[("post", "/v1/{path}")]["responses"]["200"]["content"]) == ["application/json", "text/event-stream"]
          and list(entry_ops[("get", "/v1/models")]["responses"]["200"]["content"]) == ["application/json"],
          "ответ запроса — как его дала ячейка: JSON или поток событий; список моделей — JSON")


def test_served():
    CHECKS.section("отдаётся — открыто, и версия та же, что у /api/health:")
    scout = make_scout({"controllerToken": TOKEN})
    with Served(scout) as srv:
        status, doc = srv.get("/openapi.json")
        headers = {k.lower(): v for k, v in srv.last_headers.items()}
        check(status == 200 and isinstance(doc, dict) and doc["openapi"] == "3.1.0",
              "GET /openapi.json — 200 и документ, и без токена, хотя скаут его держит")
        check(headers.get("content-type") == "application/json; charset=utf-8", "JSON в UTF-8")
        check(doc == Api(scout).spec.document() and srv.get("/openapi.json")[1] == doc,
              "тот же документ, что строит таблица, и второй раз — тот же")
        _s, health = srv.get("/api/health")
        check(doc["info"]["version"] == health["version"] == __version__,
              "info.version — то, что называет /api/health: по нему видно, какую сборку описывает документ")
        check(TOKEN not in json.dumps(doc), "negative: токена флота в описании нет — только имя заголовка")
        check(srv.get("/api/state") == (401, {"error": "fleet token required (X-Caravan-Token)"}),
              "negative: соседний путь остаётся закрытым — открыт только он")
        check(srv.get("/openapi.json?x=1")[0] == 401,
              "as-is: путь сверяется целиком, вместе с запросом — с «?x=1» это уже не /openapi.json, и ворота его закрывают")
    with Served(make_scout()) as srv:
        check(srv.get("/openapi.json")[0] == 200 and srv.post("/openapi.json")[0] == 404,
              "без токена — тоже, а POST на него — 404: в таблице только GET")


def test_one_list():
    CHECKS.section("таблица и обработчик — один список:")
    scout = make_scout()
    api = Api(scout)
    # The machine's entry is answered by its prefix (/v1/), before the tables: its rows are pinned below.
    answered = ({("get", p) for table in (api.pages, api.open_get, api.get, api.get_query) for p in table}
                | {("post", p) for p in api.post} | {("post", "/api/controller-url")}
                | {(r.method.lower(), r.path) for r in api.routes if r.entry})
    described = {(m, p) for p, item in api.spec.document()["paths"].items() for m in item}
    check(answered == described, f"скаут отвечает ровно на то, что описано (только у одного: {sorted(answered ^ described)})")
    check(sorted(api.pages) == ["/", "/index.html"] and sorted(api.open_get) == ["/api/health", "/api/pairing", "/openapi.json"],
          "страницы и открытые пути — те, что помечены в таблице")
    check(list(api.get_query) == ["/api/telemetry"] and "/api/controller-url" not in api.post,
          "запрос — у телеметрии; сопряжение обработчик отвечает сам, до ворот, и в таблице обработчиков его нет")
    unhandled = [r.path for r in api.routes if r.handler is None and not r.entry]
    entry = sorted(r.path for r in api.routes if r.entry)
    check(unhandled == ["/api/controller-url"] and entry == ["/v1/models", "/v1/models/{model}", "/v1/{path}"]
          and all(r.handler is None and r.needs_no_token for r in api.routes if r.entry),
          "строки без обработчика — сопряжение и вход машины (2.27): их обработчик отвечает сам, до ворот; вход открыт, "
          "как ячейки; negative: другой строки без обработчика нет")
    check(len({(r.method, r.path) for r in api.routes}) == len(api.routes), "negative: двух строк на один путь и метод нет")


def test_the_checks_go_red():
    CHECKS.section("проверки краснеют, когда описание врёт:")
    scout = make_scout()
    api = Api(scout)
    rows = list(api.routes)
    doc = ApiSpec(rows + [Route("GET", "/api/nothing", lambda: {}, "", tag="meta")], "0").build()
    check(problems_in(doc) == ["GET /api/nothing: no summary"], f"путь без summary назван (got {problems_in(doc)})")
    doc = ApiSpec(rows + [Route("GET", "/api/long", lambda: {}, "x" * 91, tag="meta")], "0").build()
    check(problems_in(doc) == ["GET /api/long: the summary is not one short line"], "summary длиннее 90 знаков — тоже")
    doc = ApiSpec(rows + [Route("GET", "/api/draft", lambda: {}, "What it does", "UNSURE what this is", tag="meta")], "0").build()
    check(problems_in(doc) == ["GET /api/draft: a draft left marked UNSURE"], "черновик с UNSURE — тоже")
    broken = copy.deepcopy(api.spec.document())
    broken["paths"]["/api/heartbeat"]["post"]["responses"]["200"]["content"]["application/json"]["schema"] = {}
    check(bare_schemas(broken) == ["document.paths./api/heartbeat.post.responses.200.content.application/json"],
          "пустая схема названа: она читалась бы как «тела нет»")

    text = ApiReferencePage.PATH.read_text(encoding="utf-8")
    reference = ApiReference(api.routes)
    check(ApiReferencePage().current() and reference.splice(text) == text,
          "страница docs/http-api.md собрана из таблицы путей — та же, что была бы записана заново")
    table = reference.table("GET")
    check(table.startswith("| Path | Access | Purpose |\n|---|---|---|\n| `/` | open | ")
          and "| `/api/telemetry?since=` | fleet token | " in table and "| `/api/state` | fleet token | " in table,
          "таблица GET: путь, доступ, назначение; запрос виден в пути")
    check("| `/api/controller-url` | fleet token, in the header or the body | " in reference.table("POST"),
          "у сопряжения доступ назван особо: токен в заголовке или в теле")
    drifted = text.replace("| `/api/health` | open |", "| `/api/health` | fleet token |")
    check(drifted != text and reference.splice(drifted) == text and reference.splice(drifted) != drifted,
          "negative: правку строки таблицы руками страница не переживает — при сверке она разойдётся с записанной заново")
    check(ApiReference.cell("a | b\n c") == "a \\| b c", "в ячейке таблицы «|» экранирован, переводы строк схлопнуты")
    try:
        reference.splice(text.replace("<!-- api-reference:get:start -->", ""))
        check(False, "negative: страница без маркеров принята")
    except ValueError as exc:
        check("api-reference:get:start" in str(exc), "negative: страница без маркера — отказ, а не тихое «всё в порядке»")


TESTS = (test_the_document, test_served, test_one_list, test_the_checks_go_red)

for test in TESTS:
    try:
        test()
    except Exception as exc:  # noqa: BLE001 — a crash is a red pin; the rest still runs
        check(False, f"{test.__name__} упал: {exc!r}")

sys.exit(CHECKS.finish())
