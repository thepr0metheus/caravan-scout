"""Saved launch parameters of llama cells, kept by hand."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from caravan_scout.errors import AppError


class SavedConfigs:
    """Snapshots of a llama cell's launch parameters, saved on request only —
    never automatically on start — as llama-node.bak.<stamp>.json in a
    directory next to the scout's state."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def save(self, model_path: str, port: int, gpu_layers: int, ctx_size: int) -> None:
        """Save a timestamped JSON backup of the launch parameters."""
        self.dir.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        model_name = Path(model_path).name
        data = {
            "savedAt": stamp,
            "savedAtTs": now,
            "modelPath": model_path,
            "modelName": model_name,
            "port": port,
            "gpuLayers": gpu_layers,
            "ctxSize": ctx_size,
        }
        filename = f"llama-node.bak.{stamp}.json"
        target = self.dir / filename
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)

    def listing(self) -> list[dict[str, Any]]:
        """Return saved launch configs, newest first (max 20)."""
        if not self.dir.is_dir():
            return []
        rows = []
        for p in sorted(self.dir.glob("llama-node.bak.*.json"), reverse=True)[:20]:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                data["filename"] = p.name
                rows.append(data)
            except Exception:
                continue
        return rows

    def delete(self, filename: str) -> None:
        """Delete a saved config backup by filename (no path traversal)."""
        filename = Path(filename).name  # strip any path components
        if not filename.startswith("llama-node.bak.") or not filename.endswith(".json"):
            raise AppError("invalid backup filename", 400)
        target = self.dir / filename
        if not target.exists():
            raise AppError(f"backup not found: {filename}", 404)
        target.unlink()
