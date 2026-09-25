# Architecture

> Historical note: this project was published internally as
> `llm-easy-route-agent` before the rename to **caravan-scout**.

`caravan-scout` is the hardware sidecar of the
[LAMA CARAVAN](../README.md#role) control plane: one small stdlib-only Python
service per machine that lends its GPU or CPU. The controller (lama-caravan, `:7990`) owns the
topology and builds llama-server commands; the scout executes them locally and
reports back. It reports its machine only: the agents and clients that may
live on the same box are the controller's records, made by hand (2.0).

```text
scout host                                    controller host
┌────────────────────────────────┐            ┌────────────────────────────┐
│ caravan-scout  :8092           │            │ lama-caravan admin :7990   │
│  • heartbeat thread ───────────┼──POST────► │  /api/topology/client-     │
│  • HTTP surface  ◄─────────────┼──commands──│    heartbeat               │
│  • a Cell per port:            │            │  (start/stop/configs/…)    │
│      llama-server / command    │            └────────────────────────────┘
│      cell child processes      │
│  • model cache (~/llama-model- │
│      cache), downloaded from   │
│      the controller            │
└────────────────────────────────┘
```

## Flows

**Heartbeat (up).** A daemon thread POSTs `Report.heartbeat()` to
`<controllerUrl>/api/topology/client-heartbeat` every `heartbeatIntervalSeconds`
(60 s), dropping to every 5 s while any cell is resolving/downloading/loading
so the board shows live progress. The payload carries host identity, GPU/CPU
inventory, compute apps, the engines next to the cells (Ollama, LM Studio —
the last scan of their own thread), the llama.cpp build and its update job, the scout's
version (`scoutVersion`), and per-cell `llamaNodes` — the same fields, under
the same names, as `/api/state`.

**Pairing (down).** The controller pairs the scout, from its board: the
operator enters the machine's address, the controller reads the scout's open
`/api/pairing`, then posts its own address and fleet token to
`/api/controller-url`; the scout saves them and beats once. `/api/unpair`
lets go. A freshly installed scout waits, unpaired, and records that it waits
instead of an error every minute.

**Commands (down).** The controller calls the scout's HTTP surface (see
[http-api.md](http-api.md)): llama-node start/stop/purge-cache/configs,
llama.cpp update and restore, `nvidia-smi` snapshots, listening ports, and
power.

**Variant-2 command contract.** The controller builds the FULL llama-server
argument list and sends it in `payload["args"]` with placeholders that the
scout substitutes after downloading the files:

```text
{{MODEL_PATH}}   {{MMPROJ_PATH}}   {{SPEC_PATH}}
```

These constants must stay in sync with the controller's
`LLAMA_PATH_PLACEHOLDER_*`. There is no local fallback builder: a scout that
receives no `args` refuses the start rather than assembling its own list. It
used to carry one — a mirror of the controller's, 23 flags behind — and a cell
started through it ran without half the configuration the board displayed.

Generic command cells (`CELL_KIND=command`) are the same story one level up:
the controller sends `shellLine`, the whole `bash -lc` sentence including
`set -euo pipefail`, the `PORT`/`ENV` exports and the `WORKDIR` change. The
scout executes it verbatim. Assembling it here is what let the controller's
script and the scout's line drift apart.

**Cells.** A host can run several servers at once (e.g. a translator + a
whisper cell). Each port owns a `Cell`: its `CellProcess`, its startup
progress (`resolving → downloading → loading → running`) and a cache flag.
A llama cell's start answers at once and runs the slow half — download,
artifacts, the process, the registry — as a background `LlamaLaunch`; a
command cell starts on the request. Stopping a cell drops it from the fleet
view and the registry and purges uncached models — via the safe purge that
never evicts a model still served by a sibling cell, and only files the scout
downloaded itself (`DownloadedFiles`). What was started is kept in
`state.json` (`CellRecords`); after a scout restart the survivors are adopted
again, by their command line or by the port they serve, and a llama-server a
scout started that nobody claims is reaped. A process the scout did not start
(no `CARAVAN_SCOUT_CELL` in its environment, `HostProcesses.owned`) is never
adopted or killed — on a machine shared with the controller, those are the
controller's cells.

## Module layout

`python3 -m caravan_scout.app` is the stable entry point (baked into the
systemd/launchd units); the code lives in the package:

