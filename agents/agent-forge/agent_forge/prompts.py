"""Prompt templates for agent-forge spec extraction."""

from __future__ import annotations

SPEC_EXTRACTION = """\
You are an ATLAS agent architect. Read the specification below and extract the agent parameters.

Return ONLY a JSON object with these keys:
- "agent_name": short human name, e.g. "text-classifier" (kebab-case, no spaces)
- "description": one sentence describing what the agent does
- "input_type": what the agent receives (e.g. "text documents", "images", "CSV files")
- "output_type": what the agent produces (e.g. "JSON with label and score", "SQL queries")
- "domain": application domain (e.g. "document classification", "code generation")
- "constraints": list of strings, each a notable constraint (e.g. "offline", "no external API calls")
- "uses_llm": true if the agent needs an LLM to operate, false if it is purely algorithmic

Specification:
{spec_content}

JSON:"""


# Interactive mode — fixed questions, not LLM prompts
INTERACTIVE_QUESTIONS = [
    ("agent_name", "Agent name (kebab-case, e.g. text-classifier): "),
    ("description", "One-sentence description of what it does: "),
    ("input_type", "What does it receive as input? (e.g. text documents, CSV files): "),
    ("output_type", "What does it produce as output? (e.g. JSON with label, SQL query): "),
    ("uses_llm", "Does it need an LLM to operate? (yes/no): "),
]
