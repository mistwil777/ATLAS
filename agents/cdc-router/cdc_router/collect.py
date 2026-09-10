"""ATLAS telemetry collector — verbatim pattern copy."""

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
    if not is_active():
        return
    _emit(
        {
            "ts": _now(),
            "agent": agent_name,
            "mode": get_mode(),
            "type": "metrics",
            "metrics": metrics,
        }
    )


def emit_output(agent_name: str, output: dict) -> None:
    if get_mode() != "client":
        return
    _emit(
        {
            "ts": _now(),
            "agent": agent_name,
            "mode": "client",
            "type": "output",
            "output": anonymize(output),
        }
    )


def anonymize(data: dict) -> dict:
    return {k: v for k, v in data.items() if k.lower() not in _STRIP_KEYS}


def _emit(record: dict) -> None:
    _logger.info(json.dumps(record, ensure_ascii=False))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
