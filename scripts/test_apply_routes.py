#!/usr/bin/env python3
"""Tests for apply-routes.py config rewriting.

`agents.defaults.model` may be a dict OR the shorthand string "provider/model".
Only the dict was handled, so on every real config in this fleet the script died
with "'str' object has no attribute 'get'" before touching anything — and a
dead applier is indistinguishable from an applier with nothing to do. Routes
went undelivered for four weeks without a single alarm.

Run: python3 scripts/test_apply_routes.py
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("apply_routes", ROOT / "apply-routes.py")
apply_routes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_routes)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def config_with(model):
    return {
        "agents": {"defaults": {"model": model}},
        "models": {"providers": {"lama-caravan": {
            "api": "openai-completions",
            "baseUrl": "http://10.0.0.1:8101/v1",
        }}},
    }


NEW = "http://10.0.0.1:23101/v1"


def test_string_shorthand():
    cfg = config_with("lama-caravan/main-model")
    changed = apply_routes.update_config_obj(cfg, {"primary": NEW})
    check("string form is accepted", changed)
    check("string form repoints the declared provider",
          cfg["models"]["providers"]["lama-caravan"]["baseUrl"] == NEW,
          cfg["models"]["providers"])
    check("string form does not invent a second provider",
          list(cfg["models"]["providers"]) == ["lama-caravan"],
          list(cfg["models"]["providers"]))


def test_dict_form_unchanged():
    cfg = config_with({"primary": "lama-caravan/main-model", "fallbacks": []})
    changed = apply_routes.update_config_obj(cfg, {"primary": NEW})
    check("dict form still works", changed)
    check("dict form repoints the declared provider",
          cfg["models"]["providers"]["lama-caravan"]["baseUrl"] == NEW)


def test_idempotent():
    cfg = config_with("lama-caravan/main-model")
    apply_routes.update_config_obj(cfg, {"primary": NEW})
    again = apply_routes.update_config_obj(cfg, {"primary": NEW})
    check("re-applying the same endpoint reports no change", again is False, again)


def test_fresh_agent_still_wired():
    """No declared model at all: the script must create the provider, as before."""
    cfg = {"models": {"providers": {}}}
    changed = apply_routes.update_config_obj(cfg, {"primary": NEW})
    check("fresh agent gets a provider", changed)
    check("fresh provider carries the endpoint",
          cfg["models"]["providers"]["lama-caravan"]["baseUrl"] == NEW)
    check("fresh agent gets a default model",
          cfg["agents"]["defaults"]["model"]["primary"] == "lama-caravan/main-model",
          cfg.get("agents"))


def test_restart_dispatch_covers_every_runtime():
    """Every runtime the fleet reports must map to a real restart command."""
    seen = {}

    def fake_run(cmd, **kw):
        seen[fake_run.runtime] = cmd
        class R:
            returncode = 0
            stderr = ""
        return R()

    original = apply_routes.subprocess.run
    apply_routes.subprocess.run = fake_run
    try:
        for runtime in ("docker", "host", "launchd"):
            fake_run.runtime = runtime
            apply_routes.restart_local("cerberus", runtime)
    finally:
        apply_routes.subprocess.run = original

    check("docker restarts the container",
          "docker" in seen and seen["docker"][:2] == ["docker", "restart"], seen.get("docker"))
    check("host restarts the systemd user unit",
          "host" in seen and seen["host"][:3] == ["systemctl", "--user", "restart"], seen.get("host"))
    check("launchd restarts via launchctl (macOS was never restarted at all)",
          "launchd" in seen and seen["launchd"][:3] == ["launchctl", "kickstart", "-k"],
          seen.get("launchd"))


def test_host_config_is_not_a_fallback():
    """An agent with no metadata must not inherit the host agent's config file."""
    stray = apply_routes.resolve_local_config_path({"id": "cerberus"})
    check("unknown agent resolves to no local config", stray is None, stray)
    host = apply_routes.resolve_local_config_path({"id": apply_routes.HOST_AGENT_ID})
    check("the host agent still resolves to the host config",
          host is None or host == apply_routes.HOST_OPENCLAW_CONFIG, host)
    explicit = apply_routes.resolve_local_config_path(
        {"id": "whatever", "openclawConfigPath": "/tmp/x.json"})
    check("an explicit path still wins", str(explicit) == "/tmp/x.json", explicit)


def main():
    print("apply-routes:")
    test_string_shorthand()
    test_dict_form_unchanged()
    test_idempotent()
    test_fresh_agent_still_wired()
    test_restart_dispatch_covers_every_runtime()
    test_host_config_is_not_a_fallback()
    if FAILURES:
        print(f"\n{len(FAILURES)} failed: {', '.join(FAILURES)}")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
