"""Pass 0 — CDC gap detection: vague / missing / incoherent."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from cdc_router.prompts import DETECT_GAPS_PROMPT

_log = logging.getLogger("cdc_router")
_MAX_GAPS = 5


def detect_cdc_gaps(
    cdc_text: str,
    llm_fn: Callable[[str, dict, int], str | None],
    llm_cfg: dict,
    kg_graph: dict | None = None,
) -> list[dict]:
    """Analyse the CDC for vague/missing/incoherent items before segmentation.

    Returns list of gap dicts (max _MAX_GAPS), filtered against KG known resolutions.
    Returns [] on LLM failure — pass 0 is non-blocking.
    """
    prompt = DETECT_GAPS_PROMPT.format(cdc_text=cdc_text[:1500])
    raw = llm_fn(prompt, llm_cfg, 512)
    if not raw:
        return []

    gaps = _parse_gaps(raw)
    if kg_graph:
        gaps = _filter_known_gaps(gaps, kg_graph)

    return gaps[:_MAX_GAPS]


def _parse_gaps(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()
    start = raw.find("[")
    end = raw.rfind("]") + 1
    if start == -1 or end <= 0:
        return []
    try:
        items = json.loads(raw[start:end])
        if not isinstance(items, list):
            return []
        valid = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in ("vague", "missing", "incoherent"):
                continue
            if not item.get("description"):
                continue
            valid.append(
                {
                    "type": item["type"],
                    "description": item["description"],
                    "bloc_concerned": item.get("bloc_concerned", ""),
                    "impact": item.get("impact", "medium"),
                }
            )
        return valid
    except json.JSONDecodeError:
        _log.warning("pass-0: JSON parse failed — skipping gap detection")
        return []


def _filter_known_gaps(gaps: list[dict], kg_graph: dict) -> list[dict]:
    """Remove gaps whose description matches a KG RESOLVED_BY edge payload.

    Heuristic: if any KG edge relation == 'RESOLVED_BY' and the target node
    contains a keyword from the gap description, skip that gap.
    """
    resolved_keywords: set[str] = set()
    for link in kg_graph.get("links", []):
        if link.get("relation") == "RESOLVED_BY":
            target = str(link.get("target", "")).lower()
            resolved_keywords.update(target.replace("_", " ").split())

    if not resolved_keywords:
        return gaps

    filtered = []
    for gap in gaps:
        desc_words = set(gap["description"].lower().split())
        if desc_words & resolved_keywords:
            _log.debug("pass-0: skipping already-resolved gap: %s", gap["description"][:60])
            continue
        filtered.append(gap)
    return filtered
