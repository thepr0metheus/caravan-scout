#!/usr/bin/env python3
"""Snapshot of caravan_scout/driver.py — the NVIDIA driver as the next boot
will meet it (2.19).

Pinned by value on a fake machine: /boot, /proc/driver/nvidia/version, the
library folder and the firmware's SecureBoot variable are temp files,
mokutil, modinfo and dpkg-query are FakeRun answers. What is pinned: each fact read the way the OS writes it, a fact that
cannot be read is None and not a guess, the whole answer is None where there
is no NVIDIA driver to speak of, and the minute's cache.

Run: python3 scripts/test_scout_driver.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks, FakeRun, patched  # noqa: E402

from caravan_scout.driver import DriverFacts  # noqa: E402
from caravan_scout.machine import Machine  # noqa: E402

CHECKS = Checks("scout driver")
check = CHECKS.check

NEXT = "7.0.0-34-generic"
PROC_OPEN = ("NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  610.43.02  Release Build  "
             "(dvs-builder@U22-I3-AE18-23-3)  Thu Aug 14 00:00:00 UTC 2026\nGCC version:  gcc version 13.3.0\n")
PROC_CLOSED = "NVRM version: NVIDIA UNIX x86_64 Kernel Module  580.159  Sun Jul 20 00:00:00 UTC 2026\n"
MOK = "linux Secure Boot Module Signature key"


def same(actual, expected, msg):
    ok = actual == expected
    check(ok, msg)
    if not ok:
        print(f"        got:  {actual!r}\n        want: {expected!r}")


class FakeMachine:
    """/boot, the module's own words and the library folder, in a temp dir."""

    def __init__(self, kernels=("6.17.0-14-generic", "7.0.0-31-generic", NEXT), proc=PROC_OPEN,
                 libraries=("libnvidia-ml.so.610.43.02",), dkms_key=True, efi=True, sb_var=None):
        self.root = Path(tempfile.mkdtemp(prefix="scout-driver-"))
        # No variable by default: the pins below that speak of mokutil read
        # the fallback; the variable's own pins pass its bytes.
        self.efi = self.root / "efi"
        if efi:
            (self.efi / "efivars").mkdir(parents=True)
        self.sb_var = self.efi / "efivars" / "SecureBoot-x"
        if sb_var is not None:
            self.sb_var.write_bytes(sb_var)
        self.key = self.root / "MOK.der"
        if dkms_key:
            self.key.write_bytes(b"der")
        boot = self.root / "boot"
        boot.mkdir()
        for kernel in kernels:
            (boot / f"vmlinuz-{kernel}").write_text("k")
            (boot / f"config-{kernel}").write_text("c")
        if kernels:
            (boot / "vmlinuz").symlink_to(f"vmlinuz-{kernels[-1]}")
            (boot / "vmlinuz.old").symlink_to(f"vmlinuz-{kernels[0]}")
        self.boot = boot
        self.proc = self.root / "version"
        if proc is not None:
            self.proc.write_text(proc)
        lib = self.root / "lib"
        lib.mkdir()
        for name in libraries:
            (lib / name).write_text("l")
        if libraries:
            (lib / "libnvidia-ml.so.1").symlink_to(libraries[0])
        self.lib = lib

    def facts(self, run, platform="linux", running="7.0.0-31-generic", clock=lambda: 1000.0):
        driver = DriverFacts(Machine.run_text, platform=platform, kernel=lambda: running, clock=clock)
        with patched(DriverFacts, BOOT=self.boot, PROC_VERSION=self.proc, LIB_DIRS=(self.root / "none", self.lib),
                     DKMS_KEY=self.key, EFI_DIR=self.efi, SECURE_BOOT_VAR=self.sb_var), patched(subprocess, run=run):
            return driver.facts()


def host_run(signer=MOK, path=f"/lib/modules/{NEXT}/updates/dkms/nvidia.ko.zst", sb="SecureBoot enabled\n",
             dpkg="nvidia-driver-610-open 610.43.02-0ubuntu0.24.04.1 install ok installed\n"
                  "nvidia-driver-590-open 590.48.01-0ubuntu1 deinstall ok config-files\n",
             subject=f"subject=CN = {MOK}\n", test_key="MOK.der is not enrolled\n"):
    table = {("mokutil", "--sb-state"): (0, sb),
             ("mokutil", "--test-key"): (0, test_key),
             ("openssl", "x509"): (0, subject),
             ("dpkg-query", "-W"): (0, dpkg)}
    if path is not None:
        table[("modinfo", "-k", NEXT, "-n", "nvidia")] = (0, path + "\n")
        table[("modinfo", "-k", NEXT, "-F", "version", "nvidia")] = (0, "610.43.02\n")
        table[("modinfo", "-k", NEXT, "-F", "signer", "nvidia")] = (0, signer + "\n")
    else:
        table[("modinfo", "-k", NEXT, "-n", "nvidia")] = (1, "modinfo: ERROR: Module nvidia not found.\n")
    return FakeRun(table)


