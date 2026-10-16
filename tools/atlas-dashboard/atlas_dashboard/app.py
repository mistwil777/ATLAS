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
import threading
import uuid
import zipfile
from pathlib import Path
from pathlib import Path as _Path

from atlas_memory.run_store import RunStore
from fastapi import BackgroundTasks, Cookie, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


class TestRunRequest(BaseModel):
    project: str
    ticket: str
    batch_id: str = ""


class TestValidation(BaseModel):
    agent_name: str
    valid: bool = False
    comment: str = ""
    skipped: bool = False


class BatchGenerateRequest(BaseModel):
    project: str
    n: int = 7


class ScaffoldRequest(BaseModel):
    user_answers: list[str] = []
    selected_patches: list[str] = []
    judge_feedback_context: str = ""
    additional_context: str = ""  # extracted text from uploaded documents


class ApproveRequest(BaseModel):
    agent_names: list[str] | None = None


class ImproveRequest(BaseModel):
    run_id: str | None = None


class AnswerRequest(BaseModel):
    answers: dict[str, str]


_DEFAULT_RUN_STORE_DIR = Path.home() / ".atlas" / "runs"

app = FastAPI(title="ATLAS Dashboard", version="1.0.0")


@app.on_event("startup")
def _startup_recalculate_kg_scores() -> None:
    """Recalculate KG judge_score for all agents from latest test validations at startup."""
    import contextlib as _cl  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    with _cl.suppress(Exception):
        from atlas_memory.kg import KGStore  # noqa: PLC0415

        kg = KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")
        test_runs_dir = _Path.home() / ".atlas" / "test_runs"
        if not test_runs_dir.exists():
            return
        all_runs = sorted(
            [_json.loads(f.read_text(encoding="utf-8")) for f in test_runs_dir.glob("*.json")],
            key=lambda r: r.get("created_at", ""),
        )
        agent_names = [n for n in kg._g.nodes if kg._g.nodes[n].get("node_type") == "agent"]
        for agent_name in agent_names:
            latest_per_ticket: dict[str, dict] = {}
            for run in all_runs:
                v = run.get("validations", {}).get(agent_name, {})
                if v.get("skipped") or "valid" not in v:
                    continue
                ticket_key = (run.get("ticket") or run.get("test_id", ""))[:120]
                latest_per_ticket[ticket_key] = v
            total = len(latest_per_ticket)
            if not total:
                continue
            valid_count = sum(1 for v in latest_per_ticket.values() if v["valid"])
            human_score = round((valid_count / total) * 5.0, 2)
            kg.update_human_score(agent_name, human_score)


_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")
_store: RunStore | None = None

# Session store: cookie_value → True (in-memory, single-process)
_sessions: set[str] = set()

# Background job tracking: "{run_id}:{agent_name}" → {"status": "running"|"done"|"error", "error": str}
_bg_jobs: dict[str, dict] = {}

# Async pipeline jobs: job_id → {"stage": str, "run_id": str|None, "error": str|None}
# stages: "routing" | "done" | "error"
_pipeline_jobs: dict[str, dict] = {}


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


@app.post("/api/runs")
async def api_new_run(
    file: UploadFile,  # multipart file upload
    project_name: str = Form(""),
) -> dict:
    """Upload a CDC file and launch the pipeline asynchronously. Returns job_id immediately."""
    suffix = _Path(file.filename or "cdc.txt").suffix.lower() or ".txt"
    content = await file.read()

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        if suffix in (".txt", ".md", ".rst"):
            cdc_text = content.decode("utf-8", errors="replace")
        else:
            try:
                from pair_forge.extractors import extract_content  # noqa: PLC0415

                cdc_text, _ = extract_content(_Path(tmp_path))
            except ImportError:
                cdc_text = content.decode("utf-8", errors="replace")
    finally:
        with contextlib.suppress(OSError):
            _Path(tmp_path).unlink()

    if not cdc_text.strip():
        raise HTTPException(status_code=400, detail="CDC file is empty or unreadable")

    try:
        from atlas_orchestrator.pipeline import orchestrate  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="atlas-orchestrator not installed") from exc

    llm_fn, llm_cfg = _llm_fn_for_dashboard()
    _probe = llm_fn("Reply OK", llm_cfg, 8)
    if not _probe:
        raise HTTPException(
            status_code=500,
            detail="LLM probe failed — check atlas.toml [llm] config and credentials",
        )

    store_dir = _Path(os.environ.get("ATLAS_RUN_STORE_DIR", str(_DEFAULT_RUN_STORE_DIR)))
    if project_name.strip():
        project_dir = str(_Path.home() / ".atlas" / "projects" / project_name.strip())
        _Path(project_dir).mkdir(parents=True, exist_ok=True)
    else:
        project_dir = str(_Path.home() / ".atlas" / "scaffolded")

    job_id = str(uuid.uuid4())
    _pipeline_jobs[job_id] = {"stage": "routing", "run_id": None, "error": None}

    def _run_pipeline() -> None:
        try:
            report = orchestrate(
                cdc_text=cdc_text,
                llm_fn=llm_fn,
                llm_cfg=llm_cfg,
                run_store_dir=store_dir,
                project_dir=project_dir,
                project_owner="client",
            )
            if report is None:
                _pipeline_jobs[job_id] = {
                    "stage": "error",
                    "run_id": None,
                    "error": "Pipeline failed — check dashboard logs",
                }
            else:
                _pipeline_jobs[job_id] = {
                    "stage": "done",
                    "run_id": report.get("run_id"),
                    "error": None,
                }
        except Exception as exc:  # noqa: BLE001
            _pipeline_jobs[job_id] = {"stage": "error", "run_id": None, "error": str(exc)[:300]}

    threading.Thread(target=_run_pipeline, daemon=True).start()
    return {"job_id": job_id, "status": "processing"}


@app.get("/api/pipeline-jobs/{job_id}")
def api_pipeline_job_status(job_id: str) -> dict:
    """Poll async pipeline job status. Returns stage, run_id (when done), error (on failure)."""
    job = _pipeline_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str):
    run = get_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/bg-job/{job_key:path}")
def api_bg_job(job_key: str):
    """Return current status of a background job."""
    job = _bg_jobs.get(job_key)
    if job is None:
        return {"status": "unknown"}
    return job


@app.post("/api/runs/{run_id}/approve")
def api_approve(run_id: str, body: ApproveRequest | None = None):
    store = get_store()
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    agent_names = (body.agent_names if body else None) or None
    store.set_review_decision(run_id, "approved", agent_names=agent_names)
    return {"status": "approved"}


@app.post("/api/runs/{run_id}/reject")
def api_reject(run_id: str):
    store = get_store()
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    store.set_review_decision(run_id, "rejected")
    return {"status": "rejected"}


def _resume_pipeline_with_answers(
    run_id: str,
    run: dict,
    criterion_answers: dict,
    store,
) -> dict:
    """Re-run orchestrate() with the CDC enriched by client answers (CDC-gap flow)."""
    try:
        from atlas_orchestrator.pipeline import orchestrate  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="atlas-orchestrator not installed") from exc

    cdc_full = run.get("metadata", {}).get("cdc_full", "")
    if not cdc_full:
        raise HTTPException(status_code=400, detail="No CDC text stored for this run")

    # Append answers as clarifications at the end of the CDC
    answers_block = "\n\n## Précisions apportées par le client\n" + "\n".join(
        f"- {criterion}: {answer}" for criterion, answer in criterion_answers.items()
    )
    enriched_cdc = cdc_full + answers_block

    llm_fn, llm_cfg = _llm_fn_for_dashboard()
    store_dir = _Path(os.environ.get("ATLAS_RUN_STORE_DIR", str(_DEFAULT_RUN_STORE_DIR)))

    meta = run.get("metadata", {})
    report = orchestrate(
        cdc_text=enriched_cdc,
        llm_fn=llm_fn,
        llm_cfg=llm_cfg,
        run_store_dir=store_dir,
        project_dir=meta.get("project_dir", ""),
        project_owner=meta.get("project_owner", "client"),
        skip_gap_criteria=list(criterion_answers.keys()),
    )
    if report is None:
        raise HTTPException(status_code=500, detail="Orchestration failed after client answers")

    # Index resolved CDC gaps if the new run succeeded (no further pending questions)
    new_pending = report.get("pending_questions", [])
    if not new_pending and report.get("status") != "waiting_for_client":
        pending_qs = run.get("pending_questions", [])
        for q in pending_qs:
            criterion = q.get("criterion", q.get("id", ""))
            answer = criterion_answers.get(criterion, "")
            if answer:
                resolution_key = f"client_answer_{criterion}"
                # Index each answered gap as a client_decision
                _index_resolution_in_rag(
                    agent_name="cdc-router",
                    criterion=criterion,
                    cause="missing_spec",
                    resolution=answer,
                    entry_type="client_decision",
                )
                _add_resolved_by_edges("cdc-router", criterion, resolution_key)

    new_run_id = report.get("run_id")
    if new_run_id:
        import contextlib as _cl  # noqa: PLC0415

        with _cl.suppress(Exception):
            store.mark_resumed(run_id, new_run_id)

    return {
        "status": "resumed",
        "new_run_id": new_run_id,
        "pending_questions": new_pending,
        "run_status": report.get("status", "pending"),
    }


@app.post("/api/runs/{run_id}/answer")
def api_answer(run_id: str, body: AnswerRequest):
    """Receive client answers for a waiting_for_client run and resume scaffold."""
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("status") != "waiting_for_client":
        raise HTTPException(status_code=400, detail="Run is not waiting for client answers")

    # Map question ids to criterion names
    pending_qs = {q["id"]: q["criterion"] for q in run.get("pending_questions", [])}
    criterion_answers = {
        pending_qs[qid]: ans for qid, ans in body.answers.items() if qid in pending_qs
    }

    store.set_client_answers(run_id, criterion_answers)

    agent_name = run.get("pending_agent", "")
    if not agent_name:
        raise HTTPException(status_code=400, detail="No pending agent found in run")

    # CDC-gap flow: gaps came from cdc-router pass 0 — re-run pipeline with enriched CDC
    if agent_name == "cdc-router":
        return _resume_pipeline_with_answers(run_id, run, criterion_answers, store)

    agent_state = run.get("agent_state", {}).get(agent_name, {})
    saved_cr_context = agent_state.get("cr_context", "")
    saved_attempts_log = agent_state.get("attempts_log", [])
    saved_user_answers = agent_state.get("user_answers", [])
    saved_selected_patches = agent_state.get("selected_patches", [])

    # Rebuild context with client answers injected
    ctx = _build_consolidated_context(
        agent_name, saved_attempts_log, client_answers=criterion_answers
    )
    formatted = _format_consolidated_context_for_prompt(ctx)
    new_cr_context = saved_cr_context + ("\n\n" + formatted if formatted else "")

    try:
        from agent_forge.agent import AgentForgeAgent  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="agent-forge not installed") from exc

    llm_fn, llm_cfg = _llm_fn_for_dashboard()
    cdc_full = run.get("metadata", {}).get(
        "cdc_full", run.get("metadata", {}).get("cdc_preview", "")
    )

    agent_slug = _agent_slug(agent_name)
    meta = run.get("metadata", {})
    project_dir = meta.get("project_dir", "")
    if meta.get("project_owner") == "capgemini" or not project_dir:
        base_output_dir = _Path.home() / ".atlas" / "scaffolded"
    else:
        base_output_dir = _Path(project_dir) / "agents"

    body_compat = ScaffoldRequest(
        user_answers=saved_user_answers,
        selected_patches=saved_selected_patches,
    )

    (
        success,
        result,
        attempts_log2,
        last_output_path,
        last_spec,
        last_files,
        last_js,
        last_jc,
        last_jf,
    ) = _run_scaffold_cycle(
        cr_context=new_cr_context,
        cdc_full=cdc_full,
        agent_slug=agent_slug,
        agent_name=agent_name,
        run_id=run_id,
        store=store,
        base_output_dir=base_output_dir,
        llm_fn=llm_fn,
        llm_cfg=llm_cfg,
        body=body_compat,
        forge_cls=AgentForgeAgent,
    )
    if success:
        result["resumed_from_client_answers"] = True  # type: ignore[index]
        _index_scaffold_resolutions(agent_name, saved_attempts_log, attempts_log2)
        return result

    # Still failing — try advisor then diagnose
    all_attempts = saved_attempts_log + attempts_log2
    advisor_actions = _run_improvement_advisor(agent_name, store)
    if advisor_actions:
        advisor_context = (
            new_cr_context
            + _build_prev_files_section(agent_name, store)
            + "\n\n# Corrections requises (improvement-advisor) :\n"
            + "\n".join(f"- {a['action']}" for a in advisor_actions)
        )
        (
            success3,
            result3,
            attempts_log3,
            last_output_path,
            last_spec,
            last_files,
            last_js,
            last_jc,
            last_jf,
        ) = _run_scaffold_cycle(
            cr_context=advisor_context,
            cdc_full=cdc_full,
            agent_slug=agent_slug,
            agent_name=agent_name,
            run_id=run_id,
            store=store,
            base_output_dir=base_output_dir,
            llm_fn=llm_fn,
            llm_cfg=llm_cfg,
            body=body_compat,
            forge_cls=AgentForgeAgent,
        )
        all_attempts.extend(attempts_log3)
        if success3:
            result3["advisor_used"] = True  # type: ignore[index]
            result3["resumed_from_client_answers"] = True  # type: ignore[index]
            _index_scaffold_resolutions(agent_name, saved_attempts_log, attempts_log3)
            return result3

    diagnosis = _run_diagnose(all_attempts, agent_name, cdc_full, llm_fn, llm_cfg)
    store.set_scaffold_result(
        run_id,
        agent_name,
        last_output_path,
        last_spec,
        user_answers=saved_user_answers,
        selected_patches=saved_selected_patches,
        model_id=llm_cfg.get("model_id", ""),
        judge_score=last_js,
        judge_criteria=last_jc,
        judge_feedback=last_jf,
        files=last_files,
    )
    store.set_scaffold_diagnosis(run_id, agent_name, diagnosis)
    return {
        "agent_name": agent_name,
        "output_path": last_output_path,
        "spec": last_spec,
        "files": last_files,
        "attempts": len(all_attempts),
        "diagnosis": diagnosis,
    }


