"""atlas-memory — AtlasMemoryAgent.

Shared RAG + Knowledge Graph memory for all ATLAS agents.

Operations:
  index          — store an experience (cdc, scaffold, judge_eval, pipeline_trace, contract_violation)
  search         — retrieve similar experiences
  register_agent — add/update an agent node in the Knowledge Graph
  add_edge       — add a relation between two agents in the KG
  query_composable — list agents that chain with a given agent
  get_graph      — dump the full KG
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Generator
from pathlib import Path

_DEFAULT_STORAGE = Path(os.environ.get("ATLAS_MEMORY_PATH", Path.home() / ".atlas" / "memory"))


class AtlasMemoryAgent:
    """Memory agent for indexing and retrieving ATLAS agent experiences.

    Args:
        storage_path: Root directory for persistence. Defaults to ~/.atlas/memory/
    """

    def __init__(
        self,
        storage_path: Path | None = None,
        embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    ) -> None:
        self._base = Path(storage_path) if storage_path else _DEFAULT_STORAGE
        self._rag_path = self._base / "vectors"
        self._kg_path = self._base / "graph.json"
        self._embed_fn = embed_fn
        self._rag = None
        self._kg = None

    def _get_rag(self):  # type: ignore[return]
        if self._rag is None:
            from atlas_memory.rag import RAGStore  # noqa: PLC0415

            self._rag = RAGStore(self._rag_path, embed_fn=self._embed_fn)
        return self._rag

    def _get_kg(self):  # type: ignore[return]
        if self._kg is None:
            from atlas_memory.kg import KGStore  # noqa: PLC0415

            self._kg = KGStore(self._kg_path)
        return self._kg

    def run(self, operation: str = "get_graph", **kwargs: object) -> Generator[dict, None, None]:
        """Dispatch to the requested operation. Yields log dicts, then summary."""
        from atlas_core import collect  # noqa: PLC0415

        _t0 = time.monotonic()
        yield self._info(f"atlas-memory: {operation}")

        dispatch = {
            "index": self._op_index,
            "search": self._op_search,
            "register_agent": self._op_register_agent,
            "register_tool": self._op_register_tool,
            "add_edge": self._op_add_edge,
            "query_composable": self._op_query_composable,
            "get_graph": self._op_get_graph,
        }
        handler = dispatch.get(operation)
        if handler is None:
            yield self._error(f"Unknown operation: {operation}")
            return

        try:
            result = handler(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("atlas_memory").warning("Operation '%s' failed: %s", operation, exc)
            yield self._error(f"Operation failed: {exc}")
            return

        collect.emit_metrics(
            "atlas-memory",
            {
                "duration_s": round(time.monotonic() - _t0, 3),
                "status": "success",
                "operation": operation,
            },
        )
        yield {"summary": True, "operation": operation, "result": result}

    def _op_index(
        self,
        entry_type: str,
        content: str,
        payload: dict | None = None,
        metadata: dict | None = None,
        **_: object,
    ) -> dict:
        point_id = self._get_rag().index(entry_type, content, payload, metadata)
        return {"id": point_id, "entry_type": entry_type}

    def _op_search(
        self,
        query: str,
        entry_type: str | None = None,
        top_k: int = 5,
        **_: object,
    ) -> dict:
        results = self._get_rag().search(query, entry_type, top_k)
        return {"count": len(results), "results": results}

    def _op_register_agent(
        self,
        name: str,
        capabilities: list | None = None,
        inputs: list | None = None,
        outputs: list | None = None,
        tags: list | None = None,
        **_: object,
    ) -> dict:
        self._get_kg().register_agent(
            name,
            capabilities=capabilities or [],
            inputs=inputs or [],
            outputs=outputs or [],
            tags=tags or [],
            node_type="agent",
        )
        return {"registered": name}

    def _op_register_tool(
        self,
        name: str,
        capabilities: list | None = None,
        tags: list | None = None,
        **_: object,
    ) -> dict:
        self._get_kg().register_agent(
            name,
            capabilities=capabilities or [],
            inputs=[],
            outputs=[],
            tags=tags or [],
            node_type="tool",
        )
        return {"registered": name, "node_type": "tool"}

    def _op_add_edge(
        self,
        from_agent: str,
        to_agent: str,
        relation: str = "CHAINS_WITH",
        **_: object,
    ) -> dict:
        self._get_kg().add_edge(from_agent, to_agent, relation)
        return {"edge": f"{from_agent} --{relation}--> {to_agent}"}

    def _op_query_composable(self, agent_name: str, **_: object) -> dict:
        composable = self._get_kg().query_composable(agent_name)
        return {"agent": agent_name, "composable_with": composable}

    def _op_get_graph(self, **_: object) -> dict:
        return self._get_kg().get_graph()

    def _info(self, msg: str) -> dict:
        return {"level": "info", "msg": msg}

    def _error(self, msg: str) -> dict:
        return {"level": "error", "msg": msg}
