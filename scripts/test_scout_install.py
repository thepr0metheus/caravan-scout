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


FAKE_PYTHON = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
if [[ "$1" == "-c" && "$2" == "import faster_whisper" ]]; then
  [[ -f "$FAKE_STATE/installed" ]] && exit 0 || exit 1
fi
if [[ "$1" == "-m" && "$2" == "pip" && "$*" == *faster-whisper* ]]; then touch "$FAKE_STATE/installed"; fi
exit 0
"""


def run_whisper_install(tmp, installed):
    """install-whisper.sh, for real, on a pretend Linux GPU box: a fake venv
    python that writes down every call, fake uname and nvidia-smi, HOME and
    the repo in `tmp`. Returns (exit code, output, the python calls)."""
    repo = tmp / "repo" / "scripts"
    repo.mkdir(parents=True)
    for name in ("install-whisper.sh", "fetch-cell-assets.sh"):
        (repo / name).write_text(read_real(name), encoding="utf-8")
        (repo / name).chmod(0o755)
    fake_bin, venv_bin, state = tmp / "bin", tmp / "venv" / "bin", tmp / "state"
    for d in (fake_bin, venv_bin, state, tmp / "home"):
        d.mkdir(parents=True, exist_ok=True)
    for name, body in (("uname", "#!/usr/bin/env bash\necho Linux\n"),
                       ("nvidia-smi", "#!/usr/bin/env bash\nexit 0\n")):
        (fake_bin / name).write_text(body)
        (fake_bin / name).chmod(0o755)
    (venv_bin / "python").write_text(FAKE_PYTHON)
    (venv_bin / "python").chmod(0o755)
    if installed:
        (state / "installed").touch()
    log = tmp / "calls.log"
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}", "HOME": str(tmp / "home"),
           "VENV": str(tmp / "venv"), "FAKE_LOG": str(log), "FAKE_STATE": str(state)}
    # One door opened on purpose: the script runs against fakes in `tmp` only.
    with patched(subprocess, Popen=REAL["Popen"]):
        done = REAL["run"](["bash", str(repo / "install-whisper.sh")], capture_output=True, text=True,
                           env=env, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return done.returncode, done.stdout + done.stderr, calls


def test_a_rerun_changes_nothing():
    CHECKS.section("повторная установка не трогает работающее (2.1.1):")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        code, out, calls = run_whisper_install(Path(tmp), installed=True)
    pip = [c for c in calls if c.startswith("-m pip")]
    check(code == 0 and pip == [] and "left as it is" in out,
          f"defect-history: venv, где whisper уже импортируется, остаётся как есть — повторный запуск "
          f"обновил cuDNN под работающей ячейкой (pip: {pip})")
    with tempfile.TemporaryDirectory() as tmp:
        code, out, calls = run_whisper_install(Path(tmp), installed=False)
    pip = [c for c in calls if c.startswith("-m pip")]
    check(code == 0 and pip == ["-m pip install -q --upgrade pip",
                                "-m pip install faster-whisper nvidia-cudnn-cu12 nvidia-cublas-cu12"],
          f"negative: пустой venv получает whisper — одна установка, без --upgrade пакетов (pip: {pip}, код {code})")


def test_the_firewall_hint():
    CHECKS.section("подсказка про файрвол называет настоящие порты (2.1.1):")
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    llama = read_real("install-llama.sh")
    check("8180" not in llama and "LLAMA_LAN_SUBNET" not in llama,
          "defect-history: install-llama.sh больше не велит открыть 8180 — порт одиночного llama-server, "
          "на котором ячейки не живут")
    rng = re.search(r'^CELL_RANGE="(\d+):(\d+)"$', installer, re.M)
    check(bool(rng) and f"{rng.group(1)}–{rng.group(2)}" in installer and "${CELL_RANGE}" in installer,
          "install.sh называет диапазон ячеек контроллера и команду, открывающую его контроллеру — "
          "тот же диапазон в словах и в команде")
    sibling = ROOT.parent / "lama-caravan" / "caravan" / "admin" / "paths.py"
    if rng and sibling.exists():
        text = sibling.read_text(encoding="utf-8")
        base = re.search(r'CARAVAN_CELL_BASE_PORT", "(\d+)"', text)
        span = re.search(r'CARAVAN_CELL_PORT_SPAN", "(\d+)"', text)
        want = (int(base.group(1)), int(base.group(1)) + int(span.group(1))) if base and span else None
        check(want == (int(rng.group(1)), int(rng.group(2))),
              f"диапазон тот же, что у контроллера по умолчанию ({want}) — один факт на оба репозитория")


FAKE_FLOCK = r"""#!/usr/bin/env bash
echo "$*" >> "$FLOCK_LOG"
[[ "$FLOCK_ANSWER" == "free" ]]
"""
LOCK_LINE = 'exec 9>"${LLAMA_DIR%/}.caravan-build.lock"'


def run_with_lock(script, action, answer, tmp):
    """`script` for real, with a fake flock that answers busy or free, the
    tree and the archive in `tmp`. Returns (exit code, output, flock calls)."""
    fake_bin = tmp / "bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    (fake_bin / "flock").write_text(FAKE_FLOCK)
    (fake_bin / "flock").chmod(0o755)
    log = tmp / "flock.log"
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}", "HOME": str(tmp / "home"),
           "FLOCK_LOG": str(log), "FLOCK_ANSWER": answer, "LLAMA_BUILDS_DIR": str(tmp / "builds")}
    # One door opened on purpose: the script runs against a tree and an archive in `tmp`.
    with patched(subprocess, Popen=REAL["Popen"]):
        done = REAL["run"](["bash", str(script), "--llama-dir", str(tmp / "llama.cpp"), action],
                           capture_output=True, text=True, env=env, timeout=60)
    return done.returncode, done.stdout + done.stderr, log.read_text().splitlines() if log.exists() else []


def test_one_build_at_a_time():
    CHECKS.section("одна сборка llama.cpp на дерево — общий замок с контроллером (2.2):")
    import tempfile
    script = SCRIPTS / "update-llama.sh"
    with tempfile.TemporaryDirectory() as tmp:
        code, out, calls = run_with_lock(script, "--archive-current", "busy", Path(tmp))
        lock = (Path(tmp) / "llama.cpp.caravan-build.lock").exists()
        inside = (Path(tmp) / "llama.cpp").exists()
    check(code == 75 and calls == ["-n 9"] and "another llama.cpp build or restore is running in" in out,
          f"замок занят — сборка/архив не идут: код 75 и сказано почему (код {code}, flock {calls})")
    check(lock and not inside,
          "файл замка — рядом с деревом, не внутри: свежий git clone не откажется от папки")
    with tempfile.TemporaryDirectory() as tmp:
        code, out, calls = run_with_lock(script, "--list-builds", "busy", Path(tmp))
    check(calls == [] and code != 75, "negative: список сборок ничего не меняет и замка не ждёт")
    with tempfile.TemporaryDirectory() as tmp:
        code, out, calls = run_with_lock(script, "--archive-current", "free", Path(tmp))
    check(calls == ["-n 9"] and code != 75 and "another llama.cpp build" not in out,
          "negative: замок свободен — скрипт идёт дальше")
    sibling = ROOT.parent / "lama-caravan" / "scripts" / "install-llama.sh"
    if sibling.exists():
        check(LOCK_LINE in sibling.read_text(encoding="utf-8") and LOCK_LINE in read_real("update-llama.sh"),
              "тот же файл замка у install-llama.sh контроллера — иначе замки разные и не мешают друг другу")


for fn in (test_the_installer, test_one_command, test_the_rule_can_fail, test_a_rerun_changes_nothing,
           test_the_firewall_hint, test_one_build_at_a_time):
    fn()

sys.exit(CHECKS.finish())
