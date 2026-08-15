#!/usr/bin/env python3
"""
Apply routing assignments from caravan-scout.

Reads JSON from stdin (sent by the route agent when /api/routing/apply is called):
  {"hostId": "...", "assignments": [{"agentId": "...", "routes": [...]}],
   "agents": [{"id","runtime","endpoint",...}], "time": ...}

For each assignment, point the agent's openclaw at its assigned LAMA CARAVAN proxy:
  1. Locate the agent's openclaw config (host file / docker path / over SSH for a VM).
  2. Sync the declared primary/fallback provider's baseUrl to the proxy endpoint.
     If the agent declares no primary provider (a freshly-created agent), create a
     `lama-caravan` provider and make it the default model.
  3. Write the config atomically.
  4. Restart the agent: `docker restart agent-<id>` (docker) or
     `systemctl restart openclaw-<id>` over SSH (vm). Host agents are not restarted here.

Agent metadata comes from the payload's "agents" (registry-sourced) and falls back to
config.json next to this script.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

DOCKER_AGENTS_BASE = Path.home() / "docker/agents/data"
HOST_OPENCLAW_CONFIG = Path.home() / ".openclaw/openclaw.json"
# Which agent id owns the host's own config; matches the scout's openclawAgentId.
HOST_AGENT_ID = os.environ.get("APPLY_ROUTES_HOST_AGENT_ID", "openclaw")
DOCKER_CONTAINER_PREFIX = "agent-"
VM_SSH_KEY = Path.home() / ".ssh/id_ed25519_vm"
VM_SSH_USER = os.environ.get("APPLY_ROUTES_VM_SSH_USER", os.environ.get("USER", ""))
VM_OPENCLAW_CONFIG = os.environ.get("APPLY_ROUTES_VM_OPENCLAW_CONFIG", "~/.openclaw/openclaw.json")

# Minimal model entry used when wiring a fresh agent to a new lama-caravan provider.
# Schema mirrors a known-good live agent (no contextTokens / provider timeoutSeconds — the
# openclaw gateway rejects those as "models: Invalid input").
LAMA_CARAVAN_MODEL = {
    "id": "main-model",
    "name": "main-model",
    "contextWindow": 100000,
    "maxTokens": 4096,
    "input": ["text", "image"],
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    "reasoning": False,
}


def provider_id(ref: str) -> str:
    return ref.split("/")[0] if ref else ""


def update_config_obj(config: dict, routes: dict[str, str]) -> bool:
    """Sync provider baseUrl(s) to the proxy endpoint(s), in-memory. Returns True if changed.

    Existing agents (that declare agents.defaults.model.primary) keep their provider and only
    get its baseUrl re-pointed. A fresh agent (no declared primary) is wired to a new
    `lama-caravan` provider which is also set as the default model."""
    models = config.setdefault("models", {})
    providers = models.setdefault("providers", {})
    raw_model = ((config.get("agents") or {}).get("defaults") or {}).get("model")

    # agents.defaults.model has two valid shapes: the dict {primary, fallbacks}
    # and the plain-string shorthand "provider/model". Every live config in this
    # fleet uses the string, so assuming the dict killed this script on its first
    # line of real work with "'str' object has no attribute 'get'" — and because
    # a dead applier looks exactly like an applier with nothing to do, the routes
    # it silently never delivered went unnoticed from 2026-07-20 to 2026-08-15.
    # The scout's own reader guards this with isinstance; this one did not.
    if isinstance(raw_model, str):
        defaults_model = {"primary": raw_model, "fallbacks": []}
    elif isinstance(raw_model, dict):
        defaults_model = raw_model
    else:
        defaults_model = {}

    role_to_provider: dict[str, str] = {}
    primary_ref = str(defaults_model.get("primary") or "")
    if primary_ref:
        role_to_provider["primary"] = provider_id(primary_ref)
    for fb in (defaults_model.get("fallbacks") or [])[:1]:
        role_to_provider["fallback"] = provider_id(str(fb))

    fresh = "primary" not in role_to_provider
    if fresh:
        role_to_provider["primary"] = "lama-caravan"

    changed = False
    for role, endpoint in routes.items():
        prov = role_to_provider.get(role)
        if not prov:
            print(f"  [warn] no provider for role={role}, skipping", file=sys.stderr)
            continue
        p = providers.get(prov)
        if not isinstance(p, dict):
            providers[prov] = p = {
                "api": "openai-completions",
                "apiKey": "local-llamacpp",
                "baseUrl": "",
                "models": [dict(LAMA_CARAVAN_MODEL)],
                "request": {"allowPrivateNetwork": True},
            }
            print(f"  created provider {prov!r}")
            changed = True
        if p.get("baseUrl") != endpoint:
            print(f"  {role}: {prov} {p.get('baseUrl','')!r} -> {endpoint!r}")
            p["baseUrl"] = endpoint
            changed = True
        else:
            print(f"  {role}: {prov} unchanged ({endpoint!r})")

    if fresh and changed:
        # Select the model via agents.defaults.model.primary (the schema-valid way);
        # a top-level models.default is rejected by the gateway ("models: Invalid input").
        config.setdefault("agents", {}).setdefault("defaults", {}).setdefault(
            "model", {})["primary"] = "lama-caravan/main-model"
        print("  set default model -> lama-caravan/main-model (fresh agent)")

    return changed


# ---- host / docker: local config file --------------------------------------------

def resolve_local_config_path(agent: dict) -> Path | None:
    explicit = str(agent.get("openclawConfigPath") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if agent.get("runtime") == "docker":
        p = DOCKER_AGENTS_BASE / str(agent.get("id") or "") / ".openclaw" / "openclaw.json"
        return p if p.exists() else None
    # The host's own ~/.openclaw/openclaw.json belongs to exactly ONE agent. It
    # used to be the fallback for anything not docker, so an agent whose
    # metadata failed to arrive — a VM whose registry entry is stale, which is
    # the fleet's normal condition — silently rewrote the HOST agent's endpoint
    # instead of its own. Observed live: applying cerberus's route repointed
    # foreman's host agent. Refuse rather than guess.
    if str(agent.get("id") or "") == HOST_AGENT_ID and HOST_OPENCLAW_CONFIG.exists():
        return HOST_OPENCLAW_CONFIG
    return None


def apply_local(config_path: Path, routes: dict[str, str]) -> bool:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    changed = update_config_obj(config, routes)
    if changed:
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(config_path)
    return changed


def restart_docker(agent_id: str) -> None:
    container = f"{DOCKER_CONTAINER_PREFIX}{agent_id}"
    print(f"  restarting {container}...")
    r = subprocess.run(["docker", "restart", container], capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        print(f"  {container} restarted ok")
    else:
        print(f"  [warn] restart {container} failed: {r.stderr.strip()}", file=sys.stderr)


# The gateway reads its config at startup, so an agent that is not restarted
# keeps serving the OLD baseUrl however carefully the file was rewritten. Only
# docker was ever restarted here: "host" fell through silently and "launchd"
# (macOS, what crab reports) matched no branch at all, so the Mac agent had
# never once been restarted by this script.
def restart_local(agent_id: str, runtime: str) -> None:
    if runtime == "docker":
        restart_docker(agent_id)
        return
    if runtime == "launchd":
        label = os.environ.get("APPLY_ROUTES_LAUNCHD_LABEL", "ai.openclaw.gateway")
        cmd = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"]
    else:                                    # host: systemd user unit
        cmd = ["systemctl", "--user", "restart",
               os.environ.get("APPLY_ROUTES_HOST_UNIT", "openclaw-gateway")]
    print(f"  restarting {agent_id} ({runtime}): {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except Exception as exc:
        print(f"  [warn] restart {agent_id} failed: {exc}", file=sys.stderr)
        return
    if r.returncode == 0:
        print(f"  {agent_id} restarted ok")
    else:
        print(f"  [warn] restart {agent_id} failed: {r.stderr.strip()}", file=sys.stderr)


# ---- vm: openclaw config lives on the guest, reached over SSH ---------------------

def vm_ip(agent: dict) -> str:
    endpoint = str(agent.get("endpoint") or agent.get("url") or "")
    match = re.search(r"//([0-9.]+)", endpoint)
    if match:
        return match.group(1)
    return str(agent.get("host") or "").strip()


def _ssh(ip: str, args: list[str], input_text: str | None = None, timeout: int = 25):
    cmd = ["ssh", "-i", str(VM_SSH_KEY), "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=8", f"{VM_SSH_USER}@{ip}"] + args
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout)


def apply_vm(agent: dict, routes: dict[str, str]) -> bool:
    ip = vm_ip(agent)
    agent_id = str(agent.get("id") or "")
    if not ip:
        raise RuntimeError("no VM ip resolvable from endpoint/host")
    read = _ssh(ip, ["cat", VM_OPENCLAW_CONFIG], timeout=20)
    if read.returncode != 0:
        err = read.stderr.strip()
        # accept-new adds an UNKNOWN host; it refuses a host whose key CHANGED,
        # which is what a rebuilt VM looks like. The bare "Host key verification
        # failed" gives an operator nothing to act on, so name the remedy.
        if "host key verification failed" in err.lower() or "REMOTE HOST IDENTIFICATION" in err:
            raise RuntimeError(
                f"ssh {ip}: host key changed since it was first seen (rebuilt VM?). "
                f"Verify the host, then clear the stale entry: ssh-keygen -R {ip}")
        raise RuntimeError(f"ssh read {ip}: {err}")
    config = json.loads(read.stdout)
    changed = update_config_obj(config, routes)
    if not changed:
        return False
    data = json.dumps(config, ensure_ascii=False, indent=2)
    write = _ssh(ip, ["bash", "-c", f"cat > {VM_OPENCLAW_CONFIG}.tmp && mv {VM_OPENCLAW_CONFIG}.tmp {VM_OPENCLAW_CONFIG}"],
                 input_text=data, timeout=20)
    if write.returncode != 0:
        raise RuntimeError(f"ssh write {ip}: {write.stderr.strip()}")
    # The guests run ONE user unit, openclaw-gateway — not a system unit named
    # after the agent. `sudo systemctl restart openclaw-<id>.service` therefore
    # failed on every VM in the fleet; it only ever printed a warning, so the
    # config landed and the agent went on using the previous baseUrl.
    unit = os.environ.get("APPLY_ROUTES_VM_UNIT", "openclaw-gateway")
    restart = _ssh(ip, ["systemctl", "--user", "restart", unit], timeout=40)
    if restart.returncode != 0:
        legacy = _ssh(ip, ["sudo", "-n", "systemctl", "restart",
                           f"openclaw-{agent_id}.service"], timeout=40)
        if legacy.returncode == 0:
            print(f"  restarted openclaw-{agent_id} on {ip} (legacy system unit)")
            return True
        print(f"  [warn] restart {unit} on {ip} failed: {restart.stderr.strip()}",
              file=sys.stderr)
        return True
    print(f"  restarted {unit} on {ip}")
    return True


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        print(f"[error] stdin parse failed: {exc}", file=sys.stderr)
        return 1

    # Agent metadata: prefer the payload's registry-sourced "agents", fall back to config.json.
    agent_meta: dict[str, dict] = {}
    for a in (payload.get("agents") or []):
        if isinstance(a, dict) and a.get("id"):
            agent_meta[str(a["id"])] = a
    cfg_path = Path(__file__).parent / "config.json"
    if cfg_path.exists():
        try:
            for a in (json.loads(cfg_path.read_text("utf-8")).get("agents") or []):
                if a.get("id"):
                    agent_meta.setdefault(str(a["id"]), a)
        except Exception as exc:
            print(f"[warn] could not read config.json: {exc}", file=sys.stderr)

    errors = 0
    for item in (payload.get("assignments") or []):
        agent_id = str(item.get("agentId") or "").strip()
        if not agent_id:
            continue
        routes = {
            r["role"]: r["endpoint"]
            for r in (item.get("routes") or [])
            if r.get("role") and r.get("endpoint")
        }
        if not routes:
            print(f"[{agent_id}] no routes, skipping")
            continue

        meta = agent_meta.get(agent_id, {"id": agent_id})
        runtime = str(meta.get("runtime") or "")
        try:
            if runtime == "vm":
                print(f"[{agent_id}] vm {vm_ip(meta)}")
                changed = apply_vm(meta, routes)
            else:
                ocpath = resolve_local_config_path(meta)
                if not ocpath or not ocpath.exists():
                    print(f"[{agent_id}] config not found (path={ocpath}), skipping")
                    continue
                print(f"[{agent_id}] {ocpath}")
                changed = apply_local(ocpath, routes)
                if changed:
                    restart_local(agent_id, runtime)
            if not changed:
                print(f"[{agent_id}] no changes")
        except Exception as exc:
            print(f"[{agent_id}] error: {exc}", file=sys.stderr)
            errors += 1

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