@app.delete("/api/runs/{run_id}")
def api_delete_run(run_id: str):
    import logging  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # 1. Supprimer les fichiers scaffoldés sur disque
    deleted_paths: list[str] = []
    for _agent_name, sc in (run.get("scaffolded") or {}).items():
        output_path = sc.get("output_path", "")
        if output_path:
            p = _Path(output_path)
            if p.exists():
                try:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                    deleted_paths.append(output_path)
                except OSError as exc:
                    logging.getLogger("atlas_dashboard").warning(
                        "delete_run: could not remove %s: %s", output_path, exc
                    )

    # 2. Invalider les entrées RAG liées à ce run (fail-open)
    rag_invalidated = 0
    try:
        from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

        mem = AtlasMemoryAgent()
        events = list(
            mem.run(operation="invalidate_by_run", run_id=run_id, invalidated_by="user_delete")
        )
        summary = next((e for e in events if e.get("summary")), None)
        if summary:
            rag_invalidated = summary["result"].get("invalidated", 0)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "delete_run: RAG invalidation failed (non-blocking): %s", exc
        )

    # 3. Supprimer le fichier JSON du run store
    store._path(run_id).unlink(missing_ok=True)

    return {
        "status": "deleted",
        "run_id": run_id,
        "scaffold_paths_removed": deleted_paths,
        "rag_entries_invalidated": rag_invalidated,
    }


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

        # Enrich from scaffolded results (judge_score stored there, not in blocs)
        for agent, sc in run.get("scaffolded", {}).items():
            if agent not in agent_stats:
                agent_stats[agent] = {"total": 0, "passed": 0, "failed": 0, "scores": []}
            score = sc.get("judge_score")
            if score is not None and score not in agent_stats[agent]["scores"]:
                agent_stats[agent]["scores"].append(score)
                quality_threshold = 4.0
                if score >= quality_threshold:
                    agent_stats[agent]["passed"] += 1
                else:
                    agent_stats[agent]["failed"] += 1

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


def _build_trend_points(agent: str, runs: list[dict]) -> list[dict]:
    """Build trend points for a given agent from sorted runs."""
    points: list[dict] = []
    prev_model: str | None = None
    prev_criteria: dict[str, int] = {}

    for run in runs:
        blocs = [b for b in run.get("blocs", []) if b.get("agent") == agent]
        if not blocs:
            continue

        scores = [b["judge_score"] for b in blocs if b.get("judge_score") is not None]
        avg = round(sum(scores) / len(scores), 2) if scores else None

        criteria = next((b.get("judge_criteria", []) for b in blocs if b.get("judge_criteria")), [])
        feedback = next((b.get("judge_feedback", "") for b in blocs if b.get("judge_feedback")), "")

        scaffolded = run.get("scaffolded", {}).get(agent, {})
        user_answers = scaffolded.get("user_answers", [])
        selected_patches = scaffolded.get("selected_patches", [])
        model_id = scaffolded.get("model_id", "")

        if model_id and prev_model and model_id != prev_model:
            cause = "model_change"
        elif user_answers or selected_patches:
            cause = "cr_retry"
        else:
            cause = "initial_scaffold"

        delta = None
        if points and avg is not None and points[-1].get("avg_score") is not None:
            delta = round(avg - points[-1]["avg_score"], 2)

        crit_map = {c.get("criterion", ""): c.get("score") for c in criteria}
        crit_delta = []
        for c in criteria:
            name = c.get("criterion", "")
            score = c.get("score")
            prev_score = prev_criteria.get(name)
            crit_delta.append(
                {
                    "criterion": name,
                    "score": score,
                    "prev_score": prev_score,
                    "delta": round(score - prev_score, 1)
                    if score is not None and prev_score is not None
                    else None,
                    "comment": c.get("comment", ""),
                }
            )

        pt: dict = {
            "run_id": run["run_id"],
            "created_at": run.get("created_at", ""),
            "avg_score": avg,
            "cause": cause,
            "criteria": crit_delta,
            "feedback": feedback,
            "patches_applied": selected_patches,
            "user_answers": user_answers,
            "model_id": model_id,
        }
        if delta is not None:
            pt["delta"] = delta
        if cause == "model_change" and prev_model:
            pt["model_prev"] = prev_model

        points.append(pt)
        prev_model = model_id or prev_model
        prev_criteria = crit_map

    return points


def _build_trend_points_from_scaffold(agent: str, runs: list[dict]) -> list[dict]:
    """Fallback: build trend points from scaffold judge data stored in run['scaffolded']."""
    points: list[dict] = []
    for run in runs:
        scaffolded = run.get("scaffolded", {}).get(agent)
        if not scaffolded:
            continue
        judge_score = scaffolded.get("judge_score")
        judge_criteria = scaffolded.get("judge_criteria") or []
        judge_feedback = scaffolded.get("judge_feedback") or ""
        model_id = scaffolded.get("model_id", "")
        selected_patches = scaffolded.get("selected_patches") or []
        user_answers = scaffolded.get("user_answers") or []
        if judge_score is None and not judge_criteria:
            continue
        avg = round(judge_score, 2) if judge_score is not None else None
        cause = "cr_retry" if (user_answers or selected_patches) else "initial_scaffold"
        crit_delta = [
            {
                "criterion": c.get("criterion", ""),
                "score": c.get("score"),
                "prev_score": None,
                "delta": None,
                "comment": c.get("comment", ""),
            }
            for c in judge_criteria
        ]
        points.append(
            {
                "run_id": run.get("run_id", ""),
                "created_at": run.get("created_at", ""),
                "avg_score": avg,
                "cause": cause,
                "source": "scaffold",
                "criteria": crit_delta,
                "feedback": judge_feedback,
                "patches_applied": selected_patches,
                "user_answers": user_answers,
                "model_id": model_id,
            }
        )
    return points


def _get_latest_files_for_agent(agent_name: str, runs: list[dict]) -> dict[str, str]:
    """Return the most recent scaffolded files for agent_name across all runs."""
    for run in reversed(runs):
        entry = (run.get("scaffolded") or {}).get(agent_name)
        if entry and entry.get("files"):
            return entry["files"]
    return {}


_FORGE_KEY_FILES = ("agent.py", "prompts.py", "__init__.py", "__main__.py")


def _build_prev_files_section(agent_name: str, store) -> str:
    """Format previously generated files for injection into forge spec context."""
    runs = sorted(store.list_runs(), key=lambda r: r.get("created_at", ""))
    files = _get_latest_files_for_agent(agent_name, runs)
    if not files:
        return ""
    snippets = [
        f"### {fname}\n```python\n{content[:3000]}\n```"
        for fname, content in files.items()
        if any(fname.endswith(k) for k in _FORGE_KEY_FILES) and "/tests/" not in fname
    ]
    if not snippets:
        return ""
    return "\n\n# Code actuel (à corriger, NE PAS réécrire from scratch) :\n" + "\n\n".join(
        snippets
    )


def _build_trend_points_per_attempt(agent: str, runs: list[dict]) -> list[dict]:
    """Build one trend point per scaffold attempt from run['scaffold_attempts'][agent]."""
    points: list[dict] = []
    prev_criteria: dict[str, int] = {}

    for run in runs:
        attempts = run.get("scaffold_attempts", {}).get(agent)
        if not attempts:
            continue
        for entry in attempts:
            score = entry.get("judge_score")
            criteria = entry.get("judge_criteria") or []
            feedback = entry.get("judge_feedback") or ""
            model_id = entry.get("model_id", "")
            patches = entry.get("patches_applied") or []
            answers = entry.get("user_answers") or []
            attempt_num = entry.get("attempt", 1)

            avg = round(score, 2) if score is not None else None
            if patches or answers:
                cause = "cr_retry"
            elif attempt_num == 1:
                cause = "initial_scaffold"
            else:
                cause = "retry"

            delta = None
            if points and avg is not None and points[-1].get("avg_score") is not None:
                delta = round(avg - points[-1]["avg_score"], 2)

            crit_map = {c.get("criterion", ""): c.get("score") for c in criteria}
            crit_delta = [
                {
                    "criterion": c.get("criterion", ""),
                    "score": c.get("score"),
                    "prev_score": prev_criteria.get(c.get("criterion", "")),
                    "delta": round(c["score"] - prev_criteria[c["criterion"]], 1)
                    if c.get("score") is not None and c.get("criterion") in prev_criteria
                    else None,
                    "comment": c.get("comment", ""),
                }
                for c in criteria
            ]

            pt: dict = {
                "run_id": run["run_id"],
                "created_at": entry.get("timestamp") or run.get("created_at", ""),
                "avg_score": avg,
                "cause": cause,
                "source": "scaffold",
                "criteria": crit_delta,
                "feedback": feedback,
                "patches_applied": patches,
                "user_answers": answers,
                "model_id": model_id,
                "attempt": attempt_num,
            }
            if delta is not None:
                pt["delta"] = delta
            points.append(pt)
            prev_criteria = crit_map

    return points


def _has_scores(points: list[dict]) -> bool:
    return any(p.get("avg_score") is not None for p in points)


def _build_trend_points_from_test_runs(agent: str) -> list[dict]:
    """Build trend points from KG score_history entries with source='test'.

    Groups entries by run_id → one point per test session.
    Each point embeds individual ticket details for the drill-down view.
    """
    from collections import defaultdict  # noqa: PLC0415

    try:
        from atlas_memory.kg import KGStore  # noqa: PLC0415

        kg = KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")
        node = kg._g.nodes.get(agent, {})
        history = node.get("score_history", [])
    except Exception:  # noqa: BLE001
        return []

    test_entries = [h for h in history if h.get("source") == "test" and h.get("score") is not None]
    if not test_entries:
        return []

    # Group by run_id, preserving insertion order (chronological)
    groups: dict[str, list[dict]] = defaultdict(list)
    for entry in test_entries:
        groups[entry.get("run_id", "")].append(entry)

    points = []
    prev_score = None
    for run_id, entries in groups.items():
        scores = [e["score"] for e in entries]
        avg = round(sum(scores) / len(scores), 2)
        created_at = min(e.get("timestamp", "") for e in entries)
        delta = round(avg - prev_score, 2) if prev_score is not None else None

        tickets = [
            {
                "score": e["score"],
                "passed": e["score"] >= 4,
                "input": e.get("test_detail", {}).get("input", {}),
                "output": e.get("test_detail", {}).get("output", ""),
                "feedback": e.get("test_detail", {}).get("feedback", ""),
                "criteria": e.get("test_detail", {}).get("criteria", []),
                "timestamp": e.get("timestamp", ""),
            }
            for e in entries
        ]

        pt: dict = {
            "run_id": run_id,
            "created_at": created_at,
            "avg_score": avg,
            "cause": "test_execution",
            "source": "test",
            "tickets": tickets,
            "n_tickets": len(tickets),
            "n_passed": sum(1 for t in tickets if t["passed"]),
            "criteria": [],
            "feedback": f"{sum(1 for t in tickets if t['passed'])}/{len(tickets)} tickets passés",
            "patches_applied": [],
            "user_answers": [],
            "model_id": "",
        }
        if delta is not None:
            pt["delta"] = delta
        points.append(pt)
        prev_score = avg
    return points


@app.get("/api/stats/trend")
def api_stats_trend(agent: str):
    runs = sorted(get_store().list_runs(), key=lambda r: r.get("created_at", ""))
    points = _build_trend_points_per_attempt(agent, runs)
    if not _has_scores(points):
        points = _build_trend_points(agent, runs)
    if not _has_scores(points):
        points = _build_trend_points_from_scaffold(agent, runs)
    # Merge test-validation points (append after scaffold points)
    test_points = _build_trend_points_from_test_runs(agent)
    if test_points:
        # Recalculate delta for the seam
        if points and test_points:
            last_scaffold = next(
                (p["avg_score"] for p in reversed(points) if p.get("avg_score") is not None), None
            )
            if last_scaffold is not None and test_points[0].get("avg_score") is not None:
                test_points[0]["delta"] = round(test_points[0]["avg_score"] - last_scaffold, 2)
        points = points + test_points
    return {"agent": agent, "points": points}


@app.post("/api/improve/{agent_name}")
def api_improve(agent_name: str, body: ImproveRequest | None = None):
    body = body or ImproveRequest()
    runs = sorted(get_store().list_runs(), key=lambda r: r.get("created_at", ""))
    trend_points = _build_trend_points(agent_name, runs)
    if not trend_points:
        # Fallback: check scaffold attempts stored under run["scaffolded"]
        trend_points = _build_trend_points_from_scaffold(agent_name, runs)

    if not trend_points:
        return {
            "error": f"No trend data found for agent '{agent_name}'. "
            "Run at least one atlas pipeline with this agent to generate judge scores."
        }

    try:
        from improvement_advisor.agent import ImprovementAdvisorAgent  # noqa: PLC0415
    except ImportError:
        return {
            "error": "improvement-advisor not installed — pip install -e agents/improvement-advisor"
        }

    llm_fn, llm_cfg = _llm_fn_for_dashboard()
    generated_files = _get_latest_files_for_agent(agent_name, runs)
    advisor = ImprovementAdvisorAgent(config={"llm": llm_cfg}, llm_fn=llm_fn)
    events = list(
        advisor.run(
            agent_name=agent_name,
            trend_points=trend_points,
            generated_files=generated_files,
        )
    )
    summary = next((e for e in events if e.get("summary")), None)
    if not summary:
        errors = [e.get("msg", "") for e in events if e.get("level") == "error"]
        return {"error": f"improvement-advisor failed: {'; '.join(errors) or 'no output from LLM'}"}

    run_id = body.run_id
    if run_id:
        with contextlib.suppress(Exception):
            get_store().set_improvement_result(run_id, agent_name, summary)

    from atlas_dashboard.atlas_mlflow import log_improvement_advice  # noqa: PLC0415

    log_improvement_advice(
        agent_name=agent_name,
        confidence_tier=summary.get("confidence_tier", "none"),
        pain_points_count=len(summary.get("pain_points", [])),
        suggested_count=len(summary.get("suggested_actions", [])),
        validated_count=0,
        atlas_run_id=run_id or "unknown",
        advice_json=summary,
    )

    return summary


