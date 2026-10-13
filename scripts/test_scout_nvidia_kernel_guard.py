#!/usr/bin/env python3
"""Snapshot of scripts/nvidia_kernel_guard.py — the card kept through kernel updates.

Pinned by value on a fake machine: dpkg-query, apt-cache, apt-get,
grub-editenv and grub-set-default are answers from a table; /lib/modules,
the Secure Boot variable, grub.cfg and /etc/default/grub are a dict. What is
pinned: the driver series and flavour read from the packages; a kernel
counts only with the module of THAT series (a 610 module under 615 libraries
is no card); the missing signed modules are fetched in one apt run with the
conffile options; GRUB is pointed at the newest kernel with the card, written
only on a change; and the day it was written for — 2026-10-09, kernel
7.0.0-38 installed while its modules were not — ends with the card.

Run: python3 scripts/test_scout_nvidia_kernel_guard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks  # noqa: E402

import nvidia_kernel_guard as g  # noqa: E402

CHECKS = Checks("nvidia kernel guard")
check = CHECKS.check

UUID = "0f2c1a7e-1111-4222-8333-944455556666"
SECURE_ON = b"\x06\x00\x00\x00\x01"
SECURE_OFF = b"\x06\x00\x00\x00\x00"


def grub_cfg(*kernels):
    """The menu Ubuntu's 10_linux writes: the newest first, then the submenu."""
    lines = [f"menuentry 'Ubuntu' --class ubuntu --class gnu-linux $menuentry_id_option 'gnulinux-simple-{UUID}' {{", "}",
             f"submenu 'Advanced options for Ubuntu' $menuentry_id_option 'gnulinux-advanced-{UUID}' {{"]
    for k in kernels:
        lines += [f"\tmenuentry 'Ubuntu, with Linux {k}' --class ubuntu $menuentry_id_option 'gnulinux-{k}-advanced-{UUID}' {{", "\t}",
                  f"\tmenuentry 'Ubuntu, with Linux {k} (recovery mode)' $menuentry_id_option 'gnulinux-{k}-recovery-{UUID}' {{", "\t}"]
    return "\n".join(lines + ["}"]) + "\n"


def entry(kernel):
    return f"gnulinux-advanced-{UUID}>gnulinux-{kernel}-advanced-{UUID}"


class FakeHost:
    """A machine as a table: what is installed, what apt offers, what lies on disk."""

    def __init__(self, installed=(), offered=(), files=None, secure=SECURE_ON, saved=None, grub_default="saved",
                 apt_code=0, set_code=0):
        self.installed = {name: "ii" for name in installed}
        self.offered = set(offered)
        self.files = dict(files or {})
        if secure is not None:
            self.files[g.SecureBoot.VAR] = secure
            self.files["/sys/firmware/efi"] = ""
        if grub_default is not None:
            self.files[g.Grub.DEFAULTS] = f'GRUB_TIMEOUT=0\nGRUB_DEFAULT={grub_default}\n'
        self.saved = saved
        self.apt_code, self.set_code = apt_code, set_code
        self.ran, self.said = [], []

    def rc(self, *names):
        for name in names:
            self.installed[name] = "rc"
        return self

    # the Host interface
    def run(self, cmd, env=None):
        self.ran.append((list(cmd), env))
        tool = cmd[0]
        if tool == "dpkg-query":
            import fnmatch
            pats = cmd[4:]
            rows = [f"{st}  {n}" for n, st in sorted(self.installed.items()) if any(fnmatch.fnmatch(n, p) for p in pats)]
            return (0 if rows else 1), "\n".join(rows) + ("\n" if rows else "dpkg-query: no packages found matching\n")
        if tool == "apt-cache":
            name = cmd[2]
            cand = "1.0" if name in self.offered else "(none)"
            return 0, f"{name}:\n  Installed: (none)\n  Candidate: {cand}\n"
        if tool == "apt-get":
            if self.apt_code == 0:
                for name in cmd:
                    if name.startswith("linux-modules-nvidia-"):
                        self.installed[name] = "ii"
                        series, kernel = self._split(name)
                        self.files[f"/lib/modules/{kernel}/kernel/nvidia-{series}/nvidia.ko"] = "signed"
            return self.apt_code, "" if self.apt_code == 0 else "E: Unable to locate package\n"
        if tool == "grub-editenv":
            return 0, f"saved_entry={self.saved}\n" if self.saved is not None else "next_entry=\n"
        if tool == "grub-set-default":
            if self.set_code == 0:
                self.saved = cmd[1]
            return self.set_code, ""
        if tool == "logger":
            return 0, ""
        raise AssertionError(f"the guard ran something the table does not know: {cmd}")

    @staticmethod
    def _split(package):
        """linux-modules-nvidia-615-open-7.0.0-38-generic → ('615-open', '7.0.0-38-generic')"""
        rest = package[len("linux-modules-nvidia-"):]
        series = rest.split("-")[0]
        rest = rest[len(series) + 1:]
        if rest.startswith("open-"):
            return f"{series}-open", rest[len("open-"):]
        return series, rest

    def exists(self, path):
        return path in self.files

    def read(self, path):
        value = self.files.get(path)
        return value if isinstance(value, str) else None

    def read_bytes(self, path):
        value = self.files.get(path)
        return value if isinstance(value, bytes) else None

    def say(self, text):
        self.said.append(text)

    # what the pins read
    def applied(self):
        return [cmd for cmd, _env in self.ran if cmd[0] in ("apt-get", "grub-set-default")]


