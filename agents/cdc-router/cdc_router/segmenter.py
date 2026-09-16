"""LLM-based CDC segmenter — produces structured bloc list from raw CDC text."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from cdc_router.prompts import CORRECTION_PROMPT, REPROMPT, SEGMENTATION_PROMPT

_log = logging.getLogger("cdc_router")


def segment(
    cdc_text: str,
    rag_examples: list[dict],
    kg_agents: dict,
    llm_fn: Callable[[str, dict, int], str | None],
    llm_cfg: dict,
) -> tuple[dict | None, dict | None, int]:
    """Call LLM to segment a CDC into typed blocs, then validate with atlas-judge.

    Returns (parsed_result, judge_verdict, attempts).
    parsed_result is None on failure.
    judge_verdict is None if atlas-judge is unavailable.
    Re-prompts once if the first response is not valid JSON.
    """
    prompt = SEGMENTATION_PROMPT.format(
        cdc_text=cdc_text,
        rag_examples=_fmt_examples(rag_examples),
        kg_agents=_fmt_agents(kg_agents, node_type="agent"),
        kg_tools=_fmt_agents(kg_agents, node_type="tool"),
    )

    raw = llm_fn(prompt, llm_cfg, 2048)
    if not raw:
        _log.warning("LLM returned no output")
        return None, None, 0

    result = _parse(raw)
    if result is None:
        _log.warning("First LLM response not valid JSON — re-prompting")
        reprompt = REPROMPT.format(previous_response=raw[:500])
        raw = llm_fn(reprompt, llm_cfg, 1024)
        if not raw:
            return None, None, 0
        result = _parse(raw)
        if result is None:
            return None, None, 0

    try:
        from atlas_core.judge_retry import judge_and_retry  # noqa: PLC0415

        final, verdict, attempts = judge_and_retry(
            output=json.dumps(result, ensure_ascii=False),
            agent_name="cdc-router",
            context=cdc_text,
            llm_fn=llm_fn,
            llm_cfg=llm_cfg,
            reprompt_fn=_build_correction_prompt,
            parse_fn=_parse,
            max_attempts=2,
        )
        return final, verdict, attempts
    except ImportError:
        _log.warning("atlas-core not installed — skipping quality check")
        return result, None, 1


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
        for bloc in data["blocs"]:
            if not isinstance(bloc.get("criteria"), list):
                bloc["criteria"] = []
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


def _build_correction_prompt(previous_output: str, failed_criteria: list[dict]) -> str:
    failures = "\n".join(
        f"- {c.get('criterion', '?')} (score {c.get('score', '?')}/5): {c.get('comment', '')}"
        for c in failed_criteria
    )
    return CORRECTION_PROMPT.format(previous_output=previous_output, failures=failures)


def _fmt_agents(kg_agents: dict, node_type: str = "agent") -> str:
    nodes = kg_agents.get("nodes", [])
    filtered = [n for n in nodes if n.get("node_type", "agent") == node_type]
    if not filtered:
        label = "agents" if node_type == "agent" else "tools"
        return f"No {label} registered yet."
    lines = []
    for node in filtered:
        name = node.get("id", "?")
        caps = ", ".join(node.get("capabilities", []))
        lines.append(f"- {name}: capabilities=[{caps}]")
    return "\n".join(lines)
