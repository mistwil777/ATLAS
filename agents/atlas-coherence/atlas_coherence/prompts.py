"""Prompts for atlas-coherence semantic overlap detection."""

from __future__ import annotations

COHERENCE_CHECK_PROMPT = """\
You are an expert software architect. You have a list of functional blocs (agents and tools) \
produced by a CDC decomposition. Your task is to detect semantic redundancy.

## Definition of a conflict

**Full overlap**: two components cover exactly the same responsibility — one is entirely redundant.
  Example: AgentA classifies ticket priority; AgentB also classifies ticket priority.

**Partial overlap**: one component produces outputs that another component is solely responsible for.
  Example: AgentA outputs {{priority, category, confidence}}; AgentB is dedicated to category classification.
  → AgentA should not produce 'category' — that is AgentB's sole responsibility.

## Rules
- Compare every pair of blocs (ia and tool alike).
- Focus on the described responsibility, not just the name.
- A tool and an agent can overlap if they perform the same transformation.
- If no conflict is found, return clean=true and an empty conflicts list.

## Blocs to analyse
{blocs_json}

## Return ONLY valid JSON, no markdown, no explanation.

{{
  "clean": true or false,
  "conflicts": [
    {{
      "type": "full" or "partial",
      "primary": "<agent_suggested of the component that should own this responsibility>",
      "redundant": "<agent_suggested of the component that must be corrected or removed>",
      "issue": "<one sentence — what exactly overlaps>",
      "fix": "<one sentence — what must change: remove redundant, or remove specific output field from redundant>"
    }}
  ]
}}
"""
