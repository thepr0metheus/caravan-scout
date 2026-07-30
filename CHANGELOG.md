# Changelog

## 1.3.8 — 2026-07-30

- `POST /api/host/poweroff`, beside the reboot that was already here. Its own
  path rather than a flag on the existing one: poweroff is the one action the
  controller cannot undo — nothing on the board can switch this machine back
  on — so it must not be reachable by getting a field wrong, and a scout too old
  to know it answers 404 instead of quietly doing the other one.

  Cells are not stopped first, same as reboot: systemd takes them down with the
  machine. Needs passwordless sudo for `systemctl poweroff`, and says so when it
  is missing rather than reporting success.

## 1.3.7 — 2026-07-29

- A cell whose runner this scout has never heard of no longer skips its file
  sync in silence. The launcher-to-runner mapping was a table compiled in here,
  and `transcribe` was never added to it: `sync_for_command` resolved every
  transcribe cell to no runner, returned on its first line, fetched nothing and
  logged nothing. The client ran whatever a human had once copied into `$HOME`
  while the board showed the cell as current — for four days, and it would have
  lasted until someone compared two files by hand.

  The mapping now comes from the controller's own manifest, which already
  publishes it, so a runner added on the controller reaches every client with no
  scout release at all. The table survives only as a fallback for when the
  manifest cannot be read, and a launcher that resolves to nothing is logged
  with what to do about it.

## 1.3.6 — 2026-07-29

- `GET /api/host/listeners` — what is listening on this box, with the owning
  process where the OS will say. The controller's cell-port picker could only
  see its own host, so a listener on a CLIENT was invisible: the picker painted
  the number free, the cell reserved fine, and then failed to bind. This is the
  client half of that answer, and the controller's port scan consumes it.

## 1.3.5 — 2026-07-25

- A command cell can have a model now, and keeps it. Both assumptions behind
  "command cells download nothing" broke when the caravan grew a transcribe.cpp
  runner whose model is a GGUF path like a llama cell's. Without a download the
  cell started, bound its port and reported the problem only inside its own log
  — healthy from the outside, useless in fact; it now resolves MODEL_FILE
  through the same `_ensure_model` the llama path uses and refuses to start if
  the file cannot be fetched. And because `purge_model_cache_safe()` keeps the
  files of running slots by reading `cfg["modelPath"]`, which command cells left
  empty, a cache purge would have deleted the weights out from under a live
  recognizer; the key is filled in.
- `scripts/install-transcribe.sh` — builds transcribe.cpp with CUDA (Linux) or
  Metal (macOS) and installs the Python binding into `~/transcribe-venv`. Same
  file the controller ships, so there is no second copy to drift; the cell
  servers come from the controller over `/api/cell-assets` like every other
  cell's. Standalone, like install-moonshine.sh and install-tts.sh.

## 1.3.4 — 2026-07-25

- A Stop arriving mid-start no longer loses the race. The startup worker
  captures its slot object once and could spend minutes downloading a model;
  meanwhile Stop dropped the slot and unregistered the cell, and the worker then
  started the process anyway and re-registered it — a llama-server owned by a
  slot that no longer existed. Live consequence: a cell held 10.7 GB of VRAM for
  hours while the board showed its port as stopped, and every start on the same
  GPU then failed for lack of memory. The worker now checks slot identity before
  launching and again before registering; if it lost the race it terminates the
  process it just started instead of orphaning it.
- Stop verifies the port instead of trusting empty handles. With no process
  handle and no adopted pid, `stop()` returned `{"ok": true, "detail": "not
  running"}` having consulted nothing — absence rendered as success. The handler
  now probes the listener, and reclaims it ONLY when the registry marker or the
  configured llama binary matches its cmdline; an unrecognized process is
  reported, not killed, and a stop that could not verify no longer erases the
  registry entry that makes recovery possible.

## 1.3.3 — 2026-07-25

- Every llama cell wrote to one `llama-server.log`, and each new start renamed
  it while a running cell's fd followed the old inode. A crashed cell's card
  therefore quoted whichever cell had spawned last — live incident: :8011's
  "Model loading failed" showed a benign tokenizer warning belonging to the Qwen
  cell on :8006, while its own out-of-VRAM error sat in another file. Logs are
  now per port (`llama-server.<port>.log`, `command-cell.<port>.log`).
