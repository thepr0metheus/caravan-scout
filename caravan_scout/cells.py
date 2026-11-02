"""The cells of this machine: each one on its port, the table of them, what
they look like from outside, and what outlives a scout restart."""
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from caravan_scout.cache_keeper import CacheKeeper
from caravan_scout.models import CacheFolder, CopyPace, ModelFetcher
from caravan_scout.process import CellLog, CellProcess, HostProcesses
from caravan_scout.prism import PrismRuntime
from caravan_scout.starts import CellStart
from caravan_scout.watchdog import Watchdog

class RunPace:
    """How fast a cell's run reads (2.43): the window of its bytes read (CopyPace), kept for one process — a new run of the cell
    starts the window again, and a count that went back (a child that left) does too."""

    def __init__(self, clock: Callable[[], float] | None = None):
        self.clock = clock
        self.pid = 0
        self.pace = CopyPace(clock)

    def read(self, pid: Any, done: int | None) -> int | None:
        """The run's bytes read now; its pace over the last seconds, or None while not measured."""
        if done is None:
            return None
        if pid != self.pid:
            self.pid, self.pace = pid, CopyPace(self.clock)
        self.pace.add(done)
        return self.pace.rate()


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
        # The memory its start promised to keep resident (MemoryLedger), in MiB; 0 = none promised.
        self.ram_mib = 0
        # Crashes since it was last started by hand (Watchdog); None = none.
        self.crash: dict[str, Any] | None = None
        # Whether THIS run's port has listened yet (Watchdog): None = not watched (the machine will not say what listens),
        # False = watched and silent so far, True = it listened — the run got ready.
        self.listened: bool | None = None
        # What its model is called at the machine's entry (2.27, entry.ModelName), from the start
        # that made it — said from the start's first moment, so a model being loaded is found by its
        # name as "starting"; "" for a cell no start of this scout made (its record keeps the name).
        self.name = ""
        # The files its present start asked the cache for, as the controller named them (Cells.asks): what its stop lets go of
        # when it keeps no cache. [] for a start that asked for none — a cell in an engine — and for a cell no start of this
        # scout made (one adopted after a restart): its stop lets go of nothing, and the keeper trims the cache in its time.
        self.asked: list[str] = []
        # How fast its run reads its model while it loads (2.43): the board's load line.
        self.reading = RunPace()


