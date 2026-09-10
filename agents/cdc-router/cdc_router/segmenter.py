"""LLM-based CDC segmenter — produces structured bloc list from raw CDC text."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from cdc_router.prompts import REPROMPT, SEGMENTATION_PROMPT

_log = logging.getLogger("cdc_router")


def segment(
    cdc_text: str,
    rag_examples: list[dict],
    kg_agents: dict,
    llm_fn: Callable[[str, dict, int], str | None],
    llm_cfg: dict,
) -> dict | None:
    """Call LLM to segment a CDC into typed blocs.

    Returns parsed dict with key "blocs", or None on failure.
    Re-prompts once if the first response is not valid JSON.
    """
    prompt = SEGMENTATION_PROMPT.format(
        cdc_text=cdc_text,
        rag_examples=_fmt_examples(rag_examples),
        kg_agents=_fmt_agents(kg_agents),
    )

    raw = llm_fn(prompt, llm_cfg, 1024)
    if not raw:
        _log.warning("LLM returned no output")
        return None

    result = _parse(raw)
    if result is not None:
        return result

    _log.warning("First LLM response not valid JSON — re-prompting")
    reprompt = REPROMPT.format(previous_response=raw[:500])
    raw2 = llm_fn(reprompt, llm_cfg, 1024)
    if not raw2:
        return None

    return _parse(raw2)


def _parse(raw: str) -> dict | None:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end <= 0:
        return None
    try:
        data = json.loads(raw[start:end])
        if "blocs" not in data or not isinstance(data["blocs"], list):
            return None
        return data
    except json.JSONDecodeError:
        return None


def _fmt_examples(examples: list[dict]) -> str:
    if not examples:
        return "None available."
    lines = []
    for i, ex in enumerate(examples[:3], 1):
        content = ex.get("content", "")[:200]
        payload = ex.get("payload", {})
        lines.append(f"Example {i}: {content}")
        if payload.get("segmentation"):
            lines.append(
                f"  Segmentation: {json.dumps(payload['segmentation'], ensure_ascii=False)}"
            )
    return "\n".join(lines)


def _fmt_agents(kg_agents: dict) -> str:
    nodes = kg_agents.get("nodes", [])
    if not nodes:
        return "No agents registered yet."
    lines = []
    for node in nodes:
        name = node.get("id", "?")
        caps = ", ".join(node.get("capabilities", []))
        tags = ", ".join(node.get("tags", []))
        lines.append(f"- {name}: capabilities=[{caps}], tags=[{tags}]")
    return "\n".join(lines)
