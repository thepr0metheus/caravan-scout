#!/usr/bin/env python3
"""Snapshot of caravan_scout/driver_packages.py and of the machine's hands the
controller now asks for (2.24): the driver packages and their install, the
busiest processes, a btop frame.

Pinned by value on a fake machine: nvidia-smi, dpkg-query, apt-cache, ps,
btop and top are scripted answers, /proc/modules, /etc/os-release and the
reboot markers are temp files. What is pinned: each fact read the way the
tool prints it, a fact that cannot be read said so rather than guessed, the
names apt may get, and the routes the controller calls.

Run: python3 scripts/test_scout_driver_packages.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks, FakeRun, Served, make_scout, patched  # noqa: E402

from caravan_scout.driver_packages import DriverPackages  # noqa: E402
from caravan_scout.errors import AppError  # noqa: E402

CHECKS = Checks("scout driver packages")
check = CHECKS.check


def same(actual, expected, msg):
    ok = actual == expected
    check(ok, msg)
    if not ok:
        print(f"        got:  {actual!r}\n        want: {expected!r}")


class Script:
    """subprocess.run for DriverPackages: argv prefix -> (rc, stdout, stderr)."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        best = max((prefix for prefix in self.table if tuple(cmd[:len(prefix)]) == prefix), key=len, default=None)
        if best is None:
            raise FileNotFoundError(cmd[0])
        answer = self.table[best]
        rc, out, err = answer(list(cmd)) if callable(answer) else answer
        return subprocess.CompletedProcess(list(cmd), rc, stdout=out, stderr=err)


DPKG = ("nvidia-driver-580 580.95.05-0ubuntu1 install ok installed\n"
        "nvidia-driver-610-open 610.43.02-0ubuntu0.24.04.1 install ok installed\n"
        "nvidia-driver-595 595.84-0ubuntu1 deinstall ok not-installed\n"
        "nvidia-driver-libs-only 1.0 install ok installed\n")
SEARCH = ("nvidia-driver-610 - NVIDIA driver metapackage\n"
          "nvidia-driver-610-open - NVIDIA driver (open kernel) metapackage\n"
          "nvidia-driver-620-open - NVIDIA driver (open kernel) metapackage\n"
          "nvidia-driver-620-server - not on the allowlist\n")
# What apt-cache policy would print for each package — only the ones asked
# for are printed, as apt does: a name off the allowlist that reaches the
# question comes back as a candidate too.
CANDIDATES = {"nvidia-driver-610": "610.43.02-0ubuntu0.24.04.1", "nvidia-driver-610-open": "610.43.02-0ubuntu0.24.04.1",
              "nvidia-driver-620-open": "620.10-0ubuntu1", "nvidia-driver-620-server": "620.10-0ubuntu1"}


def policy(cmd):
    return 0, "".join(f"{name}:\n  Installed: (none)\n  Candidate: {CANDIDATES[name]}\n"
                      for name in cmd[2:] if name in CANDIDATES), ""
KERNEL = "7.0.0-31-generic"
MODULES_POLICY = ("linux-modules-nvidia-610-open-generic-hwe-24.04:\n  Installed: (none)\n  Candidate: 6.8.0-40.40~22.04.1\n"
                  "linux-modules-nvidia-610-open-generic:\n  Installed: (none)\n  Candidate: 7.0.0-31.31\n"
                  f"linux-modules-nvidia-610-open-{KERNEL}:\n  Installed: (none)\n  Candidate: (none)\n")


