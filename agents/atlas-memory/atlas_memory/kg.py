"""Knowledge Graph store — models ATLAS agents and their relationships."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import networkx as nx


class KGStore:
    """Directed graph of ATLAS agents with capabilities, inputs, outputs, and relations.

    Relations: CHAINS_WITH, VALIDATES, PRODUCES, CONSUMED_BY
    """

    def __init__(self, graph_path: Path) -> None:
        self._path = graph_path
        self._g: nx.DiGraph = nx.DiGraph()
        if graph_path.exists():
            self.load()

    def register_agent(
        self,
        name: str,
        capabilities: list[str],
        inputs: list[str],
        outputs: list[str],
        tags: list[str],
    ) -> None:
        self._g.add_node(
            name,
            capabilities=capabilities,
            inputs=inputs,
            outputs=outputs,
            tags=tags,
        )
        self.save()

    def add_edge(self, from_agent: str, to_agent: str, relation: str) -> None:
        self._g.add_edge(from_agent, to_agent, relation=relation)
        self.save()

    def query_composable(self, agent_name: str) -> list[dict[str, Any]]:
        """Return agents that can be chained after agent_name (direct successors)."""
        if agent_name not in self._g:
            return []
        result = []
        for successor in self._g.successors(agent_name):
            data = dict(self._g.nodes[successor])
            data["name"] = successor
            data["relation"] = self._g[agent_name][successor].get("relation", "")
            result.append(data)
        return result

    def get_graph(self) -> dict[str, Any]:
        return nx.node_link_data(self._g, edges="links")

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(nx.node_link_data(self._g, edges="links"), indent=2), encoding="utf-8"
        )

    def load(self) -> None:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._g = nx.node_link_graph(data, directed=True, edges="links")
