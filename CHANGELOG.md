# Changelog

## 2.14.0 — 2026-09-25

- **A model of an engine next to the cells is loaded and unloaded from the
  board.** `POST /api/engines/load` and `/api/engines/unload` —
  `{kind, port, model, contextLength?}` — load a model into Ollama or LM
  Studio (0.4+) with the window asked for, or unload it. Only an engine the
  scan found, only what its view offers (`controls`), only a model it listed,
  one act per model at a time. The answer comes at once and the act runs on
  its own thread: a first load takes seconds to a minute. While it runs the
  model carries `action`; a refusal stays on it as `actionError`, in the
  engine's own words, until the next act. Ollama keeps a model loaded from
  the board until it is unloaded there (`keep_alive: -1`), as a started cell
  runs until it is stopped. Looking at an engine stays read only.

## 2.13.0 — 2026-09-25

- **An engine open to the network says who its firewall lets in.** An
  engine that listens beyond 127.0.0.1 could still be closed to the
  controller's proxy: ufw on its machine had no rule for its port, and the
  board offered to route to it. Each engine's view carries `firewall` —
  `{state, allowedFrom}`, the same reading a cell's port gets (open, all,
  restricted, blocked, unknown) — or `null` for an engine on 127.0.0.1 only,
  where no rule matters and ufw is not asked.

## 2.12.1 — 2026-09-25

- **The engines snapshot's example address is a documentation one.** 2.12.0's
  test bound an engine to a made-up 192.168 address, which the public
  mirror's leak scan cannot tell from a real one; it is 203.0.113.20
  (TEST-NET-3) now. No change to the scout itself.

## 2.12.0 — 2026-09-25

- **The engines next to the cells are named.** Ollama or LM Studio on the
  same machine held part of a card, and the board could only say "outside
  6.1 GB". The report now carries `engines`: each one found where it listens
  by default (11434, 1234) or where a process of its name listens — never on
  a cell's port — with its version, whether it takes connections from the
  network or from this machine only, its models (what is loaded, the memory
  and VRAM a loaded Ollama model holds, the window it serves, when
  keep_alive unloads it; an Ollama cloud model marked as not running here),
  its processes and their memory. Read only: GET on the engine's own API,
  nothing loaded, unloaded or stopped. Rescanned every 10 s on a thread of
  its own, so a hung engine never holds a report; `null` before the first
  scan, `[]` when none was found.
- **The processes on a card have names.** `computeApps` rows carry `name`,
  the executable nvidia-smi names ("" when it cannot) — so memory that is not
  a cell's can be named even when it is no engine.
- **Listeners on macOS, and where they are bound.** `/api/host/listeners`
  used `ss` only and answered nothing on macOS; `lsof` stands in where there
  is no ss. Each port says the addresses it is bound on (`addrs`). A failed
  ss read as "nothing listens"; a tool that fails is `{ok: false}` now.

## 2.11.0 — 2026-09-25

- **A running cell says which of its files changed on disk.** A process
  holds the files it opened, not their names: a model, projector or draft
  replaced under a running cell reaches it only on a restart. The
  controller measured that for its own cells (⟳ on the card) from the unit's
  start time; since its step 6.9 every cell is a scout's, and the files are
  on the scout's machine. A running cell now carries `launchDiskNewer`: the
  roles of the files it holds whose mtime is after its start (a checkpoint
  folder by its newest file; a file that cannot be read has not changed).

## 2.10.2 — 2026-09-25

- **The controller's own machine is shown at its network address.** The
  scout names the address the controller reaches it at; on the controller's
  own machine it was paired over 127.0.0.1 and named 127.0.0.1, so the board
  showed that machine's cells at `127.0.0.1:<port>` — an address nobody else
  reaches, and a link that opened the viewer's own computer. A controller on
  loopback (localhost, 127.0.0.0/8, ::1) now makes the scout name the address
  of its default route instead. The cells already listened on every
  interface. (Correction: the controller builds the routes to this machine's
  cells from the address its scout names, so they moved from 127.0.0.1 to the
  network address too — and they work there, checked live.)

## 2.10.1 — 2026-09-25

- **The identity snapshot's example machine is a made-up one.** 2.10.0's
  upgrade case used a real machine's name; the tests name no real machine.
  No change to the scout itself.

## 2.10.0 — 2026-09-25

