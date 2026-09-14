"""atlas-review — AtlasReviewAgent.

Re-processes rejected ATLAS pipeline runs.

Flow:
  1. Load rejected run from RunStore
  2. Extract blocs that require review (requires_review=True)
  3. For each failing bloc: query KG for alternative/composable agents
  4. Search RAG for similar past corrections (if available)
  5. Re-orchestrate the failing blocs with enriched context
  6. Persist the new run with parent_run_id reference
  7. Yield structured events + summary
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator
from pathlib import Path

_log = logging.getLogger("atlas_review")

_DEFAULT_MEMORY_PATH = Path.home() / ".atlas" / "memory"
_DEFAULT_RUN_STORE_DIR = Path.home() / ".atlas" / "runs"


class AtlasReviewAgent:
    """Re-processes rejected runs using KG routing and RAG context.

    Args:
        llm_fn:         LLM callable (prompt, cfg, max_tokens) -> str | None
        llm_cfg:        LLM config dict
        memory_path:    Path to atlas-memory storage (KG + RAG)
        run_store_dir:  Path to run store directory
    """

    def __init__(
        self,
        llm_fn: Callable[[str, dict, int], str | None] | None = None,
        llm_cfg: dict | None = None,
        memory_path: Path | None = None,
        run_store_dir: Path | None = None,
    ) -> None:
        self._llm = llm_fn or _stub_llm
        self._llm_cfg = llm_cfg or {}
        self._memory_path = Path(memory_path) if memory_path else _DEFAULT_MEMORY_PATH
        self._run_store_dir = Path(run_store_dir) if run_store_dir else _DEFAULT_RUN_STORE_DIR

    # ── Public entry point ─────────────────────────────────────────────────────

    def run(self, run_id: str) -> Generator[dict, None, None]:
        """Re-process a rejected run. Yields log dicts, then summary."""
        from atlas_core import collect  # noqa: PLC0415
        from atlas_memory.run_store import RunStore  # noqa: PLC0415

        _t0 = time.monotonic()
        yield self._info(f"atlas-review starting for run {run_id}")

        store = RunStore(self._run_store_dir)
        run = store.get_run(run_id)

        if run is None:
            yield self._error(f"Run {run_id!r} not found")
            return

        if run.get("status") != "rejected":
            yield self._warn(f"Run {run_id} status is {run['status']!r} — expected 'rejected'")

        failing_blocs = [b for b in run.get("blocs", []) if b.get("requires_review")]
        if not failing_blocs:
            yield self._info("No blocs require review — nothing to re-process")
            return

        yield self._info(f"{len(failing_blocs)} bloc(s) to re-process")

        # KG: find alternative agents for each failing bloc
        kg = self._load_kg()
        rag = self._load_rag()

        enriched_blocs = []
        for bloc in failing_blocs:
            agent_name = bloc.get("agent", "")
            yield self._info(f"Analysing bloc: agent={agent_name}")

            alternatives = self._kg_alternatives(kg, agent_name)
            past_fixes = self._rag_search(rag, bloc)

            enriched_blocs.append(
                {
                    **bloc,
                    "kg_alternatives": alternatives,
                    "rag_context": past_fixes,
                }
            )

            if alternatives:
                yield self._info(f"  KG suggests alternatives: {[a['name'] for a in alternatives]}")
            if past_fixes:
                yield self._info(f"  RAG found {len(past_fixes)} similar past correction(s)")

        # Re-orchestrate the failing blocs
        new_run_id, results = self._reorchestrate(
            enriched_blocs,
            run.get("metadata", {}).get("cdc_preview", ""),
            store,
            run_id,
        )

        if new_run_id is None:
            yield self._error("Re-orchestration failed — no new run created")
            return

        duration = round(time.monotonic() - _t0, 3)
        collect.emit_metrics(
            "atlas-review",
            {
                "duration_s": duration,
                "status": "success",
                "parent_run_id": run_id,
                "new_run_id": new_run_id,
                "reprocessed_blocs": len(failing_blocs),
            },
        )

        yield {
            "summary": True,
            "parent_run_id": run_id,
            "new_run_id": new_run_id,
            "reprocessed_blocs": len(failing_blocs),
            "results": results,
            "duration_s": duration,
        }

    # ── KG ─────────────────────────────────────────────────────────────────────

    def _load_kg(self):
        try:
            from atlas_memory.kg import KGStore  # noqa: PLC0415

            return KGStore(self._memory_path / "graph.json")
        except ImportError:
            return None

    def _kg_alternatives(self, kg, agent_name: str) -> list[dict]:
        if kg is None or not agent_name:
            return []
        try:
            return kg.query_composable(agent_name)
        except Exception:  # noqa: BLE001
            return []

    # ── RAG ────────────────────────────────────────────────────────────────────

    def _load_rag(self):
        try:
            from atlas_memory.rag import RAGStore  # noqa: PLC0415

            return RAGStore(self._memory_path / "vectors")
        except ImportError:
            return None

    def _rag_search(self, rag, bloc: dict) -> list[dict]:
        if rag is None:
            return []
        try:
            query = (
                f"correction for agent {bloc.get('agent', '')} bloc: {bloc.get('bloc', '')[:200]}"
            )
            return rag.search(query, agent_name="atlas-review", top_k=3)
        except Exception:  # noqa: BLE001
            return []

    # ── Re-orchestration ───────────────────────────────────────────────────────

    def _reorchestrate(
        self,
        enriched_blocs: list[dict],
        cdc_preview: str,
        store,
        parent_run_id: str,
    ) -> tuple[str | None, list[dict]]:
        try:
            from atlas_orchestrator.pipeline import orchestrate  # noqa: PLC0415
        except ImportError:
            _log.error("atlas-orchestrator not installed")
            return None, []

        # Build a minimal CDC from the failing blocs with enriched context
        cdc_text = self._build_review_cdc(enriched_blocs, cdc_preview)

        report = orchestrate(
            cdc_text=cdc_text,
            llm_fn=self._llm,
            llm_cfg=self._llm_cfg,
            run_store_dir=self._run_store_dir,
        )

        if report is None:
            return None, []

        new_run_id = report.get("run_id")

        # Link new run to parent
        if new_run_id:
            new_run = store.get_run(new_run_id)
            if new_run:
                new_run["parent_run_id"] = parent_run_id
                new_run["metadata"]["review_context"] = "re-processed by atlas-review"
                store._write(new_run_id, new_run)

        return new_run_id, report.get("bloc_results", [])

    def _build_review_cdc(self, enriched_blocs: list[dict], cdc_preview: str) -> str:
        lines = [
            "# ATLAS Review — Re-processing rejected blocs",
            "",
            f"Original CDC context: {cdc_preview[:300]}",
            "",
            "## Blocs requiring re-processing",
            "",
        ]
        for i, bloc in enumerate(enriched_blocs, 1):
            lines.append(f"### Bloc {i} — Agent: {bloc.get('agent', 'unknown')}")
            lines.append(f"Content: {bloc.get('bloc', '')}")
            lines.append(f"Judge score: {bloc.get('judge_score')}")

            if bloc.get("kg_alternatives"):
                alts = ", ".join(a["name"] for a in bloc["kg_alternatives"])
                lines.append(f"KG suggested alternatives: {alts}")

            if bloc.get("rag_context"):
                lines.append("Past corrections found in memory:")
                for fix in bloc["rag_context"][:2]:
                    lines.append(f"  - {str(fix)[:200]}")

            lines.append("")

        return "\n".join(lines)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _info(self, msg: str) -> dict:
        return {"level": "info", "msg": msg}

    def _warn(self, msg: str) -> dict:
        return {"level": "warning", "msg": msg}

    def _error(self, msg: str) -> dict:
        return {"level": "error", "msg": msg}


def _stub_llm(prompt: str, cfg: dict, max_tokens: int = 1024) -> str | None:
    _log.warning("No LLM configured for atlas-review — inject llm_fn at construction")
    return None
