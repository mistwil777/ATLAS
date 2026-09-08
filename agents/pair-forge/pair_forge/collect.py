"""ATLAS telemetry collector.

Controlled by the ATLAS_COLLECT environment variable:
  off      (default) — no collection
  internal — execution metrics only
  client   — metrics + anonymized outputs (requires signed B2B contract)

Silent when ATLAS_COLLECT is absent or "off". No external dependency.
Future: branchable on RAG+KG via ATLAS_BUS_URL.

See ATLAS_COLLECT_SPEC.md at the repo root for the full specification.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

_MODE = os.environ.get("ATLAS_COLLECT", "off")

_STRIP_KEYS = {"email", "name", "user", "author", "source", "file", "path", "url"}

_logger = logging.getLogger("atlas.collect")


def is_active() -> bool:
    return _MODE != "off"


def get_mode() -> str:
    return _MODE if _MODE in ("internal", "client") else "off"


def emit_metrics(agent_name: str, metrics: dict) -> None:
    """Emit execution metrics. No-op when ATLAS_COLLECT is off."""
    if not is_active():
        return
    _emit({
        "ts":      _now(),
        "agent":   agent_name,
        "mode":    get_mode(),
        "type":    "metrics",
        "metrics": metrics,
    })


def emit_output(agent_name: str, output: dict) -> None:
    """Emit a validated output. Only active in client mode — anonymized before emit."""
    if get_mode() != "client":
        return
    _emit({
        "ts":     _now(),
        "agent":  agent_name,
        "mode":   "client",
        "type":   "output",
        "output": anonymize(output),
    })


def anonymize(data: dict) -> dict:
    """Remove known identifying fields from a dict (shallow)."""
    return {k: v for k, v in data.items() if k.lower() not in _STRIP_KEYS}


def _emit(record: dict) -> None:
    _logger.info(json.dumps(record, ensure_ascii=False))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