def machine(secure=False, nvsmi=(0, "610.43.02\n", ""), reboot=None, loaded=True, dpkg=DPKG, modules_installed=False):
    root = Path(tempfile.mkdtemp(prefix="drvpkg-"))
    (root / "os-release").write_text('NAME="Ubuntu"\nVERSION_ID="24.04"\n')
    (root / "modules").write_text(("nvidia_uvm 1 0 - Live 0x0\nnvidia 2 1 nvidia_uvm, Live 0x0\n" if loaded
                                   else "snd 1 0 - Live 0x0\n"))
    marker = root / "reboot-required"
    if reboot is not None:
        marker.write_text("*** System restart required ***\n")
        if reboot:
            Path(f"{marker}.pkgs").write_text("\n".join(reboot) + "\n")
    signed_dpkg = (0, "linux-modules-nvidia-610-open-generic 7.0.0-31.31 install ok installed\n" if modules_installed else "", "")
    run = Script({
        ("nvidia-smi",): nvsmi,
        ("dpkg-query", "-W", "-f=${Package} ${Version} ${Status}\\n", "nvidia-driver-*"): (0, dpkg, ""),
        ("dpkg-query", "-W", "-f=${Package} ${Version} ${Status}\\n", "linux-modules-nvidia-610-open-generic"): signed_dpkg,
        ("apt-cache", "--names-only", "search"): (0, SEARCH, ""),
        ("apt-cache", "policy", "nvidia-driver-610"): policy,
        ("apt-cache", "policy", "linux-modules-nvidia-610-open-generic-hwe-24.04"): (0, MODULES_POLICY, ""),
    })
    packages = DriverPackages(secure_boot=lambda: secure, run=run, kernel=lambda: KERNEL, clock=lambda: 1_790_000_000,
                              reboot_marker=marker, os_release=root / "os-release", proc_modules=root / "modules")
    return packages, run


def test_running():
    CHECKS.section("версия в ядре")
    packages, _ = machine()
    same(packages.running(), ("610.43.02", ""), "nvidia-smi назвал число — это версия")
    packages, _ = machine(nvsmi=(18, "Failed to initialize NVML: Driver/library version mismatch\nNVML library version: 610.43\n", ""))
    same(packages.running(), ("", "Failed to initialize NVML: Driver/library version mismatch NVML library version: 610.43"),
         "после установки до перезагрузки — не число, а причина словами")
    packages, _ = machine(nvsmi=(9, "", "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver"))
    same(packages.running()[1], "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver",
         "причина из stderr тоже не теряется")


def test_packages():
    CHECKS.section("пакеты")
    packages, _ = machine()
    same(packages.installed(), [{"package": "nvidia-driver-610-open", "version": "610.43.02-0ubuntu0.24.04.1"},
                                {"package": "nvidia-driver-580", "version": "580.95.05-0ubuntu1"}],
         "установленные: только installed и только по белому списку, новые первыми")
    same(packages.available(), [{"package": "nvidia-driver-620-open", "version": "620.10-0ubuntu1"},
                                {"package": "nvidia-driver-610", "version": "610.43.02-0ubuntu0.24.04.1"},
                                {"package": "nvidia-driver-610-open", "version": "610.43.02-0ubuntu0.24.04.1"}],
         "доступные: кандидаты apt-cache policy, -server не по списку не берётся")
    same(packages.signed_modules("nvidia-driver-610-open"), "linux-modules-nvidia-610-open-generic",
         "подписанные модули — того пакета, чей кандидат из серии ядра 7.0.0-31, а не hwe чужой серии")
    same(packages.signed_modules("rm -rf /"), "", "чужое имя — никаких модулей")


def test_reboot_and_module():
    CHECKS.section("перезагрузка и модуль")
    packages, _ = machine()
    same(packages.reboot_pending(), (False, []), "метки нет — перезагрузка не нужна")
    packages, _ = machine(reboot=["linux-image-7.0.0-34-generic", "libc6"])
    same(packages.reboot_pending(), (True, ["linux-image-7.0.0-34-generic", "libc6"]), "метка ОС со списком пакетов")
    packages, _ = machine(reboot=[])
    same(packages.reboot_pending(), (True, []), "метка без списка — нужна, причина неизвестна")
    check(machine(loaded=True)[0].module_loaded() and not machine(loaded=False)[0].module_loaded(),
          "модуль nvidia — по /proc/modules, первое слово строки")


def test_facts():
    CHECKS.section("ответ целиком")
    packages, _ = machine(secure=True, modules_installed=True, reboot=["libc6"])
    facts = packages.facts()
    same({k: facts[k] for k in ("ok", "running", "runningError", "secureBoot", "moduleLoaded", "signedModules",
                                "signedModulesInstalled", "rebootPending", "rebootPendingPackages", "kernel", "checkedAt")},
         {"ok": True, "running": "610.43.02", "runningError": "", "secureBoot": True, "moduleLoaded": True,
          "signedModules": "linux-modules-nvidia-610-open-generic", "signedModulesInstalled": True,
          "rebootPending": True, "rebootPendingPackages": ["libc6"], "kernel": KERNEL, "checkedAt": 1_790_000_000},
         "все факты, из которых решает панель контроллера")
    same(len(facts["installed"]), 2, "и оба списка пакетов")


