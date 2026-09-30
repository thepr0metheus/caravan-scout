"""One row of the scout's table of paths."""
from __future__ import annotations

from typing import Any, Callable


class Route:
    """One thing the scout answers: its path, what runs, what it is for, who may ask.

    The rows of one list are the whole surface. The handler answers by them and
    /openapi.json and docs/http-api.md are written from them, so what the scout
    does and what it says it does are one list, not two that agree today.

    What `handler` is depends on the row:

      * a page (`page=True`): nothing in, the HTML as bytes out;
      * a GET: nothing in, the answer out; a route with `query` names takes the
        query's first values as a dict;
      * a POST: the body reader in (called only by a route that needs a body,
        so a body that does not parse fails only the routes that read it), and
        (answer, status) out;
      * None: a path ScoutHandler answers itself, before the table, because it
        has to read its body ahead of the token gate (the pairing form's).

    `public`: no fleet token needed, even when the scout holds one. `body`
    names the fields the route's own code reads; `body_more` says it hands the
    body on to code that reads more, so the list is a floor and not the whole.
    """

    PAGE, PUBLIC, GET, QUERY, POST, OWN = "page", "public", "get", "query", "post", "own"

    def __init__(self, method: str, path: str, handler: Callable[..., Any] | None, summary: str,
                 description: str = "", *, tag: str, public: bool = False, page: bool = False,
                 query: tuple[str, ...] = (), body: tuple[str, ...] = (), body_more: bool = False) -> None:
        self.method = method
        self.path = path
        self.handler = handler
        self.summary = summary
        self.description = description
        self.tag = tag
        self.public = public
        self.page = page
        self.query = query
        self.body = body
        self.body_more = body_more

    @property
    def kind(self) -> str:
        """Which table of the handler the row belongs in."""
        if self.page:
            return self.PAGE
        if self.method == "POST":
            return self.POST if self.handler is not None else self.OWN
        if self.query:
            return self.QUERY
        return self.PUBLIC if self.public else self.GET

    @property
    def needs_no_token(self) -> bool:
        """Whether anyone may ask: the page, and the paths marked `public`."""
        return self.page or self.public

    @property
    def reads_a_body(self) -> bool:
        return self.method == "POST" and (bool(self.body) or self.body_more)
