# Changelog

## 1.2.7 — 2026-07-21

- The Moonshine cell now speaks as well as listens. The same port serves
  `POST /v1/audio/speech` (json `{text, language}` -> 16-bit PCM mono wav)
  alongside the existing `POST /v1/audio/transcriptions`, and `/health` grew a
  `kinds: ["asr","tts"]` field so a client can list one cell in both roles.
  `model` is still there, so a client that predates `kinds` keeps seeing a
  plain recognizer and nothing breaks on upgrade.
- Recognition and synthesis load independently: the recognizer warms at start
  as before, a voice downloads on the first request for its language and is
  then cached. A cell used only for recognition never pays for a voice —
  measured on the fleet, each loaded voice costs ~180-275 MB of RSS on top of
  the recognizer's ~900 MB, and the cost is per language.
- Synthesis covers 20 locales including Russian and Ukrainian, which the
  recognizer side deliberately does not (whisper stays the RU recognizer).
  It speaks Moonshine's stock voice — voice cloning stays on the xtts/f5/
  cosyvoice cells.
- `run_moonshine.sh --install-only` can pre-download voices via
  `MOONSHINE_PREWARM_VOICES=ru,en`, turning a first synthesis from ~8 s into
  an instant one. Off by default so nothing pays for a voice it never uses.

## 1.2.6 — 2026-07-19

- Bundled Moonshine v2 STT cell (`stt/` + `scripts/install-moonshine.sh`):
  CPU-only speech-to-text — the EN model beats Whisper large-v3 accuracy at
  250M params and runs sub-second on a CPU core, so the GPUs stay free for
  LLMs. Same cell contract as the whisper server (`/health`,
  `POST /v1/audio/transcriptions`); the launcher self-installs its venv and
  the model downloads itself, keyed by a LANGUAGE argument
  (en es zh ja ko vi uk ar — no Russian, whisper stays the RU recognizer).
  Licensing: EN is MIT; the other languages ship under the free Moonshine
  Community License (registration + attribution, below $1M/yr revenue).

## 1.2.5 — 2026-07-18

- The bundled command-cell servers live here and only here. `tts/` and
  `whisper/` also existed in the controller repo, and the two copies had
  drifted: `_pick_device` plus the cosyvoice device selection were in that copy
  and not in this one. This repo owns them because it is what installs them —
  `scripts/install-{tts,whisper}.sh` copy them into `$HOME` on the client, and
  the cell command runs the `$HOME` copy. The reconciled `tts_server.py` is now
  the single source.

## 1.2.4 — 2026-07-18

- Adoption no longer forgets a cell that is alive. On startup the fallback
  "identify the cell by its port" path unregistered the cell whenever a single
  2 s `/health` probe failed — but startup is exactly when the host is busiest,
  so a loaded box timed out on cells that were serving fine. The record was
  deleted while the process kept running, leaving the board showing "stopped"
  forever with no way back short of killing the process by hand. Now only an
  unlistened port unregisters; a port with a live listener is adopted, and the
  probe retries 3× at 4 s before giving up on the phase.
- The firewall, context-size and metrics caches are per-port dicts instead of
  single-slot tuples. With several cells polled in rotation every lookup missed
  the cache, so `sudo ufw status` ran 232×/min and pinned one client at load
  25.8 — cell starts timed out. Same host now idles at 0.7 with 24 calls/min.
- `__version__` had drifted behind the changelog (1.2.1 vs 1.2.3); realigned.

## 1.2.3 — 2026-07-11

- The whisper cell honors an optional `task=translate` multipart field
  (any→English) — used by a voice app's flows; unknown to a server,
  the field is simply ignored.

## 1.2.2 — 2026-07-11

- Voice-clone TTS cells provision like whisper: `tts/` ships
  `tts_server.py` + `run_tts.sh` (XTTS-v2 / F5-TTS / CosyVoice2 behind one
  `/v1/audio/speech-clone` contract) and `scripts/install-tts.sh` drops
  them into `$HOME` plus the system ffmpeg torchcodec needs. Standalone —
  not part of install.sh (engines are tens of GB; pre-warm with
  `install-tts.sh --prewarm "xtts f5 cosyvoice"`).

## 1.2.1 — 2026-07-10

- Client build archives keep 2 snapshots by default (current + one-step
  undo) — client snapshots are large and a client rollback is never
  urgent (running cells keep their binary through any rebuild).
  `llamaBuildsKeep` in config.json overrides.

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
