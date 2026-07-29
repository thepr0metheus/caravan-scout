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

# launcher stem -> the runner key the controller's manifest uses. A LAST RESORT,
# used only when the manifest cannot be read: the controller publishes the same
# mapping in `runners`, and resolving it from there means a runner added on the
# controller reaches every client without a scout release.
#
# It was the hardcoded copy that bit us. `transcribe` shipped on the controller
# and was never added here, so `runner_for_command` returned "" for every
# transcribe cell, sync_for_command left on its first line, and the client ran
# whatever a human had once copied in — for four days, with a green board and
# not one line in the journal.
_LAUNCHER_RUNNER = {
    "moonshine": "moonshine",
    "whisper": "whisper",
    "tts": "custom",
    "transcribe": "transcribe",
}


def _launcher_stem(command: str) -> str:
    m = _LAUNCHER_RE.search(str(command or ""))
    return m.group(1) if m else ""


def _runner_from_manifest(stem: str, manifest: dict) -> str:
    """Which runner owns run_<stem>.sh, according to the controller itself."""
    launcher = f"run_{stem}.sh"
    for runner, names in (manifest.get("runners") or {}).items():
        if launcher in (names or []):
            return str(runner)
    return ""


def _digest(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def runner_for_command(command: str) -> str:
    """Which runner a command line belongs to, or "" when it names no launcher
    of ours (a bare custom command, a python one-liner — nothing to sync)."""
    return _LAUNCHER_RUNNER.get(_launcher_stem(command), "")


def sync_for_command(command: str, controller_url: str, headers: dict,
                     home: str | None = None, log=None, timeout: int = 10) -> dict:
    """Bring $HOME's copies of this command's cell files up to the controller's.

    Returns {name: "current"|"updated"|"kept: <why>"} — never raises.
    """
    say = log or (lambda _m: None)
    out: dict[str, str] = {}
    stem = _launcher_stem(command)
    if not stem:
        return out          # names no launcher of ours — genuinely nothing to sync
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

    # Ask the controller which runner owns this launcher before falling back to
    # the table above. The controller is the side that gains runners; a client
    # that trusts its own table is a client that stops syncing the moment one is
    # added, and says nothing while it does.
    runner = _runner_from_manifest(stem, manifest) or _LAUNCHER_RUNNER.get(stem, "")
    if not runner:
        say(f"cell-assets: run_{stem}.sh belongs to no runner the controller "
            f"publishes — keeping local copies (upgrade the controller, or this "
            f"cell runs whatever is already in $HOME)")
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
