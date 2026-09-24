#!/usr/bin/env python3
"""The shape guard can go red: scripts/check_oop.py against planted packages.

A guard that cannot fail is decoration. Each rule gets a package that breaks
it — a mixin, a module of functions, a long function beside a class, a list
entry for a file that is gone, a listed module that has become classes, a
subclass that forgot the method its parent leaves abstract — and the exact
message it must produce; and a twin that keeps the rule, which must pass.

Run: python3 scripts/test_check_oop.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks  # noqa: E402

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("check_oop", HERE / "check_oop.py")
check_oop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_oop)

CHECKS = Checks("scout oop guard")
check = CHECKS.check

CLASS_MODULE = "class Thing:\n    def run(self):\n        return 1\n"
LONG_FUNCTION = "def helper(x):\n    y = x + 1\n    z = y * 2\n    return z\n"      # 4 lines
FACE = "def face(x):\n    return Thing().run()\n\n\n"                                  # 2 lines


class Package:
    """A package in a temp dir: {file name: source}."""

    def __init__(self, files):
        self.dir = Path(tempfile.mkdtemp(prefix="oop-guard-"))
        for name, source in files.items():
            (self.dir / name).write_text(source, encoding="utf-8")

    def problems(self, listed=None):
        return check_oop.ScoutShape(self.dir, {} if listed is None else listed).problems()


def test_the_real_package():
    CHECKS.section("настоящий пакет:")
    check(check_oop.ScoutShape().problems() == [], "caravan_scout проходит все четыре правила")


def test_mixins():
    CHECKS.section("примеси:")
    got = Package({"a.py": "class HeartbeatMixin:\n    pass\n"}).problems()
    check(got == ["a.py:1: класс-примесь HeartbeatMixin — у каждой части своя работа и свои поля, не общий self"],
          f"класс *Mixin назван с файлом и строкой (got {got})")
    check(Package({"a.py": "class Mixing:\n    pass\n"}).problems() == [],
          "negative: имя, которое лишь начинается похоже, — не примесь")


def test_function_modules():
    CHECKS.section("модули без классов:")
    got = Package({"a.py": LONG_FUNCTION}).problems()
    check(got == ["a.py: ни одного класса — либо класс, либо строка в FUNCTION_MODULES с причиной",
                  "a.py: функция helper на 4 строк вне класса — логика живёт в классе, снаружи только однострочное лицо"],
          f"модуль функций без записи в списке — два нарушения (got {got})")
    check(Package({"a.py": LONG_FUNCTION}).problems({"a.py": "why"}) == [],
          "negative: тот же модуль в списке с причиной — проходит")
    got = Package({"a.py": CLASS_MODULE + "\n\n" + LONG_FUNCTION}).problems()
    check(got == ["a.py: функция helper на 4 строк вне класса — логика живёт в классе, снаружи только однострочное лицо"],
          f"класс есть, но рядом функция длиннее лица — нарушение (got {got})")
    check(Package({"a.py": FACE + CLASS_MODULE}).problems() == [],
          "boundary: лицо в две строки рядом с классом — можно")


def test_the_ratchet():
    CHECKS.section("храповик списка:")
    got = Package({"a.py": CLASS_MODULE}).problems({"gone.py": "why"})
    check(got == ["gone.py: в списке модулей без классов, но файла нет — список врёт"],
          f"строка списка без файла — нарушение (got {got})")
    got = Package({"a.py": CLASS_MODULE}).problems({"a.py": "why"})
    check(got == ["a.py: уже модуль классов — уберите его из FUNCTION_MODULES"],
          f"модуль в списке уже стал классами — его надо убрать из списка (got {got})")
    check(Package({"a.py": CLASS_MODULE + "\n\n" + LONG_FUNCTION}).problems({"a.py": "why"}) == [],
          "negative: модуль в списке с классом и длинной функцией — ещё не классы, строка честная")


def test_abstract_methods():
    CHECKS.section("абстрактные методы:")
    base = ("class Start:\n    def run(self):\n        raise NotImplementedError\n\n\n")
    got = Package({"a.py": base + "class LlamaStart(Start):\n    def other(self):\n        return 1\n"}).problems()
    check(got == ["a.py:6: LlamaStart не реализует run() — у предка он поднимает NotImplementedError"],
          f"подкласс без run() — нарушение с файлом и строкой (got {got})")
    check(Package({"a.py": base + "class LlamaStart(Start):\n    def run(self):\n        return 1\n"}).problems() == [],
          "negative: подкласс со своим run() — проходит")
    middle = "class Middle(Start):\n    def run(self):\n        return 2\n\n\n"
    check(Package({"a.py": base + middle + "class Leaf(Middle):\n    pass\n"}).problems() == [],
          "boundary: run() от промежуточного конкретного класса — наследуется, нарушения нет")
    abstract_middle = "class Middle(Start):\n    def other(self):\n        raise NotImplementedError\n\n\n"
    got = Package({"a.py": base + abstract_middle + "class Leaf(Middle):\n    def other(self):\n        return 3\n"}).problems()
    check(got == ["a.py:11: Leaf не реализует run() — у предка он поднимает NotImplementedError"],
          f"через абстрактного посредника долг не пропадает (got {got})")
    got = Package({"a.py": base, "b.py": "from a import Start\n\n\nclass Far(Start):\n    pass\n"}).problems()
    check(got == ["b.py:4: Far не реализует run() — у предка он поднимает NotImplementedError"],
          f"предок в другом модуле пакета — тоже считается (got {got})")


TESTS = (test_the_real_package, test_mixins, test_function_modules, test_the_ratchet, test_abstract_methods)

for test in TESTS:
    try:
        test()
    except Exception as exc:  # noqa: BLE001 — a crash is a red pin; the rest still runs
        check(False, f"{test.__name__} упал: {exc!r}")

sys.exit(CHECKS.finish())
