"""The cells of this machine: each one on its port, the table of them, what
they look like from outside, and what outlives a scout restart."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from caravan_scout.models import ModelFetcher
from caravan_scout.process import CellProcess, HostProcesses
from caravan_scout.starts import CellStart
from caravan_scout.watchdog import Watchdog

class Cell:
    """One cell on one port: its process, how its start is going, and whether
    its model files stay cached when it stops.

    A machine can hold several cells at once — a translator and a whisper
    cell, say; each keeps its own process, startup record, lock and flag.
    """

    def __init__(self, port: int):
        self.port = int(port)
        self.process = CellProcess()
        self.startup: dict[str, Any] = {"phase": "idle"}
        self.lock = threading.Lock()
        self.cache_models = False
        # Crashes since it was last started by hand (Watchdog); None = none.
        self.crash: dict[str, Any] | None = None


class ServerProbe:
    """What a cell's server says about itself over HTTP, per port: token rates
    and its queue from /metrics (kept ~2 s), the context window a
    llama-server was launched with from /props (kept ~30 s).

    llama-server and vLLM tell the same facts under different names.
    llama.cpp reports its rates itself. vLLM 0.24 (engine V1) exports only
    token counters, so its rates are the counters' growth per second between
    two readings — the aggregate throughput its old avg_*_throughput gauges
    used to say; a first reading has nothing to compare with and says no
    rate. Lines with labels (vLLM's engine, model) are summed per name.

    A server that does not answer says nothing: no metrics, a window of 0 —
    and that silence is kept as long as an answer would be.
    """

    #: Read as they are: Prometheus name -> the view's key.
    GAUGES = {"llamacpp:prompt_tokens_seconds": "promptTps", "llamacpp:predicted_tokens_seconds": "genTps",
              "llamacpp:requests_processing": "requestsProcessing", "llamacpp:kv_cache_usage_ratio": "_kvRatio",
              "vllm:num_requests_running": "requestsProcessing", "vllm:num_requests_waiting": "requestsWaiting"}
    #: Counted: the growth per second between two readings is the rate.
    COUNTERS = {"vllm:prompt_tokens_total": "promptTps", "vllm:generation_tokens_total": "genTps"}

    def __init__(self, clock: Callable[[], float] | None = None):
        # None asks time.time at each reading, so a patched clock is seen.
        self.clock = clock
        self._metrics: dict[Any, tuple[float, dict[str, Any]]] = {}
        self._ctx: dict[Any, tuple[float, int]] = {}
        self._counted: dict[Any, tuple[float, dict[str, float]]] = {}

    def now(self) -> float:
        return self.clock() if self.clock else time.time()

    def metrics(self, port) -> dict[str, Any]:
        """Scrape the cell's /metrics (Prometheus) for live token rates and
        its queue. Cached ~2s. Returns {promptTps, genTps, requestsProcessing,
        requestsWaiting (vLLM)} — each only when the server says it."""
        now = self.now()
        hit = self._metrics.get(port)
        if hit and now - hit[0] < 2:
            return hit[1]
        out: dict[str, Any] = {}
        counted: dict[str, float] = {}
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/metrics", timeout=1) as r:
                for line in r.read().decode("utf-8", "replace").splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    name = line.split("{", 1)[0].split(None, 1)[0]
                    try:
                        num = float(line.rsplit(None, 1)[-1])
                    except (ValueError, IndexError):
                        continue
                    if name in self.GAUGES:
                        key = self.GAUGES[name]
                        out[key] = out.get(key, 0.0) + num
                    elif name in self.COUNTERS:
                        counted[name] = counted.get(name, 0.0) + num
        except Exception:
            out, counted = {}, {}
        for key in ("promptTps", "genTps"):
            if key in out:
                out[key] = round(out[key], 2)
        for key in ("requestsProcessing", "requestsWaiting"):
            if key in out:
                out[key] = int(out[key])
        before = self._counted.get(port)
        if counted:
            if before and now > before[0]:
                for name, value in counted.items():
                    prev = before[1].get(name)
                    # A counter that went down is a restarted server: no rate.
                    if prev is not None and value >= prev:
                        out[self.COUNTERS[name]] = round((value - prev) / (now - before[0]), 2)
            self._counted[port] = (now, counted)
        # Context window the server launched with + live KV-cache occupancy.
        ctx_max = self.ctx_max(port)
        ratio = out.pop("_kvRatio", None)
        if ctx_max:
            out["ctxMax"] = ctx_max
            if ratio is not None:
                out["ctxUsed"] = int(round(ratio * ctx_max))
        self._metrics[port] = (now, out)
        return out

    def ctx_max(self, port) -> int:
        """n_ctx the llama-server was launched with, from /props. Cached ~30s
        PER PORT (same single-slot thrash as Machine.firewall — see its note)."""
        now = self.now()
        hit = self._ctx.get(port)
        if hit and now - hit[0] < 30:
            return hit[1]
        ctx = 0
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/props", timeout=1) as r:
                props = json.loads(r.read().decode("utf-8", "replace"))
            gen = props.get("default_generation_settings") or {}
            ctx = int(gen.get("n_ctx") or props.get("n_ctx") or 0)
        except Exception:
            ctx = 0
        self._ctx[port] = (now, ctx)
        return ctx


class CellRecords:
    """What was started on this machine, kept in state.json under "cells" so
    the next scout start can find it again: one record per port — the pid,
    the marker its command line carries, the config, where it logs.

    Written under the state's one lock, saved at once.
    """

    def __init__(self, state):
        self.state = state

    def add(self, port: int, kind: str, pid: int, marker: str,
            cfg: dict, log_path, cache_models: bool,
            health_path: str = "/health", launch: dict | None = None) -> None:
        with self.state.lock:
            cells = self.state.setdefault("cells", {})
            cells[str(int(port))] = {
                "port": int(port), "kind": kind, "pid": int(pid),
                "marker": str(marker or "")[:200],
                "cfg": {k: v for k, v in (cfg or {}).items() if k != "cmd"},
                "log": str(log_path or ""),
                "cacheModels": bool(cache_models),
                # Kept because re-adoption happens at scout startup, long after
                # the controller told us where this cell answers. A vLLM cell
                # replies on /v1/models; probing /health would bury it.
                "healthPath": str(health_path or "/health"),
                "startedAt": int(time.time()),
                # How it was launched, so a crash after a scout restart can be
                # followed by the same launch (Watchdog).
                **({"launch": dict(launch)} if launch else {}),
            }
            self.state.save()

    def forget(self, port) -> None:
        with self.state.lock:
            cells = self.state.get("cells") or {}
            if cells.pop(str(int(port)), None) is not None:
                self.state.save()

    def items(self) -> list[tuple[str, Any]]:
        return list((self.state.get("cells") or {}).items())

    def get(self, port) -> dict[str, Any]:
        return (self.state.get("cells") or {}).get(str(port)) or {}

    def repoint(self, rec: dict[str, Any], pid: int) -> None:
        """The record's process was found again under another pid."""
        with self.state.lock:
            rec["pid"] = pid
            self.state.save()


class Cells:
    """The cells of this machine by port, and how each looks from outside.

    A cell comes into being the first time anything starts, reports or asks
    about its port, and goes when it is stopped (drop). Its identity is the
    cancellation token of a start: a start that finds its Cell replaced knows
    it was stopped while it worked.
    """

    def __init__(self, machine, config, state):
        self.machine = machine
        self.config = config
        self.by_port: dict[int, Cell] = {}
        self._lock = threading.Lock()
        self.probe = ServerProbe()
        self.records = CellRecords(state)
        self.processes = HostProcesses()
        # The model files are the cells': downloads report into a cell's
        # startup record, and a purge keeps what a running cell holds.
        self.models = ModelFetcher(config, self.report)

    def at(self, port) -> Cell:
        """The cell on `port`, made on first use."""
        port = int(port)
        with self._lock:
            cell = self.by_port.get(port)
            if cell is None:
                cell = self.by_port[port] = Cell(port)
            return cell

    def all(self) -> list[tuple[int, Cell]]:
        """(port, cell) pairs in the order the cells came — a copy."""
        with self._lock:
            return list(self.by_port.items())

    def drop(self, port) -> None:
        with self._lock:
            self.by_port.pop(int(port), None)

    def holds(self, port, cell) -> bool:
        """True while `cell` is still THE cell of this port.

        A stop request replaces/removes the cell object, so identity is the
        cheapest possible cancellation token for the startup worker: no flags,
        no epochs — if the object changed, someone stopped or re-created the
        cell while we were downloading. Checked WITHOUT at(), which would
        re-create an empty cell as a side effect.
        """
        with self._lock:
            return self.by_port.get(int(port)) is cell

    def report(self, port, **fields: Any) -> None:
        """A start says how it is going: fields merged into the cell's record."""
        cell = self.at(port)
        with cell.lock:
            cell.startup.update(fields)

    def startup(self, port) -> dict[str, Any]:
        """A copy of the cell's startup record."""
        cell = self.at(port)
        with cell.lock:
            return dict(cell.startup)

    def view(self, cell: Cell) -> dict[str, Any]:
        """The process's status merged with its start's phase and progress, so
        the admin sees downloading/loading state before the server is up."""
        port = cell.port
        st = cell.process.status()
        with cell.lock:
            startup = dict(cell.startup)
            crash = Watchdog.public(cell.crash)
        if crash:
            st = {**st, "crash": crash}
        phase = startup.get("phase")
        if st.get("running"):
            p = st.get("port") or port
            metrics = self.probe.metrics(p) if p else {}
            view = {**st, "port": p, "phase": "running", **metrics,
                    "firewall": self.machine.firewall(p) if p else {}}
            # Whether its port answers here yet: vLLM installs and loads for
            # minutes before it listens, and from the controller a silent
            # port looks the same as a firewall. Not listening, the cell says
            # its last log lines — where the start is.
            listening = self.machine.listening_ports()
            if p and listening is not None:
                view["listening"] = int(p) in listening
                if not view["listening"]:
                    view["startingTail"] = cell.process.log_tail()
            return view
        # Crashed shortly after start (non-zero exit) — surface as error even if
        # the startup worker already marked it "running".
        if st.get("crashed"):
            reason = st.get("lastError") or Watchdog.how(st)
            if crash and crash.get("gaveUp"):
                # Said on the card: the watchdog stopped trying, and why.
                reason = (f"crashed {crash['count']} times in {Watchdog.WINDOW_SEC // 60} minutes — "
                          f"not restarting it: {crash.get('reason') or reason}")
            return {**st, "phase": "error", "port": startup.get("port") or port,
                    "modelPath": startup.get("modelPath", ""), "lastError": reason}
        if phase in ("resolving", "downloading", "loading"):
            return {
                **st, "running": False, "phase": phase,
                "modelPath": startup.get("modelPath", ""),
                "port": startup.get("port") or port,
                "downloadedBytes": startup.get("downloadedBytes", 0),
                "totalBytes": startup.get("totalBytes", 0),
                "downloadingFile": startup.get("downloadingFile", ""),
                "startedAt": startup.get("startedAt"),
            }
        if phase == "error":
            return {**st, "port": port, "phase": "error",
                    "lastError": startup.get("error") or st.get("lastError", "")}
        return {**st, "port": port}

    def views(self) -> list[dict[str, Any]]:
        return [self.view(cell) for _port, cell in self.all()]

    def first_view(self) -> dict[str, Any]:
        """The single-cell view older controllers read: the first cell's."""
        nodes = self.views()
        return nodes[0] if nodes else {"running": False, "phase": "idle"}

    def held_files(self) -> list[str]:
        """The model files the running cells hold: what a cache purge keeps."""
        keep: list[str] = []
        for _port, cell in self.all():
            keep.extend(cell.process.held_files())
        return keep

    # ── what outlives a scout restart ───────────────────────────────────────

    def adopt_survivors(self) -> None:
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
        for key, rec in self.records.items():
            try:
                port = int(rec.get("port") or key)
                rec_pid = int(rec.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            marker = str(rec.get("marker") or "")
            cmdline = self.processes.cmdline(rec_pid) if rec_pid > 1 else ""
            if rec_pid > 1 and self.processes.marker_matches(marker, cmdline):
                pid = rec_pid
            else:
                # Marker gone (an exec-chained wrapper like run_whisper.sh → exec
                # python rewrote argv) or the recorded pid was clobbered by a failed
                # restart. Identify the cell by its real contract instead: whoever
                # is healthily serving the cell's PORT right now IS the cell.
                pid = self.processes.listener(port)
                if not pid:
                    self.records.forget(port)   # nothing serves it — really gone
                    continue
                if self.processes.owned(pid) is False:
                    # Someone else serves the port now — the controller's own
                    # cell, a hand-run server. Ours is gone; theirs stays theirs.
                    print(f"[llama-node] :{port} is served by pid {pid}, which no scout "
                          f"started — not adopting it")
                    self.records.forget(port)
                    continue
                if not self.processes.healthy(port, timeout=4.0, attempts=3,
                                              health_path=rec.get("healthPath") or "/health"):
                    # Something owns the port but stayed quiet. On a loaded host
                    # that is a timeout, not a death — and unregistering here
                    # stranded a live cell as "stopped" forever while its process
                    # kept serving traffic, with no way back short of killing it.
                    # Keep the record, adopt the listener, let polling set phase.
                    print(f"[llama-node] :{port} holds the port but "
                          f"{rec.get('healthPath') or '/health'} stayed quiet — "
                          f"adopting anyway rather than forgetting it")
                if pid != rec_pid:            # re-discovered by port → keep registry honest
                    self.records.repoint(rec, pid)
            cell = self.at(port)
            log = rec.get("log") or ""
            cell.process.adopt(pid, dict(rec.get("cfg") or {}),
                               log_path=Path(log) if log else None,
                               started_at=int(rec.get("startedAt") or 0),
                               launch=rec.get("launch"))
            cell.cache_models = bool(rec.get("cacheModels"))
            self.report(port, phase="running", error="")
            adopted_pids.add(pid)
            print(f"[llama-node] adopted running cell :{port} (pid {pid})")
        self.reap_strays(keep_pids=adopted_pids)

    def reap_strays(self, keep_pids=None) -> None:
        """Kill any llama-server left from a previous agent run.

        With KillMode=process / AbandonProcessGroup the children survive the
        unit restart on purpose — adopt_survivors() re-attaches the ones
        recorded in the registry and passes their pids in `keep_pids`; whatever
        llama-server a scout started and nobody adopted is a genuine orphan
        holding the GPU and the port, and is terminated here.

        Only a process a scout started (HostProcesses.owned): it used to be any
        process running the same binary, and on a machine the scout shares
        with the controller that is the controller's own cells."""
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
        pids = [pid for pid in pids if self.processes.owned(pid) is True]
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
                self.models.purge()
            except Exception:
                pass

    # ── stopping ────────────────────────────────────────────────────────────

    def stop(self, port=None) -> dict[str, Any]:
        """Stop one cell (`port`) or every cell (no port), and say how each went.

        A stopped cell leaves the table and the registry, and the model cache
        loses what no running cell holds — unless the cell keeps its models
        cached. A stop that could not verify must not erase the registry
        entry: that would turn a recoverable cell into a genuinely unowned
        process."""
        ports = [int(port)] if port else [p for p, _ in self.all()]
        results = []
        purge_any = False
        for p in ports:
            cell = self.at(p)
            res = cell.process.stop()
            if res.get("detail") == "not running":
                res = self._reclaim(p, res)
            results.append(res)
            if not res.get("ok"):
                continue
            self.report(p, phase="idle", error="", downloadedBytes=0, totalBytes=0)
            # Don't keep models on client disks (unless caching is on).
            if not cell.cache_models:
                purge_any = True
            self.drop(p)   # a stopped cell disappears from the fleet view
            self.records.forget(p)
        # Purge once, after the stopped cells are dropped, via the SAFE variant
        # so a model still served by another running cell isn't evicted
        # (stopping whisper must not delete the translator gguf).
        if purge_any:
            try:
                self.purge_models_safely()
            except Exception:
                pass
        return results[0] if len(results) == 1 else {"ok": True, "results": results}

    def _reclaim(self, port: int, res: dict[str, Any]) -> dict[str, Any]:
        """"not running" from a process with no handles means it consulted
        NOTHING — the port may still be served by a process this scout lost
        track of (that is precisely how a stop once reported success while
        10.7 GB stayed occupied). Verify the port; kill only what we can
        recognize as ours, never an arbitrary listener."""
        lpid = self.processes.listener(port)
        if not lpid:
            return res
        cmd = self.processes.cmdline(lpid)
        mark = str(self.records.get(port).get("marker") or "")
        bin_path = str(self.config.get("llamaServerBin") or "")
        if (mark and self.processes.marker_matches(mark, cmd)) or (bin_path and bin_path in cmd):
            try:
                os.kill(lpid, signal.SIGTERM)
                deadline = time.time() + 10
                while time.time() < deadline and self.processes.cmdline(lpid):
                    time.sleep(0.3)
                if self.processes.cmdline(lpid):
                    os.kill(lpid, signal.SIGKILL)
                return {"ok": True, "reclaimed": True, "pid": lpid}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"reclaim failed: {exc}", "listenerPid": lpid}
        return {"ok": False, "listenerPid": lpid,
                "error": f"port {port} is held by an unrecognized "
                         f"process (pid {lpid}) — not killing it"}

    # ── starting ────────────────────────────────────────────────────────────

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Start a cell as the controller asks: a llama cell or a command cell,
        whichever the payload names (starts.py). A start by hand clears the
        cell's crash count: it counts since the operator last touched it."""
        start = CellStart.of(self, payload)
        try:
            port = int(start.port())
        except (TypeError, ValueError):
            port = None
        with self._lock:
            cell = self.by_port.get(port) if port is not None else None
        if cell is not None:            # only a cell that exists: a refusal leaves no slot
            with cell.lock:
                cell.crash = None
        return start.run()

    def purge_models_safely(self) -> dict[str, Any]:
        """On-demand cache purge. Keeps the currently running cells' files so a
        live server isn't broken: which files those are is the cells' to say,
        the fetcher only deletes."""
        return self.models.purge(keep=self.held_files())

