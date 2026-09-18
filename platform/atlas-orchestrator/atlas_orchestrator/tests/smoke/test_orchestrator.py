"""Smoke tests for atlas-orchestrator — no AWS, no subprocesses."""

from __future__ import annotations

from unittest.mock import MagicMock

from atlas_orchestrator.agent import AtlasOrchestratorAgent
from atlas_orchestrator.pipeline import orchestrate
from atlas_orchestrator.report import build_report

# ── Fixtures / helpers ────────────────────────────────────────────────────────

_CDC_TEXT = "Extraire les noms clients depuis les emails entrants."

_ROUTING_SUMMARY = {
    "summary": True,
    "cdc_path": "inline",
    "blocs": [
        {"bloc": "Extraire les noms clients", "type": "ia", "agent_suggested": "pair-forge"},
        {"bloc": "Valider le format email", "type": "code", "agent_suggested": "cdc-router"},
    ],
    "pipeline": ["pair-forge", "cdc-router"],
    "requires_creation": [],
    "requires_tools": [],
}

_JUDGE_SUMMARY = {
    "summary": True,
    "passed": True,
    "score": 4.5,
    "max_score": 5,
    "mode": "pointwise",
    "criteria_scores": [],
    "feedback": "Good output.",
    "winner": None,
}


def _make_mock_cdc_router(summary: dict | None = None):
    """Returns a factory for CdcRouterAgent that yields a given summary."""
    target = summary if summary is not None else _ROUTING_SUMMARY

    class _MockRouter:
        def __init__(self, **kwargs):
            pass

        def run(self, **kwargs):
            if target:
                yield {"level": "info", "msg": "routing..."}
                yield target

    return _MockRouter


def _make_mock_bus(passed: bool = True, not_found: bool = False):
    """Returns a factory for Bus that returns a StepResult."""
    from atlas_bus.runner import StepResult  # noqa: PLC0415

    def _bus_fn():
        bus = MagicMock()
        if not_found:
            step = StepResult(
                agent_name="pair-forge",
                error="Agent 'pair-forge' not found. Installed: []",
            )
        else:
            step = StepResult(agent_name="pair-forge", passed=passed)
            step.summary = {"summary": True, "output": "extracted: Jean Dupont"}
        bus.run_agent.return_value = step
        return bus

    return _bus_fn


def _make_mock_judge(passed: bool = True):
    """Returns a factory for AtlasJudgeAgent that returns a given result."""

    def _judge_fn():
        agent = MagicMock()
        agent.run.return_value = iter(
            [
                {"level": "info", "msg": "evaluating..."},
                _JUDGE_SUMMARY if passed else {**_JUDGE_SUMMARY, "passed": False, "score": 2.0},
            ]
        )
        return agent

    return _judge_fn


def _mock_llm(prompt: str, cfg: dict, max_tokens: int) -> str:
    return ""


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_orchestrate_full_mock(monkeypatch):
    """Full happy-path: 2 blocs, both pass judge."""
    monkeypatch.setattr(
        "atlas_orchestrator.pipeline.CdcRouterAgent",
        _make_mock_cdc_router(),
        raising=False,
    )
    # Patch import inside pipeline
    import atlas_orchestrator.pipeline as p

    monkeypatch.setattr(p, "_segment", lambda *a, **kw: _ROUTING_SUMMARY)

    report = orchestrate(
        cdc_text=_CDC_TEXT,
        llm_fn=_mock_llm,
        llm_cfg={},
        bus_fn=_make_mock_bus(passed=True),
        judge_fn=_make_mock_judge(passed=True),
    )

    assert report is not None
    assert report["overall_passed"] is True
    assert report["blocs_passed"] == 2
    assert report["blocs_total"] == 2


def test_orchestrate_agent_not_found(monkeypatch):
    """Bus returns 'not found' → bloc skipped, overall_passed False."""
    import atlas_orchestrator.pipeline as p

    monkeypatch.setattr(p, "_segment", lambda *a, **kw: _ROUTING_SUMMARY)

    report = orchestrate(
        cdc_text=_CDC_TEXT,
        llm_fn=_mock_llm,
        llm_cfg={},
        bus_fn=_make_mock_bus(not_found=True),
        judge_fn=_make_mock_judge(),
    )

    assert report is not None
    skipped = [r for r in report["results"] if r["status"] == "skipped"]
    assert len(skipped) == 2
    assert report["overall_passed"] is False


