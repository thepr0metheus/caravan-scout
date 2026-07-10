# Changelog

## 1.2.0 — 2026-07-10

- Build archive + restore: every successful update snapshots the built
  llama.cpp (last 5 kept) and `GET /api/llama-node/builds` /
  `POST /api/llama-node/restore {id}` list and restore them — same
  background job and heartbeat status as updates. Restore re-checks the
  clone out at the archived commit; running cells keep their binary
  until restarted.

## 1.1.0 — 2026-07-10

- One-click llama.cpp updates from the controller: `POST
  /api/llama-node/update {tag?}` runs `scripts/update-llama.sh` (a synced
  copy of the controller's install script: release-tag/commit `checkout
  -f`, stale-build-dir guard, probe-gated Blackwell workaround, cmake
  build) as a background job; `GET /api/llama-node/update-status` streams
  the log tail, and a slim status rides every heartbeat so the fleet
  board can show build progress. Running cells keep the old binary until
  restarted — never automatic. An empty tag resolves the latest upstream
  release; passing the controller's commit converges the client onto the
  controller's exact build.

## 1.0.1 — 2026-07-08

- Fix: a cell whose launch command exec's into another program (e.g.
  `run_whisper.sh` → `exec python whisper_server.py`) is now re-adopted
  across an agent restart instead of being dropped. The exec rewrites the
  process argv, so the recorded launch marker no longer appears in `ps`;
  adoption now falls back to identity by PORT — whoever is healthily
  serving the cell's port (`/health` 2xx) is adopted as the cell. This
  also recovers when a failed restart left a stale pid in the registry.
  Symptom fixed: the cell showed CONFIGURED while its healthy server was
  still running, and a START retry hit `[Errno 98] Address already in use`.

## 2026-07-04

### 📝 Обновление changelog

**Зачем:** Запись в changelog за 2026-07-04 — изменений в Caravan Scout не зафиксировано.
**Что:** Создана ветка , внесена пустая запись в changelog, ветка слита в master.
**Коммиты:** —


## 1.0.0 — 2026-07-03

First public release (formerly `llm-easy-route-agent`).

- Heartbeats: host identity, GPU/CPU inventory, compute apps, local agents
  (host processes / docker / libvirt VMs) into the LAMA CARAVAN controller.
- Server cells: start/stop llama.cpp servers and generic command cells from
  controller-built configs; model download + cache; load progress reporting.
- Routing apply: re-points local OpenAI-compatible agents at their assigned
  proxy ports (`apply-routes.py`).
- Built-in pairing page on `:8092` — paste the controller address, done.
- Stdlib-only Python package `caravan_scout/`, systemd + launchd units,
  one-line installer.
