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
- runs generic **command cells** (e.g. a whisper server) the same way;
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

It shows what the agent detected on this machine (GPUs, local agents, running
cells) and has a single **Pair** field — paste the controller address
(`http://<controller-ip>:8090`), press Pair, and the host saves it to
`config.json`, sends a heartbeat immediately and reports whether the
controller answered. No file editing, no restart.

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

⚠️ Restarting the agent kills the server cells it runs (they are child
processes). Restart the cells from the controller's board afterwards — their
configs are saved. Details: [docs/operations.md](docs/operations.md).

## Deployment rule

Source moves through git only: `commit → push → git pull on each client host →
restart the agent`. No `scp`. Runtime files (`state.json`, `var/`,
`llama-node-configs/`, the model cache) never go through git.

## Safety model

No auth on `:8092`; command cells execute controller-supplied shell. The
trusted-LAN assumption is explicit — do not expose the port beyond your LAN.
