# Caravan Scout

The client-side sidecar of the [LAMA CARAVAN](https://github.com/thepr0metheus/lama-caravan)
control plane. **Formerly known as `llm-easy-route-agent`** — if you see that
name in older screenshots, configs or docs, it is this project.

One small service per client machine — with or without a GPU —
that reports the host into the fleet topology and executes the controller's
commands: run llama.cpp server cells locally, download models, re-point
OpenClaw agents at their assigned proxy ports.

Dependency-light on purpose: Python standard library only, one JSON config,
a small HTTP API on `:8092`.

## Why put a scout on every box

The controller can only route to what it can see. The scout is how a machine
becomes visible — and it earns its keep on any box that has either
**hardware** or **agents**:

- **A machine with a GPU** (even a modest one) becomes a place where the
  fleet can run models: reserve a cell on the board, pick a model, press
  Start — the scout downloads the GGUF from the controller's cache, launches
  `llama-server`, reports load progress and slot activity back to the board.
  An old 12 GB card serving a small model at night is real capacity the
  router can use.
- **A machine with AI agents** (OpenClaw, Hermes, anything with an
  OpenAI-style `baseUrl`) gets its agents onto the topology: the scout
  detects them (host processes, docker, VMs), and `apply-routes.py` re-points
  each agent at its personal proxy port on the controller — after which all
  the queueing/scheduling/spill rules apply to that agent's traffic with no
  further changes on the machine, ever.
- Both kinds of hosts report GPUs, VRAM, running compute apps and heartbeat
  health, so the board shows the whole fleet's real state in one place.

Why it's safe to adopt: standard library Python only, one JSON config, one
small HTTP surface on `:8092`, systemd/launchd units, and the
[pairing page](#pairing-with-a-controller-no-config-editing) means setup is
"install, open a browser, paste the controller address".

The bigger picture — hybrid local/cloud routing, queues, schedules, spend
accounting — is the controller's story: see
[LAMA CARAVAN → Why](https://github.com/thepr0metheus/lama-caravan#why-lama-caravan)
and the worked example
[a day with the caravan](https://github.com/thepr0metheus/lama-caravan/blob/main/docs/day-with-the-caravan.md).

## Requirements

| Component | Requirement |
|---|---|
| OS | Linux with systemd --user, or macOS (launchd) |
| Python | **3.10+**, standard library only — no pip packages |
| For llama cells | a `llama-server` binary on this host (`scripts/install.sh` can build it; CUDA optional) |
| For GPU info | NVIDIA driver + `nvidia-smi` (optional — CPU-only hosts are fine) |
| Network | reach the controller's `:8090`; the agent listens on `:8092` |

## Documentation

| Doc | Covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Role in the control plane, flows, the Variant-2 command contract, module layout |
| [docs/http-api.md](docs/http-api.md) | Every endpoint of the `:8092` surface + contracts with the controller |
| [docs/operations.md](docs/operations.md) | Install, services, deploy, config/runtime files, quirks |

## Role

The controller (lama-caravan, `:8090`) is the topology registry and the single
command builder. This agent:

- reports host identity, GPU/CPU inventory, compute apps and local OpenClaw
  agents in a heartbeat every 60 s (faster while a model is loading);
- starts/stops llama.cpp **server cells** on this host from configs built by
  the controller (models are downloaded from the controller and cached);
- runs generic **command cells** (e.g. a whisper server) the same way — the controller
  supplies both the start line and the cell server files themselves;
- receives routing assignments and re-points each local agent's provider
  `baseUrl` at its LAMA CARAVAN proxy port (`apply-routes.py`).

```text
Machine A (any GPU or no GPU)          Machine B (controller)
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  caravan-scout :8092        │◄──────►│  LAMA CARAVAN admin :8090    │
│  ┌───────────────────────┐  │        │                              │
│  │ OpenClaw / local app  │  │        │  Topology board:             │
│  └───────────────────────┘  │        │  • sees Machine A            │
│  ┌───────────────────────┐  │        │  • shows GPU info            │
│  │ llama-server (NVIDIA) │  │        │  • "+ Add as llama server"   │
│  └───────────────────────┘  │        │    → model served over HTTP  │
└─────────────────────────────┘        └──────────────────────────────┘
```

## Install

One-liner on a fresh client host (Linux or macOS):

```sh
git clone <your-remote>/caravan-scout.git ~/projects/caravan-scout
cd ~/projects/caravan-scout
./scripts/install.sh --admin-url http://<controller-ip>:8090
```

| Situation | What happens |
|---|---|
| Linux + NVIDIA GPU | Installs CUDA toolkit, builds `llama.cpp` with CUDA, sets up the model cache |
| Linux, no GPU | Installs the agent only |
| macOS | Installs the agent + launchd service |

The host appears on the controller's Topology board within one heartbeat
(≤ 60 s). Flags: `--admin-url <url>`, `--skip-llama`, `--llama-tag <tag>`.

## Pairing with a controller (no config editing)

If you skipped `--admin-url` (or want to re-point the host later), open the
agent's built-in page from any browser:

```
http://<this-host-ip>:8092/
```

![Pairing page](docs/screenshots/pairing.png)

It shows what the agent detected on this machine (GPUs, local agents, running
cells) and has a single **Pair** field — paste the controller address
(`http://<controller-ip>:8090`), press Pair, and the host saves it to
`config.json`, sends a heartbeat immediately and reports whether the
controller answered. No file editing, no restart.

If the controller has sign-in enabled, paste its **fleet token** into the
second field (the admin shows it in System → Security); it is stored as
`controllerToken` and from then on both directions of scout ⇄ controller
traffic authenticate with it.

Manual start:

```sh
cp examples/config.example.json config.json   # edit hostId/controllerUrl
python3 -m caravan_scout.app --config config.json --state state.json
```

## Default ports

| Port | Service |
|---|---|
| `8090` | LAMA CARAVAN admin (controller) |
| `8092` | this agent |
| `8180` | llama-server on the client (default, configurable) |

## Config reference

```json
{
  "hostId": "host-a",
  "displayName": "host-a",
  "listenHost": "0.0.0.0",
  "listenPort": 8092,
  "controllerUrl": "http://<controller-ip>:8090",
  "heartbeatIntervalSeconds": 60,
  "registryUrl": "",
  "agents": [
    { "id": "openclaw", "name": "OpenClaw", "kind": "openclaw",
      "scope": "host", "runtime": "host", "port": 18791,
      "endpoint": "http://127.0.0.1:18791" }
  ],
  "llamaServerBin": "~/llama.cpp/build/bin/llama-server",
  "modelsBasePath": "~/llama-model-cache",
  "llamaNodeDefaultPort": 8180,
  "cleanOldModels": false,
  "applyCommand": "python3 ~/projects/caravan-scout/apply-routes.py",
  "openclawConfigPath": "",
  "openclawAgentId": "openclaw"
}
```

| Field | Description |
|---|---|
| `controllerUrl` | The LAMA CARAVAN admin URL the heartbeat posts to. |
| `registryUrl` | Optional fleet registry; when set, VM/docker agents are derived from `<registryUrl>/api/agents` instead of the static `agents` list. |
| `llamaServerBin` | Path to the `llama-server` binary (set by `install.sh`). |
| `modelsBasePath` | Local cache dir for downloaded models. |
| `applyCommand` | Shell command that receives routing assignments as JSON on stdin. |

## API

See [docs/http-api.md](docs/http-api.md). In one line each: GET
`health · state · llama-node/status · agent-config?id= · monitor/nvidia-smi ·
llama-node/configs · llama-node/list-cache`; POST `routing/apply · heartbeat ·
llama-node/start · llama-node/stop · llama-node/purge-cache ·
llama-node/configs/delete`.

## Services

```sh
# Linux (systemd --user; installed by install.sh)
systemctl --user restart caravan-scout.service
journalctl --user -u caravan-scout.service -f

# macOS (launchd; installed by install.sh)
launchctl kickstart -k gui/$UID/com.caravan-scout
```

Cells **survive agent restarts**: the units keep child processes alive
(`KillMode=process` / `AbandonProcessGroup`) and the fresh agent re-adopts
them from its registry (`state.json`) — same pid, same uptime, inference
uninterrupted. Orphans that match the llama-server binary but are not in the
registry are reaped. Details: [docs/operations.md](docs/operations.md).

## Deployment rule

Source moves through git only: `commit → push → git pull on each client host →
restart the agent`. No `scp`. Runtime files (`state.json`, `var/`,
`llama-node-configs/`, the model cache) never go through git.

## Safety model

No auth on `:8092`; command cells execute controller-supplied shell. The
trusted-LAN assumption is explicit — do not expose the port beyond your LAN.