class ServerProbe:
    """What a cell's server says about itself over HTTP, per port: token rates
    and its queue from /metrics (kept ~2 s), the context window a
    llama-server was launched with and how many requests it takes at once
    from /props (kept ~30 s).

    llama-server and vLLM tell the same facts under different names, and
    Strata tells them in vLLM's names and its own (`strata:`) — but only in
    Prometheus text, which it gives to a reader that asks for it (`Accept:
    text/plain`); asked without, it answers the JSON of its Monitor tab,
    which carries no sample this reader knows (2.35.1: until then a Strata
    cell showed no token speed, no queue and no context use).
    llama.cpp reports rates for the window since the last scrape; newer
    builds (including PrismML) reset that window after every scrape. Its
    cumulative token/compute-time pairs recover the speed even if another
    reader consumed the window, and keep the last measured speed while idle.
    vLLM 0.24 (engine V1) exports only
    token counters, so its rates are the counters' growth per second between
    two readings — the aggregate throughput its old avg_*_throughput gauges
    used to say; a first reading has nothing to compare with and says no
    rate. Lines with labels (vLLM's engine, model) are summed per name.

    A server that does not answer says nothing: no metrics, a window of 0 —
    and that silence is kept as long as an answer would be. A server that
    answers it keeps none (404, 501 — llama.cpp started without --metrics)
    says that: `noMetrics` (2.41).
    """

    #: Read as they are: Prometheus name -> the view's key.
    GAUGES = {"llamacpp:prompt_tokens_seconds": "promptTps", "llamacpp:predicted_tokens_seconds": "genTps",
              "llamacpp:requests_processing": "requestsProcessing", "llamacpp:kv_cache_usage_ratio": "_kvRatio",
              "vllm:num_requests_running": "requestsProcessing", "vllm:num_requests_waiting": "requestsWaiting",
              # vLLM's own number is its KV block pool, and a vLLM server says no context window to take a share of (no /props);
              # Strata says the running request's share of its context here.
              "vllm:kv_cache_usage_perc": "_kvRatio"}
    #: Counted: the growth per second between two readings is the rate.
    COUNTERS = {"vllm:prompt_tokens_total": "promptTps", "vllm:generation_tokens_total": "genTps"}
    #: Token counts divided by engine compute seconds, not elapsed wall time.
    TIMED_COUNTERS = {
        "promptTps": ("llamacpp:prompt_tokens_total", "llamacpp:prompt_seconds_total"),
        "genTps": ("llamacpp:tokens_predicted_total", "llamacpp:tokens_predicted_seconds_total"),
    }
    #: What a Strata server says of its speed (2.35.1). The running request's own rates are read as they are, and the
    #: token/compute-time pairs give the last measured ones, as llama.cpp's do. Its prompt tokens include the ones its cache
    #: already held (`vllm:prefix_cache_hits_total`): only the rest was read, and cost the time, so the pair is made of the rest.
    STRATA_GAUGES = {"strata:live_tok_s": "genTps", "strata:live_prefill_tok_s_mean": "promptTps"}
    STRATA_READ = "strata:prompt_tokens_read"
    STRATA_TIMED = {
        "promptTps": (STRATA_READ, "strata:totals_prompt_seconds_total"),
        "genTps": ("vllm:generation_tokens_total", "strata:totals_decode_seconds_total"),
    }
    #: The samples of a scrape that the Strata reading needs, by their names in the text.
    STRATA_NAMES = frozenset({"vllm:prompt_tokens_total", "vllm:generation_tokens_total", "vllm:prefix_cache_hits_total",
                              "strata:totals_prompt_seconds_total", "strata:totals_decode_seconds_total",
                              *STRATA_GAUGES})

    def __init__(self, clock: Callable[[], float] | None = None):
        # None asks time.time at each reading, so a patched clock is seen.
        self.clock = clock
        self._metrics: dict[Any, tuple[float, dict[str, Any]]] = {}
        self._props: dict[Any, tuple[float, dict[str, Any]]] = {}
        self._counted: dict[Any, tuple[float, dict[str, float]]] = {}
        self._timed: dict[Any, tuple[str, dict[str, float], dict[str, float]]] = {}

    def now(self) -> float:
        return self.clock() if self.clock else time.time()

    def metrics(self, port) -> dict[str, Any]:
        """Scrape the cell's /metrics (Prometheus) for live token rates and
        its queue. Cached ~2s. Returns {promptTps, genTps, requestsProcessing,
        requestsWaiting (vLLM)} — each only when the server says it — or
        {noMetrics: True} when it answers that it keeps none."""
        now = self.now()
        hit = self._metrics.get(port)
        if hit and now - hit[0] < 2:
            return hit[1]
        out: dict[str, Any] = {}
        counted: dict[str, float] = {}
        timed: dict[str, float] = {}
        strata: dict[str, float] = {}
        start = ""
        try:
            # A reader that asks for text/plain is what Prometheus is: llama.cpp and vLLM answer it anyway, Strata only then.
            asked = urllib.request.Request(f"http://127.0.0.1:{int(port)}/metrics", headers={"Accept": "text/plain"})
            with urllib.request.urlopen(asked, timeout=1) as r:
                start = r.headers.get("Process-Start-Time-Unix", "")
                for line in r.read().decode("utf-8", "replace").splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    name = line.split("{", 1)[0].split(None, 1)[0]
                    try:
                        num = float(line.rsplit(None, 1)[-1])
                    except (ValueError, IndexError):
                        continue
                    if not math.isfinite(num) or num < 0:
                        continue
                    if name in self.STRATA_NAMES:
                        strata[name] = strata.get(name, 0.0) + num
                    if name in self.GAUGES:
                        key = self.GAUGES[name]
                        out[key] = out.get(key, 0.0) + num
                    elif name in self.COUNTERS:
                        counted[name] = counted.get(name, 0.0) + num
                    elif any(name in pair for pair in self.TIMED_COUNTERS.values()):
                        timed[name] = timed.get(name, 0.0) + num
        except urllib.error.HTTPError as refused:
            # The server answered and keeps no metrics: llama.cpp started without --metrics says 501, a server without the path
            # 404. Said out loud (2.41), so the board says the speed is not measured — not that no request came, which is what an
            # empty reading drew on Hermes's window (2026-10-26). Any other refusal (503 while a model loads) says nothing.
            out, counted, timed, strata = ({"noMetrics": True} if refused.code in (404, 501) else {}), {}, {}, {}
        except Exception:
            out, counted, timed, strata = {}, {}, {}, {}
        if any(name.startswith("strata:") for name in strata):
            # A Strata server: its own readings of its speed. The growth of vLLM's counters between two scrapes is vLLM's way to say
            # it — here it would be zero between two requests and a spike at the one that finished.
            out.update(self.strata_rates(port, strata))
            counted = {}
        elif timed:
            for key, value in self.timed_rates(port, timed, start, out).items():
                if not out.get(key):
                    out[key] = value
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

    def strata_rates(self, port, sample: dict[str, float]) -> dict[str, float]:
        """The token rates of a Strata server: the running request's own while it reads or generates (`strata:live_*`), else the last
        measured — tokens over compute seconds of the work done since the last scrape, held while idle (`timed_rates`). A sample it does
        not say makes no rate: none is invented."""
        values = {name: sample[name] for name in ("vllm:generation_tokens_total", "strata:totals_prompt_seconds_total",
                                                  "strata:totals_decode_seconds_total") if name in sample}
        if "vllm:prompt_tokens_total" in sample and "vllm:prefix_cache_hits_total" in sample:
            values[self.STRATA_READ] = max(0.0, sample["vllm:prompt_tokens_total"] - sample["vllm:prefix_cache_hits_total"])
        live = {key: sample[name] for name, key in self.STRATA_GAUGES.items() if name in sample}
        return self.timed_rates(port, values, "", live, pairs=self.STRATA_TIMED)

    def timed_rates(self, port, values: dict[str, float], start: str,
                    gauges: dict[str, Any], pairs: dict[str, tuple[str, str]] | None = None) -> dict[str, float]:
        """The latest observed token/compute-time rates, retained while idle.

        The first scrape uses the server's accumulated average. Later scrapes
        use only the new work, which may cover several requests. A process
        identity change or a counter decrease discards the previous rates.
        """
        before = self._timed.get(port)
        previous, rates = (dict(before[1]), dict(before[2])) if before and before[0] == start else ({}, {})
        if any(value < previous.get(name, 0) for name, value in values.items()):
            previous, rates = {}, {}
        for key, (tokens, seconds) in (pairs or self.TIMED_COUNTERS).items():
            if tokens not in values or seconds not in values:
                rates.pop(key, None)
                continue
            count, duration = values[tokens], values[seconds]
            if tokens in previous and seconds in previous:
                count -= previous[tokens]
                duration -= previous[seconds]
            if duration > 0:
                rates[key] = round(count / duration, 2)
            if gauges.get(key, 0) > 0:
                rates[key] = round(gauges[key], 2)
        self._timed[port] = (start, dict(values), rates)
        return rates

    def props(self, port) -> dict[str, Any]:
        """What a llama-server says of itself at /props. Cached ~30s PER PORT
        (same single-slot thrash as Machine.firewall — see its note); {} when
        the server says nothing — another kind of server, or no answer."""
        now = self.now()
        hit = self._props.get(port)
        if hit and now - hit[0] < 30:
            return hit[1]
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/props", timeout=1) as r:
                props = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            props = {}
        props = props if isinstance(props, dict) else {}
        self._props[port] = (now, props)
        return props

    def ctx_max(self, port) -> int:
        """n_ctx the llama-server was launched with, from /props; 0 when it does not say."""
        props = self.props(port)
        gen = props.get("default_generation_settings") or {}
        try:
            return int(gen.get("n_ctx") or props.get("n_ctx") or 0)
        except (TypeError, ValueError):
            return 0

    def slots_total(self, port) -> int | None:
        """How many requests the server takes at once — llama-server's `--parallel`,
        `total_slots` of /props (2.29); None when it does not say: a server of
        another kind (the caravan's own cells, vLLM), or one that does not answer."""
        value = self.props(port).get("total_slots")
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


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
            health_path: str = "/health", launch: dict | None = None, name: str = "",
            ram_mib: float = 0) -> None:
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
                # What its model is called at the machine's entry (2.27,
                # entry.ModelName): kept, as a restart no longer has the request.
                **({"name": str(name)} if name else {}),
                # The memory it promised to keep resident (MemoryLedger): kept, as a restart
                # no longer has the request.
                **({"ramMiB": int(ram_mib)} if ram_mib else {}),
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

    def found(self, port: int, pid: int, llama_bin: Any, log_dir: Path) -> dict[str, Any]:
        """The record of a cell this scout started and has none of: what the
        machine says of its process — its command line, when it started, the
        log it was given by name — and nothing more; the controller sends the
        rest with the next start."""
        cmdline = HostProcesses.cmdline(pid)
        kind = "llama" if llama_bin and str(llama_bin) in cmdline else "command"
        log = Path(log_dir) / (f"llama-server.{int(port)}.log" if kind == "llama" else f"command-cell.{int(port)}.log")
        self.add(port, kind, pid, cmdline[:200], {"port": int(port)}, log if log.exists() else "", False)
        with self.state.lock:
            rec = (self.state.get("cells") or {}).get(str(int(port))) or {}
            started = HostProcesses.started_at(pid)
            if started:
                rec["startedAt"] = started
                self.state.save()
        return rec


