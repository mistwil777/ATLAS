"""Orchestration logic — segment → execute → judge → HITL."""

from __future__ import annotations

import logging
import os
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
    cdc_router_cls: type | None = None,
    skip_gap_criteria: list[str] | None = None,
) -> dict | None:
    """Run the full orchestration flow and return the raw report dict.

    Args:
        cdc_text:           CDC content as plain text.
        llm_fn:             LLM callable (prompt, cfg, max_tokens) -> str | None.
        llm_cfg:            LLM config dict.
        memory_path:        Optional path for atlas-memory storage.
        bus_fn:             Optional injectable Bus factory for tests (returns Bus instance).
        judge_fn:           Optional injectable AtlasJudgeAgent factory for tests.
        run_store_dir:      Directory for HITL run persistence (default: ~/.atlas/runs).
        cdc_router_cls:     Optional injectable CdcRouterAgent class for tests.
        skip_gap_criteria:  Criteria already answered by the client — gaps matching these
                            are not re-asked (used when resuming after a waiting_for_client run).
    """
    from atlas_orchestrator.report import build_report  # noqa: PLC0415

    routing_summary = _segment_with_coherence(
        cdc_text, llm_fn, llm_cfg, memory_path, cdc_router_cls
    )
    if routing_summary is None:
        return None

    blocs = routing_summary.get("blocs", [])
    requires_creation = routing_summary.get("requires_creation", [])
    requires_adaptation = routing_summary.get("requires_adaptation", [])
    requires_tools = routing_summary.get("requires_tools", [])
    requires_tools_adaptation = routing_summary.get("requires_tools_adaptation", [])
    _log.info(
        "Routing complete — %d blocs, %d to create, %d to adapt, %d tools to build",
        len(blocs),
        len(requires_creation),
        len(requires_adaptation),
        len(requires_tools),
    )

    cdc_gaps = routing_summary.get("cdc_gaps", [])
    if cdc_gaps:
        high = [g for g in cdc_gaps if g.get("impact") == "high"]
        _log.info("CDC gaps detected: %d total, %d high-impact", len(cdc_gaps), len(high))

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
    if run_store and run_id and requires_adaptation:
        run_store.set_requires_adaptation(run_id, requires_adaptation)
    if run_store and run_id and requires_tools:
        run_store.set_requires_tools(run_id, requires_tools)
    if run_store and run_id and requires_tools_adaptation:
        run_store.set_requires_tools_adaptation(run_id, requires_tools_adaptation)

    # If high-impact CDC gaps found — suspend and ask client before scaffold
    if run_store and run_id and cdc_gaps:
        high_gaps = [g for g in cdc_gaps if g.get("impact") == "high"]
        if high_gaps:
            questions = _gaps_to_questions(high_gaps)
            # Filter out gaps whose criteria were already answered in a previous run
            _skip = set(skip_gap_criteria or [])
            if _skip:
                questions = [q for q in questions if q.get("criterion", "") not in _skip]
            if not questions:
                # All high-impact gaps were already answered — proceed without suspension
                _log.info("Run %s: all high-impact gaps already answered — proceeding", run_id)
            else:
                run_store.set_pending_questions(run_id, "cdc-router", questions)
                _log.warning(
                    "Run %s suspended — %d high-impact CDC gap(s) require client input",
                    run_id,
                    len(questions),
                )
                report = build_report(
                    cdc_path=routing_summary.get("cdc_path", "inline"),
                    routing_summary=routing_summary,
                    bloc_results=[],
                )
                if report is not None:
                    report["run_id"] = run_id
                    report["status"] = "waiting_for_client"
                    report["pending_questions"] = questions
                    report["requires_review"] = False
                return report

    if os.environ.get("ATLAS_MODE", "saas") != "airgap":
        try:
            import mlflow  # noqa: PLC0415

            _mlflow_uri = os.environ.get(
                "MLFLOW_TRACKING_URI", str(Path.home() / ".atlas" / "mlflow")
            )
            mlflow.set_tracking_uri(_mlflow_uri)
            mlflow.set_experiment("atlas/routing")
            with mlflow.start_run(run_name=(run_id or "unknown")[:8]):
                mlflow.set_tags({"atlas.run_id": run_id or "unknown"})
                mlflow.log_params(
                    {
                        "blocs_count": len(blocs),
                        "requires_creation": ",".join(requires_creation) or "none",
                        "requires_tools": ",".join(requires_tools) or "none",
                        "cdc_preview": cdc_text[:200],
                    }
                )
                mlflow.log_metrics(
                    {
                        "requires_creation_count": len(requires_creation),
                        "requires_tools_count": len(requires_tools),
                        "agents_reused_count": len(routing_summary.get("atlas_agents_reused", [])),
                        "tools_reused_count": len(routing_summary.get("atlas_tools_reused", [])),
                    }
                )
        except Exception:  # noqa: BLE001
            pass

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
    cdc_router_cls: type | None = None,
    coherence_feedback: list[dict] | None = None,
) -> dict | None:
    if cdc_router_cls is None:
        try:
            from cdc_router.agent import CdcRouterAgent  # noqa: PLC0415

            cdc_router_cls = CdcRouterAgent
        except ImportError:
            _log.error("cdc-router not installed — pip install cdc-router")
            return None

    kwargs: dict = {"llm_fn": llm_fn, "config": {"llm": llm_cfg}}
    if memory_path:
        kwargs["memory_storage_path"] = memory_path

    agent = cdc_router_cls(**kwargs)
    events = list(agent.run(cdc_text=cdc_text, coherence_feedback=coherence_feedback))
    for e in events:
        if e.get("level") == "error":
            _log.error("cdc-router: %s", e.get("msg"))
        elif e.get("level") == "info":
            _log.debug("cdc-router: %s", e.get("msg"))
    summary = next((e for e in events if e.get("summary")), None)
    return summary