- The crash-reason reader respects the llama.cpp log level. It ended with a
  blind `lines[-1]`, so an informational or warning line became the failure
  reason. Now I/W lines are skipped for the loose "error/failed" scan and the
  fallback, and a levelled log with nothing worse than a warning returns no
  reason at all rather than blaming a harmless line. Unprefixed catastrophes
  (`terminate called`, tracebacks, CUDA errors) are still caught, and the
  unambiguous priority patterns stay level-blind so corrupted-download
  auto-repair keeps working.

## 1.3.2 — 2026-07-22

- Removed this agent's own llama-server argument builder (130 lines). It was a
  mirror of the controller's, kept as a fallback, and it had fallen 23 flags
  behind — no `--api-key`, `--embeddings`, `--context-shift`, `--ssl-*`,
  `--kv-unified`, `--webui`. A cell started through it looked fully configured
  on the board while running without half of that configuration. If the
  controller sends no args, the agent now refuses with a version hint instead of
  quietly starting something else.
- The shell wrapper around a command cell is no longer assembled here either.
  The controller sends `shellLine` — flags, exports, workdir, exec — as one
  sentence. The local version had already lost `set -euo pipefail` relative to
  the controller's script renderer.
- Health probes follow the path the controller computed instead of a hardcoded
  `/health`, and the path is remembered per cell so re-adoption after an agent
  restart probes the right endpoint. A vLLM cell answers on `/v1/models`; the
  old probe would have declared a healthy one dead.

  Both changes require lama-caravan v1.3.115+ on the controller.

## 1.3.1 — 2026-07-22

- The bundled copies of the cell servers are gone (`stt/`, `tts/`, `whisper/`).
  The controller owns them now, so keeping a second copy here only recreated
  the drift 1.3.0 was meant to end. Installers fetch what they need through
  `scripts/fetch-cell-assets.sh`, which reads the controller URL and fleet
  token straight from the scout's own config.
- A failed fetch is not fatal anywhere: the installer says so and moves on,
  because the scout fetches the same files before every cell start regardless.

## 1.3.0 — 2026-07-22

- Cell servers now come from the controller. Before starting a command cell the
  scout fetches the files that cell's launcher needs (`GET /api/cell-assets`,
  hashed manifest + the files themselves, fleet token as usual) and writes them
  into `$HOME`. The controller already decided WHAT to run and handed over the
  full command line; it now supplies the script that line names, so a client
  that has not pulled this repo in months still runs the current cell server.
- Nothing here can block a start. An unreachable controller, a truncated body
  or a hash mismatch all leave the existing `$HOME` copy in place and log why —
  an out-of-date cell beats no cell. Writes are atomic, so a host is never left
  with half a launcher.

## 1.2.9 — 2026-07-21

- The Moonshine cell's voice cache is now an LRU capped at
  `MOONSHINE_TTS_CACHE` voices (5 by default). Holding every voice ever asked
  for cost ~180-275 MB each, and English alone offers 60+ — a client letting a
  user audition them would have grown the cell without bound.
- Eviction calls the voice's `close()` and collects, which matters more than
  the eviction itself: measured with a cap of 2 and four languages cycled 20
  times, dropping the reference alone went 913 -> 1858 MB, while closing brings
  the same run to 915 -> 1634 MB (~18 MB down to ~5 MB per switch). The
  remainder is allocator fragmentation, so this bounds growth rather than
  eliminating it — a cell driven through hundreds of switches still creeps and
  a restart is the cure. Evicted voices stay on disk; returning to one is a
  local reload.

## 1.2.8 — 2026-07-21

- The Moonshine cell now offers a choice of voices.
  `GET /v1/audio/voices?language=xx` answers `{present, downloadable}` — the
  stock voices already on disk and the ones still fetchable (60+ for English,
  4 for Russian). `POST /v1/audio/speech` takes an optional `voice`; omit it
  and the language default speaks, exactly as before.
- The voice cache is keyed by (language, voice) instead of language alone, so
  two voices of the same language coexist rather than evicting each other.
  Memory behaves as in 1.2.7: nothing loads until asked for, ~180-275 MB per
  voice held.

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
