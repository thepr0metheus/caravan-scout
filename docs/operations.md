# Operations

## Install

On the machine being added (Linux or macOS):

```sh
git clone <your-remote>/caravan-scout.git ~/projects/caravan-scout
cd ~/projects/caravan-scout
./install.sh
```

`install.sh` is idempotent and runs the scout where it was cloned: builds
llama.cpp with CUDA when an NVIDIA GPU is present (`scripts/install-llama.sh`,
which leaves an already built binary and its checkout alone), provisions the
faster-whisper server on NVIDIA hosts (`install-whisper.sh`), installs and
starts the systemd `--user` unit (Linux, with lingering so it survives
logout and reboot) or the LaunchAgent (macOS), opens the scout's port in ufw
when it can, waits for `/api/pairing` to answer and prints the address and
port. It writes no controller address: the controller pairs the scout from
its board (Model servers → ＋ Add scout) through `POST /api/controller-url`,
and lets go of it through `POST /api/unpair`.

`uninstall.sh` stops the cells through the scout's own API, then removes the
service and the scout's files (config, state, cell artifacts, logs), and
names what it left: the llama.cpp build, the model cache, the whisper venv,
the clone.

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
llama-server that a scout started and that is in no record — an orphan
holding a GPU and a port (`Cells.reap_strays`). Every cell the scout starts
carries `CARAVAN_SCOUT_CELL=<port>` in its environment; a process without it
— the controller's own cells on a machine the scout shares, a server run by
hand — is never killed and never adopted (`HostProcesses.owned`).

## Config

`config.json` next to the launcher (see the README for the full field table).
The essentials: `hostId`, `controllerUrl`, `llamaServerBin`, `modelsBasePath`,
and `controllerToken` when the controller has sign-in enabled.

`controllerUrl` and `controllerToken` are written by the controller when it
adds the scout (`POST /api/controller-url`: atomic rewrite, an immediate
heartbeat, no restart) and removed when it lets go (`POST /api/unpair`). The
page on `http://<host>:8092/` only reads.

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
  line — if a scout started it) and reaps only unmatched llama-server orphans
  a scout started. Adopted processes are managed by pid
  (liveness `kill(pid,0)`, stop SIGTERM→SIGKILL) — the one thing lost across
  the adopt boundary is the exit code of a crash that happens while adopted.
  A command cell started by a scout older than 2.2 carries no marker: if its
  exec chain rewrote its command line, 2.2 does not re-adopt it after the
  update — restart it from the board.

- **Cell crash root causes live on the scout host**, one log per port:
  `<modelsBasePath>/llama-server.<port>.log`, and `command-cell.<port>.log`
  for command cells — the previous run's log is moved aside on every start
  (15 kept), never truncated. The scout extracts the crash reason (OOM /
  corrupt GGUF / mmproj mismatch) into the heartbeat, so the board shows it.
- **`sudo -n ufw allow <port>`** on cell start is best-effort: without
  passwordless sudo the port silently stays closed to the LAN.
- **`cacheModels=false` (default)**: models re-download on every start and are
  purged on stop; with caching on they stay. `cleanOldModels=true` (off by
  default) removes the other cached models after a start, keeping what the
  running cells hold.
- **Models are read in place when this machine has them.** The controller
  says where it reads each model file (`inPlace` in the start request). The
  scout on the controller's own machine, or one that mounts a library at the
  same path, reads the file there — the same size, or it is another file —
  instead of copying it into its cache; a look at a path that does not answer
  in 3 s (a dead NFS mount) counts as absent. A library's file or a folder
  model (seamless) that this machine lacks is refused with what to do. A
  command cell gets `LLAMA_MODELS_DIR` pointing where its model really is — in
  place or in the cache; a value set in the cell's config wins.
- **The scout deletes only what it downloaded.** Each download is written down
  in `.caravan-downloads.json` in the cache; the purge, the old-model cleanup
  and the corrupt-model retry delete only those files. A cache dir set to a
  shared folder — the model library on the controller's machine, a NAS mount —
  loses nothing. Files cached by a scout older than 2.2 are not in the record
  and stay until removed by hand.
- **One llama.cpp build at a time per tree.** `update-llama.sh` (and the
  controller's `install-llama.sh`) take `flock` on
  `<llama dir>.caravan-build.lock`, next to the tree; a second build or restore
  stops with exit 75. Without `flock` (macOS) there is no lock.
- **No auth on `:8092`** and command cells execute controller-supplied shell —
  the trusted-LAN assumption is explicit. Do not expose the port beyond it.
- The heartbeat drops to a fast cadence while any cell is
  resolving/downloading/loading, so board progress is near-live.
