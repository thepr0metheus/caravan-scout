# Operations

## Install

One-liner on a fresh scout host (Linux or macOS):

```sh
git clone <your-remote>/caravan-scout.git ~/projects/caravan-scout
cd ~/projects/caravan-scout
bash scripts/install.sh --admin-url http://<controller-ip>:7990
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
# on each scout host
cd ~/projects/caravan-scout && git pull --ff-only
python3 -m py_compile caravan_scout/*.py
systemctl --user restart caravan-scout.service   # or launchctl kickstart
```

A restart does **not** stop the host's cells: the fresh scout re-adopts every
cell in its registry (see below). What it does terminate at start is a
llama-server that is in no record — an orphan holding a GPU and a port
(`Cells.reap_strays`).

## Config

`config.json` next to the launcher (see the README for the full field table).
The essentials: `hostId`, `controllerUrl`, `llamaServerBin`, `modelsBasePath`,
and `controllerToken` when the controller has sign-in enabled.

`controllerUrl` can also be set from a browser: open `http://<host>:8092/`
and use the Pair form — it rewrites `config.json` atomically and fires an
immediate heartbeat (no restart needed).

Runtime files (never in git): `state.json` (heartbeat status and the `cells`
registry; a 1.x state's `assignments`/`applyStatus` are dropped once at
start), `llama-node-configs/`, `var/server-cells/<port>/`, the model cache
(`~/llama-model-cache` by default).

## Known quirks

- **Scout restarts do not interrupt cells.** systemd (`KillMode=process`) and
  launchd (`AbandonProcessGroup`) leave the llama-server / command children
  running when the scout stops; the fresh scout re-adopts them from the
  `cells` registry in `state.json` (pid + cmdline-marker match, or whoever
  healthily serves the cell's port when an exec chain rewrote the command
  line) and reaps only unmatched llama-server orphans. Adopted processes are managed by pid
  (liveness `kill(pid,0)`, stop SIGTERM→SIGKILL) — the one thing lost across
  the adopt boundary is the exit code of a crash that happens while adopted.

- **Cell crash root causes live on the scout host**, one log per port:
  `<modelsBasePath>/llama-server.<port>.log`, and `command-cell.<port>.log`
  for command cells — the previous run's log is moved aside on every start
  (15 kept), never truncated. The scout extracts the crash reason (OOM /
  corrupt GGUF / mmproj mismatch) into the heartbeat, so the board shows it.
- **`sudo -n ufw allow <port>`** on cell start is best-effort: without
  passwordless sudo the port silently stays closed to the LAN.
- **`cacheModels=false` (default)**: models re-download on every start and are
  purged on stop; with caching on, only the active models are kept.
- **No auth on `:8092`** and command cells execute controller-supplied shell —
  the trusted-LAN assumption is explicit. Do not expose the port beyond it.
- The heartbeat drops to a fast cadence while any cell is
  resolving/downloading/loading, so board progress is near-live.
