# Operations

## Install

One-liner on a fresh client host (Linux or macOS):

```sh
git clone <your-remote>/caravan-scout.git ~/projects/caravan-scout
cd ~/projects/caravan-scout
bash scripts/install.sh --admin-url http://<controller-ip>:8090
```

`install.sh` is idempotent: writes `config.json` (host id, controller URL),
installs the systemd `--user` unit (Linux) or LaunchAgent (macOS), builds
llama.cpp with CUDA when an NVIDIA GPU is present (`scripts/install-llama.sh`),
and on NVIDIA hosts provisions the faster-whisper server (`install-whisper.sh`).

Manual start:

```sh
python3 -m caravan_scout.app --config config.json --state state.json
```

## Services

| Platform | Unit | Notes |
|---|---|---|
| Linux | `caravan-scout.service` (systemd `--user`) | `WorkingDirectory=%h/projects/caravan-scout`; enable linger for boot-time start |
| macOS | `launchd/com.caravan-scout.plist` | `launchctl kickstart -k gui/$UID/com.caravan-scout` to restart |

## Deploy

Git only — same rule as the controller:

```sh
# locally
git commit … && git push
# on each client host
cd ~/projects/caravan-scout && git pull --ff-only
python3 -m py_compile caravan_scout/*.py
systemctl --user restart caravan-scout.service   # or launchctl kickstart
```

⚠️ **A restart kills the host's running server cells.** llama-server processes
are children of the agent; on startup `reap_stray_llama_servers()` also
terminates orphaned ones. After deploying, restart the affected cells from the
controller's board (their configs are saved). Plan deploys accordingly.

## Config

`config.json` next to the launcher (see the README for the full field table).
The essentials: `hostId`, `controllerUrl`, `agents` (or `registryUrl` to derive
them from the fleet registry), `llamaServerBin`, `modelsBasePath`,
`applyCommand`.

`controllerUrl` can also be set from a browser: open `http://<host>:8092/`
and use the Pair form — it rewrites `config.json` atomically and fires an
immediate heartbeat (no restart needed).

Runtime files (never in git): `state.json` (assignments, apply/heartbeat
status), `llama-node-configs/`, `var/server-cells/<port>/`, the model cache
(`~/llama-model-cache` by default).

## Known quirks

- **Cell crash root causes live on the client**, in
  `<modelsBasePath>/llama-server.log` — rotated on every start (15 kept). All
  slots share that file; command cells log to `command-cell.log` next to it.
  The agent extracts the crash reason (OOM / corrupt GGUF / mmproj mismatch)
  into the heartbeat, so the board shows it.
- **`sudo -n ufw allow <port>`** on cell start is best-effort: without
  passwordless sudo the port silently stays closed to the LAN.
- **`cacheModels=false` (default)**: models re-download on every start and are
  purged on stop; with caching on, only the active models are kept.
- **No auth on `:8092`** and command cells execute controller-supplied shell —
  the trusted-LAN assumption is explicit. Do not expose the port beyond it.
- The heartbeat drops to a fast cadence while any slot is
  resolving/downloading/loading, so board progress is near-live.
