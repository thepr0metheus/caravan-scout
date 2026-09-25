#!/usr/bin/env python3
"""Snapshot of what the scout reads off its host: GPUs, CPU and RAM, ufw, listeners.

caravan_scout/machine.py, the Machine — the nvidia-smi cards and per-process
GPU memory, the lspci fallback for a card whose driver is missing, who ufw
lets reach a port, CPU load/cores/RAM, the ports something listens on — and
the caches around them: gpus (10 s), compute_apps (5 s), firewall (30 s, one
entry per port); listeners and nvidia_smi are asked on demand.

Pinned by value before the scout is rewritten into classes. Every answer
comes from a fake host: nvidia-smi, lspci, ufw, ss, sysctl, /proc, the
platform and the clock are stand-ins, so a pin says the same on a Mac, in CI
and on a GPU box. Agents, VMs, docker and the runtime inventory are left out
on purpose: that code is being deleted, and its pins would go with it.

The tests are plain functions, one scenario each, run once in order — the
shape of the other snapshots here. What holds state is an object: the Checks
ledger and the fakes.

Run: python3 scripts/test_scout_hw.py
"""
import builtins
import contextlib
import io
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import (BLOCKED, Checks, FakeRun, RealCallBlocked,  # noqa: E402
                            make_scout, patched)

from caravan_scout.machine import Machine  # noqa: E402

CHECKS = Checks("scout hw")
check = CHECKS.check

T0 = 1_790_000_000.0


# ── helpers ──────────────────────────────────────────────────────────────────

def same(actual, expected, msg):
    """check(actual == expected) that prints both values when it fails."""
    ok = actual == expected
    check(ok, msg)
    if not ok:
        print(f"        got:  {actual!r}\n        want: {expected!r}")


class Raised:
    """What a call raised, standing in for its value: a crash under a mutant
    then fails the pin that made the call instead of ending the file."""

    def __init__(self, exc):
        self.exc = exc

    def __repr__(self):
        return f"Raised({self.exc!r})"


def outcome(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — a crash is an answer to pin, not the end of the file
        return Raised(exc)


def dig(value, *path):
    """value[p0][p1]…, or None when that shape is not there."""
    for key in path:
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError):
            return None
    return value


class KwRun(FakeRun):
    """FakeRun that also keeps each call's keyword arguments. The timeout is
    part of what a probe promises: a hung driver or ufw must not hang the
    scout's status pass."""

    def __init__(self, table=None):
        super().__init__(table)
        self.kwargs = []

    def __call__(self, cmd, *args, **kwargs):
        self.kwargs.append(dict(kwargs))
        return super().__call__(cmd, *args, **kwargs)


class NvidiaSmi:
    """subprocess.run for a bare `nvidia-smi` with separate stdout and stderr
    (FakeRun gives both the same text, and nvidia_smi picks one).
    `error(cmd, kwargs)` builds an exception to raise instead of answering."""

    def __init__(self, rc=0, stdout="", stderr="", error=None):
        self.rc, self.stdout, self.stderr, self.error = rc, stdout, stderr, error
        self.calls, self.kwargs = [], []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        self.kwargs.append(dict(kwargs))
        if self.error:
            raise self.error(list(cmd), kwargs)
        return subprocess.CompletedProcess(list(cmd), self.rc, stdout=self.stdout, stderr=self.stderr)


class FakeClock:
    """time.time for a pin: it stands still until the pin moves it."""

    def __init__(self, now=T0):
        self.now = now

    def time(self):
        return self.now


class FakeHost:
    """One imagined machine for host_cpu_ram: platform, cores, load average,
    CPU affinity, /proc files and sysctl. A file given as None does not exist;
    loadavg/affinity given as None are calls the OS does not offer."""

    def __init__(self, platform="linux", cpu_count=8, loadavg=(2.0, 1.5, 1.0), affinity=None,
                 cpuinfo=None, meminfo=None, sysctl=None):
        self.platform = platform
        self.cpu_count = cpu_count
        self.loadavg = loadavg
        self.affinity = affinity
        self.files = {"/proc/cpuinfo": cpuinfo, "/proc/meminfo": meminfo}
        self.run = KwRun({("sysctl",): sysctl} if sysctl is not None else {})
        self._real_open = builtins.open
        self._real_exists = os.path.exists

    def _getloadavg(self):
        if self.loadavg is None:
            raise OSError("Load averages are unobtainable")
        return self.loadavg

    def _sched_getaffinity(self, _pid):
        if self.affinity is None:
            raise AttributeError("sched_getaffinity")  # as on macOS: no such call
        return set(self.affinity)

    def _exists(self, path):
        if path in self.files:
            return self.files[path] is not None
        return self._real_exists(path)

    def _open(self, path, *args, **kwargs):
        if path in self.files:
            if self.files[path] is None:
                raise FileNotFoundError(2, "No such file or directory", path)
            return io.StringIO(self.files[path])
        return self._real_open(path, *args, **kwargs)

    def cpu_ram(self):
        with patched(sys, platform=self.platform), \
                patched(os, cpu_count=lambda: self.cpu_count, getloadavg=self._getloadavg,
                        sched_getaffinity=self._sched_getaffinity), \
                patched(os.path, exists=self._exists), \
                patched(builtins, open=self._open), \
                patched(subprocess, run=self.run):
            return outcome(Machine.cpu_ram)


