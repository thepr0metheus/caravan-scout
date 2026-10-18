#!/usr/bin/env python3
"""Every test script of the scout can fail: its verdict is its exit code.

`Checks.finish()` (scripts/_scout_harness.py) RETURNS 1 when a check failed. A script that calls it as a statement — `CHECKS.finish()` — prints
"FAILED" and exits 0, and the CI (`for t in scripts/test_scout_*.py; do python3 "$t"; done`) goes on green. scripts/test_scout_metrics.py ended so
from its first day (2.23.2) until 2026-10-18, when a run of mutants left fourteen of seventeen alive, each of them with the test printing its
FAIL lines and the exit code 0. A test that cannot go red is not a test.

This guard names every `finish()` that stands alone as a statement. It tries itself first: a snippet with the broken ending must be named, and the
two good endings must not — so a guard that has gone blind says so.

Run: python3 scripts/check_test_verdicts.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestVerdicts:
    """The `finish()` calls of a source that stand as statements: their return value, the verdict, thrown away."""

    @staticmethod
    def dropped(source: str) -> list[int]:
        """The line numbers of the calls of `.finish()` whose result goes nowhere."""
        lines = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Attribute) and func.attr == "finish":
                    lines.append(node.lineno)
        return lines

    @classmethod
    def itself(cls) -> list[str]:
        """What is wrong with the guard, if anything: it must name the broken ending and pass the two good ones."""
        problems = []
        if cls.dropped("CHECKS.finish()\n") != [1]:
            problems.append("the guard does not name a bare `CHECKS.finish()`")
        if cls.dropped("sys.exit(CHECKS.finish())\n"):
            problems.append("the guard names `sys.exit(CHECKS.finish())`, which is right")
        if cls.dropped("def main():\n    return CHECKS.finish()\n\nsys.exit(main())\n"):
            problems.append("the guard names `return CHECKS.finish()`, which is right")
        return problems

    @classmethod
    def scan(cls, root: Path = ROOT) -> list[str]:
        found = []
        for path in sorted((root / "scripts").glob("test_*.py")):
            for line in cls.dropped(path.read_text(encoding="utf-8")):
                found.append(f"{path.relative_to(root)}:{line}: `finish()` as a statement — its verdict is thrown away, the script exits 0 whatever failed "
                             f"(write `sys.exit(CHECKS.finish())`)")
        return found


def main() -> int:
    broken = TestVerdicts.itself()
    if broken:
        print("check_test_verdicts: the guard cannot see:")
        for line in broken:
            print("  - " + line)
        return 1
    found = TestVerdicts.scan()
    if found:
        print(f"check_test_verdicts: FAILED — {len(found)} test(s) that cannot fail:")
        for line in found:
            print("  - " + line)
        return 1
    count = len(list((ROOT / "scripts").glob("test_*.py")))
    print(f"check_test_verdicts OK: {count} test scripts, each one's exit code is its verdict; the guard sees the broken ending")
    return 0


if __name__ == "__main__":
    sys.exit(main())
