"""Starting a cell: the controller's request, accepted or refused, and the
launch that follows it — a llama-server or whatever a command cell runs."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from caravan_scout.cell_assets import CellAssets
from caravan_scout.errors import AppError
from caravan_scout.paths import (LLAMA_PATH_PLACEHOLDER_MMPROJ, LLAMA_PATH_PLACEHOLDER_MODEL,
                                 LLAMA_PATH_PLACEHOLDER_SPEC, SERVER_CELLS_DIR)


class CellArtifacts:
    """What a llama cell leaves next to itself under var/server-cells/<port>/:
    start.sh — the exact command, runnable by hand — and cell.json — what it
    was started with. Both are replaced atomically: a reader of the old file
    sees it whole."""

    def __init__(self, config):
        self.config = config

    @staticmethod
    def dir_of(port) -> Path:
        return SERVER_CELLS_DIR / str(int(port))

    def write(self, port: int, bin_path: str, args: list[str],
              config: dict[str, Any], runtime_cfg: dict[str, Any]) -> dict[str, Any]:
        cell_dir = self.dir_of(port)
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


class CellStart:
    """One start request, from the controller's payload to an answer: a cell
    on its way up, or a refusal that says why.

    A llama cell and a command cell start differently — LlamaStart answers at
    once and hands the slow part to a LlamaLaunch in the background,
    CommandStart does all of it on the request. Which one a payload is, the
    payload says: cellKind, or CELL_KIND in its config (KINDS, below). What
    both share is here: the port, the refusal when the port is busy, and
    opening the firewall.
    """

    #: cell kind -> the start that runs it; anything else is a llama cell.
    KINDS: dict[str, type[CellStart]] = {}

    def __init__(self, cells, payload: dict[str, Any]):
        self.cells = cells
        self.payload = payload
        self.config = payload.get("config") if isinstance(payload.get("config"), dict) else {}

    @classmethod
    def of(cls, cells, payload: dict[str, Any]) -> CellStart:
        req_config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        kind = str(payload.get("cellKind") or req_config.get("CELL_KIND") or "").strip().lower()
        return cls.KINDS.get(kind, LlamaStart)(cells, payload)

    def run(self) -> dict[str, Any]:
        raise NotImplementedError

    def hints(self) -> dict[str, Any]:
        """Where the controller reads each model file, keyed by the path this
        scout is sent (ModelFetcher.in_place)."""
        hints = self.payload.get("inPlace")
        return hints if isinstance(hints, dict) else {}

    @staticmethod
    def models_root(model_raw: str, model_abs: str) -> dict[str, str]:
        """LLAMA_MODELS_DIR for a command that names its model as
        ${LLAMA_MODELS_DIR:-…}/<path>: the folder the model really sits under
        here — read in place, or in this scout's cache. The command used to
        fall back to ~/llama.cpp/models while the download went to the cache,
        and the two never met. A value the operator set is exported by the
        start line itself and wins."""
        raw = str(model_raw or "").strip().strip("/")
        if not raw or not model_abs or Path(model_raw).is_absolute():
            return {}
        path = str(model_abs).rstrip("/")
        if not path.endswith("/" + raw):
            return {}
        return {"LLAMA_MODELS_DIR": path[: -len(raw) - 1] or "/"}

    def port(self) -> int:
        return int(self.config.get("PORT") or self.payload.get("port")
                   or self.cells.config.get("llamaNodeDefaultPort") or 8180)

    def busy(self, port: int) -> dict[str, Any] | None:
        """The refusal when the port already runs a server or a start is under
        way there; None when it is free."""
        cell = self.cells.at(port)
        if cell.process.status().get("running"):
            return {"ok": False, "error": f"a server is already running on port {port}"}
        phase = self.cells.startup(port).get("phase")
        if phase in ("resolving", "downloading", "loading"):
            return {"ok": False, "error": f"startup already in progress on port {port} ({phase})", "phase": phase}
        return None

    def short_of_vram(self, port: int) -> dict[str, Any] | None:
        """The refusal when the card cannot hold what the cell reserves the
        moment it starts; None when it can, or when the start reserves
        nothing up front.

        How much a runner reserves is the controller's knowledge (vLLM takes
        utilization × the card); it sends it with the start as `vram`
        {device, reserveMiB, who, why, lower}. What is free is this machine's,
        read now — also when the start comes from autostart at boot, where
        two cells may want one card. Refused rather than started: otherwise
        the cell dies in a crash loop a minute later. Best effort, like the
        controller's own check: no nvidia-smi, no such card — no refusal."""
        want = self.payload.get("vram")
        if not isinstance(want, dict):
            return None
        try:
            device, reserve = str(int(want.get("device") or 0)), float(want.get("reserveMiB") or 0)
        except (TypeError, ValueError):
            return None
        card = next((g for g in self.cells.machine.nvidia_gpus() if str(g.get("index")) == device), None)
        try:
            free = float((card or {}).get("memoryFreeMiB"))
        except (TypeError, ValueError):
            return None
        if reserve <= 0 or free >= reserve:
            return None
        holders = sorted(f":{p}" for p, cell in self.cells.all()
                         if p != port and cell.process.status().get("running"))
        lower = str(want.get("lower") or "what it reserves")
        hint = f" — stop {', '.join(holders)} or lower {lower}" if holders else f" — lower {lower}"
        who, why = str(want.get("who") or "the cell"), str(want.get("why") or "")
        return {"ok": False, "error": f"{who} wants {reserve / 1024:.1f} GiB reserved{f' ({why})' if why else ''} "
                                      f"but only {free / 1024:.1f} GiB VRAM is free on GPU {device}{hint}"}

    @staticmethod
    def open_firewall(port: int) -> None:
        """Open the port in ufw so the admin can reach the cell. Silently
        skips if ufw is inactive or passwordless sudo is not set up."""
        try:
            subprocess.run(["sudo", "-n", "ufw", "allow", str(port)],
                           capture_output=True, timeout=5)
        except Exception:
            pass


