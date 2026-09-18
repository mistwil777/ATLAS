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
from pydantic import BaseModel


class ScaffoldRequest(BaseModel):
    user_answers: list[str] = []
    selected_patches: list[str] = []
    judge_feedback_context: str = ""


class ApproveRequest(BaseModel):
    agent_names: list[str] | None = None


class ImproveRequest(BaseModel):
    run_id: str | None = None


class AnswerRequest(BaseModel):
    answers: dict[str, str]


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

    return {
        "status": "resumed",
        "new_run_id": report.get("run_id"),
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

    import re as _re  # noqa: PLC0415

    agent_slug = _re.sub(r"(?<!^)(?=[A-Z])", "-", agent_name).lower()
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

    success, result, attempts_log2, last_output_path, last_spec, last_files = _run_scaffold_cycle(
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
        # Index: client answers resolved missing_spec criteria
        _index_scaffold_resolutions(agent_name, saved_attempts_log, attempts_log2)
        return result

    # Still failing — try advisor then diagnose
    all_attempts = saved_attempts_log + attempts_log2
    advisor_actions = _run_improvement_advisor(agent_name, store)
    if advisor_actions:
        advisor_context = (
            new_cr_context
            + "\n\n# Improvement-advisor suggested actions:\n"
            + "\n".join(f"- {a['action']}" for a in advisor_actions)
        )
        success3, result3, attempts_log3, last_output_path, last_spec, last_files = (
            _run_scaffold_cycle(
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
                "criteria": crit_delta,
                "feedback": judge_feedback,
                "patches_applied": selected_patches,
                "user_answers": user_answers,
                "model_id": model_id,
            }
        )
    return points


@app.get("/api/stats/trend")
def api_stats_trend(agent: str):
    runs = sorted(get_store().list_runs(), key=lambda r: r.get("created_at", ""))
    return {"agent": agent, "points": _build_trend_points(agent, runs)}


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
    advisor = ImprovementAdvisorAgent(config={"llm": llm_cfg}, llm_fn=llm_fn)
    events = list(advisor.run(agent_name=agent_name, trend_points=trend_points))
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
            score = cs.get("score", 5)
            cause = cs.get("cause")
            if not name or score >= 4:
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
) -> tuple[bool, dict | None, list[dict], str, dict, dict]:
    """Run one scaffold cycle (up to max_attempts). Returns (success, result, attempts_log,
    last_output_path, last_spec, last_files). result is None on failure."""
    attempts_log: list[dict] = []
    last_output_path = ""
    last_spec: dict = {}
    last_files: dict = {}

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
        if summary is None:
            errors = [e.get("msg", "") for e in events if e.get("level") == "error"]
            raise HTTPException(status_code=500, detail=f"Scaffolding failed: {errors}")

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
            )

    return False, None, attempts_log, last_output_path, last_spec, last_files


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

        advisor = ImprovementAdvisorAgent(config={"llm": llm_cfg}, llm_fn=llm_fn)
        events = list(advisor.run(agent_name=agent_name, trend_points=trend_points))
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


