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
        node_type: str = "agent",
        shareable: bool = True,
        judge_score: float | None = None,
        confidence: float = 1.0,
        project: str = "",
        cdc_summary: str = "",
        context_tags: list[str] | None = None,
        output_type: str = "json",
        parent: str | None = None,
    ) -> None:
        existing = dict(self._g.nodes.get(name, {}))
        attrs: dict = {
            "node_type": node_type,
            "capabilities": capabilities,
            "inputs": inputs,
            "outputs": outputs,
            "tags": tags,
            "shareable": shareable,
            "confidence": max(0.0, min(1.0, confidence)),
            "project": project or existing.get("project", ""),
            "cdc_summary": cdc_summary or existing.get("cdc_summary", ""),
            "context_tags": context_tags
            if context_tags is not None
            else existing.get("context_tags", []),
            "output_type": output_type or existing.get("output_type", "json"),
            # preserve existing history lists
            "score_history": existing.get("score_history", []),
            "coaching_history": existing.get("coaching_history", []),
        }
        if judge_score is not None:
            attrs["judge_score"] = judge_score
        if parent is not None:
            attrs["parent"] = parent
        elif existing.get("parent"):
            attrs["parent"] = existing["parent"]
        self._g.add_node(name, **attrs)
        self.save()

    def fork_agent(self, base_name: str, context: str) -> str:
        """Create a context-specific fork of base_name. Returns the fork name.

        The fork inherits base capabilities/tags/cdc_summary but starts with
        a clean score history. A FORKED_FROM edge links fork → base.
        """
        fork_name = f"{base_name}#{context}"
        if fork_name in self._g:
            return fork_name

        base = dict(self._g.nodes.get(base_name, {}))
        attrs: dict = {
            "node_type": base.get("node_type", "agent"),
            "capabilities": list(base.get("capabilities", [])),
            "inputs": list(base.get("inputs", [])),
            "outputs": list(base.get("outputs", [])),
            "tags": list(base.get("tags", [])),
            "shareable": base.get("shareable", True),
            "confidence": base.get("confidence", 1.0),
            "project": context,
            "cdc_summary": base.get("cdc_summary", ""),
            "context_tags": [],
            "output_type": base.get("output_type", "json"),
            "score_history": [],
            "coaching_history": [],
            "parent": base_name,
        }
        if base.get("judge_score") is not None:
            attrs["judge_score"] = base["judge_score"]

        self._g.add_node(fork_name, **attrs)
        self._g.add_edge(fork_name, base_name, relation="FORKED_FROM")
        self.save()
        return fork_name

    def propagate_to_parent(self, fork_name: str) -> str | None:
        """Copy coaching_history patches from fork to its parent. Returns parent name or None."""
        fork = self._g.nodes.get(fork_name)
        if not fork:
            return None
        parent_name: str | None = fork.get("parent")
        if not parent_name or parent_name not in self._g:
            return None
        parent = self._g.nodes[parent_name]
        fork_patches = fork.get("coaching_history", [])
        parent_patches = parent.get("coaching_history", [])
        existing_descs = {p.get("patch") for p in parent_patches}
        for patch in fork_patches:
            if patch.get("patch") not in existing_descs:
                parent_patches.append(patch)
        parent["coaching_history"] = parent_patches
        self.save()
        return parent_name

    def needs_fork(self, name: str, context_tags: list[str]) -> bool:
        """True if agent exists but was created for a different context."""
        if name not in self._g:
            return False
        existing_tags = set(self._g.nodes[name].get("context_tags", []))
        incoming_tags = set(context_tags or [])
        return bool(existing_tags) and bool(incoming_tags) and existing_tags != incoming_tags

    def update_agent_score(
        self,
        name: str,
        judge_score: float,
        run_id: str,
        source: str = "scaffold",
        test_detail: dict | None = None,
    ) -> None:
        """Record a new judge score and update confidence = judge_score / 5.0.

        source: "scaffold" (code evaluated without execution) or "test" (real execution).
        test_detail: for source="test", stores {input, output, feedback, criteria} for
        the detail view. Multiple test_detail entries with the same run_id are grouped
        into one session point in Trends.
        """
        if name not in self._g:
            return
        node = self._g.nodes[name]
        history: list[dict] = list(node.get("score_history", []))
        entry: dict = {
            "score": judge_score,
            "run_id": run_id,
            "source": source,
            "timestamp": __import__("datetime")
            .datetime.now(__import__("datetime").timezone.utc)
            .isoformat(),
        }
        if test_detail is not None:
            entry["test_detail"] = test_detail
        history.append(entry)
        node["score_history"] = history
        node["judge_score"] = judge_score
        node["confidence"] = max(0.0, min(1.0, judge_score / 5.0))
        self.save()

    def update_human_score(self, name: str, human_score: float) -> None:
        """Record aggregated human validation score (separate from LLM judge_score)."""
        if name not in self._g:
            return
        self._g.nodes[name]["human_score"] = human_score
        self.save()

    def get_nodes_by_type(self, node_type: str) -> set[str]:
        return {n for n, d in self._g.nodes(data=True) if d.get("node_type", "agent") == node_type}

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

    def update_node_coaching(
        self,
        name: str,
        patch_description: str,
        criterion: str,
        delta: float,
    ) -> None:
        if name not in self._g:
            return
        node = self._g.nodes[name]
        # skip if node is not shareable (airgap or cross_learning=false)
        if not node.get("shareable", True):
            return
        history = node.get("coaching_history", [])
        history.append({"patch": patch_description, "criterion": criterion, "delta": delta})
        node["coaching_history"] = history
        self.save()

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