def driver_615(*kernels_with_module, extra=()):
    """A machine after the move to 615: both commons installed, the 610 ones transitional."""
    installed = ["nvidia-kernel-common-610", "nvidia-kernel-common-615", "nvidia-driver-610-open", "nvidia-driver-615-open",
                 "nvidia-kernel-source-615-open", "linux-image-7.0.0-34-generic", "linux-image-7.0.0-38-generic",
                 "linux-image-generic-hwe-24.04", *extra]
    files = {f"/lib/modules/{k}/kernel/nvidia-615-open/nvidia.ko": "signed" for k in kernels_with_module}
    files[g.Grub.CFG] = grub_cfg("7.0.0-38-generic", "7.0.0-34-generic")
    return installed, files


def test_what_the_machine_has():
    CHECKS.section("серия драйвера и ядра — из пакетов:")
    installed, files = driver_615()
    host = FakeHost(installed + ["linux-image-6.17.0-29-generic"], files=files).rc("linux-image-6.17.0-29-generic")
    driver = g.Driver.of(g.Packages(host))
    check((driver.series, driver.open_modules, driver.name) == (615, True, "615-open"),
          "после перехода серия — старшая из nvidia-kernel-common (615), открытые модули")
    check(driver.modules_package("7.0.0-38-generic") == "linux-modules-nvidia-615-open-7.0.0-38-generic"
          and driver.module_path("7.0.0-38-generic") == "/lib/modules/7.0.0-38-generic/kernel/nvidia-615-open/nvidia.ko",
          "имя пакета подписанных модулей и путь модуля — той же серии")
    check(g.Kernels(g.Packages(host)).all() == ["7.0.0-38-generic", "7.0.0-34-generic"],
          "ядра — новое первым; negative: метапакет и снятое ядро (rc) не ядра")
    closed = FakeHost(["nvidia-kernel-common-580", "nvidia-driver-580"])
    check(g.Driver.of(g.Packages(closed)).name == "580", "negative: закрытые модули — без -open")
    check(g.Driver.of(g.Packages(FakeHost(["linux-image-7.0.0-38-generic"]))) is None,
          "negative: драйвера NVIDIA нет — серии нет, а не выдуманная")
    check(g.Version.key("7.0.0-38-generic") > g.Version.key("7.0.0-4-generic"), "версии сравниваются числами: 38 позже 4")