# ── Scaffold / Install / Re-run ────────────────────────────────────────────────


def _agent_slug(agent_name: str) -> str:
    """Convert agent name to kebab-case slug for filesystem use.

    Strips the fork suffix (#context) before conversion so that
    JustificationGenerator#sncf and JustificationGenerator share the same
    base slug on disk — the forge operates on base code, the KG tracks the fork.
    """
    import re as _re  # noqa: PLC0415

    base = agent_name.split("#")[0]
    return _re.sub(r"(?<!^)(?=[A-Z])", "-", base).lower()


def _llm_fn_for_dashboard():
    """Build LLM callable from atlas.toml (CWD) — same logic as atlas-cli."""
    try:
        import os  # noqa: PLC0415

        from atlas_cli.config import llm_fn_from_config, load  # noqa: PLC0415

        cfg = load()
        # propagate deployment_mode to env so downstream components respect it
        os.environ.setdefault("ATLAS_MODE", cfg.get("project", {}).get("deployment_mode", "saas"))
        os.environ.setdefault(
            "ATLAS_CROSS_LEARNING",
            str(cfg.get("project", {}).get("cross_learning", True)).lower(),
        )
        return llm_fn_from_config(cfg)
    except Exception as exc:  # noqa: BLE001
        import logging  # noqa: PLC0415

        logging.getLogger("atlas_dashboard").error(
            "Cannot build LLM fn (is atlas.toml present in CWD?): %s", exc
        )
        raise HTTPException(status_code=500, detail=f"LLM not configured: {exc}") from exc


def _build_consolidated_context(
    agent_name: str,
    attempts_log: list[dict],
    client_answers: dict | None = None,
) -> dict:
    """Build a structured failure summary from scaffold attempts history.

    Replaces raw cr_context string concatenation with a deduplicated, cause-classified dict.
    client_answers is for Step 4 (waiting_for_client) — included now with default {}.
    """
    client_answers = client_answers or {}
    history: dict[str, dict] = {}

    for attempt_entry in attempts_log:
        for cs in attempt_entry.get("criteria_scores", []):
            name = cs.get("criterion", "")
            score = cs.get("score")
            cause = cs.get("cause")
            if not name or score is None or score >= 4:
                continue
            if name not in history:
                history[name] = {"occurrences": 0, "last_score": score, "last_cause": cause}
            history[name]["occurrences"] += 1
            history[name]["last_score"] = score
            if cause is not None:
                history[name]["last_cause"] = cause

    open_issues = [
        {
            "criterion": name,
            "cause": data["last_cause"],
            "occurrences": data["occurrences"],
            "last_score": data["last_score"],
        }
        for name, data in history.items()
    ]

    return {
        "agent_name": agent_name,
        "client_answers": client_answers,
        "open_issues": open_issues,
        "failed_criteria_history": history,
    }


def _format_consolidated_context_for_prompt(ctx: dict) -> str:
    """Format a consolidated context dict as a clean prompt section (no raw accumulation)."""
    lines: list[str] = []
    open_issues = ctx.get("open_issues", [])
    if not open_issues and not ctx.get("client_answers"):
        return ""

    fixable = [i for i in open_issues if i.get("cause") in ("model_error", "code_error", None)]
    missing = [i for i in open_issues if i.get("cause") == "missing_spec"]

    if fixable:
        lines.append("# Criteria to fix (repeated failures — must address):")
        for issue in fixable:
            n = issue["occurrences"]
            lines.append(f"- {issue['criterion']} (failed {n}x, last score={issue['last_score']})")

    if missing:
        if lines:
            lines.append("")
        lines.append("# Behaviors not specified in the CDC — apply a reasonable default:")
        for issue in missing:
            lines.append(f"- {issue['criterion']}")

    client_answers = ctx.get("client_answers", {})
    if client_answers:
        if lines:
            lines.append("")
        lines.append("# Client decisions (authoritative — apply exactly):")
        for criterion, answer in client_answers.items():
            lines.append(f"- {criterion}: {answer}")

    return "\n".join(lines)


_GENERATE_QUESTIONS_PROMPT = """\
Tu es un assistant de projet IA. Un agent nommé '{agent_name}' a été construit pour ce projet, \
mais certains comportements ne sont pas spécifiés dans le cahier des charges.

Avant de finaliser cet agent, formule des questions précises pour le client.

## Critères non spécifiés ({n_issues})
{issues_block}

## Extrait du cahier des charges
{cdc_excerpt}

## Règles de formulation
- Formule des questions qui montrent ta compréhension du projet, pas ton ignorance
- N'utilise jamais : "manquant", "non spécifié", "information manquante", "missing_spec"
- Chaque question doit être autonome et compréhensible sans contexte technique
- Choisis question_type selon la nature du critère :
  "text"            → réponse libre (valeurs métier, libellés, règles)
  "single_choice"   → choix unique parmi 2-4 options
  "multiple_choice" → sélection multiple parmi des options
- hint : exemple concret ou liste des options si question_type != "text"
- Maximum {max_questions} questions, priorisées par impact sur le comportement de l'agent
- Pour single_choice et multiple_choice, inclure le champ "options" avec la liste

## Format — JSON strict, tableau, aucun texte avant/après
[
  {{
    "criterion": "<nom du critère>",
    "question": "<question client-facing>",
    "question_type": "text"|"single_choice"|"multiple_choice",
    "hint": "<exemple ou liste d'options>",
    "options": ["<option1>", "<option2>"]
  }}
]
"""


def _generate_questions(
    missing_spec_issues: list[dict],
    agent_name: str,
    cdc_text: str,
    llm_fn,
    llm_cfg: dict,
    max_questions: int = 3,
) -> list[dict]:
    """Generate structured client questions for missing_spec criteria.

    Returns list of question dicts with id, question_type and hint (max max_questions).
    """
    import json as _json  # noqa: PLC0415
    import logging as _logging  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    if not missing_spec_issues:
        return []

    issues = missing_spec_issues[:max_questions]
    issues_block = "\n".join(
        f"- {i['criterion']} (failed {i.get('occurrences', 1)}x)" for i in issues
    )
    prompt = _GENERATE_QUESTIONS_PROMPT.format(
        agent_name=agent_name,
        n_issues=len(issues),
        issues_block=issues_block,
        cdc_excerpt=cdc_text[:600],
        max_questions=max_questions,
    )

    raw = llm_fn(prompt, llm_cfg, 1024)
    if not raw:
        return []

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()
    start = raw.find("[")
    end = raw.rfind("]") + 1
    if start == -1 or end <= 0:
        return []

    try:
        questions_raw = _json.loads(raw[start:end])
    except _json.JSONDecodeError:
        _logging.getLogger("atlas_dashboard").warning("_generate_questions: JSON parse failed")
        return []

    now = datetime.now(timezone.utc).isoformat()
    questions = []
    for i, q in enumerate(questions_raw[:max_questions]):
        criterion = q.get("criterion", "")
        question = q.get("question", "")
        if not criterion or not question:
            continue
        questions.append(
            {
                "id": f"q_{i + 1:03d}",
                "agent": agent_name,
                "criterion": criterion,
                "question": question,
                "question_type": q.get("question_type", "text"),
                "hint": q.get("hint", ""),
                "options": q.get("options", []),
                "cause": "missing_spec",
                "created_at": now,
            }
        )

    return questions


def _run_scaffold_cycle(
    cr_context: str,
    cdc_full: str,
    agent_slug: str,
    agent_name: str,
    run_id: str,
    store,
    base_output_dir: _Path,
    llm_fn,
    llm_cfg: dict,
    body: ScaffoldRequest,
    forge_cls,
    max_attempts: int = 3,
    quality_threshold: float = 4.0,
) -> tuple[bool, dict | None, list[dict], str, dict, dict, float | None, list, str]:
    """Run one scaffold cycle (up to max_attempts). Returns (success, result, attempts_log,
    last_output_path, last_spec, last_files, last_judge_score, last_judge_criteria, last_judge_feedback).
    result is None on failure."""
    attempts_log: list[dict] = []
    last_output_path = ""
    last_spec: dict = {}
    last_files: dict = {}
    last_judge_score: float | None = None
    last_judge_criteria: list = []
    last_judge_feedback: str = ""

    for attempt in range(1, max_attempts + 1):
        constraints = cr_context
        if attempts_log:
            ctx = _build_consolidated_context(agent_name, attempts_log)
            formatted = _format_consolidated_context_for_prompt(ctx)
            if formatted:
                constraints += "\n\n" + formatted

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write(
                f"agent_name: {agent_slug}\n\n"
                f"# Agent: {agent_name}\n\n"
                f"{cdc_full}"
                f"{constraints}"
            )
            attempt_spec_path = _Path(f.name)

        try:
            forge = forge_cls(output_dir=base_output_dir, config={"llm": llm_cfg}, llm_fn=llm_fn)
            events = list(forge.run(spec_path=attempt_spec_path))
        finally:
            attempt_spec_path.unlink(missing_ok=True)

        summary = next((e for e in events if e.get("summary")), None)
        import logging as _log  # noqa: PLC0415

        _log.getLogger("atlas_dashboard").warning(
            "Forge events for %s: %s",
            agent_slug,
            [(e.get("level", "?"), e.get("msg", "")[:120]) for e in events],
        )
        if summary is None:
            errors = [e.get("msg", "") for e in events if e.get("level") == "error"]
            raise HTTPException(status_code=500, detail=f"Scaffolding failed: {errors}")

        _log.getLogger("atlas_dashboard").warning("Forge summary for %s: %s", agent_slug, summary)
        generated_path = _Path(summary["output_path"])
        target_path = base_output_dir / agent_slug
        if generated_path != target_path and generated_path.exists():
            import shutil as _shutil  # noqa: PLC0415

            if target_path.exists():
                _shutil.rmtree(target_path)
            generated_path.rename(target_path)

        _fix_entry_point(target_path, agent_name)
        _pip_install(target_path)

        judge_score, judge_criteria, judge_feedback = _judge_scaffold(
            target_path, agent_name, cdc_full, llm_fn, llm_cfg
        )
        attempts_log.append(
            {"attempt": attempt, "criteria_scores": judge_criteria, "feedback": judge_feedback}
        )
        last_output_path = str(target_path)
        last_spec = summary
        last_files = _read_generated_files(target_path)
        last_judge_score = judge_score
        last_judge_criteria = judge_criteria or []
        last_judge_feedback = judge_feedback or ""

        from atlas_dashboard.atlas_mlflow import log_scaffold_attempt  # noqa: PLC0415

        _judge_passed = judge_score is not None and judge_score >= quality_threshold
        log_scaffold_attempt(
            agent_name=agent_name,
            attempt=attempt,
            judge_score=judge_score,
            judge_passed=_judge_passed,
            criteria=judge_criteria or [],
            feedback=judge_feedback or "",
            model_id=llm_cfg.get("model_id", ""),
            patches_applied=body.selected_patches,
            user_answers=body.user_answers,
            run_id=run_id,
            atlas_run_id=run_id,
        )

        store.add_scaffold_attempt(
            run_id=run_id,
            agent_name=agent_name,
            attempt=attempt,
            judge_score=judge_score,
            judge_criteria=judge_criteria or [],
            judge_feedback=judge_feedback or "",
            model_id=llm_cfg.get("model_id", ""),
            patches_applied=body.selected_patches or [],
            user_answers=body.user_answers or [],
        )

        _scaffold_output = "\n".join(last_files.values()) if last_files else ""
        _index_judge_in_rag(
            agent_name=agent_name,
            output=_scaffold_output,
            judge_score=judge_score,
            judge_criteria=judge_criteria or [],
            judge_feedback=judge_feedback or "",
            judge_passed=_judge_passed,
            attempt=attempt,
            patches_applied=body.selected_patches or [],
        )
        if not _judge_passed:
            _add_failed_on_edges(agent_name, judge_criteria or [])

        if _judge_passed:
            store.set_scaffold_result(
                run_id,
                agent_name,
                last_output_path,
                last_spec,
                user_answers=body.user_answers,
                selected_patches=body.selected_patches,
                model_id=llm_cfg.get("model_id", ""),
                judge_score=judge_score,
                judge_criteria=judge_criteria,
                judge_feedback=judge_feedback,
                files=last_files,
            )
            return (
                True,
                {
                    "agent_name": agent_name,
                    "output_path": last_output_path,
                    "spec": last_spec,
                    "files": last_files,
                    "attempts": attempt,
                    "judge_score": judge_score,
                },
                attempts_log,
                last_output_path,
                last_spec,
                last_files,
                last_judge_score,
                last_judge_criteria,
                last_judge_feedback,
            )

    return (
        False,
        None,
        attempts_log,
        last_output_path,
        last_spec,
        last_files,
        last_judge_score,
        last_judge_criteria,
        last_judge_feedback,
    )


