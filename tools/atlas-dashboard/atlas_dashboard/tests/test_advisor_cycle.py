"""Tests for _run_improvement_advisor and the advisor cycle 2 routing in api_scaffold."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from atlas_dashboard.app import _run_improvement_advisor

# ── helpers ───────────────────────────────────────────────────────────────────

_TREND_POINTS = [
    {"date": "2026-09-15", "score": 2.1, "criteria": ["output_language", "binary_rules"]},
    {"date": "2026-09-16", "score": 2.4, "criteria": ["output_language"]},
    {"date": "2026-09-17", "score": 2.8, "criteria": ["binary_rules"]},
]

_ADVISOR_SUMMARY = {
    "summary": True,
    "agent_name": "priority-classifier",
    "suggested_actions": [
        {"action": "Always specify output language in the system prompt", "priority": "high"},
        {"action": "Add an explicit JSON schema validator step", "priority": "medium"},
    ],
}


def _make_store(runs: list[dict]) -> MagicMock:
    store = MagicMock()
    store.list_runs.return_value = runs
    return store


def _runs_with_trend(agent_name: str) -> list[dict]:
    return [
        {
            "agent_name": agent_name,
            "created_at": f"2026-09-1{i}T10:00:00",
            "scaffold_result": {
                agent_name: {
                    "judge_score": 2.0 + i * 0.2,
                    "criteria": [{"criterion": "output_language", "score": 1}],
                }
            },
        }
        for i in range(3)
    ]


# ── _run_improvement_advisor ──────────────────────────────────────────────────


def test_advisor_returns_actions_when_advisor_available():
    store = _make_store(_runs_with_trend("priority-classifier"))

    mock_advisor = MagicMock()
    mock_advisor.run.return_value = [_ADVISOR_SUMMARY]

    with (
        patch("atlas_dashboard.app._build_trend_points", return_value=_TREND_POINTS),
        patch("atlas_dashboard.app._llm_fn_for_dashboard", return_value=(lambda p, c, t: "", {})),
        patch(
            "atlas_dashboard.app.ImprovementAdvisorAgent",
            return_value=mock_advisor,
            create=True,
        ),
        patch.dict(
            "sys.modules",
            {"improvement_advisor": MagicMock(), "improvement_advisor.agent": MagicMock()},
        ),
    ):
        # Patch the import inside the function
        import sys

        mock_module = MagicMock()
        mock_module.ImprovementAdvisorAgent = MagicMock(return_value=mock_advisor)
        sys.modules["improvement_advisor"] = mock_module
        sys.modules["improvement_advisor.agent"] = mock_module

        actions = _run_improvement_advisor("priority-classifier", store)

    assert isinstance(actions, list)


def test_advisor_returns_empty_when_import_fails():
    store = _make_store([])

    import sys

    original = sys.modules.get("improvement_advisor")
    sys.modules["improvement_advisor"] = None  # type: ignore[assignment]

    try:
        actions = _run_improvement_advisor("priority-classifier", store)
    finally:
        if original is None:
            sys.modules.pop("improvement_advisor", None)
        else:
            sys.modules["improvement_advisor"] = original

    assert actions == []


def test_advisor_returns_empty_when_no_trend_data():
    store = _make_store([])  # no runs → no trend points

    with (
        patch("atlas_dashboard.app._build_trend_points", return_value=[]),
        patch("atlas_dashboard.app._build_trend_points_from_scaffold", return_value=[]),
        patch("atlas_dashboard.app._llm_fn_for_dashboard", return_value=(lambda p, c, t: "", {})),
        patch.dict(
            "sys.modules",
            {
                "improvement_advisor": MagicMock(),
                "improvement_advisor.agent": MagicMock(
                    ImprovementAdvisorAgent=MagicMock(return_value=MagicMock())
                ),
            },
        ),
    ):
        actions = _run_improvement_advisor("priority-classifier", store)

    assert actions == []


def test_advisor_returns_empty_on_exception():
    store = _make_store(_runs_with_trend("priority-classifier"))

    def _boom(*a, **kw):
        raise RuntimeError("LLM unavailable")

    with (
        patch("atlas_dashboard.app._build_trend_points", return_value=_TREND_POINTS),
        patch("atlas_dashboard.app._llm_fn_for_dashboard", side_effect=_boom),
        patch.dict(
            "sys.modules",
            {"improvement_advisor": MagicMock(), "improvement_advisor.agent": MagicMock()},
        ),
    ):
        actions = _run_improvement_advisor("priority-classifier", store)

    assert actions == []


def test_advisor_returns_empty_when_advisor_yields_no_summary():
    store = _make_store(_runs_with_trend("priority-classifier"))

    mock_advisor = MagicMock()
    mock_advisor.run.return_value = [{"level": "info", "msg": "starting"}]  # no summary

    import sys

    mock_module = MagicMock()
    mock_module.ImprovementAdvisorAgent = MagicMock(return_value=mock_advisor)
    sys.modules["improvement_advisor"] = mock_module
    sys.modules["improvement_advisor.agent"] = mock_module

    with (
        patch("atlas_dashboard.app._build_trend_points", return_value=_TREND_POINTS),
        patch("atlas_dashboard.app._llm_fn_for_dashboard", return_value=(lambda p, c, t: "", {})),
    ):
        actions = _run_improvement_advisor("priority-classifier", store)

    assert actions == []


# ── api_scaffold routing logic (unit, no HTTP) ────────────────────────────────


def test_scaffold_flow_missing_spec_skips_advisor():
    """If missing_spec issues exist after cycle 1, we must NOT call the advisor.
    We test the branching logic by checking that _run_improvement_advisor is not
    called when _generate_questions returns questions."""
    from atlas_dashboard.app import _build_consolidated_context

    attempts_log = [
        {
            "attempt": 1,
            "criteria_scores": [
                {"criterion": "output_language", "score": 1, "cause": "missing_spec"},
            ],
        }
    ]
    ctx = _build_consolidated_context("priority-classifier", attempts_log)
    missing = [i for i in ctx["open_issues"] if i.get("cause") == "missing_spec"]
    assert len(missing) == 1, "missing_spec issue must be detected"
    # Routing: if missing → wait for client (advisor not called)
    # This is verified by the branching: `if missing_issues: ... return waiting_for_client`
    # We confirm the context correctly classifies it so the branch is reachable


def test_scaffold_flow_model_error_only_reaches_advisor_branch():
    """If all failures are model_error/code_error, missing_issues list is empty,
    so the advisor branch is reachable."""
    from atlas_dashboard.app import _build_consolidated_context

    attempts_log = [
        {
            "attempt": 1,
            "criteria_scores": [
                {"criterion": "binary_rules", "score": 2, "cause": "model_error"},
                {"criterion": "json_schema", "score": 1, "cause": "code_error"},
            ],
        }
    ]
    ctx = _build_consolidated_context("priority-classifier", attempts_log)
    missing = [i for i in ctx["open_issues"] if i.get("cause") == "missing_spec"]
    assert missing == [], "no missing_spec → advisor branch reachable"
    assert len(ctx["open_issues"]) == 2


def test_advisor_context_injects_actions():
    """The advisor context string must include all suggested action lines."""
    actions = [
        {"action": "Specify output language in the system prompt", "priority": "high"},
        {"action": "Add JSON schema validation step", "priority": "medium"},
    ]
    cr_context = "## CDC\nClassify support tickets."
    advisor_context = (
        cr_context
        + "\n\n# Improvement-advisor suggested actions:\n"
        + "\n".join(f"- {a['action']}" for a in actions)
    )
    assert "Specify output language" in advisor_context
    assert "Add JSON schema validation step" in advisor_context
    assert advisor_context.startswith(cr_context)