class LlamaStart(CellStart):
    """A llama cell's start: checked on the request, launched in the background.

    Downloading a multi-GB model can take minutes, far longer than the
    admin's HTTP client timeout, so the heavy work runs off the request
    thread. Progress is reported via llamaNode (phase + bytes) in the
    heartbeat / /api/state.
    """

    def run(self) -> dict[str, Any]:
        bin_path = str(self.cells.config.get("llamaServerBin") or "").strip()
        if not bin_path:
            raise AppError("llamaServerBin not configured in config.json — run install.sh first", 400)

        # Full admin form config (all llama.cpp flags). Falls back to a minimal
        # config synthesised from the legacy individual fields for older callers.
        payload, config = self.payload, self.config
        model_path_raw = str(payload.get("modelPath") or config.get("MODEL_FILE") or "").strip()
        if not model_path_raw:
            raise AppError("modelPath is required", 400)
        if not config:
            config = self.config = {
                "MODEL_FILE": model_path_raw,
                "PORT": payload.get("port"),
                "N_GPU_LAYERS": payload.get("gpuLayers"),
                "CTX_SIZE": payload.get("ctxSize"),
            }

        port = self.port()
        config["PORT"] = port
        config.setdefault("HOST", "0.0.0.0")
        refusal = self.busy(port)
        if refusal:
            return refusal
        cell = self.cells.at(port)

        mmproj_raw = str(config.get("MMPROJ_FILE") or "").strip()
        spec_raw = str(config.get("SPEC_DRAFT_MODEL_FILE") or "").strip()
        cache_models = bool(payload.get("cacheModels", self.cells.config.get("cacheModels", False)))
        cell.cache_models = cache_models

        # Variant 2: the controller supplies the argument list (with path
        # placeholders) and this scout only substitutes the real paths.
        incoming_args = payload.get("args") if isinstance(payload.get("args"), list) else None

        self.cells.report(
            port, phase="resolving", modelPath=model_path_raw,
            downloadedBytes=0, totalBytes=0, error="", startedAt=int(time.time()),
        )
        self.open_firewall(port)
        launch = LlamaLaunch(self.cells, port, bin_path, config, model_path_raw,
                             mmproj_raw, spec_raw, cache_models, incoming_args,
                             hints=self.hints())
        threading.Thread(target=launch.run, daemon=True).start()
        return {"ok": True, "status": "starting", "phase": "resolving", "port": port}


