# Changelog

## 2.35.1 — 2026-10-18

- **A Strata cell says its token speed, its queue and its context use.** The owner, 2026-10-18, after talking to a local model through a
  chat page: "some characteristics I do not see — tokens per second are zero". The model ran in a Strata cell, and the machine's
  card said "prompt 0 / gen 0 t/s". The scout read a cell's `/metrics` as Prometheus text. Strata answers Prometheus text, in vLLM's
  metric names and its own (`strata:`), only to a reader that asks for it (`Accept: text/plain`); asked without, it answers the JSON of its
  Monitor tab, in which the reader found no sample: the cell said no speed (`promptTps`, `genTps`), no queue (`requestsProcessing`,
  `requestsWaiting`), no context use (`ctxUsed`).
  (1) The scout asks for `text/plain`, as Prometheus does. llama.cpp and vLLM answer the same either way.
  (2) A scrape with a `strata:` sample is a Strata scrape. Its speeds are the running request's own while it reads or generates
  (`strata:live_prefill_tok_s_mean`, `strata:live_tok_s`), and while it idles the last measured ones: tokens over compute seconds of the
  work done since the last scrape (`timed_rates`, the same rule as llama.cpp's), the accumulated average at the first scrape. The prompt
  tokens its cache already held (`vllm:prefix_cache_hits_total`) are not counted as read ones: of 86650 prompt tokens 38423 were held, so
  48227 were read in 18.053 s, 2671 per second, not 4800. A sample the server does not say makes no rate, and a counter that went down
  (a restarted cell) drops the old ones. The growth of vLLM's counters between two scrapes — zero between two requests and a spike at
  the one that finished — is not how a Strata cell is read; a vLLM server (no `strata:` sample) is read as before.
  (3) `requestsProcessing`, `requestsWaiting` come from vLLM's names, and `ctxUsed` from `vllm:kv_cache_usage_perc`, which Strata gives as the
  running request's share of its context (the context window is `/props`'s `n_ctx`; a vLLM server has no `/props`, so its pool usage
  is never taken for a context share).
  (4) A test that could not fail. `test_scout_metrics.py` ended with `CHECKS.finish()` as a statement: the verdict `finish()` returns was
  thrown away, so the script printed its FAIL lines and exited 0, and the CI went on green. It had done so since 2.23.2; a run of
  mutants of this release found it — fourteen of seventeen alive, each with the test showing FAIL and the exit code 0. It ends with
  `sys.exit(CHECKS.finish())` now, and `scripts/check_test_verdicts.py` (a CI step of its own, which tries itself on the broken ending first)
  names every test script that ends the old way.
  Checked: `test_scout_metrics.py` (27 checks, 12 new: the cell is asked with `Accept: text/plain`, the first scrape's averages with the cached prompt
  tokens left out, a request at work, the new work only, held while idle, the queue, a restart, a cache that holds more than was asked, no
  seconds, the JSON of the Monitor tab, vLLM unchanged); `test_scout_cells.py`, `test_scout_report.py` (their stand-ins take a request as well as an address); `check_test_verdicts.py` (it names the one broken ending before the fix and passes
  30 scripts after); the whole suite on 3.12 and 3.9; 16 mutants in tree copies, all caught (the first run left 14 alive: see (4)). Not checked: on the
  real machine until it is deployed (the server's text was read there by hand: its names are the ones the test uses).

## 2.35.0 — 2026-10-18

- **A running cell says how much memory it holds in RAM: `ramBytes` on every cell of `llamaNodes` and `llamaNode`.** The owner,
  2026-10-18, looking at the thin rows of a machine's cells, where only the video memory a cell holds was written: "for each model show
  two badges, how much it holds in RAM and how much on the GPU, even if one of them is not occupied". The video memory has been
  the controller's to work out, from the compute apps by pid. The RAM had no source at all: the cell's report said nothing of it.
  Now `CellRegistry.views()` takes one table of the machine's processes (`Machine.processes()`, the `ps -A` an engine's `ramBytes`
  already comes from), and a running cell's `ramBytes` is the RSS of its process and of everything it started, in bytes, read by
  parent pid (`HostProcesses.resident`), so it works on macOS too, where `/proc` does not. One `ps` serves every cell, and none
  is asked when no cell runs. It is `null`, not `0`, when ps did not answer or the table does not hold the cell's process: "not
  told" is not "nothing", and the card draws it as a dash. A cell that is not running has no `ramBytes`.
  RSS counts the memory-mapped model files a process has touched: the weights of a CPU cell are in it, and a file that two
  cells map is in both. It is what `top` shows as RES, not the cell's own cost. The controller's row says so in the badge's tip.
  Checked: `test_scout_cells.py` (402 checks, 12 new: the sum over a process tree, another cell's tree left out, the cut at
  `TREE_LIMIT`, a table with a cycle, rows with no RSS, no table / empty / unknown pid / not a number, the view of a running cell
  with the number and without it, a lone call taking its own table, one `ps` for all cells and none when nothing runs), `report_sample.py` (the sample shows both
  `ramBytes: 2147483648` and `null`), `check_oop.py`, the rest of the suite on 3.12 and 3.9; 12 mutants in tree copies, all caught.
  Not checked: on a real machine (2.35.0 is not deployed); on macOS, where only the parent-pid walk can work.

