"""This machine's NVIDIA driver packages as apt sees them, and installing one."""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from caravan_scout.errors import AppError


class DriverPackages:
    """What apt says about this machine's NVIDIA driver, and the one way to install one.

    The controller used to ask its own machine — nvidia-smi, dpkg, apt-cache,
    mokutil — and to install there with sudo apt-get. A controller in a
    container has none of that, and the driver belongs to the machine anyway,
    so it is the machine's scout that answers now (2.24). The scout reports the
    facts and runs the install; the controller decides what to install and
    when (caravan/admin/gpu_driver.py there). The two versions stay two:
    RUNNING is the one in the kernel (nvidia-smi), INSTALLED the one in the
    packages (dpkg) — right after an update they differ until a reboot.

    Names reach apt only through allowlists: a driver package is
    `nvidia-driver-<N>[-open]` and a signed-modules package is built from it.
    The controller checks the name too, and the scout checks it again here:
    it arrives over HTTP. Under Secure Boot the install carries the signed
    modules for the running kernel and takes the DKMS build out of their way,
    or the kernel refuses the module after the reboot (2026-09-07).
    """

    PACKAGE_RE = re.compile(r"^nvidia-driver-(\d{2,4})(-open)?$")
    MODULES_RE = re.compile(r"^linux-modules-nvidia-(\d{2,4})(-open)?-[a-z0-9.\-]+$")
    VERSION_RE = re.compile(r"^\d+(\.\d+)+$")
    REBOOT_MARKER = Path("/var/run/reboot-required")
    OS_RELEASE = Path("/etc/os-release")
    PROC_MODULES = Path("/proc/modules")

    def __init__(self, secure_boot: Callable[[], bool | None], run: Callable[..., Any] | None = None,
                 kernel: Callable[[], str] | None = None, clock: Callable[[], float] = time.time,
                 reboot_marker: Path | None = None, os_release: Path | None = None,
                 proc_modules: Path | None = None):
        self.secure_boot = secure_boot
        self.run = run or subprocess.run
        self.kernel = kernel or (lambda: os.uname().release)
        self.clock = clock
        self.reboot_marker = reboot_marker or self.REBOOT_MARKER
        self.os_release = os_release or self.OS_RELEASE
        self.proc_modules = proc_modules or self.PROC_MODULES

    def _text(self, cmd: list[str], timeout: int) -> tuple[bool, str]:
        """(exit code 0, stdout and stderr) — the words of a failure are kept,
        since nvidia-smi says why it cannot answer in them."""
        try:
            res = self.run(cmd, text=True, capture_output=True, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — a missing tool is an answer here
            return False, str(exc)
        return res.returncode == 0, f"{res.stdout or ''}\n{res.stderr or ''}".strip()

    @staticmethod
    def version_key(text: Any) -> list[int]:
        return [int(part) for part in re.findall(r"\d+", str(text or ""))] or [0]

    def running(self) -> tuple[str, str]:
        """(version in the kernel, why there is none). Right after an install
        nvidia-smi prints "Failed to initialize NVML: Driver/library version
        mismatch" where a number is expected: a number has to look like one."""
        _ok, text = self._text(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], 8)
        for line in text.splitlines():
            if self.VERSION_RE.match(line.strip()):
                return line.strip(), ""
        return "", " ".join(text.split())[:200]

    def _dpkg(self, pattern: str) -> list[dict[str, str]]:
        """Installed packages matching `pattern`. dpkg's status is three words
        and the third carries the value: a removed package says not-installed."""
        _ok, text = self._text(["dpkg-query", "-W", "-f=${Package} ${Version} ${Status}\\n", pattern], 10)
        found = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[4] == "installed":
                found.append({"package": parts[0], "version": parts[1]})
        return found

    def installed(self) -> list[dict[str, str]]:
        """Installed driver metapackages, newest first."""
        found = [row for row in self._dpkg("nvidia-driver-*") if self.PACKAGE_RE.match(row["package"])]
        return sorted(found, key=lambda row: self.version_key(row["version"]), reverse=True)

    def package_installed(self, name: str) -> bool:
        return bool(name) and any(row["package"] == name for row in self._dpkg(name))

    def policy(self, names: list[str]) -> list[dict[str, str]]:
        """apt-cache policy's CANDIDATE versions — what an install would take."""
        if not names:
            return []
        _ok, text = self._text(["apt-cache", "policy", *names], 25)
        found, current = [], ""
        for line in text.splitlines():
            if not line.startswith(" ") and line.rstrip().endswith(":"):
                current = line.rstrip()[:-1].strip()
            elif current and line.strip().startswith("Candidate:"):
                version = line.split(":", 1)[1].strip()
                if version and version != "(none)":
                    found.append({"package": current, "version": version})
                current = ""
        return found

    def available(self) -> list[dict[str, str]]:
        """Driver candidates from apt, newest first, filtered by the allowlist."""
        _ok, text = self._text(["apt-cache", "--names-only", "search", "^nvidia-driver-[0-9]+(-open)?$"], 20)
        names = [line.split(" - ")[0].strip() for line in text.splitlines()]
        names = [name for name in names if self.PACKAGE_RE.match(name)]
        return sorted(self.policy(names), key=lambda row: self.version_key(row["version"]), reverse=True)

    def module_loaded(self, name: str = "nvidia") -> bool:
        try:
            lines = self.proc_modules.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return False
        return any(line.split(" ", 1)[0] == name for line in lines)

    def _version_id(self) -> str:
        try:
            for line in self.os_release.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("VERSION_ID="):
                    return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return ""

    def signed_modules(self, driver_package: str) -> str:
        """The signed-modules metapackage for the RUNNING kernel, or "".

        Two candidates (HWE and GA) and the exact-kernel name last; the pick is
        the one whose candidate version starts with the running kernel's
        series — another series would drag in yet another kernel.
        """
        match = self.PACKAGE_RE.match(str(driver_package or "").strip())
        if not match:
            return ""
        base = f"linux-modules-nvidia-{match.group(1)}{match.group(2) or ''}"
        release = self.kernel()
        series = re.match(r"^(\d+\.\d+\.\d+-\d+)", release)
        series = series.group(1) if series else ""
        version_id = self._version_id()
        names = [f"{base}-generic-hwe-{version_id}"] if version_id else []
        names += [f"{base}-generic", f"{base}-{release}"]
        versions = {row["package"]: row["version"] for row in self.policy(names)}
        for name in names:
            version = versions.get(name) or ""
            if version and (not series or version.startswith(series)):
                return name
        return ""

    def reboot_pending(self) -> tuple[bool, list[str]]:
        """The OS's own reboot request and its package list ([] when unknown)."""
        if not self.reboot_marker.exists():
            return False, []
        try:
            lines = Path(f"{self.reboot_marker}.pkgs").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return True, []
        return True, [line.strip() for line in lines if line.strip()]

    def facts(self) -> dict[str, Any]:
        """Everything the controller's driver panel decides from, read once."""
        running, running_error = self.running()
        installed = self.installed()
        signed = self.signed_modules(installed[0]["package"]) if installed else ""
        reboot, reboot_packages = self.reboot_pending()
        return {"ok": True, "running": running, "runningError": running_error,
                "installed": installed, "available": self.available(),
                "secureBoot": self.secure_boot(), "moduleLoaded": self.module_loaded(),
                "signedModules": signed, "signedModulesInstalled": bool(signed) and self.package_installed(signed),
                "rebootPending": reboot, "rebootPendingPackages": reboot_packages,
                "kernel": self.kernel(), "checkedAt": int(self.clock())}

    def install_command(self, package: str) -> list[str]:
        """The install, checked: an allowlisted name apt offers, and under
        Secure Boot its signed modules with the DKMS build moved out of their
        way (a DKMS module in updates/dkms/ ranks above the signed one)."""
        name = str(package or "").strip()
        if not self.PACKAGE_RE.match(name):
            raise AppError(f"not a driver package: {name!r}", 400)
        if not any(row["package"] == name for row in self.available()):
            raise AppError(f"no such driver package in apt: {name}", 404)
        modules = self.signed_modules(name) if self.secure_boot() else ""
        if modules and not self.MODULES_RE.match(modules):
            raise AppError(f"not a modules package: {modules!r}", 500)
        apt = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "--no-install-recommends", name]
        if not modules:
            return ["sudo", "-n", *apt]
        kernel = self.kernel()
        script = (f"{' '.join(apt)} {modules} && "
                  f"for m in $(dkms status 2>/dev/null | sed -n 's/^\\(nvidia\\/[^,: ]*\\).*/\\1/p' | sort -u); do "
                  f"dkms remove \"$m\" -k {kernel} >/dev/null 2>&1 || true; done; "
                  f"depmod -a {kernel}")
        return ["sudo", "-n", "bash", "-c", script]

    def install(self, package: str, job) -> dict[str, Any]:
        """Start the install as the machine's background job; its status right away."""
        cmd = self.install_command(package)
        return job.start(cmd, f"driver:{str(package).strip()}", dict(os.environ))