- **A renamed machine stays the same host.** The id a scout reports was the
  hostname, read at every start: `hostnamectl set-hostname` turned a machine
  into a new host on the board, and its cells, their schedules and
  autostart, its power schedule and the client record of the same machine
  stayed with the old one. The id in use is now pinned in `state.json`
  (`[identity] host id '…' pinned` in the log), and a rename changes only
  the name the board shows — the live hostname, at the next report. The
  first run after the upgrade pins the id the scout reports now, so no
  machine turns into a new host by upgrading. A `hostId` in `config.json`
  is the operator's choice: it wins and becomes the pin; the board then sees
  a new host, and its cells move with "move cells". Not `/etc/machine-id`:
  clones of one VM image share it, and macOS has none.

## 2.9.1 — 2026-09-25

- **The build script is the controller's copy again.** `update-llama.sh` is a
  synced copy of the controller's `install-llama.sh`, and it had fallen two
  changes behind. It still restarted the controller's `lama-cell@` units after
  a build: they went in the controller's step 6.9, a scout's cells never had
  one, and the scout's own job passes `--no-restart` anyway. It also lacked
  the guard that removes a stale `.git/index.lock` nobody holds (one sat for
  17 days and failed every update at "fetching"). The copy is synced, and a
  test compares it with the controller's line for line whenever the
  controller's repository is next to it.
- **What the controller no longer has is the scout's own fact.** The memory
  limits (MemoryHigh 70 %, MemoryMax 80 %, swap 2 GB) and the crash-banner
  rule (the engine-death words; 3 crashes in 15 minutes on a build younger
  than 6 hours) were checked against the controller's copies. Those copies
  went with the controller's own cells. The limits check then said "no
  controller next to us, skipped" with the controller right there, and the
  rule check failed. Both are pinned by value here now. A check that finds
  the controller's repository but not the file it reads fails instead of
  skipping.

## 2.9.0 — 2026-09-25

- **vLLM on this machine, its versions and its updates.** The controller
  updated and rolled back vLLM in its own machine's venv only; a machine
  with a scout had no way to move its vLLM, and the controller's machine
  runs its cells through its scout now. The scout answers for
  `~/vllm-venv` — the venv the controller's vLLM start line provisions and
  runs: `GET /api/vllm` (the version from its dist-info folder, the
  versions it has had — five, newest first: the rollback candidates, kept
  next to state.json — and the install job), `POST /api/vllm/update`
  `{version?}` (the latest release, or a pinned version: a rollback) and
  `GET /api/vllm/update-status`. The install is a job of its own, apart
  from the llama.cpp build's (`BackgroundJob`, the build's job taken out
  of `LlamaBuilds`), so it never shows as "building…" on the board.

## 2.8.2 — 2026-09-25

