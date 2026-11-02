#!/usr/bin/env python3
"""Prism/llama.cpp rates survive idle scrapes and independent metrics readers; a Strata cell's speed, queue and context use are read (2.35.1).

On 2026-10-18 the owner talked to a local model through a chat page and found the machine's token speed at zero: "prompt 0 / gen 0 t/s". The model
ran in a Strata cell. The scout read a cell's /metrics as Prometheus text, and Strata answers Prometheus text only to a reader that asks for it (`Accept:
text/plain`) — asked without, it answers the JSON of its Monitor tab, where the reader found no sample at all. The cell said no speed, no queue, no
context use. Pinned here, each with its negative: that the cell is asked the way Prometheus asks; the speeds at the first scrape (the accumulated
average, the prompt tokens its cache already held left out); the running request's own speeds while it works; the last measured speed held while idle;
the new work since the last scrape; the queue and the context use; no rate invented from a sample the server does not say; a restart; a JSON answer;
a server that answers it keeps no metrics (404, 501 — llama.cpp started without --metrics) says so, and one loading or silent says nothing (2.41).
"""
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


def where(url):
    """The address a stub of urlopen was asked for: the metrics are asked with a Request (an Accept header), the props with a string."""
    return getattr(url, "full_url", url)


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
        url = where(url)
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
        url = where(url)
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
        url = where(url)
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
        return Response("{}" if where(url).endswith("/props") else body, start="")

    with patched(urllib.request, urlopen=answer):
        body += counters
        sample = ServerProbe().metrics(22013)
        check(sample.get("promptTps") == 50 and sample.get("genTps") == 20, "labeled token counts and compute durations are summed together")
        body = "llamacpp:prompt_tokens_seconds 123.456\nllamacpp:predicted_tokens_seconds 42.125\n"
        older = ServerProbe().metrics(22013)
        check(older == {"promptTps": 123.46, "genTps": 42.12}, "gauge-only llama.cpp builds keep their existing behavior")


def strata(prompt=86650, hits=38423, gen=8111, prompt_s=18.053, decode_s=38.392, *, running=0, waiting=0, live_gen=0, live_prompt=0, ratio=0.0):
    """What a Strata server answers to a reader that asks for text/plain (its names as `/metrics` gave them on 2026-10-18)."""
    return f"""# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{{model_name="m"}} {running}
vllm:num_requests_waiting{{model_name="m"}} {waiting}
vllm:kv_cache_usage_perc{{model_name="m"}} {ratio}
vllm:prompt_tokens_total{{model_name="m"}} {prompt}
vllm:generation_tokens_total{{model_name="m"}} {gen}
vllm:prefix_cache_hits_total{{model_name="m"}} {hits}
strata:live_state{{model_name="m",state="idle"}} {int(not running)}
strata:live_state{{model_name="m",state="generating"}} {int(bool(running))}
strata:live_tok_s{{model_name="m"}} {live_gen}
strata:live_prefill_tok_s_mean{{model_name="m"}} {live_prompt}
strata:totals_prompt_seconds_total{{model_name="m"}} {prompt_s}
strata:totals_decode_seconds_total{{model_name="m"}} {decode_s}
"""