def _run_improvement_advisor(agent_name: str, store) -> list[dict]:
    """Call improvement-advisor after failed scaffold cycle. Returns suggested_actions or []."""
    import logging  # noqa: PLC0415

    try:
        from improvement_advisor.agent import ImprovementAdvisorAgent  # noqa: PLC0415
    except ImportError:
        return []

    try:
        llm_fn, llm_cfg = _llm_fn_for_dashboard()
        runs = sorted(store.list_runs(), key=lambda r: r.get("created_at", ""))
        trend_points = _build_trend_points(agent_name, runs)
        if not trend_points:
            trend_points = _build_trend_points_from_scaffold(agent_name, runs)
        if not trend_points:
            logging.getLogger("atlas_dashboard").info(
                "improvement-advisor: no trend data for '%s' — skipping", agent_name
            )
            return []

        generated_files = _get_latest_files_for_agent(agent_name, runs)
        advisor = ImprovementAdvisorAgent(config={"llm": llm_cfg}, llm_fn=llm_fn)
        events = list(
            advisor.run(
                agent_name=agent_name,
                trend_points=trend_points,
                generated_files=generated_files,
            )
        )
        summary = next((e for e in events if e.get("summary")), None)
        if not summary:
            return []

        actions = summary.get("suggested_actions", [])
        logging.getLogger("atlas_dashboard").info(
            "improvement-advisor: %d actions for '%s' — injecting into cycle 2",
            len(actions),
            agent_name,
        )
        return actions
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "improvement-advisor call failed (non-blocking): %s", exc
        )
        return []


def _do_scaffold(run_id: str, agent_name: str, body: ScaffoldRequest) -> None:
    """Background worker — runs scaffold cycle and updates run store when done."""
    import logging as _log  # noqa: PLC0415

    job_key = f"{run_id}:{agent_name}"
    store = get_store()
    _log.getLogger("atlas_dashboard").warning("_do_scaffold START %s", agent_name)
    _bg_jobs[job_key] = {"status": "running"}
    try:
        store.set_scaffold_status(run_id, agent_name, "running")
        result = _scaffold_sync(run_id, agent_name, body)
        store.set_scaffold_status(run_id, agent_name, "done")
        _bg_jobs[job_key] = {"status": "done", "result": result}
        _log.getLogger("atlas_dashboard").warning(
            "_do_scaffold DONE %s result=%s", agent_name, str(result)[:200]
        )
    except Exception as exc:  # noqa: BLE001
        store.set_scaffold_status(run_id, agent_name, "error")
        _bg_jobs[job_key] = {"status": "error", "error": str(exc)}
        _log.getLogger("atlas_dashboard").warning("_do_scaffold ERROR %s: %s", agent_name, exc)


@app.post("/api/runs/{run_id}/scaffold/{agent_name}")
def api_scaffold(
    run_id: str,
    agent_name: str,
    body: ScaffoldRequest | None = None,
    background_tasks: BackgroundTasks = BackgroundTasks(),  # noqa: B008
):
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
        import importlib.util as _ilu  # noqa: PLC0415

        if _ilu.find_spec("agent_forge") is None:
            raise ImportError
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="agent-forge not installed") from exc

    llm_fn, llm_cfg = _llm_fn_for_dashboard()
    _probe = llm_fn("Reply OK", llm_cfg, 8)
    if not _probe:
        raise HTTPException(
            status_code=500,
            detail="LLM probe failed — check atlas.toml [llm] config and credentials",
        )

    job_key = f"{run_id}:{agent_name}"
    if _bg_jobs.get(job_key, {}).get("status") == "running":
        return {"status": "scaffolding", "run_id": run_id, "agent_name": agent_name}

    background_tasks.add_task(_do_scaffold, run_id, agent_name, body or ScaffoldRequest())
    return {"status": "scaffolding", "run_id": run_id, "agent_name": agent_name}


@app.post("/api/runs/{run_id}/scaffold/{agent_name}/ingest")
async def api_scaffold_ingest(
    run_id: str,
    agent_name: str,
    files: list[UploadFile],
):
    """Upload documents and extract their text via AtlasIngestAgent.

    Returns extracted text to be passed as additional_context in ScaffoldRequest.
    """
    from atlas_ingest.agent import AtlasIngestAgent  # noqa: PLC0415

    store = get_store()
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")

    tmp_paths: list[_Path] = []
    try:
        for upload in files:
            suffix = _Path(upload.filename or "file").suffix or ".bin"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(await upload.read())
            tmp_paths.append(_Path(tmp.name))

        agent = AtlasIngestAgent()
        events = list(agent.run(files=tmp_paths))
        summary_event = next((e for e in events if e.get("summary")), None)
        extracted = summary_event["output"] if summary_event else ""
    finally:
        for p in tmp_paths:
            p.unlink(missing_ok=True)

    return {"extracted_text": extracted, "file_count": len(files)}


def _scaffold_sync(run_id: str, agent_name: str, body: ScaffoldRequest) -> dict:
    """Synchronous scaffold logic — runs in a background thread."""
    from agent_forge.agent import AgentForgeAgent  # noqa: PLC0415

    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"Run {run_id} not found")

    cdc_full = run.get("metadata", {}).get(
        "cdc_full", run.get("metadata", {}).get("cdc_preview", "")
    )
    llm_fn, llm_cfg = _llm_fn_for_dashboard()

    agent_slug = _agent_slug(agent_name)

    meta = run.get("metadata", {})
    project_dir = meta.get("project_dir", "")
    project_owner = meta.get("project_owner", "client")
    if project_owner == "capgemini" or not project_dir:
        base_output_dir = _Path.home() / ".atlas" / "scaffolded"
    else:
        base_output_dir = _Path(project_dir) / "agents"
    base_output_dir.mkdir(parents=True, exist_ok=True)

    cr_context = ""
    if body.additional_context:
        cr_context += "\n\n# Context from attached documents:\n" + body.additional_context
    if body.user_answers:
        cr_context += "\n\n# User answers to diagnostic questions:\n" + "\n".join(
            f"- {a}" for a in body.user_answers if a.strip()
        )
    if body.selected_patches:
        cr_context += "\n\n# Patches to apply:\n" + "\n".join(
            f"- {p}" for p in body.selected_patches if p.strip()
        )
    if body.judge_feedback_context:
        cr_context += (
            "\n\n# Judge feedback from previous attempt (address these issues):\n"
            + body.judge_feedback_context
        )

    # Cycle 1
    (
        success,
        result,
        attempts_log,
        last_output_path,
        last_spec,
        last_files,
        last_js,
        last_jc,
        last_jf,
    ) = _run_scaffold_cycle(
        cr_context=cr_context,
        cdc_full=cdc_full,
        agent_slug=agent_slug,
        agent_name=agent_name,
        run_id=run_id,
        store=store,
        base_output_dir=base_output_dir,
        llm_fn=llm_fn,
        llm_cfg=llm_cfg,
        body=body,
        forge_cls=AgentForgeAgent,
    )
    if success:
        return result  # type: ignore[return-value]

    # Cycle 1 exhausted — check cause distribution
    ctx1 = _build_consolidated_context(agent_name, attempts_log)
    missing_issues = [i for i in ctx1["open_issues"] if i.get("cause") == "missing_spec"]

    if missing_issues:
        questions = _generate_questions(missing_issues, agent_name, cdc_full, llm_fn, llm_cfg)
        if questions:
            store.set_pending_questions(run_id, agent_name, questions)
            store.save_agent_state(
                run_id,
                agent_name,
                {
                    "cr_context": cr_context,
                    "attempts_log": attempts_log,
                    "user_answers": body.user_answers,
                    "selected_patches": body.selected_patches,
                },
            )
            return {
                "status": "waiting_for_client",
                "agent_name": agent_name,
                "pending_questions": questions,
            }

    # Only model_error/code_error — call improvement-advisor before giving up
    advisor_actions = _run_improvement_advisor(agent_name, store)
    if advisor_actions:
        advisor_context = (
            cr_context
            + _build_prev_files_section(agent_name, store)
            + "\n\n# Corrections requises (improvement-advisor) :\n"
            + "\n".join(f"- {a['action']}" for a in advisor_actions)
        )
        (
            success2,
            result2,
            attempts_log2,
            last_output_path,
            last_spec,
            last_files,
            last_js,
            last_jc,
            last_jf,
        ) = _run_scaffold_cycle(
            cr_context=advisor_context,
            cdc_full=cdc_full,
            agent_slug=agent_slug,
            agent_name=agent_name,
            run_id=run_id,
            store=store,
            base_output_dir=base_output_dir,
            llm_fn=llm_fn,
            llm_cfg=llm_cfg,
            body=body,
            forge_cls=AgentForgeAgent,
        )
        attempts_log.extend(attempts_log2)
        if success2:
            result2["advisor_used"] = True  # type: ignore[index]
            _index_scaffold_resolutions(agent_name, attempts_log, attempts_log2)
            return result2  # type: ignore[return-value]

    # All cycles failed — diagnose
    diagnosis = _run_diagnose(attempts_log, agent_name, cdc_full, llm_fn, llm_cfg)
    store.set_scaffold_result(
        run_id,
        agent_name,
        last_output_path,
        last_spec,
        user_answers=body.user_answers,
        selected_patches=body.selected_patches,
        model_id=llm_cfg.get("model_id", ""),
        judge_score=last_js,
        judge_criteria=last_jc,
        judge_feedback=last_jf,
        files=last_files,
    )
    store.set_scaffold_diagnosis(run_id, agent_name, diagnosis)
    return {
        "agent_name": agent_name,
        "output_path": last_output_path,
        "spec": last_spec,
        "files": last_files,
        "attempts": len(attempts_log),
        "diagnosis": diagnosis,
    }


def _do_rejudge(run_id: str, agent_name: str) -> None:
    """Background worker — re-runs judge on an already-scaffolded agent."""
    job_key = f"{run_id}:rejudge:{agent_name}"
    _bg_jobs[job_key] = {"status": "running"}
    try:
        store = get_store()
        run = store.get_run(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found")
        sc = run.get("scaffolded", {}).get(agent_name)
        if sc is None:
            raise ValueError(f"Agent {agent_name} not scaffolded in run {run_id}")
        target_path = _Path(sc["output_path"])
        cdc_full = run.get("metadata", {}).get(
            "cdc_full", run.get("metadata", {}).get("cdc_preview", "")
        )
        llm_fn, llm_cfg = _llm_fn_for_dashboard()
        judge_score, judge_criteria, judge_feedback = _judge_scaffold(
            target_path, agent_name, cdc_full, llm_fn, llm_cfg
        )
        store.set_scaffold_judge(run_id, agent_name, judge_score, judge_criteria, judge_feedback)
        _bg_jobs[job_key] = {"status": "done", "judge_score": judge_score}
    except Exception as exc:  # noqa: BLE001
        _bg_jobs[job_key] = {"status": "error", "error": str(exc)}


@app.post("/api/runs/{run_id}/rejudge/{agent_name}")
def api_rejudge(
    run_id: str,
    agent_name: str,
    background_tasks: BackgroundTasks = BackgroundTasks(),  # noqa: B008
):
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if agent_name not in run.get("scaffolded", {}):
        raise HTTPException(status_code=400, detail=f"{agent_name} not scaffolded")

    job_key = f"{run_id}:rejudge:{agent_name}"
    if _bg_jobs.get(job_key, {}).get("status") == "running":
        return {"status": "judging", "run_id": run_id, "agent_name": agent_name}

    background_tasks.add_task(_do_rejudge, run_id, agent_name)
    return {"status": "judging", "run_id": run_id, "agent_name": agent_name}


def _do_build_tool(run_id: str, tool_name: str) -> None:
    """Background worker for tool building."""
    job_key = f"{run_id}:tool:{tool_name}"
    _bg_jobs[job_key] = {"status": "running"}
    try:
        result = _build_tool_sync(run_id, tool_name)
        _bg_jobs[job_key] = {"status": "done", "result": result}
    except Exception as exc:  # noqa: BLE001
        _bg_jobs[job_key] = {"status": "error", "error": str(exc)}


@app.post("/api/runs/{run_id}/build-tool/{tool_name}")
def api_build_tool(
    run_id: str,
    tool_name: str,
    background_tasks: BackgroundTasks = BackgroundTasks(),  # noqa: B008
):
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if tool_name not in (run.get("requires_tools") or []):
        raise HTTPException(
            status_code=400, detail=f"{tool_name} is not in requires_tools for this run"
        )
    try:
        import importlib.util as _ilu2  # noqa: PLC0415

        if _ilu2.find_spec("tool_forge") is None:
            raise ImportError
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="tool-forge not installed") from exc

    job_key = f"{run_id}:tool:{tool_name}"
    if _bg_jobs.get(job_key, {}).get("status") == "running":
        return {"status": "building", "run_id": run_id, "tool_name": tool_name}

    background_tasks.add_task(_do_build_tool, run_id, tool_name)
    return {"status": "building", "run_id": run_id, "tool_name": tool_name}


def _build_tool_sync(run_id: str, tool_name: str) -> dict:
    """Synchronous tool build logic — runs in a background thread."""
    from tool_forge.agent import ToolForgeAgent  # noqa: PLC0415

    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"Run {run_id} not found")

    llm_fn, llm_cfg = _llm_fn_for_dashboard()
    cdc_full = run.get("metadata", {}).get("cdc_full", "")
    tool_bloc = next(
        (
            b
            for b in run.get("blocs", [])
            if b.get("agent_suggested") == tool_name and b.get("type") == "tool"
        ),
        None,
    )
    spec_lines = [f"# {tool_name}", ""]
    if tool_bloc:
        spec_lines.append(tool_bloc.get("bloc", ""))
        criteria = tool_bloc.get("criteria", [])
        if criteria:
            spec_lines += ["", "## Requirements"] + [f"- {c}" for c in criteria]
    if cdc_full:
        spec_lines += ["", "## CDC context (for reference)", cdc_full[:600]]
    spec_text = "\n".join(spec_lines)

    meta = run.get("metadata", {})
    project_dir = meta.get("project_dir", "")
    project_owner = meta.get("project_owner", "client")
    if project_owner == "capgemini" or not project_dir:
        libs_dir = _Path.home() / ".atlas" / "libs"
    else:
        libs_dir = _Path(project_dir) / "libs"
    libs_dir.mkdir(parents=True, exist_ok=True)

    agent = ToolForgeAgent(output_dir=libs_dir, config={"llm": llm_cfg}, llm_fn=llm_fn)
    events = list(agent.run(spec_text=spec_text))
    summary = next((e for e in events if e.get("summary")), None)
    if summary is None:
        errors = [e.get("msg", "") for e in events if e.get("level") == "error"]
        raise ValueError(f"Tool generation failed: {errors}")

    files = _read_generated_files(_Path(summary["output_path"]))
    store.set_tool_result(run_id, tool_name, summary["output_path"], summary, files=files)
    _index_tool_in_rag(tool_name, spec_text, summary)

    from atlas_dashboard.atlas_mlflow import log_tool_build  # noqa: PLC0415

    log_tool_build(
        tool_name=tool_name,
        contract_passed=summary.get("contract_passed", False),
        module_name=summary.get("module_name", ""),
        functions=summary.get("functions", []),
        atlas_run_id=run_id,
    )

    return {
        "tool_name": tool_name,
        "output_path": summary["output_path"],
        "module_name": summary.get("module_name", ""),
        "functions": summary.get("functions", []),
        "files": files,
    }


