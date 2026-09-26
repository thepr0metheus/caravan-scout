"""A file only its owner reads (2.20).

config.json holds the fleet token; state.json holds the start requests and
launches of the cells, and with them the cells' keys. Written with the
machine's umask (002 on the fleet's machines), both were 0664 — readable by
every user of the machine.
"""
from __future__ import annotations

import os
from pathlib import Path


class PrivateFile:
    """Written whole — a temp file renamed over it, so a reader sees the old
    file or the new one — and 0600 from its first byte."""

    MODE = 0o600

    def __init__(self, path):
        self.path = Path(path)

    def write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, self.MODE)
        try:
            # A temp file left over from a crash keeps its old mode through
            # O_CREAT: set it on the open file, before the text goes in.
            os.fchmod(fd, self.MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = None
                fh.write(text)
        finally:
            if fd is not None:
                os.close(fd)
        tmp.replace(self.path)
