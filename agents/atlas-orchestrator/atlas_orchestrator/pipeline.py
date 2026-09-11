"""Orchestration logic — segment → execute → judge."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

_log = logging.getLogger("atlas_orchestrator")


def orchestrate(
    cdc_text: str,
    llm_fn: Callable,
    llm_cfg: dict,
    memory_path: Path | None = None,
    bus_fn: Callable | None = None,
    judge_fn: Callable | None = None,
) -> dict | None:
    """Run the full orchestration flow and return the raw report dict.

    Args:
        cdc_text:    CDC content as plain text.
        llm_fn:      LLM callable (prompt, cfg, max_tokens) -> str | None.
        llm_cfg:     LLM config dict.
        memory_path: Optional path for atlas-memory storage.
        bus_fn:      Optional injectable Bus factory for tests (returns Bus instance).
        judge_fn:    Optional injectable AtlasJudgeAgent factory for tests.
    """
    from atlas_orchestrator.report import build_report  # noqa: PLC0415

    routing_summary = _segment(cdc_text, llm_fn, llm_cfg, memory_path)
    if routing_summary is None:
        return None

    blocs = routing_summary.get("blocs", [])
    _log.info("Routing complete — %d blocs", len(blocs))

    bus = _get_bus(bus_fn)
    bloc_results = [_process_bloc(bloc, cdc_text, bus, llm_fn, llm_cfg, judge_fn) for bloc in blocs]

    return build_report(
        cdc_path=routing_summary.get("cdc_path", "inline"),
        routing_summary=routing_summary,
        bloc_results=bloc_results,
    )


def _segment(
    cdc_text: str,
    llm_fn: Callable,
    llm_cfg: dict,
    memory_path: Path | None,
) -> dict | None:
    try:
        from cdc_router.agent import CdcRouterAgent  # noqa: PLC0415
    except ImportError:
        _log.error("cdc-router not installed — pip install cdc-router")
        return None

    kwargs: dict = {"llm_fn": llm_fn, "llm_cfg": llm_cfg}
    if memory_path:
        kwargs["memory_storage_path"] = memory_path

    agent = CdcRouterAgent(**kwargs)
    events = list(agent.run(cdc_text=cdc_text))
    summary = next((e for e in events if e.get("summary")), None)
    return summary


def _process_bloc(
    bloc: dict,
    cdc_text: str,
    bus: object,
    llm_fn: Callable,
    llm_cfg: dict,
    judge_fn: Callable | None,
) -> dict:
    agent_name = bloc.get("agent_suggested", "")
    content = bloc.get("bloc", "")
    bloc_type = bloc.get("type", "ia")

    result: dict = {
        "bloc": content,
        "type": bloc_type,
        "agent": agent_name,
        "status": "skipped",
        "judge_passed": None,
        "judge_score": None,
        "output_preview": "",
    }

    if not agent_name:
        _log.warning("Bloc has no agent_suggested — skipping")
        return result

    step = _execute_bloc(agent_name, content, cdc_text, bus)

    if step is None:
        result["status"] = "skipped"
        _log.warning("Agent '%s' not found in Bus — bloc skipped", agent_name)
        return result

    if not step.passed:
        result["status"] = "failed"
        _log.warning("Agent '%s' failed: %s", agent_name, step.error[:200])
        return result

    result["status"] = "success"
    output_text = _extract_output(step.summary)
    result["output_preview"] = output_text[:300]

    judge_result = _judge_bloc(output_text, agent_name, llm_fn, llm_cfg, judge_fn)
    result["judge_passed"] = judge_result.get("passed")
    result["judge_score"] = judge_result.get("score")

    return result


def _execute_bloc(
    agent_name: str,
    content: str,
    cdc_text: str,
    bus: object,
) -> object:
    """Call Bus.run_agent — returns StepResult or None if agent not found."""
    if bus is None:
        return None

    step = bus.run_agent(agent_name, {"cdc_bloc": content, "context": cdc_text})  # type: ignore[union-attr]
    # Bus returns StepResult with error if agent not installed
    if step.error and "not found" in step.error:
        return None
    return step


def _judge_bloc(
    output: str,
    agent_name: str,
    llm_fn: Callable,
    llm_cfg: dict,
    judge_fn: Callable | None,
) -> dict:
    try:
        if judge_fn is not None:
            agent = judge_fn()
        else:
            from atlas_judge.agent import AtlasJudgeAgent  # noqa: PLC0415

            agent = AtlasJudgeAgent(llm_fn=llm_fn, config={"llm": llm_cfg})

        events = list(agent.run(operation="pointwise", output=output, agent_name=agent_name))
        summary = next((e for e in events if e.get("summary")), None)
        if summary:
            return {"passed": summary.get("passed"), "score": summary.get("score")}
    except ImportError:
        _log.warning("atlas-judge not installed — skipping evaluation")
    except Exception as exc:  # noqa: BLE001
        _log.warning("atlas-judge evaluation failed: %s", exc)

    return {"passed": None, "score": None}


def _get_bus(bus_fn: Callable | None) -> object:
    if bus_fn is not None:
        return bus_fn()
    try:
        from atlas_bus.bus import Bus  # noqa: PLC0415

        return Bus()
    except ImportError:
        _log.warning("atlas-bus not installed — agent execution disabled")
        return None


def _extract_output(summary: dict) -> str:
    """Best-effort extraction of a text preview from a summary dict."""
    for key in ("output", "text", "content", "result", "pairs"):
        val = summary.get(key)
        if val:
            return str(val)
    return str(summary)