@app.post("/api/runs/{run_id}/install-tool/{tool_name}")
def api_install_tool(run_id: str, tool_name: str):
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    built = run.get("tools_built", {}).get(tool_name)
    if not built:
        raise HTTPException(status_code=400, detail="Tool not built yet")
    output_path = built["output_path"]
    if not _Path(output_path).exists():
        raise HTTPException(status_code=400, detail=f"Tool path not found: {output_path}")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", output_path, "-q"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"pip install failed: {result.stderr[:500]}")
    project, cdc_summary, context_tags = _extract_run_context(run)
    kg_ok, kg_err = _register_tool_in_kg(
        tool_name, output_path, project=project, cdc_summary=cdc_summary, context_tags=context_tags
    )
    # create KG edge caller_agent --USES_TOOL--> tool if called_by is set on the bloc
    blocs = run.get("blocs", [])
    called_by = next(
        (
            b.get("called_by")
            for b in blocs
            if (b.get("agent_suggested") == tool_name or b.get("agent") == tool_name)
            and b.get("type") == "tool"
            and b.get("called_by")
        ),
        None,
    )
    # called_by may be a CSV of multiple agents — create one edge per agent
    if called_by:
        for caller in [c.strip() for c in called_by.split(",") if c.strip()]:
            _add_kg_edge(caller, tool_name, "USES_TOOL")
    store.set_tool_installed(run_id, tool_name)
    return {
        "status": "installed",
        "tool_name": tool_name,
        "called_by": called_by,
        "kg_registered": kg_ok,
        "kg_error": kg_err or None,
    }


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

    # Register the new agent in the KG with judge score + confidence + context
    sc_entry = (store.get_run(run_id) or {}).get("scaffolded", {}).get(agent_name, {})
    judge_score = sc_entry.get("judge_score")
    project, cdc_summary, context_tags = _extract_run_context(run)
    # Build meaningful capabilities from bloc justification + criteria
    agent_bloc = next((b for b in run.get("blocs", []) if b.get("agent") == agent_name), None)
    kg_capabilities: list[str] = []
    if agent_bloc:
        if agent_bloc.get("justification"):
            kg_capabilities.append(agent_bloc["justification"])
        kg_capabilities.extend(agent_bloc.get("criteria") or [])
    _register_agent_in_kg(
        agent_name,
        output_path,
        judge_score=judge_score,
        run_id=run_id,
        project=project,
        cdc_summary=cdc_summary,
        context_tags=context_tags,
        capabilities=kg_capabilities or None,
    )

    # Create PIPELINE_MEMBER edges between all installed agents in this run
    installed_agents = [
        name
        for name, sc in run.get("scaffolded", {}).items()
        if sc.get("installed") or name == agent_name
    ]
    for peer in installed_agents:
        if peer != agent_name:
            _add_kg_edge(agent_name, peer, "PIPELINE_MEMBER")
            _add_kg_edge(peer, agent_name, "PIPELINE_MEMBER")

    store.set_agent_installed(run_id, agent_name)
    return {"status": "installed", "agent_name": agent_name}


def _read_generated_files(target_path: _Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for p in target_path.rglob("*"):
        if p.is_file() and p.suffix in (".py", ".toml", ".md", ".json", ".txt"):
            with contextlib.suppress(Exception):
                files[str(p.relative_to(target_path))] = p.read_text(encoding="utf-8")
    return files


def _pip_install(target_path: _Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", str(target_path), "-q"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"pip install failed: {result.stderr[:500]}")


def _judge_scaffold(
    target_path: _Path,
    agent_name: str,
    cdc_full: str,
    llm_fn,
    llm_cfg: dict,
) -> tuple[float | None, list[dict], str]:
    """Run atlas-judge pointwise on the scaffolded agent.

    Returns (score, criteria_scores, feedback).
    score is None if judge could not run (non-blocking).
    """
    import logging  # noqa: PLC0415

    try:
        agent_slug = target_path.name
        module_slug = agent_slug.replace("-", "_")

        # Generate a synthetic test input so the agent receives a realistic payload
        # (not the CDC spec itself, which is the requirements doc, not an agent input)
        _test_input_prompt = (
            f"Based on this agent specification:\n{cdc_full[:800]}\n\n"
            "Generate ONE realistic example INPUT that this agent should process "
            "(e.g. a support ticket, an email, a text snippet — whatever the agent expects). "
            "Return only the example input text, nothing else."
        )
        test_input = llm_fn(_test_input_prompt, llm_cfg, 200) or "Sample input for evaluation."

        # Try to load the agent and run it on the synthetic test input
        try:
            import importlib  # noqa: PLC0415
            import sys as _sys  # noqa: PLC0415

            # Editable installs done at runtime don't add .pth paths until next startup —
            # ensure the agent source directory is importable right now.
            _agent_src = str(target_path)
            if _agent_src not in _sys.path:
                _sys.path.insert(0, _agent_src)

            # Evict stale cached modules so each attempt loads fresh code from disk
            stale = [k for k in _sys.modules if k == module_slug or k.startswith(f"{module_slug}.")]
            for k in stale:
                del _sys.modules[k]

            # Try {module}.agent first (agent-forge places class in agent.py, not __init__)
            mod = None
            for candidate in (f"{module_slug}.agent", module_slug):
                try:
                    mod = importlib.import_module(candidate)
                    break
                except ImportError:
                    continue

            class_name = (
                next(
                    (k for k in dir(mod) if k.endswith("Agent") and not k.startswith("_")),
                    None,
                )
                if mod
                else None
            )

            if class_name and mod:
                agent_instance = getattr(mod, class_name)(
                    llm_fn=llm_fn, config={"llm": llm_cfg}, llm_cfg=llm_cfg
                )
                output_events = list(agent_instance.run(cdc_bloc=test_input, context=cdc_full))
                # Extract meaningful output: prefer "output" key, skip info/warn messages
                parts = []
                for e in output_events:
                    if not isinstance(e, dict):
                        continue
                    if "output" in e and e["output"] is not None:
                        parts.append(str(e["output"]))
                    elif "summary" in e and e["summary"] is not True:
                        parts.append(str(e["summary"]))
                    # intentionally skip "msg" events (info/warn) — not agent output
                agent_output = " ".join(parts)
                # Strip markdown code fences so the judge sees raw JSON
                import re as _re  # noqa: PLC0415

                _fence = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", agent_output, _re.DOTALL)
                if _fence:
                    agent_output = _fence.group(1)
                logging.getLogger("atlas_dashboard").warning(
                    "Judge scaffold agent_output for %s: %r", agent_name, agent_output[:500]
                )
            else:
                agent_output = "(agent loaded but no Agent class found)"
        except Exception as exc:  # noqa: BLE001
            agent_output = f"(agent execution failed: {exc})"
            logging.getLogger("atlas_dashboard").warning(
                "Judge scaffold: agent execution failed for %s: %s", agent_name, exc
            )

        # Derive evaluation criteria from CDC when no criteria.py exists for this agent
        module_slug = target_path.name.replace("-", "_")
        derived_criteria: list[str] = []
        try:
            import importlib as _il  # noqa: PLC0415

            mod_c = _il.import_module(f"{module_slug}.criteria")
            derived_criteria = list(getattr(mod_c, "CRITERIA", {}).values())
        except ImportError:
            pass

        if not derived_criteria:
            _criteria_prompt = (
                f"Based on this agent specification:\n{cdc_full[:1200]}\n\n"
                "List 4 to 6 evaluation criteria for this agent's output. "
                "Each criterion must be a single sentence starting with a verb "
                "(e.g. 'Output must contain...', 'Response must be...', 'Agent must...'). "
                "Return a JSON array of strings only, no explanation."
            )
            _raw_criteria = llm_fn(_criteria_prompt, llm_cfg, 300) or "[]"
            try:
                import json as _json  # noqa: PLC0415
                import re as _re  # noqa: PLC0415

                _m = _re.search(r"\[.*\]", _raw_criteria, _re.DOTALL)
                derived_criteria = _json.loads(_m.group()) if _m else []
            except Exception:  # noqa: BLE001
                derived_criteria = []

        from atlas_judge.agent import AtlasJudgeAgent  # noqa: PLC0415

        judge = AtlasJudgeAgent(config={"llm": llm_cfg}, llm_fn=llm_fn)
        events = list(
            judge.run(
                operation="pointwise",
                output=agent_output,
                agent_name=agent_name,
                context=cdc_full,
                criteria=derived_criteria or None,
            )
        )
        summary = next((e for e in events if e.get("summary")), None)
        if summary is None:
            return None, [], ""

        criteria_scores = summary.get("criteria_scores", [])
        feedback = summary.get("feedback", "")
        score_val = summary.get("score")
        if score_val is None and criteria_scores:
            scores = [
                c.get("score", 0)
                for c in criteria_scores
                if isinstance(c.get("score"), int | float)
            ]
            score_val = sum(scores) / len(scores) if scores else None

        # Enrich feedback with explicit patch targets for low-score criteria
        patch_hints: list[str] = []
        for c in criteria_scores:
            if isinstance(c.get("score"), int | float) and c["score"] < 3:
                cr_text = c.get("criterion", "")
                fb_text = c.get("feedback", "")
                if cr_text:
                    hint = f"PATCH TARGET — criterion '{cr_text}': {fb_text}"
                    if "prompts.py" not in hint and any(
                        kw in (cr_text + fb_text).lower()
                        for kw in (
                            "label",
                            "level",
                            "priority",
                            "langue",
                            "language",
                            "format",
                            "output",
                        )
                    ):
                        hint += " — Fix in: prompts.py (system prompt / output format)"
                    patch_hints.append(hint)
        if patch_hints:
            feedback = feedback + "\n\n" + "\n".join(patch_hints)

        return score_val, criteria_scores, feedback

    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "Judge evaluation failed for %s (non-blocking): %s", agent_name, exc
        )
        return None, [], ""


def _run_diagnose(
    attempts_log: list[dict],
    agent_name: str,
    cdc_full: str,
    llm_fn,
    llm_cfg: dict,
) -> dict:
    """Call atlas-judge diagnose after all retry attempts exhausted."""
    import logging  # noqa: PLC0415

    try:
        from atlas_judge.agent import AtlasJudgeAgent  # noqa: PLC0415

        judge = AtlasJudgeAgent(config={"llm": llm_cfg}, llm_fn=llm_fn)
        events = list(
            judge.run(
                operation="diagnose",
                attempts=attempts_log,
                agent_name=agent_name,
                context=cdc_full[:800],
            )
        )
        summary = next((e for e in events if e.get("summary")), None)
        if summary:
            return {k: v for k, v in summary.items() if k != "summary"}
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").error("Diagnose failed for %s: %s", agent_name, exc)

    return {
        "mode": "diagnose",
        "problems": ["Diagnosis unavailable — LLM call failed"],
        "questions": [],
        "proposed_patches": [],
    }


def _extract_run_context(run: dict) -> tuple[str, str, list[str]]:
    """Extract project name, cdc_summary, and context_tags from a run dict."""
    meta = run.get("metadata", {})
    project_dir = meta.get("project_dir", "")
    project = _Path(project_dir).name if project_dir else ""

    cdc_full = meta.get("cdc_full", meta.get("cdc_preview", ""))
    # First non-empty line starting with # is the CDC title → use as summary
    cdc_summary = ""
    for line in cdc_full.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            cdc_summary = line[:120]
            break

    # Extract context_tags from CDC keywords (basic heuristic)
    context_tags: list[str] = []
    cdc_lower = cdc_full.lower()
    keyword_map = {
        "ticket": "tickets",
        "support": "support",
        "classif": "classification",
        "priorit": "priority",
        "catégor": "category",
        "français": "fr",
        "french": "fr",
        "english": "en",
        "saas": "saas",
        "it ": "it",
        "factur": "billing",
        "onboarding": "onboarding",
    }
    for kw, tag in keyword_map.items():
        if kw in cdc_lower and tag not in context_tags:
            context_tags.append(tag)

    return project, cdc_summary, context_tags


def _register_tool_in_kg(
    tool_name: str,
    output_path: str,
    project: str = "",
    cdc_summary: str = "",
    context_tags: list[str] | None = None,
) -> tuple[bool, str]:
    import logging  # noqa: PLC0415

    try:
        from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

        mem = AtlasMemoryAgent()
        events = list(
            mem.run(
                operation="register_tool",
                name=tool_name,
                capabilities=[f"Reusable Python tool: {tool_name}"],
                tags=["tool", "generated"],
                project=project,
                cdc_summary=cdc_summary,
                context_tags=context_tags or [],
            )
        )
        err = next((e for e in events if e.get("level") == "error"), None)
        if err:
            msg = err.get("msg", "unknown error")
            logging.getLogger("atlas_dashboard").warning(
                "KG tool registration warning for %s: %s", tool_name, msg
            )
            return False, msg
        return True, ""
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        logging.getLogger("atlas_dashboard").warning(
            "KG tool registration failed for %s (non-blocking): %s", tool_name, msg
        )
        return False, msg


def _add_kg_edge(from_node: str, to_node: str, relation: str) -> None:
    import logging  # noqa: PLC0415

    try:
        from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

        mem = AtlasMemoryAgent()
        list(
            mem.run(operation="add_edge", from_agent=from_node, to_agent=to_node, relation=relation)
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "KG edge %s->%s failed (non-blocking): %s", from_node, to_node, exc
        )


def _index_tool_in_rag(tool_name: str, spec_text: str, summary: dict) -> None:
    import logging  # noqa: PLC0415

    try:
        from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

        mem = AtlasMemoryAgent()
        list(
            mem.run(
                operation="index",
                entry_type="tool_spec",
                content=spec_text[:500],
                payload={
                    "tool_name": tool_name,
                    "module_name": summary.get("module_name", ""),
                    "functions": summary.get("functions", []),
                    "description": summary.get("description", ""),
                },
                metadata={"tool": tool_name},
            )
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "RAG indexing failed for tool %s (non-blocking): %s", tool_name, exc
        )


def _index_judge_in_rag(
    agent_name: str,
    output: str,
    judge_score: float | None,
    judge_criteria: list,
    judge_feedback: str,
    judge_passed: bool,
    attempt: int,
    patches_applied: list,
) -> None:
    import logging  # noqa: PLC0415

    try:
        from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

        mem = AtlasMemoryAgent()
        outcome = "success" if judge_passed else "failure"
        list(
            mem.run(
                operation="index",
                entry_type="judge_eval",
                content=output[:500],
                payload={
                    "agent_name": agent_name,
                    "outcome": outcome,
                    "score": judge_score,
                    "passed": judge_passed,
                    "criteria_scores": judge_criteria,
                    "feedback": judge_feedback,
                    "attempt": attempt,
                    "patches": patches_applied,
                },
                metadata={"agent": agent_name, "outcome": outcome},
            )
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "RAG judge indexing failed for %s attempt %d (non-blocking): %s",
            agent_name,
            attempt,
            exc,
        )


def _add_failed_on_edges(agent_name: str, criteria_scores: list) -> None:
    import logging  # noqa: PLC0415

    try:
        from atlas_memory.agent import _is_shareable  # noqa: PLC0415

        if not _is_shareable():
            return
        from atlas_memory.kg import KGStore  # noqa: PLC0415

        _kg_path = _Path.home() / ".atlas" / "memory" / "graph.json"
        kg = KGStore(_kg_path)
        for c in criteria_scores:
            crit_name = c.get("criterion", "")
            score = c.get("score")
            if not crit_name or score is None or score >= 3:
                continue
            if crit_name not in kg._g.nodes:
                kg.register_agent(
                    name=crit_name,
                    capabilities=[f"evaluation criterion: {crit_name}"],
                    inputs=[],
                    outputs=[],
                    tags=["criterion"],
                    node_type="criterion",
                    shareable=True,
                )
            kg.add_edge(agent_name, crit_name, "FAILED_ON")
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "KG FAILED_ON edges for %s (non-blocking): %s", agent_name, exc
        )


