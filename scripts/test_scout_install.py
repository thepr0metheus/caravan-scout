#!/usr/bin/env python3
"""The installer sources only helpers that are safe to source.

install.sh (then scripts/install.sh) sourced install-whisper.sh and then called install_whisper. That
helper is a script of its own: it defines no such function, and it exits
early on a host with no NVIDIA GPU. Sourced, its `exit 0` ended the installer
before its summary; on a GPU host the missing function failed the install at
its very end, under `set -e`. Nothing caught it: an installer runs once per
machine, and each run looked like a machine-specific hiccup.

The rule, checked by reading the scripts:
1. a helper that install.sh sources defines the function called right after;
2. a sourced helper that can `exit` keeps its own run behind a BASH_SOURCE
   check — otherwise its exit ends the installer.

Run: python3 scripts/test_scout_install.py
"""
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import REAL, Checks, patched  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
CHECKS = Checks("scout install")
check = CHECKS.check

SOURCE_RE = re.compile(r'^\s*(?:source|\.)\s+"?\$INSTALL_DIR/scripts/([\w.-]+)"?')


def installer_problems(installer, read_helper):
    """What breaks the rule in `installer` (its text), reading helpers through
    `read_helper(name) -> text`. An empty list when nothing does."""
    problems = []
    lines = installer.splitlines()
    for i, line in enumerate(lines):
        m = SOURCE_RE.match(line)
        if not m:
            continue
        helper = m.group(1)
        text = read_helper(helper)
        if re.search(r"^\s*exit\b", text, re.M) and "BASH_SOURCE" not in text:
            problems.append(f"{helper}: sourced, but it exits on its own — run it with bash instead")
        for after in lines[i + 1:]:
            stripped = after.strip()
            if not stripped or stripped.startswith("#"):
                continue
            called = stripped.split()[0]
            if not re.search(rf"^{re.escape(called)}\s*\(\)", text, re.M):
                problems.append(f"{helper}: sourced, then {called} is called, which it does not define")
            break
    return problems


def read_real(name):
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_the_installer():
    CHECKS.section("установщик подключает только безопасные помощники:")
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    check(installer_problems(installer, read_real) == [],
          "install.sh: подключает install-llama.sh, тот определяет install_llama и прячет свой запуск за BASH_SOURCE")
    check('bash "$INSTALL_DIR/scripts/install-whisper.sh"' in installer,
          "install-whisper.sh запускается отдельным процессом")


def test_one_command():
    CHECKS.section("установка одной командой, сопряжение — с контроллера (2.1):")
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    uninstaller = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    for name in ("install.sh", "uninstall.sh"):
        path = ROOT / name
        # One door opened on purpose: `bash -n` parses the script and runs nothing.
        with patched(subprocess, Popen=REAL["Popen"]):
            syntax = REAL["run"](["bash", "-n", str(path)], capture_output=True, text=True)
        check(syntax.returncode == 0 and os.access(path, os.X_OK),
              f"{name} в корне репозитория: исполняемый, bash его разбирает ({syntax.stderr.strip()[:80]})")
    check(not (SCRIPTS / "install.sh").exists(), "negative: второго установщика в scripts/ нет — один путь")
    check("--admin-url" not in installer and "ADMIN_URL" not in installer
          and not re.search(r"controllerUrl['\"]\]\s*=", installer),
          "negative: адрес контроллера установщик не спрашивает и не пишет — сопрягает контроллер")
    check("systemctl --user restart caravan-scout.service" in installer and "enable-linger" in installer,
          "служба запущена сразу и переживает выход из системы")
    check("/api/pairing" in installer and "Add scout" in installer,
          "в конце — адрес и порт от самого скаута и где их ввести: Model servers → ＋ Add scout")
    stop_at, disable_at = uninstaller.find("/api/llama-node/stop"), uninstaller.find("disable --now")
    check(0 <= stop_at < disable_at,
          "удаление сначала останавливает ячейки через скаут, потом службу — llama-server не остаётся сиротой на GPU")
    check("X-Caravan-Token" in uninstaller and "print(token" not in uninstaller.replace(" ", ""),
          "токен флота для остановки берётся из config.json в заголовок и не печатается")


def test_the_rule_can_fail():
    CHECKS.section("правило краснеет на прежнем тексте:")
    old = ('if on_linux; then\n'
           '  # shellcheck source=scripts/install-whisper.sh\n'
           '  source "$INSTALL_DIR/scripts/install-whisper.sh"\n'
           '  install_whisper "$INSTALL_DIR" "${HOME}/wsr"\n'
           'fi\n')
    found = installer_problems(old, read_real)
    check(found == ["install-whisper.sh: sourced, but it exits on its own — run it with bash instead",
                    "install-whisper.sh: sourced, then install_whisper is called, which it does not define"],
          f"defect-history: прежний вызов — оба нарушения названы (got {found})")
    guarded = ('install_x() { :; }\nif [[ "${BASH_SOURCE[0]}" == "$0" ]]; then\n  exit 1\nfi\n')
    ok = 'source "$INSTALL_DIR/scripts/x.sh"\ninstall_x a b\n'
    check(installer_problems(ok, lambda _name: guarded) == [],
          "negative: помощник с функцией и запуском за BASH_SOURCE — нарушения нет")


for fn in (test_the_installer, test_one_command, test_the_rule_can_fail):
    fn()

sys.exit(CHECKS.finish())
