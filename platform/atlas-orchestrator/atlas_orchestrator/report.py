"""Build the final orchestration report."""

from __future__ import annotations


def build_report(
    cdc_path: str,
    routing_summary: dict,
    bloc_results: list[dict],
) -> dict:
    """Assemble the structured report from orchestration results.

    Args:
        cdc_path:        CDC origin (path or "inline").
        routing_summary: Summary dict from cdc-router (blocs, pipeline, requires_creation).
        bloc_results:    List of per-bloc execution + judge results.
    """
    blocs_total = len(bloc_results)
    processed = [r for r in bloc_results if r["status"] != "skipped"]
    passed = [r for r in processed if r.get("judge_passed") is True]

    overall_passed = len(passed) == len(processed) and len(processed) > 0

    return {
        "cdc_path": cdc_path,
        "blocs_total": blocs_total,
        "blocs_processed": len(processed),
        "blocs_passed": len(passed),
        "pipeline": routing_summary.get("pipeline", []),
        "requires_creation": routing_summary.get("requires_creation", []),
        "requires_adaptation": routing_summary.get("requires_adaptation", []),
        "requires_tools": routing_summary.get("requires_tools", []),
        "requires_tools_adaptation": routing_summary.get("requires_tools_adaptation", []),
        "results": bloc_results,
        "overall_passed": overall_passed,
    }
