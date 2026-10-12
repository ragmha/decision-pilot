"""Versioned decision log: one JSON line per decision, for audit and rollback."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .client import Provider


def record(path: Path | None, pilot: str, item_id: str, provider: Provider, config_version: str,
           decision: dict, latency_ms: float) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pilot": pilot,
        "id": item_id,
        "provider": provider.name,
        "model": provider.model,
        "config": config_version,
        "decision": decision,
        "latency_ms": round(latency_ms, 1),
    }
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