| Module | Owns |
|---|---|
| `paths.py` | Env-driven constants, the `{{…}}` placeholder contract, `DEFAULT_CONFIG` |
| `errors.py` | `AppError` (HTTP-visible failures) |
| `machine.py` | `Machine` — the host as its OS tells it: NVIDIA GPUs and the processes on them (with their names, 2.12), CPU/RAM, who ufw lets in, listening ports and where they are bound (`ss`, or `lsof` on macOS), the process table (`ps`), raw `nvidia-smi`, the address facing the controller; the caches that keep polling cheap |
| `engines.py` | `ForeignEngines` — the model engines on this machine that are not its cells, rescanned every 10 s on a thread of their own; `EngineKind` → `Ollama`, `LmStudio` — the kinds as a table: default port, process names, how the API is read; `EngineAsk` — a GET on an engine's port (the scan's only verb: looking never changes an engine); `EngineCall` — a POST, used only by `ForeignEngines.act`: a model loaded or unloaded because the board asked (2.14), on a thread of its own, with what is under way and what failed last shown on the model; a load that would not fit into the cards' free memory is a question back to the board (2.15: `EngineKind.estimate`, `ForeignEngines.short_of_memory`); `LmsCli` — LM Studio's own command line (`~/.lmstudio/bin/lms`), for what its REST API does not say or do: its memory estimate, a load with an idle limit (`--ttl`), the limits of what is loaded (`lms ps --json`) — 2.15; most of `lms` wakes LM Studio up when it is not running, so a read, an estimate or a load asks `lms server status` (which does not) first, and every sequence of commands holds the kind's lock — a read cannot meet a stop half-way (2.16.1) |
| `process.py` | `CellProcess` — one cell's process: start, adopt, stop, status; `CellLog` — the log kept across runs and read for a crash reason; `HostProcesses` — a process found again by its command line or by the port it serves; a cell's launch files that changed on disk since it started (`changed_since`, 2.11); `MemoryScope` — the memory limits a cell is launched with (the values the controller's cell unit had, in a systemd user scope; the scout is their one home since the controller's step 6.9) |
| `report.py` | `Report` — what the scout says about its machine: `public()` for /api/state, `heartbeat()` for the beat (same facts, same names), `pairing()` for the scout's page and the controller's first look |
| `heartbeat.py` | `Heartbeat` — one beat to the controller, the loop of beats, and pairing; each outcome written to state.json |
| `models.py` | `ModelFetcher` — the model cache: download from the controller with retries, verify, clean up, purge; reports progress through a callback |
| `cells.py` | `Cell` (one port); `Cells` — the table by port, startup records, the views the controller reads, re-adoption after a restart, stray reaping, stop, the safe purge, and the model cache the cells own; `CellRecords` (state.json `cells`); `ServerProbe` (a cell server's /metrics and /props: llama.cpp's rates as they are, vLLM's from its token counters) |
| `autostart.py` | `Autostart` — the cells that start when the machine boots: the kept start requests, refreshed on each start, started on the first scout start of a boot |
| `watchdog.py` | `Watchdog` — a crashed cell launched again the same way after 10 s, at most 3 times in 10 minutes, and the crash note the board shows |
| `telemetry.py` | `Telemetry` — the machine's cards and processor, a sample a second while a board watches (ten seconds otherwise), ten minutes kept, for the board's charts |
| `suspect.py` | `CrashSuspect` — cells crashing soon after a fresh llama.cpp build: the incident kept per build, dismissed per build, and the archived build to offer |
| `builds.py` | `LlamaBuilds` — llama.cpp on this machine: the binary's version and date, the update/restore job, the archive of earlier builds |
| `job.py` | `BackgroundJob` — one long job at a time (a llama.cpp build, a vLLM install): a thread, its output in a ring buffer, the whole and the slim status, the refusal while one runs |
| `vllm.py` | `VllmVenv` — vLLM in `~/vllm-venv`: the installed version, the versions it had (the rollback candidates), the pip job that installs another |
| `saved_configs.py` | `SavedConfigs` — launch parameters saved by hand as `llama-node.bak.<stamp>.json` |
| `cell_assets.py` | `CellAssets` — before a command cell starts, the files its launcher runs are brought up to the controller's copies (by sha256); never blocks a start |
| `starts.py` | `CellStart` → `LlamaStart` (checked on the request) + `LlamaLaunch` (download, artifacts, start, register — in the background) and `CommandStart` (all on the request); `CellArtifacts` — start.sh and cell.json under `var/server-cells/<port>/` |
| `config.py` | `ScoutConfig` — config.json merged over the defaults (and what the file itself says, `from_file`); the token; `pair()`, its one writer |
| `identity.py` | `HostIdentity` — the id this machine goes by on the board, pinned in state.json at the first run so a rename keeps it (config.json's `hostId` wins and becomes the pin); the name the board shows, the live hostname unless config.json names one |
| `state.py` | `ScoutState` — state.json as a dict with one lock and an atomic save; drops 1.x agent keys once |
| `scout.py` | `Scout` — the machine's scout, put together: the config, the state, the identity (settled before anything reads hostId), the machine, the cells (and their model cache), the llama.cpp builds, the saved configs, the report and the heartbeat |
| `http.py` | `Api` — the `:8092` surface as tables, one line per path, behind the fleet-token gate; `ScoutHandler` — one request answered from them; `Power` — reboot and poweroff, each on its own path |
| `webui.py` | `PairingPage` — the read-only page on `GET /`: the machine, its pairing, the address to enter on the controller; reads the open `/api/pairing` |
| `engine_servers.py` | `EngineServers` — the engines' servers the board turns on and off (2.16): how each is started again (learned from the run of this scout's user, or from where it installs itself in the home — `state.json` `engineServers`), who runs each (`runBy`), the stopped ones as engines of their own, and the start once per boot; `EngineProcs` — the machine's side: a process as /proc says it, a server started in a session of its own (its own environment mark, never a cell's), SIGTERM then SIGKILL with its children |
| `app.py` | The process entry point: arguments, the scout, re-adoption, the heartbeat, watchdog, telemetry and engines threads, the server |

The shape is guarded: `scripts/check_oop.py` (in CI, with a self-test that
plants each breakage) refuses a `*Mixin` class, a module of functions not on
its short list of exceptions — `__init__.py`, `paths.py`, `app.py`, each with
its reason — and a subclass that leaves its parent's `NotImplementedError`
method unimplemented.

Layering is strict: `paths`/`errors` ← `job` ← `config`/`state`/`process`/`builds`/`vllm`/`saved_configs` ← `machine`/`starts` ← `cells` ← `report` ← `heartbeat` ← `scout` ←
`http` ← `app`. State lives in `state.json` next to the config; per-node
launch artifacts under `var/server-cells/<port>/`.
