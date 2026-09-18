"""Unit tests for consolidated context and question generation — no LLM, no network."""

from __future__ import annotations

import json

from atlas_dashboard.app import (
    _build_consolidated_context,
    _format_consolidated_context_for_prompt,
    _generate_questions,
)

# ── _build_consolidated_context ───────────────────────────────────────────────

_ATTEMPTS_LOG = [
    {
        "attempt": 1,
        "criteria_scores": [
            {"criterion": "output_language", "score": 1, "cause": "missing_spec"},
            {"criterion": "binary_rules", "score": 2, "cause": "model_error"},
            {"criterion": "json_schema", "score": 5, "cause": None},
        ],
    },
    {
        "attempt": 2,
        "criteria_scores": [
            {"criterion": "output_language", "score": 1, "cause": "missing_spec"},
            {"criterion": "binary_rules", "score": 3, "cause": "model_error"},
        ],
    },
]


def test_build_consolidated_context_deduplicates():
    ctx = _build_consolidated_context("MyAgent", _ATTEMPTS_LOG)
    hist = ctx["failed_criteria_history"]
    assert "output_language" in hist
    assert hist["output_language"]["occurrences"] == 2
    assert hist["output_language"]["last_cause"] == "missing_spec"
    assert "binary_rules" in hist
    assert hist["binary_rules"]["occurrences"] == 2
    assert "json_schema" not in hist  # score=5, should not appear


def test_build_consolidated_context_open_issues_cause():
    ctx = _build_consolidated_context("MyAgent", _ATTEMPTS_LOG)
    issues = {i["criterion"]: i for i in ctx["open_issues"]}
    assert issues["output_language"]["cause"] == "missing_spec"
    assert issues["binary_rules"]["cause"] == "model_error"


def test_build_consolidated_context_client_answers_forwarded():
    answers = {"output_language": "fr"}
    ctx = _build_consolidated_context("MyAgent", _ATTEMPTS_LOG, client_answers=answers)
    assert ctx["client_answers"] == answers


def test_build_consolidated_context_empty_log():
    ctx = _build_consolidated_context("MyAgent", [])
    assert ctx["open_issues"] == []
    assert ctx["failed_criteria_history"] == {}


# ── _format_consolidated_context_for_prompt ───────────────────────────────────


def test_format_separates_model_error_and_missing_spec():
    ctx = _build_consolidated_context("MyAgent", _ATTEMPTS_LOG)
    text = _format_consolidated_context_for_prompt(ctx)
    assert "Criteria to fix" in text
    assert "binary_rules" in text
    assert "Behaviors not specified" in text
    assert "output_language" in text


def test_format_includes_client_answers():
    answers = {"output_language": "fr"}
    ctx = _build_consolidated_context("MyAgent", _ATTEMPTS_LOG, client_answers=answers)
    text = _format_consolidated_context_for_prompt(ctx)
    assert "Client decisions" in text
    assert "fr" in text


def test_format_empty_returns_empty_string():
    ctx = _build_consolidated_context("MyAgent", [])
    assert _format_consolidated_context_for_prompt(ctx) == ""


def test_format_no_duplicate_criteria():
    """Same criterion failing 3 times must appear only once in the formatted output."""
    log = [
        {
            "attempt": i,
            "criteria_scores": [{"criterion": "foo", "score": 1, "cause": "model_error"}],
        }
        for i in range(1, 4)
    ]
    ctx = _build_consolidated_context("A", log)
    text = _format_consolidated_context_for_prompt(ctx)
    assert text.count("foo") == 1


# ── _generate_questions ───────────────────────────────────────────────────────


def _mock_llm(response: str):
    def _fn(prompt, cfg, max_tokens):
        return response

    return _fn


_MOCK_QUESTIONS_RESPONSE = json.dumps(
    [
        {
            "criterion": "output_language",
            "question": "Pour garantir la cohérence avec vos conventions, quels labels votre équipe utilise-t-elle ?",
            "question_type": "text",
            "hint": "ex: critique / haute / normale / basse",
            "options": [],
        },
        {
            "criterion": "empty_ticket_behavior",
            "question": "Comment souhaitez-vous traiter un ticket vide ou illisible ?",
            "question_type": "single_choice",
            "hint": "Classer en basse / Rejeter / Escalader",
            "options": ["Classer en basse", "Rejeter", "Escalader"],
        },
    ]
)


def test_generate_questions_structure():
    issues = [
        {"criterion": "output_language", "occurrences": 2},
        {"criterion": "empty_ticket_behavior", "occurrences": 1},
    ]
    qs = _generate_questions(
        issues, "PriorityClassifier", "CDC text", _mock_llm(_MOCK_QUESTIONS_RESPONSE), {}
    )
    assert len(qs) == 2
    q0 = qs[0]
    assert q0["id"] == "q_001"
    assert q0["agent"] == "PriorityClassifier"
    assert q0["cause"] == "missing_spec"
    assert q0["question_type"] == "text"
    assert "created_at" in q0


def test_generate_questions_single_choice_has_options():
    issues = [{"criterion": "empty_ticket_behavior", "occurrences": 1}]
    qs = _generate_questions(
        issues, "PriorityClassifier", "CDC", _mock_llm(_MOCK_QUESTIONS_RESPONSE), {}
    )
    sc = next((q for q in qs if q["question_type"] == "single_choice"), None)
    assert sc is not None
    assert len(sc["options"]) == 3


def test_generate_questions_max_respected():
    """Even if LLM returns more, max_questions caps the output."""
    big_response = json.dumps(
        [
            {"criterion": f"c{i}", "question": f"Q{i}?", "question_type": "text", "hint": ""}
            for i in range(10)
        ]
    )
    issues = [{"criterion": f"c{i}", "occurrences": 1} for i in range(10)]
    qs = _generate_questions(issues, "A", "CDC", _mock_llm(big_response), {}, max_questions=3)
    assert len(qs) <= 3


def test_generate_questions_empty_issues_returns_empty():
    qs = _generate_questions([], "A", "CDC", _mock_llm(_MOCK_QUESTIONS_RESPONSE), {})
    assert qs == []


def test_generate_questions_invalid_llm_response_returns_empty():
    qs = _generate_questions(
        [{"criterion": "foo", "occurrences": 1}], "A", "CDC", _mock_llm("not json at all"), {}
    )
    assert qs == []
