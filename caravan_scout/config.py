"""The scout's config.json: what it was set up with, and the pairing that
points it at a controller."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from caravan_scout.errors import AppError
from caravan_scout.paths import DEFAULT_CONFIG


class ScoutConfig:
    """config.json, read once and merged over DEFAULT_CONFIG.

    Read like a dict (get, []); its writers are pair() and unpair(), the only
    things that change the file on disk.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data = self.load()

    def load(self) -> dict[str, Any]:
        config = DEFAULT_CONFIG.copy()
        # What the file itself says, apart from the defaults: a hostId or a
        # displayName written there is the operator's choice (HostIdentity).
        self._file: dict[str, Any] = {}
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise AppError("config must be a JSON object")
            config.update(payload)
            self._file = dict(payload)
        config["listenPort"] = int(config.get("listenPort") or 8092)
        config["heartbeatIntervalSeconds"] = max(2, int(config.get("heartbeatIntervalSeconds") or 60))
        return config

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def from_file(self, key: str) -> Any:
        """The value config.json itself holds for `key` — None when only a
        default supplies it."""
        return self._file.get(key)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def token(self) -> str:
        return str(self.data.get("controllerToken") or "").strip()

    def headers(self) -> dict[str, str]:
        token = self.token()
        return {"X-Caravan-Token": token} if token else {}

    @staticmethod
    def controller_address(raw_url: str) -> str:
        """A controller address as it is stored: with a scheme, without the
        trailing slash. AppError when it is no address at all."""
        url = str(raw_url or "").strip()
        if url and "://" not in url:
            url = "http://" + url
        parsed = urlparse(url)
        # Checked BEFORE the trailing slash goes: "http://" stripped first was
        # "http:", which gained a second "http://" and passed as the host
        # "http" — a pairing with nothing, saved as if it worked.
        if not url or parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise AppError("controller url must look like http://host:7990")
        return url.rstrip("/")

    def pair(self, raw_url: str, raw_token: str = "") -> str:
        """Point the scout at a controller: validate the address, write it (and
        a token, when one is given) into config.json — the raw file, not the
        merged defaults, which do not belong in the user's file — and take it
        into the running config. Returns the address as stored."""
        url = self.controller_address(raw_url)
        raw = self._raw()
        raw["controllerUrl"] = url
        token = str(raw_token or "").strip()
        if token:
            raw["controllerToken"] = token
        self._write(raw)
        with self._lock:
            self.data["controllerUrl"] = url
            if token:
                self.data["controllerToken"] = token
        return url

    def unpair(self) -> None:
        """Forget the controller: its address and its token leave config.json
        and the running config. The machine, its cells and the rest of the file
        stay; the scout waits to be paired again — by any controller, since
        without a token nothing is closed (a trusted LAN, as on a fresh
        install)."""
        raw = self._raw()
        raw.pop("controllerUrl", None)
        raw.pop("controllerToken", None)
        self._write(raw)
        with self._lock:
            self.data["controllerUrl"] = ""
            self.data.pop("controllerToken", None)

    def _raw(self) -> dict[str, Any]:
        """config.json as the user's file holds it — without the defaults,
        which do not belong in it. {} when it is missing or unreadable."""
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    return loaded
            except Exception:
                pass
        return {}

    def _write(self, raw: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
