"""Routes segmented blocs to existing ATLAS agents or flags them for creation."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

_log = logging.getLogger("cdc_router.router")


def _check_capability_coherence(
    existing: dict,
    required_name: str,
    required_type: str,
    required_description: str,
    llm_fn: Callable,
    llm_cfg: dict,
) -> str:
    """Ask LLM if existing KG component can be reused, adapted, or must be recreated.

    Returns 'reuse', 'adapt', or 'create'.
    """
    from cdc_router.prompts import CAPABILITY_CHECK_PROMPT  # noqa: PLC0415

    prompt = CAPABILITY_CHECK_PROMPT.format(
        existing_name=existing.get("id", ""),
        existing_type=existing.get("node_type", ""),
        existing_description=existing.get("cdc_summary", existing.get("description", "")),
        existing_capabilities=", ".join(existing.get("capabilities", []) or []),
        required_name=required_name,
        required_type=required_type,
        required_description=required_description,
    )
    try:
        raw = llm_fn(prompt, llm_cfg, 256)
        if not raw:
            return "reuse"
        data = json.loads(raw.strip())
        return data.get("decision", "reuse")
    except Exception:  # noqa: BLE001
        _log.debug("capability check failed for %s — defaulting to reuse", required_name)
        return "reuse"


def route(
    blocs: list[dict],
    kg_graph: dict,
    llm_fn: Callable | None = None,
    llm_cfg: dict | None = None,
) -> dict:
    """Map blocs to agents, detect missing ones.

    Returns enriched routing dict with:
      - blocs: each with agent_exists field populated
      - pipeline: ordered list of agent names (ia blocs only)
      - requires_creation: agents that need to be created
      - atlas_agents_reused: existing agents matched
    """
    nodes = kg_graph.get("nodes", [])
    known_agents = {n["id"]: n for n in nodes if n.get("node_type", "agent") == "agent"}
    known_tools = {n["id"]: n for n in nodes if n.get("node_type") == "tool"}
    _do_check = llm_fn is not None and llm_cfg is not None

    enriched_blocs = []
    pipeline = []
    requires_creation = []
    requires_tools = []
    atlas_agents_reused = []
    atlas_tools_reused = []
    seen_names: set[str] = set()  # deduplicate blocs by (type, agent_name)

    for bloc in blocs:
        b = dict(bloc)
        bloc_type = b.get("type")
        agent_name = b.get("agent") or b.get("agent_suggested")

        # Skip duplicate blocs (same type + name already processed)
        dedup_key = f"{bloc_type}:{agent_name}" if agent_name else None
        if dedup_key and dedup_key in seen_names:
            continue
        if dedup_key:
            seen_names.add(dedup_key)

        # Normalize: prefer 'agent' field, fall back to 'agent_suggested'
        agent_name = b.get("agent") or b.get("agent_suggested")

        if bloc_type == "ia" and agent_name:
            candidate = known_agents.get(agent_name)
            if candidate:
                decision = (
                    _check_capability_coherence(
                        candidate,
                        agent_name,
                        "agent",
                        b.get("justification", ""),
                        llm_fn,
                        llm_cfg,
                    )
                    if _do_check
                    else "reuse"
                )
                b["reuse_decision"] = decision
                if decision == "reuse":
                    b["agent_exists"] = True
                    if agent_name not in atlas_agents_reused:
                        atlas_agents_reused.append(agent_name)
                    if agent_name not in pipeline:
                        pipeline.append(agent_name)
                else:
                    # adapt or create → treat as new, suffix name if collision
                    b["agent_exists"] = False
                    new_name = agent_name if decision == "adapt" else f"{agent_name}_v2"
                    b["agent"] = new_name
                    if new_name not in requires_creation:
                        requires_creation.append(new_name)
                    if new_name not in pipeline:
                        pipeline.append(new_name)
            else:
                b["agent_exists"] = False
                if agent_name not in requires_creation:
                    requires_creation.append(agent_name)
                if agent_name not in pipeline:
                    pipeline.append(agent_name)
        elif bloc_type == "tool" and agent_name:
            candidate = known_tools.get(agent_name)
            if candidate:
                decision = (
                    _check_capability_coherence(
                        candidate,
                        agent_name,
                        "tool",
                        b.get("justification", ""),
                        llm_fn,
                        llm_cfg,
                    )
                    if _do_check
                    else "reuse"
                )
                b["reuse_decision"] = decision
                if decision == "reuse":
                    b["agent_exists"] = True
                    if agent_name not in atlas_tools_reused:
                        atlas_tools_reused.append(agent_name)
                else:
                    b["agent_exists"] = False
                    new_name = agent_name if decision == "adapt" else f"{agent_name}_v2"
                    b["agent"] = new_name
                    if new_name not in requires_tools:
                        requires_tools.append(new_name)
            else:
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
        "atlas_tools_reused": atlas_tools_reused,
    }
