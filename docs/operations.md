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
cell in its registry (see below). It does cut the requests going through the
machine's entry (`:8092/v1`, 2.27) — a cell's own port is the way that a
restart does not touch. What it does terminate at start is a
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

Runtime files (never in git): `state.json` (heartbeat status, the `cells`
registry and the pinned `hostId`; a 1.x state's `assignments`/`applyStatus`
are dropped once at start), `llama-node-configs/`, `var/server-cells/<port>/`, the model cache
(`~/llama-model-cache` by default).

Files that hold a secret are 0600 (2.20): `config.json` (the fleet token)
and `state.json` (the cells' start requests and launches). Before 2.20 the
machine's umask made them 0664; since 2.20.1 the scout narrows them to 0600
when it starts (config.json is otherwise written only when the scout is
paired).

## The NVIDIA card through kernel updates (Secure Boot)

Under Secure Boot the card's kernel module loads only when it is signed by a
key the firmware trusts — Canonical's, shipped per kernel as
`linux-modules-nvidia-<series>[-open]-<kernel>`. A DKMS build is signed by the
machine's own key, never enrolled, and the kernel refuses it. The automatic
updates have installed a kernel without those modules three times, and the
next boot came up without the card: a driver update that dropped its signed
companion, a modules package whose driver was in a pocket the updates were not
allowed, and — 2026-10-09 — a move of the driver from series 610 to 615 by
renamed packages: unattended-upgrade read the conffile the new
`nvidia-kernel-common` takes over from the old one as a prompt and skipped the
series, but not the kernel it installed in the same run.

`scripts/install-nvidia-kernel-guard.sh` (as root) puts a guard in the way:

- `Dpkg::Options --force-confdef --force-confold` for apt
  (`/etc/apt/apt.conf.d/53unattended-upgrades-dpkg-options`), so
  unattended-upgrade does not skip a package for a conffile prompt —
  `dpkg_conffile_prompt()` in `/usr/bin/unattended-upgrade` — and finishes a
  driver series move by itself;
- `nvidia-kernel-guard modules` after the automatic updates (an
  `ExecStartPost` of `apt-daily-upgrade.service`): every installed kernel gets
  the signed modules of the machine's driver series when apt offers them;
- `nvidia-kernel-guard boot` after every dpkg run (an APT `DPkg::Post-Invoke`
  hook): GRUB (`GRUB_DEFAULT=saved`) boots the newest kernel that has the
  module of that series — when the newest has none, the newest older one that
  has. A module of another series counts as none: under the new series'
  libraries it is a "Driver/library version mismatch".

`nvidia-kernel-guard status` (as root, it reads grub.cfg) says what the two
would do. `--uninstall` takes it all away and puts `/etc/default/grub` back.
Its messages are in the journal: `journalctl -t nvidia-kernel-guard`.

## Known quirks

- **A renamed machine stays the same host (2.10+).** The id was the
  hostname at every start, so `hostnamectl set-hostname` turned a machine
  into a new host, and its cells, schedules and power buttons stayed with
  the old one. The first run of 2.10 pins the id the scout reports then in
  `state.json` (`[identity] host id '…' pinned` in the log); from then on a
  rename changes only the name the board shows, at the next report. A
  `hostId` in `config.json` still wins and becomes the pin. Deleting
  `state.json` forgets the pin (and the cell registry). Not
  `/etc/machine-id`: clones of one VM image share it, and macOS has none.

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
  corrupt GGUF / mmproj mismatch) into the heartbeat, so the board shows it,
  and since 2.6 the last 8 lines of the crashed run's log too (on hover).
  Both leave the machine with keys scrubbed out: a value with a key's prefix
  (`lcv1_`, `sk-`, `hf_`, `ghp_`, `glpat-`), a `Bearer` value, whatever
  follows a name like `api_key` / `password` / `secret`, and a long value
  after `token` — `EOS token = 151645` stays as llama.cpp wrote it.
- **`sudo -n ufw allow <port>`** on cell start is best-effort: without
  passwordless sudo the port silently stays closed to the LAN.
- **`cacheModels=false` (default)**: models re-download on every start and are
  purged on stop — files and model folders, a command cell's too (2.31; it kept
  them always); with caching on they stay, and a model is written to the disk
  once. An engine starts only from a file on this machine's disk, so without a
  cache every start writes the whole model again. A download under way is
  never purged. With caching on, the cache keeps **the last N models that ran**
  (2.34; N is 1 to 5, 1 until the owner sets it on the machine's page, kept in
  `state.json` as `cacheKeep`) with all their files — the parts of a split GGUF,
  the projector, the draft layer, a model's folder — and deletes the rest of what
  this scout downloaded after a start that reached a running process. What a
  cell that starts or runs holds stays whatever its age; nothing is deleted
  while no model is known to have run; a file this scout did not download (it
  was copied in by hand) is never deleted by itself, only counted and shown, and
  deleted when the operator asks with the figure he was shown (409 when it
  changed since). `GET /api/llama-node/cache` says the setting and the figures,
  `POST .../cache/keep` sets N and trims at once, `POST .../cache/delete-foreign`
  deletes the files the scout did not download. This replaces `cleanOldModels`
  (2.33 and before, off by default and unused by the board): a `config.json` that
  still carries it is read and ignored.
