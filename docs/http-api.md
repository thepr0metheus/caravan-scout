# HTTP API (`:8092`)

Everything is JSON; errors come back as `{"error": "..."}` with the `AppError`
status (or 500). Open by default on a trusted LAN; see [Auth](#auth) for the
fleet token. The scout speaks about its machine only — since 2.0 it knows
nothing about the agents or clients on it.

## GET

| Path | Purpose |
|---|---|
| `/` | Built-in pairing page (HTML): host summary + a form that sets `controllerUrl`. |
| `/api/pairing` | What the pairing page shows, open even with a token: host id/name/IP, platform, GPU names, cells running/total, `controllerUrl`, `tokenRequired`, heartbeat `{state, lastAt, error}` — never the controller's reply. |
| `/api/health` | Liveness, open even with a token: `{ok, service, version, tokenRequired, time}`. |
| `/api/state` | The machine: host identity/IP, GPUs, CPU/RAM, compute apps, heartbeat status, llama.cpp build and update status (`llamaUpdate`), the scout's own version (`scoutVersion`), per-cell `llamaNodes`. The heartbeat pushes the same facts under the same names. |
| `/api/llama-node/status` | Just the cells: `{ok, nodes: [...]}`. |
| `/api/monitor/nvidia-smi` | A raw `nvidia-smi` snapshot for the controller's monitor drawer. |
| `/api/host/listeners` | TCP ports listening on this machine, with the owning process where the OS says: `{ok, ports: [{port, proc, pid}]}` — the controller's port picker. |
| `/api/llama-node/configs` | Saved launch configs stored on this client (`llama-node-configs/`). |
| `/api/llama-node/list-cache` | Contents of the local model cache. |
| `/api/llama-node/update-status` | The llama.cpp update job: running/done, return code, the last 200 lines. |
| `/api/llama-node/builds` | Archived llama.cpp builds on this host, newest first. |

## POST

| Path | Purpose |
|---|---|
| `/api/heartbeat` | Trigger one immediate heartbeat POST to the controller (the controller calls this when the Topology page opens). |
| `/api/controller-url` | Pair this host with a controller: `{url, token?}` → validates (`http[s]://`, scheme optional in the form), writes `controllerUrl` into `config.json` (atomic, preserves the rest of the file), updates the running scout and fires one heartbeat right away. Returns `{ok, controllerUrl, heartbeat}` — `heartbeat.state` is `error` if the controller didn't answer (the URL is still saved). |
| `/api/llama-node/start` | Start a server cell on this host. A llama cell needs the controller-built `args` (with `{{MODEL_PATH}}`-style placeholders), `port`, model file references to download and `cacheModels`. A command cell (`cellKind=command`) needs `shellLine` — the complete `bash -lc` sentence — plus `healthPath`, which is stored with the cell so re-adoption after a scout restart probes the right endpoint (a vLLM cell answers on `/v1/models`, not `/health`). Missing `args` or `shellLine` is refused with a version hint: this scout never assembles its own. Async: returns `{status: "starting", phase: "resolving"}` immediately; progress is visible in `/api/llama-node/status` and the fast heartbeats. |
| `/api/llama-node/stop` | Stop one cell (`{port}`) or ALL cells (no port). Drops the cell from the fleet view and the registry; purges cached models unless the cell had `cacheModels` (safe purge — never evicts a model a sibling cell still serves). A port still held by a process the scout lost track of is reclaimed only when it is recognisably ours (its marker or the llama-server binary); an unrecognised holder is named and left alone. |
| `/api/llama-node/update` | Start a llama.cpp update job: `{tag?}` — empty is the latest release; a commit works too. 409 while one runs. |
| `/api/llama-node/restore` | Restore an archived build: `{id}`. |
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
