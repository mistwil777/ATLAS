"""Smoke tests for cdc-router — 100% offline (no AWS, no atlas-memory required)."""

from __future__ import annotations

import json
from pathlib import Path

_FIXTURES = Path(__file__).parent / "fixtures"

_VALID_SEGMENTATION = json.dumps(
    {
        "blocs": [
            {
                "bloc": "classify ticket urgency",
                "type": "ia",
                "agent_suggested": "ticket-classifier",
                "justification": "unstructured natural language, semantic classification",
                "criteria": [
                    "Output must return a classification from: urgent, normal, low.",
                    "Response must reference the ticket content to justify the classification.",
                ],
            },
            {
                "bloc": "save to PostgreSQL",
                "type": "code",
                "agent_suggested": None,
                "justification": "deterministic write to known schema",
                "criteria": [
                    "Output must confirm successful write with row count.",
                    "All required fields must be present before insert.",
                ],
            },
        ]
    }
)


def _mock_llm_ok(prompt: str, cfg: dict, max_tokens: int) -> str:
    return _VALID_SEGMENTATION


def _mock_llm_invalid_then_ok(prompt: str, cfg: dict, max_tokens: int) -> str:
    if not hasattr(_mock_llm_invalid_then_ok, "_called"):
        _mock_llm_invalid_then_ok._called = True
        return "not json at all"
    del _mock_llm_invalid_then_ok._called
    return _VALID_SEGMENTATION


def _mock_llm_always_fail(prompt: str, cfg: dict, max_tokens: int) -> str:
    return "not json"


# ── segmenter tests ───────────────────────────────────────────────────────────


def test_segmenter_with_mock_llm():
    from cdc_router.segmenter import segment

    result, verdict, attempts = segment("Classify support tickets.", [], {}, _mock_llm_ok, {})
    assert result is not None
    assert "blocs" in result
    assert len(result["blocs"]) == 2
    assert result["blocs"][0]["type"] == "ia"
    assert result["blocs"][1]["type"] == "code"
    assert attempts >= 1


def test_segmenter_blocs_have_criteria():
    from cdc_router.segmenter import segment

    result, _verdict, _attempts = segment("Classify support tickets.", [], {}, _mock_llm_ok, {})
    assert result is not None
    for bloc in result["blocs"]:
        assert "criteria" in bloc
        assert isinstance(bloc["criteria"], list)
        assert len(bloc["criteria"]) >= 1


def test_segmenter_invalid_json_reprompt():
    from cdc_router.segmenter import segment

    result, _verdict, _attempts = segment(
        "Classify support tickets.", [], {}, _mock_llm_invalid_then_ok, {}
    )
    assert result is not None
    assert "blocs" in result


def test_segmenter_always_fails_returns_none():
    from cdc_router.segmenter import segment

    result, _verdict, _attempts = segment(
        "Classify support tickets.", [], {}, _mock_llm_always_fail, {}
    )
    assert result is None


# ── router tests ──────────────────────────────────────────────────────────────


def test_router_maps_existing_agent():
    from cdc_router.router import route

    blocs = [
        {"bloc": "classify", "type": "ia", "agent_suggested": "pair-forge", "justification": "x"}
    ]
    kg_graph = {"nodes": [{"id": "pair-forge", "capabilities": [], "tags": []}], "links": []}

    result = route(blocs, kg_graph)
    assert result["blocs"][0]["agent_exists"] is True
    assert "pair-forge" in result["atlas_agents_reused"]
    assert "pair-forge" not in result["requires_creation"]


def test_router_detects_missing_agent():
    from cdc_router.router import route

    blocs = [
        {
            "bloc": "classify",
            "type": "ia",
            "agent_suggested": "ticket-classifier",
            "justification": "x",
        }
    ]
    result = route(blocs, {"nodes": [], "links": []})

    assert result["blocs"][0]["agent_exists"] is False
    assert "ticket-classifier" in result["requires_creation"]
    assert result["atlas_agents_reused"] == []


def test_router_code_bloc_has_no_agent():
    from cdc_router.router import route

    blocs = [{"bloc": "save to db", "type": "code", "agent_suggested": None, "justification": "x"}]
    result = route(blocs, {"nodes": [], "links": []})

    assert result["blocs"][0]["agent_exists"] is False
    assert result["pipeline"] == []
    assert result["requires_creation"] == []


# ── full run() tests ──────────────────────────────────────────────────────────


def test_run_with_mock_llm():
    from cdc_router.agent import CdcRouterAgent

    agent = CdcRouterAgent(llm_fn=_mock_llm_ok)
    events = list(agent.run(cdc_text="Classify support tickets and save to DB."))

    summary = next((e for e in events if e.get("summary")), None)
    assert summary is not None
    assert "blocs" in summary
    assert "pipeline" in summary
    assert "requires_creation" in summary


def test_run_empty_cdc_returns_error():
    from cdc_router.agent import CdcRouterAgent

    agent = CdcRouterAgent(llm_fn=_mock_llm_ok)
    events = list(agent.run(cdc_text=""))

    assert any(e.get("level") == "error" for e in events)
    assert not any(e.get("summary") for e in events)


def test_run_with_cdc_file():
    from cdc_router.agent import CdcRouterAgent

    fixture = _FIXTURES / "sample_cdc.txt"
    agent = CdcRouterAgent(llm_fn=_mock_llm_ok)
    events = list(agent.run(cdc_path=str(fixture)))

    summary = next((e for e in events if e.get("summary")), None)
    assert summary is not None


# ── contract + prompts tests ──────────────────────────────────────────────────


def test_check_dependencies():
    from cdc_router.tools import check_dependencies

    result = check_dependencies()
    assert result["missing"] == []
    assert result["deprecated"] == []
    assert result["untested_versions"] == []


def test_prompts_contain_binary_rules():
    from cdc_router.prompts import SEGMENTATION_PROMPT

    assert (
        '"ia"' in SEGMENTATION_PROMPT
        or "'ia'" in SEGMENTATION_PROMPT
        or "ia" in SEGMENTATION_PROMPT
    )
    assert (
        '"code"' in SEGMENTATION_PROMPT
        or "'code'" in SEGMENTATION_PROMPT
        or "code" in SEGMENTATION_PROMPT
    )
    assert "deterministic" in SEGMENTATION_PROMPT
    assert "unstructured" in SEGMENTATION_PROMPT