- **A command cell starts in the background (2.31)** and answers `starting` at
  once, as a llama cell does: its files arrive while the board shows the
  `downloading` phase. A stop before or during the download cancels the start.
  It reads the files the controller names (`modelFiles`); the first part of a
  split GGUF brings the other parts, and a running cell holds all of them.
  `CARAVAN_CELL_SCRATCH` names its scratch folder (`<cache>/.cells/<port>`):
  kept with the cell's models; emptied at the start and removed at the stop
  when they are not kept.
- **A crashed cell comes back (2.5+).** A cell that dies without being
  stopped — a non-zero exit, or gone while adopted — is launched again the same
  way 10 s later (the launch its record keeps, also after a scout restart), at
  most 3 times in 10 minutes; then it stays down and its error says the
  watchdog gave up and why. So does a cell that never gets ready (2.34.1): a
  start that dies before its port ever listens, the fourth time in a row — a
  load that ends in a kill after minutes never fills the 10-minute window, and
  the cell was restarted without end (2026-10-18); a run that did listen
  resets that count, and a machine that will not say what listens leaves the
  rule off. A clean exit (code 0) is not a crash; a process
  killed by a signal says which (`died of SIGSEGV`, 2.6+); a SIGKILL — which
  the process cannot catch and its log cannot explain — says so first, and that
  it is usually the machine's memory killer (systemd-oomd or the kernel's),
  with the log's last words after it (2.34.1). The crash
  note (`crash` on the cell: count since the last start by hand, time, reason,
  and the last lines of the log, read when the crash is seen — the relaunch
  moves that log aside) is the 💥 on the board; a start by hand clears it. A cell launched by a scout
  older than 2.5 has no launch kept and is reported, not restarted.
- **A cell the scout lost track of is held again (2.32+).** Every cell the scout starts carries
  `CARAVAN_SCOUT_CELL=<port>` in its environment, and the mark stays through every `exec` of a launcher and every
  child (a server and the engine under it carry it both). On a start the scout finds its cells again by it: the
  recorded pid with the mark, or — when the pid was clobbered — the process that carries the mark of the port; and
  a cell that does not listen yet (a model loads for a minute and more) is kept, not forgotten for its silent
  port. While the scout runs, a process that carries the mark of a port no cell of the scout holds is held again
  after two passes (every 15 s, `[reconcile]` in the journal). A start for such a port holds the cell and answers that
  a server already runs there, instead of starting a second copy beside it; a port held by a process no scout
  started is refused with its pid and command line. Stop reaches a cell by its mark too, the engine under a server
  with it, and a stop that left something of the cell carrying its mark ends it (`stopped, but … still carry its
  mark`). A restarted cell is never relaunched while another process of the scout's still runs its port.
  (2026-10-14: systemd-oomd killed the scout in the middle of Strata's 37 GB load; the new scout forgot the
  loading cell, the board could neither stop it nor start another, and a second start loaded the model again.)
- **A start the memory cannot hold is refused (2.32+).** A command cell that keeps its weights in RAM (Strata keeps every
  expert there) says how much with its start (`ram`: `reserveMiB`, `who`, `why`). The scout refuses when what the kernel
  says is available, less 2 GiB for the system, does not cover it, or when what the cells up or still loading
  promised, and this one, pass 90 % of the machine's memory — the second is what sees a cell that is still loading,
  whose memory has not come yet. The answer names the numbers and the cells to stop. No `ram`, or no `/proc/meminfo`:
  no check.
- **A fresh build that crashes (2.6+).** With the llama-server binary younger
  than 6 hours, 3 engine crashes in 15 minutes (the watchdog's crash words
  say `CUDA error`, `GGML_ABORT`, `SIGSEGV`, `SIGABRT` or `Aborted (core
  dumped)` — the words the controller reads in its own journal) raise an
  incident: `llamaSuspect` in both reports, and the board's banner offers the
  newest archived build of another commit. It is kept in `state.json` under
  the build (commit and binary time) until dismissed for that build or the
  build changes. Nothing is restored without the operator.
- **Charts a second apart (2.8+).** The scout samples its cards (nvidia-smi)
  and processor every second while a board watches — the controller asks
  `/api/telemetry?since=` about once a second while one is open — and every ten
  seconds otherwise; ten minutes are kept, so an opened board has them. The
  processor share is measured from /proc/stat as the controller measures its
  own; on macOS the one-minute load average stands in.
- **A cell that runs but does not listen yet (2.7+).** vLLM installs its venv
  and loads for minutes before its port opens; from the controller a silent
  port looks like a firewall. The scout asks its own OS which ports listen
  (`ss`, `lsof` on macOS, once per 2 s for all cells) and says `listening` per
  running cell; one that does not listen yet also says its last log lines
  (`startingTail`), and the board shows it starting, with where the start is.
