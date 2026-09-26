"""The key a cell answers to (2.20).

Every cell — a llama-server, a vLLM, a caravan cell server — is to answer
only the caravan's proxy: the controller derives one key per port and sends
it with the start as `cellKey`. This scout sets it on the process under the
name each server reads, presents it on its own requests to the cell, and
keeps it out of everything a person or the board reads: the start script,
the cell's JSON, the report and the log tails.

A controller older than keys sends none, and the cell starts open, as before.
"""
from __future__ import annotations

import re
from typing import Any

from caravan_scout.errors import AppError


class CellKey:
    """One cell's key, or none."""

    #: The name each server reads its key from: llama-server, vLLM, the
    #: caravan's own cell servers. Every cell gets all three — each server
    #: reads its own, the others mean nothing to it.
    ENV = ("LLAMA_API_KEY", "VLLM_API_KEY", "CARAVAN_CELL_KEY")
    #: What a key may be: a token a header carries as it is.
    SHAPE = re.compile(r"[A-Za-z0-9_\-]{16,128}")

    def __init__(self, value: str | None = None):
        self.value = value or None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CellKey:
        """The key a start request carries; none when it carries none.
        Refused when it is not a token — it goes into a header and into the
        process's environment as it is."""
        raw = payload.get("cellKey")
        if raw is None or raw == "":
            return cls()
        if not isinstance(raw, str) or not cls.SHAPE.fullmatch(raw):
            raise AppError("cellKey must be 16-128 letters, digits, '_' or '-'", 400)
        return cls(raw)

    @classmethod
    def of_env(cls, env: dict[str, Any] | None) -> CellKey:
        """The key a launch carried, read back from the environment this scout
        gave it — all a record kept across a scout restart has."""
        # Any of the names would do: this scout sets them together.
        value = (env or {}).get(cls.ENV[-1])
        return cls(value if isinstance(value, str) and cls.SHAPE.fullmatch(value) else None)

    def env(self) -> dict[str, str]:
        """The process's environment for the key: every name, or nothing."""
        return {name: self.value for name in self.ENV} if self.value else {}

    def headers(self) -> dict[str, str]:
        """What this scout's own requests to the cell carry."""
        return {"Authorization": f"Bearer {self.value}"} if self.value else {}

    @classmethod
    def public(cls, env: dict[str, str] | None) -> dict[str, str]:
        """`env` without the key: what a file a person reads may say."""
        return {k: v for k, v in (env or {}).items() if k not in cls.ENV}
