# Caravan Scout

The hardware sidecar of the [LAMA CARAVAN](https://github.com/thepr0metheus/lama-caravan)
control plane: one small service on each machine that lends its GPU or CPU to
the fleet. **Formerly known as `llm-easy-route-agent`** — if you see that
name in older screenshots, configs or docs, it is this project.

What it does, and all it does:

- **reports its machine** — GPUs and what runs on them, CPU/RAM, the cells,
  the llama.cpp build — in a heartbeat to the controller;
- **runs cells** the controller configures: llama.cpp servers and command
  cells (speech recognition, TTS…), models downloaded from the controller and
  cached here, the cells re-adopted after the scout restarts;
- **keeps llama.cpp current** — updates or rolls back the build when the
  controller asks.

It knows nothing about AI agents (since 2.0). The agents and the machines
they run on — clients — are records the operator makes by hand on the
controller's board; a machine that only runs agents installs nothing. A
machine that both lends a GPU and runs agents simply appears twice: as a node
with its hardware, and as a client card.

Dependency-light on purpose: Python standard library only, one JSON config,
a small HTTP API on `:8092`. Built as small classes with one job each, its
whole HTTP surface pinned by value in tests that cannot reach the host.

## Why put a scout on a box

The controller can only run models where it can see hardware. The scout is how
a machine's hardware becomes visible and usable:

- **A machine with a GPU** (even a modest one) becomes a place where the
  fleet can run models: reserve a cell on the board, pick a model, press
  Start — the scout downloads the GGUF from the controller's cache, launches
  `llama-server`, reports load progress and cell activity back to the board.
  An old 12 GB card serving a small model at night is real capacity the
  router can use.
- **A machine without one** can still run CPU cells (speech recognition,
  small models) the same way.
- Every scout reports GPUs, VRAM, running compute apps and its heartbeat, so
  the board shows the fleet's real hardware in one place.

A machine where AI agents run (OpenClaw, Hermes, anything with an OpenAI-style
`baseUrl`) needs no scout: the operator adds it on the controller's board as a
client and gives each agent a proxy port by hand.

Why it's safe to adopt: standard library Python only, one JSON config, one
small HTTP surface on `:8092`, systemd/launchd units, and the
setup is "run `./install.sh`, then enter the printed address on the
controller's board" ([Adding the machine](#adding-the-machine-to-a-controller)).

The bigger picture — hybrid local/cloud routing, queues, schedules, spend
accounting — is the controller's story: see
[LAMA CARAVAN → Why](https://github.com/thepr0metheus/lama-caravan#why-lama-caravan)
and the worked example
[a day with the caravan](https://github.com/thepr0metheus/lama-caravan/blob/main/docs/day-with-the-caravan.md).

## Requirements

| Component | Requirement |
|---|---|
| OS | Linux with systemd --user, or macOS (launchd) |
| Python | **3.9+**, standard library only — no pip packages (the macOS scout runs on the system's 3.9; CI runs the snapshots on 3.9 and 3.12) |
| For llama cells | a `llama-server` binary on this host (`./install.sh` builds it; CUDA optional) |
| For GPU info | NVIDIA driver + `nvidia-smi` (optional — CPU-only hosts are fine) |
| Network | reach the controller's `:7990`; the scout listens on `:8092` |

## Documentation

| Doc | Covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Role in the control plane, flows, the Variant-2 command contract, module layout |
| [docs/http-api.md](docs/http-api.md) | Every endpoint of the `:8092` surface + contracts with the controller |
| [docs/operations.md](docs/operations.md) | Install, services, deploy, config/runtime files, quirks |

## Role

The controller (lama-caravan, `:7990`) is the topology registry and the single
command builder. The scout:

- reports host identity, GPU/CPU inventory and compute apps in a heartbeat
  every 60 s (every 5 s while a cell is loading);
- starts/stops llama.cpp **server cells** on this host from configs built by
  the controller (models are downloaded from the controller and cached);
- runs generic **command cells** (e.g. a whisper server) the same way — the controller
  supplies both the start line and the cell server files themselves;
- updates or rolls back llama.cpp on this host when the controller asks.

```text
Machine A (any GPU or no GPU)          Machine B (controller)
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  caravan-scout :8092        │◄──────►│  LAMA CARAVAN admin :7990    │
│  ┌───────────────────────┐  │        │  Topology board:             │
│  │ llama-server (NVIDIA) │  │        │  • Machine A as a node       │
│  └───────────────────────┘  │        │  • its GPUs and cells        │
│  ┌───────────────────────┐  │        │  • "＋ Reserve cell"         │
│  │ command cell (whisper)│  │        │    → model served over HTTP  │
│  └───────────────────────┘  │        │                              │
└─────────────────────────────┘        └──────────────────────────────┘
```

## Install

On the machine you are adding (Linux or macOS), from a clone of this
repository:

```sh
git clone <your-remote>/caravan-scout.git ~/projects/caravan-scout
cd ~/projects/caravan-scout
./install.sh
```

That is the whole setup on the machine. The installer puts in what the
machine needs, starts the scout as a service that survives logout and reboot,
waits until it answers and ends with its address and port:

```text
[install] ━━━ caravan-scout is running ━━━
  Address : 192.0.2.23
  Port    : 8092

  Now open the LAMA CARAVAN board: Model servers → ＋ Add scout,
  and enter 192.0.2.23 with port 8092. That is all.
```

| Situation | What happens |
|---|---|
| Linux + NVIDIA GPU | CUDA toolkit if missing (asks for your password once), `llama.cpp` built with CUDA, the model cache, the whisper speech server |
| Linux, no GPU | the scout only — CPU cells need nothing more |
| macOS | the scout + a launchd agent; llama.cpp from Homebrew (`brew install llama.cpp`) |

Re-running it is safe. Flags: `--skip-llama`, `--skip-whisper`,
`--llama-tag <tag>`. `./uninstall.sh` takes the scout off again: it stops
the cells first, then removes the service and the scout's own files, and
names what it left (the llama.cpp build, the model cache) with the command
that removes each.

### Speech engines (standalone, run when you want one)

Not part of `install.sh` on purpose — each takes minutes and most hosts need
none of them. Run the one you want, then add a cell of that runner in the
caravan:

```sh
./scripts/install-moonshine.sh    # 🌙 CPU speech-to-text + synthesis
./scripts/install-transcribe.sh   # 📝 transcribe.cpp — GGUF ASR on ggml (CUDA/Metal)
./scripts/install-tts.sh          # 🛠 voice-clone TTS cells
```

`install-transcribe.sh` builds a C++ library from source (cmake + a few
minutes), unlike the others which only make a venv. It is what gives a client
a good RUSSIAN recognizer: GigaAM-v3 sits near 8% WER against 21-25% for
whisper large-v3, from a 260 MB GGUF the controller ships to the cell's model
cache on first start.

## Adding the machine to a controller

The controller pairs the scout — from its board, not from this machine. On
the LAMA CARAVAN board open **Model servers → ＋ Add scout**, enter the
address the installer printed (port 8092 is filled in) and press Connect.
The controller finds the scout, hands it its own address and — when sign-in
is on — its fleet token, and waits for the first heartbeat: the machine
appears as a node with its GPUs, ready for cells. Connecting again is the
connection test, and the machine's ✕ on the board lets go of it: the scout
forgets the controller and waits to be added again.

A scout nobody has added yet is open on the LAN, like any fresh install;
once paired, everything but its page and `/api/health` asks for the fleet
token. After the token is regenerated on the controller, adding the scout
again hands over the new one.

The scout's own page, `http://<this-host>:8092/`, is for reading: what the
machine has, whether a controller has paired it, and the address to enter.

Manual start (without the service):

```sh
python3 -m caravan_scout.app --config config.json --state state.json
```

## Default ports

| Port | Service |
|---|---|
| `7990` | LAMA CARAVAN admin (controller) |
| `8092` | this scout |
| `8180` | llama-server on the client (default, configurable) |

## Config reference

```json
{
  "hostId": "host-a",
  "displayName": "host-a",
  "listenHost": "0.0.0.0",
  "listenPort": 8092,
  "heartbeatIntervalSeconds": 60,
  "llamaServerBin": "~/llama.cpp/build/bin/llama-server",
  "modelsBasePath": "~/llama-model-cache",
  "llamaNodeDefaultPort": 8180,
  "cleanOldModels": false
}
```

| Field | Description |
|---|---|
| `controllerUrl` | The LAMA CARAVAN admin URL the heartbeat posts to — written by the controller when it adds the scout. |
| `llamaServerBin` | Path to the `llama-server` binary (set by `install.sh`). |
| `modelsBasePath` | Local cache dir for downloaded models. |
| `controllerToken` | The fleet token, when the controller has sign-in enabled (the controller hands it over when it adds the scout). |

## API

See [docs/http-api.md](docs/http-api.md). In one line each: GET
`health · pairing · state · llama-node/status · monitor/nvidia-smi · host/listeners ·
llama-node/configs · llama-node/list-cache · llama-node/update-status ·
llama-node/builds`; POST `controller-url · unpair · heartbeat · llama-node/start ·
llama-node/stop · llama-node/update · llama-node/restore ·
llama-node/purge-cache · llama-node/configs/delete · host/reboot ·
host/poweroff`.

## Services

```sh
# Linux (systemd --user; installed by install.sh)
systemctl --user restart caravan-scout.service
journalctl --user -u caravan-scout.service -f

# macOS (launchd; installed by install.sh)
launchctl kickstart -k gui/$UID/com.caravan-scout
```

Cells **survive scout restarts**: the units keep child processes alive
(`KillMode=process` / `AbandonProcessGroup`) and the fresh scout re-adopts
them from its registry (`state.json`) — same pid, same uptime, inference
uninterrupted. Orphans that match the llama-server binary but are not in the
registry are reaped. Details: [docs/operations.md](docs/operations.md).

## Deployment rule

Source moves through git only: `commit → push → git pull on each scout host →
restart the scout`. No `scp`. Runtime files (`state.json`, `var/`,
`llama-node-configs/`, the model cache) never go through git.

## Safety model

Open until a controller adds it, on a trusted LAN; once the controller has
handed over its fleet token, every endpoint except the scout's page,
`/api/pairing` and `/api/health` requires it — including letting go of the
scout, which only its controller can do. A pairing with a new token passes
only when the same controller accepts that token. Command cells execute
controller-supplied shell — do not expose the port beyond your LAN.
