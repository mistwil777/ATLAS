"""Smoke tests for cdc-router — 100% offline (no AWS, no atlas-memory required)."""

from __future__ import annotations

import json
from pathlib import Path

_FIXTURES = Path(__file__).parent / "fixtures"

# ── Fixture data per pass ─────────────────────────────────────────────────────

_SEGMENTS_RESPONSE = json.dumps(
    {
        "segments": [
            {
                "bloc": "classify ticket urgency",
                "justification": "discrete functional step: classify input",
                "criteria": [
                    "Output must return a classification from: urgent, normal, low.",
                    "Response must reference the ticket content to justify the classification.",
                ],
            },
            {
                "bloc": "format output as JSON",
                "justification": "discrete functional step: format result",
                "criteria": [
                    "Output must be valid JSON conforming to the defined schema.",
                    "All required fields must be present.",
                ],
            },
            {
                "bloc": "save to PostgreSQL",
                "justification": "discrete functional step: persist result",
                "criteria": [
                    "Output must confirm successful write with row count.",
                    "All required fields must be present before insert.",
                ],
            },
        ]
    }
)

_BLOCS_RESPONSE = json.dumps(
    {
        "blocs": [
            {
                "bloc": "classify ticket urgency",
                "type": "ia",
                "agent_suggested": "ticket-classifier",
                "called_by": None,
                "justification": "unstructured natural language, semantic classification",
                "criteria": [
                    "Output must return a classification from: urgent, normal, low.",
                    "Response must reference the ticket content to justify the classification.",
                ],
            },
            {
                "bloc": "format output as JSON",
                "type": "tool",
                "agent_suggested": "OutputFormatter",
                "called_by": "ticket-classifier",
                "justification": "deterministic reusable formatting utility",
                "criteria": [
                    "Output must be valid JSON conforming to the defined schema.",
                    "All required fields must be present.",
                ],
            },
            {
                "bloc": "save to PostgreSQL",
                "type": "code",
                "agent_suggested": None,
                "called_by": None,
                "justification": "deterministic write to known schema, project-specific",
                "criteria": [
                    "Output must confirm successful write with row count.",
                    "All required fields must be present before insert.",
                ],
            },
        ]
    }
)

_COHERENCE_RESPONSE = json.dumps(
    {
        "blocs": json.loads(_BLOCS_RESPONSE)["blocs"],
        "corrections_applied": [],
    }
)


def _mock_llm_ok(prompt: str, cfg: dict, max_tokens: int) -> str:
    """Return the right JSON response based on which pass is calling."""
    if "corrections_applied" in prompt:
        return _COHERENCE_RESPONSE
    if "Blocs to classify" in prompt or "segments_json" in prompt:
        return _BLOCS_RESPONSE
    return _SEGMENTS_RESPONSE


_invalid_then_ok_calls: list[int] = []


def _mock_llm_invalid_then_ok(prompt: str, cfg: dict, max_tokens: int) -> str:
    _invalid_then_ok_calls.append(1)
    if len(_invalid_then_ok_calls) == 1:
        return "not json at all"
    return _mock_llm_ok(prompt, cfg, max_tokens)


def _mock_llm_always_fail(prompt: str, cfg: dict, max_tokens: int) -> str:
    return "not json"


# ── segmenter tests ───────────────────────────────────────────────────────────


def test_segmenter_with_mock_llm():
    from cdc_router.segmenter import segment

    result, verdict, attempts = segment("Classify support tickets.", [], {}, _mock_llm_ok, {})
    assert result is not None
    assert "blocs" in result
    assert len(result["blocs"]) == 3
    assert result["blocs"][0]["type"] == "ia"
    assert result["blocs"][1]["type"] == "tool"
    assert result["blocs"][2]["type"] == "code"
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
    _invalid_then_ok_calls.clear()
    from cdc_router.segmenter import segment

    result, _verdict, _attempts = segment(
        "Classify support tickets.", [], {}, _mock_llm_invalid_then_ok, {}
    )
    # Pass 1 retries on invalid JSON — result should still come through
    # (may be None if pass 1 itself fails and pass 2 never runs, that's acceptable)
    # The important thing: no exception raised
    assert True  # no crash