def test_install_command():
    CHECKS.section("установка")
    packages, _ = machine()
    for bad, status in (("nvidia-driver-610; rm -rf /", 400), ("libnvidia-gl-610", 400), ("nvidia-driver-999", 404)):
        try:
            packages.install_command(bad)
            got = None
        except AppError as exc:
            got = exc.status
        same(got, status, f"{bad!r} — отказ {status}")
    same(packages.install_command("nvidia-driver-620-open"),
         ["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "--no-install-recommends",
          "nvidia-driver-620-open"], "без Secure Boot — один apt-get")
    secure, _ = machine(secure=True)
    cmd = secure.install_command("nvidia-driver-610-open")
    check(cmd[:4] == ["sudo", "-n", "bash", "-c"], "с Secure Boot — один скрипт под sudo")
    check("nvidia-driver-610-open linux-modules-nvidia-610-open-generic &&" in cmd[4],
          "драйвер и подписанные модули одной установкой")
    check(f"dkms remove \"$m\" -k {KERNEL}" in cmd[4] and cmd[4].endswith(f"depmod -a {KERNEL}"),
          "сборка DKMS для этого ядра убирается, зависимости пересчитываются")


class FakeJob:
    def __init__(self):
        self.started = []

    def start(self, cmd, tag, env):
        self.started.append((cmd, tag))
        return {"running": True, "tag": tag}


def test_install():
    packages, _ = machine()
    job = FakeJob()
    got = packages.install("nvidia-driver-620-open", job)
    same((got, job.started[0][1]), ({"running": True, "tag": "driver:nvidia-driver-620-open"}, "driver:nvidia-driver-620-open"),
         "установка уходит в фоновое задание машины с меткой пакета")


def test_machine_hands():
    CHECKS.section("процессы и снимок btop")
    scout = make_scout()
    ps = ("  PID COMMAND         USER     %CPU %MEM   RSS\n"
          " 4242 llama-server    skynet   95.5 12.0 1048576\n"
          "  777 python3         skynet    3.1  0.4 20480\n"
          "  bad row\n")
    with patched(subprocess, run=FakeRun({("ps", "-eo"): (0, ps)})):
        got = scout.machine.top_processes()
    same(got["processes"], [{"pid": 4242, "name": "llama-server", "user": "skynet", "cpuPct": 95.5, "memPct": 12.0,
                             "rssMiB": 1024.0},
                            {"pid": 777, "name": "python3", "user": "skynet", "cpuPct": 3.1, "memPct": 0.4, "rssMiB": 20.0}],
         "процессы по нагрузке, плохая строка пропущена")
    with patched(subprocess, run=FakeRun({})):
        none = scout.machine.top_processes()
    same((none["ok"], none["processes"]), (False, None), "ps не ответил — None, а не пустая машина")
    with patched(subprocess, run=FakeRun({("timeout",): (0, "\x1b[1mbtop frame"), ("bash", "-lc"): (0, "top table")})):
        snap = scout.machine.btop_snapshot()
    same({k: snap[k] for k in ("kind", "ok", "frame", "top")},
         {"kind": "btop", "ok": True, "frame": "\x1b[1mbtop frame", "top": "top table"},
         "кадр btop как нарисован и таблица top рядом")


def test_routes():
    CHECKS.section("маршруты")
    scout = make_scout()
    fake, _ = machine()
    job = FakeJob()
    with patched(scout, driver_packages=fake, driver_install=job), Served(scout) as srv:
        status, body = srv.get("/api/host/driver")
        same((status, body.get("running"), len(body.get("available") or [])), (200, "610.43.02", 3), "GET /api/host/driver — факты")
        status, body = srv.post("/api/host/driver/install", {"package": "nvidia-driver-620-open"})
        same((status, body.get("tag")), (200, "driver:nvidia-driver-620-open"), "POST /api/host/driver/install — задание")
        status, body = srv.post("/api/host/driver/install", {"package": "evil; rm -rf /"})
        same(status, 400, "чужое имя пакета через HTTP — 400")


for fn in (test_running, test_packages, test_reboot_and_module, test_facts, test_install_command, test_install,
           test_machine_hands, test_routes):
    fn()
sys.exit(CHECKS.finish())