def cpuinfo(*cores):
    """/proc/cpuinfo text: one processor block per (physical id, core id)."""
    return "\n".join(
        f"processor\t: {n}\nvendor_id\t: GenuineIntel\nphysical id\t: {sock}\n"
        f"siblings\t: 8\ncore id\t\t: {core}\ncpu cores\t: 4\n"
        for n, (sock, core) in enumerate(cores))


def meminfo(**fields):
    return "".join(f"{k}:{v:>16} kB\n" for k, v in fields.items())


# ── fixtures ─────────────────────────────────────────────────────────────────

GPU_QUERY = ["nvidia-smi",
             "--query-gpu=index,name,memory.total,memory.used,memory.free,"
             "utilization.gpu,temperature.gpu,power.draw,uuid",
             "--format=csv,noheader,nounits"]
APPS_QUERY = ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory",
              "--format=csv,noheader,nounits"]
UUID_A = "GPU-00000000-0000-0000-0000-00000000000a"
UUID_B = "GPU-00000000-0000-0000-0000-00000000000b"

SMI_CARD_A = f"0, NVIDIA GeForce RTX 3090, 24576, 1024, 23552, 3, 41, 27.50, {UUID_A}"
SMI_CARD_B = f"1, NVIDIA GeForce RTX 5090, 32607, 30000, 2607, 97, 78, 540.12, {UUID_B}"
SMI_TWO_CARDS = f"{SMI_CARD_A}\n{SMI_CARD_B}\n"
SMI_BROKEN = "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver."

CARD_A = {"index": "0", "name": "NVIDIA GeForce RTX 3090", "vendor": "nvidia", "driverStatus": "ok",
          "memoryTotalMiB": "24576", "memoryUsedMiB": "1024", "memoryFreeMiB": "23552",
          "utilizationGpuPct": "3", "temperatureC": "41", "powerDrawW": "27.50", "uuid": UUID_A}
CARD_B = {"index": "1", "name": "NVIDIA GeForce RTX 5090", "vendor": "nvidia", "driverStatus": "ok",
          "memoryTotalMiB": "32607", "memoryUsedMiB": "30000", "memoryFreeMiB": "2607",
          "utilizationGpuPct": "97", "temperatureC": "78", "powerDrawW": "540.12", "uuid": UUID_B}

LSPCI = """00:02.0 VGA compatible controller: Intel Corporation UHD Graphics 630 (rev 02)
01:00.0 VGA compatible controller: NVIDIA Corporation GA102 [GeForce RTX 3090] (rev a1)
01:00.1 Audio device: NVIDIA Corporation GA102 High Definition Audio Controller (rev a1)
02:00.0 3D controller: NVIDIA Corporation GH100 [H100 PCIe] (rev a1)
03:00.0 Display controller: NVIDIA Corporation Device 2bb1 (rev a1)
04:00.0 VGA compatible controller: NVIDIA Corporation Device [NVIDIA GeForce RTX 5090] (rev a1)
05:00.0 VGA compatible controller: NVIDIA Corporation Device [] (rev a1)
"""


def lspci_card(index, name):
    return {"index": str(index), "name": name, "vendor": "nvidia", "driverStatus": "driver_missing"}


LSPCI_CARDS = [lspci_card(0, "NVIDIA GeForce RTX 3090"), lspci_card(1, "NVIDIA H100 PCIe"),
               lspci_card(2, "NVIDIA GPU"), lspci_card(3, "NVIDIA GeForce RTX 5090"),
               lspci_card(4, "NVIDIA GPU")]

UFW_STATUS = ["sudo", "-n", "ufw", "status"]
UFW_ACTIVE = """Status: active

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW       Anywhere
22001/tcp                  ALLOW       10.0.0.20                  # controller
22001/tcp                  ALLOW       10.0.0.21
22001                      ALLOW       10.0.0.20
22002/tcp                  ALLOW       Anywhere
22003/tcp                  DENY        Anywhere
22004/tcp                  ALLOW IN    10.0.0.30
22005:22010/tcp            ALLOW       Anywhere
22011/tcp                  ALLOW OUT   Anywhere
22/tcp (v6)                ALLOW       Anywhere (v6)
22002/tcp (v6)             ALLOW       Anywhere (v6)
22012/tcp (v6)             ALLOW       fd00::20 (v6)
"""
FW_22001 = {"state": "restricted", "allowedFrom": ["10.0.0.20", "10.0.0.21"]}
FW_ALL = {"state": "all", "allowedFrom": ["Anywhere"]}
FW_BLOCKED = {"state": "blocked", "allowedFrom": []}

SS_LISTEN = """LISTEN 0      4096         0.0.0.0:8092       0.0.0.0:*
LISTEN 0      511        127.0.0.1:22001      0.0.0.0:*
LISTEN 0      4096            [::]:8092          [::]:*
LISTEN 0      128                *:9100             *:*
LISTEN 0      4096   127.0.0.53%lo:53         0.0.0.0:*
garbage
LISTEN 0      128             [::]:ssh           [::]:*
"""

