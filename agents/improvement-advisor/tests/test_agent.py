"""Unit tests for ImprovementAdvisorAgent."""

from __future__ import annotations

import json

from improvement_advisor.agent import ImprovementAdvisorAgent
from improvement_advisor.stats import _confidence_tier, compute_stats

MOCK_TREND_POINTS = [
    {
        "run_id": "run1",
        "created_at": "2026-09-16T10:00:00Z",
        "avg_score": 2.8,
        "cause": "initial_scaffold",
        "criteria": [
            {"criterion": "priority_alignment", "score": 2, "delta": None, "comment": ""},
            {"criterion": "json_format", "score": 4, "delta": None, "comment": ""},
        ],
        "feedback": "Enum values in English.",
        "patches_applied": [],
        "user_answers": [],
        "model_id": "nova-pro",
    },
    {
        "run_id": "run2",
        "created_at": "2026-09-16T11:00:00Z",
        "avg_score": 3.9,
        "cause": "cr_retry",
        "criteria": [
            {"criterion": "priority_alignment", "score": 4, "delta": 2.0, "comment": ""},
            {"criterion": "json_format", "score": 4, "delta": 0.0, "comment": ""},
        ],
        "feedback": "Better.",
        "patches_applied": ["Add French enum values"],
        "user_answers": [],
        "model_id": "nova-pro",
    },
]

MOCK_LLM_RESPONSE = json.dumps(
    {
        "pain_points": [
            {"criterion": "json_format", "pattern": "Stagnant at 4", "severity": "low"}
        ],
        "suggested_actions": [
            {
                "action": "Add TypedDict schema",
                "targets": ["json_format"],
                "expected_delta": "+0.5",
                "priority": 1,
            }
        ],
        "forecast": {
            "score_range": [3.9, 4.5],
            "confidence_tier": "low",
            "rationale": "Only 2 data points.",
        },
    }
)


def _mock_llm(prompt, cfg, max_tokens=1024):
    return MOCK_LLM_RESPONSE


def test_confidence_tier():
    assert _confidence_tier(0) == "none"
    assert _confidence_tier(1) == "none"
    assert _confidence_tier(2) == "low"
    assert _confidence_tier(3) == "low"
    assert _confidence_tier(4) == "medium"
    assert _confidence_tier(8) == "high"


def test_compute_stats():
    stats = compute_stats(MOCK_TREND_POINTS)
    assert stats["n_points"] == 2
    assert stats["confidence_tier"] == "low"
    assert "priority_alignment" in stats["crit_stats"]


def test_agent_run_success():
    agent = ImprovementAdvisorAgent(llm_fn=_mock_llm)
    events = list(agent.run(agent_name="PriorityClassifier", trend_points=MOCK_TREND_POINTS))
    summary = next(e for e in events if e.get("summary"))
    assert summary["confidence_tier"] == "low"
    assert len(summary["pain_points"]) == 1
    assert len(summary["suggested_actions"]) == 1


def test_agent_no_data():
    agent = ImprovementAdvisorAgent(llm_fn=_mock_llm)
    events = list(agent.run(agent_name="X", trend_points=[]))
    errors = [e for e in events if e.get("level") == "error"]
    assert errors


def test_agent_invalid_llm_response():
    def bad_llm(p, c, m=1024):
        return "not json at all"

    agent = ImprovementAdvisorAgent(llm_fn=bad_llm)
    events = list(agent.run(agent_name="X", trend_points=MOCK_TREND_POINTS))
    errors = [e for e in events if e.get("level") == "error"]
    assert errors
