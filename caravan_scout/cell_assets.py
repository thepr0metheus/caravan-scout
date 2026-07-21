"""Fetch the cell servers from the controller before a command cell starts.

The controller already tells us WHAT to run — it hands over a full command line
like `bash $HOME/run_moonshine.sh "$PORT" en`. It now also supplies the script
that line names. Before this, every host obtained those files on its own: a
client from its own clone of this repo, the controller from somebody copying a
file in by hand. Nothing compared the copies, so they drifted for months without
a single error — a client that had not pulled in a while quietly ran an old
cell server while the board showed it as current.

Failure here never blocks a start. A cell that cannot reach the controller, or
whose asset comes back malformed, runs whatever is already in $HOME: an
out-of-date cell is worth more than no cell. Every such fallback is logged with
the reason, because silence is exactly how the drift lasted this long.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request

# Launcher names as they appear in a command line, mapped to nothing in
# particular — the controller's manifest says which files each runner needs.
# We only need to spot WHICH launcher a command invokes.
_LAUNCHER_RE = re.compile(r"\brun_([a-z0-9_]+)\.sh\b")

# launcher stem -> the runner key the controller's manifest uses
_LAUNCHER_RUNNER = {
    "moonshine": "moonshine",
    "whisper": "whisper",
    "tts": "custom",
}


def _digest(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def runner_for_command(command: str) -> str:
    """Which runner a command line belongs to, or "" when it names no launcher
    of ours (a bare custom command, a python one-liner — nothing to sync)."""
    m = _LAUNCHER_RE.search(str(command or ""))
    if not m:
        return ""
    return _LAUNCHER_RUNNER.get(m.group(1), "")


def sync_for_command(command: str, controller_url: str, headers: dict,
                     home: str | None = None, log=None, timeout: int = 10) -> dict:
    """Bring $HOME's copies of this command's cell files up to the controller's.

    Returns {name: "current"|"updated"|"kept: <why>"} — never raises.
    """
    say = log or (lambda _m: None)
    out: dict[str, str] = {}
    runner = runner_for_command(command)
    if not runner:
        return out
    base = str(controller_url or "").rstrip("/")
    if not base:
        say("cell-assets: no controllerUrl — keeping local copies")
        return out
    home = home or os.path.expanduser("~")

    try:
        req = urllib.request.Request(f"{base}/api/cell-assets", headers=dict(headers or {}))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            manifest = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001
        say(f"cell-assets: manifest unavailable ({exc}) — keeping local copies")
        return out

    wanted = (manifest.get("runners") or {}).get(runner) or []
    assets = manifest.get("assets") or {}
    for name in wanted:
        meta = assets.get(name) or {}
        want_hash = str(meta.get("sha256") or "")
        dst = os.path.join(home, name)
        if want_hash and _digest(dst) == want_hash:
            out[name] = "current"
            continue
        try:
            url = f"{base}/api/cell-assets/file?name={urllib.parse.quote(name)}"
            req = urllib.request.Request(url, headers=dict(headers or {}))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
            got = hashlib.sha256(payload).hexdigest()
            if want_hash and got != want_hash:
                # A truncated or proxied-through-something body would otherwise
                # overwrite a working launcher with rubbish.
                out[name] = "kept: hash mismatch"
                say(f"cell-assets: {name} arrived with the wrong hash — keeping local copy")
                continue
            tmp = dst + ".new"
            with open(tmp, "wb") as fh:
                fh.write(payload)
            if meta.get("executable") or name.endswith(".sh"):
                os.chmod(tmp, 0o755)
            os.replace(tmp, dst)          # atomic: never a half-written launcher
            out[name] = "updated"
            say(f"cell-assets: {name} updated from controller")
        except Exception as exc:  # noqa: BLE001
            out[name] = f"kept: {exc}"
            say(f"cell-assets: {name} not fetched ({exc}) — keeping local copy")
    return out