def test_which_kernels_carry_the_card():
    CHECKS.section("ядро с картой — только с модулем этой серии:")
    installed, files = driver_615("7.0.0-38-generic")
    files["/lib/modules/7.0.0-34-generic/kernel/nvidia-610-open/nvidia.ko"] = "signed"
    plan = g.Guard(FakeHost(installed, files=files)).plan()
    check(plan["good"] == ["7.0.0-38-generic"] and plan["target"] == "7.0.0-38-generic" and plan["newest"] == "7.0.0-38-generic",
          "у 7.0.0-38 модуль 615 есть — карта будет")
    check("7.0.0-34-generic" not in plan["good"],
          "negative: модуль 610 под библиотеками 615 — не карта (несовпадение версий драйвера)")
    files.pop("/lib/modules/7.0.0-38-generic/kernel/nvidia-615-open/nvidia.ko")
    files["/lib/modules/7.0.0-38-generic/updates/dkms/nvidia.ko"] = "dkms"
    check(g.Guard(FakeHost(installed, files=files)).plan()["good"] == [],
          "negative: сборка DKMS при Secure Boot — не карта: ядро её отвергнет")
    check(g.Guard(FakeHost(installed, files=files, secure=SECURE_OFF)).plan()["good"] == ["7.0.0-38-generic"],
          "Secure Boot выключен — сборка DKMS грузится, это карта")
    unread = g.Guard(FakeHost(installed, files=files, secure=b"\x06"))
    check(unread.plan()["secureBoot"] is None and unread.plan()["good"] == [],
          "negative: Secure Boot не прочитан — DKMS не в счёт, только подписанный")


def test_boot():
    CHECKS.section("GRUB — на самое новое ядро с картой:")
    installed, files = driver_615("7.0.0-38-generic", "7.0.0-34-generic")
    host = FakeHost(installed, files=files)
    check(g.Guard(host).boot() == 0 and host.applied() == [] and host.said == [],
          "у нового ядра модуль есть, сохранённого пункта нет — GRUB и так грузит первое: ничего не пишется")
    host = FakeHost(installed, files=files, saved=entry("7.0.0-34-generic"))
    check(g.Guard(host).boot() == 0 and host.applied() == [["grub-set-default", "0"]] and host.saved == "0",
          "модуль у нового ядра появился — GRUB снова грузит первое, самое новое")
    check("newest kernel again" in host.said[-1], "и это сказано в журнал")
    installed, files = driver_615("7.0.0-34-generic")
    host = FakeHost(installed, files=files)
    check(g.Guard(host).boot() == 0 and host.saved == entry("7.0.0-34-generic"),
          "у нового ядра модуля нет — GRUB грузит 7.0.0-34, где карта есть")
    check("not the newest 7.0.0-38-generic" in host.said[-1], "и сказано почему")
    again = FakeHost(installed, files=files, saved=entry("7.0.0-34-generic"))
    check(g.Guard(again).boot() == 0 and again.applied() == [], "negative: уже выбрано — второй раз не пишется")
    installed, files = driver_615()
    host = FakeHost(installed, files=files, saved=entry("7.0.0-34-generic"))
    check(g.Guard(host).boot() == 1 and host.applied() == [] and "no installed kernel" in host.said[-1],
          "ни у одного ядра модуля нет — GRUB не трогается, и это сказано")
    installed, files = driver_615("7.0.0-34-generic")
    host = FakeHost(installed, files=files, grub_default="0")
    check(g.Guard(host).boot() == 1 and host.applied() == [] and "GRUB_DEFAULT is not 'saved'" in host.said[-1],
          "negative: GRUB не по сохранённому пункту — страж не правит его сам, а говорит запустить установщик")
    files[g.Grub.CFG] = grub_cfg("7.0.0-38-generic")
    host = FakeHost(installed, files=files)
    check(g.Guard(host).boot() == 1 and host.applied() == [] and "no entry for 7.0.0-34-generic" in host.said[-1],
          "в меню нет пункта для ядра с картой — GRUB не трогается")
    del files[g.Grub.CFG]
    check(g.Guard(FakeHost(installed, files=files)).boot() == 1, "negative: grub.cfg не прочитан — ничего не пишется")
    installed, files = driver_615("7.0.0-34-generic")
    host = FakeHost(installed, files=files, set_code=1)
    check(g.Guard(host).boot() == 1 and "failed" in host.said[-1], "grub-set-default упал — код 1 и слова")
    host = FakeHost(["linux-image-7.0.0-38-generic"])
    check(g.Guard(host).boot() == 0 and host.ran[-1][0][0] == "dpkg-query" and host.said == [],
          "negative: машины без драйвера NVIDIA страж не касается")