def test_orchestrate_agent_failure(monkeypatch):
    """Bus returns passed=False → bloc status=failed."""
    import atlas_orchestrator.pipeline as p

    monkeypatch.setattr(p, "_segment", lambda *a, **kw: _ROUTING_SUMMARY)

    report = orchestrate(
        cdc_text=_CDC_TEXT,
        llm_fn=_mock_llm,
        llm_cfg={},
        bus_fn=_make_mock_bus(passed=False),
        judge_fn=_make_mock_judge(),
    )

    assert report is not None
    failed = [r for r in report["results"] if r["status"] == "failed"]
    assert len(failed) == 2


def test_orchestrate_judge_skipped(monkeypatch):
    """judge_fn raises ImportError → judge_passed=None, bloc not blocking."""
    import atlas_orchestrator.pipeline as p

    monkeypatch.setattr(p, "_segment", lambda *a, **kw: _ROUTING_SUMMARY)

    def _no_judge_fn():
        raise ImportError("atlas-judge not installed")

    report = orchestrate(
        cdc_text=_CDC_TEXT,
        llm_fn=_mock_llm,
        llm_cfg={},
        bus_fn=_make_mock_bus(passed=True),
        judge_fn=_no_judge_fn,
    )

    assert report is not None
    for r in report["results"]:
        if r["status"] == "success":
            assert r["judge_passed"] is None


def test_orchestrate_cdc_router_failure(monkeypatch):
    """cdc-router returns None → orchestrate returns None."""
    import atlas_orchestrator.pipeline as p

    monkeypatch.setattr(p, "_segment", lambda *a, **kw: None)

    report = orchestrate(
        cdc_text=_CDC_TEXT,
        llm_fn=_mock_llm,
        llm_cfg={},
        bus_fn=_make_mock_bus(),
        judge_fn=_make_mock_judge(),
    )

    assert report is None


def test_run_generator_yields_summary(monkeypatch):
    """AtlasOrchestratorAgent.run() yields a summary event."""
    import atlas_orchestrator.pipeline as p

    monkeypatch.setattr(p, "_segment", lambda *a, **kw: _ROUTING_SUMMARY)

    agent = AtlasOrchestratorAgent(
        llm_fn=_mock_llm,
        bus_fn=_make_mock_bus(passed=True),
        judge_fn=_make_mock_judge(passed=True),
    )
    events = list(agent.run(cdc_text=_CDC_TEXT))
    summary = next((e for e in events if e.get("summary")), None)

    assert summary is not None
    assert "overall_passed" in summary
    assert "results" in summary


def test_report_structure():
    """build_report returns all expected fields."""
    bloc_results = [
        {
            "bloc": "A",
            "type": "ia",
            "agent": "pair-forge",
            "status": "success",
            "judge_passed": True,
            "judge_score": 4.5,
            "output_preview": "...",
        },
        {
            "bloc": "B",
            "type": "code",
            "agent": "cdc-router",
            "status": "skipped",
            "judge_passed": None,
            "judge_score": None,
            "output_preview": "",
        },
    ]
    report = build_report(
        cdc_path="inline",
        routing_summary=_ROUTING_SUMMARY,
        bloc_results=bloc_results,
    )

    assert report["blocs_total"] == 2
    assert report["blocs_processed"] == 1  # skipped excluded
    assert report["blocs_passed"] == 1
    assert report["overall_passed"] is True
    assert "pipeline" in report
    assert "requires_creation" in report
    assert "requires_tools" in report
    assert len(report["results"]) == 2


def test_check_dependencies():
    from atlas_orchestrator.tools import check_dependencies

    result = check_dependencies()
    assert "missing" in result
    assert isinstance(result["missing"], list)
    # boto3 is installed in dev deps
    boto3_missing = [m for m in result["missing"] if m["primary"]["import"] == "boto3"]
    assert len(boto3_missing) == 0


# ── CDC gap → waiting_for_client tests ───────────────────────────────────────