SS_PROCS = """LISTEN 0      4096         0.0.0.0:22001      0.0.0.0:*    users:(("llama-server",pid=4242,fd=3))
LISTEN 0      4096            [::]:22001         [::]:*    users:(("llama-server",pid=4242,fd=4))
LISTEN 0      128          0.0.0.0:22         0.0.0.0:*
LISTEN 0      128             [::]:22            [::]:*
LISTEN 0      511        127.0.0.1:3000       0.0.0.0:*    users:(("node",pid=777,fd=20))
LISTEN 0      4096         0.0.0.0:8092       0.0.0.0:*
LISTEN 0      4096            [::]:8092          [::]:*    users:(("python3",pid=900,fd=5))
LISTEN 0      511          0.0.0.0:80         0.0.0.0:*    users:(("nginx",pid=10,fd=6),("nginx",pid=11,fd=6))
LISTEN 0      4096         0.0.0.0:9000       0.0.0.0:*    users:(("first",pid=1,fd=3))
LISTEN 0      4096            [::]:9000          [::]:*    users:(("second",pid=2,fd=3))
LISTEN 0      4096         0.0.0.0:9100       0.0.0.0:*    users:(("exporter",pid=55,fd=3))
LISTEN 0      4096            [::]:9100          [::]:*
garbage
LISTEN 0      128             [::]:ssh           [::]:*
"""
LISTENERS = [{"port": 22, "proc": "", "pid": 0},
             {"port": 80, "proc": "nginx", "pid": 10},
             {"port": 3000, "proc": "node", "pid": 777},
             {"port": 8092, "proc": "python3", "pid": 900},
             {"port": 9000, "proc": "first", "pid": 1},
             {"port": 9100, "proc": "exporter", "pid": 55},
             {"port": 22001, "proc": "llama-server", "pid": 4242}]


def by_port(rows, port):
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("port") == port:
            return row
    return None


# ── Machine: GPUs ────────────────────────────────────────────────────────────

def test_nvidia_gpus():
    CHECKS.section("карты из nvidia-smi:")
    run = KwRun({("nvidia-smi",): (0, SMI_TWO_CARDS)})
    with patched(subprocess, run=run):
        got = outcome(Machine.nvidia_gpus)
    same(got, [CARD_A, CARD_B],
         "две карты — ровно те имена полей, что читает доска (контракт с gpu_state контроллера)")
    same(run.calls, [GPU_QUERY], "один вызов nvidia-smi с запросом ровно этих полей в этом порядке")
    same([k.get("timeout") for k in run.kwargs], [5], "таймаут 5 с: зависший драйвер не вешает скаута")
    same(dig(got, 0, "memoryTotalMiB"), "24576",
         "as-is: числа приходят строками, как их напечатал nvidia-smi")

    na = "2, NVIDIA GeForce RTX 3090, 24576, 0, 24576, 0, 35, [N/A], " + UUID_A
    with patched(subprocess, run=KwRun({("nvidia-smi",): (0, na + "\n")})):
        got = outcome(Machine.nvidia_gpus)
    same(dig(got, 0, "powerDrawW"), "[N/A]", "as-is: «[N/A]» проходит насквозь строкой, не превращается в 0")

    eight = "0, NVIDIA GeForce RTX 3090, 24576, 1024, 23552, 3, 41, 27.50"
    with patched(subprocess, run=KwRun({("nvidia-smi",): (0, eight + "\n")})):
        got = outcome(Machine.nvidia_gpus)
    same(got, [{**CARD_A, "uuid": ""}], "boundary: восемь полей (без uuid) — карта есть, uuid пустой")

    seven = "0, NVIDIA GeForce RTX 3090, 24576, 1024, 23552, 3, 41"
    with patched(subprocess, run=KwRun({("nvidia-smi",): (0, f"{seven}\n\n{SMI_CARD_B}\n")})):
        got = outcome(Machine.nvidia_gpus)
    same(got, [CARD_B], "negative: строка короче восьми полей и пустая строка пропускаются")

    for name, answer in (("код возврата не 0", (9, SMI_BROKEN)),
                         ("nvidia-smi нет", FileNotFoundError(2, "No such file or directory")),
                         ("таймаут", subprocess.TimeoutExpired(GPU_QUERY, 5))):
        with patched(subprocess, run=KwRun({("nvidia-smi",): answer})):
            same(outcome(Machine.nvidia_gpus), [], f"negative: {name} — пустой список")


def test_nvidia_apps():
    CHECKS.section("память карт по процессам:")
    out = (f"{UUID_A}, 4242, 20480\n{UUID_B}, 4242, 18000\n{UUID_A}, 5151, [N/A]\n"
           f"{UUID_A}, [N/A], 100\n{UUID_B}, 6161\n\n")
    run = KwRun({("nvidia-smi",): (0, out)})
    with patched(subprocess, run=run):
        got = outcome(Machine.nvidia_apps)
    same(got, [{"gpuUuid": UUID_A, "pid": 4242, "usedMiB": 20480},
               {"gpuUuid": UUID_B, "pid": 4242, "usedMiB": 18000},
               {"gpuUuid": UUID_A, "pid": 5151, "usedMiB": 0}],
         "pid→карта многие-ко-многим: один сервер на двух картах — две строки; pid и память числами")
    same(run.calls, [APPS_QUERY], "запрос compute-apps: gpu_uuid, pid, used_memory")
    same([k.get("timeout") for k in run.kwargs], [5], "таймаут 5 с")
    same(dig(got, 2, "usedMiB"), 0, "as-is: память «[N/A]» читается как 0 МиБ — неизвестное нарисовано нулём")
    same(len(got) if isinstance(got, list) else got, 3,
         "negative: pid «[N/A]», строка из двух полей и пустая строка пропускаются")
    for name, answer in (("код возврата не 0", (9, SMI_BROKEN)),
                         ("nvidia-smi нет", FileNotFoundError(2, "No such file or directory"))):
        with patched(subprocess, run=KwRun({("nvidia-smi",): answer})):
            same(outcome(Machine.nvidia_apps), [], f"negative: {name} — пустой список")