def _index_resolution_in_rag(
    agent_name: str,
    criterion: str,
    cause: str,
    resolution: str,
    entry_type: str,
) -> None:
    """Index a resolved failure in RAG so same blockers recur less often.

    entry_type: 'client_decision' | 'scaffold_fix' | 'deterministic_fix'
    """
    import logging  # noqa: PLC0415

    try:
        from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

        mem = AtlasMemoryAgent()
        content = f"{agent_name}: {criterion} ({cause}) → {resolution}"
        list(
            mem.run(
                operation="index",
                entry_type=entry_type,
                content=content[:500],
                payload={
                    "agent_name": agent_name,
                    "criterion": criterion,
                    "cause": cause,
                    "resolution": resolution,
                },
                metadata={"agent": agent_name, "criterion": criterion, "cause": cause},
            )
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "RAG resolution indexing failed for %s/%s (non-blocking): %s",
            agent_name,
            criterion,
            exc,
        )


def _add_resolved_by_edges(agent_name: str, criterion: str, resolution_key: str) -> None:
    """Record scaffold resolution in coaching_history of the agent node (no KG sub-nodes).

    Criterion and resolution data belong in the agent payload, not as separate graph nodes.
    RAG indexing (Qdrant) is handled separately by _index_resolution_in_rag.
    """
    import logging  # noqa: PLC0415

    try:
        from atlas_memory.agent import _is_shareable  # noqa: PLC0415

        if not _is_shareable():
            return
        from atlas_memory.kg import KGStore  # noqa: PLC0415

        _kg_path = _Path.home() / ".atlas" / "memory" / "graph.json"
        kg = KGStore(_kg_path)
        if agent_name in kg._g.nodes:
            kg.update_node_coaching(
                name=agent_name,
                patch_description=resolution_key,
                criterion=criterion,
                delta=0.0,
            )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "KG coaching update for %s/%s (non-blocking): %s", agent_name, criterion, exc
        )


def _index_scaffold_resolutions(
    agent_name: str,
    attempts_log_before: list[dict],
    attempts_log_after: list[dict],
) -> None:
    """After a successful scaffold cycle, index what was fixed vs what failed before.

    Compares failing criteria from cycle N-1 against the now-passing cycle N.
    """
    failed_before = {
        cs["criterion"]: cs
        for attempt in attempts_log_before
        for cs in attempt.get("criteria_scores", [])
        if cs.get("score", 5) < 3 and cs.get("criterion")
    }
    if not failed_before:
        return

    for criterion, cs in failed_before.items():
        cause = cs.get("cause") or "model_error"
        if cause == "missing_spec":
            entry_type = "client_decision"
            resolution = "client_answer_provided"
        elif cause == "code_error":
            entry_type = "deterministic_fix"
            resolution = f"code_fix:{criterion}"
        else:
            entry_type = "scaffold_fix"
            resolution = f"scaffold_fix:{criterion}"
        _index_resolution_in_rag(agent_name, criterion, cause, resolution, entry_type)
        _add_resolved_by_edges(agent_name, criterion, resolution)


def _fix_entry_point(output_path: _Path, agent_name: str) -> None:
    """Patch pyproject.toml so the atlas.agents entry-point key matches agent_name."""
    import re  # noqa: PLC0415

    pyproject = output_path / "pyproject.toml"
    if not pyproject.exists():
        return
    text = pyproject.read_text(encoding="utf-8")
    # Replace any existing entry-point key under [atlas.agents] with agent_name
    text = re.sub(
        r'(\[project\.entry-points\."atlas\.agents"\]\s*\n)[^\[]+',
        lambda m: m.group(1)
        + next((f'{agent_name} = "{v}"\n' for v in re.findall(r'= "([^"]+)"', m.group(0))), ""),
        text,
    )
    pyproject.write_text(text, encoding="utf-8")


def _register_agent_in_kg(
    agent_name: str,
    output_path: str,
    judge_score: float | None = None,
    run_id: str | None = None,
    project: str = "",
    cdc_summary: str = "",
    context_tags: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> str:
    """Add the freshly installed agent to atlas-memory KG. Returns the effective KG name.

    If an agent with the same base name already exists for a different context,
    a fork is created automatically: {agent_name}#{project}.
    """
    import logging  # noqa: PLC0415

    effective_name = agent_name
    try:
        from atlas_memory.kg import KGStore  # noqa: PLC0415

        kg = KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")
        if kg.needs_fork(agent_name, context_tags or []):
            context_id = (project or "unknown").replace(" ", "_").lower()
            effective_name = kg.fork_agent(agent_name, context_id)
            logging.getLogger("atlas_dashboard").info(
                "Auto-forked %s → %s (context mismatch)", agent_name, effective_name
            )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "Fork check failed for %s (non-blocking): %s", agent_name, exc
        )

    try:
        from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

        confidence = (judge_score / 5.0) if judge_score is not None else 1.0
        mem = AtlasMemoryAgent()
        events = list(
            mem.run(
                operation="register_agent",
                name=effective_name,
                capabilities=capabilities or [f"Handles {agent_name} tasks as defined in CDC"],
                tags=["scaffolded"],
                judge_score=judge_score,
                confidence=confidence,
                project=project,
                cdc_summary=cdc_summary,
                context_tags=context_tags or [],
                output_type="json",
                parent=agent_name if effective_name != agent_name else None,
            )
        )
        err = next((e for e in events if e.get("level") == "error"), None)
        if err:
            logging.getLogger("atlas_dashboard").warning(
                "KG registration warning for %s: %s", effective_name, err.get("msg")
            )
        if judge_score is not None and run_id:
            list(
                mem.run(
                    operation="update_agent_score",
                    name=effective_name,
                    judge_score=judge_score,
                    run_id=run_id,
                    source="scaffold",
                )
            )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "KG registration failed for %s (non-blocking): %s", effective_name, exc
        )
    return effective_name


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


@app.get("/api/runs/{run_id}/tools/{tool_name}/download")
def api_download_tool(run_id: str, tool_name: str):
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    built = run.get("tools_built", {}).get(tool_name)
    if not built:
        raise HTTPException(status_code=400, detail="Tool not built yet")
    output_path = _Path(built["output_path"])
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Tool directory not found")
    _SKIP = {"__pycache__", ".git", ".egg-info"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in output_path.rglob("*"):
            if p.is_file() and not any(
                part.endswith(".egg-info") or part in _SKIP for part in p.parts
            ):
                zf.write(p, p.relative_to(output_path.parent))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{tool_name}.zip"'},
    )


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

    _SKIP = {"__pycache__", ".git", ".egg-info"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in output_path.rglob("*"):
            if p.is_file() and not any(
                part.endswith(".egg-info") or part in _SKIP for part in p.parts
            ):
                zf.write(p, p.relative_to(output_path.parent))
    buf.seek(0)

    filename = f"{agent_name}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Test in-dashboard ──────────────────────────────────────────────────────────


_TEST_INPUTS_PROMPT = """\
You are helping test an AI agent. Given a CDC (cahier des charges) excerpt that describes \
what this agent does, generate {n} realistic test input examples.

Agent: {agent_name}
CDC excerpt:
{cdc_excerpt}

All ATLAS agents accept exactly two inputs:
- "cdc_bloc": a string containing the realistic input data for this agent (e.g. a ticket, \
a document excerpt, a structured text). This is the main payload.
- "context": an optional string with additional context (can be empty string).

Return ONLY a JSON array of {n} objects, each with keys "cdc_bloc" and "context". \
The "cdc_bloc" value must be a realistic, domain-specific input string based on the CDC. \
No explanation, no markdown, just the JSON array.
"""


@app.get("/api/runs/{run_id}/agents/{agent_name}/test-inputs")
def api_generate_test_inputs(run_id: str, agent_name: str, n: int = 2):
    """Generate n realistic test input examples from the CDC for this agent."""
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    cdc_full = run.get("metadata", {}).get(
        "cdc_full", run.get("metadata", {}).get("cdc_preview", "")
    )
    if not cdc_full:
        raise HTTPException(status_code=400, detail="No CDC found for this run")

    # Extract the bloc relevant to this agent if possible
    blocs = run.get("blocs", [])
    agent_bloc = next(
        (b for b in blocs if b.get("name") == agent_name or b.get("agent") == agent_name),
        None,
    )
    cdc_excerpt = (
        agent_bloc.get("description", "") or agent_bloc.get("cdc_summary", "") or cdc_full[:2000]
        if agent_bloc
        else cdc_full[:2000]
    )

    llm_fn, llm_cfg = _llm_fn_for_dashboard()
    prompt = _TEST_INPUTS_PROMPT.format(
        agent_name=agent_name,
        cdc_excerpt=cdc_excerpt,
        n=max(1, min(n, 5)),
    )

    import json as _json  # noqa: PLC0415
    import logging  # noqa: PLC0415

    raw = llm_fn(prompt, llm_cfg, 1024)
    if not raw:
        raise HTTPException(status_code=502, detail="LLM returned empty response")

    # Strip markdown code fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[: cleaned.rfind("```")]

    try:
        examples = _json.loads(cleaned)
        if not isinstance(examples, list):
            examples = [examples]
    except _json.JSONDecodeError as exc:
        logging.getLogger("atlas_dashboard").warning(
            "test-inputs JSON parse error for %s: %s — raw: %s", agent_name, exc, raw[:200]
        )
        raise HTTPException(status_code=502, detail=f"LLM returned invalid JSON: {exc}") from exc

    return {"agent": agent_name, "inputs": examples}


class RunTestRequest(BaseModel):
    inputs: dict
    test_id: str = ""


@app.post("/api/runs/{run_id}/agents/{agent_name}/run-test")
def api_run_test(run_id: str, agent_name: str, body: RunTestRequest):
    """Execute agent_name via atlas-bus with the given inputs. Returns the real output."""
    import logging  # noqa: PLC0415

    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    scaffolded = run.get("scaffolded", {}).get(agent_name)
    if not scaffolded:
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{agent_name}' not scaffolded yet. Run scaffold first.",
        )

    try:
        from atlas_bus.bus import Bus  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="atlas-bus not installed") from exc

    bus = Bus()
    known = bus.list_agents()
    if agent_name not in known:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Agent '{agent_name}' not found in atlas-bus. "
                f"Run 'atlas install' first. Known agents: {sorted(known)}"
            ),
        )

    log = logging.getLogger("atlas_dashboard")
    log.info("run-test: executing %s with inputs %s", agent_name, list(body.inputs.keys()))

    step = bus.run_agent(agent_name, body.inputs)

    return {
        "agent": agent_name,
        "passed": step.passed,
        "output": step.summary,
        "logs": step.logs,
        "error": step.error or None,
    }


class JudgeTestRequest(BaseModel):
    agent_name: str
    input: dict
    output: str
    run_id: str = ""


