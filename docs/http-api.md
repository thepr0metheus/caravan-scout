# HTTP API (`:8092`)

Everything is JSON; errors come back as `{"error": "..."}` with the `AppError`
status (or 500). Open by default on a trusted LAN; see [Auth](#auth) for the
fleet token. The scout speaks about its machine only — since 2.0 it knows
nothing about the agents or clients on it.

The scout describes itself: `GET /openapi.json` (open, OpenAPI 3.1) is written
from the table of paths the scout answers by (`caravan_scout/routes.py`), and
so are the two endpoint tables below — `python3 scripts/api_reference.py
--write` writes them again, and a test fails when the page and the table
disagree. Types of fields and shapes of answers are not described yet.

## GET

<!-- api-reference:get:start -->
| Path | Access | Purpose |
|---|---|---|
| `/` | open | The scout's page: this machine's address for the controller's board. HTML, read-only since 2.1: the machine, whether a controller has paired it, and the address:port to enter on the controller's board. |
| `/index.html` | open | The scout's page: this machine's address for the controller's board. HTML, read-only since 2.1: the machine, whether a controller has paired it, and the address:port to enter on the controller's board. |
| `/api/pairing` | open | What the page shows: this machine and whether a controller has paired it. Host id, name and IP, the scout's port, platform, GPU names, cells running and total, `controllerUrl`, `tokenRequired` and the heartbeat `{state, lastAt, error}` (`state` is `unpaired` until a controller adds the scout) — never the controller's reply. The controller reads it first when the operator adds the scout. |
| `/api/health` | open | Liveness: service, version, whether a fleet token is required, time. `{ok, service, version, tokenRequired, time}`. |
| `/openapi.json` | open | This description of the API, as OpenAPI 3.1. Written from the table the scout answers by, so it cannot drift from what the scout does; the endpoint tables of docs/http-api.md are written from the same table. `info.version` is the version /api/health names. |
| `/api/state` | fleet token | The machine as a whole: hardware, cells, engines, llama.cpp build, driver. The machine: host identity/IP, GPUs, CPU/RAM, compute apps (`[{gpuUuid, pid, name, usedMiB}]` — `name` is the process's executable, "" when nvidia-smi cannot name it, 2.12+), `engines` — the model engines on this machine that are not its cells (2.12+, below), heartbeat status, llama.cpp build and update status (`llamaUpdate`), the scout's own version (`scoutVersion`), per-cell `llamaNodes` (a running cell's `promptTps`, `genTps` and `requestsProcessing` from its /metrics — a vLLM cell's also `requestsWaiting`, and its rates from its token counters, 2.7+; `listening` — whether a running cell's port listens on this machine yet, and while it does not, `startingTail`, the last lines of its log, 2.7+; `launchDiskNewer` — the roles (`model`, `mmproj`, `draft`) of the files a running cell holds that changed on disk after it started, so a restart would pick them up (a folder by its newest file), 2.11+; a cell that crashed since it was last started by hand carries `crash: {count, at, reason, tail?, gaveUp?}`, 2.5+; `tail` — the last 8 lines of the crashed run's log, keys scrubbed, 2.6+), `autostart` — the ports that start with the machine, stopped ones too (2.4+), `driver` — the NVIDIA driver as the next boot will meet it (2.19+, below), and `llamaSuspect` — `{suspect: false}` or the incident of cells crashing after a fresh llama.cpp build: `crashes15m`, `builtAt`, `currentCommit`, `firstSeenAt`, `lastSeenAt`, `restoreCandidate` (the archived build to offer, or null) (2.6+). The heartbeat pushes the same facts under the same names. |
| `/api/llama-node/status` | fleet token | Every server cell of this machine with its state. `{ok, nodes: [...]}`. |
| `/api/monitor/nvidia-smi` | fleet token | A raw nvidia-smi snapshot for the controller's monitor drawer |
| `/api/host/listeners` | fleet token | TCP ports listening on this machine, and what owns each. `{ok, ports: [{port, proc, pid, addrs}]}` — the controller's port picker. The owning process where the OS says; `addrs` (2.12+): the addresses the port is bound on (`127.0.0.1` only — this machine alone reaches it). `ss` on Linux, `lsof` where there is none (macOS, 2.12+); a tool that fails is `{ok: false, error}`, not an empty machine. |
| `/api/llama-node/configs` | fleet token | Launch configs saved on this machine. Stored in `llama-node-configs/`. |
| `/api/llama-node/update-status` | fleet token | The llama.cpp update or restore job: running, result, last 200 lines. Running or done, the return code, the last 200 lines. |
| `/api/llama-node/builds` | fleet token | Archived llama.cpp builds on this machine, newest first |
| `/api/llama-node/list-cache` | fleet token | What the local model cache holds |
| `/api/vllm` | fleet token | vLLM in this machine's venv: version, history, the install job. (2.9+) In `~/vllm-venv`: `{ok, installed, version, venv, history: [{version, seenAt}], job}` — the version read from its dist-info folder, the versions the venv has had (newest first, five kept: the rollback candidates) and the install job, briefly. |
| `/api/vllm/update-status` | fleet token | The vLLM install job: running, result, last 200 lines. (2.9+) |
| `/api/telemetry?since=` | fleet token | The machine second by second: samples newer than a moment, for the board's charts. (2.8+) Samples newer than `since` (epoch seconds; 0 or missing returns all that are kept) — `{t, gpus: [{index, memUsedMiB, memTotalMiB, utilPct, powerW, tempC}], cpuPct, ram}`. Ten minutes are kept, one sample a second while the machine is watched and one in ten seconds otherwise; each ask marks the machine watched for 30 s. |
<!-- api-reference:get:end -->

### `driver` (2.19+)

The NVIDIA driver as the next boot will meet it — what would have warned
before the reboot of 2026-09-26, when a kernel the automatic updates had
installed came up without its Canonical-signed modules and Secure Boot
refused the unsigned build. Facts only; the controller draws the conclusion.
`null` where the question does not arise: not Linux, or no NVIDIA driver at
all (none loaded, no library installed, no module for the next kernel).
Read at most once a minute.

| Field | What it is |
|---|---|
| `secureBoot` | `true`/`false` from the firmware's `SecureBoot` variable (`/sys/firmware/efi/efivars`, readable without root); `false` on a machine that did not boot through EFI; `mokutil --sb-state` when the variable cannot be read; `null` when nothing can say |
| `kernelRunning` | the running kernel (`uname -r`) |
| `kernelNext` | the newest `vmlinuz-<version>` in `/boot` — what the default boot entry starts; `null` when there is none |
| `loaded` | the version of the module loaded now (`/proc/driver/nvidia/version`), `null` when none is |
| `installed` | the userspace driver's version (its `libnvidia-ml.so.<version>`), `null` when none is installed |
| `nextModule` | the `nvidia` module the next kernel would load: `{path, version, signer}` from `modinfo -k`, `signer` `""` when nobody signed it; `null` when it has none |
| `dkmsKey` | the key DKMS signs its builds with (`/var/lib/shim-signed/mok/MOK.der`): `{signer, enrolled}` — its name as a module's `signer` reads it, and whether the firmware trusts it (`mokutil --test-key`; `null` when it cannot say). A build signed by an unenrolled key is refused under Secure Boot like an unsigned one. `null` when there is no such key |
| `package` | the installed `nvidia-driver-*` package whose version is the library's (`installed`) — several can be installed at once; `null` when none is, or `installed` is unknown |

The heartbeat carries it under the same name.

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
             "expiresAt": "2026-09-22T17:00:00+00:00",          // when it is let go if unused: Ollama's
                                 // keep_alive; LM Studio's lastUsedTime + ttlMs from `lms ps` (2.15)
             "staysLoaded": null, // LM Studio (2.15): true — no idle limit, false — one;
                                 // null when `lms ps` did not say (Ollama says it by a far expiresAt)
             "instances": null,                                 // LM Studio's loaded copies
             "action": {"op": "load", "since": 1790000000},     // an act under way (2.14)
             "actionError": {"op": "unload", "error": "…", "at": 1790000000}}],  // the last one refused
 "controls": ["unload", "delete"], // what the board may do to it (2.14, 2.17): nothing to an
                                 // engine that did not answer or wants a token; LM Studio 0.3
                                 // is read, not driven, and LM Studio deletes nothing
 "holds": true,                  // a load can say how long the model stays unused (2.15):
                                 // Ollama always; LM Studio only where its `lms` is
 "runBy": "user",                // who runs its server (2.16): "user" — this scout's user
                                 // ("stop" is in controls when it knows how to start it
                                 // again), "other" — another user, a system service,
                                 // the operator's to stop; "" — the machine did not say
 "autostart": false,             // started from the board: it starts when the machine boots
 "serverAction": {"op": "start", "since": 1790000000},  // a start or stop under way (2.16)
 "serverError": {"op": "stop", "error": "…", "at": 1790000000},  // the last one that failed
 "firewall": {"state": "blocked", "allowedFrom": []},   // who ufw lets reach its port
                                 // (2.13; as a cell's port); null on 127.0.0.1 only
 "pids": [5100, 5151],           // its processes and their children: the cards'
                                 // memory is named by these
 "ramBytes": 1283457024}         // their RSS; null when ps did not answer