def test_lspci_fallback():
    CHECKS.section("карта без драйвера — через lspci:")
    run = KwRun({("lspci",): (0, LSPCI)})
    with patched(subprocess, run=run), patched(sys, platform="linux"):
        got = outcome(Machine.lspci_gpus)
    same(got, LSPCI_CARDS, "Linux: все карты NVIDIA из lspci, по порядку, с driverStatus driver_missing")
    same(dig(got, 0, "name"), "NVIDIA GeForce RTX 3090", "имя — из последних скобок, с приставкой NVIDIA")
    same(dig(got, 1, "name"), "NVIDIA H100 PCIe", "«3D controller» (карта без видеовыхода) — тоже карта")
    same(dig(got, 2, "name"), "NVIDIA GPU", "«Display controller» без скобок — карта с общим именем")
    same(dig(got, 3, "name"), "NVIDIA GeForce RTX 5090", "имя уже с NVIDIA — приставка не удваивается")
    same(dig(got, 4, "name"), "NVIDIA GPU", "boundary: пустые скобки — общее имя")
    same(run.calls, [["lspci"]], "один вызов lspci без флагов")
    same([k.get("timeout") for k in run.kwargs], [5], "таймаут 5 с")
    not_cards = ("00:02.0 VGA compatible controller: Intel Corporation UHD Graphics 630 (rev 02)\n"
                 "01:00.1 Audio device: NVIDIA Corporation GA102 High Definition Audio Controller (rev a1)\n")
    with patched(subprocess, run=KwRun({("lspci",): (0, not_cards)})), patched(sys, platform="linux"):
        same(outcome(Machine.lspci_gpus), [],
             "negative: звуковая функция карты NVIDIA и видео Intel — не карты")

    run = KwRun({("lspci",): (0, LSPCI)})
    with patched(subprocess, run=run), patched(sys, platform="darwin"):
        got = outcome(Machine.lspci_gpus)
    same((got, run.calls), ([], []), "negative: не Linux — пусто, lspci даже не спрашивается")
    for name, answer in (("код возврата не 0", (1, "pcilib: cannot open /sys/bus/pci")),
                         ("lspci нет", FileNotFoundError(2, "No such file or directory"))):
        with patched(subprocess, run=KwRun({("lspci",): answer})), patched(sys, platform="linux"):
            same(outcome(Machine.lspci_gpus), [], f"negative: {name} — пусто")


def test_gpu_inventory():
    CHECKS.section("инвентарь карт: nvidia-smi, иначе lspci:")
    run = KwRun({("nvidia-smi",): (0, SMI_TWO_CARDS), ("lspci",): (0, LSPCI)})
    with patched(subprocess, run=run), patched(sys, platform="linux"):
        got = outcome(Machine.gpu_inventory)
    same(got, [CARD_A, CARD_B], "драйвер работает — живые карты nvidia-smi")
    same([c[0] for c in run.calls], ["nvidia-smi"], "negative: при живом nvidia-smi lspci не зовётся")

    run = KwRun({("nvidia-smi",): (9, SMI_BROKEN), ("lspci",): (0, LSPCI)})
    with patched(subprocess, run=run), patched(sys, platform="linux"):
        got = outcome(Machine.gpu_inventory)
    same(got, LSPCI_CARDS, "драйвер сломан — карты из lspci с пометкой driver_missing, а не «карт нет»")

    run = KwRun({("nvidia-smi",): (0, ""), ("lspci",): (0, LSPCI)})
    with patched(subprocess, run=run), patched(sys, platform="linux"):
        got = outcome(Machine.gpu_inventory)
    same(got, LSPCI_CARDS, "boundary: nvidia-smi ответил пусто с кодом 0 — всё равно спрашиваем lspci")

    run = KwRun({})
    with patched(subprocess, run=run), patched(sys, platform="darwin"):
        got = outcome(Machine.gpu_inventory)
    same((got, [c[0] for c in run.calls]), ([], ["nvidia-smi"]),
         "negative: Mac без nvidia-smi — пусто, lspci не спрашивается")


# ── Machine: ufw ─────────────────────────────────────────────────────────────

