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

    # Smoke-test the LLM before handing it to agent-forge
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
    output_dir = base_output_dir

    _SCAFFOLD_MAX_ATTEMPTS = 3
    _SCAFFOLD_QUALITY_THRESHOLD = 4.0

    attempts_log: list[dict] = []
    last_output_path: str = ""
    last_files: dict[str, str] = {}
    last_spec: dict = {}

    try:
        # CR context injected by user after a diagnosis
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

        for attempt in range(1, _SCAFFOLD_MAX_ATTEMPTS + 1):
            # Enrich spec with feedback from previous failed attempts
            constraints = cr_context
            if attempts_log:
                lines = []
                for a in attempts_log:
                    for cs in a.get("criteria_scores", []):
                        if cs.get("score", 5) < 4:
                            lines.append(f"- {cs['criterion']}: {cs.get('comment', '')}")
                if lines:
                    constraints += (
                        "\n\n# Constraints from previous attempts — must fix:\n" + "\n".join(lines)
                    )

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
                forge = AgentForgeAgent(
                    output_dir=output_dir, config={"llm": llm_cfg}, llm_fn=llm_fn
                )
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

            # Install and evaluate with judge
            _pip_install(target_path)

            judge_score, judge_criteria, judge_feedback = _judge_scaffold(
                target_path, agent_name, cdc_full, llm_fn, llm_cfg
            )
            attempts_log.append(
                {
                    "attempt": attempt,
                    "criteria_scores": judge_criteria,
                    "feedback": judge_feedback,
                }
            )

            last_output_path = str(target_path)
            last_spec = summary
            last_files = _read_generated_files(target_path)

            if judge_score is not None and judge_score >= _SCAFFOLD_QUALITY_THRESHOLD:
                store.set_scaffold_result(run_id, agent_name, last_output_path, last_spec)
                return {
                    "agent_name": agent_name,
                    "output_path": last_output_path,
                    "spec": last_spec,
                    "files": last_files,
                    "attempts": attempt,
                    "judge_score": judge_score,
                }

        # All attempts failed — run diagnose
        diagnosis = _run_diagnose(attempts_log, agent_name, cdc_full, llm_fn, llm_cfg)
        store.set_scaffold_result(run_id, agent_name, last_output_path, last_spec)
        store.set_scaffold_diagnosis(run_id, agent_name, diagnosis)
        return {
            "agent_name": agent_name,
            "output_path": last_output_path,
            "spec": last_spec,
            "files": last_files,
            "attempts": _SCAFFOLD_MAX_ATTEMPTS,
            "diagnosis": diagnosis,
        }

    finally:
        pass  # attempt_spec_path is cleaned up inside the loop


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
    _register_tool_in_kg(tool_name, output_path)
    # create KG edge caller_agent --USES_TOOL--> tool if called_by is set on the bloc
    blocs = run.get("blocs", [])
    called_by = next(
        (
            b.get("called_by")
            for b in blocs
            if b.get("agent_suggested") == tool_name and b.get("type") == "tool"
        ),
        None,
    )
    if called_by:
        _add_kg_edge(called_by, tool_name, "USES_TOOL")
    store.set_tool_installed(run_id, tool_name)
    return {"status": "installed", "tool_name": tool_name, "called_by": called_by}


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

        # Try to load the agent and run it on a minimal test input
        try:
            import importlib  # noqa: PLC0415

            mod = importlib.import_module(module_slug)
            class_name = next(
                (k for k in dir(mod) if k.endswith("Agent") and not k.startswith("_")),
                None,
            )
            if class_name:
                agent_instance = getattr(mod, class_name)(llm_fn=llm_fn, config={"llm": llm_cfg})
                output_events = list(agent_instance.run(cdc_bloc=cdc_full[:200], context="test"))
                agent_output = " ".join(
                    str(e.get("output", e.get("msg", "")))
                    for e in output_events
                    if isinstance(e, dict)
                )
            else:
                agent_output = "(agent loaded but no Agent class found)"
        except Exception as exc:  # noqa: BLE001
            agent_output = f"(agent execution failed: {exc})"

        judge = AtlasJudgeAgent(config={"llm": llm_cfg}, llm_fn=llm_fn)
        events = list(
            judge.run(
                operation="pointwise",
                output=agent_output,
                agent_name=agent_name,
                context=cdc_full[:500],
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


def _register_tool_in_kg(tool_name: str, output_path: str) -> None:
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
            logging.getLogger("atlas_dashboard").warning(
                "KG tool registration warning for %s: %s", tool_name, err.get("msg")
            )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_dashboard").warning(
            "KG tool registration failed for %s (non-blocking): %s", tool_name, exc
        )


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