@app.post("/api/runs/{run_id}/scaffold/{agent_name}")
def api_scaffold(
    run_id: str,
    agent_name: str,
    body: ScaffoldRequest | None = None,
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
        from agent_forge.agent import AgentForgeAgent  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="agent-forge not installed") from exc

    llm_fn, llm_cfg = _llm_fn_for_dashboard()

    _probe = llm_fn("Reply OK", llm_cfg, 8)
    if not _probe:
        raise HTTPException(
            status_code=500,
            detail="LLM probe failed — check atlas.toml [llm] config and credentials",
        )

    import re as _re  # noqa: PLC0415

    agent_slug = _re.sub(r"(?<!^)(?=[A-Z])", "-", agent_name).lower()

    meta = run.get("metadata", {})
    project_dir = meta.get("project_dir", "")
    project_owner = meta.get("project_owner", "client")
    if project_owner == "capgemini" or not project_dir:
        base_output_dir = _Path.home() / ".atlas" / "scaffolded"
    else:
        base_output_dir = _Path(project_dir) / "agents"
    base_output_dir.mkdir(parents=True, exist_ok=True)

    body = body or ScaffoldRequest()
    cr_context = ""
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
    success, result, attempts_log, last_output_path, last_spec, last_files = _run_scaffold_cycle(
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

    # Cycle 1 exhausted — check cause distribution before next action
    ctx1 = _build_consolidated_context(agent_name, attempts_log)
    missing_issues = [i for i in ctx1["open_issues"] if i.get("cause") == "missing_spec"]

    if missing_issues:
        # Suspend and ask client — only the client can resolve missing_spec
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
            + "\n\n# Improvement-advisor suggested actions:\n"
            + "\n".join(f"- {a['action']}" for a in advisor_actions)
        )
        success2, result2, attempts_log2, last_output_path, last_spec, last_files = (
            _run_scaffold_cycle(
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


@app.post("/api/runs/{run_id}/build-tool/{tool_name}")
def api_build_tool(run_id: str, tool_name: str):
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if tool_name not in (run.get("requires_tools") or []):
        raise HTTPException(
            status_code=400, detail=f"{tool_name} is not in requires_tools for this run"
        )

    try:
        from tool_forge.agent import ToolForgeAgent  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="tool-forge not installed") from exc

    llm_fn, llm_cfg = _llm_fn_for_dashboard()

    # Build spec from the matching bloc + CDC context
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
        raise HTTPException(status_code=500, detail=f"Tool generation failed: {errors}")

    store.set_tool_result(run_id, tool_name, summary["output_path"], summary)
    _index_tool_in_rag(tool_name, spec_text, summary)

    from atlas_dashboard.atlas_mlflow import log_tool_build  # noqa: PLC0415

    log_tool_build(
        tool_name=tool_name,
        contract_passed=summary.get("contract_passed", False),
        module_name=summary.get("module_name", ""),
        functions=summary.get("functions", []),
        atlas_run_id=run_id,
    )

    files = _read_generated_files(_Path(summary["output_path"]))
    return {
        "tool_name": tool_name,
        "output_path": summary["output_path"],
        "module_name": summary["module_name"],
        "functions": summary["functions"],
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
    kg_ok, kg_err = _register_tool_in_kg(tool_name, output_path)
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
    if called_by:
        _add_kg_edge(called_by, tool_name, "USES_TOOL")
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

    # Register the new agent in the KG so cdc-router can find it at re-run
    _register_agent_in_kg(agent_name, output_path)

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
        from atlas_judge.agent import AtlasJudgeAgent  # noqa: PLC0415

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

            mod = importlib.import_module(module_slug)
            class_name = next(
                (k for k in dir(mod) if k.endswith("Agent") and not k.startswith("_")),
                None,
            )
            if class_name:
                agent_instance = getattr(mod, class_name)(llm_fn=llm_fn, config={"llm": llm_cfg})
                output_events = list(agent_instance.run(cdc_bloc=test_input, context=cdc_full))
                agent_output = " ".join(
                    str(e.get("output", e.get("summary", e.get("msg", ""))))
                    for e in output_events
                    if isinstance(e, dict)
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


def _register_tool_in_kg(tool_name: str, output_path: str) -> tuple[bool, str]:
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
            score = c.get("score", 5)
            if not crit_name or score >= 3:
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
    """Add KG edges: criterion --RESOLVED_BY--> resolution_key."""
    import logging  # noqa: PLC0415

    try:
        from atlas_memory.agent import _is_shareable  # noqa: PLC0415

        if not _is_shareable():
            return
        from atlas_memory.kg import KGStore  # noqa: PLC0415

        _kg_path = _Path.home() / ".atlas" / "memory" / "graph.json"
        kg = KGStore(_kg_path)
        # Ensure both nodes exist with node_type="resolution" so _fmt_agents()
        # never picks them up as agents or tools in cdc-router pass 2.
        for node_name in (criterion, resolution_key):
            if node_name not in kg._g.nodes:
                kg.register_agent(
                    name=node_name,
                    capabilities=[],
                    inputs=[],
                    outputs=[],
                    tags=["resolution"],
                    node_type="resolution",
                    shareable=True,
                )
        kg.add_edge(criterion, resolution_key, "RESOLVED_BY")
        kg.add_edge(agent_name, criterion, "RESOLVED")
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "KG RESOLVED_BY edge for %s/%s (non-blocking): %s", criterion, resolution_key, exc
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in output_path.rglob("*"):
            if p.is_file():
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
    return {
        "nodes": [
            {
                "id": n.get("id", ""),
                "node_type": n.get("node_type", "agent"),
                "capabilities": n.get("capabilities", []),
                "tags": n.get("tags", []),
            }
            for n in raw_nodes
        ],
        "edges": [
            {
                "source": lnk.get("source", ""),
                "target": lnk.get("target", ""),
                "relation": lnk.get("relation", ""),
            }
            for lnk in raw_links
        ],
    }


@app.get("/", response_class=HTMLResponse)
def ui_index():
    tpl = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(tpl.read_text(encoding="utf-8"))
