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


PROMPT_GENERATION = """\
You are an ATLAS agent prompt engineer.

Given the agent specification below, write the body of the MAIN_PROMPT string that will be injected \
into the agent's prompts.py file.

Rules:
- Write ALL instructions in ENGLISH, regardless of the specification language.
- Extract ALL enumerated values VERBATIM from the spec and list them as strict constraints.
  Example: if the spec says "priorité: critique | haute | normale | basse", the prompt must say
  "Allowed values for priority: critique, haute, normale, basse — no other value is accepted."
- For numeric fields (confidence, score…): specify the exact type and range (e.g. float between 0.0 and 1.0).
- For categorical fields: list every allowed value explicitly.
- Output format: describe the EXACT JSON structure expected, with EXACT field names as defined in the spec.
  Do NOT invent field names. Do NOT add fields not present in the spec (e.g. no "confidence_score", no "secondary_category").
- Include a concrete JSON example using the exact field names and example values.
- The prompt must end with: "Input:\n{{input}}" so the agent receives the actual input to process.
- The prompt must be self-contained: the agent must not need any external context to produce correct output.
- Do NOT include placeholder text like "TODO" or "define what the agent should do".

Return ONLY the prompt body (plain text, no Python string delimiters, no markdown fences).
The result will be used as-is inside a Python triple-quoted string.

Specification:
{spec_content}

Prompt body:"""


# Interactive mode — fixed questions, not LLM prompts
INTERACTIVE_QUESTIONS = [
    ("agent_name", "Agent name (kebab-case, e.g. text-classifier): "),
    ("description", "One-sentence description of what it does: "),
    ("input_type", "What does it receive as input? (e.g. text documents, CSV files): "),
    ("output_type", "What does it produce as output? (e.g. JSON with label, SQL query): "),
    ("uses_llm", "Does it need an LLM to operate? (yes/no): "),
]
