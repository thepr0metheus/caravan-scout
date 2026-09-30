#!/usr/bin/env python3
"""docs/http-api.md's two endpoint tables, written from the scout's table of paths.

The page was written by hand and drifted: it went on describing a load the scout
dropped in 2.18 and a download it dropped in 2.22. The rows of caravan_scout/routes.py
are what the scout answers by, what GET /openapi.json describes and what these
tables say, so there is one list and the page cannot disagree with it. Between the
`api-reference` markers the page is rendered; everything else on it stays as written.

test_scout_openapi.py checks the page is current.

Run: python3 scripts/api_reference.py          — say whether the page is current
     python3 scripts/api_reference.py --write  — write it again
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import make_scout  # noqa: E402

from caravan_scout.api_spec import ApiReference  # noqa: E402
from caravan_scout.http import Api  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class ApiReferencePage:
    """docs/http-api.md, and what its tables should say."""

    PATH = ROOT / "docs" / "http-api.md"

    def text(self) -> str:
        """The page as it should be: the tables written from the rows."""
        return ApiReference(Api(make_scout()).routes).splice(self.PATH.read_text(encoding="utf-8"))

    def current(self) -> bool:
        return self.PATH.read_text(encoding="utf-8") == self.text()

    def write(self) -> None:
        self.PATH.write_text(self.text(), encoding="utf-8")


def main(argv: list[str]) -> int:
    page = ApiReferencePage()
    if "--write" in argv:
        page.write()
        print(f"written: {page.PATH.relative_to(ROOT)}")
        return 0
    if page.current():
        print(f"{page.PATH.relative_to(ROOT)} is current")
        return 0
    print(f"{page.PATH.relative_to(ROOT)} does not say what the table of paths says — run with --write")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
