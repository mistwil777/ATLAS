"""Smoke tests for atlas-coherence."""

from __future__ import annotations

import json

BLOCS_CLEAN = [
    {
        "agent_suggested": "PriorityClassifier",
        "type": "ia",
        "bloc": "Classifies the priority level (critical/high/medium/low) of a support ticket.",
        "justification": "Requires semantic interpretation of ticket content.",
    },
    {
        "agent_suggested": "CategoryClassifier",
        "type": "ia",
        "bloc": "Classifies the category (billing/technical/account) of a support ticket.",
        "justification": "Requires semantic interpretation of ticket content.",
    },
]

BLOCS_FULL_OVERLAP = [
    {
        "agent_suggested": "PriorityClassifier",
        "type": "ia",
        "bloc": "Classifies the priority level of a support ticket.",
        "justification": "Requires semantic interpretation.",
    },
    {
        "agent_suggested": "TicketPrioritizer",
        "type": "ia",
        "bloc": "Assigns a priority score to each support ticket.",
        "justification": "Requires semantic interpretation.",
    },
]

BLOCS_PARTIAL_OVERLAP = [
    {
        "agent_suggested": "PriorityClassifier",
        "type": "ia",
        "bloc": "Classifies the priority and category of a support ticket. Output: {priority, category, confidence}.",
        "justification": "Requires semantic interpretation.",
    },
    {
        "agent_suggested": "CategoryClassifier",
        "type": "ia",
        "bloc": "Classifies the category of a support ticket. Output: {category}.",
        "justification": "Requires semantic interpretation.",
    },
]


def _make_llm(response: dict):
    def llm_fn(prompt: str, cfg: dict, max_tokens: int = 2048) -> str:
        return json.dumps(response)

    return llm_fn


def test_clean_blocs_returns_clean():
    from atlas_coherence.agent import AtlasCoherenceAgent

    agent = AtlasCoherenceAgent(
        llm_fn=_make_llm({"clean": True, "conflicts": []})
    )
    events = list(agent.run(blocs=BLOCS_CLEAN))
    summary = next(e for e in events if e.get("summary"))
    assert summary["clean"] is True
    assert summary["conflicts"] == []


def test_full_overlap_detected():
    from atlas_coherence.agent import AtlasCoherenceAgent

    conflict = {
        "type": "full",
        "primary": "PriorityClassifier",
        "redundant": "TicketPrioritizer",
        "issue": "Both agents classify ticket priority.",
        "fix": "Remove TicketPrioritizer — PriorityClassifier owns this responsibility.",
    }
    agent = AtlasCoherenceAgent(
        llm_fn=_make_llm({"clean": False, "conflicts": [conflict]})
    )
    events = list(agent.run(blocs=BLOCS_FULL_OVERLAP))
    summary = next(e for e in events if e.get("summary"))
    assert summary["clean"] is False
    assert len(summary["conflicts"]) == 1
    assert summary["conflicts"][0]["type"] == "full"


def test_partial_overlap_detected():
    from atlas_coherence.agent import AtlasCoherenceAgent

    conflict = {
        "type": "partial",
        "primary": "CategoryClassifier",
        "redundant": "PriorityClassifier",
        "issue": "PriorityClassifier outputs 'category', which is CategoryClassifier's sole responsibility.",
        "fix": "Remove 'category' from PriorityClassifier's output.",
    }
    agent = AtlasCoherenceAgent(
        llm_fn=_make_llm({"clean": False, "conflicts": [conflict]})
    )
    events = list(agent.run(blocs=BLOCS_PARTIAL_OVERLAP))
    summary = next(e for e in events if e.get("summary"))
    assert summary["clean"] is False
    assert summary["conflicts"][0]["type"] == "partial"


def test_single_bloc_skips_llm():
    called = []

    def llm_fn(prompt, cfg, max_tokens=2048):
        called.append(prompt)
        return None

    from atlas_coherence.agent import AtlasCoherenceAgent

    agent = AtlasCoherenceAgent(llm_fn=llm_fn)
    events = list(agent.run(blocs=[BLOCS_CLEAN[0]]))
    summary = next(e for e in events if e.get("summary"))
    assert summary["clean"] is True
    assert called == []