def test_ufw_access():
    CHECKS.section("кого ufw пускает на порт:")
    answers = {}
    run = KwRun({tuple(UFW_STATUS): (0, UFW_ACTIVE)})
    with patched(subprocess, run=run):
        for port in (22001, 22002, 22003, 22004, 22007, 22011, 22012, 2200, 9999, 22, "22001"):
            answers[port] = outcome(Machine.ufw_access, port)
    same(answers[22001], FW_22001,
         "только с адресов — restricted: комментарий срезан, повтор убран, порядок первого появления")
    same(answers[22002], FW_ALL, "Anywhere — all")
    same(answers[22], FW_ALL, "правило v6 «Anywhere (v6)» — тоже Anywhere")
    same(answers[22003], FW_BLOCKED, "negative: DENY — не разрешение: blocked")
    same(answers[22004], {"state": "restricted", "allowedFrom": ["10.0.0.30"]}, "«ALLOW IN» — IN не часть адреса")
    same(answers[22012], FW_BLOCKED,
         "negative: источник с пометкой (v6) в список не попадает (as-is: порт только с таким правилом — blocked)")
    same(answers[2200], FW_BLOCKED, "negative: порт 2200 — не 22001: сравнение числом, не префиксом")
    same(answers[9999], FW_BLOCKED, "negative: ufw активен, правила на порт нет — blocked")
    same(answers["22001"], FW_22001, "порт строкой — то же, что числом")
    same(answers[22007], FW_BLOCKED,
         "as-is: диапазон «22005:22010/tcp ALLOW Anywhere» не понимается — порт внутри читается blocked")
    same(answers[22011], {"state": "restricted", "allowedFrom": ["OUT   Anywhere"]},
         "as-is: исходящее «ALLOW OUT» читается входящим источником «OUT   Anywhere»")
    same(run.calls, [UFW_STATUS] * 11, "каждый вопрос — один `sudo -n ufw status` (без пароля; кэш — на скауте)")
    same({k.get("timeout") for k in run.kwargs}, {4}, "таймаут 4 с (по умолчанию run_text)")

    with patched(subprocess, run=KwRun({tuple(UFW_STATUS): (0, "Status: inactive\n")})):
        same(outcome(Machine.ufw_access, 22001), {"state": "open", "allowedFrom": []},
             "ufw выключен — open: пускает всех")
    with patched(subprocess, run=KwRun({tuple(UFW_STATUS): (0, "Status: active\n")})):
        same(outcome(Machine.ufw_access, 22001), FW_BLOCKED, "negative: активен без правил — blocked, не open")
    for name, answer in (("sudo просит пароль", (1, "sudo: a password is required")),
                         ("ufw нет", FileNotFoundError(2, "No such file or directory"))):
        with patched(subprocess, run=KwRun({tuple(UFW_STATUS): answer})):
            same(outcome(Machine.ufw_access, 22001), {"state": "unknown"},
                 f"negative: {name} — unknown (as-is: без allowedFrom)")
    run = KwRun({tuple(UFW_STATUS): (0, UFW_ACTIVE)})
    with patched(subprocess, run=run):
        got = [outcome(Machine.ufw_access, p) for p in ("abc", None)]
    same((got, run.calls), ([{"state": "unknown"}] * 2, []), "negative: порт не число — unknown, ufw не спрашивается")


# ── Machine: CPU and RAM ─────────────────────────────────────────────────────

HT_4_CORES = cpuinfo((0, 0), (0, 1), (0, 2), (0, 3), (0, 0), (0, 1), (0, 2), (0, 3))
MEMINFO = meminfo(MemTotal=65843012, MemFree=2345678, MemAvailable=40000000, Buffers=123456)


def test_cpu_ram():
    CHECKS.section("CPU и RAM узла:")
    linux = FakeHost(platform="linux", cpu_count=8, loadavg=(2.0, 1.5, 1.0), affinity=range(6),
                     cpuinfo=HT_4_CORES, meminfo=MEMINFO, sysctl=(0, "34359738368\n"))
    got = linux.cpu_ram()
    same(got, {"loadPct": 25.0, "load1": 2.0, "ncpu": 8, "logicalCores": 8, "availableCores": 6,
               "physicalCores": 4, "ram": {"usedGb": 24.6, "totalGb": 62.8}},
         "Linux: нагрузка в % от ядер, ядра логические/доступные/физические, RAM из /proc/meminfo")
    same(dig(got, "availableCores"), 6, "на VM с приколотыми ядрами — доступный срез (sched_getaffinity), не все ядра")
    same(dig(got, "physicalCores"), 4, "физические ядра — пары (physical id, core id): гиперпотоки не считаются")
    same(linux.run.calls, [], "negative: на Linux с /proc/meminfo sysctl не зовётся")

    two_sockets = FakeHost(cpu_count=4, affinity=range(4), cpuinfo=cpuinfo((0, 0), (0, 1), (1, 0), (1, 1)),
                           meminfo=MEMINFO)
    same(dig(two_sockets.cpu_ram(), "physicalCores"), 4,
         "два сокета с одинаковыми core id — четыре ядра, а не два")
    no_core_ids = FakeHost(cpu_count=8, affinity=range(8), cpuinfo="processor\t: 0\nmodel name\t: ARMv8\n",
                           meminfo=MEMINFO)
    same(dig(no_core_ids.cpu_ram(), "physicalCores"), 8, "boundary: в cpuinfo нет core id — физических = логических")
    no_cpuinfo = FakeHost(cpu_count=8, affinity=range(8), cpuinfo=None, meminfo=MEMINFO)
    same(dig(no_cpuinfo.cpu_ram(), "physicalCores"), 8, "negative: cpuinfo нет — физических = логических")

    busy = FakeHost(cpu_count=8, loadavg=(12.0, 9.0, 4.0), affinity=range(8), cpuinfo=HT_4_CORES, meminfo=MEMINFO)
    got = busy.cpu_ram()
    same((dig(got, "loadPct"), dig(got, "load1")), (100.0, 12.0),
         "boundary: нагрузка выше числа ядер — процент упирается в 100, load1 — как есть")

    tiny = FakeHost(cpu_count=None, loadavg=(0.7351, 0.5, 0.2), affinity=None, cpuinfo=None, meminfo=None)
    same(tiny.cpu_ram(), {"loadPct": 73.5, "load1": 0.74, "ncpu": 1, "logicalCores": 1, "availableCores": 1,
                          "physicalCores": 1},
         "boundary: cpu_count неизвестен — одно ядро; без meminfo и sysctl RAM нет вовсе")

    no_load = FakeHost(cpu_count=8, loadavg=None, affinity=None, cpuinfo=HT_4_CORES, meminfo=MEMINFO)
    same(no_load.cpu_ram(), {"logicalCores": 8, "availableCores": 8, "physicalCores": 4,
                             "ram": {"usedGb": 24.6, "totalGb": 62.8}},
         "negative: load average недоступен — ключей нагрузки нет (не ноль); без affinity доступны все ядра")

    mac = FakeHost(platform="darwin", cpu_count=10, loadavg=(3.0, 2.0, 1.0), affinity=None,
                   cpuinfo=None, meminfo=None, sysctl=(0, "34359738368\n"))
    got = mac.cpu_ram()
    same(got, {"loadPct": 30.0, "load1": 3.0, "ncpu": 10, "logicalCores": 10, "availableCores": 10,
               "physicalCores": 10, "ram": {"usedGb": None, "totalGb": 32.0}},
         "Mac: объём из sysctl hw.memsize, занятое — None (не знаем), а не 0")
    same(mac.run.calls, [["sysctl", "-n", "hw.memsize"]], "Mac: один вызов sysctl -n hw.memsize")
    for name, answer in (("sysctl упал", (1, "sysctl: unknown oid 'hw.memsize'")),
                         ("sysctl ответил не числом", (0, "lots\n"))):
        got = FakeHost(platform="darwin", cpu_count=10, affinity=None, sysctl=answer).cpu_ram()
        check(isinstance(got, dict) and "ram" not in got, f"negative: {name} — ключа ram нет")

    linux_no_meminfo = FakeHost(platform="linux", cpu_count=8, affinity=range(8), cpuinfo=HT_4_CORES,
                                meminfo=None, sysctl=(255, "sysctl: cannot stat /proc/sys/hw/memsize"))
    got = linux_no_meminfo.cpu_ram()
    same(("ram" in (got if isinstance(got, dict) else {}), linux_no_meminfo.run.calls),
         (False, [["sysctl", "-n", "hw.memsize"]]),
         "negative: Linux без /proc/meminfo спрашивает sysctl, тот падает — ключа ram нет")

    old_kernel = FakeHost(cpu_count=8, affinity=range(8), cpuinfo=HT_4_CORES,
                          meminfo=meminfo(MemTotal=65843012, MemFree=2345678))
    same(dig(old_kernel.cpu_ram(), "ram"), {"usedGb": 62.8, "totalGb": 62.8},
         "as-is: без MemAvailable (ядро < 3.14) занятой считается ВСЯ память")
    broken = FakeHost(cpu_count=8, affinity=range(8), cpuinfo=HT_4_CORES,
                      meminfo="MemTotal:       lots kB\nMemAvailable:   1 kB\n")
    got = broken.cpu_ram()
    check(isinstance(got, dict) and "ram" not in got and got.get("physicalCores") == 4,
          "negative: битый meminfo — ключа ram нет, остальное на месте")


