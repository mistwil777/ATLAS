"""Smoke tests — RunStore persistence and HITL review flow."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas_memory.run_store import RunStore


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs")


def test_create_run(store: RunStore) -> None:
    run_id = store.create_run()
    assert isinstance(run_id, str) and len(run_id) == 36
    run = store.get_run(run_id)
    assert run is not None
    assert run["status"] == "pending"
    assert run["requires_review"] is False
    assert run["blocs"] == []


def test_append_bloc_no_review(store: RunStore) -> None:
    run_id = store.create_run()
    store.append_bloc(
        run_id, {"agent": "agent-forge", "judge_passed": True, "requires_review": False}
    )
    run = store.get_run(run_id)
    assert len(run["blocs"]) == 1
    assert run["requires_review"] is False


def test_append_bloc_triggers_review(store: RunStore) -> None:
    run_id = store.create_run()
    store.append_bloc(
        run_id, {"agent": "agent-forge", "judge_passed": False, "requires_review": True}
    )
    run = store.get_run(run_id)
    assert run["requires_review"] is True


def test_approve(store: RunStore) -> None:
    run_id = store.create_run()
    store.set_review_decision(run_id, "approved")
    assert store.get_run(run_id)["status"] == "approved"


def test_reject(store: RunStore) -> None:
    run_id = store.create_run()
    store.set_review_decision(run_id, "rejected")
    assert store.get_run(run_id)["status"] == "rejected"


def test_list_runs(store: RunStore) -> None:
    store.create_run()
    id2 = store.create_run()
    store.append_bloc(id2, {"requires_review": True})
    all_runs = store.list_runs()
    assert len(all_runs) == 2
    review_runs = store.list_runs(only_review=True)
    assert len(review_runs) == 1
    assert review_runs[0]["run_id"] == id2


def test_invalid_decision(store: RunStore) -> None:
    run_id = store.create_run()
    with pytest.raises(ValueError):
        store.set_review_decision(run_id, "maybe")


# ── waiting_for_client flow ───────────────────────────────────────────────────


def test_set_pending_questions(store: RunStore) -> None:
    run_id = store.create_run()
    questions = [
        {"id": "q_001", "agent": "MyAgent", "criterion": "output_language", "question": "Q?"}
    ]
    store.set_pending_questions(run_id, "MyAgent", questions)
    run = store.get_run(run_id)
    assert run["status"] == "waiting_for_client"
    assert run["pending_agent"] == "MyAgent"
    assert len(run["pending_questions"]) == 1
    assert run["pending_questions"][0]["id"] == "q_001"


def test_save_agent_state_merges(store: RunStore) -> None:
    run_id = store.create_run()
    store.save_agent_state(run_id, "MyAgent", {"cr_context": "ctx1", "attempts_log": []})
    store.save_agent_state(run_id, "MyAgent", {"user_answers": ["yes"]})
    run = store.get_run(run_id)
    state = run["agent_state"]["MyAgent"]
    assert state["cr_context"] == "ctx1"
    assert state["user_answers"] == ["yes"]


def test_set_client_answers_resumes(store: RunStore) -> None:
    run_id = store.create_run()
    store.set_pending_questions(run_id, "MyAgent", [])
    assert store.get_run(run_id)["status"] == "waiting_for_client"
    store.set_client_answers(run_id, {"output_language": "fr"})
    run = store.get_run(run_id)
    assert run["status"] == "scaffolding"
    assert run["client_answers"]["output_language"] == "fr"


def test_set_client_answers_accumulates(store: RunStore) -> None:
    run_id = store.create_run()
    store.set_client_answers(run_id, {"output_language": "fr"})
    store.set_client_answers(run_id, {"priority_labels": "haute/normale"})
    run = store.get_run(run_id)
    assert run["client_answers"]["output_language"] == "fr"
    assert run["client_answers"]["priority_labels"] == "haute/normale"
