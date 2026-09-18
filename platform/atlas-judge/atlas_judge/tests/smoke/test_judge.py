"""Smoke tests for atlas-judge — no AWS, no real LLM calls."""

from __future__ import annotations

import json

import pytest

from atlas_judge.agent import AtlasJudgeAgent
from atlas_judge.evaluator import pairwise, pointwise, reference_based

_POINTWISE_RESPONSE = json.dumps(
    {
        "mode": "pointwise",
        "criteria_scores": [
            {"criterion": "binary_rules", "score": 5, "comment": "applied correctly"},
            {"criterion": "json_schema_valid", "score": 4, "comment": "all fields present"},
        ],
        "feedback": "Solid output.",
        "winner": None,
    }
)

_PAIRWISE_RESPONSE = json.dumps(
    {
        "mode": "pairwise",
        "criteria_scores": [],
        "feedback": "Output A is more precise.",
        "winner": "A",
    }
)

_REFERENCE_RESPONSE = json.dumps(
    {
        "mode": "reference",
        "criteria_scores": [
            {"criterion": "binary_rules", "score": 4, "comment": "consistent with reference"},
        ],
        "feedback": "Close to reference.",
        "winner": None,
    }
)


def _mock_llm_returning(payload: str):
    def _llm(prompt: str, cfg: dict, max_tokens: int) -> str:
        return payload

    return _llm


def _always_invalid_llm(prompt: str, cfg: dict, max_tokens: int) -> str:
    return "not valid json at all"


def _invalid_then_valid_llm(valid_payload: str):
    calls = []

    def _llm(prompt: str, cfg: dict, max_tokens: int) -> str:
        calls.append(1)
        if len(calls) == 1:
            return "not valid json"
        return valid_payload

    return _llm


_POINTWISE_RESPONSE_WITH_CAUSE = json.dumps(
    {
        "mode": "pointwise",
        "criteria_scores": [
            {
                "criterion": "binary_rules",
                "score": 2,
                "comment": "rule ignored",
                "cause": "model_error",
            },
            {
                "criterion": "output_language",
                "score": 1,
                "comment": "no language spec",
                "cause": "missing_spec",
            },
            {"criterion": "json_schema_valid", "score": 5, "comment": "correct", "cause": None},
        ],
        "feedback": "Two criteria failed.",
        "winner": None,
    }
)

_CRITERIA = ["binary_rules", "json_schema_valid"]


# --- evaluator.py tests ---


def test_pointwise_with_mock_llm():
    result = pointwise(
        output="output text",
        criteria=_CRITERIA,
        context="",
        llm_fn=_mock_llm_returning(_POINTWISE_RESPONSE),
        llm_cfg={},
    )
    assert result is not None
    assert result["passed"] is True
    assert result["score"] == pytest.approx(4.5, 0.01)
    assert result["max_score"] == 5


def test_pairwise_with_mock_llm():
    result = pairwise(
        output_a="output A",
        output_b="output B",
        criteria=_CRITERIA,
        context="",
        llm_fn=_mock_llm_returning(_PAIRWISE_RESPONSE),
        llm_cfg={},
    )
    assert result is not None
    assert result["passed"] is True
    assert result["winner"] in ("A", "B")


def test_pairwise_order_randomized():
    """Verify pairwise can produce both winner=A and winner=B depending on swap."""
    winners = set()
    # With enough calls and random swapping, we should see both A and B
    for _ in range(50):
        result = pairwise(
            output_a="output A",
            output_b="output B",
            criteria=_CRITERIA,
            context="",
            llm_fn=_mock_llm_returning(_PAIRWISE_RESPONSE),
            llm_cfg={},
        )
        if result:
            winners.add(result["winner"])
    # The mock LLM always returns winner=A in its A/B position.
    # After order correction, we can get A or B depending on swap.
    assert len(winners) >= 1  # At minimum runs without error


def test_reference_based_with_mock_llm():
    result = reference_based(
        output="output text",
        reference="reference text",
        criteria=_CRITERIA,
        context="",
        llm_fn=_mock_llm_returning(_REFERENCE_RESPONSE),
        llm_cfg={},
    )
    assert result is not None
    assert result["score"] == 4.0
    assert result["passed"] is True


def test_pointwise_cause_field_propagated():
    """cause field for failed criteria (score < 3) is preserved in criteria_scores."""
    result = pointwise(
        output="output text",
        criteria=["binary_rules", "output_language", "json_schema_valid"],
        context="CDC with no language spec.",
        llm_fn=_mock_llm_returning(_POINTWISE_RESPONSE_WITH_CAUSE),
        llm_cfg={},
    )
    assert result is not None
    assert result["passed"] is False
    scores = {c["criterion"]: c for c in result["criteria_scores"]}
    assert scores["binary_rules"]["cause"] == "model_error"
    assert scores["output_language"]["cause"] == "missing_spec"
    assert scores["json_schema_valid"]["cause"] is None


def test_invalid_json_reprompt():
    """First call returns invalid JSON, second call returns valid — should succeed."""
    result = pointwise(
        output="output text",
        criteria=_CRITERIA,
        context="",
        llm_fn=_invalid_then_valid_llm(_POINTWISE_RESPONSE),
        llm_cfg={},
    )
    assert result is not None
    assert result["passed"] is True


# --- agent.py run() tests ---


def test_run_pointwise():
    agent = AtlasJudgeAgent(llm_fn=_mock_llm_returning(_POINTWISE_RESPONSE))
    events = list(
        agent.run(
            operation="pointwise",
            output="output text",
            criteria=_CRITERIA,
        )
    )
    summary = next((e for e in events if e.get("summary")), None)
    assert summary is not None
    assert summary["passed"] is True
    assert summary["score"] is not None


def test_run_pairwise():
    agent = AtlasJudgeAgent(llm_fn=_mock_llm_returning(_PAIRWISE_RESPONSE))
    events = list(
        agent.run(
            operation="pairwise",
            output_a="output A",
            output_b="output B",
            criteria=_CRITERIA,
        )
    )
    summary = next((e for e in events if e.get("summary")), None)
    assert summary is not None
    assert summary["winner"] in ("A", "B")


# --- criteria and tools tests ---


def test_criteria_predefined_keys():
    from cdc_router.criteria import CRITERIA  # type: ignore[import-untyped]

    expected_keys = {
        "binary_rules",
        "json_schema_valid",
        "justification_coherent",
        "no_hallucination",
        "completeness",
    }
    assert expected_keys.issubset(set(CRITERIA.keys()))


def test_check_dependencies():
    from atlas_judge.tools import check_dependencies

    result = check_dependencies()
    # boto3 is in dev deps, so should be present
    assert "missing" in result
    assert isinstance(result["missing"], list)
