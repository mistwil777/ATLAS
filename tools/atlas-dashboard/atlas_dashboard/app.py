"""FastAPI application for ATLAS HITL dashboard."""

from __future__ import annotations

from pathlib import Path

from atlas_memory.run_store import RunStore
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

_DEFAULT_RUN_STORE_DIR = Path.home() / ".atlas" / "runs"

app = FastAPI(title="ATLAS Dashboard", version="1.0.0")
_store: RunStore | None = None


def get_store() -> RunStore:
    global _store
    if _store is None:
        store_dir = Path(
            __import__("os").environ.get("ATLAS_RUN_STORE_DIR", str(_DEFAULT_RUN_STORE_DIR))
        )
        _store = RunStore(store_dir)
    return _store


# ── API ────────────────────────────────────────────────────────────────────────


@app.get("/api/runs")
def api_list_runs(review: bool = False):
    return get_store().list_runs(only_review=review)


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str):
    run = get_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.post("/api/runs/{run_id}/approve")
def api_approve(run_id: str):
    store = get_store()
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    store.set_review_decision(run_id, "approved")
    return {"status": "approved"}


@app.post("/api/runs/{run_id}/reject")
def api_reject(run_id: str):
    store = get_store()
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    store.set_review_decision(run_id, "rejected")
    return {"status": "rejected"}


# ── Stats ──────────────────────────────────────────────────────────────────────


@app.get("/api/stats")
def api_stats():
    runs = get_store().list_runs()
    total = len(runs)
    if total == 0:
        return {
            "total_runs": 0,
            "pending": 0,
            "approved": 0,
            "rejected": 0,
            "review_rate": 0.0,
            "judge_pass_rate": 0.0,
            "by_agent": {},
        }

    status_counts: dict[str, int] = {"pending": 0, "approved": 0, "rejected": 0}
    review_count = 0
    agent_stats: dict[str, dict] = {}

    for run in runs:
        status_counts[run.get("status", "pending")] = (
            status_counts.get(run.get("status", "pending"), 0) + 1
        )
        if run.get("requires_review"):
            review_count += 1

        for bloc in run.get("blocs", []):
            agent = bloc.get("agent") or "unknown"
            if agent not in agent_stats:
                agent_stats[agent] = {"total": 0, "passed": 0, "failed": 0, "scores": []}
            agent_stats[agent]["total"] += 1
            jp = bloc.get("judge_passed")
            if jp is True:
                agent_stats[agent]["passed"] += 1
            elif jp is False:
                agent_stats[agent]["failed"] += 1
            score = bloc.get("judge_score")
            if score is not None:
                agent_stats[agent]["scores"].append(score)

    # Compute pass rates and avg scores per agent
    by_agent = {}
    total_judged = 0
    total_passed = 0
    for agent, s in agent_stats.items():
        judged = s["passed"] + s["failed"]
        total_judged += judged
        total_passed += s["passed"]
        by_agent[agent] = {
            "total_blocs": s["total"],
            "judge_pass_rate": round(s["passed"] / judged, 3) if judged else None,
            "avg_score": round(sum(s["scores"]) / len(s["scores"]), 3) if s["scores"] else None,
        }

    return {
        "total_runs": total,
        "pending": status_counts.get("pending", 0),
        "approved": status_counts.get("approved", 0),
        "rejected": status_counts.get("rejected", 0),
        "review_rate": round(review_count / total, 3) if total else 0.0,
        "judge_pass_rate": round(total_passed / total_judged, 3) if total_judged else None,
        "by_agent": by_agent,
    }


# ── UI ─────────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def ui_index():
    tpl = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(tpl.read_text(encoding="utf-8"))