class LlamaLaunch:
    """The slow half of a llama cell's start, off the request thread: fetch
    the model files, write the cell's artifacts, start llama-server and
    register it — or give up cleanly when the cell was stopped meanwhile."""

    def __init__(self, cells, port: int, bin_path: str, config: dict[str, Any],
                 model: str, mmproj: str = "", spec: str = "",
                 cache_models: bool = False, args: list[str] | None = None,
                 hints: dict[str, Any] | None = None):
        self.hints = dict(hints or {})   # where the controller reads each file
        self.cells = cells
        self.port = port
        self.bin_path = bin_path
        self.config = config
        self.model = model
        self.mmproj = mmproj
        self.spec = spec
        self.cache_models = cache_models
        self.args = args

    @staticmethod
    def resolve_paths(args: list[str], model_abs: str, mmproj_abs: str,
                      spec_abs: str) -> list[str]:
        """Swap the controller's path placeholders for the real downloaded paths."""
        subst = {
            LLAMA_PATH_PLACEHOLDER_MODEL: str(model_abs),
            LLAMA_PATH_PLACEHOLDER_MMPROJ: str(mmproj_abs or ""),
            LLAMA_PATH_PLACEHOLDER_SPEC: str(spec_abs or ""),
        }
        return [subst.get(a, a) for a in args]

    def run(self) -> None:
        port, bin_path, config = self.port, self.bin_path, self.config
        cache_models, incoming_args = self.cache_models, self.args
        cells, models = self.cells, self.cells.models
        cell = cells.at(port)
        try:
            mp, mmproj_abs, spec_abs = models.download_all(
                self.model, self.mmproj, self.spec, use_cache=cache_models, port=port,
                hints=self.hints)
        except Exception as exc:
            cells.report(port, phase="error", error=str(exc))
            return

        def build_args() -> list[str]:
            # The controller builds the arg list; this scout only substitutes the
            # paths of files it downloaded. It used to carry its own builder as a
            # fallback — a 130-line mirror of the admin's, already 23 flags behind
            # (no --api-key, --embeddings, --context-shift, --ssl-*…). A cell
            # started through it looked configured on the board and ran without
            # half of that config. Refusing is the honest answer.
            if not incoming_args:
                raise AppError(
                    "controller sent no args for this llama cell — it is older "
                    "than this agent (needs lama-caravan v1.3.115+)", 400)
            return self.resolve_paths(incoming_args, str(mp), mmproj_abs, spec_abs)

        cells.report(port, phase="loading")
        # A string field: an exact count, or "auto"/"all" (llama.cpp fits what
        # the device holds and leaves the rest in RAM). int() on "auto" raises,
        # which would have turned the one setting that makes an oversized model
        # start into a failure to start at all. Bookkeeping only — the real
        # -ngl comes from the args the controller sends.
        _ngl_raw = str(config.get("N_GPU_LAYERS") or "").strip().lower()
        try:
            gpu_layers = int(_ngl_raw) if _ngl_raw and _ngl_raw not in ("auto", "all", "max") else 999
        except ValueError:
            gpu_layers = 999
        ctx_size = int(config.get("CTX_SIZE") or 4096)
        args = build_args()
        # Per PORT, not one file for the whole host. Every llama cell used to
        # write to llama-server.log and each new start renamed it away, while a
        # running cell's fd followed the old inode — so a crashed cell's card
        # quoted whichever cell had spawned last. That is how :8011's "Model
        # loading failed" ended up showing a benign tokenizer warning from the
        # Qwen cell on :8006 instead of its own out-of-VRAM error.
        log_path = models.cache_dir() / f"llama-server.{int(port)}.log"
        # Expose specType in the heartbeat so the UI can show the MTP badge
        # even for built-in MTP (where specPath is empty).
        _spec_type_raw = str(config.get("SPEC_TYPE") or "").strip().lower()
        if _spec_type_raw == "mtp":
            _spec_type_raw = "draft-mtp"
        cfg = {"modelPath": str(mp), "mmprojPath": mmproj_abs, "specPath": spec_abs,
               "specType": _spec_type_raw, "port": port,
               "gpuLayers": gpu_layers, "ctxSize": ctx_size}
        artifacts = CellArtifacts(cells.config)
        cfg["artifact"] = artifacts.write(port, bin_path, args, config, cfg)
        # A Stop that arrived while we were downloading has already dropped the
        # cell and unregistered it. Starting now would resurrect a process
        # nobody owns — exactly how a llama-server once survived with 10.7 GB of
        # VRAM while the board showed its port as stopped. Identity is the check:
        # Cells.drop removed OUR object, so a fresh lookup no longer returns it.
        if not cells.holds(port, cell):
            print(f"[llama-node] :{port} start cancelled — the cell was stopped mid-download")
            return
        result = cell.process.start(bin_path, args, cfg, log_path=log_path)

        # Auto-recovery: if this start failed on a truncated/corrupted cached
        # file, delete the bad files and re-download once before giving up.
        # Only this attempt's own error: a failed start never ran, so the log on
        # the port is the PREVIOUS run's — reading it deleted a good model when
        # the binary was missing and an old run had said "corrupted".
        if not result.get("ok") and cache_models:
            err = result.get("error") or ""
            if models.is_corruption_error(err):
                print(f"[llama-node] corruption detected in cached file(s), deleting and retrying…")
                mine = [p for p in (mp, mmproj_abs, spec_abs) if p and models.downloaded.has(p)]
                others = [p for p in (mp, mmproj_abs, spec_abs) if p and p not in mine]
                if others:
                    # Not downloaded by this scout: read in place from a library, or
                    # in the cache from before the scout kept a record. Not its to
                    # delete — a library's file is nobody's cache.
                    cells.report(port, phase="error",
                                 error=f"the model file looks damaged: {others[0]} — this scout did "
                                       f"not download it, so it left it alone; replace or delete it "
                                       f"on this machine")
                    return
                for p in mine:
                    try:
                        Path(p).unlink(missing_ok=True)
                        models.downloaded.forget(p)
                        print(f"[llama-node]   deleted: {p}")
                    except Exception as del_err:
                        print(f"[llama-node]   delete failed for {p}: {del_err}")
                cells.report(port, phase="downloading", downloadedBytes=0, totalBytes=0,
                             downloadingFile="re-downloading…")
                try:
                    mp, mmproj_abs, spec_abs = models.download_all(
                        self.model, self.mmproj, self.spec, use_cache=False, port=port,
                        hints=self.hints)
                except Exception as exc:
                    cells.report(port, phase="error", error=str(exc))
                    return
                cells.report(port, phase="loading")
                args = build_args()
                cfg = {"modelPath": str(mp), "mmprojPath": mmproj_abs, "specPath": spec_abs,
                       "specType": _spec_type_raw, "port": port,
                       "gpuLayers": gpu_layers, "ctxSize": ctx_size}
                cfg["artifact"] = artifacts.write(port, bin_path, args, config, cfg)
                result = cell.process.start(bin_path, args, cfg, log_path=log_path)

        if result.get("ok") and not cells.holds(port, cell):
            # Stopped between our start and here (the corruption retry keeps this
            # window open for a re-download). Registering would re-add the cell
            # the stop just deleted; letting the process live would orphan it.
            print(f"[llama-node] :{port} stopped during startup — terminating the fresh process")
            try:
                cell.process.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[llama-node] :{port} cleanup stop failed: {exc}")
            return
        if result.get("ok"):
            cells.report(port, phase="running", error="")
            cells.records.add(port, "llama", result.get("pid") or 0, bin_path,
                              cfg, log_path, cache_models, launch=cell.process.launch_spec())
            # Manual snapshots only — no auto-save of launch params on start.
            # Caching on ⇒ keep only the active model (don't accumulate on disk).
            # Caching off ⇒ files get purged on stop anyway, no cleanup needed here.
            if cache_models and self.cells.config.get("cleanOldModels"):
                try:
                    # What the neighbours run stays too: the cleanup used to keep
                    # only this cell's files and took a running cell's model.
                    models.cleanup_old([str(mp), mmproj_abs, spec_abs, *cells.held_files()])
                except Exception:
                    pass
        else:
            cells.report(port, phase="error", error=result.get("error") or "start failed")


