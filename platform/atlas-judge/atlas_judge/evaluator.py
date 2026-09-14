"""LLM-as-judge evaluation functions — pointwise, pairwise, reference-based."""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Callable

from atlas_judge.prompts import PAIRWISE_PROMPT, POINTWISE_PROMPT, REFERENCE_PROMPT, REPROMPT

_log = logging.getLogger("atlas_judge")
_PASS_THRESHOLD = 4


def pointwise(
    output: str,
    criteria: list[str],
    context: str,
    llm_fn: Callable[[str, dict, int], str | None],
    llm_cfg: dict,
) -> dict | None:
    prompt = POINTWISE_PROMPT.format(
        criteria_block=_fmt_criteria(criteria),
        context=context or "No additional context provided.",
        output=output,
    )
    result = _call_and_parse(prompt, llm_fn, llm_cfg)
    if result is None:
        return None
    result["score"] = _aggregate_score(result.get("criteria_scores", []))
    result["max_score"] = 5
    result["passed"] = result["score"] >= _PASS_THRESHOLD
    return result


def pairwise(
    output_a: str,
    output_b: str,
    criteria: list[str],
    context: str,
    llm_fn: Callable[[str, dict, int], str | None],
    llm_cfg: dict,
) -> dict | None:
    # Randomize order to mitigate position bias
    swapped = random.random() < 0.5
    first, second = (output_b, output_a) if swapped else (output_a, output_b)

    prompt = PAIRWISE_PROMPT.format(
        criteria_block=_fmt_criteria(criteria),
        context=context or "No additional context provided.",
        output_a=first,
        output_b=second,
    )
    result = _call_and_parse(prompt, llm_fn, llm_cfg)
    if result is None:
        return None

    # Correct winner if we swapped
    if swapped and result.get("winner") in ("A", "B"):
        result["winner"] = "B" if result["winner"] == "A" else "A"

    result["score"] = None
    result["max_score"] = None
    result["passed"] = result.get("winner") is not None
    return result


def reference_based(
    output: str,
    reference: str,
    criteria: list[str],
    context: str,
    llm_fn: Callable[[str, dict, int], str | None],
    llm_cfg: dict,
) -> dict | None:
    prompt = REFERENCE_PROMPT.format(
        criteria_block=_fmt_criteria(criteria),
        context=context or "No additional context provided.",
        reference=reference,
        output=output,
    )
    result = _call_and_parse(prompt, llm_fn, llm_cfg)
    if result is None:
        return None
    result["score"] = _aggregate_score(result.get("criteria_scores", []))
    result["max_score"] = 5
    result["passed"] = result["score"] >= _PASS_THRESHOLD
    return result


def _call_and_parse(
    prompt: str,
    llm_fn: Callable[[str, dict, int], str | None],
    llm_cfg: dict,
) -> dict | None:
    raw = llm_fn(prompt, llm_cfg, 1024)
    if not raw:
        _log.warning("LLM returned no output")
        return None

    result = _parse(raw)
    if result is not None:
        return result

    _log.warning("First LLM response not valid JSON — re-prompting")
    raw2 = llm_fn(REPROMPT.format(previous_response=raw[:500]), llm_cfg, 1024)
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
        if "criteria_scores" not in data:
            return None
        return data
    except json.JSONDecodeError:
        return None


def _aggregate_score(criteria_scores: list[dict]) -> float:
    scores = [c.get("score", 0) for c in criteria_scores if isinstance(c.get("score"), int | float)]
    return round(sum(scores) / len(scores), 2) if scores else 0.0


def _fmt_criteria(criteria: list[str]) -> str:
    return "\n".join(f"- {c}" for c in criteria) if criteria else "- Overall quality"