def test_the_morning_before():
    CHECKS.section("утро перед сбоем 2026-09-26:")
    got = FakeMachine().facts(host_run())
    same(got, {"secureBoot": True, "kernelRunning": "7.0.0-31-generic", "kernelNext": NEXT, "loaded": "610.43.02",
               "installed": "610.43.02",
               "nextModule": {"path": f"/lib/modules/{NEXT}/updates/dkms/nvidia.ko.zst", "version": "610.43.02",
                              "signer": MOK},
               "dkmsKey": {"signer": MOK, "enrolled": False},
               "package": "nvidia-driver-610-open"},
         "Secure Boot включён, следующее ядро — новое, и у него только сборка DKMS, подписанная ключом машины, "
         "которого прошивка не знает: ровно то, что утром можно было сказать")


def test_each_fact():
    CHECKS.section("каждый факт — как ОС его пишет:")
    signed = FakeMachine().facts(host_run(signer="Canonical Ltd. Kernel Module Signing",
                                          path=f"/lib/modules/{NEXT}/kernel/nvidia-610-open/nvidia.ko"))
    same(signed["nextModule"], {"path": f"/lib/modules/{NEXT}/kernel/nvidia-610-open/nvidia.ko", "version": "610.43.02",
                                "signer": "Canonical Ltd. Kernel Module Signing"},
         "подписанный модуль: путь в kernel/ и подписант — как говорит modinfo")
    same(FakeMachine(kernels=("7.0.0-9-generic", "7.0.0-31-generic")).facts(host_run())["kernelNext"], "7.0.0-31-generic",
         "следующее ядро — новейшее по номеру, а не по алфавиту (31 > 9); ссылки vmlinuz и vmlinuz.old не в счёт")
    same([FakeMachine().facts(host_run(sb=answer))["secureBoot"]
          for answer in ("SecureBoot disabled\n", "EFI variables are not supported on this system\n", "")],
         [False, None, None],
         "Secure Boot выключен — False; mokutil не может сказать или молчит — None, а не «выключен»")
    same(FakeMachine(proc=PROC_CLOSED).facts(host_run())["loaded"], "580.159",
         "загруженный модуль закрытой сборки говорит о себе иначе — версия всё равно читается")
    same(FakeMachine(libraries=("libnvidia-ml.so.580.159", "libnvidia-ml.so.610.43.02")).facts(host_run())["installed"],
         "610.43.02", "установленная библиотека — самая новая из лежащих")
    same(FakeMachine().facts(host_run(path=None))["nextModule"], None,
         "у следующего ядра модуля nvidia нет — None")
    same(FakeMachine().facts(host_run(dpkg="nvidia-driver-610-open 610.43.02-0ubuntu0.24.04.1 deinstall ok config-files\n"))
         ["package"], None, "пакет драйвера снят (остались настройки) — None")
    same([FakeMachine().facts(host_run(test_key=answer))["dkmsKey"]["enrolled"]
          for answer in ("MOK.der is already enrolled\n", "MOK.der is not enrolled\n", "")],
         [True, False, None], "ключ DKMS записан в прошивку — True, нет — False, mokutil молчит — None")
    same(FakeMachine().facts(host_run(subject="subject=C = GB, O = Machine, CN = box-a signing key, emailAddress = x\n"))
         ["dkmsKey"]["signer"], "box-a signing key", "имя ключа — его CN, среди других частей имени")
    same(FakeMachine().facts(host_run(subject=""))["dkmsKey"], {"signer": None, "enrolled": False},
         "openssl не прочёл ключ — имени нет (None), а записан ли он, всё равно сказано")
    same(FakeMachine(dkms_key=False).facts(host_run())["dkmsKey"], None, "ключа DKMS на машине нет — None")


ON, OFF = b"\x06\x00\x00\x00\x01", b"\x06\x00\x00\x00\x00"


def asked(run, *prefix):
    return any(call[:len(prefix)] == list(prefix) for call in run.calls)