# ── Machine: small parts ─────────────────────────────────────────────────────

def test_run_text():
    CHECKS.section("run_text: вывод или пусто:")
    run = KwRun({("tool", "ok"): (0, "line 1\nline 2\n"), ("tool", "bad"): (3, "partial output"),
                 ("tool", "slow"): subprocess.TimeoutExpired(["tool", "slow"], 4), ("tool", "quiet"): (0, "")})
    with patched(subprocess, run=run):
        got = [outcome(Machine.run_text, ["tool", "ok"]), outcome(Machine.run_text, ["tool", "bad"]),
               outcome(Machine.run_text, ["tool", "slow"]), outcome(Machine.run_text, ["no-such-tool"]),
               outcome(Machine.run_text, ["tool", "quiet"]), outcome(Machine.run_text, ["tool", "ok"], timeout=9)]
    same(dig(got, 0), "line 1\nline 2\n", "код 0 — stdout как есть, без обрезки")
    same(dig(got, 1), "", "negative: код не 0 — пусто, даже если что-то напечатано")
    same(dig(got, 2), "", "negative: таймаут — пусто")
    same(dig(got, 3), "", "negative: инструмента нет — пусто")
    same(dig(got, 4), "", "as-is: успех с пустым выводом неотличим от отказа — оба пусто")
    same([k.get("timeout") for k in run.kwargs], [4, 4, 4, 4, 4, 9], "таймаут по умолчанию 4 с, свой — передаётся")
    same({(k.get("text"), k.get("capture_output")) for k in run.kwargs}, {(True, True)},
         "текстом и с перехватом вывода")


def test_gpus_cache():
    CHECKS.section("кэш карт на скауте (10 с):")
    clock = FakeClock()
    run = KwRun({("nvidia-smi",): (0, SMI_TWO_CARDS)})
    scout = make_scout()
    with patched(time, time=clock.time), patched(subprocess, run=run), patched(sys, platform="linux"):
        first = outcome(scout.machine.gpus)
        run.table[("nvidia-smi",)] = (0, SMI_CARD_A + "\n")  # the host changed; the cache does not know yet
        clock.now = T0 + 9.9
        cached = outcome(scout.machine.gpus)
        calls_cached = len(run.calls)
        clock.now = T0 + 10
        fresh = outcome(scout.machine.gpus)
        calls_fresh = len(run.calls)
    same(first, [CARD_A, CARD_B], "первый опрос — живые карты")
    same((cached, calls_cached), ([CARD_A, CARD_B], 1), "в пределах 10 с — из кэша, nvidia-smi не запускается")
    same((fresh, calls_fresh), ([CARD_A], 2), "boundary: ровно через 10 с — опрос заново и новый ответ")

    clock = FakeClock()
    run = KwRun({("nvidia-smi",): (9, SMI_BROKEN), ("lspci",): (0, "")})
    scout = make_scout()
    with patched(time, time=clock.time), patched(subprocess, run=run), patched(sys, platform="linux"):
        first = outcome(scout.machine.gpus)
        clock.now = T0 + 5
        again = outcome(scout.machine.gpus)
    same((first, again, len(run.calls)), ([], [], 2),
         "negative: пустой ответ тоже кэшируется — хост без карт не гоняет nvidia-smi и lspci на каждый запрос")


