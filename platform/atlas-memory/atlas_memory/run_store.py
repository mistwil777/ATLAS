"""Run store — persists ATLAS execution runs and HITL review state."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RunStore:
    """File-backed store for ATLAS pipeline runs.

    Each run is a JSON file under <store_dir>/<run_id>.json.
    Schema:
      run_id, created_at, status (pending|approved|rejected),
      requires_review (bool), blocs (list of bloc results with judge info)
    """

    def __init__(self, store_dir: Path) -> None:
        self._dir = store_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── write ──────────────────────────────────────────────────────────────────

    def create_run(self, metadata: dict[str, Any] | None = None) -> str:
        run_id = str(uuid.uuid4())
        run = {
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
            "requires_review": False,
            "requires_creation": [],
            "requires_tools": [],
            "scaffolded": {},
            "tools_built": {},
            "blocs": [],
            "metadata": metadata or {},
        }
        self._write(run_id, run)
        return run_id

    def append_bloc(self, run_id: str, bloc_result: dict[str, Any]) -> None:
        run = self._read(run_id)
        run["blocs"].append(bloc_result)
        if bloc_result.get("requires_review"):
            run["requires_review"] = True
        self._write(run_id, run)

    def set_requires_creation(self, run_id: str, agents: list[str]) -> None:
        run = self._read(run_id)
        run["requires_creation"] = agents
        self._write(run_id, run)

    def set_requires_tools(self, run_id: str, tools: list[str]) -> None:
        run = self._read(run_id)
        run["requires_tools"] = tools
        self._write(run_id, run)

    def set_tool_installed(self, run_id: str, tool_name: str) -> None:
        run = self._read(run_id)
        run.setdefault("tools_built", {}).setdefault(tool_name, {})["installed"] = True
        self._write(run_id, run)

    def set_tool_result(
        self,
        run_id: str,
        tool_name: str,
        output_path: str,
        spec: dict[str, Any],
        files: dict[str, str] | None = None,
    ) -> None:
        run = self._read(run_id)
        run.setdefault("tools_built", {})[tool_name] = {
            "output_path": output_path,
            "module_name": spec.get("module_name", ""),
            "functions": spec.get("functions", []),
            "description": spec.get("description", ""),
            "installed": False,
            "files": files or {},
        }
        self._write(run_id, run)

    def set_scaffold_result(
        self,
        run_id: str,
        agent_name: str,
        output_path: str,
        spec: dict[str, Any],
        user_answers: list[str] | None = None,
        selected_patches: list[str] | None = None,
        model_id: str | None = None,
        judge_score: float | None = None,
        judge_criteria: list[dict[str, Any]] | None = None,
        judge_feedback: str | None = None,
        files: dict[str, str] | None = None,
    ) -> None:
        run = self._read(run_id)
        run.setdefault("scaffolded", {})[agent_name] = {
            "output_path": output_path,
            "installed": False,
            "spec": spec,
            "user_answers": user_answers or [],
            "selected_patches": selected_patches or [],
            "model_id": model_id or "",
            "judge_score": judge_score,
            "judge_criteria": judge_criteria or [],
            "judge_feedback": judge_feedback or "",
            "files": files or {},
        }
        # Clear any stale diagnosis so the UI shows the new result, not the old failure
        if judge_score is not None:
            run.get("scaffold_diagnosis", {}).pop(agent_name, None)
        self._write(run_id, run)

    def add_scaffold_attempt(
        self,
        run_id: str,
        agent_name: str,
        attempt: int,
        judge_score: float | None,
        judge_criteria: list[dict[str, Any]],
        judge_feedback: str,
        model_id: str = "",
        patches_applied: list[str] | None = None,
        user_answers: list[str] | None = None,
    ) -> None:
        """Append one scaffold attempt (judge result) to run['scaffold_attempts'][agent_name]."""
        run = self._read(run_id)
        entry = {
            "attempt": attempt,
            "judge_score": judge_score,
            "judge_criteria": judge_criteria,
            "judge_feedback": judge_feedback or "",
            "model_id": model_id,
            "patches_applied": patches_applied or [],
            "user_answers": user_answers or [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        run.setdefault("scaffold_attempts", {}).setdefault(agent_name, []).append(entry)
        self._write(run_id, run)

    def set_scaffold_judge(
        self,
        run_id: str,
        agent_name: str,
        judge_score: float | None,
        judge_criteria: list[dict[str, Any]],
        judge_feedback: str,
    ) -> None:
        """Update judge results for an already-scaffolded agent."""
        run = self._read(run_id)
        entry = run.setdefault("scaffolded", {}).setdefault(agent_name, {})
        entry["judge_score"] = judge_score
        entry["judge_criteria"] = judge_criteria
        entry["judge_feedback"] = judge_feedback
        self._write(run_id, run)

    def set_agent_installed(self, run_id: str, agent_name: str) -> None:
        run = self._read(run_id)
        run.setdefault("scaffolded", {}).setdefault(agent_name, {})["installed"] = True
        self._write(run_id, run)

    def set_improvement_result(
        self,
        run_id: str,
        agent_name: str,
        advice: dict[str, Any],
    ) -> None:
        run = self._read(run_id)
        run.setdefault("improvement", {})[agent_name] = {
            "advice": advice,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(run_id, run)

    def set_scaffold_diagnosis(
        self, run_id: str, agent_name: str, diagnosis: dict[str, Any]
    ) -> None:
        run = self._read(run_id)
        run.setdefault("scaffold_diagnosis", {})[agent_name] = diagnosis
        self._write(run_id, run)

    def set_pending_questions(
        self,
        run_id: str,
        agent_name: str,
        questions: list[dict[str, Any]],
    ) -> None:
        """Store generated questions and set run status to waiting_for_client."""
        run = self._read(run_id)
        run["pending_questions"] = questions
        run["pending_agent"] = agent_name
        run["status"] = "waiting_for_client"
        self._write(run_id, run)

    def save_agent_state(
        self,
        run_id: str,
        agent_name: str,
        state: dict[str, Any],
    ) -> None:
        """Merge state into run['agent_state'][agent_name] (for scaffold resume)."""
        run = self._read(run_id)
        run.setdefault("agent_state", {}).setdefault(agent_name, {}).update(state)
        self._write(run_id, run)

    def set_client_answers(
        self,
        run_id: str,
        answers: dict[str, str],
    ) -> None:
        """Store client answers keyed by criterion name and resume scaffolding."""
        run = self._read(run_id)
        run.setdefault("client_answers", {}).update(answers)
        run["status"] = "scaffolding"
        self._write(run_id, run)

    def mark_resumed(self, run_id: str, new_run_id: str) -> None:
        """Mark a waiting_for_client run as resumed after client answers were processed."""
        run = self._read(run_id)
        run["status"] = "resumed"
        run["resumed_as"] = new_run_id
        self._write(run_id, run)

    def set_review_decision(
        self,
        run_id: str,
        decision: str,
        agent_names: list[str] | None = None,
    ) -> None:
        """decision: 'approved' | 'rejected'. agent_names: partial selection or None for all."""
        if decision not in ("approved", "rejected"):
            raise ValueError(f"Invalid decision: {decision!r}")
        run = self._read(run_id)
        if agent_names:
            for bloc in run.get("blocs", []):
                if bloc.get("agent") in agent_names:
                    bloc["review_decision"] = decision
            pending = [
                b
                for b in run.get("blocs", [])
                if b.get("requires_review") and not b.get("review_decision")
            ]
            if not pending:
                run["status"] = decision
        else:
            run["status"] = decision
        self._write(run_id, run)

    # ── read ───────────────────────────────────────────────────────────────────

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_runs(self, only_review: bool = False) -> list[dict[str, Any]]:
        runs = []
        for p in sorted(self._dir.glob("*.json"), reverse=True):
            try:
                run = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if only_review and not run.get("requires_review"):
                continue
            runs.append(run)
        return runs

    # ── internals ──────────────────────────────────────────────────────────────

    def _path(self, run_id: str) -> Path:
        return self._dir / f"{run_id}.json"

    def _read(self, run_id: str) -> dict[str, Any]:
        path = self._path(run_id)
        if not path.exists():
            raise KeyError(f"Run {run_id!r} not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, run_id: str, data: dict[str, Any]) -> None:
        self._path(run_id).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
