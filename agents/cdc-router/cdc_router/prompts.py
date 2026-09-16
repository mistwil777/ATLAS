"""Prompts for cdc-router LLM calls."""

SEGMENTATION_PROMPT = """\
You are an expert software architect analyzing a functional specification (CDC).
Your task: segment the CDC into functional blocs, decide for each whether it requires AI reasoning, \
a reusable deterministic tool, or inline code — and generate evaluation criteria for each bloc.

## Decision rules — apply strictly, in order

Use "ia" if ANY of these is true:
- Input is unstructured natural language (email, contract, comment, support ticket, free text)
- Task requires semantic interpretation (classification, summarization, entity extraction, sentiment, intent)
- Expected result is ambiguous or context-dependent

Use "tool" if ALL of these are true AND it is NOT "ia":
- Logic is deterministic and reusable across multiple projects (formatting, scoring, validation, parsing)
- The bloc deserves its own standalone library callable by any agent
- Examples: output formatter, confidence scorer, schema validator, text normalizer

Use "code" if ALL of these are true AND it is NOT "ia" or "tool":
- Transformation is trivial, single-use, and project-specific
- Not worth packaging as a reusable library
- Expected result is deterministic for a given input

## Criteria generation rules

For each bloc, generate 2 to 4 evaluation criteria as assertion strings.
- For "ia" blocs: assert semantic quality, completeness, absence of hallucination, alignment with CDC intent
- For "tool" blocs: assert output contract, determinism, edge-case handling, reusability
- For "code" blocs: assert output format, field presence, value constraints (deterministic, verifiable)
- Each criterion must be a single sentence starting with a verb ("Output must...", "Response must...", "All fields must...")
- Criteria must be specific to the bloc content — never generic

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
      "type": "ia" or "tool" or "code",
      "agent_suggested": "<PascalCase name — for 'ia': agent e.g. TicketClassifier; for 'tool': tool e.g. OutputFormatter; for 'code': null>",
      "justification": "<one sentence applying the decision rule>",
      "criteria": [
        "<criterion 1>",
        "<criterion 2>"
      ]
    }}
  ]
}}
"""

CORRECTION_PROMPT = """\
Your previous segmentation was evaluated and failed on the following criteria:

{failures}

Here is your previous output:
{previous_output}

Fix ONLY what failed. Keep correct blocs unchanged.
Preserve the "criteria" field on each bloc — update it only if the failure is about criteria quality.
Return ONLY valid JSON, no markdown, no explanation.

{{
  "blocs": [
    {{
      "bloc": "...",
      "type": "ia" or "tool" or "code",
      "agent_suggested": "<PascalCase functional name> or null",
      "justification": "...",
      "criteria": ["...", "..."]
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
      "type": "ia" or "tool" or "code",
      "agent_suggested": "<PascalCase functional name> or null",
      "justification": "...",
      "criteria": ["...", "..."]
    }}
  ]
}}

Previous response:
{previous_response}
"""