_HIGH_GAP = {
    "type": "missing",
    "description": "Langue de sortie non spécifiée — français ou anglais ?",
    "bloc_concerned": "Classifier les tickets",
    "impact": "high",
}

_LOW_GAP = {
    "type": "vague",
    "description": "Performance acceptable non définie",
    "bloc_concerned": "",
    "impact": "low",
}

_ROUTING_WITH_GAPS = {
    **_ROUTING_SUMMARY,
    "cdc_gaps": [_HIGH_GAP, _LOW_GAP],
}

_ROUTING_WITH_LOW_GAPS_ONLY = {
    **_ROUTING_SUMMARY,
    "cdc_gaps": [_LOW_GAP],
}


def test_orchestrate_suspends_on_high_impact_gaps(tmp_path):
    """High-impact gaps must suspend the run (waiting_for_client) before scaffold."""
    report = orchestrate(
        cdc_text=_CDC_TEXT,
        llm_fn=lambda p, c, t: "",
        llm_cfg={},
        run_store_dir=tmp_path,
        bus_fn=_make_mock_bus(),
        judge_fn=_make_mock_judge(),
        cdc_router_cls=_make_mock_cdc_router(_ROUTING_WITH_GAPS),
    )
    assert report is not None
    assert report.get("status") == "waiting_for_client"
    assert len(report.get("pending_questions", [])) == 1  # only the high-impact gap


def test_orchestrate_no_suspension_on_low_gaps_only(tmp_path):
    """Low-impact gaps alone must NOT suspend the run."""
    report = orchestrate(
        cdc_text=_CDC_TEXT,
        llm_fn=lambda p, c, t: "",
        llm_cfg={},
        run_store_dir=tmp_path,
        bus_fn=_make_mock_bus(),
        judge_fn=_make_mock_judge(),
        cdc_router_cls=_make_mock_cdc_router(_ROUTING_WITH_LOW_GAPS_ONLY),
    )
    assert report is not None
    assert report.get("status") != "waiting_for_client"


def test_orchestrate_no_suspension_when_no_gaps(tmp_path):
    """No gaps → run proceeds normally."""
    report = orchestrate(
        cdc_text=_CDC_TEXT,
        llm_fn=lambda p, c, t: "",
        llm_cfg={},
        run_store_dir=tmp_path,
        bus_fn=_make_mock_bus(),
        judge_fn=_make_mock_judge(),
        cdc_router_cls=_make_mock_cdc_router(_ROUTING_SUMMARY),
    )
    assert report is not None
    assert report.get("status") != "waiting_for_client"


def test_gaps_to_questions_converts_correctly():
    from atlas_orchestrator.pipeline import _gaps_to_questions

    gaps = [_HIGH_GAP, _LOW_GAP]
    questions = _gaps_to_questions(gaps)
    assert len(questions) == 2
    assert questions[0]["id"] == "q_001"
    assert questions[0]["cause"] == "missing_spec"
    assert questions[0]["impact"] == "high"
    assert questions[0]["gap_type"] == "missing"
    assert "Classifier les tickets" in questions[0]["question"]
    assert questions[0]["question_type"] == "text"
    assert questions[1]["id"] == "q_002"


def test_run_store_receives_pending_questions_on_high_gap(tmp_path):
    """set_pending_questions must be called with the high-impact gap questions."""
    report = orchestrate(
        cdc_text=_CDC_TEXT,
        llm_fn=lambda p, c, t: "",
        llm_cfg={},
        run_store_dir=tmp_path,
        bus_fn=_make_mock_bus(),
        judge_fn=_make_mock_judge(),
        cdc_router_cls=_make_mock_cdc_router(_ROUTING_WITH_GAPS),
    )
    assert report is not None
    run_id = report.get("run_id")
    assert run_id is not None

    # Read run file directly — avoids importing atlas-memory (not installed in this CI job)
    import json as _json

    run_file = tmp_path / f"{run_id}.json"
    assert run_file.exists(), f"Run file not found: {run_file}"
    run = _json.loads(run_file.read_text())
    assert run["status"] == "waiting_for_client"
    assert len(run["pending_questions"]) == 1
    assert run["pending_agent"] == "cdc-router"