## 2.34.1 — 2026-10-18

- **A cell killed from outside says so, and a cell that never gets ready is not restarted for ever.** A Strata cell on the owner's
  machine (Swift IQ3_S, 2026-10-18) was killed four times by `systemd-oomd` — its memory scope was full and the pressure stood
  above 50 % — and restarted each time: the cause is told in the controller's 1.3.574. Two things of the scout's made it worse:
  (1) The card said `still starting (461 s) - please wait ...`. A process that is killed by SIGKILL cannot say why: its log's last
  line is merely the last thing it said, and here that was the launcher's "still alive" line. The crash note's reason now begins
  with the signal and what it usually is (`died of SIGKILL (killed from outside — on a machine short of memory that is
  systemd-oomd or the kernel's OOM killer)`), the log's last words after it when there are any; a SIGABRT or a SIGSEGV, which a
  process raises itself and explains on its way out, and every exit code keep the log's line. And a line `[strata] still starting
  (N s) …` is no longer read as a cause (`CellLog.PROGRESS_NOISE`).
  (2) The watchdog gives up after 3 restarts in 10 minutes. A start that dies after 4 to 8 minutes never fills that window, so the
  loop never ended — until the owner pressed Stop. Now the starts that died before their port ever listened are counted on their
  own (the watchdog notes it each tick, `Cell.listened`): after 3 restarts that did not take, the next death is the last, whatever
  the clock says, and the card says `never ready in 4 starts — not restarting it: …`. A run that did listen resets the count; a
  machine that will not say what listens (`listening_ports()` is None) leaves the rule off.
  Checked: `test_scout_watchdog.py` (50 checks: a SIGKILL with a log line, with none, with 400 characters of one; SIGABRT/SIGSEGV,
  an exit code and an unknown code unchanged; four unready runs of five minutes give up with the right words; a ready run resets the
  count; no listening data — no give-up; the window rule and its words unchanged), `test_scout_node.py` (181: a log of progress
  lines names no cause, a real cause before them stays, another `[strata]` line and a "still starting" without seconds are read as
  before); 21 mutants in tree copies, all caught.
  Not checked: on the machine that had the loop (the controller's 1.3.574 makes the start refuse in the first place).

## 2.34.0 — 2026-10-18

- **The model cache keeps the last N models that ran, with all their files, and deletes the rest by itself.** The owner, 2026-10-18,
  when the disk of a machine was 94 % full of model copies and a start died with `No space left on device`: "keep in the cache only
  the last models, from 1 to 5, 1 by default, with all their files; show the setting somewhere noticeable; everything older is
  deleted automatically". Until now a model stayed in the cache until someone asked for a purge: every cell that ran was copied
  from the library to the disk once, and none of the copies ever left (`cleanOldModels`, the one knob, was off and never used by
  the board).
  The scout now records each start (`modelUse` in `state.json`: the files the start names — the model, its projector, its
  draft layer —, the port, the time) before the files are copied, and marks it once its server runs. After a start that
  reached a running process, when the setting is changed, and once at the boot after the cells that start with the machine
  have asked for their files, the cache is trimmed (`CacheKeeper.trim`): what stays is the files of the `keep` models that ran
  most recently (all parts of a split GGUF, the projector, the draft layer, a model's folder), of every cell that is starting
  or running whatever its age, and of every running process; the rest of what this scout downloaded goes. The setting
  (`cacheKeep`, 1 to 5, 1 until set) lives in `state.json` — `config.json` stays the file the operator wrote.
  Two things are never done by guess: nothing is deleted while the scout knows no model that ran (the answer says
  `waiting: true`, as the first start after the update begins the log: "unknown" is not "nothing"), and a file this scout did not
  download — copied in by hand, left from before the ledger — is never deleted by itself: it is counted and shown
  (`modelCache.foreign`: files, bytes, the folders they lie in) and deleted only when the operator asks with the size he was
  shown (`POST /api/llama-node/cache/delete-foreign {expectBytes}`; 409 and nothing deleted when it changed since, 400 without).
  A model that was started and died while loading is not "the last model": its copy goes with the next trim, and a retry
  copies it again.
  New: `GET /api/llama-node/cache` (the setting, what the scout holds, what it did not download, the disk), `POST
  /api/llama-node/cache/keep {keep}` (sets and trims at once), `POST /api/llama-node/cache/delete-foreign`, and `modelCache` in
  both reports (`/api/state` and the heartbeat), so the board shows the figures without asking. Retired: `cleanOldModels` and
  `ModelFetcher.cleanup_old` (a `config.json` that still carries the flag is read and ignored).
  Checked: `test_scout_cache_keeper.py` (89 checks: the setting and what it refuses, the log of starts, the trim in every case — waiting,
  keep 1/2/5, split parts, a folder model, a running process's file, a starting cell, a download in flight, a failed start —, the foreign files and
  their deletion by figure, the figures and their reuse, the routes and their closing under a fleet token), `test_scout_cells.py` (the
  worker's trim rewritten for the keeper; llama and command starts are recorded and committed, only with caching on), `test_scout_report.py`
  (the field and its figures), `test_scout_autostart.py` (the trim comes after the cells), `test_scout_openapi.py` (45 operations),
  `docs/report-sample.json` regenerated, 36 mutants in tree copies, all caught.
  Not checked: on a real machine with real models (the first start after the update only begins the log; nothing is deleted before a
  model has run); the board's control for the setting (controller 1.3.576).

## 2.33.2 — 2026-10-17

- **A library's footer is not the reason a cell crashed, and a wrapper that says "see root cause above" is not either.**
  pydantic ends every validation error with a pointer to its docs, `For further information visit https://errors.pydantic.dev/…`,
  and that line's `error` (it is in the address) took the place of the cause the line above it states. Two vLLM cells died
  on the owner's board with the card saying `crashed 4 times in 10 minutes — not restarting it: For further information
  visit https://errors.pydantic.dev/2.13/v/value_error` (2026-10-17): the real causes were `Please pass the argument
  trust_remote_code=True to allow custom code to be run` and `Unrecognized model in … Should have a model_type key in its
  config.json`, one line higher. The footer is now left out of the reading, as the debugger's lines are (2.33.1), with or
  without the `(APIServer pid=…)` prefix and any indent; a different `For further information …` line of a program, or the
  address alone, stays what it was. A log of nothing but the footer names no reason.
  A third vLLM cell ended with `RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}`
  — and the cause stood a screen higher (`torch._inductor.exc.InductorError: AssertionError: auto_functionalized was not
  removed`). A log without levels (a Python process: vLLM, whisper, a command cell) is now read by its last EXCEPTION line
  (`…Error: …`, `…Exception: …`, with or without the process prefix and a dotted name) — except a line that says where
  the cause stands: a wrapper that says it is above (`see root cause above`, so the line before it is the cause) and a
  pydantic header that says it is below (`1 validation error for ModelConfig`, so the lines under it are read). A log with
  levels (llama.cpp) is read as before.
  Checked: `test_scout_node.py` (177 checks: the cause one line above the footer, the line that says what to do, the footer
  alone, a footer with indent and no prefix, a different phrase and the bare address; the engine-core wrapper, a chain of
  exceptions, `Exception` without `Error`, a wrapper with nothing above it, a levelled log with a stray exception line —
  negatives), 17 mutants, all caught.

## 2.33.1 — 2026-10-17

- **The reason a cell crashed is the cause, not the debugger's last line.** llama.cpp's crash handler runs gdb, and gdb writes
  into the log after the server's own message: frames (`#3  0x… in ggml_abort () from …`), a warning about a source file
  it cannot find (`warning: 360	../csu/libc-start.c: No such file or directory.`), the detach lines. The scout took the
  reason from the log by patterns, and `no such file` is a priority pattern (a missing model is the most actionable
  reason there is): so a cell that died with `CUDA error: no kernel image is available for execution on the device`
  showed on the board `crashed 4 times in 10 minutes — not restarting it: warning: 360 ../csu/libc-start.c: No such file
  or directory` (2026-10-17), and a frame's `abort` would have won the same way. The debugger's lines are now left out
  of the reading (a frame by its shape, gdb's `warning: <line number>` about a source, `[Inferior … detached]`,
  `[New LWP …]`, `[Thread debugging …]`), and `no kernel image` is a priority pattern of its own: the engine has no
  code for the card it runs on, and the fix — a build for that card — is the only thing the message says. A real
  `No such file or directory` from the program's own line stays the reason, and a log of nothing but the debugger's
  lines names no reason (it used to name the last line, whatever it was).
  **Checked:** `scripts/test_scout_node.py` (164 checks: the real log of the crash — the CUDA error, the frames, the libc
  warning, the detach line — names the CUDA error; an indented frame is noise too; a program's own `No such file` and
  `warning: … failed` stay reasons; a log of only noise names none; `no kernel image` wins over a later generic error).
  Mutants: 9 (each part of the noise pattern, the window, the fallback, the new priority pattern), all caught.
  **Not checked:** on the live machine: 2.33.1 is not deployed yet.

