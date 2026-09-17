"""Deterministic stats computed before calling the LLM."""

from __future__ import annotations

import statistics


def compute_stats(trend_points: list[dict]) -> dict:
    """Compute deterministic stats from trend points for the LLM prompt."""
    n = len(trend_points)
    confidence_tier = _confidence_tier(n)

    scores = [p["avg_score"] for p in trend_points if p.get("avg_score") is not None]
    score_trend = _trend_direction(scores)

    crit_stats = _per_criterion(trend_points)

    summary_lines = [
        f"N={n} runs | confidence={confidence_tier} | score trend={score_trend}",
        f"Score range: {min(scores):.2f}–{max(scores):.2f}" if scores else "No scores",
    ]
    for cname, cdata in crit_stats.items():
        line = f"  {cname}: avg={cdata['avg']:.1f} σ={cdata['std']:.2f} trend={cdata['trend']}"
        if cdata["unaddressed"]:
            line += " [UNADDRESSED]"
        summary_lines.append(line)

    return {
        "n_points": n,
        "confidence_tier": confidence_tier,
        "scores": scores,
        "score_trend": score_trend,
        "crit_stats": crit_stats,
        "stats_summary": "\n".join(summary_lines),
    }


def _confidence_tier(n: int) -> str:
    if n < 2:
        return "none"
    if n < 4:
        return "low"
    if n < 8:
        return "medium"
    return "high"


def _trend_direction(values: list[float]) -> str:
    if len(values) < 2:
        return "unknown"
    delta = values[-1] - values[0]
    if delta > 0.3:
        return "improving"
    if delta < -0.3:
        return "regressing"
    return "stable"


def _per_criterion(trend_points: list[dict]) -> dict[str, dict]:
    crit_scores: dict[str, list[float]] = {}
    crit_deltas: dict[str, list[float]] = {}

    for p in trend_points:
        for c in p.get("criteria", []):
            name = c.get("criterion", "")
            if not name:
                continue
            s = c.get("score")
            d = c.get("delta")
            if s is not None:
                crit_scores.setdefault(name, []).append(float(s))
            if d is not None:
                crit_deltas.setdefault(name, []).append(float(d))

    result = {}
    for name, sc_list in crit_scores.items():
        avg = statistics.mean(sc_list)
        std = statistics.stdev(sc_list) if len(sc_list) > 1 else 0.0
        deltas = crit_deltas.get(name, [])
        trend = _trend_direction(sc_list)
        unaddressed = (
            abs(avg - sc_list[0]) < 0.3 and len(sc_list) >= 2 and all(abs(d) < 0.5 for d in deltas)
        )
        result[name] = {
            "avg": avg,
            "std": std,
            "trend": trend,
            "unaddressed": unaddressed,
            "scores": sc_list,
        }
    return result
