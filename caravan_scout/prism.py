"""The isolated, pinned PrismML runtime used by Bonsai cells."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
from pathlib import Path

from caravan_scout.errors import AppError


class PrismRuntime:
    """Install a release once, keeping its libraries beside its own server.

    A stock libggml cannot interpret Prism's tensor types 142/143. Installing
    into llama.cpp's build tree, or inheriting its LD_LIBRARY_PATH, can make a
    correctly selected Prism binary load the wrong libraries.
    """
    RELEASE = "prism-b10743-adfffbe"
    BASE_URL = "https://github.com/PrismML-Eng/llama.cpp/releases/download"
    # Digests published on the pinned release; no moving 'latest' at startup.
    ASSETS = {
        "linux-cuda-12.4-x64": "fef4c7e8d83ff261d89809c1b302fe5f826adc1a74e13654b67ec056fbc1c639",
        "linux-cuda-12.8-x64": "43b73a24d5cd83c4482750ee52e59afac497c669c008a319e44e43a0033757e2",
        "linux-cuda-13.3-x64": "bfa6e1e0af2f21295e863cf26444c49b5d0094221ea785836cd810325fca2159",
        "ubuntu-x64": "1bb340929fddae8667c97ec6d4064a5768fe1dd08eeee0074b8d5e08f07dfc31",
        "ubuntu-arm64": "ee4cb83a2985572459a5686ef175503d4e526b46b97c7fcc7152e868169b3060",
        "macos-arm64": "596d257973080ca5011a4be50477c5f93ed1d231fcccfc2afb44bb573bb9629a",
        "macos-x64": "3527598651d783b66d525afbcdd0cbbba12a8d59c388bb0e13fa85715381969d",
        "ubuntu-vulkan-x64": "95a3d082629643642842bc2195e0730880273005171517cf857804702f693edc",
        "ubuntu-rocm-7.2-x64": "fc539098d17f3a25668027a8f4cd35c7e5b78148abe2cd7948c66ebb988cc203",
    }

    def __init__(self, config, *, system=None, machine=None, run=None, open_url=None):
        self.config = config
        self.root = Path(config.get("prismRuntimeDir") or "~/.local/share/caravan/prismml").expanduser().resolve()
        self.system = system or sys.platform
        self.machine = machine or platform.machine()
        self.run = run or subprocess.run
        self.open_url = open_url or urllib.request.urlopen
        self.lock = threading.Lock()
        self._help: dict[str, set[str]] = {}

    @classmethod
    def asset(cls, system: str, machine: str, cuda: str = "", backend: str = "auto") -> str:
        arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine.lower())
        if system == "darwin" and backend in ("auto", "cpu") and arch:
            return f"macos-{arch}"
        if system != "linux" or not arch:
            raise AppError(f"PrismML has no managed runtime for {system}/{machine}", 400)
        if backend == "cpu" or (backend == "auto" and not cuda):
            return f"ubuntu-{arch}"
        if backend in ("vulkan", "rocm") and arch == "x64":
            return {"vulkan": "ubuntu-vulkan-x64", "rocm": "ubuntu-rocm-7.2-x64"}[backend]
        if backend not in ("auto", "cuda") or arch != "x64":
            raise AppError(f"PrismML has no managed {backend} runtime for {machine}", 400)
        match = re.fullmatch(r"(\d+)\.(\d+)", cuda)
        version = tuple(map(int, match.groups())) if match else None
        if version is None or version < (12, 4):
            raise AppError("PrismML CUDA needs an NVIDIA driver supporting CUDA 12.4 or newer", 400)
        flavor = "13.3" if version >= (13, 3) else "12.8" if version >= (12, 8) else "12.4"
        return f"linux-cuda-{flavor}-x64"

    def selected_asset(self) -> str:
        cuda = ""
        if self.system == "linux":
            try:
                result = self.run(["nvidia-smi"], capture_output=True, text=True, timeout=10)
                match = re.search(r"CUDA(?: UMD)? Version:\s*(\d+\.\d+)", result.stdout or "")
                cuda = match.group(1) if result.returncode == 0 and match else ""
            except (OSError, subprocess.SubprocessError):
                pass
        return self.asset(self.system, self.machine, cuda, str(self.config.get("prismBackend") or "auto"))

    @staticmethod
    def environment(binary: str) -> dict[str, str]:
        folder = Path(binary).parent
        libraries = ":".join(str(p) for p in (folder, folder / "lib", folder.parent / "lib") if p.is_dir())
        return {"LD_LIBRARY_PATH": libraries, "DYLD_LIBRARY_PATH": libraries}

    def probe(self, binary: str, option: str) -> str:
        result = self.run([binary, option], capture_output=True, text=True, timeout=30,
                          env={**os.environ, **self.environment(binary), "CUDA_VISIBLE_DEVICES": ""})
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            raise AppError(f"PrismML runtime cannot run {option}: {detail}", 400)
        return (result.stdout or "") + (result.stderr or "")

    @staticmethod
    def unpack(archive: Path, target: Path) -> None:
        """Validate links as well as names before extracting a vendor archive."""
        with tarfile.open(archive, "r:gz") as tar:
            root = target.resolve()
            for member in tar.getmembers():
                dest = (target / member.name).resolve()
                if not dest.is_relative_to(root) or not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                    raise AppError(f"unsafe PrismML archive member: {member.name}")
                if member.issym() or member.islnk():
                    link = (dest.parent / member.linkname if member.issym() else target / member.linkname).resolve()
                    if not link.is_relative_to(root):
                        raise AppError(f"unsafe PrismML archive link: {member.name}")
            if hasattr(tarfile, "data_filter"):
                tar.extractall(target, filter="data")
            else:
                tar.extractall(target)

    def ensure(self, progress=None) -> str:
        override = str(self.config.get("prismServerBin") or "").strip()
        if override:
            binary = str(Path(override).expanduser())
            self.probe(binary, "--version")
            return binary
        with self.lock:
            flavor = self.selected_asset()
            directory = self.root / f"{self.RELEASE}-{flavor}"
            manifest = directory / "runtime.json"
            if manifest.exists():
                info = json.loads(manifest.read_text())
                binary = str(directory / info["binary"])
                if Path(binary).is_file():
                    self.activate(binary)
                    return binary
            self.root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".install-", dir=self.root))
            try:
                archive = staging / "runtime.tar.gz"
                url = f"{self.BASE_URL}/{self.RELEASE}/llama-{self.RELEASE}-bin-{flavor}.tar.gz"
                digest = hashlib.sha256()
                with self.open_url(url, timeout=60) as source, archive.open("wb") as output:
                    total = int(source.headers.get("Content-Length") or 0)
                    downloaded = 0
                    while block := source.read(1024 * 1024):
                        output.write(block)
                        digest.update(block)
                        downloaded += len(block)
                        if progress:
                            progress(downloaded, total, url.rsplit("/", 1)[-1])
                if digest.hexdigest() != self.ASSETS[flavor]:
                    raise AppError("PrismML archive SHA-256 mismatch — runtime was not installed")
                extracted = staging / "files"
                extracted.mkdir()
                self.unpack(archive, extracted)
                candidates = list(extracted.rglob("llama-server"))
                if len(candidates) != 1:
                    raise AppError("PrismML archive must contain exactly one llama-server")
                binary = candidates[0]
                binary.chmod(binary.stat().st_mode | 0o111)
                version = self.probe(str(binary), "--version").strip()
                (extracted / "runtime.json").write_text(json.dumps({
                    "release": self.RELEASE, "asset": flavor, "sha256": digest.hexdigest(),
                    "binary": str(binary.relative_to(extracted)), "version": version,
                }, indent=2) + "\n")
                # An incomplete prior install is never published as current.
                if directory.exists():
                    raise AppError(f"incomplete PrismML install at {directory}; move it aside and retry")
                extracted.rename(directory)
                installed = str(directory / binary.relative_to(extracted))
                self.activate(installed)
                return installed
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def activate(self, binary: str) -> None:
        # Stable path for generated previews, versioned path for running cells.
        link = self.root / ".current.tmp"
        link.unlink(missing_ok=True)
        link.symlink_to(Path(binary).parent.relative_to(self.root), target_is_directory=True)
        link.replace(self.root / "current")

    def validate_args(self, binary: str, args: list[str]) -> None:
        if binary not in self._help:
            self._help[binary] = set(re.findall(r"(?<!\w)--[a-z][a-z0-9-]*", self.probe(binary, "--help")))
        unknown = sorted({arg.split("=", 1)[0] for arg in args if arg.startswith("--")} - self._help[binary])
        if unknown:
            raise AppError(f"PrismML {self.RELEASE} does not support: {', '.join(unknown)}; adjust the cell settings", 400)

    def facts(self) -> dict:
        override = str(self.config.get("prismServerBin") or "").strip()
        if override:
            binary = str(Path(override).expanduser())
            return {"supported": True, "installed": Path(binary).is_file(), "binary": binary, "release": "custom"}
        current = self.root / "current"
        manifests = []
        if current.exists():
            for directory in (current.resolve(), *current.resolve().parents):
                if not directory.is_relative_to(self.root.resolve()):
                    break
                if (directory / "runtime.json").is_file():
                    manifests.append(directory / "runtime.json")
                    break
        for manifest in manifests:
            try:
                info = json.loads(manifest.read_text())
                return {"supported": True, "installed": True, **info, "binary": str(manifest.parent / info["binary"])}
            except (OSError, ValueError, KeyError):
                continue
        return {"supported": True, "installed": False, "release": self.RELEASE}


if __name__ == "__main__":
    from caravan_scout.config import ScoutConfig
    from caravan_scout.paths import PROJECT_ROOT
    runtime = PrismRuntime(ScoutConfig(PROJECT_ROOT / "config.json"))
    if sys.argv[1:] == ["install"]:
        print(runtime.ensure())
    else:
        print(json.dumps(runtime.facts(), indent=2))