class CommandStart(CellStart):
    """A generic command cell (CELL_KIND="command"), started on the request.

    Runs an arbitrary managed process (e.g. whisper-server) in the same
    single-process cell as a llama node — no llama-server binary, and a model
    only when the command names one. SECURITY: this executes a
    controller-supplied shell command on this host; only the trusted-LAN
    admin can reach this endpoint.
    """

    def run(self) -> dict[str, Any]:
        payload, config, cells = self.payload, self.config, self.cells
        command = re.sub(r"^\s*exec\s+", "",
                         str(payload.get("command") or config.get("COMMAND") or "").strip()).strip()
        if not command:
            raise AppError("command is required for a command cell", 400)
        port = self.port()
        refusal = self.busy(port) or self.short_of_vram(port)
        if refusal:
            return refusal
        cell = cells.at(port)

        # Open the port in ufw so the admin/clients can reach the cell.
        self.open_firewall(port)

        # The controller sends the whole start line — shell flags, exports,
        # workdir, exec. This scout used to rebuild it from `command` plus the
        # config, mirroring the controller's script renderer, and the mirror had
        # already lost `set -euo pipefail`: one config, two behaviours depending
        # on which host ran the cell. Refusing beats guessing.
        shell_line = str(payload.get("shellLine") or "").strip()
        if not shell_line:
            raise AppError(
                "controller sent no shellLine for this command cell — it is older "
                "than this agent (needs lama-caravan v1.3.115+)", 400)
        log_path = cells.models.cache_dir() / f"command-cell.{int(port)}.log"
        # A command cell used to mean "no model, ever". The transcribe runner
        # broke that: its model is a GGUF PATH like a llama cell's, and the
        # command the controller sends names it under the models dir. So a
        # download CAN be needed here — and without one the failure was quiet:
        # the cell came up healthy on its port and only said "model file not
        # found" inside its own log.
        #
        # modelPath is filled in for a second reason. The safe purge keeps the
        # files of RUNNING cells by reading exactly this key; left empty, a
        # cache purge deletes the weights out from under a running recognizer
        # — the kind of bug that surfaces weeks later.
        model_raw = str(config.get("MODEL_FILE") or "").strip()
        model_abs = ""
        if model_raw:
            cells.report(port, phase="resolving", modelPath=model_raw,
                         downloadedBytes=0, totalBytes=0, error="",
                         startedAt=int(time.time()))
            try:
                model_abs = str(cells.models.ensure(model_raw, report=True,
                                                    use_cache=True, port=port,
                                                    hint=self.hints().get(model_raw)))
            except Exception as exc:  # noqa: BLE001
                cells.report(port, phase="error", error=str(exc))
                return {"ok": False, "error": f"model not available: {exc}"}
        cfg = {"modelPath": model_abs, "port": port, "cellKind": "command", "command": command}
        # Whatever else a command cell runs is not downloadable, so its cache is
        # never purged on stop.
        cell.cache_models = True
        cells.report(port, phase="loading", modelPath=command[:80],
                     downloadedBytes=0, totalBytes=0, error="",
                     startedAt=int(time.time()))
        # The controller owns the cell servers; pick up its current copy before
        # running the launcher this command names. Never fatal — see CellAssets.
        try:
            synced = CellAssets(
                str(cells.config.get("controllerUrl") or ""),
                cells.config.headers(),
                log=lambda m: print(f"[llama-node] {m}")).sync(command)
            if synced:
                print(f"[llama-node] cell-assets :{port} — " +
                      ", ".join(f"{k}={v}" for k, v in synced.items()))
        except Exception as exc:  # noqa: BLE001
            print(f"[llama-node] cell-assets :{port} skipped ({exc})")
        result = cell.process.start_command(shell_line, cfg, log_path=log_path,
                                            extra_env=self.models_root(model_raw, model_abs))
        cells.report(port, phase="running" if result.get("ok") else "error",
                     error="" if result.get("ok") else (result.get("error") or "start failed"))
        if result.get("ok"):
            # Marker for re-adoption: the exec'd command line with $PORT expanded
            # (the shell resolves it before exec, so ps shows the resolved form).
            marker = command.replace("$PORT", str(port)).replace("~/", "")[:120]
            cells.records.add(port, "command", result.get("pid") or 0, marker,
                              cfg, log_path, cell.cache_models,
                              health_path=str(payload.get("healthPath") or "/health"),
                              launch=cell.process.launch_spec())
        return result


CellStart.KINDS = {"command": CommandStart}