- **A vLLM cell's queue and speed (2.7+).** vLLM 0.24 (engine V1) exports
  no rates, only token counters; the scout reads its /metrics like a
  llama-server's and reports the counters' growth per second between two
  readings as `promptTps` / `genTps` (aggregate throughput, what vLLM's old
  gauges said), with `requestsProcessing` and `requestsWaiting`. A first
  reading, a counter seen for the first time, or a restarted server (a
  counter that went down) gives no rate rather than a made-up one.
- **A Strata cell's speed, queue and context use (2.35.1+).** Strata answers `/metrics` with the JSON of its Monitor tab unless the
  reader asks for Prometheus text (`Accept: text/plain`, the way Prometheus asks): the scout asks so, and reads vLLM's names and Strata's
  own (`strata:`). `promptTps` and `genTps` are the running request's own speeds while it reads or generates, and while it idles the last
  measured ones — tokens over compute seconds of the work done since the last scrape, as llama.cpp's are; at the first scrape, the
  server's accumulated average. The prompt tokens a Strata server's cache already held are not counted as read ones. `requestsProcessing`
  and `requestsWaiting` are its queue, and `ctxUsed` the running request's share of the context. A server that does not say its compute
  seconds, or a restarted one (a counter that went down), gives no rate rather than a made-up one.
- **A start the card cannot hold is refused (2.7+).** vLLM reserves
  utilization × the card when it starts and otherwise dies in a crash loop a
  minute later. The controller sends what a start reserves (`vram`); the scout
  reads its free memory at launch — at boot too, where two autostart cells may
  want one card — and refuses with the numbers and what holds THAT card (2.33+):
  the cells with a process on it, with what each holds ("`:22024 (21.5 GiB)`"; a
  cell whose server handed the card to a child counts), and any other process by
  its executable ("`ollama (6.2 GiB)`"); under 256 MiB (a driver's or a display's
  context) nobody is named, and a cell of the other card never is. A card whose
  whole memory is less than the reserve is said to be too small ("`GPU 0 has
  only 24.0 GiB in all`") — stopping cells will not help — and another card with
  the room is named ("`or pick GPU 1 (29.3 GiB free)`"). Before 2.33 the refusal
  named every running cell of the machine, on a machine with two cards the ones
  on the other card too. No nvidia-smi, or no such card: no check, as on the
  controller; nvidia-smi that does not say who holds what: nobody is named.
- **Memory limits (2.6+).** On Linux a cell is launched in its own systemd
  user scope with the limits the controller's cells had in their
  `lama-cell@.service` (the unit went in the controller's step 6.9; the scout
  is these values' one home): `MemoryHigh=70%`, `MemoryMax=80%`,
  `MemorySwapMax=2G` of this machine's RAM.
  A model that eats the memory is slowed and then killed alone, not the
  machine with the scout on it. The scout asks once per run, launching a scope
  and reading its `memory.max`; the answer is one `[cells] …` line in the
  scout's log. Where there is no user systemd (the scout started by hand from
  an ssh session), no memory controller for it, or on macOS, cells run without
  limits and that line says why. A cell already running keeps what it was
  launched with until its next start.
- **Autostart (2.4+).** A cell with ↟ on the board starts when the machine
  boots: the scout keeps the start request the controller sent (refreshed on
  every start and when the cell's settings are saved) and starts those cells on
  its first start of a boot — `state.json` keeps the boot id (`autostartBoot`),
  so a scout restart or update in the same boot starts nothing, as systemd's
  `enable` does. A machine that will not say which boot it is (no
  `/proc/sys/kernel/random/boot_id`, no `sysctl kern.boottime`) gets no
  autostart, and the log says so. Both reports carry the kept requests
  (`autostartRequests`, 2.26): a controller that did not start a cell — one
  moved to another machine — keeps a record of it from them, and the cell
  stays on its board when it stops.
- **Models are read in place when this machine has them.** The controller
  says where it reads each model file (`inPlace` in the start request). The
  scout on the controller's own machine, or one that mounts a library at the
  same path, reads the file there — the same size, or it is another file —
  instead of copying it into its cache; a look at a path that does not answer
  in 3 s (a dead NFS mount) counts as absent. A library's file that this
  machine lacks is refused with what to do. A command cell gets
  `LLAMA_MODELS_DIR` pointing where its model really is — in place or in the
  cache; a value set in the cell's config wins.
- **A folder model comes through the controller too (2.30).** A checkpoint, a
  whisper cache, seamless: the scout asks the controller for the folder's
  listing (`GET /api/models/folder`) and takes each file the way it takes a
  model file, then makes the folder's links again. The files gather in
  `<folder>.part`; the folder takes its name when all of them are in, with
  `.caravan-folder.json` written last. A fetch cut short is resumed: a file
  already of its size is kept. A folder the scout did not download is never
  replaced. No machine with a scout needs rights to the share the models live on.
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
