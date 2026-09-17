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
        self, run_id: str, tool_name: str, output_path: str, spec: dict[str, Any]
    ) -> None:
        run = self._read(run_id)
        run.setdefault("tools_built", {})[tool_name] = {
            "output_path": output_path,
            "module_name": spec.get("module_name", ""),
            "functions": spec.get("functions", []),
            "description": spec.get("description", ""),
            "installed": False,
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
    ) -> None:
        run = self._read(run_id)
        run.setdefault("scaffolded", {})[agent_name] = {
            "output_path": output_path,
            "installed": False,
            "spec": spec,
            "user_answers": user_answers or [],
            "selected_patches": selected_patches or [],
            "model_id": model_id or "",
        }
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
