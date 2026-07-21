# Architecture

> Historical note: this project was published internally as
> `llm-easy-route-agent` before the rename to **caravan-scout**.

`caravan-scout` is the client-side sidecar of the
[LAMA CARAVAN](../README.md#role) control plane: one small stdlib-only Python
service per client machine. The controller (lama-caravan, `:8090`) owns the
topology and builds llama-server commands; the agent executes them locally and
reports back.

```text
client host                                   controller host
┌────────────────────────────────┐            ┌────────────────────────────┐
│ caravan-scout  :8092           │            │ lama-caravan admin :8090   │
│  • heartbeat thread ───────────┼──POST────► │  /api/topology/client-     │
│  • HTTP surface  ◄─────────────┼──commands──│    heartbeat               │
│  • _Slot per port:             │            │  (start/stop/configs/…)    │
│      llama-server / command    │            └────────────────────────────┘
│      cell child processes      │
│  • model cache (~/llama-model- │
│      cache), downloaded from   │
│      the controller            │
└────────────────────────────────┘
```

## Flows

**Heartbeat (up).** A daemon thread POSTs `heartbeat_payload()` to
`<controllerUrl>/api/topology/client-heartbeat` every `heartbeatIntervalSeconds`
(60 s), dropping to a faster cadence while any slot is resolving/downloading/
loading so the board shows live progress. The payload carries host identity,
GPU/CPU inventory, compute apps, the OpenClaw agents on this host (statically
configured or derived from the fleet registry), discovery `candidates`
(docker/libvirt), and per-slot `llamaNodes`.

**Commands (down).** The controller calls the agent's HTTP surface (see
[http-api.md](http-api.md)): llama-node start/stop/purge-cache/configs,
`nvidia-smi` snapshots, OpenClaw config reads, and `POST /api/routing/apply`
(re-points each agent's provider `baseUrl` at its assigned caravan proxy port
via `applyCommand`, typically `apply-routes.py`).

**Variant-2 command contract.** The controller builds the FULL llama-server
argument list and sends it in `payload["args"]` with placeholders that the
agent substitutes after downloading the files:

```text
{{MODEL_PATH}}   {{MMPROJ_PATH}}   {{SPEC_PATH}}
```

These constants must stay in sync with the controller's
`LLAMA_PATH_PLACEHOLDER_*`. There is no local fallback builder: an agent that
receives no `args` refuses the start rather than assembling its own list. It
used to carry one — a mirror of the controller's, 23 flags behind — and a cell
started through it ran without half the configuration the board displayed.

Generic command cells (`CELL_KIND=command`) are the same story one level up:
the controller sends `shellLine`, the whole `bash -lc` sentence including
`set -euo pipefail`, the `PORT`/`ENV` exports and the `WORKDIR` change. The
agent executes it verbatim. Assembling it here is what let the controller's
script and the agent's line drift apart.

**Slots.** A host can run several servers at once (e.g. a translator + a
whisper cell). Each port owns a `_Slot`: its `LlamaNode` child process, async
startup progress (`resolving → downloading → starting → running`) and a
per-slot cache flag. Stopping a slot drops it from the fleet view and purges
uncached models — via the safe purge that never evicts a model still served
by a sibling slot.

## Module layout

`python3 -m caravan_scout.app` is the stable entry point (baked into the
systemd/launchd units); the code lives in the package:

| Module | Owns |
|---|---|
| `paths.py` | Env-driven constants, the `{{…}}` placeholder contract, `DEFAULT_CONFIG` |
| `errors.py` | `AppError` (HTTP-visible failures) |
| `hw.py` | Host probes: NVIDIA GPUs, CPU/RAM, firewall state, docker/libvirt, listen ports |
| `node.py` | `LlamaNode` process wrapper (start/stop/status, log rotation, crash-reason extraction) + `_Slot` |
| `registry.py` | `RegistryMixin` — agent roster from the fleet registry, runtime detection |
| `openclaw.py` | `OpenclawMixin` — read agents' OpenClaw configs, derive live assignments |
| `heartbeat.py` | `HeartbeatMixin` — public state snapshot + heartbeat POST loop |
| `models.py` | `ModelsMixin` — model cache: download from the controller, verify, purge |
| `cells.py` | `CellsMixin` — resolve the controller's args/shellLine, write cell artifacts, start/stop, routing apply |
| `agent.py` | `RouteAgent(…mixins)` — slots/config/state core |
| `http.py` | Handler factory for the `:8092` surface |
| `app.py` | Thin launcher + legacy re-exports |

Layering is strict: `paths`/`errors` ← `hw`/`node` ← mixins ← `agent` ←
`http` ← `app`. State lives in `state.json` next to the config; per-node
launch artifacts under `var/server-cells/<port>/`.