def test_strata_speed():
    CHECKS.section("a Strata cell: speed, queue, context use:")
    clock = [1000.0]
    body = [strata()]
    asked = []

    def answer(req, **_kw):
        url = where(req)
        asked.append((url, dict(req.header_items()) if hasattr(req, "header_items") else None))
        return Response(json.dumps({"default_generation_settings": {"n_ctx": 131072}}) if url.endswith("/props") else body[0], start="")

    probe = ServerProbe(clock=lambda: clock[0])
    with patched(urllib.request, urlopen=answer):
        first = probe.metrics(22028)
        check(asked[0] == ("http://127.0.0.1:22028/metrics", {"Accept": "text/plain"}),
              f"the cell is asked the way Prometheus asks — Strata answers text only then, otherwise the JSON of its Monitor tab: {asked[0]}")
        check(first == {"requestsProcessing": 0, "requestsWaiting": 0, "promptTps": 2671.41, "genTps": 211.27, "ctxMax": 131072, "ctxUsed": 0},
              f"first scrape, idle: the accumulated average — the prompt tokens its cache already held (38423 of 86650) are not read ones, so "
              f"48227 over 18.053 s, 8111 tokens over 38.392 s; nobody working, the queue empty, no context in use: {first}")
        clock[0] += 3
        check(probe.metrics(22028) == first, "an idle scrape holds what was measured instead of replacing it with zero")

        body[0] = strata(running=1, live_gen=188.4, live_prompt=4100.55, ratio=0.0125)
        clock[0] += 3
        live = probe.metrics(22028)
        check(live.get("genTps") == 188.4 and live.get("promptTps") == 4100.55 and live.get("requestsProcessing") == 1 and live.get("ctxUsed") == 1638,
              f"a request at work: its own speeds, one in work, its share of the context (0.0125 of 131072 = 1638): {live}")

        body[0] = strata(prompt=87650, hits=39023, gen=8411, prompt_s=18.253, decode_s=39.892)
        clock[0] += 3
        done = probe.metrics(22028)
        check(done.get("promptTps") == 2000.0 and done.get("genTps") == 200.0 and done.get("requestsProcessing") == 0 and done.get("ctxUsed") == 0,
              f"the request is done: the speed of the new work only — 400 tokens read (1000 less 600 cached) in 0.2 s, 300 generated in 1.5 s — "
              f"not the growth of the counters over the 3 s between scrapes (that would say 133 and 100): {done}")
        clock[0] += 60
        check(probe.metrics(22028) == done, "and held while the cell idles, a minute later the same")

        body[0] = strata(prompt=87650, hits=39023, gen=8411, prompt_s=18.253, decode_s=39.892, waiting=2)
        clock[0] += 3
        check(probe.metrics(22028).get("requestsWaiting") == 2, "the queue is said as it is")

        body[0] = strata(prompt=100, hits=100, gen=0, prompt_s=0, decode_s=0)
        clock[0] += 3
        restarted = probe.metrics(22028)
        check("promptTps" not in restarted and "genTps" not in restarted,
              f"negative: counters that went down are a restarted cell: the old rates are dropped, and a new one has measured nothing yet — no speed, "
              f"not a zero and not the old one: {restarted}")

        body[0] = strata(prompt=100, hits=150, gen=10, prompt_s=1.0, decode_s=1.0)
        other = ServerProbe(clock=lambda: clock[0]).metrics(22029)
        check(other.get("promptTps") == 0.0 and other.get("genTps") == 10.0,
              f"boundary: more tokens held by the cache than were asked for (a server that miscounts) — the read ones are none, the speed 0 and never "
              f"a negative one: {other}")

        body[0] = "vllm:prompt_tokens_total 500\nstrata:live_tok_s 0\n"
        clock[0] += 3
        partial = probe.metrics(22028)
        check("promptTps" not in partial and "genTps" not in partial,
              f"negative: a server that does not say its compute seconds gets no rate invented from its token counts: {partial}")

        body[0] = json.dumps({"engine": {"model": "m"}, "live": {"state": "idle", "tok_s": 0}, "totals": {"requests": 24, "output_tokens": 8111}})
        clock[0] += 3
        old = probe.metrics(22028)
        check(old == {"ctxMax": 131072}, f"negative: the JSON of the Monitor tab (a server that was not asked for text) says nothing here: {old}")

    plain = "vllm:prompt_tokens_total 1000\nvllm:generation_tokens_total 400\nvllm:num_requests_running 1\n"
    seen = [plain]

    def vllm(req, **_kw):
        url = where(req)
        if url.endswith("/props"):
            raise OSError("404")
        return Response(seen[0], start="")
    probe = ServerProbe(clock=lambda: clock[0])
    with patched(urllib.request, urlopen=vllm):
        probe.metrics(22012)
        seen[0] = "vllm:prompt_tokens_total 1600\nvllm:generation_tokens_total 520\nvllm:num_requests_running 1\n"
        clock[0] += 4
        grown = probe.metrics(22012)
        check(grown.get("promptTps") == 150.0 and grown.get("genTps") == 30.0,
              f"a vLLM server (no `strata:` sample) is still read by the growth of its counters per second: {grown}")


def test_refused():
    CHECKS.section("a server that keeps no metrics says so (2.41):")
    import io
    import urllib.error
    clock = [1000.0]
    status = [501]

    def answer(url, **_kw):
        url = where(url)
        if url.endswith("/props"):
            return Response(json.dumps({"n_ctx": 131072}))
        if status[0] is None:
            raise OSError("connection refused")
        raise urllib.error.HTTPError(url, status[0], "no metrics", {}, io.BytesIO(b"Start it with --metrics"))

    probe = ServerProbe(clock=lambda: clock[0])
    with patched(urllib.request, urlopen=answer):
        check(probe.metrics(22020) == {"noMetrics": True, "ctxMax": 131072},
              "llama.cpp started without --metrics answers 501: the reading says the server keeps none, beside its window")
        status[0] = 404
        clock[0] += 3
        check(probe.metrics(22020) == {"noMetrics": True, "ctxMax": 131072}, "a server without the path answers 404: the same")
        status[0] = 503
        clock[0] += 3
        check(probe.metrics(22020) == {"ctxMax": 131072},
              "negative: 503 (a model still loading) is not \"keeps none\" — it says nothing")
        status[0] = None
        clock[0] += 3
        check(probe.metrics(22020) == {"ctxMax": 131072}, "negative: a server that does not answer says nothing either")


for test in (test_prism_idle_and_new_work, test_gauges_and_ports, test_restart_and_absence, test_labels_and_old_servers, test_strata_speed,
             test_refused):
    test()
sys.exit(CHECKS.finish())