- **A cell's log makes its own folder.** The logs live in the model cache,
  and only a download made that folder: a scout that reads every model in
  place had none, and its first cell died opening its log ("No such file or
  directory") before it ran. The folder is now made where the log opens —
  for a llama cell, a command cell and a restart after a crash alike.

## 2.8.1 — 2026-09-25

- **A llama cell starts with the environment the controller names.** A
  CPU-only cell (`N_GPU_LAYERS` 0) must not see the card: a CUDA build of
  llama.cpp still wakes it at `-ngl 0` and dies of out-of-memory when a
  neighbour fills it. The controller's own start script has always hidden
  the card (`CUDA_VISIBLE_DEVICES=""`); this scout started the same cell
  with the card in view. The start request now carries `env` (NAME ->
  value): llama-server starts with it over the scout's own environment,
  the cell's start.sh and cell.json write it down, and a restart after a
  crash keeps it. A malformed `env` is refused (400) before anything
  starts.

## 2.8.0 — 2026-09-24

- **The machine second by second.** The controller draws its own machine
  from a sample a second; a scout's machine was drawn from its reports, a
  GPU reading kept ten seconds. `Telemetry` samples the cards and the
  processor every second while a board watches — the controller asks
  `GET /api/telemetry?since=` about once a second while one is open — and
  every ten seconds otherwise, and keeps ten minutes. The processor share is
  measured from /proc/stat, as the controller measures its own (macOS: the
  load average). Both reports say so (`telemetry`), and the controller asks
  only a scout that does.

## 2.7.0 — 2026-09-24

- **vLLM cells, ready for the move.** A start the card cannot hold is refused
  before it runs: the controller sends what a start reserves (`vram` — vLLM
  takes utilization × the card), and the scout checks its free memory at
  launch, at boot too, naming the numbers, the card and the cells holding
  it. A vLLM cell reports its queue (`requestsProcessing`,
  `requestsWaiting`) and its token speed — vLLM 0.24 exports only counters,
  so the rates are their growth per second between two readings; a first
  reading, a new counter or a restarted server gives no rate.
- **A cell that runs but does not listen yet says so.** vLLM installs and
  loads for minutes before its port opens; the scout asks its OS which ports
  listen (`ss`, `lsof` on macOS) and says `listening` per running cell, and
  one still starting says its last log lines (`startingTail`), so the board
  shows it starting and where, instead of running.
- The metrics probe is `ServerProbe` now (it reads llama-server and vLLM).
  The vLLM start line itself comes from the controller, which fixed it in
  1.3.362: it used to never start on a scout.

## 2.6.0 — 2026-09-24

- **Memory limits, as the controller's cells have.** On Linux a cell is
  launched in its own systemd user scope with `MemoryHigh=70%`,
  `MemoryMax=80%` and `MemorySwapMax=2G` — the values of the controller's
  `lama-cell@.service`: a model that eats the RAM is slowed, then killed
  alone, not the machine with the scout on it. The scout asks once, by
  launching a scope and reading its `memory.max` (systemd accepts the limit
  even where nothing would enforce it), and says the answer in one `[cells]`
  line of its log. macOS and hosts without a user systemd run cells as
  before, and the line says why.
- **The last lines of a crashed cell's log.** The crash note carries the last
  8 lines of the crashed run's log (`crash.tail`), read when the crash is
  seen — the relaunch moves that log aside. The board shows them on hover, as
  a cell of the controller shows its journal. Keys are scrubbed out of
  whatever leaves the machine from a cell's log, the crash reason included:
  a key's prefix (`lcv1_`, `sk-`, `hf_`, `ghp_`, `glpat-`), a `Bearer` value,
  whatever follows `api_key` / `password` / `secret`, a long value after
  `token` — `EOS token = 151645` stays.
- **A fresh llama.cpp build that crashes cells.** With the binary younger
  than 6 hours, 3 engine crashes in 15 minutes (the controller's words:
  `CUDA error`, `GGML_ABORT`, `SIGSEGV`, `SIGABRT`, a core dump) raise an
  incident, kept in `state.json` under the build until it is dismissed for
  that build (`POST /api/llama-node/suspect-dismiss`) or the build changes.
  Both reports carry it as `llamaSuspect`, with the newest archived build of
  another commit to offer; the board's banner offers the rollback, and only
  the operator restores.
- **A cell's end in one set of words.** A cell killed by a signal says which
  (`died of SIGSEGV`, not `exited (code -11)`), the fresh-build suspect hears
  it, and the card and the crash note say the same; an adopted cell's exit
  code, which the scout cannot know, reads as unknown instead of `None`.

## 2.5.0 — 2026-09-24

- **A crashed cell comes back.** The controller's cells are systemd units
  with Restart=on-failure; a scout's cell stayed down. The watchdog launches
  a cell that died without being stopped again the same way 10 s later — the
  argv, the extra environment and the log each cell's record now keeps, so
  also after a scout restart — at most 3 times in 10 minutes; then it stays
  down and its error says the watchdog gave up and why. A clean exit is not a
  crash. Each cell carries its crash note (`crash`: how many times since it
  was last started by hand, when, why) — the 💥 on the board; a start by hand
  clears it. A cell launched before 2.5 keeps no launch and is reported, not
  restarted.

## 2.4.0 — 2026-09-24

- **Cells start with the machine.** ↟ on the board now works for a scout's
  cells: the scout keeps the request that starts the cell (the controller
  sends it on ↟ and again when the cell's settings are saved; every start
  refreshes it) and starts those cells when its machine boots —
  `POST /api/llama-node/autostart`, and the ports in `autostart` of both
  reports. On the first scout start of a boot only: the machine's boot id is
  written down, and a scout restart or update in the same boot starts nothing
  — a cell the operator stopped stays stopped, as with systemd's `enable`. A
  machine that will not say which boot it is gets no autostart rather than a
  surprise start.

## 2.3.0 — 2026-09-24

- **Models are read where they are.** The controller now says, for every
  model file of a cell it starts, where it reads that file itself — its
  disk, a library, a folder (`inPlace`). A scout that has the same file at
  that path — the scout on the controller's own machine, one that mounts a
  library at the same path — reads it there, with no copy in its cache and
  nothing to purge. The size tells the same file from another at that path;
  a path that does not answer in 3 s (a dead NFS mount) counts as absent.
  - A library's model or a folder model (seamless) that this machine does
    not have is refused (409) naming the library or the folder and what to
    do — a download from the controller could only answer 404.
  - A command cell gets `LLAMA_MODELS_DIR` pointing where its model really
    is. Its command reads `${LLAMA_MODELS_DIR:-~/llama.cpp/models}/<file>`
    while the download went to the scout's cache, and the two never met: a
    transcribe cell on a scout found no model unless its config said where.
    A value set in the cell's config still wins.

## 2.2.0 — 2026-09-24

- **The scout touches only what it started and what it downloaded.** On a
  machine it shares — the controller's, where it is to run next — it used to
  endanger what was not its own:
  - At start it killed every llama-server running its binary that its
    registry did not name — the controller's own cells included — and it
    adopted whoever served a cell's port. Every cell now carries
    `CARAVAN_SCOUT_CELL=<port>` in its environment (it survives `exec`), and
    only such a process is reaped or adopted by port.
  - It deleted model files by pattern: every `.gguf` but the active one after
    each start with caching on (`cleanOldModels` was never read), and every
    `.gguf`/`.tmp` on stop with caching off — a cache dir pointed at a model
    library would have lost the library. Downloads are written down in
    `.caravan-downloads.json`; the purge, the cleanup and the corrupt-model
    retry delete only those. `cleanOldModels` is honoured (off by default),
    and the cleanup keeps what the running cells hold — it took a
    neighbour's model.
  - The corrupt-model retry read the port's log after a start that never
    ran — the previous run's — and deleted a good model when the binary was
    missing. It now acts on this attempt's own error, and on a model it did
    not download it only says the file looks damaged.
  - `update-llama.sh` and the controller's `install-llama.sh` take one `flock`
    next to the llama.cpp tree: two builds of one tree at once left a binary
    of two commits.
  - Upgrade note: a command cell started by 2.1 whose exec chain rewrote its
    command line is not re-adopted after the update — restart it from the
    board. Files cached before 2.2 are not in the record and stay.

## 2.1.1 — 2026-09-24

- **Installing the scout again changes nothing that works.** The whisper
  step upgraded its venv on every run, so a reinstall quietly moved a working
  whisper cell onto new CUDA libraries (it happened: cuDNN 9.24 → 9.26 on the
  first reinstall). A venv where faster-whisper already imports is left as it
  is now; remove it to reinstall.
- **The firewall hint names the ports cells really use.** The llama.cpp step
  told the operator to open 8180 — the port of the single llama-server of old,
  on which no cell lives. The installer's firewall step now opens the scout's
  port as before and names the controller's cell range (22001–22999 unless
  changed there) with the command that opens it to the controller alone; a
  whole range stays the operator's call. The README's port table says the same.

