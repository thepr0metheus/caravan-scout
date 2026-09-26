"""What decides whether this machine's NVIDIA card comes back after a reboot.

2026-09-26: the automatic updates installed a kernel a day before a reboot,
held back the Canonical-signed NVIDIA modules it needed (their driver was in
a pocket they were not allowed), and the machine came up with Secure Boot
refusing the unsigned build: no card until someone fixed it by hand. Nothing
had said so before the reboot. The scout reports the facts that would have;
the controller draws the conclusion (caravan/domain/driver_outlook.py there).
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable


class DriverFacts:
    """This machine's NVIDIA driver as its next boot will meet it: Secure
    Boot, the kernel running and the one that boots next, the driver version
    loaded and the one installed, and the module the next kernel would load.

    Cached for a minute: these change when packages are installed, not from
    one poll to the next, and asking costs a few processes. None where the
    question does not arise — not Linux, or no NVIDIA driver anywhere: none
    loaded, no library installed, no module for the next kernel. A fact that
    cannot be read is None, never a guess.
    """

    TTL = 60
    #: The kernels a boot can pick; the newest is what GRUB boots by default.
    #: Only versioned names count: vmlinuz and vmlinuz.old are links to them.
    BOOT = Path("/boot")
    #: What the loaded module says of itself.
    PROC_VERSION = Path("/proc/driver/nvidia/version")
    #: Where the userspace library lives, its version in its file name.
    LIB_DIRS = (Path("/usr/lib/x86_64-linux-gnu"), Path("/usr/lib64"), Path("/usr/lib"))
    KERNEL_FILE = re.compile(r"^vmlinuz-(\d[\w.+-]*)$")
    #: The key DKMS signs its builds with on Ubuntu — trusted only if it was
    #: enrolled at the console (MOK); a build signed by an unenrolled key is
    #: refused under Secure Boot just like an unsigned one.
    DKMS_KEY = Path("/var/lib/shim-signed/mok/MOK.der")
    #: The firmware's own word on Secure Boot: four bytes of attributes, then
    #: 1 (on) or 0 (off), readable by anyone. mokutil reads the same and is
    #: not installed everywhere: a machine without it was "unknown" while its
    #: firmware said "off" (2.19.1).
    EFI_DIR = Path("/sys/firmware/efi")
    SECURE_BOOT_VAR = EFI_DIR / "efivars" / "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
    LOADED = re.compile(r"Kernel Module(?:\s+for\s+\S+)?\s+(\d+(?:\.\d+)+)")
    COMMON_NAME = re.compile(r"CN\s*=\s*([^,/\n]+)")
    LIBRARY = re.compile(r"^libnvidia-ml\.so\.(\d+(?:\.\d+)+)$")

    def __init__(self, run_text: Callable[..., str], platform: str | None = None,
                 kernel: Callable[[], str] | None = None, clock: Callable[[], float] = time.time):
        self.run_text = run_text
        self.platform = platform or sys.platform
        self.kernel = kernel or (lambda: os.uname().release)
        self.clock = clock
        self._facts: dict[str, Any] | None = None
        self._at = 0.0

    def facts(self) -> dict[str, Any] | None:
        now = self.clock()
        if self._at and now - self._at < self.TTL:
            return self._facts
        self._facts = self.read()
        self._at = now
        return self._facts

    def read(self) -> dict[str, Any] | None:
        if not self.platform.startswith("linux"):
            return None
        running = self.kernel() or None
        following = self.next_kernel()
        loaded = self.loaded_version()
        installed = self.installed_version()
        module = self.module(following) if following else None
        if loaded is None and installed is None and module is None:
            return None
        return {
            "secureBoot": self.secure_boot(),
            "kernelRunning": running,
            "kernelNext": following,
            "loaded": loaded,
            "installed": installed,
            "nextModule": module,
            "dkmsKey": self.dkms_key(),
            "package": self.driver_package(installed),
        }

    @staticmethod
    def version_key(text: str) -> tuple:
        return tuple(int(part) for part in re.findall(r"\d+", text))

    def next_kernel(self) -> str | None:
        """The newest kernel in /boot — what the default boot entry starts."""
        try:
            names = [entry.name for entry in self.BOOT.iterdir()]
        except OSError:
            return None
        kernels = [m.group(1) for m in map(self.KERNEL_FILE.match, names) if m]
        return max(kernels, key=self.version_key) if kernels else None

    def loaded_version(self) -> str | None:
        try:
            text = self.PROC_VERSION.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        found = self.LOADED.search(text)
        return found.group(1) if found else None

    def installed_version(self) -> str | None:
        """The userspace driver's version, from its library's file name."""
        versions = []
        for folder in self.LIB_DIRS:
            try:
                names = [entry.name for entry in folder.iterdir()]
            except OSError:
                continue
            versions += [m.group(1) for m in map(self.LIBRARY.match, names) if m]
        return max(versions, key=self.version_key) if versions else None

    def module(self, kernel: str) -> dict[str, Any] | None:
        """The nvidia module `kernel` would load: where it lies, its version
        and who signed it ("" when nobody did). None when there is none."""
        path = self.run_text(["modinfo", "-k", kernel, "-n", "nvidia"]).strip()
        if not path:
            return None
        return {
            "path": path,
            "version": self.run_text(["modinfo", "-k", kernel, "-F", "version", "nvidia"]).strip() or None,
            "signer": self.run_text(["modinfo", "-k", kernel, "-F", "signer", "nvidia"]).strip(),
        }

    def dkms_key(self) -> dict[str, Any] | None:
        """The key DKMS signs with: its name, as a module's signer reads, and
        whether the firmware trusts it. None when there is no such key."""
        if not self.DKMS_KEY.exists():
            return None
        subject = self.run_text(["openssl", "x509", "-inform", "der", "-in", str(self.DKMS_KEY), "-noout", "-subject"])
        found = self.COMMON_NAME.search(subject)
        test = self.run_text(["mokutil", "--test-key", str(self.DKMS_KEY)]).lower()
        enrolled = True if "is already enrolled" in test else False if "is not enrolled" in test else None
        return {"signer": found.group(1).strip() if found else None, "enrolled": enrolled}

    def secure_boot(self) -> bool | None:
        """The firmware's variable; a machine that did not boot through EFI
        has no Secure Boot at all (False); mokutil when the variable cannot be
        read; None when nothing can say."""
        try:
            data = self.SECURE_BOOT_VAR.read_bytes()
        except OSError:
            data = b""
        if len(data) == 5 and data[4] in (0, 1):
            return data[4] == 1
        if not self.EFI_DIR.exists():
            return False
        out = self.run_text(["mokutil", "--sb-state"]).lower()
        if "secureboot enabled" in out:
            return True
        if "secureboot disabled" in out:
            return False
        return None

    def driver_package(self, installed: str | None) -> str | None:
        """The installed nvidia-driver-* package the driver in use comes from,
        on a Debian-style system: the one whose version is the library's.
        Several can be installed at once — a machine had 550 and 580 and ran
        580 — so the first one would be a guess: without the library's
        version, or with no package of it, None."""
        if not installed:
            return None
        out = self.run_text(["dpkg-query", "-W", "-f", "${Package} ${Version} ${Status}\\n", "nvidia-driver-*"])
        for line in out.splitlines():
            name, version, status = (line.split(" ", 2) + ["", ""])[:3]
            upstream = version.split(":", 1)[-1]
            if (status.strip() == "install ok installed" and name.startswith("nvidia-driver-")
                    and upstream.startswith(installed + "-")):
                return name
        return None
