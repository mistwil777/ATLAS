"""Discover installed ATLAS agents via importlib.metadata entry-points."""

from __future__ import annotations

from importlib.metadata import entry_points


def discover_agents() -> dict[str, str]:
    """Return {agent-name: "module:ClassName"} for all installed ATLAS agents.

    Agents register themselves via:
        [project.entry-points."atlas.agents"]
        pair-forge = "pair_forge.agent:PairForgeAgent"
    """
    eps = entry_points(group="atlas.agents")
    return {ep.name: ep.value for ep in eps}
