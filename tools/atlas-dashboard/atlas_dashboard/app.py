"""FastAPI application for ATLAS HITL dashboard."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import secrets
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from pathlib import Path as _Path

from atlas_memory.run_store import RunStore
from fastapi import Cookie, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

_DEFAULT_RUN_STORE_DIR = Path.home() / ".atlas" / "runs"

app = FastAPI(title="ATLAS Dashboard", version="1.0.0")
_store: RunStore | None = None

# Session store: cookie_value → True (in-memory, single-process)
_sessions: set[str] = set()


def _token() -> str | None:
    """Return the configured token, or None if auth is disabled."""
    return os.environ.get("ATLAS_DASHBOARD_TOKEN") or None


def _check_session(atlas_session: str | None) -> bool:
    if _token() is None:
        return True
    return atlas_session is not None and atlas_session in _sessions


def _require_auth(request: Request, atlas_session: str | None = Cookie(default=None)):
    if not _check_session(atlas_session):
        raise HTTPException(status_code=401, detail="Unauthorized")


def get_store() -> RunStore:
    global _store
    if _store is None:
        store_dir = Path(os.environ.get("ATLAS_RUN_STORE_DIR", str(_DEFAULT_RUN_STORE_DIR)))
        _store = RunStore(store_dir)
    return _store


# ── Auth ───────────────────────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
def ui_login():
    tpl = Path(__file__).parent / "templates" / "login.html"
    return HTMLResponse(tpl.read_text(encoding="utf-8"))


@app.post("/login")
async def do_login(request: Request):
    form = await request.form()
    password = form.get("password", "")
    token = _token()
    if token is None or secrets.compare_digest(
        hashlib.sha256(str(password).encode()).hexdigest(),
        hashlib.sha256(token.encode()).hexdigest(),
    ):
        session_id = secrets.token_hex(32)
        _sessions.add(session_id)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie("atlas_session", session_id, httponly=True, samesite="lax")
        return response
    return HTMLResponse(
        (Path(__file__).parent / "templates" / "login.html")
        .read_text(encoding="utf-8")
        .replace("<!--ERROR-->", '<p class="error">Invalid password.</p>'),
        status_code=401,
    )


@app.get("/logout")
def do_logout(atlas_session: str | None = Cookie(default=None)):
    if atlas_session:
        _sessions.discard(atlas_session)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("atlas_session")
    return response


# ── Auth middleware ────────────────────────────────────────────────────────────


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    public = {"/login", "/logout"}
    if request.url.path in public:
        return await call_next(request)
    if _token() is not None:
        session = request.cookies.get("atlas_session")
        if session not in _sessions:
            return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)


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


@app.delete("/api/runs/{run_id}")
def api_delete_run(run_id: str):
    store = get_store()
    path = store._path(run_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    path.unlink()
    return {"status": "deleted"}


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


# ── Scaffold / Install / Re-run ────────────────────────────────────────────────


def _llm_fn_for_dashboard():
    """Build LLM callable from atlas.toml (CWD) — same logic as atlas-cli."""
    try:
        from atlas_cli.config import llm_fn_from_config, load  # noqa: PLC0415

        cfg = load()
        return llm_fn_from_config(cfg)
    except Exception as exc:  # noqa: BLE001
        import logging  # noqa: PLC0415

        logging.getLogger("atlas_dashboard").error(
            "Cannot build LLM fn (is atlas.toml present in CWD?): %s", exc
        )
        raise HTTPException(status_code=500, detail=f"LLM not configured: {exc}") from exc


@app.post("/api/runs/{run_id}/scaffold/{agent_name}")
def api_scaffold(run_id: str, agent_name: str):
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    cdc_full = run.get("metadata", {}).get(
        "cdc_full", run.get("metadata", {}).get("cdc_preview", "")
    )
    if not cdc_full:
        raise HTTPException(status_code=400, detail="No CDC text stored for this run")

    try:
        from agent_forge.agent import AgentForgeAgent  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="agent-forge not installed") from exc

    llm_fn, llm_cfg = _llm_fn_for_dashboard()

    # Smoke-test the LLM before handing it to agent-forge
    _probe = llm_fn("Reply OK", llm_cfg, 8)
    if not _probe:
        raise HTTPException(
            status_code=500,
            detail="LLM probe failed — check atlas.toml [llm] config and credentials",
        )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(f"# Agent: {agent_name}\n\n{cdc_full}")
        spec_path = _Path(f.name)

    output_dir = _Path.home() / ".atlas" / "scaffolded"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        agent = AgentForgeAgent(output_dir=output_dir, config={"llm": llm_cfg}, llm_fn=llm_fn)
        events = list(agent.run(spec_path=spec_path))
        summary = next((e for e in events if e.get("summary")), None)
        if summary is None:
            errors = [e.get("msg", "") for e in events if e.get("level") == "error"]
            raise HTTPException(status_code=500, detail=f"Scaffolding failed: {errors}")

        output_path = summary["output_path"]
        spec = summary

        # Read generated files
        files: dict[str, str] = {}
        for p in _Path(output_path).rglob("*"):
            if p.is_file() and p.suffix in (".py", ".toml", ".md", ".json", ".txt"):
                with contextlib.suppress(Exception):
                    files[str(p.relative_to(output_path))] = p.read_text(encoding="utf-8")

        store.set_scaffold_result(run_id, agent_name, output_path, spec)
        return {"agent_name": agent_name, "output_path": output_path, "spec": spec, "files": files}
    finally:
        spec_path.unlink(missing_ok=True)


@app.post("/api/runs/{run_id}/install/{agent_name}")
def api_install(run_id: str, agent_name: str):
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    scaffolded = run.get("scaffolded", {}).get(agent_name)
    if not scaffolded:
        raise HTTPException(status_code=400, detail="Agent not scaffolded yet")

    output_path = scaffolded["output_path"]
    if not _Path(output_path).exists():
        raise HTTPException(status_code=400, detail=f"Scaffolded path not found: {output_path}")

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", output_path, "-q"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"pip install failed: {result.stderr[:500]}")

    # Register the new agent in the KG so cdc-router can find it at re-run
    _register_agent_in_kg(agent_name, output_path)

    store.set_agent_installed(run_id, agent_name)
    return {"status": "installed", "agent_name": agent_name}


def _register_agent_in_kg(agent_name: str, output_path: str) -> None:
    """Add the freshly installed agent to atlas-memory KG."""
    import logging  # noqa: PLC0415

    try:
        from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

        mem = AtlasMemoryAgent()
        events = list(
            mem.run(
                operation="register_agent",
                name=agent_name,
                capabilities=[f"Handles {agent_name} tasks as defined in CDC"],
                tags=["scaffolded"],
            )
        )
        err = next((e for e in events if e.get("level") == "error"), None)
        if err:
            logging.getLogger("atlas_dashboard").warning(
                "KG registration warning for %s: %s", agent_name, err.get("msg")
            )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "KG registration failed for %s (non-blocking): %s", agent_name, exc
        )


@app.post("/api/runs/{run_id}/rerun")
def api_rerun(run_id: str):
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    cdc_text = run.get("metadata", {}).get("cdc_full", "")
    if not cdc_text:
        raise HTTPException(status_code=400, detail="No CDC text stored for this run")

    try:
        from atlas_orchestrator.pipeline import orchestrate  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="atlas-orchestrator not installed") from exc

    llm_fn, llm_cfg = _llm_fn_for_dashboard()
    store_dir = _Path(os.environ.get("ATLAS_RUN_STORE_DIR", str(_DEFAULT_RUN_STORE_DIR)))
    report = orchestrate(cdc_text=cdc_text, llm_fn=llm_fn, llm_cfg=llm_cfg, run_store_dir=store_dir)
    if report is None:
        raise HTTPException(status_code=500, detail="Orchestration failed")

    return {"new_run_id": report.get("run_id"), "requires_review": report.get("requires_review")}


@app.get("/api/runs/{run_id}/agents/{agent_name}/download")
def api_download_agent(run_id: str, agent_name: str):
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    scaffolded = run.get("scaffolded", {}).get(agent_name)
    if not scaffolded:
        raise HTTPException(status_code=400, detail="Agent not scaffolded yet")

    output_path = _Path(scaffolded["output_path"])
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Scaffolded directory not found")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in output_path.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(output_path.parent))
    buf.seek(0)

    filename = f"{agent_name}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── UI ─────────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def ui_index():
    tpl = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(tpl.read_text(encoding="utf-8"))
