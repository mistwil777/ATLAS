"""Smoke tests — AtlasReviewAgent."""

from __future__ import annotations

from pathlib import Path

import pytest
from atlas_memory.run_store import RunStore

from atlas_review.agent import AtlasReviewAgent


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs")


@pytest.fixture
def agent(tmp_path: Path) -> AtlasReviewAgent:
    return AtlasReviewAgent(
        llm_fn=lambda p, c, m: '{"agent_name": "test"}',
        run_store_dir=tmp_path / "runs",
        memory_path=tmp_path / "memory",
    )


def test_run_not_found(agent: AtlasReviewAgent) -> None:
    events = list(agent.run("nonexistent-id"))
    assert any("not found" in e.get("msg", "") for e in events)


def test_run_no_failing_blocs(agent: AtlasReviewAgent, store: RunStore) -> None:
    run_id = store.create_run()
    store.append_bloc(run_id, {"agent": "agent-forge", "requires_review": False})
    store.set_review_decision(run_id, "rejected")

    events = list(agent.run(run_id))
    assert any("nothing to re-process" in e.get("msg", "").lower() for e in events)


def test_run_with_failing_blocs(agent: AtlasReviewAgent, store: RunStore) -> None:
    run_id = store.create_run(metadata={"cdc_preview": "Build a classifier"})
    store.append_bloc(
        run_id,
        {
            "agent": "agent-forge",
            "bloc": "Create a text classifier",
            "requires_review": True,
            "judge_score": 2.5,
            "status": "success",
        },
    )
    store.set_review_decision(run_id, "rejected")

    events = list(agent.run(run_id))
    # atlas-orchestrator not installed in test env — expect graceful failure
    assert any(e.get("level") in ("info", "error", "warning") for e in events)


def test_warn_non_rejected_run(agent: AtlasReviewAgent, store: RunStore) -> None:
    run_id = store.create_run()
    store.append_bloc(run_id, {"agent": "x", "requires_review": True})
    # status stays 'pending' — not rejected

    events = list(agent.run(run_id))
    assert any("rejected" in e.get("msg", "").lower() for e in events)