## 2.1.0 — 2026-09-24

- **The controller adds the scout; the machine only installs it.** On the
  machine: `./install.sh` from the root of this repository — it installs
  what the machine needs, starts the scout as a service that survives logout
  and reboot, waits until it answers and prints its address and port. On the
  controller: Model servers → ＋ Add scout, the address, Connect — the
  controller hands the scout its address and fleet token and waits for the
  first heartbeat. Its ✕ on the board lets go (`POST /api/unpair`: the scout
  forgets the controller; its cells keep running).
  - Gone, with no way back: `--admin-url`, `scripts/install.sh` (it is
    `./install.sh` now) and the Pair form on the scout's page. The page only
    reads: the machine, whether a controller has paired it, and the address
    to enter.
  - A scout nobody has paired records `unpaired` instead of an error every
    minute; `/api/pairing` names the scout's port.
  - `./uninstall.sh` takes the scout off: the cells first, through the scout
    itself, then the service and the scout's files; it names what it left.
  - The installer leaves a built llama-server alone: it used to move the
    llama.cpp checkout to the latest tag while the binary stayed as it was —
    source and binary of two builds. And it finds CUDA under
    /usr/local/cuda instead of installing the toolkit a second time.

## 2.0.1 — 2026-09-24

- **The model cache is purged fully on Python 3.9** — the macOS scout's.
  The purge and the old-model cleanup deleted files while `rglob` was still
  walking the cache, and removed each emptied folder as they went; on 3.9 the
  walk then scanned a folder that was gone and raised FileNotFoundError
  halfway, leaving the rest of the cache on disk. Nothing said so: the stop
  swallows a failed purge. The walk is finished before anything is deleted
  now, and CI runs the snapshots on 3.9 as well as 3.12 — they had only ever
  run on 3.12 and newer. The README names 3.9 as the floor, which is what
  the fleet runs.

