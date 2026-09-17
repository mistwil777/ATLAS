"""LLM-based CDC segmenter — three-pass architecture.

Pass 1: functional segmentation (what each bloc does, no classification).
Pass 2: classification ia/tool/code per bloc with state-of-art criteria + iterations.
Pass 3: architectural coherence validation, partial reclassification if needed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from cdc_router.prompts import (
    CLASSIFICATION_PROMPT,
    COHERENCE_PROMPT,
    CORRECTION_PROMPT,
    REPROMPT,
    SEGMENTATION_PROMPT,
)

_log = logging.getLogger("cdc_router")


def segment(
    cdc_text: str,
    rag_examples: list[dict],
    kg_agents: dict,
    llm_fn: Callable[[str, dict, int], str | None],
    llm_cfg: dict,
) -> tuple[dict | None, dict | None, int]:
    """Segment a CDC into typed blocs using three LLM passes.

    Returns (parsed_result, judge_verdict, attempts).
    parsed_result is None on failure.
    """
    # Pass 1 — functional segmentation
    segments = _pass1_segment(cdc_text, rag_examples, llm_fn, llm_cfg)
    if not segments:
        return None, None, 0

    # Pass 2 — classification with judge iterations
    result, verdict, attempts = _pass2_classify(segments, cdc_text, kg_agents, llm_fn, llm_cfg)
    if not result:
        return None, verdict, attempts

    # Pass 3 — architectural coherence
    result = _pass3_coherence(result, llm_fn, llm_cfg)

    # Deterministic exclusivity enforcement (never rely on LLM alone)
    result = _enforce_exclusivity(result)

    return result, verdict, attempts


# ── Pass 1 ────────────────────────────────────────────────────────────────────


def _pass1_segment(
    cdc_text: str,
    rag_examples: list[dict],
    llm_fn: Callable,
    llm_cfg: dict,
) -> list[dict] | None:
    prompt = SEGMENTATION_PROMPT.format(
        cdc_text=cdc_text,
        rag_examples=_fmt_examples(rag_examples),
    )
    raw = llm_fn(prompt, llm_cfg, 2048)
    if not raw:
        _log.warning("Pass 1: LLM returned no output")
        return None

    data = _parse_json(raw)
    if data is None:
        _log.warning("Pass 1: invalid JSON — re-prompting")
        reprompt = REPROMPT.format(previous_response=raw[:500]).replace('"blocs"', '"segments"')
        raw2 = llm_fn(reprompt, llm_cfg, 1024)
        data = _parse_json(raw2) if raw2 else None

    if data is None or "segments" not in data or not isinstance(data["segments"], list):
        _log.warning("Pass 1: could not parse segments")
        return None

    segments = data["segments"]
    for seg in segments:
        if not isinstance(seg.get("criteria"), list):
            seg["criteria"] = []

    _log.info("Pass 1: %d segments identified", len(segments))
    return segments


# ── Pass 2 ────────────────────────────────────────────────────────────────────


def _pass2_classify(
    segments: list[dict],
    cdc_text: str,
    kg_agents: dict,
    llm_fn: Callable,
    llm_cfg: dict,
) -> tuple[dict | None, dict | None, int]:
    prompt = CLASSIFICATION_PROMPT.format(
        cdc_text=cdc_text,
        segments_json=json.dumps({"segments": segments}, ensure_ascii=False, indent=2),
        kg_agents=_fmt_agents(kg_agents, node_type="agent"),
        kg_tools=_fmt_agents(kg_agents, node_type="tool"),
    )

    raw = llm_fn(prompt, llm_cfg, 2048)
    if not raw:
        _log.warning("Pass 2: LLM returned no output")
        return None, None, 0

    result = _parse_blocs(raw)
    if result is None:
        _log.warning("Pass 2: invalid JSON — re-prompting")
        reprompt = REPROMPT.format(previous_response=raw[:500])
        raw2 = llm_fn(reprompt, llm_cfg, 1024)
        result = _parse_blocs(raw2) if raw2 else None
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
            parse_fn=_parse_blocs,
            max_attempts=3,
        )
        _log.info("Pass 2: classification complete (%d attempts)", attempts)
        return final, verdict, attempts
    except ImportError:
        _log.warning("Pass 2: atlas-core not installed — skipping quality check")
        return result, None, 1


# ── Pass 3 ────────────────────────────────────────────────────────────────────


def _pass3_coherence(
    result: dict,
    llm_fn: Callable,
    llm_cfg: dict,
) -> dict:
    prompt = COHERENCE_PROMPT.format(blocs_json=json.dumps(result, ensure_ascii=False, indent=2))
    raw = llm_fn(prompt, llm_cfg, 2048)
    if not raw:
        _log.warning("Pass 3: LLM returned no output — skipping coherence check")
        return result

    data = _parse_json(raw)
    if data is None or "blocs" not in data or not isinstance(data["blocs"], list):
        _log.warning("Pass 3: invalid JSON — keeping pass 2 result")
        return result

    corrections = data.get("corrections_applied", [])
    if corrections:
        _log.info("Pass 3: %d correction(s) applied: %s", len(corrections), corrections)
    else:
        _log.info("Pass 3: architecture is coherent")

    return {"blocs": data["blocs"]}


# ── Deterministic exclusivity enforcement ────────────────────────────────────


def _enforce_exclusivity(result: dict) -> dict:
    """Ensure no name appears as both ia and tool — ia wins if conflict."""
    blocs = result.get("blocs", [])
    ia_names = {
        b["agent_suggested"] for b in blocs if b.get("type") == "ia" and b.get("agent_suggested")
    }
    tool_names = {
        b["agent_suggested"] for b in blocs if b.get("type") == "tool" and b.get("agent_suggested")
    }
    conflicts = ia_names & tool_names

    if not conflicts:
        # Backfill called_by for any tool bloc that still lacks it
        _backfill_called_by(blocs)
        return result

    for name in conflicts:
        _log.warning(
            "Exclusivity conflict: '%s' classified as both ia and tool — keeping ia",
            name,
        )
    for bloc in blocs:
        if bloc.get("type") == "tool" and bloc.get("agent_suggested") in conflicts:
            bloc["type"] = "ia"
            bloc["called_by"] = None
            bloc["justification"] = (
                bloc.get("justification", "") + " [reclassified ia: exclusivity conflict resolved]"
            )

    _backfill_called_by(blocs)
    return {"blocs": blocs}


def _backfill_called_by(blocs: list[dict]) -> None:
    """Infer called_by for tool blocs that LLM left empty."""
    last_ia: str | None = None
    for bloc in blocs:
        if bloc.get("type") == "ia" and bloc.get("agent_suggested"):
            last_ia = bloc["agent_suggested"]
        if bloc.get("type") == "tool" and not bloc.get("called_by"):
            bloc["called_by"] = last_ia or ""


# ── Parsers ───────────────────────────────────────────────────────────────────


def _parse_json(raw: str) -> dict | None:
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
        return json.loads(raw[start:end])
    except json.JSONDecodeError:
        return None


def _parse_blocs(raw: str) -> dict | None:
    """Parse and validate a blocs JSON response (pass 2/3 format)."""
    data = _parse_json(raw) if isinstance(raw, str) else raw
    if data is None:
        return None
    if "blocs" not in data or not isinstance(data["blocs"], list):
        return None
    for bloc in data["blocs"]:
        if not isinstance(bloc.get("criteria"), list):
            bloc["criteria"] = []
    return data


# Keep legacy _parse as alias for backwards compat with tests
_parse = _parse_blocs


# ── Formatters ────────────────────────────────────────────────────────────────


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
