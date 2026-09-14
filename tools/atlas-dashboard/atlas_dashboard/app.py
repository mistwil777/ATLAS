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


# ── UI ─────────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def ui_index():
    tpl = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(tpl.read_text(encoding="utf-8"))