class Cells:
    """The cells of this machine by port, and how each looks from outside.

    A cell comes into being the first time anything starts or asks about its
    port, and goes when it is stopped (drop). Its identity is the cancellation
    token of a start: a start that finds its Cell replaced knows it was
    stopped while it worked.
    """

    def __init__(self, machine, config, state):
        self.machine = machine
        self.config = config
        self.by_port: dict[int, Cell] = {}
        self._lock = threading.Lock()
        self.probe = ServerProbe()
        self.records = CellRecords(state)
        self.processes = HostProcesses()
        self.prism = PrismRuntime(config)
        # The model files are the cells': downloads report into a cell's
        # startup record, and a purge keeps what a running cell holds.
        self.models = ModelFetcher(config, self.report, folder=CacheFolder(state))
        # How much the cache keeps, the trim to it and the move of its folder: asks the cells at the moment it deletes.
        self.keeper = CacheKeeper(state, self.models, held=self.held_files, live=self.live_ports, loading=self.loading_ports)

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

    def report(self, port, **fields: Any) -> bool:
        """A start says how it is going: fields merged into the record of the
        cell that is there. True when they were kept.

        A report never makes a cell. A start that was stopped goes on copying
        its model to the end of the file, and each report of that copy used to
        make the stopped port's cell again: "downloading", then "loading", a
        start under way for good with nothing behind it. The board showed the
        last file at 100 % with no way to start, and a new start was refused
        "already in progress" until another stop came (2026-10-19: seamless on
        :22008, a checkpoint of 12 files, stopped on its way). What reports is
        the start that holds the cell (`holds`); the one that does not is
        late, and its words are dropped.
        """
        with self._lock:
            cell = self.by_port.get(int(port))
        if cell is None:
            return False
        with cell.lock:
            cell.startup.update(fields)
        return True

    #: The names of a cell's log in the model cache, by what it runs (starts.py names them so).
    LOG_NAMES = ("llama-server.{port}.log", "command-cell.{port}.log")

    def log_file(self, port: int) -> CellLog:
        """Where the cell on `port` writes: its process's log while this scout holds the process, the log its record names
        after a restart, else a log of its name in the model cache (the cell was started before this scout knew it). A
        CellLog of nothing when none of them is there.

        Asked WITHOUT at(), which would make an empty cell of a port nobody started."""
        with self._lock:
            cell = self.by_port.get(int(port))
        if cell is not None and cell.process.log.path:
            return cell.process.log
        rec = self.records.get(int(port))
        if rec.get("log"):
            return CellLog(Path(rec["log"]))
        for name in self.LOG_NAMES:
            path = self.models.cache_dir() / name.format(port=int(port))
            if path.is_file():
                return CellLog(path)
        return CellLog(None)

    def log(self, port, lines=None, run="") -> dict[str, Any]:
        """What the cell on `port` wrote (2.36, the board's window): the last lines of a run — the newest when `run`
        names none — and the runs there are to read, newest first. {ok: false, error} for a port that is not a number or
        has no log here, and for a name that is not one of its runs."""
        try:
            port = int(str(port).strip())
        except (TypeError, ValueError):
            return {"ok": False, "error": "port must be a number"}
        log = self.log_file(port)
        runs = log.runs()
        if not runs:
            return {"ok": False, "error": f"no log of a cell on port {port} here"}
        name = str(run or "") or runs[0]["name"]
        chosen = log.run(name)
        if chosen is None:
            return {"ok": False, "error": f"{name!r} is not a log of the cell on port {port}"}
        return {"ok": True, "port": port, "run": name, "lines": chosen.lines(lines), "runs": runs}

    def startup(self, port) -> dict[str, Any]:
        """A copy of the cell's startup record."""
        cell = self.at(port)
        with cell.lock:
            return dict(cell.startup)

    def view(self, cell: Cell, table: Callable[[], dict[int, dict[str, Any]] | None] | None = None) -> dict[str, Any]:
        """The process's status merged with its start's phase and progress, so
        the admin sees downloading/loading state before the server is up.

        `table` gives the machine's process table (`Machine.processes()`) for the memory a running cell holds; `views()` hands every cell the
        one it took, and a lone call takes its own."""
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
            pids = self.processes.descendants(st.get("pid"))
            # What its processes have read, how fast, and of how much (2.43): a loading cell's progress. Said for every running
            # cell; the board shows it while the cell loads.
            read = self.processes.bytes_read([st.get("pid"), *pids])
            with cell.lock:
                pace = cell.reading.read(st.get("pid"), read)
            view = {**st, "port": p, "phase": "running", **metrics,
                    # The processes it started (2.31): the card's memory may be
                    # a child's — Strata's engine under its server.
                    "pids": pids,
                    "readBytes": read, "readBytesPerSec": pace, "modelBytes": startup.get("modelBytes"),
                    # The memory its processes hold in RAM (2.35), their RSS in bytes: the card's row says it beside the video memory.
                    # None when ps did not answer — "not told", never a zero.
                    "ramBytes": self.processes.resident(st.get("pid"), (table or self.machine.processes)()),
                    "firewall": self.machine.firewall(p) if p else {},
                    # Which of the files it holds changed on disk after it
                    # started (2.11): ⟳ on the card — restart to pick them up.
                    "launchDiskNewer": cell.process.changed_since(st.get("startedAt"))}
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
                if crash.get("neverReady"):
                    reason = (f"never ready in {crash['count']} starts — not restarting it: {crash.get('reason') or reason}")
                else:
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
                # How fast the copy goes, bytes a second over its last seconds (CopyPace, 2.42); None while not measured.
                "downloadBytesPerSec": startup.get("downloadBytesPerSec"),
                "startedAt": startup.get("startedAt"),
            }
        if phase == "error":
            return {**st, "port": port, "phase": "error",
                    "lastError": startup.get("error") or st.get("lastError", "")}
        return {**st, "port": port}

    def views(self) -> list[dict[str, Any]]:
        asked: list = []

        def table():
            """One `ps` for every cell, and none when no cell runs."""
            if not asked:
                asked.append(self.machine.processes())
            return asked[0]
        return [self.view(cell, table) for _port, cell in self.all()]

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

    #: The phases of a start that has asked the cache for its files and not let go of them.
    STARTING = ("resolving", "downloading", "loading", "running")
    #: The phases of a start whose files are still coming — found, copied, read: the cache's folder does not move under them.
    LOADING = ("resolving", "downloading", "loading")

    def loading_ports(self) -> set[int]:
        """The ports whose start is still bringing its files (LOADING): a cell that runs has its weights loaded, and its open
        files outlive their names."""
        ports: set[int] = set()
        for port, cell in self.all():
            with cell.lock:
                phase = str(cell.startup.get("phase") or "")
            if phase in self.LOADING:
                ports.add(int(port))
        return ports

    def live_ports(self) -> set[int]:
        """The ports whose cell is starting or running: the files its start asked for are in use, though its process may not run
        yet. A cell that failed (error) or was stopped (dropped) is not."""
        ports: set[int] = set()
        for port, cell in self.all():
            with cell.lock:
                phase = str(cell.startup.get("phase") or "")
            if phase in self.STARTING or cell.process.status().get("running"):
                ports.add(int(port))
        return ports

    # ── what outlives a scout restart ───────────────────────────────────────

    def adopt_survivors(self) -> None:
        """Re-attach cells that survived the agent restart, then reap only the
        truly orphaned llama-server processes.

        The registry (state.json "cells") records every started cell with its
        pid + a cmdline marker. On startup, for each record, in this order:
        the pid is alive and carries this scout's mark for the cell's port in
        its environment (CELL_ENV, kept through every exec of a launcher and
        every child) → adopt it; or its command line still contains the marker
        → adopt it into a fresh slot (same pid, same uptime — deploys stop
        killing inference). When neither holds — a failed restart clobbered the
        recorded pid, or the process is not there — fall back to identity by
        PORT: adopt whoever is healthily serving the cell's port. And when
        nothing serves the port yet, adopt the process that carries the mark of
        it: a cell loading its model does not listen for a minute and more, and
        a scout restarted in that minute used to forget it — the cell went on
        loading outside the registry, the board saw none, and its Stop was dead
        (Strata, 2026-10-14). Only a port with no process at all is forgotten.
        Anything matching llamaServerBin that was NOT adopted is a real stray
        and gets reaped as before."""
        adopted_pids = set()
        for key, rec in self.records.items():
            try:
                port = int(rec.get("port") or key)
                rec_pid = int(rec.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            marker = str(rec.get("marker") or "")
            cmdline = self.processes.cmdline(rec_pid) if rec_pid > 1 else ""
            if rec_pid > 1 and (self.processes.port_of(rec_pid) == port
                                or self.processes.marker_matches(marker, cmdline)):
                pid = rec_pid
            else:
                # The mark and the marker are both gone (an exec-chained wrapper
                # like run_whisper.sh → exec python rewrote argv) or the recorded
                # pid was clobbered by a failed restart. Identify the cell by its
                # real contract instead: whoever is healthily serving the cell's
                # PORT right now IS the cell.
                pid = self.processes.listener(port)
                if not pid:
                    pid = self.processes.carrier(port, rec_pid)
                    if not pid:
                        self.records.forget(port)   # nothing runs it — really gone
                        print(f"[llama-node] :{port} is gone: no process serves it or carries its mark — "
                              f"forgetting it")
                        continue
                    print(f"[llama-node] :{port} does not listen yet, but pid {pid} still runs it (it "
                          f"carries this scout's mark for the port) — adopting it while it loads")
                else:
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
            self.take_over(port, pid, rec)
            adopted_pids.add(pid)
        self.reap_strays(keep_pids=adopted_pids)

    def take_over(self, port: int, pid: int, rec: dict[str, Any]) -> None:
        """Hold a process this scout started and had lost track of as the cell
        of its port: the start-up adoption, Reconcile and a start that finds
        its cell already running all end here."""
        cell = self.at(port)
        log = rec.get("log") or ""
        launch = rec.get("launch")
        # A record written before 2.38.2 keeps the promise beside its launch, not in it: the relaunch reads it there.
        if launch and rec.get("ramMiB") and "ramMiB" not in launch:
            launch = {**launch, "ramMiB": int(rec.get("ramMiB") or 0)}
        cell.process.adopt(pid, dict(rec.get("cfg") or {}),
                           log_path=Path(log) if log else None,
                           started_at=int(rec.get("startedAt") or 0),
                           launch=launch)
        cell.cache_models = bool(rec.get("cacheModels"))
        cell.ram_mib = int(rec.get("ramMiB") or 0)
        self.report(port, phase="running", error="")
        print(f"[llama-node] adopted running cell :{port} (pid {pid})")

    def tracking(self, port: Any) -> bool:
        """Whether a cell of this scout runs on `port`, or its start is under
        way there: what it holds, as far as the scout knows."""
        port = int(port)
        with self._lock:
            cell = self.by_port.get(port)
        if cell is None:
            return False
        if cell.process.status().get("running"):
            return True
        with cell.lock:
            return cell.startup.get("phase") in ("resolving", "downloading", "loading")

    def survivor(self, port: Any) -> int:
        """The process a scout started for `port` that no cell of this scout
        holds: found by its mark, listening or still loading. 0 when there is
        none — or when this scout's own cell on the port is held."""
        if self.tracking(port):
            return 0
        return self.processes.carrier(port, self.records.get(port).get("pid") or 0)

    def adopt_found(self, port: Any, pid: int) -> None:
        """Hold the process `survivor` found. A record of the port, if one is
        left, keeps what the start knew (config, launch, log); without one the
        cell is written down from what the machine can say of the process."""
        port = int(port)
        rec = self.records.get(port)
        if not rec:
            rec = self.records.found(port, pid, self.config.get("llamaServerBin"), self.models.cache_dir())
        elif int(rec.get("pid") or 0) != int(pid):
            self.records.repoint(rec, int(pid))
        self.take_over(port, int(pid), rec)

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
        own: set[Path] = set()
        for p in ports:
            cell = self.at(p)
            res = cell.process.stop()
            if res.get("detail") == "not running":
                res = self._reclaim(p, res)
            elif res.get("ok"):
                self._sweep(p)
            results.append(res)
            if not res.get("ok"):
                continue
            self.report(p, phase="idle", error="", downloadedBytes=0, totalBytes=0)
            # A cell that keeps no cache lets go of its own models when a stop stopped it — the files its present start asked
            # for (own_files) — and of nothing else: the rest of the cache is the keeper's. A stop of nothing ("not running", no
            # listener reclaimed) lets go of nothing: the board removes a stopped cell with one more stop, `at` made the cell
            # afresh with caching off, and the purge that followed deleted every cached model no running cell held — 116 GB at
            # one "Remove cell"; a stop of an Ollama cell, whose model lives in the engine, deleted 92 GB more, and 101.5 GB
            # again once its port's past starts counted as its own (2026-10-26).
            if not cell.cache_models and res.get("detail") != "not running":
                own |= self.own_files(cell)
            self.drop(p)   # a stopped cell disappears from the fleet view
            self.records.forget(p)
            # What it made from its model (CARAVAN_CELL_SCRATCH) goes the way
            # its models go: kept with a cache, removed without one.
            if not cell.cache_models:
                try:
                    self.models.clear_scratch(p)
                except OSError:
                    pass
        # Once, after the stopped cells are dropped, keeping what a running cell holds: a model still served by another cell is
        # not evicted (stopping whisper must not delete the translator gguf).
        if own:
            try:
                self.models.release(own, keep=self.held_files())
            except Exception:
                pass
        return results[0] if len(results) == 1 else {"ok": True, "results": results}

    def asks(self, port, files) -> None:
        """A start of `port` asks the cache for `files`: the keeper's log hears of it before the first byte is copied — a trim
        that another start's end triggers must find them wanted, whole or half copied — and the cell keeps the list, what its
        stop lets go of (own_files)."""
        names = [str(f).strip() for f in files if str(f or "").strip()]
        self.at(port).asked = names
        self.keeper.begin(port, names)

    def own_files(self, cell) -> set[Path]:
        """The files `cell`'s present start asked the cache for, where they are on this machine — every part of a split GGUF, the
        projector, the draft, a folder; none for a start that asked for none (a cell in an engine) or a cell no start of this
        scout made. Not the keeper's log by port: a port outlives its cells, and an Ollama cell made on the port of deleted test
        cells let go of their models — 101.5 GB of the library's copies on the controller's machine (2026-10-26)."""
        return self.keeper.files_of({"files": cell.asked}) if cell.asked else set()

    def _reclaim(self, port: int, res: dict[str, Any]) -> dict[str, Any]:
        """"not running" from a process with no handles means it consulted
        NOTHING — the port may still be served by a process this scout lost
        track of (that is precisely how a stop once reported success while
        10.7 GB stayed occupied). Verify the port; kill only what we can
        recognize as ours, never an arbitrary listener.

        Ours is first of all what carries this scout's mark for the port
        (HostProcesses.port_of), listening or still loading, with the tree
        under it; then what the record's command-line marker or the llama
        binary says it is. A process that stood in none of those used to be
        "unrecognized" however plainly it was a scout's cell: Strata's record
        carried a marker no argv could match (2026-10-14)."""
        lpid = self.processes.listener(port)
        marked = self.processes.tagged_cells().get(int(port)) or []
        if not lpid and not marked:
            return res
        targets = list(marked)
        if lpid and lpid not in targets and self.processes.port_of(lpid) != int(port):
            cmd = self.processes.cmdline(lpid)
            mark = str(self.records.get(port).get("marker") or "")
            bin_path = str(self.config.get("llamaServerBin") or "")
            if not ((mark and self.processes.marker_matches(mark, cmd)) or (bin_path and bin_path in cmd)):
                return {"ok": False, "listenerPid": lpid,
                        "error": f"port {port} is held by an unrecognized "
                                 f"process (pid {lpid}) — not killing it"}
            targets.append(lpid)
        elif lpid and lpid not in targets:
            targets.append(lpid)        # carries the mark: a child of the cell that holds the port
        try:
            self._terminate([pid for root in targets for pid in [root, *self.processes.descendants(root)]])
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"reclaim failed: {exc}", "listenerPid": lpid or targets[0]}
        return {"ok": True, "reclaimed": True, "pid": lpid or targets[0]}

    def _sweep(self, port: int) -> None:
        """After a stop: whatever still carries this scout's mark for the port
        goes too. A server that hands the card to a child (Strata's engine
        under its server) leaves the child holding 31 GB when the parent's
        SIGTERM does not reach it; a stop that ends with a cell's memory still
        in use is no stop."""
        left = self.processes.tagged_cells().get(int(port)) or []
        if left:
            print(f"[llama-node] :{port} stopped, but {left} still carry its mark — terminating them")
            try:
                self._terminate([pid for root in left for pid in [root, *self.processes.descendants(root)]])
            except Exception as exc:  # noqa: BLE001
                print(f"[llama-node] :{port} sweep failed: {exc}")

    def _terminate(self, pids: list[int]) -> None:
        """SIGTERM, up to 10 s for them to go, then SIGKILL for the rest."""
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        # Alive is asked of `ps`, as the stop of a cell it has no handle of always did: a
        # signal-0 probe also says "alive" of a process that is a zombie until its parent reaps it.
        deadline = time.time() + 10
        while time.time() < deadline and any(self.processes.cmdline(p) for p in pids):
            time.sleep(0.3)
        for pid in pids:
            if self.processes.cmdline(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

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