def test_the_firmware():
    CHECKS.section("Secure Boot — слово прошивки (2.19.1):")
    run = host_run(sb="")
    same(FakeMachine(sb_var=ON).facts(run)["secureBoot"], True, "переменная прошивки говорит 1 — включён")
    check(not asked(run, "mokutil", "--sb-state"), "negative: переменная прочитана — mokutil не спрашивается")
    run = host_run(sb="SecureBoot enabled\n")
    same(FakeMachine(sb_var=OFF).facts(run)["secureBoot"], False,
         "переменная говорит 0 — выключен, что бы ни сказал mokutil (машина без mokutil была «неизвестно», "
         "а прошивка говорила «выключен»)")
    same(FakeMachine(sb_var=b"\x06\x00").facts(host_run(sb="SecureBoot enabled\n"))["secureBoot"], True,
         "boundary: переменная не по форме (не 5 байт, последний байт 0) — не читается как «выключен», "
         "говорит mokutil")
    same(FakeMachine(sb_var=b"\x06\x00\x00\x00\x02").facts(host_run(sb=""))["secureBoot"], None,
         "boundary: в переменной не 0 и не 1, а mokutil молчит — None, а не догадка")
    run = host_run(sb="SecureBoot enabled\n")
    same(FakeMachine(efi=False).facts(run)["secureBoot"], False,
         "машина загрузилась не через EFI — Secure Boot'а у неё нет вовсе (False), mokutil не спрашивается")
    check(not asked(run, "mokutil", "--sb-state"), "negative: без EFI mokutil не спрашивается")


def test_the_package():
    CHECKS.section("пакет драйвера — тот, чья версия у библиотеки (2.19.1):")
    two = ("nvidia-driver-550 550.163.01-0ubuntu0.24.04.2 install ok installed\n"
           "nvidia-driver-580 580.173.02-0ubuntu0.24.04.1 install ok installed\n"
           "nvidia-driver-binary  unknown ok not-installed\n")
    same(FakeMachine(libraries=("libnvidia-ml.so.580.173.02",)).facts(host_run(dpkg=two))["package"], "nvidia-driver-580",
         "установлены 550 и 580, библиотека 580.173.02 — пакет 580, а не первый в списке")
    same(FakeMachine(libraries=("libnvidia-ml.so.610.43.02",)).facts(host_run(dpkg=two))["package"], None,
         "negative: ни одного пакета той версии, что у библиотеки, — None, а не первый попавшийся")
    same(FakeMachine(libraries=("libnvidia-ml.so.580.173",)).facts(host_run(dpkg=two))["package"], None,
         "boundary: версия библиотеки — лишь начало версии пакета (580.173 и 580.173.02) — не она")
    same(FakeMachine(libraries=("libnvidia-ml.so.580.173.02",)).facts(
        host_run(dpkg="nvidia-driver-580 1:580.173.02-1 install ok installed\n"))["package"], "nvidia-driver-580",
         "версия пакета с эпохой (1:…) — сравнивается без неё")
    run = host_run()
    got = FakeMachine(libraries=()).facts(run)
    check(got["package"] is None and not asked(run, "dpkg-query"),
          "библиотеки нет — версии не с чем сравнить: None, и dpkg не спрашивается")


def test_nothing_to_say():
    CHECKS.section("где сказать нечего — None:")
    run = host_run()
    same(FakeMachine().facts(run, platform="darwin"), None, "не Linux — None")
    check(run.calls == [], "negative: не Linux — ни одного процесса не запущено")
    same(FakeMachine(proc=None, libraries=()).facts(host_run(path=None)), None,
         "нет ни загруженного модуля, ни библиотеки, ни модуля у следующего ядра — драйвера NVIDIA нет, None")
    lone = FakeMachine(proc=None, libraries=()).facts(host_run())
    check(lone is not None and lone["loaded"] is None and lone["installed"] is None,
          "только модуль у следующего ядра — ответ есть, а загруженного и установленного — None")
    same(FakeMachine(proc="garbage\n").facts(host_run())["loaded"], None,
         "negative: файл модуля не по форме — None, а не мусорная версия")
    same(FakeMachine(kernels=()).facts(host_run())["kernelNext"], None,
         "в /boot нет ядер — следующего ядра нет (None), и модуль у него не спрашивается")


def test_the_minute():
    CHECKS.section("кэш на минуту:")
    machine = FakeMachine()
    now = [1000.0]
    driver = DriverFacts(Machine.run_text, platform="linux", kernel=lambda: "7.0.0-31-generic", clock=lambda: now[0])
    run = host_run()
    with patched(DriverFacts, BOOT=machine.boot, PROC_VERSION=machine.proc, LIB_DIRS=(machine.lib,),
                 EFI_DIR=machine.efi, SECURE_BOOT_VAR=machine.sb_var), patched(subprocess, run=run):
        first = driver.facts()
        calls = len(run.calls)
        now[0] += DriverFacts.TTL - 1
        second = driver.facts()
        same_calls = len(run.calls) == calls
        now[0] += 2
        driver.facts()
    check(second is first and same_calls, "в пределах минуты — тот же ответ и ни одного нового процесса")
    check(len(run.calls) > calls, "через минуту — читается заново")


if __name__ == "__main__":
    test_the_morning_before()
    test_each_fact()
    test_the_firmware()
    test_the_package()
    test_nothing_to_say()
    test_the_minute()
    sys.exit(CHECKS.finish())