```

A model the engine does not describe keeps `null`, never a zero. `models` is
`null` for an engine that did not list them (`auth`, `unreachable`).

## POST

<!-- api-reference:post:start -->
| Path | Access | Purpose |
|---|---|---|
| `/api/controller-url` | fleet token, in the header or the body | Pair this machine with a controller: its address and the fleet token. The controller calls it when the operator adds the scout on its board: `{url, token?}` → validates (`http[s]://`, scheme optional in the form), writes `controllerUrl` into `config.json` (atomic, preserves the rest of the file), updates the running scout and fires one heartbeat right away. Returns `{ok, controllerUrl, heartbeat}`; `heartbeat.state` is `error` if the controller did not answer (the URL is still saved). The fleet token may come in the body (`token`) instead of the header, since the pairing form has no header. A token regenerated on the controller passes without the one the scout holds when the address is the same controller's and that controller accepts a heartbeat carrying it; a different address still needs the token the scout holds. |
| `/api/heartbeat` | fleet token | Send one heartbeat to the controller now. The controller calls it when the Topology page opens. |
| `/api/unpair` | fleet token | Let go of the controller: its address and the fleet token leave config.json. The ✕ on the machine's node on the board: `controllerUrl` and `controllerToken` leave `config.json`, the heartbeat stops (`state: unpaired`); the cells keep running. Token-gated like everything else, so only the scout's own controller can let go. |
| `/api/llama-node/start` | fleet token | Start a server cell: llama.cpp, vLLM or a command cell. A llama cell needs the controller-built `args` (with `{{MODEL_PATH}}`-style placeholders), `port`, model file references to download and `cacheModels`. A command cell (`cellKind=command`) needs `shellLine` — the complete `bash -lc` sentence — plus `healthPath`, which is stored with the cell so re-adoption after a scout restart probes the right endpoint (a vLLM cell answers on `/v1/models`, not `/health`). Missing `args` or `shellLine` is refused with a version hint: this scout never assembles its own. `inPlace` (optional, 2.3+) maps each model path the cell is sent to where the controller reads that file — `{path, size}` for a file, `{path, dir: true}` for a folder, `library` when it is a library's: a file this machine has at that path with that size is read there (no copy, never deleted); a library's file or a folder it lacks is refused (409) naming what is missing, since a download cannot bring it. `vram` (optional, 2.7+) — `{device, reserveMiB, who, why, lower}`, what the cell reserves on a card the moment it starts (the controller's rule: vLLM takes utilization × the card): a command cell whose card has less free right now is refused before it runs, naming the running cells that hold it. `env` (optional, 2.8.1+) — `{NAME: value}` a llama cell's server starts with, over the scout's own environment: a CPU-only cell (`N_GPU_LAYERS` 0) gets `CUDA_VISIBLE_DEVICES=""`, the environment the controller's start.sh exports; it is written into the cell's start.sh and kept for a restart after a crash. A name that is not a shell variable name, or a value that is not a string, is refused (400) before anything starts. Since 2.20 a command cell takes `env` too (it was dropped), and `env` may not name `CARAVAN_SCOUT_CELL` (400): the scout sets that marker itself, last. Async: returns `{status: "starting", phase: "resolving"}` immediately; progress is visible in `/api/llama-node/status` and the fast heartbeats. |
| `/api/llama-node/autostart` | fleet token | Set whether a cell starts with the machine. (2.4+) `{port, enabled, payload}`. On keeps `payload` — the very request `/api/llama-node/start` takes — and the scout starts the cell when its machine boots: on the first scout start of a boot only (the machine's boot id), so a scout update never brings back a cell the operator stopped. A start of an autostart cell refreshes the kept request. Off drops it. Answers `{ok, port, autostart: [ports]}`; 400 for a bad port or an on without a request. |
| `/api/host/reboot` | fleet token | Reboot this machine. `sudo -n systemctl reboot` or `poweroff`; needs passwordless sudo for it and says so when it is missing. Two paths, not a flag: poweroff cannot be undone from the board, and an old scout answers 404 to a path it does not have instead of doing the wrong one of the two. |
| `/api/host/poweroff` | fleet token | Power this machine off — nothing on the board can switch it back on. `sudo -n systemctl reboot` or `poweroff`; needs passwordless sudo for it and says so when it is missing. Two paths, not a flag: poweroff cannot be undone from the board, and an old scout answers 404 to a path it does not have instead of doing the wrong one of the two. |
| `/api/llama-node/stop` | fleet token | Stop one cell by its port, or every cell. `{port}`, or no port for ALL cells. Drops the cell from the fleet view and the registry; purges cached models unless the cell had `cacheModels` (safe purge — never evicts a model a sibling cell still serves). A port still held by a process the scout lost track of is reclaimed only when it is recognisably ours (its marker or the llama-server binary); an unrecognised holder is named and left alone. |
| `/api/llama-node/update` | fleet token | Start the llama.cpp update job (empty tag: the latest release). `{tag?}` — empty is the latest release; a commit works too. With `restoreId` in the body the same job restores an archived build instead of building. 409 while one runs. |
| `/api/vllm/update` | fleet token | Install another vLLM version into the machine's venv. (2.9+) Into `~/vllm-venv`: `{version?}` — empty is the latest release (`pip install --upgrade vllm`), a version pins it (`vllm==X`; a rollback is an older pin). The current version goes into the history first. Its own background job, apart from the llama.cpp build's: 409 while one runs; 400 for a version that is not one, or when the venv does not exist yet (the first vLLM cell start provisions it). Running cells keep their vLLM until restarted. |
| `/api/llama-node/restore` | fleet token | Restore an archived llama.cpp build. `{id}` — `restoreId` is read as an alias, and `id` wins when both are given. 400 "build id is required" without one: the update job would read an empty id as no restore and build the latest release instead. The same job as the update, so 409 while one runs. |
| `/api/llama-node/suspect-dismiss` | fleet token | Hide the "fresh build, crashing cells" banner for the current build. (2.6+) A new build can raise it again. |
| `/api/llama-node/purge-cache` | fleet token | Clear the model cache, never evicting a model a running cell serves |
| `/api/llama-node/configs/delete` | fleet token | Delete a saved launch config by its filename |
| `/api/engines/unload` | fleet token | Unload a model from Ollama or LM Studio. (2.14+) `{kind, port, model}` — only an engine the last scan found and a model it listed and holds loaded (else 400/404/409 with the reason), one act per model at a time. Answered at once with `{ok, engines}`, the model carrying `action: {op, since}`; the act runs on a thread of its own, then the engines are scanned again. Ollama: `/api/generate` with `keep_alive: 0`. LM Studio 0.4+: `/api/v1/models/unload` for every loaded instance. A refusal stays on the model as `actionError: {op, error, at}` — the engine's own words — until the next act on it. There is no load (2.18): a cell in the engine loads its model when it starts. |
| `/api/engines/delete` | fleet token | Delete a model from Ollama's disk. (2.17) `{kind, port, model}` — Ollama only (`DELETE /api/delete`), on a thread of its own with `action: {op: "delete"}` on the model while it runs; a loaded model is refused (409: unload it first). LM Studio has no verb for it and does not say which files are a model's, so it does not offer it. Nothing is downloaded into an engine (2.22). |
| `/api/engines/start` | fleet token | Start an engine's server (Ollama, LM Studio). (2.16) `{kind, port}` — a known server that is not running: a view of its own, `state: "stopped"`, `models: null`. Only what its view's `controls` offer. Answered at once with `{ok, engines}`, the engine carrying `serverAction`; the start waits up to 60 s for the server to answer, the stop up to 20 s for it to go quiet, then the engines are scanned again; what failed stays as `serverError`. Ollama: `ollama serve` with the binary and the `OLLAMA_*` / device variables of the run it was learned from, in a session of its own (a scout restart leaves it running), output in `engine-logs/ollama.log` next to the state; stopped with SIGTERM, and SIGKILL for it and its children after 10 s. LM Studio: `lms daemon up` + `lms server start -p <port> --bind <address>`; stopped with `lms daemon down` (`lms server stop` where no daemon runs). A server started here starts again when the machine boots (once per boot), until it is stopped here. |
| `/api/engines/stop` | fleet token | Stop an engine's server (Ollama, LM Studio). (2.16) `{kind, port}` — a server this scout's user runs, when the scout knows how to start it again. Only what its view's `controls` offer. Answered at once with `{ok, engines}`, the engine carrying `serverAction`; the start waits up to 60 s for the server to answer, the stop up to 20 s for it to go quiet, then the engines are scanned again; what failed stays as `serverError`. Ollama: `ollama serve` with the binary and the `OLLAMA_*` / device variables of the run it was learned from, in a session of its own (a scout restart leaves it running), output in `engine-logs/ollama.log` next to the state; stopped with SIGTERM, and SIGKILL for it and its children after 10 s. LM Studio: `lms daemon up` + `lms server start -p <port> --bind <address>`; stopped with `lms daemon down` (`lms server stop` where no daemon runs). A server started here starts again when the machine boots (once per boot), until it is stopped here. |
<!-- api-reference:post:end -->

## Auth

Open by default. When `controllerToken` is set in `config.json` (manually or
via the pairing form's token field), every endpoint except `GET /`,
`/index.html`, `/api/pairing`, `/api/health` and `/openapi.json` requires the same value in `X-Caravan-Token`
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