def test_modules():
    CHECKS.section("подписанные модули — каждому ядру, что предлагает apt:")
    installed, files = driver_615("7.0.0-38-generic")
    host = FakeHost(installed, offered={"linux-modules-nvidia-615-open-7.0.0-34-generic"}, files=files)
    check(g.Guard(host).modules() == 0, "прошло")
    apt = [cmd for cmd in host.applied() if cmd[0] == "apt-get"]
    check(apt == [["apt-get", "install", "-y", "--no-install-recommends", "-o", "Dpkg::Options::=--force-confdef",
                   "-o", "Dpkg::Options::=--force-confold", "linux-modules-nvidia-615-open-7.0.0-34-generic"]],
          f"один запуск apt: модули 615 для старого ядра, с опциями файлов настроек (got {apt})")
    env = [e for cmd, e in host.ran if cmd[0] == "apt-get"][0]
    check(env == {"DEBIAN_FRONTEND": "noninteractive"}, "apt без вопросов")
    check([c for c in host.applied() if c[0] == "grub-set-default"] == [],
          "negative: у нового ядра модуль есть — GRUB не трогается")
    installed, files = driver_615("7.0.0-38-generic", "7.0.0-34-generic")
    host = FakeHost(installed, files=files)
    check(g.Guard(host).modules() == 0 and [c for c in host.applied() if c[0] == "apt-get"] == [],
          "negative: у всех ядер модуль есть — apt не запускается")
    installed, files = driver_615("7.0.0-38-generic")
    host = FakeHost(installed, files=files)
    check(g.Guard(host).modules() == 0 and host.said == [] and [c for c in host.applied() if c[0] == "apt-get"] == [],
          "negative: старому ядру модулей новой серии apt не предлагает — это не новость, страж молчит")
    installed, files = driver_615("7.0.0-34-generic")
    host = FakeHost(installed, files=files)
    check(g.Guard(host).modules() == 0 and [c for c in host.applied() if c[0] == "apt-get"] == []
          and "apt offers no linux-modules-nvidia-615-open-7.0.0-38-generic" in host.said[0]
          and host.saved == entry("7.0.0-34-generic"),
          "apt модулей для нового ядра не предлагает — это сказано, и GRUB держит старое ядро с картой")
    host = FakeHost(installed, offered={"linux-modules-nvidia-615-open-7.0.0-38-generic"}, files=files, apt_code=100)
    check(g.Guard(host).modules() == 100 and "apt-get install failed (100)" in " ".join(host.said)
          and host.saved == entry("7.0.0-34-generic"),
          "apt упал — код его, и GRUB всё равно на ядре с картой")


def test_the_day_it_was_written_for():
    CHECKS.section("2026-10-09: ядро 7.0.0-38 пришло, его модули — нет:")
    installed = ["nvidia-kernel-common-610", "nvidia-driver-610-open", "nvidia-kernel-source-610-open",
                 "linux-image-7.0.0-34-generic", "linux-image-7.0.0-38-generic",
                 "linux-modules-nvidia-610-open-7.0.0-34-generic", "linux-modules-nvidia-610-open-generic-hwe-24.04"]
    files = {"/lib/modules/7.0.0-34-generic/kernel/nvidia-610-open/nvidia.ko": "signed",
             g.Grub.CFG: grub_cfg("7.0.0-38-generic", "7.0.0-34-generic")}
    host = FakeHost(installed, files=files)
    check(g.Guard(host).boot() == 0 and host.saved == entry("7.0.0-34-generic"),
          "без стража машина загрузила бы 7.0.0-38 без карты; со стражем GRUB держит 7.0.0-34")
    host = FakeHost(installed, offered={"linux-modules-nvidia-610-open-7.0.0-38-generic"}, files=files,
                    saved=entry("7.0.0-34-generic"))
    check(g.Guard(host).modules() == 0 and "linux-modules-nvidia-610-open-7.0.0-38-generic" in host.installed
          and host.saved == "0",
          "после автообновления страж ставит модули 610 под 7.0.0-38 (они были в архиве) — и GRUB грузит новое ядро")


def test_the_command():
    CHECKS.section("команда:")
    check(g.main([]) == 2 and g.main(["reboot"]) == 2 and g.main(["boot", "status"]) == 2,
          "negative: без задания, чужое задание или два — код 2 и подсказка, ничего не запущено")


def main():
    for fn in (test_what_the_machine_has, test_which_kernels_carry_the_card, test_boot, test_modules,
               test_the_day_it_was_written_for, test_the_command):
        fn()
    return CHECKS.finish()


if __name__ == "__main__":
    sys.exit(main())