@app.post("/api/runs/{run_id}/agents/{agent_name}/judge-test")
def api_judge_test(run_id: str, agent_name: str, body: JudgeTestRequest):
    """Judge the real output of an agent against its input. Stores score with source='test'."""
    import json as _json  # noqa: PLC0415
    import logging  # noqa: PLC0415

    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    if not body.output:
        raise HTTPException(status_code=400, detail="output is required")

    cdc_full = run.get("metadata", {}).get(
        "cdc_full", run.get("metadata", {}).get("cdc_preview", "")
    )
    criteria = _get_agent_criteria(agent_name)

    # Build context: input data + CDC excerpt so judge has full picture
    input_str = _json.dumps(body.input, ensure_ascii=False, indent=2)
    context = f"Input:\n{input_str}\n\nCDC (excerpt):\n{cdc_full[:1500]}"

    try:
        from atlas_judge.agent import AtlasJudgeAgent  # noqa: PLC0415
        from atlas_memory.kg import KGStore  # noqa: PLC0415

        llm_fn, llm_cfg = _llm_fn_for_dashboard()
        judge = AtlasJudgeAgent(config={"llm": llm_cfg}, llm_fn=llm_fn)
        events = list(
            judge.run(
                operation="pointwise",
                output=body.output,
                agent_name=agent_name,
                context=context,
                criteria=criteria or None,
            )
        )
        summary = next((e for e in events if e.get("summary")), None)
        if not summary:
            raise HTTPException(status_code=500, detail="Judge returned no summary")

        criteria_scores = summary.get("criteria_scores", [])
        feedback = summary.get("feedback", "")
        score_val = summary.get("score")
        if score_val is None and criteria_scores:
            scores = [
                c.get("score", 0)
                for c in criteria_scores
                if isinstance(c.get("score"), int | float)
            ]
            score_val = round(sum(scores) / len(scores), 2) if scores else None

        test_run_id = body.run_id or run_id
        if score_val is not None:
            kg = KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")
            if agent_name in kg._g:
                kg.update_agent_score(
                    agent_name,
                    score_val,
                    run_id=test_run_id,
                    source="test",
                    test_detail={
                        "input": body.input,
                        "output": body.output,
                        "feedback": feedback,
                        "criteria": criteria_scores,
                    },
                )

        return {
            "ok": True,
            "agent": agent_name,
            "judge_score": score_val,
            "judge_criteria": criteria_scores,
            "judge_feedback": feedback,
            "source": "test",
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "judge-test failed for %s: %s", agent_name, exc
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── UI ─────────────────────────────────────────────────────────────────────────


@app.get("/api/kg")
def api_get_kg():
    import logging  # noqa: PLC0415

    try:
        from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

        mem = AtlasMemoryAgent()
        events = list(mem.run(operation="get_graph"))
        kg = next((e["result"] for e in events if e.get("summary")), {})
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning("KG fetch failed: %s", exc)
        kg = {}

    raw_nodes = kg.get("nodes", [])
    raw_links = kg.get("links", [])

    # Only expose agent/tool nodes — criterion/resolution nodes stay in the graph
    # for RAG memory but must not pollute the visualization.
    entity_types = {"agent", "tool"}
    visible_nodes = [n for n in raw_nodes if n.get("node_type", "agent") in entity_types]
    visible_ids = {n.get("id", "") for n in visible_nodes}

    return {
        "nodes": [
            {
                "id": n.get("id", ""),
                "node_type": n.get("node_type", "agent"),
                "capabilities": n.get("capabilities", []),
                "tags": n.get("tags", []),
                "judge_score": n.get("judge_score"),
                "human_score": n.get("human_score"),
                "confidence": n.get("confidence", 1.0),
                "shareable": n.get("shareable", True),
                "score_history": n.get("score_history", []),
                "project": n.get("project", ""),
                "cdc_summary": n.get("cdc_summary", ""),
                "context_tags": n.get("context_tags", []),
                "output_type": n.get("output_type", "json"),
            }
            for n in visible_nodes
        ],
        "edges": [
            {
                "from": lnk.get("source", ""),
                "to": lnk.get("target", ""),
                "relation": lnk.get("relation", ""),
            }
            for lnk in raw_links
            if lnk.get("source") in visible_ids and lnk.get("target") in visible_ids
        ],
    }


_TEST_RUNS_DIR = _Path.home() / ".atlas" / "test_runs"


def _kg_nodes() -> list[dict]:
    kg_path = _Path.home() / ".atlas" / "memory" / "graph.json"
    if not kg_path.exists():
        return []
    import json as _json  # noqa: PLC0415

    data = _json.loads(kg_path.read_text(encoding="utf-8"))
    return data.get("nodes", [])


def _get_atlas_entry_points() -> dict[str, str]:
    """Return {agent_name: 'module:Class'} from atlas.agents entry points."""
    import importlib.metadata as _m  # noqa: PLC0415

    return {ep.name: ep.value for ep in _m.entry_points(group="atlas.agents")}


def _load_agent_class(entry_value: str):
    """Import and return the agent class from 'module:Class' entry point value."""
    import importlib  # noqa: PLC0415

    module_path, class_name = entry_value.rsplit(":", 1)
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


@app.get("/api/projects")
def api_get_projects():
    nodes = _kg_nodes()
    projects = sorted(
        {n.get("project", "") for n in nodes if n.get("node_type") == "agent" and n.get("project")}
    )
    return projects


@app.post("/api/test/run")
def api_test_run(req: TestRunRequest):
    import datetime as _dt  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    import logging  # noqa: PLC0415

    nodes = _kg_nodes()
    agents_for_project = [
        n["id"] for n in nodes if n.get("node_type") == "agent" and n.get("project") == req.project
    ]
    if not agents_for_project:
        raise HTTPException(status_code=404, detail=f"No agents found for project '{req.project}'")

    entry_points = _get_atlas_entry_points()
    llm_fn, llm_cfg = _llm_fn_for_dashboard()

    test_id = str(uuid.uuid4())
    results: list[dict] = []

    for agent_name in agents_for_project:
        ep_value = entry_points.get(agent_name)
        if not ep_value:
            results.append({"agent": agent_name, "error": "entry point not found", "output": None})
            continue
        try:
            AgentClass = _load_agent_class(ep_value)
            agent_instance = AgentClass(llm_fn=llm_fn, llm_cfg=llm_cfg)
            output_raw = None
            for event in agent_instance.run(cdc_bloc=req.ticket, context=""):
                if event.get("summary"):
                    output_raw = event.get("output", "")
            # Try to parse JSON from output (strip markdown fences if present)
            output_json = None
            if output_raw:
                import re as _re  # noqa: PLC0415

                clean = _re.sub(
                    r"^```(?:json)?\s*|\s*```$", "", output_raw.strip(), flags=_re.DOTALL
                )
                import contextlib as _contextlib  # noqa: PLC0415

                with _contextlib.suppress(Exception):
                    output_json = _json.loads(clean)
            results.append(
                {
                    "agent": agent_name,
                    "output": output_raw or "",
                    "output_json": output_json,
                    "error": None,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("atlas_dashboard").warning(
                "Test run failed for %s: %s", agent_name, exc
            )
            results.append(
                {"agent": agent_name, "output": "", "output_json": None, "error": str(exc)}
            )

    run_data = {
        "test_id": test_id,
        "project": req.project,
        "ticket": req.ticket,
        "batch_id": req.batch_id or "",
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "results": results,
        "validations": {},
    }
    _TEST_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (_TEST_RUNS_DIR / f"{test_id}.json").write_text(
        _json.dumps(run_data, indent=2), encoding="utf-8"
    )
    return run_data


@app.get("/api/test/runs")
def api_test_runs():
    import json as _json  # noqa: PLC0415

    if not _TEST_RUNS_DIR.exists():
        return []
    runs = []
    for f in sorted(_TEST_RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[
        :50
    ]:
        import contextlib as _cl  # noqa: PLC0415

        with _cl.suppress(Exception):
            runs.append(_json.loads(f.read_text(encoding="utf-8")))
    return runs


@app.post("/api/test/runs/{test_id}/validate")
def api_test_validate(test_id: str, body: TestValidation):
    import json as _json  # noqa: PLC0415

    path = _TEST_RUNS_DIR / f"{test_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Test run not found")
    run_data = _json.loads(path.read_text(encoding="utf-8"))
    if body.skipped:
        run_data.setdefault("validations", {})[body.agent_name] = {"skipped": True}
    else:
        run_data.setdefault("validations", {})[body.agent_name] = {
            "valid": body.valid,
            "comment": body.comment,
        }
    path.write_text(_json.dumps(run_data, indent=2), encoding="utf-8")

    # Update human_score in KG (latest-per-ticket aggregate of human valid/invalid)
    # human_score is separate from judge_score (LLM judge).
    if not body.skipped:
        try:
            from atlas_memory.kg import KGStore  # noqa: PLC0415

            kg = KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")
            if body.agent_name in kg._g:
                all_runs = sorted(
                    [
                        _json.loads(f.read_text(encoding="utf-8"))
                        for f in _TEST_RUNS_DIR.glob("*.json")
                    ],
                    key=lambda r: r.get("created_at", ""),
                )
                project = run_data.get("project", "")
                latest_per_ticket: dict[str, dict] = {}
                for run in all_runs:
                    if run.get("project") != project:
                        continue
                    v = run.get("validations", {}).get(body.agent_name, {})
                    if v.get("skipped") or "valid" not in v:
                        continue
                    ticket_key = (run.get("ticket") or run.get("test_id", ""))[:120]
                    latest_per_ticket[ticket_key] = v
                valid_count = sum(1 for v in latest_per_ticket.values() if v["valid"])
                total = len(latest_per_ticket)
                if total > 0:
                    human_score = round((valid_count / total) * 5.0, 2)
                    kg.update_human_score(body.agent_name, human_score)
        except Exception:  # noqa: BLE001
            pass

    return {"ok": True}


def _get_agent_criteria(agent_name: str) -> list[str]:
    """Retrieve judge criteria for agent_name from the most recent scaffold run."""

    runs = sorted(
        get_store().list_runs(),
        key=lambda r: r.get("created_at", ""),
        reverse=True,
    )
    for run in runs:
        entry = run.get("scaffolded", {}).get(agent_name, {})
        criteria = entry.get("judge_criteria") or []
        if criteria:
            return [c.get("criterion", "") for c in criteria if c.get("criterion")]
        # Also check scaffold_attempts
        for attempt in run.get("scaffold_attempts", {}).get(agent_name, []):
            criteria = attempt.get("judge_criteria") or []
            if criteria:
                return [c.get("criterion", "") for c in criteria if c.get("criterion")]
    return []


class TestJudgeRequest(BaseModel):
    agent_name: str


@app.post("/api/test/runs/{test_id}/judge")
def api_test_judge(test_id: str, body: TestJudgeRequest):
    """Run LLM judge on the output of a test run and update KG judge_score."""
    import json as _json  # noqa: PLC0415
    import logging  # noqa: PLC0415

    path = _TEST_RUNS_DIR / f"{test_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Test run not found")
    run_data = _json.loads(path.read_text(encoding="utf-8"))

    result = next(
        (r for r in run_data.get("results", []) if r.get("agent") == body.agent_name), None
    )
    if not result or not result.get("output"):
        raise HTTPException(status_code=400, detail="No output found for this agent in this run")

    ticket = run_data.get("ticket", "")
    agent_output = result["output"]
    criteria = _get_agent_criteria(body.agent_name)

    try:
        from atlas_judge.agent import AtlasJudgeAgent  # noqa: PLC0415
        from atlas_memory.kg import KGStore  # noqa: PLC0415

        llm_fn, llm_cfg = _llm_fn_for_dashboard()
        judge = AtlasJudgeAgent(config={"llm": llm_cfg}, llm_fn=llm_fn)
        events = list(
            judge.run(
                operation="pointwise",
                output=agent_output,
                agent_name=body.agent_name,
                context=ticket,
                criteria=criteria or None,
            )
        )
        summary = next((e for e in events if e.get("summary")), None)
        if not summary:
            raise HTTPException(status_code=500, detail="Judge returned no summary")

        criteria_scores = summary.get("criteria_scores", [])
        feedback = summary.get("feedback", "")
        score_val = summary.get("score")
        if score_val is None and criteria_scores:
            scores = [
                c.get("score", 0)
                for c in criteria_scores
                if isinstance(c.get("score"), int | float)
            ]
            score_val = round(sum(scores) / len(scores), 2) if scores else None

        # Store judge result in test run
        run_data.setdefault("judge_results", {})[body.agent_name] = {
            "judge_score": score_val,
            "judge_criteria": criteria_scores,
            "judge_feedback": feedback,
        }
        path.write_text(_json.dumps(run_data, indent=2), encoding="utf-8")

        # Update KG judge_score
        if score_val is not None:
            kg = KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")
            if body.agent_name in kg._g:
                kg.update_agent_score(body.agent_name, score_val, run_id=test_id, source="scaffold")

        return {
            "ok": True,
            "judge_score": score_val,
            "judge_criteria": criteria_scores,
            "judge_feedback": feedback,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "Judge failed for %s: %s", body.agent_name, exc
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/agents/{agent_name}/recalculate-score")
def api_recalculate_score(agent_name: str):
    """Recalculate KG judge_score from latest test run validations (latest per ticket)."""
    import json as _json  # noqa: PLC0415

    try:
        from atlas_memory.kg import KGStore  # noqa: PLC0415

        kg = KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")
        if agent_name not in kg._g:
            raise HTTPException(status_code=404, detail=f"Agent {agent_name} not in KG")
        all_runs = sorted(
            [_json.loads(f.read_text(encoding="utf-8")) for f in _TEST_RUNS_DIR.glob("*.json")],
            key=lambda r: r.get("created_at", ""),
        )
        latest_per_ticket: dict[str, dict] = {}
        for run in all_runs:
            v = run.get("validations", {}).get(agent_name, {})
            if v.get("skipped") or "valid" not in v:
                continue
            ticket_key = (run.get("ticket") or run.get("test_id", ""))[:120]
            latest_per_ticket[ticket_key] = v
        total = len(latest_per_ticket)
        if not total:
            return {"ok": False, "detail": "No validated test runs found for this agent"}
        valid_count = sum(1 for v in latest_per_ticket.values() if v["valid"])
        score = round((valid_count / total) * 5.0, 2)
        kg.update_human_score(agent_name, score)
        return {"ok": True, "score": score, "valid": valid_count, "total": total}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/test/runs/{test_id}")
def api_test_delete(test_id: str):
    path = _TEST_RUNS_DIR / f"{test_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    path.unlink()
    return {"ok": True}


@app.post("/api/test/extract-file")
async def api_test_extract_file(file: UploadFile):
    """Extract text from an uploaded .pdf or .txt file."""
    name = (file.filename or "").lower()
    raw = await file.read()
    if name.endswith(".pdf"):
        try:
            import io  # noqa: PLC0415

            from pdfminer.high_level import extract_text_to_fp  # noqa: PLC0415
            from pdfminer.layout import LAParams  # noqa: PLC0415

            out = io.StringIO()
            extract_text_to_fp(io.BytesIO(raw), out, laparams=LAParams())
            text = out.getvalue().strip()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"PDF extraction failed: {exc}") from exc
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    return {"text": text}


@app.get("/api/test/generate-ticket")
def api_test_generate_ticket(project: str = "", user_prompt: str = ""):
    """Generate a synthetic support ticket for the given project using the LLM."""

    from atlas_memory.kg import KGStore  # noqa: PLC0415

    kg = KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")
    # gather context from KG for the selected project
    cdc_summary = ""
    context_tags: list[str] = []
    for _node, data in kg._g.nodes(data=True):
        if data.get("node_type") == "agent" and (not project or data.get("project") == project):
            cdc_summary = cdc_summary or data.get("cdc_summary", "")
            context_tags += [t for t in data.get("context_tags", []) if t not in context_tags]

    context_line = f"Projet : {project}." if project else ""
    summary_line = f"Contexte du système : {cdc_summary}." if cdc_summary else ""
    tags_line = f"Tags : {', '.join(context_tags)}." if context_tags else ""

    user_instruction = (
        f"Instructions: {user_prompt}"
        if user_prompt
        else ("Generate a realistic and varied example appropriate to the context.")
    )
    prompt = (
        f"{context_line} {summary_line} {tags_line}\n\n"
        f"{user_instruction}\n\n"
        "Write the example in 3-6 lines, as if written by an end user. "
        "No title, no markdown, plain text only."
    ).strip()

    llm_fn, llm_cfg = _llm_fn_for_dashboard()
    try:
        ticket_text = llm_fn(prompt, llm_cfg, 256)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"ticket": ticket_text.strip()}


@app.post("/api/test/generate-batch")
def api_test_generate_batch(req: BatchGenerateRequest):
    """Generate N diverse test cases for the given project using the LLM."""
    import json as _json  # noqa: PLC0415
    import re as _re  # noqa: PLC0415

    from atlas_memory.kg import KGStore  # noqa: PLC0415

    kg = KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")
    cdc_summary = ""
    context_tags: list[str] = []
    for _node, data in kg._g.nodes(data=True):
        if data.get("node_type") == "agent" and (
            not req.project or data.get("project") == req.project
        ):
            cdc_summary = cdc_summary or data.get("cdc_summary", "")
            context_tags += [t for t in data.get("context_tags", []) if t not in context_tags]

    prompt = (
        f"You are building a test suite for an AI pipeline.\n"
        f"Project: {req.project}\n"
        f"System: {cdc_summary}\n"
        f"Tags: {', '.join(context_tags)}\n\n"
        f"Generate exactly {req.n} distinct test cases covering:\n"
        f"- 2-3 nominal cases (typical input the system handles well)\n"
        f"- 2 edge cases (unusual but valid input)\n"
        f"- 1-2 ambiguous cases (input where the right answer is debatable)\n"
        f"- 1 out-of-scope case (input the system should flag or handle gracefully)\n\n"
        f"Return ONLY a JSON array, no markdown, no explanation:\n"
        f'[\n  {{"id": 1, "type": "nominal", "description": "brief label", "input": "the actual test text"}},\n  ...\n]'
    )

    llm_fn, llm_cfg = _llm_fn_for_dashboard()
    try:
        raw = llm_fn(prompt, llm_cfg, 1024)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    clean = _re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=_re.DOTALL)
    try:
        cases = _json.loads(clean)
        if not isinstance(cases, list):
            raise ValueError("not a list")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=422, detail=f"LLM returned invalid JSON: {exc}\n\nRaw: {raw[:300]}"
        ) from exc

    return {"cases": cases}


