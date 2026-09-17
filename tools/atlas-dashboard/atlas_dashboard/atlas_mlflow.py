"""MLflow integration for ATLAS — non-blocking, skips if mlflow not installed."""

from __future__ import annotations

import logging
import os
from pathlib import Path

_log = logging.getLogger("atlas_dashboard.mlflow")
_TRACKING_URI = os.environ.get(
    "MLFLOW_TRACKING_URI",
    str(Path.home() / ".atlas" / "mlflow"),
)


def _mlflow():
    """Return mlflow module or None if not installed or blocked by deployment mode."""
    # airgap mode: block remote MLflow URIs to prevent data egress
    if os.environ.get("ATLAS_MODE", "saas") == "airgap" and _TRACKING_URI.startswith("http"):
        _log.warning("MLflow remote URI blocked in deployment_mode=airgap — logging disabled")
        return None
    try:
        import mlflow  # noqa: PLC0415

        mlflow.set_tracking_uri(_TRACKING_URI)
        return mlflow
    except ImportError:
        return None


def log_scaffold_attempt(
    *,
    agent_name: str,
    attempt: int,
    judge_score: float | None,
    judge_passed: bool | None,
    criteria: list[dict],
    feedback: str,
    model_id: str,
    patches_applied: list[str],
    user_answers: list[str],
    run_id: str,
    atlas_run_id: str,
) -> None:
    ml = _mlflow()
    if ml is None:
        return
    try:
        ml.set_experiment(f"atlas/scaffold/{agent_name}")
        with ml.start_run(run_name=f"attempt-{attempt}"):
            ml.set_tags(
                {
                    "atlas.run_id": atlas_run_id,
                    "atlas.agent": agent_name,
                    "atlas.attempt": str(attempt),
                }
            )
            ml.log_params(
                {
                    "model_id": model_id or "unknown",
                    "attempt": attempt,
                    "patches_count": len(patches_applied),
                    "answers_count": len(user_answers),
                    "has_cr_context": bool(patches_applied or user_answers),
                }
            )
            if judge_score is not None:
                ml.log_metric("judge_score", judge_score)
            if judge_passed is not None:
                ml.log_metric("judge_passed", int(judge_passed))
            for c in criteria:
                name = c.get("criterion", "").replace(" ", "_").lower()[:50]
                score = c.get("score")
                if name and score is not None:
                    ml.log_metric(f"criterion/{name}", score)
            if patches_applied:
                ml.log_text("\n".join(patches_applied), "patches_applied.txt")
            if user_answers:
                ml.log_text("\n".join(a for a in user_answers if a.strip()), "user_answers.txt")
            if feedback:
                ml.log_text(feedback, "judge_feedback.txt")
    except Exception as exc:  # noqa: BLE001
        _log.warning("MLflow scaffold log failed (non-blocking): %s", exc)


def log_tool_build(
    *,
    tool_name: str,
    contract_passed: bool,
    module_name: str,
    functions: list[str],
    atlas_run_id: str,
) -> None:
    ml = _mlflow()
    if ml is None:
        return
    try:
        ml.set_experiment("atlas/tool-builds")
        with ml.start_run(run_name=tool_name):
            ml.set_tags(
                {
                    "atlas.run_id": atlas_run_id,
                    "atlas.tool": tool_name,
                }
            )
            ml.log_params(
                {
                    "module_name": module_name,
                    "functions_count": len(functions),
                }
            )
            ml.log_metric("contract_passed", int(contract_passed))
    except Exception as exc:  # noqa: BLE001
        _log.warning("MLflow tool build log failed (non-blocking): %s", exc)


def log_improvement_advice(
    *,
    agent_name: str,
    confidence_tier: str,
    pain_points_count: int,
    suggested_count: int,
    validated_count: int,
    atlas_run_id: str,
    advice_json: dict | None = None,
) -> None:
    ml = _mlflow()
    if ml is None:
        return
    try:
        ml.set_experiment(f"atlas/coaching/{agent_name}")
        with ml.start_run(run_name=f"advice-{atlas_run_id[:8]}"):
            ml.set_tags({"atlas.run_id": atlas_run_id, "atlas.agent": agent_name})
            ml.log_params({"confidence_tier": confidence_tier})
            ml.log_metrics(
                {
                    "pain_points_count": pain_points_count,
                    "suggested_count": suggested_count,
                    "validated_count": validated_count,
                }
            )
            if advice_json:
                import json as _json  # noqa: PLC0415

                ml.log_text(_json.dumps(advice_json, ensure_ascii=False, indent=2), "advice.json")
    except Exception as exc:  # noqa: BLE001
        _log.warning("MLflow coaching log failed (non-blocking): %s", exc)


def log_routing(
    *,
    atlas_run_id: str,
    blocs_count: int,
    requires_creation: list[str],
    requires_tools: list[str],
    agents_reused: list[str],
    tools_reused: list[str],
    cdc_preview: str,
) -> None:
    ml = _mlflow()
    if ml is None:
        return
    try:
        ml.set_experiment("atlas/routing")
        with ml.start_run(run_name=atlas_run_id[:8]):
            ml.set_tags({"atlas.run_id": atlas_run_id})
            ml.log_params(
                {
                    "blocs_count": blocs_count,
                    "requires_creation": ",".join(requires_creation) or "none",
                    "requires_tools": ",".join(requires_tools) or "none",
                    "cdc_preview": cdc_preview[:200],
                }
            )
            ml.log_metrics(
                {
                    "requires_creation_count": len(requires_creation),
                    "requires_tools_count": len(requires_tools),
                    "agents_reused_count": len(agents_reused),
                    "tools_reused_count": len(tools_reused),
                }
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning("MLflow routing log failed (non-blocking): %s", exc)