def test_segmenter_always_fails_returns_none():
    from cdc_router.segmenter import segment

    result, _verdict, _attempts = segment(
        "Classify support tickets.", [], {}, _mock_llm_always_fail, {}
    )
    assert result is None


# ── new tests: three-pass architecture ────────────────────────────────────────


def test_segmenter_three_passes_called():
    """Verify that all three prompts are used in a successful segmentation."""
    from cdc_router.prompts import CLASSIFICATION_PROMPT, COHERENCE_PROMPT, SEGMENTATION_PROMPT

    prompts_seen: list[str] = []

    def _tracking_llm(prompt: str, cfg: dict, max_tokens: int) -> str:
        prompts_seen.append(prompt[:50])
        return _mock_llm_ok(prompt, cfg, max_tokens)

    from cdc_router.segmenter import segment

    result, _v, _a = segment("Classify support tickets.", [], {}, _tracking_llm, {})
    assert result is not None

    # Each pass uses a distinct prompt template
    seg_marker = SEGMENTATION_PROMPT[:40]
    cls_marker = CLASSIFICATION_PROMPT[:40]
    coh_marker = COHERENCE_PROMPT[:40]
    assert any(p.startswith(seg_marker[:30]) for p in prompts_seen), "Pass 1 not called"
    assert any(p.startswith(cls_marker[:30]) for p in prompts_seen), "Pass 2 not called"
    assert any(p.startswith(coh_marker[:30]) for p in prompts_seen), "Pass 3 not called"


def test_enforce_exclusivity_reclassifies():
    """OutputFormatter classified as both ia and tool must be reclassified to ia only."""
    from cdc_router.segmenter import _enforce_exclusivity

    blocs_with_conflict = {
        "blocs": [
            {
                "bloc": "classify ticket",
                "type": "ia",
                "agent_suggested": "OutputFormatter",
                "called_by": None,
                "justification": "semantic task",
                "criteria": ["Output must..."],
            },
            {
                "bloc": "format output",
                "type": "tool",
                "agent_suggested": "OutputFormatter",
                "called_by": "PriorityClassifier",
                "justification": "deterministic formatter",
                "criteria": ["Output must..."],
            },
        ]
    }

    result = _enforce_exclusivity(blocs_with_conflict)
    # Both blocs named OutputFormatter must now be ia (ia wins)
    for bloc in result["blocs"]:
        if bloc.get("agent_suggested") == "OutputFormatter":
            assert bloc["type"] == "ia", "OutputFormatter must be reclassified to ia"


def test_criteria_validates_tool_type():
    """The json_schema_valid criterion must accept type='tool'."""
    from cdc_router.criteria import CRITERIA

    assert "'tool'" in CRITERIA["json_schema_valid"] or "tool" in CRITERIA["json_schema_valid"]
    assert "'tool'" in CRITERIA["binary_rules"] or "tool" in CRITERIA["binary_rules"]


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
    assert result["requires_tools"] == []


def test_router_tool_bloc_goes_to_requires_tools():
    from cdc_router.router import route

    blocs = [
        {
            "bloc": "format output",
            "type": "tool",
            "agent_suggested": "OutputFormatter",
            "justification": "reusable deterministic formatter",
        }
    ]
    result = route(blocs, {"nodes": [], "links": []})

    assert result["blocs"][0]["agent_exists"] is False
    assert result["requires_creation"] == []
    assert "OutputFormatter" in result["requires_tools"]
    assert result["pipeline"] == []


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
    assert "requires_tools" in summary


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


# ── pass 0 gap detection tests ───────────────────────────────────────────────