## 2.0.0 — 2026-09-24

- **Rewritten into classes, each with one job.** The scout was one class
  assembled from mixins that shared a single `self`. Now: `Machine` (the
  host's probes and their caches), `Cells` with `Cell`, `CellRecords` and
  `LlamaProbe`, `CellProcess`/`CellLog`/`HostProcesses`, the starts
  (`LlamaStart` + `LlamaLaunch`, `CommandStart`, `CellArtifacts`),
  `LlamaBuilds`, `SavedConfigs`, `Report`, `Heartbeat`, `Api` with its route
  tables and `Power`, `PairingPage`, `CellAssets`; `Scout` only puts them
  together. The HTTP surface is the same: every stage was checked against the
  snapshot (over 800 pins, output identical line for line) and against
  mutants of the moved code. `scripts/check_oop.py` keeps the shape — no
  mixins, no modules of loose functions beyond three named exceptions, no
  abstract method left unimplemented — with a self-test that plants each
  breakage, in CI.
- **The report's shape is one sample for both sides.**
  `docs/report-sample.json` is the heartbeat and `/api/state` of an imagined
  machine, built from the real report code; the scout's tests check it still
  produces it, and the controller keeps a byte-identical copy and checks it
  reads every field.
- **The scout knows its machine, and nothing about agents.** It reported the
  AI agents on its host — from a static list, a fleet registry, docker and
  libvirt — read their OpenClaw configs, and applied routes the controller
  sent it, re-pointing each agent at a proxy port. The controller stopped
  reading all of that: its clients are records the operator makes by hand,
  and a client needs nothing installed.
  - Gone: the agent list and the fleet registry (`agents`, `registryUrl`),
    `openclaw.py`, `apply-routes.py` and `applyCommand`,
    `GET /api/agent-config` and `POST /api/routing/apply` (both answer 404
    now), the report's `agents`, `candidates`, `assignments` and
    `applyStatus`, the docker/libvirt/runtime probes, and the pairing page's
    "Local agents detected" row.
  - A state.json written by 1.x drops its `assignments` and `applyStatus`
    once at start, with a line in the log.
- A model download retries again when the controller does not answer. The
  retry reported its progress without the cell's port, so it raised
  TypeError on the first blip: the promised waits of 5, 15 and 30 s never
  came, and the board showed the TypeError.
- A cell with a draft model and no mmproj starts. The draft's path was read
  from a list position that only exists with an mmproj: both files
  downloaded, then "list index out of range" and no process.
- The pairing page works on a scout that has a fleet token. It read
  `/api/state`, which the token closes, so the page for pasting the token
  stayed blank. It reads `GET /api/pairing` now: open, and only what the page
  shows — no controller reply. Its hints name the controller's port 7990.
- A pairing address with no host is refused. `http://` lost its trailing
  slash first, became `http:`, gained a second `http://` and was saved as the
  host "http".
- The heartbeat carries the llama.cpp update status and the scout's own
  version, both under the names `/api/state` uses (`llamaUpdate`,
  `scoutVersion`; the report's `version` became `scoutVersion`). The
  controller keeps whichever report arrived last, so a field only one of
  them carried was erased by the other every minute — "building…" blinked
  on the board.
- `install.sh` finishes. It sourced `install-whisper.sh` — a script of its
  own — and then called `install_whisper`, which never existed: on a host
  without an NVIDIA GPU the helper's `exit 0` ended the installer before its
  summary, and on a GPU host the missing function failed the install at its
  last step. The helper runs as its own process now, and a test reads the
  installer for the rule. Its hints name the controller's port, 7990.
- **A new fleet token is taken from the pairing page.** After the token was
  regenerated on the controller, a paired scout still held the old one, and
  the page — the way to hand it the new one — checked the new token against
  the old and refused; the way back was editing config.json on the machine.
  A pairing with the same controller address now takes a token the scout does
  not hold once that controller accepts a heartbeat carrying it. Pointing the
  scout at another controller still needs the token it holds.
- The whole surface is pinned by value in `scripts/test_scout_*.py`, with
  processes, signals and the network shut in the harness, and runs in CI.

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
