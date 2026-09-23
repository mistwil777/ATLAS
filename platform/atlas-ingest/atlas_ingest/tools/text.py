"""Plain text extraction — txt, md, csv, json."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path


def extract(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()

    if suffix == ".csv":
        return _csv_to_text(raw)
    if suffix == ".json":
        return _json_to_text(raw)
    return raw


def _csv_to_text(raw: str) -> str:
    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        return raw
    headers = list(rows[0].keys())
    lines = [", ".join(headers)]
    for row in rows[:100]:  # cap at 100 rows for context injection
        lines.append(", ".join(str(v) for v in row.values()))
    if len(rows) > 100:
        lines.append(f"... ({len(rows) - 100} more rows truncated)")
    return "\n".join(lines)


def _json_to_text(raw: str) -> str:
    try:
        data = json.loads(raw)
        return json.dumps(data, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return raw
