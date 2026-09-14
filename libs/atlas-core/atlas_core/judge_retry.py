"""Shared judge-and-retry loop for ATLAS agents.

Usage:
    from atlas_core.judge_retry import judge_and_retry

    result, verdict, attempts = judge_and_retry(
        output=json.dumps(blocs),
        agent_name="cdc-router",
        context=cdc_text,
        llm_fn=self._llm,
        llm_cfg=self.llm_cfg,
        reprompt_fn=build_correction_prompt,  # called with (output, failed_criteria)
        max_attempts=2,
    )
"""

from __future__ import annotations

import logging
from collections.abc import Callable

_log = logging.getLogger("atlas_core.judge_retry")


def judge_and_retry(
    output: str,
    agent_name: str,
    context: str,
    llm_fn: Callable[[str, dict, int], str | None],
    llm_cfg: dict,
    reprompt_fn: Callable[[str, list[dict]], str],
    parse_fn: Callable[[str], dict | None],
    max_attempts: int = 2,
) -> tuple[dict | None, dict | None, int]:
    """Run atlas-judge on output; re-generate if it fails.

    Args:
        output:       JSON string of the output to evaluate.
        agent_name:   Name of the agent whose criteria.py is loaded by atlas-judge.
        context:      Original task context (e.g. the CDC text).
        llm_fn:       LLM callable used for re-generation.
        llm_cfg:      LLM config dict.
        reprompt_fn:  Called with (original_output, failed_criteria) → new prompt string.
        parse_fn:     Parses raw LLM string → dict | None (same parser as the agent uses).
        max_attempts: Max re-generation attempts after a failed judge (default 2).

    Returns:
        (final_parsed_output, judge_verdict, total_attempts)
        judge_verdict is None if atlas-judge is unavailable.
    """
    try:
        from atlas_judge.agent import AtlasJudgeAgent  # noqa: PLC0415
    except ImportError:
        _log.warning("atlas-judge not installed — skipping quality check")
        parsed = parse_fn(output)
        return parsed, None, 1

    judge = AtlasJudgeAgent(llm_fn=llm_fn, config={"llm": llm_cfg})

    current_output = output
    current_parsed = parse_fn(output)
    verdict = None
    attempts = 0

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        events = list(
            judge.run(
                operation="pointwise",
                output=current_output,
                agent_name=agent_name,
                context=context,
            )
        )
        verdict = next((e for e in events if e.get("summary")), None)

        if verdict is None:
            _log.warning("atlas-judge returned no verdict (attempt %d)", attempt)
            break

        if verdict.get("passed"):
            break

        if attempt == max_attempts:
            _log.warning("Output did not pass atlas-judge after %d attempts", max_attempts)
            break

        failed_criteria = [
            c for c in verdict.get("criteria_scores", []) if c.get("score", 5) < 4
        ]
        _log.info(
            "Judge failed on %d criteria — re-generating (attempt %d/%d)",
            len(failed_criteria),
            attempt,
            max_attempts,
        )

        correction_prompt = reprompt_fn(current_output, failed_criteria)
        raw = llm_fn(correction_prompt, llm_cfg, 1024)
        if not raw:
            _log.warning("LLM returned nothing on correction attempt %d", attempt)
            break

        new_parsed = parse_fn(raw)
        if new_parsed is None:
            _log.warning("Correction attempt %d produced invalid JSON", attempt)
            break

        current_output = raw
        current_parsed = new_parsed

    return current_parsed, verdict, attempts
