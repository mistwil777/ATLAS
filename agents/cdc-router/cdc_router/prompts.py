"""Prompts for cdc-router LLM calls."""

SEGMENTATION_PROMPT = """\
You are an expert software architect analyzing a functional specification (CDC).
Your task: segment the CDC into functional blocs and decide for each whether it requires AI or deterministic code.

## Decision rules — apply strictly, no exceptions

Use "ia" if ANY of these is true:
- Input is unstructured natural language (email, contract, comment, support ticket, free text)
- Task requires semantic interpretation (classification, summarization, entity extraction, sentiment, intent)
- Expected result is ambiguous or context-dependent

Use "code" if ALL of these are true:
- Input format is known and parseable (CSV, JSON, XML, fixed schema, date, regex)
- Task is a deterministic transformation (calculation, sort, format, lookup, rule validation)
- Expected result is deterministic for a given input

## Available ATLAS agents
{kg_agents}

## Similar CDCs from past experience
{rag_examples}

## CDC to analyze
{cdc_text}

## Output format — return ONLY valid JSON, no markdown, no explanation

{{
  "blocs": [
    {{
      "bloc": "<short description of the functional bloc>",
      "type": "ia" or "code",
      "agent_suggested": "<agent name if ia, null if code>",
      "justification": "<one sentence applying the decision rule>"
    }}
  ]
}}
"""

REPROMPT = """\
Your previous response was not valid JSON. Return ONLY the JSON object, no markdown fences, no explanation.

Expected format:
{{
  "blocs": [
    {{
      "bloc": "...",
      "type": "ia" or "code",
      "agent_suggested": "..." or null,
      "justification": "..."
    }}
  ]
}}

Previous response:
{previous_response}
"""