def test_compute_apps_cache():
    CHECKS.section("кэш памяти по процессам на скауте (5 с):")
    clock = FakeClock()
    run = KwRun({("nvidia-smi",): (0, f"{UUID_A}, 4242, 20480\n")})
    scout = make_scout()
    with patched(time, time=clock.time), patched(subprocess, run=run):
        first = outcome(scout.machine.compute_apps)
        run.table[("nvidia-smi",)] = (0, f"{UUID_A}, 5151, 1024\n")
        clock.now = T0 + 4.9
        cached = outcome(scout.machine.compute_apps)
        calls_cached = len(run.calls)
        clock.now = T0 + 5
        fresh = outcome(scout.machine.compute_apps)
        calls_fresh = len(run.calls)
    same(first, [{"gpuUuid": UUID_A, "pid": 4242, "usedMiB": 20480}], "первый опрос — живая карта процессов")
    same((cached, calls_cached), (first, 1), "в пределах 5 с — из кэша")
    same((fresh, calls_fresh), ([{"gpuUuid": UUID_A, "pid": 5151, "usedMiB": 1024}], 2),
         "boundary: ровно через 5 с — опрос заново и новый ответ")

    clock = FakeClock()
    run = KwRun({})
    scout = make_scout()
    with patched(time, time=clock.time), patched(subprocess, run=run):
        answers = [outcome(scout.machine.compute_apps), outcome(scout.machine.compute_apps)]
    same((answers, len(run.calls)), ([[], []], 1), "negative: пустой ответ тоже кэшируется")


def test_firewall_cache():
    CHECKS.section("кэш ufw на скауте (30 с, по записи на порт):")
    clock = FakeClock()
    run = KwRun({tuple(UFW_STATUS): (0, UFW_ACTIVE)})
    scout = make_scout()
    with patched(time, time=clock.time), patched(subprocess, run=run):
        answers = [outcome(scout.machine.firewall, p) for p in (22001, 22002, 22001, 22002, 22001, 22002)]
        calls_alternating = len(run.calls)
        clock.now = T0 + 29.9
        outcome(scout.machine.firewall, 22001)
        outcome(scout.machine.firewall, 22002)
        calls_before_ttl = len(run.calls)
        clock.now = T0 + 30
        after = outcome(scout.machine.firewall, 22001)
        calls_after_ttl = len(run.calls)
    same(answers, [FW_22001, FW_ALL] * 3, "каждому порту — своё решение")
    same(calls_alternating, 2,
         "две ячейки через раз — ufw по разу на порт (кэш на один слот давал 232 форка/мин и load 25)")
    same(calls_before_ttl, 2, "в пределах 30 с — из кэша")
    same((after, calls_after_ttl), (FW_22001, 3), "boundary: ровно через 30 с — ufw заново")

    clock = FakeClock()
    run = KwRun({tuple(UFW_STATUS): (0, UFW_ACTIVE)})
    scout = make_scout()
    with patched(time, time=clock.time), patched(subprocess, run=run):
        outcome(scout.machine.firewall, 22001)
        clock.now = T0 + 20
        outcome(scout.machine.firewall, 22002)
        clock.now = T0 + 30
        outcome(scout.machine.firewall, 22001)
        outcome(scout.machine.firewall, 22002)
    same(len(run.calls), 3, "у каждого порта свой срок: в T+30 устарел только спрошенный в T")

    clock = FakeClock()
    run = KwRun({tuple(UFW_STATUS): (1, "sudo: a password is required")})
    scout = make_scout()
    with patched(time, time=clock.time), patched(subprocess, run=run):
        answers = [outcome(scout.machine.firewall, 22001), outcome(scout.machine.firewall, 22001)]
    same((answers, len(run.calls)), ([{"state": "unknown"}] * 2, 1),
         "negative: отказ sudo тоже кэшируется — без пароля ufw не дёргается на каждый опрос")

    clock = FakeClock()
    run = KwRun({tuple(UFW_STATUS): (0, UFW_ACTIVE)})
    scout = make_scout()
    with patched(time, time=clock.time), patched(subprocess, run=run):
        outcome(scout.machine.firewall, 22001)
        outcome(scout.machine.firewall, "22001")
    same(len(run.calls), 2, "as-is: ключ кэша — порт как передан: 22001 и «22001» — две записи")


# ── the scout: probes served over HTTP ───────────────────────────────────────

