"""CellsMixin: llama/command server cells — build args, artifacts, start/stop, apply routes."""
from __future__ import annotations

import json
import os
import re
import signal
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from caravan_scout.paths import LLAMA_PATH_PLACEHOLDER_MMPROJ, LLAMA_PATH_PLACEHOLDER_MODEL, LLAMA_PATH_PLACEHOLDER_SPEC, SERVER_CELLS_DIR
from caravan_scout.errors import AppError


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class CellsMixin:
    # ── llama.cpp update job ──────────────────────────────────────────────────
    # Runs scripts/update-llama.sh (a synced copy of the controller's
    # install-llama.sh: release-tag/commit checkout -f, stale-build-dir guard,
    # probe-gated Blackwell workaround, cmake build) as a background thread and
    # streams its output into a ring buffer. Running cells keep the OLD binary
    # (they hold its inode) until restarted — deliberately never automatic.
    # The slim status rides every heartbeat so the controller UI can show
    # "building…" without extra calls.

    def _llama_update_job(self) -> dict:
        job = getattr(self, "_llama_update_state", None)
        if job is None:
            job = {"running": False, "startedAt": 0, "tag": "", "lines": [],
                   "done": False, "rc": None, "error": ""}
            self._llama_update_state = job
            self._llama_update_lock = threading.Lock()
        return job

    def llama_update_status(self) -> dict:
        job = self._llama_update_job()
        with self._llama_update_lock:
            snap = {k: v for k, v in job.items() if k != "lines"}
            snap["lines"] = list(job["lines"])[-200:]
            return snap

    def llama_update_status_slim(self) -> dict:
        job = self._llama_update_job()
        with self._llama_update_lock:
            return {"running": job["running"], "done": job["done"], "rc": job["rc"],
                    "startedAt": job["startedAt"], "tag": job["tag"],
                    "lastLine": (job["lines"][-1] if job["lines"] else "")}

    def llama_builds_list(self) -> dict:
        """Archived build snapshots on THIS host (newest first) — the update
        script writes one per successful build and prunes to 5 by default."""
        root = Path(os.environ.get("LLAMA_BUILDS_DIR")
                    or Path.home() / ".local" / "share" / "lama-caravan" / "llama-builds")
        rows = []
        if root.is_dir():
            for entry in sorted(root.iterdir(), reverse=True):
                meta = entry / "meta.json"
                if not meta.is_file():
                    continue
                try:
                    row = json.loads(meta.read_text(encoding="utf-8"))
                except Exception:
                    continue
                row["id"] = entry.name
                rows.append(row)
        return {"ok": True, "builds": rows}

    def llama_update_start(self, body: dict) -> dict:
        """POST /api/llama-node/update {tag?} — empty tag = latest release; a
        commit sha works too (checkout -f accepts either), which is how the
        controller converges a client onto its own build. With {restoreId} the
        same job restores an archived build instead of building."""
        job = self._llama_update_job()
        script = Path(__file__).resolve().parent.parent / "scripts" / "update-llama.sh"
        if not script.exists():
            raise AppError(f"update script not found: {script}", 500)
        tag = str((body or {}).get("tag") or "").strip()
        restore_id = str((body or {}).get("restoreId") or "").strip()
        if restore_id:
            cmd = ["bash", str(script), "--restore", restore_id]
            tag = f"restore:{restore_id}"
        else:
            cmd = ["bash", str(script), "--force", "--no-restart"]
            if tag:
                cmd += ["--llama-tag", tag]
        with self._llama_update_lock:
            if job["running"]:
                raise AppError("a llama.cpp update is already running", 409)
            job.update({"running": True, "startedAt": int(time.time()), "tag": tag,
                        "lines": [], "done": False, "rc": None, "error": ""})
        env = dict(os.environ)
        env["PATH"] = "/usr/local/cuda/bin:" + env.get("PATH", "/usr/bin:/bin")
        # Clients keep a SHORT archive (default 2: current + one-step undo) —
        # client snapshots are big and a client rollback is never urgent: cells
        # keep serving their old binary through any rebuild. config.json
        # `llamaBuildsKeep` overrides.
        env.setdefault("LLAMA_BUILDS_KEEP",
                       str(int(self.config.get("llamaBuildsKeep") or 2)))

        def _run():
            rc, error = -1, ""
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, env=env)
                for line in proc.stdout:
                    clean = _ANSI_RE.sub("", line.rstrip())
                    with self._llama_update_lock:
                        job["lines"].append(clean)
                        if len(job["lines"]) > 500:
                            del job["lines"][:100]
                rc = proc.wait()
            except Exception as exc:
                error = str(exc)
            finally:
                with self._llama_update_lock:
                    job.update({"running": False, "done": True, "rc": rc, "error": error})

        threading.Thread(target=_run, daemon=True, name="llama-update").start()
        return self.llama_update_status()

    def _server_cell_dir(self, port: int) -> Path:
        return SERVER_CELLS_DIR / str(int(port))

    def _write_llama_cell_artifacts(self, port: int, bin_path: str, args: list[str],
                                    config: dict[str, Any], runtime_cfg: dict[str, Any]) -> dict[str, Any]:
        cell_dir = self._server_cell_dir(port)
        cell_dir.mkdir(parents=True, exist_ok=True)
        start_path = cell_dir / "start.sh"
        json_path = cell_dir / "cell.json"
        cmd = [str(Path(bin_path).expanduser()), *[str(a) for a in args]]
        script = "#!/usr/bin/env bash\nset -euo pipefail\n\nexec " + " ".join(shlex.quote(x) for x in cmd) + " \"$@\"\n"
        tmp_start = start_path.with_suffix(".sh.tmp")
        tmp_json = json_path.with_suffix(".json.tmp")
        tmp_start.write_text(script, encoding="utf-8")
        tmp_start.chmod(0o755)
        tmp_start.replace(start_path)
        payload = {
            "hostId": str(self.config.get("hostId") or ""),
            "port": int(port),
            "config": config,
            "runtime": runtime_cfg,
            "cmd": cmd,
            "generatedAt": int(time.time()),
            "startScript": str(start_path),
        }
        tmp_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_json.replace(json_path)
        return {"dir": str(cell_dir), "startScript": str(start_path),
                "cellJson": str(json_path), "generatedAt": payload["generatedAt"]}

    def _build_llama_args(self, config: dict[str, Any], model_abs: str,
                          mmproj_abs: str, spec_abs: str) -> list[str]:
        """Build the full llama-server arg list (after the binary) from the
        admin form config — mirror of buildCmdline() in the admin UI."""
        c = config if isinstance(config, dict) else {}
        truthy = self._truthy

        def has(key: str) -> bool:
            v = c.get(key)
            return v is not None and str(v).strip() != ""

        args: list[str] = [
            "--host", str(c.get("HOST") or "0.0.0.0"),
            "--port", str(c.get("PORT") or 8180),
            "--model", model_abs,
        ]
        # numeric / value flags that the admin always renders
        pairs = [
            ("--ctx-size", "CTX_SIZE"), ("--threads", "THREADS"),
            ("--threads-batch", "THREADS_BATCH"), ("--batch-size", "BATCH_SIZE"),
            ("--ubatch-size", "UBATCH_SIZE"), ("--parallel", "PARALLEL"),
            ("--n-gpu-layers", "N_GPU_LAYERS"),
            ("--cache-type-k", "CACHE_TYPE_K"), ("--cache-type-v", "CACHE_TYPE_V"),
            ("--predict", "N_PREDICT"), ("--keep", "KEEP"),
            ("--cpu-range", "CPU_RANGE"), ("--poll", "POLL"),
            ("--rope-scaling", "ROPE_SCALING"), ("--rope-scale", "ROPE_SCALE"),
            ("--rope-freq-base", "ROPE_FREQ_BASE"), ("--rope-freq-scale", "ROPE_FREQ_SCALE"),
            ("--numa", "NUMA"), ("--device", "DEVICE"),
            ("--split-mode", "SPLIT_MODE"), ("--tensor-split", "TENSOR_SPLIT"),
            ("--main-gpu", "MAIN_GPU"), ("--fit-target", "FIT_TARGET"),
            ("--fit-ctx", "FIT_CTX"), ("--alias", "ALIAS"),
            ("--api-prefix", "API_PREFIX"), ("--timeout", "TIMEOUT"),
            ("--threads-http", "THREADS_HTTP"), ("--cache-reuse", "CACHE_REUSE"),
            ("--image-min-tokens", "IMAGE_MIN_TOKENS"),
            ("--image-max-tokens", "IMAGE_MAX_TOKENS"),
            ("--reasoning", "REASONING"), ("--reasoning-format", "REASONING_FORMAT"),
            ("--reasoning-budget", "REASONING_BUDGET"),
            ("--chat-template", "CHAT_TEMPLATE"),
            ("--chat-template-kwargs", "CHAT_TEMPLATE_KWARGS"),
        ]
        for flag, key in pairs:
            if has(key):
                args += [flag, str(c[key]).strip()]

        if truthy(c.get("CPU_STRICT")):
            args += ["--cpu-strict", "1"]

        # on/off toggles with explicit negation (only when set)
        def add_bool(key: str, on: str, off: str) -> None:
            if has(key):
                args.append(on if truthy(c[key]) else off)

        add_bool("KV_OFFLOAD", "--kv-offload", "--no-kv-offload")
        add_bool("MMAP", "--mmap", "--no-mmap")
        add_bool("CACHE_PROMPT", "--cache-prompt", "--no-cache-prompt")
        add_bool("ENABLE_SLOTS", "--slots", "--no-slots")
        add_bool("SKIP_CHAT_PARSING", "--skip-chat-parsing", "--no-skip-chat-parsing")

        if has("FIT"):
            args += ["--fit", "on" if truthy(c["FIT"]) else "off"]
        if truthy(c.get("ENABLE_PROPS")):
            args.append("--props")
        if truthy(c.get("ENABLE_CONT_BATCHING")):
            args.append("--cont-batching")
        if truthy(c.get("ENABLE_METRICS")):
            args.append("--metrics")
        if truthy(c.get("ENABLE_MLOCK")):
            args.append("--mlock")

        if mmproj_abs:
            args += ["--mmproj", mmproj_abs]
            args.append("--mmproj-offload" if truthy(c.get("OFFLOAD_MMPROJ")) else "--no-mmproj-offload")

        spec_type_raw = str(c.get("SPEC_TYPE") or "").strip().lower()
        if spec_type_raw == "mtp":
            spec_type_raw = "draft-mtp"
        if spec_abs:
            # External draft model (separate .gguf file).
            spec_type = spec_type_raw or "draft-mtp"
            if spec_type and spec_type != "none":
                args += ["--spec-type", spec_type, "--model-draft", spec_abs]
                draft_gpu = str(c.get("SPEC_DRAFT_N_GPU_LAYERS") or "999").strip()
                draft_max = str(c.get("SPEC_DRAFT_N_MAX") or "").strip()
                draft_min = str(c.get("SPEC_DRAFT_N_MIN") or "").strip()
                if draft_gpu:
                    args += ["--gpu-layers-draft", draft_gpu]
                if draft_max:
                    args += ["--spec-draft-n-max", draft_max]
                if draft_min:
                    args += ["--spec-draft-n-min", draft_min]
        elif spec_type_raw == "draft-mtp":
            # Built-in MTP: MTP layers are embedded in the model weights —
            # no separate draft file needed. Just pass --spec-type and n-max.
            draft_max = str(c.get("SPEC_DRAFT_N_MAX") or "2").strip()
            draft_min = str(c.get("SPEC_DRAFT_N_MIN") or "").strip()
            args += ["--spec-type", "draft-mtp"]
            if draft_max:
                args += ["--spec-draft-n-max", draft_max]
            if draft_min:
                args += ["--spec-draft-n-min", draft_min]
            print(f"[llama-node] built-in MTP enabled: --spec-type draft-mtp --spec-draft-n-max {draft_max}")

        if truthy(c.get("ENABLE_JINJA")):
            args.append("--jinja")
        if truthy(c.get("ENABLE_FLASH_ATTN")):
            args += ["--flash-attn", "on"]
        if not truthy(c.get("ENABLE_WEBUI")):
            args.append("--no-webui")

        # Built-in WebUI tools / MCP proxy (recent llama-server features).
        if truthy(c.get("ENABLE_TOOLS")):
            args += ["--tools", "all"]
        if truthy(c.get("ENABLE_AGENT")):
            args.append("--agent")
        if truthy(c.get("ENABLE_MCP_PROXY")):
            args.append("--ui-mcp-proxy")

        # Fallback: raw extra flags typed in the admin UI, appended verbatim.
        if has("EXTRA_ARGS"):
            try:
                args += shlex.split(str(c["EXTRA_ARGS"]))
            except ValueError:
                args += str(c["EXTRA_ARGS"]).split()
        return args

    # ── llama-node config backups ──────────────────────────────────────────

    # ── cell registry (state.json) — survives agent restarts so cells can be
    #    re-adopted instead of reaped ─────────────────────────────────────────

    def _register_cell(self, port: int, kind: str, pid: int, marker: str,
                       cfg: dict, log_path, cache_models: bool) -> None:
        with self.lock:
            cells = self.state.setdefault("cells", {})
            cells[str(int(port))] = {
                "port": int(port), "kind": kind, "pid": int(pid),
                "marker": str(marker or "")[:200],
                "cfg": {k: v for k, v in (cfg or {}).items() if k != "cmd"},
                "log": str(log_path or ""),
                "cacheModels": bool(cache_models),
                "startedAt": int(time.time()),
            }
            self.save_state()

    def _unregister_cell(self, port: int) -> None:
        with self.lock:
            cells = self.state.get("cells") or {}
            if cells.pop(str(int(port)), None) is not None:
                self.save_state()

    @staticmethod
    def _marker_matches(marker: str, cmdline: str) -> bool:
        """The exec'd argv[0] may be a resolved binary path (python3 → .../MacOS/Python),
        so besides the exact substring also accept the marker's argument tail."""
        if not marker or not cmdline:
            return False
        if marker in cmdline:
            return True
        tail = " ".join(marker.split()[1:])
        return bool(tail) and tail in cmdline

    @staticmethod
    def _pid_cmdline(pid: int) -> str:
        try:
            out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                                 capture_output=True, text=True, timeout=5)
            return out.stdout.strip()
        except Exception:
            return ""

    @staticmethod
    def _port_listener_pid(port: int) -> int:
        """PID LISTENING on <port> (any local address), via `ss`; 0 if none. Lets us
        re-identify a cell by its port when the launch marker no longer matches: a
        wrapper that exec's into another program (run_whisper.sh → exec python)
        rewrites argv, so the marker is gone from ps though the port is still served."""
        want = str(int(port))
        try:
            out = subprocess.run(["ss", "-ltnpH"], capture_output=True,
                                 text=True, timeout=5).stdout
        except Exception:
            return 0
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[3].rsplit(":", 1)[-1] != want:
                continue  # parts[3] is the Local Address:Port column
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
        return 0

    @staticmethod
    def _port_health_ok(port: int, timeout: float = 2.0) -> bool:
        """True if the server on <port> answers GET /health with 2xx. llama-server and
        the whisper cell both expose /health; this confirms a real, healthy cell is
        serving the port before we adopt whatever pid owns it."""
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{int(port)}/health", timeout=timeout) as resp:
                return 200 <= int(getattr(resp, "status", 200) or 200) < 300
        except Exception:
            return False

    def adopt_or_reap_strays(self) -> None:
        """Re-attach cells that survived the agent restart, then reap only the
        truly orphaned llama-server processes.

        The registry (state.json "cells") records every started cell with its
        pid + a cmdline marker. On startup: pid alive AND its command line still
        contains the marker → adopt into a fresh slot (same pid, same uptime —
        deploys stop killing inference). When the marker no longer matches — an
        exec-chained command cell rewrote its argv, or a failed restart clobbered
        the recorded pid — fall back to identity by PORT: adopt whoever is healthily
        serving the cell's port. Anything matching llamaServerBin that was NOT
        adopted is a real stray and gets reaped as before."""
        adopted_pids = set()
        for key, rec in list((self.state.get("cells") or {}).items()):
            try:
                port = int(rec.get("port") or key)
                rec_pid = int(rec.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            marker = str(rec.get("marker") or "")
            cmdline = self._pid_cmdline(rec_pid) if rec_pid > 1 else ""
            if rec_pid > 1 and self._marker_matches(marker, cmdline):
                pid = rec_pid
            else:
                # Marker gone (an exec-chained wrapper like run_whisper.sh → exec
                # python rewrote argv) or the recorded pid was clobbered by a failed
                # restart. Identify the cell by its real contract instead: whoever
                # is healthily serving the cell's PORT right now IS the cell.
                pid = self._port_listener_pid(port)
                if not (pid and self._port_health_ok(port)):
                    self._unregister_cell(port)
                    continue
                if pid != rec_pid:            # re-discovered by port → keep registry honest
                    with self.lock:
                        rec["pid"] = pid
                        self.save_state()
            slot = self._slot(port)
            log = rec.get("log") or ""
            slot.node.adopt(pid, dict(rec.get("cfg") or {}),
                            log_path=Path(log) if log else None,
                            started_at=int(rec.get("startedAt") or 0))
            slot.cache_models = bool(rec.get("cacheModels"))
            self._set_llama_startup(port, phase="running", error="")
            adopted_pids.add(pid)
            print(f"[llama-node] adopted running cell :{port} (pid {pid})")
        self.reap_stray_llama_servers(keep_pids=adopted_pids)

    def reap_stray_llama_servers(self, keep_pids=None) -> None:
        """Kill any llama-server left from a previous agent run.

        With KillMode=process / AbandonProcessGroup the children survive the
        unit restart on purpose — adopt_or_reap_strays() re-attaches the ones
        recorded in the registry and passes their pids in `keep_pids`; whatever
        llama-server remains unmatched is a genuine orphan holding the GPU and
        the port, and is terminated here."""
        keep = {int(p) for p in (keep_pids or set())}
        bin_path = str(self.config.get("llamaServerBin") or "").strip()
        if not bin_path:
            return
        try:
            out = subprocess.run(["pgrep", "-f", bin_path],
                                 capture_output=True, text=True, timeout=5)
            pids = [int(p) for p in out.stdout.split()
                    if p.strip().isdigit() and int(p) != os.getpid()
                    and int(p) not in keep]
        except Exception:
            return
        if not pids:
            return
        print(f"[llama-node] reaping {len(pids)} stray llama-server(s): {pids}")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        time.sleep(2)
        for pid in pids:
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        if not keep:
            try:
                self._purge_model_cache()
            except Exception:
                pass

    def save_llama_node_config(self, model_path: str, port: int,
                               gpu_layers: int, ctx_size: int) -> None:
        """Save a timestamped JSON backup of the launch parameters."""
        self._configs_dir.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        model_name = Path(model_path).name
        data = {
            "savedAt": stamp,
            "savedAtTs": now,
            "modelPath": model_path,
            "modelName": model_name,
            "port": port,
            "gpuLayers": gpu_layers,
            "ctxSize": ctx_size,
        }
        filename = f"llama-node.bak.{stamp}.json"
        target = self._configs_dir / filename
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)

    def list_llama_node_configs(self) -> list[dict[str, Any]]:
        """Return saved launch configs, newest first (max 20)."""
        if not self._configs_dir.is_dir():
            return []
        rows = []
        for p in sorted(self._configs_dir.glob("llama-node.bak.*.json"), reverse=True)[:20]:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                data["filename"] = p.name
                rows.append(data)
            except Exception:
                continue
        return rows

    def delete_llama_node_config(self, filename: str) -> None:
        """Delete a saved config backup by filename (no path traversal)."""
        filename = Path(filename).name  # strip any path components
        if not filename.startswith("llama-node.bak.") or not filename.endswith(".json"):
            raise AppError("invalid backup filename", 400)
        target = self._configs_dir / filename
        if not target.exists():
            raise AppError(f"backup not found: {filename}", 404)
        target.unlink()

    def monitor_nvidia_smi(self) -> dict[str, Any]:
        """Run nvidia-smi and return raw text output for the admin monitor panel."""
        try:
            result = subprocess.run(
                ["nvidia-smi"], text=True, capture_output=True, timeout=5
            )
            ok = result.returncode == 0
            output = (result.stdout if ok else result.stderr or result.stdout).strip()
        except FileNotFoundError:
            ok, output = False, "nvidia-smi not found"
        except Exception as exc:
            ok, output = False, str(exc)
        return {
            "kind": "nvidia-smi",
            "ok": ok,
            "output": output,
            "source": self.config.get("hostId") or self.config.get("displayName") or "remote",
            "time": int(time.time()),
        }

    def llama_node_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Kick off model resolve (local or download) + llama-server start in a
        background thread and return immediately.

        Downloading a multi-GB model can take minutes, far longer than the
        admin's HTTP client timeout, so the heavy work runs off the request
        thread. Progress is reported via llamaNode (phase + bytes) in the
        heartbeat / /api/state.
        """
        req_config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        cell_kind = str(payload.get("cellKind") or req_config.get("CELL_KIND") or "").strip().lower()
        if cell_kind == "command":
            return self._command_cell_start(payload, req_config)

        bin_path = str(self.config.get("llamaServerBin") or "").strip()
        if not bin_path:
            raise AppError("llamaServerBin not configured in config.json — run install.sh first", 400)

        # Full admin form config (all llama.cpp flags). Falls back to a minimal
        # config synthesised from the legacy individual fields for older callers.
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        model_path_raw = str(payload.get("modelPath") or config.get("MODEL_FILE") or "").strip()
        if not model_path_raw:
            raise AppError("modelPath is required", 400)
        if not config:
            config = {
                "MODEL_FILE": model_path_raw,
                "PORT": payload.get("port"),
                "N_GPU_LAYERS": payload.get("gpuLayers"),
                "CTX_SIZE": payload.get("ctxSize"),
            }

        port = int(config.get("PORT") or payload.get("port") or self.config.get("llamaNodeDefaultPort") or 8180)
        config["PORT"] = port
        config.setdefault("HOST", "0.0.0.0")
        slot = self._slot(port)
        if slot.node.status().get("running"):
            return {"ok": False, "error": f"a server is already running on port {port}"}
        phase = self._get_llama_startup(port).get("phase")
        if phase in ("resolving", "downloading", "loading"):
            return {"ok": False, "error": f"startup already in progress on port {port} ({phase})", "phase": phase}

        mmproj_raw = str(config.get("MMPROJ_FILE") or "").strip()
        spec_raw = str(config.get("SPEC_DRAFT_MODEL_FILE") or "").strip()
        cache_models = bool(payload.get("cacheModels", self.config.get("cacheModels", False)))
        slot.cache_models = cache_models

        # Variant 2: controller-supplied argument list (with path placeholders).
        # When present we only substitute paths; the legacy _build_llama_args is
        # a fallback for older controllers that don't send it.
        incoming_args = payload.get("args") if isinstance(payload.get("args"), list) else None

        self._set_llama_startup(
            port, phase="resolving", modelPath=model_path_raw,
            downloadedBytes=0, totalBytes=0, error="", startedAt=int(time.time()),
        )

        # Open the port in ufw so the admin server can reach llama-server.
        # Silently skips if ufw is inactive or passwordless sudo is not set up.
        try:
            import subprocess as _sp
            _sp.run(["sudo", "-n", "ufw", "allow", str(port)],
                    capture_output=True, timeout=5)
        except Exception:
            pass
        threading.Thread(
            target=self._llama_startup_worker,
            args=(port, bin_path, config, model_path_raw, mmproj_raw, spec_raw, cache_models, incoming_args),
            daemon=True,
        ).start()
        return {"ok": True, "status": "starting", "phase": "resolving", "port": port}

    def _command_cell_start(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Start a generic command cell (CELL_KIND="command") on this host.

        Runs an arbitrary managed process (e.g. whisper-server) in the same
        single-process slot as a llama node — no model download, no
        llama-server binary. SECURITY: this executes a controller-supplied shell
        command on this host; only the trusted-LAN admin can reach this endpoint.
        """
        command = re.sub(r"^\s*exec\s+", "",
                         str(payload.get("command") or config.get("COMMAND") or "").strip()).strip()
        if not command:
            raise AppError("command is required for a command cell", 400)
        port = int(config.get("PORT") or payload.get("port")
                   or self.config.get("llamaNodeDefaultPort") or 8180)
        slot = self._slot(port)
        if slot.node.status().get("running"):
            return {"ok": False, "error": f"a server is already running on port {port}"}
        phase = self._get_llama_startup(port).get("phase")
        if phase in ("resolving", "downloading", "loading"):
            return {"ok": False, "error": f"startup already in progress on port {port} ({phase})", "phase": phase}

        # Open the port in ufw so the admin/clients can reach the cell.
        try:
            import subprocess as _sp
            _sp.run(["sudo", "-n", "ufw", "allow", str(port)], capture_output=True, timeout=5)
        except Exception:
            pass

        # Mirror the controller's render: export PORT, then ENV, then cd WORKDIR,
        # then exec the command (as one bash -lc line).
        parts = [f"export PORT={shlex.quote(str(port))}"]
        for raw in re.split(r"[\n,]", str(config.get("ENV") or "")):
            item = raw.strip()
            if not item or item.startswith("#") or "=" not in item:
                continue
            k, v = item.split("=", 1)
            k = k.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                continue
            v = v.strip().replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'export {k}="{v}"')
        workdir = str(config.get("WORKDIR") or "").strip()
        if workdir:
            parts.append(f"cd {shlex.quote(workdir)}")
        parts.append(f"exec {command}")
        shell_line = "; ".join(parts)
        log_path = self._model_cache_dir() / "command-cell.log"
        cfg = {"modelPath": "", "port": port, "cellKind": "command", "command": command}
        # Command cells download nothing — never purge a model cache on stop.
        slot.cache_models = True
        self._set_llama_startup(port, phase="loading", modelPath=command[:80],
                                downloadedBytes=0, totalBytes=0, error="",
                                startedAt=int(time.time()))
        result = slot.node.start_command(shell_line, cfg, log_path=log_path)
        self._set_llama_startup(port, phase="running" if result.get("ok") else "error",
                                error="" if result.get("ok") else (result.get("error") or "start failed"))
        if result.get("ok"):
            # Marker for re-adoption: the exec'd command line with $PORT expanded
            # (the shell resolves it before exec, so ps shows the resolved form).
            marker = command.replace("$PORT", str(port)).replace("~/", "")[:120]
            self._register_cell(port, "command", result.get("pid") or 0, marker,
                                cfg, log_path, slot.cache_models)
        return result

    @staticmethod
    def _resolve_arg_paths(args: list[str], model_abs: str, mmproj_abs: str,
                           spec_abs: str) -> list[str]:
        """Swap the controller's path placeholders for the real downloaded paths."""
        subst = {
            LLAMA_PATH_PLACEHOLDER_MODEL: str(model_abs),
            LLAMA_PATH_PLACEHOLDER_MMPROJ: str(mmproj_abs or ""),
            LLAMA_PATH_PLACEHOLDER_SPEC: str(spec_abs or ""),
        }
        return [subst.get(a, a) for a in args]

    def _llama_startup_worker(self, port: int, bin_path: str, config: dict[str, Any],
                              model_path_raw: str, mmproj_raw: str, spec_raw: str,
                              cache_models: bool = False,
                              incoming_args: list[str] | None = None) -> None:
        slot = self._slot(port)
        try:
            mp, mmproj_abs, spec_abs = self._download_all_model_files(
                model_path_raw, mmproj_raw, spec_raw, use_cache=cache_models, port=port)
        except Exception as exc:
            self._set_llama_startup(port, phase="error", error=str(exc))
            return

        def build_args() -> list[str]:
            # Variant 2: prefer the controller-built arg list (single source of
            # truth) and only substitute paths; fall back to local building.
            if incoming_args:
                return self._resolve_arg_paths(incoming_args, str(mp), mmproj_abs, spec_abs)
            return self._build_llama_args(config, str(mp), mmproj_abs, spec_abs)

        self._set_llama_startup(port, phase="loading")
        gpu_layers = int(config.get("N_GPU_LAYERS") or 999)
        ctx_size = int(config.get("CTX_SIZE") or 4096)
        args = build_args()
        log_path = self._model_cache_dir() / "llama-server.log"
        # Expose specType in the heartbeat so the UI can show the MTP badge
        # even for built-in MTP (where specPath is empty).
        _spec_type_raw = str(config.get("SPEC_TYPE") or "").strip().lower()
        if _spec_type_raw == "mtp":
            _spec_type_raw = "draft-mtp"
        cfg = {"modelPath": str(mp), "mmprojPath": mmproj_abs, "specPath": spec_abs,
               "specType": _spec_type_raw, "port": port,
               "gpuLayers": gpu_layers, "ctxSize": ctx_size}
        artifact = self._write_llama_cell_artifacts(port, bin_path, args, config, cfg)
        cfg["artifact"] = artifact
        result = slot.node.start(bin_path, args, cfg, log_path=log_path)

        # Auto-recovery: if the error looks like a truncated/corrupted cached
        # file, delete the bad files and re-download once before giving up.
        # NOTE: result["error"] may be the generic "exiting due to model loading error"
        # last line — also scan the log directly for corruption patterns.
        if not result.get("ok") and cache_models:
            err = result.get("error") or ""
            log_err = slot.node._read_log_error(log_path)
            if self._is_corruption_error(err) or self._is_corruption_error(log_err):
                print(f"[llama-node] corruption detected in cached file(s), deleting and retrying…")
                for p in [mp, mmproj_abs, spec_abs]:
                    if p:
                        try:
                            Path(p).unlink(missing_ok=True)
                            print(f"[llama-node]   deleted: {p}")
                        except Exception as del_err:
                            print(f"[llama-node]   delete failed for {p}: {del_err}")
                self._set_llama_startup(port, phase="downloading", downloadedBytes=0, totalBytes=0,
                                        downloadingFile="re-downloading…")
                try:
                    mp, mmproj_abs, spec_abs = self._download_all_model_files(
                        model_path_raw, mmproj_raw, spec_raw, use_cache=False, port=port)
                except Exception as exc:
                    self._set_llama_startup(port, phase="error", error=str(exc))
                    return
                self._set_llama_startup(port, phase="loading")
                args = build_args()
                cfg = {"modelPath": str(mp), "mmprojPath": mmproj_abs, "specPath": spec_abs,
                       "specType": _spec_type_raw, "port": port,
                       "gpuLayers": gpu_layers, "ctxSize": ctx_size}
                artifact = self._write_llama_cell_artifacts(port, bin_path, args, config, cfg)
                cfg["artifact"] = artifact
                result = slot.node.start(bin_path, args, cfg, log_path=log_path)

        if result.get("ok"):
            self._set_llama_startup(port, phase="running", error="")
            self._register_cell(port, "llama", result.get("pid") or 0, bin_path,
                                cfg, log_path, cache_models)
            # Manual snapshots only — no auto-save of launch params on start.
            # Caching on ⇒ keep only the active model (don't accumulate on disk).
            # Caching off ⇒ files get purged on stop anyway, no cleanup needed here.
            if cache_models:
                try:
                    self._cleanup_old_models([str(mp), mmproj_abs, spec_abs])
                except Exception:
                    pass
        else:
            self._set_llama_startup(port, phase="error", error=result.get("error") or "start failed")

