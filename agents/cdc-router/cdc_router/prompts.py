"""Prompts for cdc-router LLM calls."""

SEGMENTATION_PROMPT = """\
You are an expert software architect analyzing a functional specification (CDC).
Your task: segment the CDC into functional blocs, decide for each whether it requires AI or deterministic code, \
and generate evaluation criteria that will be used by a judge agent to validate the output of each bloc.

## Decision rules — apply strictly, no exceptions

Use "ia" if ANY of these is true:
- Input is unstructured natural language (email, contract, comment, support ticket, free text)
- Task requires semantic interpretation (classification, summarization, entity extraction, sentiment, intent)
- Expected result is ambiguous or context-dependent

Use "code" if ALL of these are true:
- Input format is known and parseable (CSV, JSON, XML, fixed schema, date, regex)
- Task is a deterministic transformation (calculation, sort, format, lookup, rule validation)
- Expected result is deterministic for a given input

## Criteria generation rules

For each bloc, generate 2 to 4 evaluation criteria as assertion strings.
- For "code" blocs: assert output format, field presence, value constraints (deterministic, verifiable)
- For "ia" blocs: assert semantic quality, completeness, absence of hallucination, alignment with CDC intent
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
      "type": "ia" or "code",
      "agent_suggested": "<PascalCase functional name, e.g. TicketClassifier, SentimentAnalyzer — NOT a model name like Claude or GPT>",
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
      "type": "ia" or "code",
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
      "type": "ia" or "code",
      "agent_suggested": "<PascalCase functional name> or null",
      "justification": "...",
      "criteria": ["...", "..."]
    }}
  ]
}}

Previous response:
{previous_response}
"""
