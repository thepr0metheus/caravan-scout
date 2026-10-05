#!/usr/bin/env python3
"""Prism runtime: release selection, isolation, refusal and managed launch."""
from __future__ import annotations
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import TMP, make_scout, patched
from caravan_scout.errors import AppError
from caravan_scout.prism import PrismRuntime
from caravan_scout.process import CellLog
from caravan_scout.starts import LlamaLaunch, LlamaStart


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=TMP))
        self.calls = []
        self.config = {"prismRuntimeDir": str(self.root)}
        self.archive = io.BytesIO()
        with tarfile.open(fileobj=self.archive, mode="w:gz") as tar:
            for name, body in (("release/bin/llama-server", b"server"), ("release/bin/libggml.so", b"lib")):
                info = tarfile.TarInfo(name); info.size = len(body); info.mode = 0o755
                tar.addfile(info, io.BytesIO(body))
        self.data = self.archive.getvalue()
        self.runtime = PrismRuntime(self.config, system="linux", machine="x86_64",
                                    run=self.fake_run, open_url=self.download)
        self.runtime.ASSETS = {**PrismRuntime.ASSETS, "ubuntu-x64": hashlib.sha256(self.data).hexdigest()}

    def fake_run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="--model --port --ctx-size --n-gpu-layers\nversion prism", stderr="")

    def download(self, url, **kwargs):
        self.calls.append((url, kwargs))
        source = io.BytesIO(self.data)
        source.headers = {"Content-Length": str(len(self.data))}
        return source

    def test_cuda_and_arch_selection(self):
        cases = [("linux", "x86_64", "13.0", "auto", "linux-cuda-12.8-x64"),
                 ("linux", "x86_64", "13.3", "auto", "linux-cuda-13.3-x64"),
                 ("linux", "x86_64", "12.4", "cuda", "linux-cuda-12.4-x64"),
                 ("linux", "aarch64", "", "auto", "ubuntu-arm64"),
                 ("darwin", "arm64", "", "auto", "macos-arm64"),
                 ("linux", "x86_64", "13.0", "cpu", "ubuntu-x64")]
        for *args, expected in cases:
            with self.subTest(args=args): self.assertEqual(PrismRuntime.asset(*args), expected)
        for args in (("linux", "x86_64", "12.3", "cuda"), ("win32", "amd64", "", "auto"),
                     ("linux", "aarch64", "13.0", "cuda"), ("linux", "x86_64", "", "typo")):
            with self.subTest(args=args), self.assertRaises(AppError): PrismRuntime.asset(*args)

    def test_install_and_reuse(self):
        progress = []
        binary = self.runtime.ensure(progress=lambda *args: progress.append(args))
        self.assertTrue(Path(binary).is_file())
        self.assertTrue(self.runtime.facts()["installed"])
        self.assertEqual(self.runtime.facts()["binary"], binary)
        self.assertEqual(progress[-1][:2], (len(self.data), len(self.data)))
        again = self.runtime.ensure()
        self.assertEqual(binary, again)
        self.assertEqual(sum(isinstance(c[0], str) for c in self.calls), 1)
        self.assertIn("prism-b10743-adfffbe", binary)
        self.assertEqual(list(self.root.glob(".install-*")), [])

    def test_concurrent_starts_share_installation(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            binaries = list(pool.map(lambda _: self.runtime.ensure(), range(2)))
        self.assertEqual(binaries[0], binaries[1])
        self.assertEqual(sum(isinstance(c[0], str) for c in self.calls), 1)

    def test_failed_binary_is_not_published(self):
        original = self.runtime.run
        def failed(argv, **kwargs):
            if argv[-1] == "--version":
                return SimpleNamespace(returncode=1, stdout="", stderr="missing libcuda")
            return original(argv, **kwargs)
        with patched(self.runtime, run=failed), self.assertRaisesRegex(AppError, "missing libcuda"):
            self.runtime.ensure()
        self.assertFalse(self.runtime.facts()["installed"])
        self.assertEqual(list(self.root.glob(".install-*")), [])

    def test_digest_mismatch_does_not_publish(self):
        self.runtime.ASSETS = {"ubuntu-x64": "0" * 64}
        with self.assertRaisesRegex(AppError, "SHA-256"): self.runtime.ensure()
        self.assertFalse((self.root / "current").exists())
        self.assertFalse(self.runtime.facts()["installed"])
        self.assertEqual(list(self.root.glob(".install-*")), [])

    def test_archive_path_and_link_escape_refused(self):
        for name, link in (("../outside", None), ("safe", "../../outside")):
            archive = self.root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                member = tarfile.TarInfo(name)
                if link: member.type = tarfile.SYMTYPE; member.linkname = link
                tar.addfile(member)
            with self.assertRaises(AppError): self.runtime.unpack(archive, self.root / "target")

    def test_library_environment_is_isolated(self):
        binary = self.runtime.ensure()
        env = self.runtime.environment(binary)
        self.assertNotIn("llama.cpp/build", env["LD_LIBRARY_PATH"])
        self.assertTrue(all(str(self.root) in p for p in env["LD_LIBRARY_PATH"].split(":")))
        argv, kwargs = next((argv, kwargs) for argv, kwargs in self.calls
                            if isinstance(argv, list) and argv[-1] == "--version")
        self.assertIn(str(Path(argv[0]).parent), kwargs["env"]["LD_LIBRARY_PATH"])
        self.assertNotIn("llama.cpp/build", kwargs["env"]["LD_LIBRARY_PATH"])

    def test_flag_refusal(self):
        binary = self.runtime.ensure()
        self.runtime.validate_args(binary, ["--model", "/model.gguf", "--port", "22013"])
        with self.assertRaisesRegex(AppError, "--future-option"):
            self.runtime.validate_args(binary, ["--future-option", "yes"])

    def test_launch_uses_prism_not_global_llama(self):
        scout = make_scout({"llamaServerBin": "/stock/llama-server"})
        scout.cells.prism = self.runtime
        cell = scout.cells.at(22013)
        config = {"RUNNER": "prism", "MODEL_FILE": "bonsai.gguf", "PORT": 22013, "N_GPU_LAYERS": "0"}
        starts = []
        with patched(scout.cells.models, download_all=lambda *a, **k: ("/cache/bonsai.gguf", "", "")), \
             patched(cell.process, start=lambda binary, args, cfg, **kw: starts.append((binary, args, cfg, kw)) or {"ok": True, "pid": 1}, launch_spec=lambda: {}), \
             patched(scout.cells.records, add=lambda *a, **k: None):
            launch = LlamaLaunch(scout.cells, 22013, "/stock/llama-server", config, "bonsai.gguf",
                                 args=["--model", "{{MODEL_PATH}}", "--port", "22013"], env={"CUDA_VISIBLE_DEVICES": ""})
            launch.run()
        binary, args, cfg, options = starts[0]
        self.assertNotEqual(binary, "/stock/llama-server")
        self.assertEqual(args[1], "/cache/bonsai.gguf")
        self.assertEqual(cfg["runner"], "prism")
        self.assertEqual(options["extra_env"]["CUDA_VISIBLE_DEVICES"], "")
        generated = json.loads(Path(cfg["artifact"]["cellJson"]).read_text())
        self.assertEqual(generated["config"]["RUNNER"], "prism")
        self.assertEqual(generated["cmd"][0], binary)

    def test_stop_during_install_never_launches(self):
        scout = make_scout()
        scout.cells.prism = self.runtime
        cell = scout.cells.at(22013)
        with patched(self.runtime, ensure=lambda **kw: scout.cells.drop(22013) or "/prism/llama-server", validate_args=lambda *a: None):
            LlamaLaunch(scout.cells, 22013, "", {"RUNNER": "prism"}, "m.gguf", args=["--model", "{{MODEL_PATH}}"]).run()
        self.assertFalse(scout.cells.holds(22013, cell))
        self.assertEqual(scout.cells.all(), [])

    def test_unknown_native_runner_refused(self):
        with self.assertRaisesRegex(AppError, "unknown native runner"):
            LlamaStart(make_scout().cells, {"config": {"RUNNER": "prsim"}, "modelPath": "m.gguf"}).run()

    def test_actionable_log_line_wins_over_generic_failure(self):
        log = self.root / "model.log"
        log.write_text("E gguf: tensor 'output.weight' has invalid ggml type 142\nE srv: exiting due to model loading error\n")
        self.assertIn("invalid ggml type 142", CellLog(log).crash_reason())


if __name__ == "__main__": unittest.main()
