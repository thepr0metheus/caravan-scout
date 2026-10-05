#!/usr/bin/env python3
"""Prism/llama.cpp rates survive idle scrapes and independent metrics readers."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scout_harness import Checks, patched  # noqa: E402

import urllib.request  # noqa: E402
from caravan_scout.cells import ServerProbe  # noqa: E402

CHECKS = Checks("server metrics")
check = CHECKS.check


class Response:
    def __init__(self, text, start="1000"):
        self.body = text.encode()
        self.headers = {"Process-Start-Time-Unix": start} if start else {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def metrics(prompt=431, prompt_s=0.379305, gen=432, gen_s=3.15469, *, prompt_rate=0, gen_rate=0):
    # Actual metric names exported by prism-b10743-adfffbe; its gauges reset
    # on every scrape, while these four counters keep the completed work.
    return f"""# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total {prompt}
llamacpp:prompt_seconds_total {prompt_s}
llamacpp:tokens_predicted_total {gen}
llamacpp:tokens_predicted_seconds_total {gen_s}
llamacpp:prompt_tokens_seconds {prompt_rate}
llamacpp:predicted_tokens_seconds {gen_rate}
llamacpp:requests_processing 0
"""


def test_prism_idle_and_new_work():
    CHECKS.section("PrismML: idle scrapes and new work:")
    clock = [1000.0]
    body = [metrics()]
    calls = []

    def answer(url, **_kw):
        calls.append(url)
        return Response(json.dumps({"n_ctx": 262144}) if url.endswith("/props") else body[0])

    probe = ServerProbe(clock=lambda: clock[0])
    with patched(urllib.request, urlopen=answer):
        first = probe.metrics(22013)
        check(first == {"promptTps": 1136.29, "genTps": 136.94, "requestsProcessing": 0, "ctxMax": 262144},
              f"zero gauges recover the accumulated token/compute-time average on first scrape: {first}")
        clock[0] += 1
        check(probe.metrics(22013) == first and len(calls) == 2, "two-second cache avoids another destructive scrape")
        clock[0] += 100
        check(probe.metrics(22013) == first, "idle scrapes retain the measured speed instead of replacing it with zero")
        body[0] = metrics(prompt=531, prompt_s=0.879305, gen=482, gen_s=3.55469)
        clock[0] += 100
        newer = probe.metrics(22013)
        check(newer.get("promptTps") == 200 and newer.get("genTps") == 125,
              f"new work uses deltas of compute seconds, excluding the 100 seconds of idle time: {newer}")
        clock[0] += 3
        check(probe.metrics(22013) == newer, "the new observed speed is retained until more work arrives")


def test_gauges_and_ports():
    CHECKS.section("valid gauges and independent cells:")
    clock = [1000.0]
    body = [metrics(prompt_rate=104.75, gen_rate=138.14)]

    def answer(url, **_kw):
        if url.endswith("/props"):
            return Response("{}")
        return Response(body[0] if ":22013/" in url else metrics(prompt=0, prompt_s=0, gen=0, gen_s=0))

    probe = ServerProbe(clock=lambda: clock[0])
    with patched(urllib.request, urlopen=answer):
        first = probe.metrics(22013)
        check(first.get("genTps") == 138.14 and first.get("promptTps") == 104.75,
              "nonzero server gauges keep precedence over the accumulated average")
        body[0] = metrics()
        clock[0] += 3
        check(probe.metrics(22013) == first, "idle keeps the valid gauge observed at the previous scrape")
        other = probe.metrics(22001)
        check(other.get("genTps") == 0 and other.get("promptTps") == 0, "a cell with no completed work keeps zero; ports do not share speeds")


def test_restart_and_absence():
    CHECKS.section("process restarts and unavailable samples:")
    clock = [1000.0]
    body, start = [metrics()], ["1000"]
    failed = [False]

    def answer(url, **_kw):
        if url.endswith("/props"):
            return Response("{}")
        if failed[0]:
            raise OSError("unavailable")
        return Response(body[0], start[0])

    probe = ServerProbe(clock=lambda: clock[0])
    with patched(urllib.request, urlopen=answer):
        probe.metrics(22013)
        body[0] = metrics(prompt=0, prompt_s=0, gen=0, gen_s=0)
        clock[0] += 3
        empty = probe.metrics(22013)
        check(empty.get("genTps") == 0 and empty.get("promptTps") == 0, "counter decrease discards the old process's rates")
        body[0] = metrics()
        clock[0] += 3
        probe.metrics(22013)
        # A replacement can have larger counters before we notice the restart.
        start[0] = "2000"
        body[0] = metrics(prompt=1000, prompt_s=10, gen=1000, gen_s=20)
        clock[0] += 3
        replacement = probe.metrics(22013)
        check(replacement.get("genTps") == 50 and replacement.get("promptTps") == 100,
              "process-start header detects a replacement even when its counters increased")
        failed[0] = True
        clock[0] += 3
        check(probe.metrics(22013) == {}, "network failure emits no fake speed or stale rate")
        failed[0] = False
        body[0] = "llamacpp:requests_processing 0\nllamacpp:prompt_tokens_total 1000\n"
        clock[0] += 3
        missing = probe.metrics(22013)
        check("genTps" not in missing and "promptTps" not in missing, "missing token/time pairs do not invent a rate")
        body[0] = metrics(prompt_s="NaN", gen_s="Inf", prompt_rate="NaN", gen_rate="-Inf")
        clock[0] += 3
        invalid = probe.metrics(22013)
        check("genTps" not in invalid and "promptTps" not in invalid, "nonfinite readings do not reach the report")


def test_labels_and_old_servers():
    CHECKS.section("label aggregation and older servers:")
    body = metrics(prompt=100, prompt_s=2, gen=100, gen_s=4)
    counters = """llamacpp:prompt_tokens_total{engine="1"} 50
llamacpp:prompt_seconds_total{engine="1"} 1
llamacpp:tokens_predicted_total{engine="1"} 20
llamacpp:tokens_predicted_seconds_total{engine="1"} 2
"""

    def answer(url, **_kw):
        return Response("{}" if url.endswith("/props") else body, start="")

    with patched(urllib.request, urlopen=answer):
        body += counters
        sample = ServerProbe().metrics(22013)
        check(sample.get("promptTps") == 50 and sample.get("genTps") == 20, "labeled token counts and compute durations are summed together")
        body = "llamacpp:prompt_tokens_seconds 123.456\nllamacpp:predicted_tokens_seconds 42.125\n"
        older = ServerProbe().metrics(22013)
        check(older == {"promptTps": 123.46, "genTps": 42.12}, "gauge-only llama.cpp builds keep their existing behavior")


for test in (test_prism_idle_and_new_work, test_gauges_and_ports, test_restart_and_absence, test_labels_and_old_servers):
    test()
CHECKS.finish()