## 2.33.0 — 2026-10-17

- **The refusal of a start the card cannot hold names what holds THAT card, and says when the card is too small.** The
  controller sends a start's VRAM reserve with the card it is for (`vram`), and the scout refuses a start that finds less
  free there. The words named every running cell of the machine as the ones to stop. On a machine with an RTX 3090 and an
  RTX 5090 (2026-10-17) a 31 GB server pinned to the 24 GB card was told to stop cells that held nothing on it, and nothing
  said that stopping them would not help: the card was smaller than the reserve. Now the refusal reads who holds memory on
  the card from `nvidia-smi` (the processes with their memory) and names, biggest first, the cells with a process on it —
  also through a child, as Strata's engine is a child of its server — and any other process by its executable
  (`stop :22024 (21.5 GiB), :22010 (5.9 GiB) or lower GPU_MEMORY_UTILIZATION`, `stop ollama (6.2 GiB) …`). A holder under
  256 MiB (a driver's or a display's context) is not named, the cell being started is not named, a cell of the other
  card never is. A reserve bigger than the card's whole memory gets another sentence — `GPU 0 has only 24.0 GiB in all —
  lower GPU_MEMORY_UTILIZATION` — because stopping cells cannot make the card bigger. Either way the words name another card
  with the room when there is one, the one with the most: `or pick GPU 1 (29.3 GiB free)`. `nvidia-smi` that does not say
  who holds what names nobody, where it used to name every cell.
  **Checked:** `scripts/test_scout_vram.py` (names only that card's holders with their memory, through a child process,
  by executable for a process that is no cell, nothing under 256 MiB, not the cell being started, not the cell of the other
  card; a card too small, with and without another card to pick, the most roomy of three, the reserve equal to the
  whole card is not "too small", one card, the second card as the one asked for, no apps data names nobody).
  Mutants: 26 (who is named, through a child, summed, the biggest first, a stopped cell, the cell being started, the 256 MiB line, a card too small and its edge, another card with the room and its edge, the words), all caught.
  **Not checked:** on the live machine: 2.33.0 is not deployed yet.

## 2.32.0 — 2026-10-14

- **A cell the scout started is found again, also while it loads.** The scout's start-up adoption knew a cell by the
  command line its record remembered, then by the port it serves — and forgot a cell whose process lived but whose
  port was silent. A model that loads (Strata: 37 GB into RAM, a minute and more) does not listen yet, and the
  record's marker for such a cell is the controller's command line, `$HOME` and quotes in it, which no `exec`-chained
  launcher's argv can match. On 2026-10-14 systemd-oomd killed the scout in the middle of such a load; the new scout found
  the pid alive, the marker gone and the port silent — and dropped the cell. It went on loading outside the registry:
  the board saw no cell, Stop was off, Start ran a second copy beside it (the first held the card), and the machine's
  memory went to two loads. Now a cell is known first by the mark every cell carries in its environment
  (`CARAVAN_SCOUT_CELL=<port>`, kept through every exec and every child): the recorded pid with the mark, or, when the
  pid was clobbered, the process that carries the mark of the port; a silent port with such a process is adopted while
  it loads. Only a port nothing runs is forgotten, and the journal says so.
- **Reconcile.** While the scout runs, a process that carries the mark of a port no cell of the scout holds is held again,
  after two passes in a row (every 15 s; `[reconcile]` in the journal). A cell with no record gets one from what the
  machine says of its process — its command line, its start time, the log it was given by name.
- **A start does not run a second copy.** A start for a port whose cell this scout lost track of holds that cell and
  answers that a server already runs there; a port held by a process no scout started is refused with its pid and
  command line, before the cell loads its model and fails to bind. The watchdog does not relaunch a crashed cell while
  another process of the scout's still runs its port.
- **Stop reaches a cell by its mark.** A cell no registry held was "an unrecognized process" however plainly it was a
  scout's: Strata's record carried a marker no argv could match. Now what carries the mark of the port is the cell's —
  listening or loading, with the engine under its server — and goes; and a stop that ended with something of the cell
  still carrying its mark ends that too (an engine that holds 31 GB of the card when its server's SIGTERM does not reach it).
- **A start the memory cannot hold is refused.** A command cell that keeps its weights in RAM says so with its start
  (`ram`: `reserveMiB`, `who`, `why`; lama-caravan 1.3.545 sends it for Strata). The scout refuses when what the kernel says
  is available, less 2 GiB for the system, does not cover it, or when what the cells up or still loading promised, and
  this cell, pass 90 % of the machine's memory — the second sees a cell still loading, whose memory has not come yet.
  `MemoryLedger`; the promise is kept in the cell's record, so a restart still knows it.
- **Tests:** `test_scout_lost_cell.py` — 54 checks: the questions to the OS (`port_of`, `tagged_cells`, `carrier`,
  `started_at`) over a /proc of the pin's own; the incident replayed (a record whose marker cannot match, nothing
  listening, the mark on the server and the engine: adopted, not forgotten); the clobbered pid; what is really gone;
  Reconcile (two passes, a process that vanishes between them, a cell under way, a failing pass); a start that finds its cell
  and one that meets a stranger on its port; the ledger, including the second cell started while the first loads; stop by
  the mark, the sweep, the watchdog. `test_scout_cells.py`: the port is asked of the host before the firewall opens
  (3 pins). Docs: `docs/architecture.md`, `docs/operations.md`, `docs/http-api.md` (`ram`).

## 2.31.0 — 2026-10-13

- **A command cell keeps its models as the controller asks.** The scout
  forced `cacheModels` on for every command cell — "nothing it runs is
  downloadable" stopped being true with the transcribe runner. Now it does what
  the controller asks, as a llama cell does: kept, a model is written to the
  disk once; not kept, every start downloads it and every stop deletes it — a
  scout starts an engine only from its own disk, so "no cache" writes the
  whole model again at every start (the owner keeps them for the disks' sake,
  2026-10-13). Without a cache the stop's purge takes model folders too (2.30
  fetched them and never let them go), and what is being downloaded right now
  is never purged: a stop on one port deleted the `.tmp` a start on another
  was still filling.
- **A command cell starts in the background.** Its start ran on the request,
  download included, while the controller waits 10 s for the answer — a model
  of any size answered "timed out" while the scout went on downloading it. It
  answers `starting` at once now, as a llama cell does, and reports its phases;
  a stop before or during the download cancels the start (no process, no
  record). The firewall opens only after the request is accepted.
- **The files a cell reads, and all their parts.** The controller names the
  files a command cell reads (`modelFiles`, lama-caravan 1.3.512): Strata reads
  its weights and its draft layer. The first part of a split GGUF brings the
  others — the scout fetched only the first, and the engine stopped at the
  second: Strata's model is 68 GB in two parts. A running cell holds every
  part, so a neighbour's purge leaves them alone.
- **A scratch folder per cell.** `CARAVAN_CELL_SCRATCH` names a folder under
  the cache for what a cell makes from its model (Strata builds its pack
  there). It lives as the cell's models live: kept with them, so a start of the
  same model uses what the last one made instead of writing 2.3 GB again;
  emptied at the start and removed at the stop when they are not kept.
- **A cell's child processes (`pids`).** A running cell's view names the
  processes its server started, and theirs (`/proc/<pid>/task/*/children`).
  Strata's server runs its engine as a child, and the card's 31 GB were the
  child's: the board found no memory for the cell and called it a CPU cell.
  The controller binds the card by these too (lama-caravan 1.3.512).

## 2.30.0 — 2026-10-12

- **A folder model comes through the controller.** A checkpoint, a whisper
  cache, seamless, a robot policy: a folder model this machine lacked was
  refused ("a folder cannot be downloaded"), so it ran only where the folder
  already sat. Now the scout asks the controller for the folder's listing
  (`GET /api/models/folder`, lama-caravan 1.3.509), takes each file the way it
  takes a model file and makes the folder's links again (a Hugging Face cache:
  snapshots/ → blobs/). The scout reads the caravan's models only through the
  controller — the owner, 2026-10-12: no machine with a scout gets rights to
  the share they live on. The files gather in `<folder>.part`, the folder takes
  its name when all are in, `.caravan-folder.json` last; a fetch cut short
  resumes, and a folder the scout did not download is never replaced. A path or
  a link in the listing that leaves the folder is refused.

## 2.29.0 — 2026-10-12

- **A model's places.** Each model of the machine's entry says how many
  requests its running copies take at once (`slots` — llama-server's
  `total_slots` of /props) and how many they work on now (`busy` — /metrics,
  the count a copy is picked by), each copy its own and the model their sum:
  0 when none runs, `null` when one of them does not say (the caravan's own
  cells, vLLM). The controller queues a model's requests by them, as it queues
  a cell's by its /slots, so a busy model gives way to a pool's next row
  (step 2 of the owner's plan). The places are asked only of running copies;
  /props is read once for the window and the places (kept 30 s).

## 2.28.0 — 2026-10-12

- **The machine's models in both reports.** The heartbeat and `/api/state`
  carry `entryModels`: the machine's models exactly as `GET /v1/models` lists
  them — the name, the state, the copies. A controller names a model of this
  machine by the name the entry's clients use (step 2 of the owner's plan:
  the machine as a provider, a pool's row "machine + model"). `null` when the
  scout does not know them.

## 2.27.0 — 2026-10-12

- **The machine's entry by OpenAI's API.** The models of this machine at one
  address: `GET /v1/models` lists every cell the scout runs, starts or keeps
  a start request for, by its model's name, and a POST under `/v1` that names
  a model in `model` goes to a running cell of it — the answer relayed as it
  comes, a stream as a stream. Step 1 of the owner's plan (2026-10-12): a
  model is called by its name, not by its port.
  - The name: `MODEL_NAME` (the operator's), else `ALIAS`, else one made of
    its model, else `cell-<port>`. A start gives it at once — a model being
    loaded is found as `starting` — and the cell's record keeps it.
  - Two cells of one model are its copies: a request goes to the one with
    the fewest requests in work, equal ones in turn.
  - `model` reaches the cell in its own words when its server answers to
    other names: the scout asks a running cell's `/v1/models` once per start
    (vLLM refuses a name it does not serve).
  - Refusals in OpenAI's shape: 400 no `model` (or a body that cannot be
    read), 404 `model_not_found`, 503 `model_not_running`, 502
    `cell_unreachable`, 413 over 256 MiB. A body sent in chunks is read too.
  - Open like the cells; a cell's own port answers as before and stays the
    spare way in. The client's `Authorization` does not reach a cell.

## 2.26.0 — 2026-10-12

- **A stopped cell stays on the board of a controller that did not start it.**
  The scout drops a stopped cell from its report, and a controller shows a
  stopped cell only from a record of its own. A controller moved to another
  machine had no record of the cells the old one started, so on 2026-10-12
  every cell of a machine went off the board as it was stopped — with its
  switch, and with the kanban's cable to it. Both reports now carry
  `autostartRequests`: the start request kept for each autostart cell, by
  port — `{config, modelPath, cacheModels}`, the settings whole (an empty
  value is a flag taken off). The controller keeps a record of each cell it
  lacks from it (lama-caravan 1.3.504).

## 2.25.1 — 2026-10-10

- **The card through kernel updates under Secure Boot.** A guard for the
  machine, outside the scout: `scripts/install-nvidia-kernel-guard.sh` (as
  root). After the automatic updates every installed kernel gets the
  Canonical-signed modules of the machine's NVIDIA driver series, and GRUB
  boots the newest kernel that has them — a kernel installed without them is
  not booted. Apt gets `--force-confdef --force-confold`, so
  unattended-upgrade finishes a driver series move instead of skipping it on
  a false conffile prompt: on 2026-10-09 it skipped 610 -> 615 and installed
  the kernel anyway, and the next boot had no card. See docs/operations.md.

## 2.25.0 — 2026-10-07

- **Why there are no cards, in nvidia-smi's words.** When nvidia-smi names no
  card, the scout lists the cards lspci sees as "driver missing" — all lspci
  can say, while the driver may be installed and waiting for a reboot
  ("Driver/library version mismatch"). Both reports now carry `gpuError`:
  the first line nvidia-smi wrote instead of the cards (at times with exit
  code 0), "nvidia-smi did not answer in 5 s" for a hung driver, or "" when
  it named the cards or is not installed. The
  controller read this on its own machine only; it now takes it from every
  scout, its own machine's included.

## 2.24.0 — 2026-10-07

- **The machine's hands for its controller.** A controller in a container cannot
  reach the machine it runs on, so it asks that machine's scout for what it
  used to read and do there itself:
  - `GET /api/host/driver` — the NVIDIA driver packages as apt sees them: the
    version in the kernel and the installed ones, the candidates, Secure Boot,
    the signed modules for the running kernel, the OS's own reboot request.
    The controller's driver panel decides from these.
  - `POST /api/host/driver/install` and `GET /api/host/driver/install-status` —
    a driver package installed as a background job. Only `nvidia-driver-<N>`
    and `-open` names that apt offers; under Secure Boot together with their
    signed modules, the DKMS build moved out of their way. Needs passwordless
    sudo for apt-get.
  - `GET /api/host/processes` — the busiest processes by CPU.
  - `GET /api/monitor/btop` — a btop frame and top's table for the monitor drawer.

## 2.23.3 — 2026-10-05

- **The snapshots pass on Linux again.** They were green on a Mac and red on
  every Linux runner, so the GitHub CI failed on each release since 2.19:
  - A snapshot's scout read the machine's own NVIDIA driver facts: `/boot`,
    `/proc` and `modinfo` about the next kernel. The harness refused the
    `modinfo` call, and four files stopped there. A snapshot's machine now has
    no NVIDIA driver anywhere; the driver's own pins point those paths at their
    own files, as before.
  - The quiet-health adoption pin left "whose listener is it" to the host: a Mac
    answered "cannot tell" and Linux "not ours". It now says "ours", like the
    other adoption pins.
  - The engines-loop pin stopped the loop with a `StopIteration` thrown through
    a generator, which Python 3.9 turns into a `RuntimeError`. It stops on an
    exception of its own now.
- Checked in a Linux container (Python 3.12 and 3.14, with a kernel file in
  `/boot` like a GitHub runner) and on macOS (3.9 and 3.13). Taking any of the
  three fixes away turns its pins red again.
- The scout itself is unchanged: only the tests and the version.

## 2.23.2 — 2026-10-02

Recover PrismML/llama.cpp token speeds from cumulative token and compute-time
counters when scrape-window gauges are zero or absent. Keep the last measured
speed while idle; calculate new work from counter deltas without including idle
wall time. The first reading uses the server's accumulated average. Discard old
rates when the process-start header changes or counters decrease, and omit
unavailable/nonfinite samples. vLLM throughput calculation stays unchanged.

## 2.23.1 — 2026-10-02

Recognize the NVIDIA 610 `CUDA UMD Version` header when selecting a PrismML CUDA archive.

## 2.23.0 — 2026-10-02

Add an isolated PrismML runtime for Bonsai cells: pinned official archives with SHA-256 verification, on-demand installation, platform/CUDA selection, option validation, progress reporting and restart/adoption through the normal cell lifecycle. Report the runtime separately from stock llama.cpp.

## 2.22.0 — 2026-09-30

- **The scout describes itself: `GET /openapi.json`.** OpenAPI 3.1, open like
  `/api/health` (the token gate stands after it), `info.version` the version
  `/api/health` names. It is written from the one list of rows the scout
  answers by (`caravan_scout/routes.py`), so a path is in it exactly when the
  scout answers it: the path, a summary, who may ask (the page, `/api/pairing`,
  `/api/health` and `/openapi.json` are open, the rest need the fleet token in
  `X-Caravan-Token`), the query and body fields the scout reads, and "may read
  more" on the start request that is passed on whole. Types of fields and shapes
  of answers are said not to be described yet. The endpoint tables of
  `docs/http-api.md` are rendered from the same rows and a test fails when the
  page and the list disagree; the hand-written ones had drifted — they still
  described a load dropped in 2.18. The home rule for every app with an HTTP
  API; the controller has served its own since 1.3.405.
- **A restore that names no build is refused.** `POST /api/llama-node/restore`
  with no `id`, a blank one or null turned into an ordinary update — the job
  reads an empty `restoreId` as "no restore" and builds the latest release, a
  `git pull` and a rebuild on a machine whose operator asked to go back. It is
  now 400 "build id is required" before any job is touched; the controller has
  refused such a request itself since its 1.3.406. `POST /api/llama-node/update`
  is unchanged: an empty tag there is the latest release.
- **Nothing is downloaded into an engine through the scout.** The operator's
  decision, kept by the controller since its 1.3.401: a model reaches Ollama or
  LM Studio by that engine's own tools. Gone: `POST /api/engines/pull` (404
  now), the `pull` control of both engines, the `downloading` and
  `downloadError` marks on an engine's view, and the code behind them.
  Deleting a model from Ollama stays, and so does the download of a model file
  for a cell, which is another thing.

## 2.21.0 — 2026-09-27

- **Keys on the cells' doors are cancelled — the operator's decision.** A
  cell stays what it was before 2.20: a model on its machine's address and
  a port from the list, reached in the local network without any key. The
  scout no longer takes `cellKey`, writes no `cell.key`, sends no key with
  its own requests, and a cell's view no longer says `door` (it could only
  say `open`). What 2.20 fixed along the way stays: a command cell takes
  `env`, the scout's marker is set last and cannot be named in `env`,
  config.json and state.json are 0600, and `var/` is ignored by git.

## 2.20.1 — 2026-09-26

- **A secret file written before 2.20 is narrowed at start.** On the fleet
  after 2.20.0, state.json was 0600 at its first save but config.json — the
  fleet token — stayed 0664: it is written only when the scout is paired.
  The scout now makes both the owner's only when it starts, a group-readable
  one too; a missing file is not created.

## 2.20.0 — 2026-09-26

- **A cell answers to its key (`cellKey`).** Step 1 of keys on every
  cell's door: the controller will derive one key per port and its proxy
  will present it. A start request may now carry `cellKey`; the process
  gets it as `LLAMA_API_KEY`, `VLLM_API_KEY` and `CARAVAN_CELL_KEY` (each
  server reads its own), and the scout's own requests to the cell —
  `/metrics`, `/props`, its health at re-adoption — carry it, or a closed
  cell would lose its speeds and the context window the routes are sized
  by. The key stays out of the command line, start.sh and cell.json
  (start.sh reads it from `cell.key`, 0600), the report and the log tails.
  Each running cell's view says `door`: `closed` when a request without
  the key is refused, `open` when it is served — asked once per start.
  A controller that sends no key gets cells as before: open, and said so.
- **A command cell takes `env` too.** It was dropped without a word; the
  names the scout sets itself (the key's, and the marker it finds its cells
  by) are refused in `env`, and the marker is set last, so no environment a
  start carries can take it away.
- **Secrets on disk are the owner's only.** `config.json` holds the fleet
  token and `state.json` the cells' start requests, now with their keys;
  written with the machine's umask they were 0664, readable by every user
  of the machine. Both are now written 0600, through a temp file as before.
  `var/` is ignored by git.

## 2.19.1 — 2026-09-26

- **The driver facts say what the firmware and dpkg say, not a guess.**
  Looked at on the fleet the day 2.19.0 went out: a machine without
  `mokutil` reported Secure Boot as unknown while its firmware said "off",
  and a machine with both `nvidia-driver-550` and `nvidia-driver-580`
  installed, running 580, was reported as 550 — the first in the list.
  `secureBoot` now comes from the firmware's own `SecureBoot` variable
  (readable without root), is `false` on a machine that did not boot
  through EFI, and falls back to `mokutil` only when the variable cannot
  be read. `package` is the installed package whose version is the
  library's, and `null` when none is.

## 2.19.0 — 2026-09-26

- **The NVIDIA driver as the next boot will meet it (`driver`).** On
  2026-09-26 a machine rebooted into a kernel the automatic updates had
  installed the day before, without the Canonical-signed modules it needed:
  Secure Boot refused the unsigned build and the card was gone until it was
  fixed by hand. Nothing had said so before the reboot. The report now
  carries what would have: Secure Boot, the kernel running and the one that
  boots next, the driver version loaded and the one installed, and the
  module the next kernel would load — where it lies and who signed it. Facts
  only, read at most once a minute; the controller draws the conclusion.

## 2.18.0 — 2026-09-26

- **No model is loaded into an engine from the board any more.** The
  operator's choice (controller 1.3.379): a model of Ollama or LM Studio is
  reached through a cell in its engine, and the cell loads it when it
  starts. `POST /api/engines/load` is gone, and with it what only a load
  used: the window and the idle limit it could be told (`holds` in an
  engine's view, `lms load --ttl`), and the memory question before it
  (`short`: Ollama's file-and-cache estimate, LM Studio's
  `lms load --estimate-only`). An engine's `controls` no longer offer
  `load`. Unloading, deleting and downloading a model, and starting or
  stopping the engine's server, are as they were; so is reading how long
  LM Studio holds what is loaded (`lms ps`).

## 2.17.0 — 2026-09-25

- **A model is downloaded into an engine, or deleted from it, from the
  board.** `POST /api/engines/pull {kind, port, model}` — Ollama's streamed
  `/api/pull` (the progress the sum of its layers; a failure can come as a
  line after a 200), LM Studio's `/api/v1/models/download` job asked every
  2 s. Answered at once; the engine carries `downloading` {model, since,
  doneBytes, totalBytes}, kept up to date, one download per engine at a
  time; a failure stays as `downloadError` in the engine's words.
  `POST /api/engines/delete {kind, port, model}` removes an Ollama model
  (`DELETE /api/delete`) — not a loaded one; LM Studio has no verb for it
  and does not say which files are a model's, so it does not offer it.

## 2.16.2 — 2026-09-25

- **Starting LM Studio from the board survives its own self-copy.** LM
  Studio's daemon copies `lms` over itself as it starts; on the live check
  `lms daemon up` started it, and the `lms server start` right after failed
  with "Text file busy", leaving the daemon up and its server not. A command
  that meets the copy is now tried again, a quarter of a second apart, for
  up to five seconds; any other failure to run is said at once.

## 2.16.1 — 2026-09-25

- **Looking at LM Studio never wakes it up.** Most of `lms` starts LM Studio
  when it is not running (`lms ps` prints "Waking up LM Studio service...").
  On the live check a board stop of LM Studio met the scan's `lms ps --json`
  half-way, and the daemon came straight back. Now a read of the idle
  limits, an estimate and a load through the command line ask `lms server
  status` first — which does not wake it — and skip `lms` when the server
  is not running; and every sequence of `lms` commands holds one lock, so a
  read cannot slip between a stop's command and its end.

## 2.16.0 — 2026-09-25

- **An engine's server is started and stopped from the board.** `POST
  /api/engines/start|stop {kind, port}`, answered at once; a start waits up
  to 60 s for the server to answer, a stop up to 20 s for it to go quiet,
  and what failed stays as `serverError`. Only this scout's user's servers:
  another user's — a system service — is named (`runBy: "other"`) and left
  to the operator. How to start a server again is learned from its run
  (Ollama: its binary and `OLLAMA_*` / device variables; LM Studio: its
  `lms`, port and address) or from where it installs itself in the home,
  and kept in `state.json` (`engineServers`); a stop is offered only when a
  start can follow. A known server that is not running is an engine of its
  own, `state: "stopped"`, whose only control is `start`. Ollama runs in a
  session of its own, with the scout's engine mark — never a cell's, so the
  stray-cell reaper cannot take it — output in `engine-logs/ollama.log`;
  stopped with SIGTERM, SIGKILL for it and its children after 10 s. LM
  Studio: `lms daemon up` + `lms server start`, `lms daemon down` (`lms
  server stop` where no daemon runs). A server started from the board
  starts again when the machine boots — once per boot — until it is
  stopped from the board.

## 2.15.0 — 2026-09-25

- **A load that would not fit into the cards' free memory asks first.** A
  load reads the cards' free VRAM at that moment (all cards together: an
  engine spreads a model across them) and, when the model would not fit,
  does not start: the answer is `{ok: false, short: {needBytes, freeBytes,
  basis, error}}` with status 200 — a question for the operator, not a
  refusal — and the same load with `force: true` starts anyway. The need is
  as close as each engine lets it be: LM Studio's own estimate (`lms load
  --estimate-only`), Ollama's file and the cache of the window asked for
  (from the model's shape in `/api/show`), else the file alone — at least
  that much. Cards that do not say their free memory ask nothing.
- **A load says how long the model stays unused.** `hold`: seconds after
  its last use (a minute to a week), or `-1` / none — until unloaded, as
  before. Ollama takes it as `keep_alive`; LM Studio through `lms load
  --ttl`, the one way it takes an idle limit. The view says `holds` — where
  a hold can be kept — and a hold is refused (409) elsewhere. LM Studio's
  loaded models say when they will be let go (`expiresAt`, from `lms ps
  --json`) or `staysLoaded`.
- `LmsCli` — LM Studio's own command line, `~/.lmstudio/bin/lms`, for what its
  REST API does not say or do.

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