def test_listeners():
    CHECKS.section("кто слушает на хосте (/api/host/listeners):")
    run = KwRun({("ss",): (0, SS_PROCS)})
    scout = make_scout()
    with patched(subprocess, run=run):
        got = outcome(scout.machine.listeners)
    same(got, {"ok": True, "ports": LISTENERS}, "по строке на порт, по возрастанию порта, владелец где ОС его назвала")
    rows = dig(got, "ports")
    same(by_port(rows, 8092), {"port": 8092, "proc": "python3", "pid": 900},
         "v4 без владельца и v6 с владельцем — одна строка, с владельцем")
    same(by_port(rows, 9100), {"port": 9100, "proc": "exporter", "pid": 55},
         "negative: владелец из первой строки не затирается пустой второй")
    same(by_port(rows, 9000), {"port": 9000, "proc": "first", "pid": 1}, "boundary: два владельца — первый")
    same(by_port(rows, 80), {"port": 80, "proc": "nginx", "pid": 10}, "несколько процессов на сокете — первый")
    same(by_port(rows, 22), {"port": 22, "proc": "", "pid": 0},
         "владелец неизвестен (не наш процесс, без root) — порт всё равно занят; as-is: pid 0")
    same(run.calls, [["ss", "-ltnpH"]], "ss -ltnpH: с процессами")
    same([k.get("timeout") for k in run.kwargs], [6], "таймаут 6 с")

    with patched(subprocess, run=KwRun({})):
        same(outcome(scout.machine.listeners), {"ok": False, "error": "ss", "ports": []},
             "negative: ss нет — ok false с текстом ошибки и пустым списком, а не «никто не слушает»")
    with patched(subprocess, run=KwRun({("ss",): OSError("x" * 300)})):
        same(outcome(scout.machine.listeners), {"ok": False, "error": "x" * 160, "ports": []},
             "boundary: текст ошибки обрезан до 160")
    with patched(subprocess, run=KwRun({("ss",): (1, "ss: unknown option -- H")})):
        same(outcome(scout.machine.listeners), {"ok": True, "ports": []},
             "as-is: код возврата ss не смотрится — упавший ss читается как «никто не слушает»")


def test_nvidia_smi():
    CHECKS.section("сырой nvidia-smi для панели монитора:")
    clock = FakeClock(T0 + 0.8)
    scout = make_scout()
    table = "\n+-----------------------------------------+\n| NVIDIA-SMI 580.82.07  Driver Version: 580.82.07 |\n+----+\n\n"
    fake = NvidiaSmi(0, stdout=table, stderr="")
    with patched(subprocess, run=fake), patched(time, time=clock.time):
        got = outcome(scout.machine.nvidia_smi)
    same(got, {"kind": "nvidia-smi", "ok": True, "output": table.strip(), "source": "box-a", "time": int(T0)},
         "работает — ok, вывод без крайних пустых строк, источник — hostId, время целым")
    same((fake.calls, [k.get("timeout") for k in fake.kwargs]), ([["nvidia-smi"]], [5]),
         "голый nvidia-smi, таймаут 5 с")

    def monitor(fake_run):
        with patched(subprocess, run=fake_run), patched(time, time=clock.time):
            got = outcome(scout.machine.nvidia_smi)
        return dig(got, "ok"), dig(got, "output")

    same(monitor(NvidiaSmi(9, stdout="partial table", stderr=SMI_BROKEN + "\n")), (False, SMI_BROKEN),
         "negative: код не 0 — ok false, текст из stderr")
    same(monitor(NvidiaSmi(9, stdout="Failed to initialize NVML: Driver/library version mismatch\n", stderr="")),
         (False, "Failed to initialize NVML: Driver/library version mismatch"),
         "boundary: stderr пуст — текст из stdout")
    same(monitor(NvidiaSmi(error=lambda cmd, kw: FileNotFoundError(2, "No such file or directory", cmd[0]))),
         (False, "nvidia-smi not found"), "negative: nvidia-smi нет — «nvidia-smi not found»")
    same(monitor(NvidiaSmi(error=lambda cmd, kw: subprocess.TimeoutExpired(cmd, kw.get("timeout")))),
         (False, "Command '['nvidia-smi']' timed out after 5 seconds"),
         "negative: таймаут — текст исключения")

    # hostId empty in config.json is no choice: the id is the one pinned from
    # the machine's hostname (2.10, HostIdentity) — never empty, so the
    # displayName and "remote" fallbacks are left to a machine with no name.
    with patched(subprocess, run=NvidiaSmi(0, stdout="ok")), patched(time, time=clock.time), \
            patched(socket, gethostname=lambda: "box-h.lan"), contextlib.redirect_stdout(io.StringIO()):
        sources = [dig(outcome(make_scout(cfg).machine.nvidia_smi), "source")
                   for cfg in ({"hostId": "", "displayName": "Box B"}, {"hostId": "", "displayName": ""})]
    with patched(subprocess, run=NvidiaSmi(0, stdout="ok")), patched(time, time=clock.time), \
            patched(socket, gethostname=lambda: ""), contextlib.redirect_stdout(io.StringIO()):
        sources.append(dig(outcome(make_scout({"hostId": "", "displayName": "Box B"}).machine.nvidia_smi), "source"))
    same(sources, ["box-h", "box-h", "remote"],
         "источник: id машины — при пустом hostId в файле прибитый hostname; машина без имени — «remote», "
         "а не displayName (2.10)")


TESTS = (test_nvidia_gpus, test_nvidia_apps, test_lspci_fallback, test_gpu_inventory,
         test_ufw_access, test_cpu_ram, test_run_text,
         test_gpus_cache, test_compute_apps_cache,
         test_firewall_cache, test_listeners, test_nvidia_smi)

for test in TESTS:
    blocked_before = len(BLOCKED)
    try:
        test()
    except (Exception, RealCallBlocked) as exc:  # noqa: BLE001 — a crash is a red pin; the rest still runs
        check(False, f"{test.__name__} упал: {exc!r}")
    if len(BLOCKED) > blocked_before:
        check(False, f"{test.__name__} дотянулся до хоста: {BLOCKED[blocked_before:]}")

sys.exit(CHECKS.finish())
