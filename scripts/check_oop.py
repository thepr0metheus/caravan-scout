#!/usr/bin/env python3
"""Guard of the scout's shape: objects, not mixins and loose functions.

The scout was one class assembled from mixins that shared a single `self`:
any method could read or write any other part's fields, and the agent code
that is gone now was threaded through all of them. It was rewritten into
classes with one job each (docs/architecture.md). Without a rule the next
feature is another method on a shared `self`, and the next module a hundred
lines of functions.

Rules, checked by reading the code, not the docstrings:
1. No mixins: no class named `*Mixin`.
2. Every module under caravan_scout/ is a class module — at least one class,
   and no top-level function longer than FACE_LINES lines (a face is a
   one-liner that hands over to a class) — except the modules on
   FUNCTION_MODULES, each with the reason it is not one.
3. The list is a ratchet: a listed module that has become a class module
   must leave the list, and a listed file that does not exist is an error.
4. A method that raises NotImplementedError makes its class abstract: every
   subclass in the package overrides it — a start kind without its own
   run() would answer every request with a crash.

Negative self-test: scripts/test_check_oop.py.

Run: python3 scripts/check_oop.py
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "caravan_scout"
FACE_LINES = 3

# Modules that stay without classes, and why.
FUNCTION_MODULES = {
    "__init__.py": "the package's version string, nothing else",
    "paths.py": "constants read from the environment at import, nothing that runs",
    "app.py": "the process entry point: arguments, then the scout's parts are started — nothing kept",
}


class Module:
    """One module of the package, as its syntax tree tells it."""

    def __init__(self, path):
        self.path = path
        self.name = path.name
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.classes = [n for n in self.tree.body if isinstance(n, ast.ClassDef)]

    def long_functions(self):
        out = []
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                if length > FACE_LINES:
                    out.append((node.name, length))
        return out

    @staticmethod
    def abstract_methods(cls):
        """Methods of `cls` whose body raises NotImplementedError."""
        names = set()
        for item in cls.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(item):
                if isinstance(node, ast.Raise) and node.exc is not None:
                    exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                    if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                        names.add(item.name)
        return names


class ScoutShape:
    """The four rules over one package directory and its list of exceptions."""

    def __init__(self, package=PACKAGE, listed=None):
        self.package = package
        self.listed = FUNCTION_MODULES if listed is None else listed
        self.modules = [Module(p) for p in sorted(package.glob("*.py"))]

    def problems(self):
        found = []
        for name in sorted(self.listed):
            if not (self.package / name).exists():
                found.append(f"{name}: в списке модулей без классов, но файла нет — список врёт")
        for mod in self.modules:
            for cls in mod.classes:
                if cls.name.endswith("Mixin"):
                    found.append(f"{mod.name}:{cls.lineno}: класс-примесь {cls.name} — "
                                 f"у каждой части своя работа и свои поля, не общий self")
            long_funcs = mod.long_functions()
            if mod.name in self.listed:
                if mod.classes and not long_funcs:
                    found.append(f"{mod.name}: уже модуль классов — уберите его из FUNCTION_MODULES")
                continue
            if not mod.classes:
                found.append(f"{mod.name}: ни одного класса — либо класс, либо строка в "
                             f"FUNCTION_MODULES с причиной")
            for fname, length in long_funcs:
                found.append(f"{mod.name}: функция {fname} на {length} строк вне класса — "
                             f"логика живёт в классе, снаружи только однострочное лицо")
        found.extend(self._forgotten_overrides())
        return found

    def _forgotten_overrides(self):
        classes = {cls.name: (mod, cls) for mod in self.modules for cls in mod.classes}
        found = []
        for name, (mod, cls) in classes.items():
            if Module.abstract_methods(cls):
                continue          # abstract itself: its subclasses answer for it
            ancestors = [classes[b][1] for b in self._ancestors(cls, classes)]
            needed = set()
            for base in ancestors:
                needed |= Module.abstract_methods(base)
            for method in sorted(needed):
                done = self._defines(cls, method) or any(
                    self._defines(base, method) and method not in Module.abstract_methods(base)
                    for base in ancestors)
                if not done:
                    found.append(f"{mod.name}:{cls.lineno}: {name} не реализует {method}() — "
                                 f"у предка он поднимает NotImplementedError")
        return found

    @staticmethod
    def _defines(cls, method):
        return any(isinstance(i, (ast.FunctionDef, ast.AsyncFunctionDef)) and i.name == method for i in cls.body)

    @staticmethod
    def _ancestors(cls, classes):
        seen, todo = [], [b.id for b in cls.bases if isinstance(b, ast.Name)]
        while todo:
            base = todo.pop(0)
            if base in seen or base not in classes:
                continue
            seen.append(base)
            todo.extend(b.id for b in classes[base][1].bases if isinstance(b, ast.Name))
        return seen


def main():
    shape = ScoutShape()
    found = shape.problems()
    if found:
        print("scout oop FAILED:")
        for p in found:
            print("  - " + p)
        return 1
    modules = len(shape.modules)
    print(f"scout oop OK: {modules} модулей, {modules - len(FUNCTION_MODULES)} — классы, "
          f"{len(FUNCTION_MODULES)} без классов по записанной причине")
    return 0


if __name__ == "__main__":
    sys.exit(main())
