"""Orchestration logic — segment → execute → judge → HITL."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

_log = logging.getLogger("atlas_orchestrator")

_DEFAULT_RUN_STORE_DIR = Path.home() / ".atlas" / "runs"


def orchestrate(
    cdc_text: str,
    llm_fn: Callable,
    llm_cfg: dict,
    memory_path: Path | None = None,
    bus_fn: Callable | None = None,
    judge_fn: Callable | None = None,
    run_store_dir: Path | None = None,
    project_dir: str | None = None,
    project_owner: str = "client",
) -> dict | None:
    """Run the full orchestration flow and return the raw report dict.

    Args:
        cdc_text:       CDC content as plain text.
        llm_fn:         LLM callable (prompt, cfg, max_tokens) -> str | None.
        llm_cfg:        LLM config dict.
        memory_path:    Optional path for atlas-memory storage.
        bus_fn:         Optional injectable Bus factory for tests (returns Bus instance).
        judge_fn:       Optional injectable AtlasJudgeAgent factory for tests.
        run_store_dir:  Directory for HITL run persistence (default: ~/.atlas/runs).
    """
    from atlas_orchestrator.report import build_report  # noqa: PLC0415

    routing_summary = _segment(cdc_text, llm_fn, llm_cfg, memory_path)
    if routing_summary is None:
        return None

    blocs = routing_summary.get("blocs", [])
    requires_creation = routing_summary.get("requires_creation", [])
    _log.info("Routing complete — %d blocs, %d to create", len(blocs), len(requires_creation))

    run_store = _get_run_store(run_store_dir)
    run_id = (
        run_store.create_run(
            metadata={
                "cdc_preview": cdc_text[:200],
                "cdc_full": cdc_text,
                "project_dir": project_dir or "",
                "project_owner": project_owner,
            }
        )
        if run_store
        else None
    )
    if run_store and run_id and requires_creation:
        run_store.set_requires_creation(run_id, requires_creation)

    bus = _get_bus(bus_fn)
    bloc_results = []
    for bloc in blocs:
        result = _process_bloc(bloc, cdc_text, bus, llm_fn, llm_cfg, judge_fn)
        if run_store and run_id:
            run_store.append_bloc(run_id, result)
        bloc_results.append(result)

    requires_review = any(r.get("requires_review") for r in bloc_results)
    if run_store and run_id and requires_review:
        _log.warning(
            "Run %s requires human review — %d bloc(s) failed judge",
            run_id,
            sum(1 for r in bloc_results if r.get("requires_review")),
        )

    report = build_report(
        cdc_path=routing_summary.get("cdc_path", "inline"),
        routing_summary=routing_summary,
        bloc_results=bloc_results,
    )
    if report is not None:
        report["run_id"] = run_id
        report["requires_review"] = requires_review
    return report


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

    kwargs: dict = {"llm_fn": llm_fn, "config": {"llm": llm_cfg}}
    if memory_path:
        kwargs["memory_storage_path"] = memory_path

    agent = CdcRouterAgent(**kwargs)
    events = list(agent.run(cdc_text=cdc_text))
    for e in events:
        if e.get("level") == "error":
            _log.error("cdc-router: %s", e.get("msg"))
        elif e.get("level") == "info":
            _log.debug("cdc-router: %s", e.get("msg"))
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
        "justification": bloc.get("justification", ""),
        "criteria": bloc.get("criteria") or [],
        "status": "skipped",
        "judge_passed": None,
        "judge_score": None,
        "output_preview": "",
        "requires_review": False,
        "judge_criteria": [],
        "judge_feedback": "",
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

    bloc_criteria = bloc.get("criteria") or []
    judge_passed, judge_score, judge_criteria, judge_feedback = _judge_bloc(
        output_text, agent_name, bloc_criteria, cdc_text, llm_fn, llm_cfg, judge_fn
    )
    result["judge_passed"] = judge_passed
    result["judge_score"] = judge_score
    result["judge_criteria"] = judge_criteria
    result["judge_feedback"] = judge_feedback
    result["requires_review"] = judge_passed is False

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
    if step.error and "not found" in step.error:
        return None
    return step


def _judge_bloc(
    output: str,
    agent_name: str,
    criteria: list[str],
    context: str,
    llm_fn: Callable,
    llm_cfg: dict,
    judge_fn: Callable | None,
) -> tuple[bool | None, float | None, list, str]:
    """Evaluate a bloc output using dynamic criteria from cdc-router."""
    try:
        if judge_fn is not None:
            judge = judge_fn()
        else:
            from atlas_judge.agent import AtlasJudgeAgent  # noqa: PLC0415

            judge = AtlasJudgeAgent(llm_fn=llm_fn, config={"llm": llm_cfg})

        events = list(
            judge.run(
                operation="pointwise",
                output=output,
                agent_name=agent_name,
                criteria=criteria if criteria else None,
                context=context,
            )
        )
        summary = next((e for e in events if e.get("summary")), None)
        if summary:
            return (
                summary.get("passed"),
                summary.get("score"),
                summary.get("criteria_scores", []),
                summary.get("feedback", ""),
            )
    except ImportError:
        _log.warning("atlas-judge not installed — skipping evaluation")
    except Exception as exc:  # noqa: BLE001
        _log.warning("atlas-judge evaluation failed: %s", exc)

    return None, None, [], ""


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


def _get_run_store(run_store_dir: Path | None) -> object:
    try:
        from atlas_memory.run_store import RunStore  # noqa: PLC0415

        return RunStore(run_store_dir or _DEFAULT_RUN_STORE_DIR)
    except ImportError:
        _log.warning("atlas-memory not installed — run persistence disabled")
        return None
