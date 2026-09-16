"""Routes segmented blocs to existing ATLAS agents or flags them for creation."""

from __future__ import annotations


def route(blocs: list[dict], kg_graph: dict) -> dict:
    """Map blocs to agents, detect missing ones.

    Returns enriched routing dict with:
      - blocs: each with agent_exists field populated
      - pipeline: ordered list of agent names (ia blocs only)
      - requires_creation: agents that need to be created
      - atlas_agents_reused: existing agents matched
    """
    known_agents = {node["id"] for node in kg_graph.get("nodes", [])}

    enriched_blocs = []
    pipeline = []
    requires_creation = []
    requires_tools = []
    atlas_agents_reused = []

    for bloc in blocs:
        b = dict(bloc)
        bloc_type = b.get("type")
        agent_name = b.get("agent_suggested")

        if bloc_type == "ia" and agent_name:
            exists = agent_name in known_agents
            b["agent_exists"] = exists
            if exists:
                if agent_name not in atlas_agents_reused:
                    atlas_agents_reused.append(agent_name)
                if agent_name not in pipeline:
                    pipeline.append(agent_name)
            else:
                if agent_name not in requires_creation:
                    requires_creation.append(agent_name)
                if agent_name not in pipeline:
                    pipeline.append(agent_name)
        elif bloc_type == "tool" and agent_name:
            b["agent_exists"] = False
            if agent_name not in requires_tools:
                requires_tools.append(agent_name)
        else:
            b["agent_exists"] = False

        enriched_blocs.append(b)

    return {
        "blocs": enriched_blocs,
        "pipeline": pipeline,
        "requires_creation": requires_creation,
        "requires_tools": requires_tools,
        "atlas_agents_reused": atlas_agents_reused,
    }