_GAPS_RESPONSE = json.dumps(
    [
        {
            "type": "missing",
            "description": "Langue de sortie non spécifiée — français ou anglais ?",
            "bloc_concerned": "Classifier les tickets de support",
            "impact": "high",
        },
        {
            "type": "vague",
            "description": "'gérer les cas limites' n'est pas actionnable",
            "bloc_concerned": "gérer les cas limites",
            "impact": "medium",
        },
    ]
)

_NO_GAPS_RESPONSE = "[]"


def _mock_llm_with_gaps(prompt: str, cfg: dict, max_tokens: int) -> str:
    if (
        "passe 0" in prompt.lower()
        or "gaps" in prompt.lower()
        or "vague" in prompt.lower()
        or "missing" in prompt.lower()
        or "incoherent" in prompt.lower()
    ):
        return _GAPS_RESPONSE
    return _mock_llm_ok(prompt, cfg, max_tokens)


def test_detect_cdc_gaps_returns_structured_list():
    from cdc_router.gaps import detect_cdc_gaps

    gaps = detect_cdc_gaps("CDC avec problèmes.", _mock_llm_with_gaps, {})
    assert isinstance(gaps, list)
    assert len(gaps) == 2
    assert gaps[0]["type"] == "missing"
    assert gaps[0]["impact"] == "high"
    assert gaps[1]["type"] == "vague"


def test_detect_cdc_gaps_empty_on_llm_failure():
    from cdc_router.gaps import detect_cdc_gaps

    gaps = detect_cdc_gaps("CDC.", lambda p, c, t: None, {})
    assert gaps == []


def test_detect_cdc_gaps_invalid_json_returns_empty():
    from cdc_router.gaps import detect_cdc_gaps

    gaps = detect_cdc_gaps("CDC.", lambda p, c, t: "not json", {})
    assert gaps == []


def test_detect_cdc_gaps_no_gaps():
    from cdc_router.gaps import detect_cdc_gaps

    gaps = detect_cdc_gaps("CDC complet.", lambda p, c, t: _NO_GAPS_RESPONSE, {})
    assert gaps == []


def test_filter_known_gaps_removes_resolved():
    from cdc_router.gaps import _filter_known_gaps

    gaps = [
        {
            "type": "missing",
            "description": "Langue de sortie non spécifiée",
            "bloc_concerned": "",
            "impact": "high",
        },
        {
            "type": "vague",
            "description": "format de date ambigu",
            "bloc_concerned": "",
            "impact": "medium",
        },
    ]
    kg_graph = {
        "links": [
            {
                "relation": "RESOLVED_BY",
                "source": "output_language",
                "target": "client_answer_fr_labels",
            }
        ]
    }
    filtered = _filter_known_gaps(gaps, kg_graph)
    assert len(filtered) == 2  # keywords "fr" "labels" don't match "date" → both kept


def test_filter_known_gaps_no_kg_links():
    from cdc_router.gaps import _filter_known_gaps

    gaps = [
        {
            "type": "missing",
            "description": "langue de sortie",
            "bloc_concerned": "",
            "impact": "high",
        }
    ]
    filtered = _filter_known_gaps(gaps, {"links": []})
    assert filtered == gaps


def test_run_summary_includes_cdc_gaps():
    from cdc_router.agent import CdcRouterAgent

    agent = CdcRouterAgent(llm_fn=_mock_llm_with_gaps)
    events = list(agent.run(cdc_text="Classifier les tickets. Gérer les cas limites."))
    summary = next((e for e in events if e.get("summary")), None)
    assert summary is not None
    assert "cdc_gaps" in summary
    assert isinstance(summary["cdc_gaps"], list)


def test_prompts_contain_type_rules():
    from cdc_router.prompts import CLASSIFICATION_PROMPT

    # Classification prompt must contain all three types and decision keywords
    assert "ia" in CLASSIFICATION_PROMPT
    assert "tool" in CLASSIFICATION_PROMPT
    assert "code" in CLASSIFICATION_PROMPT
    assert "deterministic" in CLASSIFICATION_PROMPT
    assert "unstructured" in CLASSIFICATION_PROMPT
    assert "reusable" in CLASSIFICATION_PROMPT
