# HTTP API (`:8092`)

Everything is JSON; errors come back as `{"error": "..."}` with the `AppError`
status (or 500). There is **no auth** — the surface assumes a trusted LAN (the
same assumption the whole control plane makes).

## GET

| Path | Purpose |
|---|---|
| `/` | Built-in pairing page (HTML): host summary + a form that sets `controllerUrl`. |
| `/api/health` | Liveness: `{ok, service, time}`. |
| `/api/state` | Full public state: host identity/IP, GPUs, CPU/RAM, compute apps, agents (+runtime annotations), discovery candidates, assignments, apply/heartbeat status, per-slot `llamaNodes`. Same shape the heartbeat pushes. |
| `/api/llama-node/status` | Just the managed server slots: `{ok, nodes: [...]}`. |
| `/api/agent-config?id=` | The named agent's OpenClaw config (read from this host / docker volume / over SSH for a VM). |
| `/api/monitor/nvidia-smi` | A raw `nvidia-smi` snapshot for the controller's monitor drawer. |
| `/api/llama-node/configs` | Saved launch configs stored on this client (`llama-node-configs/`). |
| `/api/llama-node/list-cache` | Contents of the local model cache. |

## POST

| Path | Purpose |
|---|---|
| `/api/routing/apply` | Apply routing assignments: validates the payload, stores it in `state.json`, pipes it to `applyCommand` (e.g. `apply-routes.py`, which re-points each OpenClaw agent's provider `baseUrl` and restarts it). |
| `/api/heartbeat` | Trigger one immediate heartbeat POST to the controller (the controller calls this when the Topology page opens). |
| `/api/controller-url` | Pair this host with a controller: `{url}` → validates (`http[s]://`, scheme optional in the form), writes `controllerUrl` into `config.json` (atomic, preserves the rest of the file), updates the running agent and fires one heartbeat right away. Returns `{ok, controllerUrl, heartbeat}` — `heartbeat.state` is `error` if the controller didn't answer (the URL is still saved). |
| `/api/llama-node/start` | Start a server cell on this host. Body carries the controller-built `args` (with `{{MODEL_PATH}}`-style placeholders), `port`, model file references to download, `cacheModels`, and optional `CELL_KIND=command` fields (`COMMAND`, `ENV`, `WORKDIR`, `HEALTH_PATH`). Async: returns `{status: "starting", phase: "resolving"}` immediately; progress is visible in `/api/llama-node/status` and the fast heartbeats. |
| `/api/llama-node/stop` | Stop one slot (`{port}`) or ALL slots (no port). Drops the slot from the fleet view; purges cached models unless the slot had `cacheModels` (safe purge — never evicts a model a sibling slot still serves). |
| `/api/llama-node/purge-cache` | Manually clear the model cache (safe variant). |
| `/api/llama-node/configs/delete` | Delete a saved launch config by `filename`. |

## Contracts with the controller

- The heartbeat goes UP to `<controllerUrl>/api/topology/client-heartbeat`;
  everything else is the controller calling DOWN into this surface.
- `{{MODEL_PATH}}` / `{{MMPROJ_PATH}}` / `{{SPEC_PATH}}` in `args` must match
  the controller's `LLAMA_PATH_PLACEHOLDER_*` constants.
- Model files are downloaded from the controller's
  `GET /api/models/download?path=…` (resumable, retry/backoff, atomic rename,
  size verification — a truncated download is deleted, never served).