class AgentPromptPatch(BaseModel):
    prompt: str


def _find_prompts_py(agent_name: str, project: str = "") -> _Path | None:
    """Locate the prompts.py for a given agent name across all projects."""

    slug = _agent_slug(agent_name)
    base = _Path.home() / ".atlas" / "projects"
    if project:
        candidate = base / project / "agents" / slug / slug.replace("-", "_") / "prompts.py"
        if candidate.exists():
            return candidate
    # search all projects
    for p in sorted(base.iterdir()) if base.exists() else []:
        candidate = p / "agents" / slug / slug.replace("-", "_") / "prompts.py"
        if candidate.exists():
            return candidate
    return None


@app.get("/api/agents/{agent_name}/prompt")
def api_agent_get_prompt(agent_name: str, project: str = ""):
    path = _find_prompts_py(agent_name, project)
    if not path:
        raise HTTPException(status_code=404, detail=f"prompts.py not found for {agent_name}")
    content = path.read_text(encoding="utf-8")
    # Extract MAIN_PROMPT value
    import re as _re  # noqa: PLC0415

    m = _re.search(r'MAIN_PROMPT\s*=\s*"""(.*?)"""', content, _re.DOTALL)
    if not m:
        raise HTTPException(status_code=422, detail="MAIN_PROMPT not found in prompts.py")
    return {"prompt": m.group(1), "path": str(path)}


@app.patch("/api/agents/{agent_name}/prompt")
def api_agent_patch_prompt(agent_name: str, body: AgentPromptPatch, project: str = ""):
    path = _find_prompts_py(agent_name, project)
    if not path:
        raise HTTPException(status_code=404, detail=f"prompts.py not found for {agent_name}")
    content = path.read_text(encoding="utf-8")
    import re as _re  # noqa: PLC0415

    new_content = _re.sub(
        r'(MAIN_PROMPT\s*=\s*""").*?(""")',
        lambda m: m.group(1) + body.prompt + m.group(2),
        content,
        flags=_re.DOTALL,
    )
    if new_content == content:
        raise HTTPException(status_code=422, detail="Could not replace MAIN_PROMPT")
    # Backup original
    backup = path.with_suffix(".py.bak")
    backup.write_text(content, encoding="utf-8")
    path.write_text(new_content, encoding="utf-8")
    return {"ok": True, "backup": str(backup)}


@app.post("/api/agents/{agent_name}/prompt/restore")
def api_agent_restore_prompt(agent_name: str, project: str = ""):
    path = _find_prompts_py(agent_name, project)
    if not path:
        raise HTTPException(status_code=404, detail=f"prompts.py not found for {agent_name}")
    backup = path.with_suffix(".py.bak")
    if not backup.exists():
        raise HTTPException(status_code=404, detail="No backup found")
    path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
    backup.unlink()
    return {"ok": True}


@app.get("/api/folders")
def api_folders():
    """Return all CDC projects with their agents and tools."""
    try:
        from atlas_memory.kg import KGStore  # noqa: PLC0415

        kg = KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")
        projects: dict[str, dict] = {}
        for n, d in kg._g.nodes(data=True):
            proj = d.get("project", "")
            if not proj or d.get("node_type") not in ("agent", "tool"):
                continue
            if proj not in projects:
                projects[proj] = {"name": proj, "agents": [], "tools": []}
            entry = {
                "name": n,
                "node_type": d.get("node_type", "agent"),
                "judge_score": d.get("judge_score"),
                "human_score": d.get("human_score"),
                "parent": d.get("parent"),
            }
            if d.get("node_type") == "tool":
                projects[proj]["tools"].append(entry)
            else:
                projects[proj]["agents"].append(entry)
        return {"projects": sorted(projects.values(), key=lambda p: p["name"])}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class DownloadZipRequest(BaseModel):
    project: str
    agent: str


@app.post("/api/agents/download-zip")
def api_download_agent_zip(body: DownloadZipRequest):
    """Stream a zip of the agent package for download."""
    zip_bytes = _mcp_agent_zip(body.project, body.agent)
    slug = body.agent.replace("#", "_")
    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}.zip"'},
    )


@app.get("/api/agents/{agent_name}/forks")
def api_agent_forks(agent_name: str):
    """Return all KG forks of a base agent."""
    try:
        from atlas_memory.kg import KGStore  # noqa: PLC0415

        kg = KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")
        forks = [
            {"name": n, **{k: v for k, v in d.items() if k != "coaching_history"}}
            for n, d in kg._g.nodes(data=True)
            if d.get("parent") == agent_name
        ]
        return {"base": agent_name, "forks": forks}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/agents/{agent_name}/propagate")
def api_agent_propagate(agent_name: str):
    """Propagate coaching patches from a fork to its parent agent."""
    try:
        from atlas_memory.kg import KGStore  # noqa: PLC0415

        kg = KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")
        parent = kg.propagate_to_parent(agent_name)
        if parent is None:
            raise HTTPException(
                status_code=400, detail="Agent has no parent or parent not found in KG."
            )
        return {"ok": True, "fork": agent_name, "parent": parent}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── MCP Server (HTTP Streamable, 2025 spec) ───────────────────────────────────
#
# Exposes three tools:
#   list_projects         → list CDC projects known in the KG
#   open_project(name)    → agents + tools in the project with descriptions
#   download_agent(project, agent) → zip of the agent package
#
# Config for VSCode / Claude Code:
#   { "mcpServers": { "atlas": { "url": "http://localhost:8050/mcp" } } }

import json as _json  # noqa: E402


def _mcp_kg():  # type: ignore[return]
    from atlas_memory.kg import KGStore  # noqa: PLC0415

    return KGStore(_Path.home() / ".atlas" / "memory" / "graph.json")


def _mcp_list_projects() -> list[str]:
    kg = _mcp_kg()
    projects = {d.get("project", "") for _, d in kg._g.nodes(data=True) if d.get("project")}
    return sorted(projects)


def _mcp_open_project(name: str) -> list[dict]:
    kg = _mcp_kg()
    return [
        {
            "name": n,
            "node_type": d.get("node_type", "agent"),
            "capabilities": d.get("capabilities", []),
            "judge_score": d.get("judge_score"),
            "human_score": d.get("human_score"),
            "parent": d.get("parent"),
            "context_tags": d.get("context_tags", []),
        }
        for n, d in kg._g.nodes(data=True)
        if d.get("project") == name and d.get("node_type") in ("agent", "tool")
    ]


def _mcp_agent_zip(project: str, agent: str) -> bytes:
    """Return a zip of the installed agent package, or raise HTTPException."""
    workspace_dir = _Path.home() / ".atlas" / "workspace"
    slug = agent.replace("#", "_")
    agent_dir = workspace_dir / slug
    if not agent_dir.exists():
        # Fallback: search by project slug
        project_slug = project.replace(" ", "_").lower()
        agent_dir = workspace_dir / project_slug / slug
    if not agent_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Agent package not found for '{agent}' in project '{project}'. Run 'atlas install' first.",
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        _SKIP = {"__pycache__", ".git", ".egg-info"}
        for f in agent_dir.rglob("*"):
            if f.is_file() and not any(p.endswith(".egg-info") or p in _SKIP for p in f.parts):
                zf.write(f, f.relative_to(agent_dir.parent))
    return buf.getvalue()


_MCP_TOOLS = [
    {
        "name": "list_projects",
        "description": "List all CDC projects available in the ATLAS Knowledge Graph.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "open_project",
        "description": "List all agents and tools in a given ATLAS project with their descriptions and scores.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Project name"}},
            "required": ["name"],
        },
    },
    {
        "name": "download_agent",
        "description": "Download the zip package of a scaffolded agent from an ATLAS project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name"},
                "agent": {"type": "string", "description": "Agent name (as shown in open_project)"},
            },
            "required": ["project", "agent"],
        },
    },
]


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP HTTP Streamable endpoint."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    method = body.get("method", "")
    req_id = body.get("id")

    def _ok(result: object) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return _ok(
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "atlas-mcp", "version": "0.1.0"},
            }
        )

    if method == "tools/list":
        return _ok({"tools": _MCP_TOOLS})

    if method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name", "")
        args = params.get("arguments", {})

        try:
            if tool_name == "list_projects":
                projects = _mcp_list_projects()
                text = _json.dumps(projects, ensure_ascii=False, indent=2)
                return _ok({"content": [{"type": "text", "text": text}]})

            if tool_name == "open_project":
                name = args.get("name", "")
                items = _mcp_open_project(name)
                text = _json.dumps(items, ensure_ascii=False, indent=2)
                return _ok({"content": [{"type": "text", "text": text}]})

            if tool_name == "download_agent":
                project = args.get("project", "")
                agent = args.get("agent", "")
                zip_bytes = _mcp_agent_zip(project, agent)
                import base64  # noqa: PLC0415

                b64 = base64.b64encode(zip_bytes).decode()
                return _ok(
                    {
                        "content": [
                            {"type": "text", "text": f"Agent '{agent}' zip (base64):"},
                            {"type": "text", "text": b64},
                        ]
                    }
                )

            return _err(-32601, f"Unknown tool: {tool_name}")
        except HTTPException as exc:
            return _err(-32603, exc.detail)
        except Exception as exc:  # noqa: BLE001
            return _err(-32603, str(exc))

    return _err(-32601, f"Unknown method: {method}")


@app.get("/", response_class=HTMLResponse)
def ui_index():
    tpl = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(tpl.read_text(encoding="utf-8"))
