# HTTP API (`:8092`)

Everything is JSON; errors come back as `{"error": "..."}` with the `AppError`
status (or 500). Open by default on a trusted LAN; see [Auth](#auth) for the
fleet token. The scout speaks about its machine only — since 2.0 it knows
nothing about the agents or clients on it.

## GET

| Path | Purpose |
|---|---|
| `/` | The scout's page (HTML, read-only since 2.1): the machine, whether a controller has paired it, and the address:port to enter on the controller's board. |
| `/api/pairing` | What the page shows, open even with a token: host id/name/IP, the scout's port, platform, GPU names, cells running/total, `controllerUrl`, `tokenRequired`, heartbeat `{state, lastAt, error}` (`state` is `unpaired` until a controller adds it) — never the controller's reply. The controller reads it first when the operator adds the scout. |
| `/api/health` | Liveness, open even with a token: `{ok, service, version, tokenRequired, time}`. |
| `/api/state` | The machine: host identity/IP, GPUs, CPU/RAM, compute apps (`[{gpuUuid, pid, name, usedMiB}]` — `name` is the process's executable, "" when nvidia-smi cannot name it, 2.12+), `engines` — the model engines on this machine that are not its cells (2.12+, below), heartbeat status, llama.cpp build and update status (`llamaUpdate`), the scout's own version (`scoutVersion`), per-cell `llamaNodes` (a running cell's `promptTps`, `genTps` and `requestsProcessing` from its /metrics — a vLLM cell's also `requestsWaiting`, and its rates from its token counters, 2.7+; `listening` — whether a running cell's port listens on this machine yet, and while it does not, `startingTail`, the last lines of its log, 2.7+; `launchDiskNewer` — the roles (`model`, `mmproj`, `draft`) of the files a running cell holds that changed on disk after it started, so a restart would pick them up (a folder by its newest file), 2.11+; a cell that crashed since it was last started by hand carries `crash: {count, at, reason, tail?, gaveUp?}`, 2.5+; `tail` — the last 8 lines of the crashed run's log, keys scrubbed, 2.6+), `autostart` — the ports that start with the machine, stopped ones too (2.4+), and `llamaSuspect` — `{suspect: false}` or the incident of cells crashing after a fresh llama.cpp build: `crashes15m`, `builtAt`, `currentCommit`, `firstSeenAt`, `lastSeenAt`, `restoreCandidate` (the archived build to offer, or null) (2.6+). The heartbeat pushes the same facts under the same names. |
| `/api/llama-node/status` | Just the cells: `{ok, nodes: [...]}`. |
| `/api/monitor/nvidia-smi` | A raw `nvidia-smi` snapshot for the controller's monitor drawer. |
| `/api/telemetry?since=<epoch>` | The machine second by second (2.8+): samples newer than `since` — `{t, gpus: [{index, memUsedMiB, memTotalMiB, utilPct, powerW, tempC}], cpuPct, ram}` — ten minutes kept, one a second while watched and one in ten seconds otherwise; each ask marks the machine watched for 30 s. `since` 0 or missing returns the whole ten minutes. |
| `/api/host/listeners` | TCP ports listening on this machine, with the owning process where the OS says: `{ok, ports: [{port, proc, pid, addrs}]}` — the controller's port picker. `addrs` (2.12+): the addresses the port is bound on (`127.0.0.1` only — this machine alone reaches it). `ss` on Linux, `lsof` where there is none (macOS, 2.12+); a tool that fails is `{ok: false, error}`, not an empty machine. |
| `/api/llama-node/configs` | Saved launch configs stored on this client (`llama-node-configs/`). |
| `/api/llama-node/list-cache` | Contents of the local model cache. |
| `/api/llama-node/update-status` | The llama.cpp update job: running/done, return code, the last 200 lines. |
| `/api/llama-node/builds` | Archived llama.cpp builds on this host, newest first. |
| `/api/vllm` | vLLM in this machine's `~/vllm-venv` (2.9+): `{ok, installed, version, venv, history: [{version, seenAt}], job}` — the version read from its dist-info folder, the versions the venv has had (newest first, five kept: the rollback candidates) and the install job, briefly. |
| `/api/vllm/update-status` | The vLLM install job: running/done, return code, the last 200 lines (2.9+). |

### `engines` (2.12+)

Ollama and LM Studio next to the cells, found where each listens by default
(11434, 1234) or where a process of its name listens — never on a cell's
port — and read through their own APIs, GET only: the scout changes nothing
in them. Rescanned every 10 s by a thread of its own, so a hung engine never
holds a report. `null` before the first scan, `[]` when none was found.

```json
{"kind": "ollama", "label": "Ollama", "port": 11434,
 "listen": "network",            // "loopback": this machine only; "": the OS did not say
 "state": "ok",                  // "auth": wants a token; "unreachable": its port is silent
 "version": "0.12.3",            // "" when the engine does not say (LM Studio)
 "api": "v1",                    // LM Studio only: its native API, or 0.3's "v0"
 "installedKnown": false,        // only when the installed list did not answer
 "models": [{"name": "qwen3:8b", "type": "", "format": "gguf", "family": "qwen3",
             "params": "8.2B", "quant": "Q4_K_M", "fileBytes": 5225388164,
             "remote": false,    // an Ollama cloud model: it does not run here
             "loaded": true,     // null when the engine did not say
             "memBytes": 6591830464, "vramBytes": 5333539264,   // Ollama only
             "contextLength": 4096, "maxContextLength": null,
             "expiresAt": "2026-09-22T17:00:00+00:00",          // Ollama's keep_alive
             "instances": null}],                               // LM Studio's loaded copies
 "pids": [5100, 5151],           // its processes and their children: the cards'
                                 // memory is named by these
 "ramBytes": 1283457024}         // their RSS; null when ps did not answer
```

A model the engine does not describe keeps `null`, never a zero. `models` is
`null` for an engine that did not list them (`auth`, `unreachable`).

## POST

| Path | Purpose |
|---|---|
| `/api/heartbeat` | Trigger one immediate heartbeat POST to the controller (the controller calls this when the Topology page opens). |
| `/api/unpair` | The controller lets go of this machine (the ✕ on its node): `controllerUrl` and `controllerToken` leave `config.json`, the heartbeat stops (`state: unpaired`); the cells keep running. Token-gated like everything else — only the scout's controller can let go. |
| `/api/controller-url` | Pair this host with a controller — the controller calls it when the operator adds the scout on its board: `{url, token?}` → validates (`http[s]://`, scheme optional in the form), writes `controllerUrl` into `config.json` (atomic, preserves the rest of the file), updates the running scout and fires one heartbeat right away. Returns `{ok, controllerUrl, heartbeat}` — `heartbeat.state` is `error` if the controller didn't answer (the URL is still saved). |
| `/api/llama-node/start` | Start a server cell on this host. A llama cell needs the controller-built `args` (with `{{MODEL_PATH}}`-style placeholders), `port`, model file references to download and `cacheModels`. A command cell (`cellKind=command`) needs `shellLine` — the complete `bash -lc` sentence — plus `healthPath`, which is stored with the cell so re-adoption after a scout restart probes the right endpoint (a vLLM cell answers on `/v1/models`, not `/health`). Missing `args` or `shellLine` is refused with a version hint: this scout never assembles its own. `inPlace` (optional, 2.3+) maps each model path the cell is sent to where the controller reads that file — `{path, size}` for a file, `{path, dir: true}` for a folder, `library` when it is a library's: a file this machine has at that path with that size is read there (no copy, never deleted); a library's file or a folder it lacks is refused (409) naming what is missing, since a download cannot bring it. `vram` (optional, 2.7+) — `{device, reserveMiB, who, why, lower}`, what the cell reserves on a card the moment it starts (the controller's rule: vLLM takes utilization × the card): a command cell whose card has less free right now is refused before it runs, naming the running cells that hold it. `env` (optional, 2.8.1+) — `{NAME: value}` a llama cell's server starts with, over the scout's own environment: a CPU-only cell (`N_GPU_LAYERS` 0) gets `CUDA_VISIBLE_DEVICES=""`, the environment the controller's start.sh exports; it is written into the cell's start.sh and kept for a restart after a crash. A name that is not a shell variable name, or a value that is not a string, is refused (400) before anything starts. Async: returns `{status: "starting", phase: "resolving"}` immediately; progress is visible in `/api/llama-node/status` and the fast heartbeats. |
| `/api/llama-node/stop` | Stop one cell (`{port}`) or ALL cells (no port). Drops the cell from the fleet view and the registry; purges cached models unless the cell had `cacheModels` (safe purge — never evicts a model a sibling cell still serves). A port still held by a process the scout lost track of is reclaimed only when it is recognisably ours (its marker or the llama-server binary); an unrecognised holder is named and left alone. |
| `/api/llama-node/autostart` | Autostart of one cell (2.4+): `{port, enabled, payload}`. On keeps `payload` — the very request `/api/llama-node/start` takes — and the scout starts the cell when its machine boots: on the first scout start of a boot only (the machine's boot id), so a scout update never brings back a cell the operator stopped. A start of an autostart cell refreshes the kept request. Off drops it. Answers `{ok, port, autostart: [ports]}`; 400 for a bad port or an on without a request. |
| `/api/llama-node/update` | Start a llama.cpp update job: `{tag?}` — empty is the latest release; a commit works too. 409 while one runs. |
| `/api/llama-node/restore` | Restore an archived build: `{id}`. |
| `/api/vllm/update` | Install another vLLM into `~/vllm-venv` (2.9+): `{version?}` — empty is the latest release (`pip install --upgrade vllm`), a version pins it (`vllm==X`; a rollback is an older pin). The current version goes into the history first. Its own background job, apart from the llama.cpp build's: 409 while one runs; 400 for a version that is not one, or when the venv does not exist yet (the first vLLM cell start provisions it). Running cells keep their vLLM until restarted. |
| `/api/llama-node/suspect-dismiss` | Hide the "fresh build, crashing cells" banner for the current build (2.6+); a new build can raise it again. |
| `/api/llama-node/purge-cache` | Manually clear the model cache (safe variant). |
| `/api/llama-node/configs/delete` | Delete a saved launch config by `filename`. |
| `/api/host/reboot` · `/api/host/poweroff` | Power-cycle or power off this machine (`sudo -n systemctl …`; needs passwordless sudo for it and says so when it is missing). Two paths, not a flag: poweroff cannot be undone from the board. |

## Auth

Open by default. When `controllerToken` is set in `config.json` (manually or
via the pairing form's token field), every endpoint except `GET /`,
`/index.html`, `/api/pairing` and `/api/health` requires the same value in `X-Caravan-Token`
— the controller sends it automatically once its sign-in is enabled, and the
scout adds it to heartbeats and model downloads. See the controller's
`docs/security.md` for the whole story.

One exception, for a token regenerated on the controller: `/api/controller-url`
with the SAME controller address and a token the scout does not hold passes
once that controller accepts a heartbeat carrying the new token; the scout
then keeps it. A different address still needs the token the scout holds.

## Contracts with the controller

- The heartbeat goes UP to `<controllerUrl>/api/topology/client-heartbeat`;
  everything else is the controller calling DOWN into this surface.
- The report's shape is one file for both sides: `docs/report-sample.json`
  (the heartbeat and `/api/state` of an imagined machine, built by
  `scripts/report_sample.py` from the real report code). The scout's tests
  check it still produces exactly that; the controller keeps a byte-identical
  copy and checks it reads every field. Change a field here — regenerate with
  `--write` and copy it over, or both sides go red.
- `{{MODEL_PATH}}` / `{{MMPROJ_PATH}}` / `{{SPEC_PATH}}` in `args` must match
  the controller's `LLAMA_PATH_PLACEHOLDER_*` constants.
- Model files are downloaded from the controller's
  `GET /api/models/download?path=…` (resumable, retry/backoff, atomic rename,
  size verification — a truncated download is deleted, never served).