_MAX_COHERENCE_PASSES = 3


def _segment_with_coherence(
    cdc_text: str,
    llm_fn: Callable,
    llm_cfg: dict,
    memory_path: Path | None,
    cdc_router_cls: type | None = None,
) -> dict | None:
    """Segment, then run coherence loop until clean or max passes reached."""
    coherence_feedback: list[dict] | None = None

    for pass_n in range(_MAX_COHERENCE_PASSES):
        routing_summary = _segment(
            cdc_text, llm_fn, llm_cfg, memory_path, cdc_router_cls, coherence_feedback
        )
        if routing_summary is None:
            return None

        blocs = routing_summary.get("blocs", [])
        coherence = _check_coherence(blocs, llm_fn, llm_cfg)

        if coherence["clean"]:
            if pass_n > 0:
                _log.info("Coherence clean after %d pass(es)", pass_n + 1)
            return routing_summary

        full = [c for c in coherence["conflicts"] if c.get("type") == "full"]
        partial = [c for c in coherence["conflicts"] if c.get("type") == "partial"]

        # Full overlaps resolved deterministically — remove redundant bloc
        if full:
            redundant = {c["redundant"] for c in full}
            blocs = [b for b in blocs if b.get("agent_suggested") not in redundant]
            routing_summary["blocs"] = blocs
            for name in redundant:
                _log.info("Coherence: removed fully redundant bloc '%s'", name)

        if not partial:
            # Only full overlaps — resolved, no need to re-segment
            return routing_summary

        # Partial overlaps require re-segmentation with feedback
        coherence_feedback = partial
        _log.info(
            "Coherence pass %d: %d partial overlap(s) — re-segmenting with feedback",
            pass_n + 1,
            len(partial),
        )

    # Max passes reached — return last result and log warning
    _log.warning(
        "Coherence: max passes (%d) reached with unresolved partial overlaps — proceeding",
        _MAX_COHERENCE_PASSES,
    )
    return routing_summary


def _check_coherence(
    blocs: list[dict],
    llm_fn: Callable,
    llm_cfg: dict,
) -> dict:
    """Call atlas-coherence agent. Returns {clean, conflicts}. Fails open (clean=True)."""
    try:
        from atlas_coherence.agent import AtlasCoherenceAgent  # noqa: PLC0415

        agent = AtlasCoherenceAgent(llm_fn=llm_fn, config={"llm": llm_cfg})
        events = list(agent.run(blocs=blocs))
        summary = next((e for e in events if e.get("summary")), None)
        if summary:
            return {"clean": summary.get("clean", True), "conflicts": summary.get("conflicts", [])}
    except ImportError:
        _log.debug("atlas-coherence not installed — skipping coherence check")
    except Exception as exc:  # noqa: BLE001
        _log.warning("atlas-coherence failed: %s — proceeding", exc)
    return {"clean": True, "conflicts": []}


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
        "called_by": bloc.get("called_by", ""),
        "justification": bloc.get("justification", ""),
        "criteria": bloc.get("criteria") or [],
        "agent_exists": bloc.get("agent_exists"),
        "reuse_decision": bloc.get("reuse_decision"),
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


def _gaps_to_questions(gaps: list[dict]) -> list[dict]:
    """Convert CDC gap dicts to pending-question format compatible with run_store."""
    import datetime  # noqa: PLC0415

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    questions = []
    for i, gap in enumerate(gaps):
        desc = gap.get("description", "")
        bloc = gap.get("bloc_concerned", "")
        context = f" (bloc : {bloc})" if bloc else ""
        questions.append(
            {
                "id": f"q_{i + 1:03d}",
                "agent": "cdc-router",
                "criterion": f"cdc_gap_{i + 1}",
                "question": f"{desc}{context}",
                "question_type": "text",
                "hint": gap.get("suggestion", ""),
                "options": [],
                "cause": "missing_spec",
                "gap_type": gap.get("type", "missing"),
                "impact": gap.get("impact", "high"),
                "created_at": now,
            }
        )
    return questions


def _get_run_store(run_store_dir: Path | None) -> object:
    try:
        from atlas_memory.run_store import RunStore  # noqa: PLC0415

        return RunStore(run_store_dir or _DEFAULT_RUN_STORE_DIR)
    except ImportError:
        _log.warning("atlas-memory not installed — run persistence disabled")
        return None
