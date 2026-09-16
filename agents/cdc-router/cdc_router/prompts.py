"""Prompts for cdc-router LLM calls."""

SEGMENTATION_PROMPT = """\
You are an expert software architect analyzing a functional specification (CDC).
Your task: segment the CDC into functional blocs, decide for each whether it requires AI reasoning, \
a reusable deterministic tool, or inline code — and generate evaluation criteria for each bloc.

## Decision rules — apply strictly, in order

**Step 1 — is it "ia"?** Yes if ANY of these:
- Input is unstructured natural language (email, ticket, contract, free text)
- Task requires semantic interpretation (classification, summarization, extraction, intent, sentiment)
- Expected result is ambiguous or context-dependent

**Step 2 — is it "tool"?** Yes if ALL of these (and NOT "ia"):
- Logic is fully deterministic: same input always produces same output, no LLM needed
- Useful beyond this project: another team could import this function as-is
- Ask yourself: "would I write a unit test for this?" → if yes, it is "tool"
- Typical examples: output formatter, JSON schema validator, confidence scorer, text normalizer,
  priority scorer, structured response builder, field extractor from typed data, CSV/JSON encoder

**Step 3 — is it "code"?** Yes if ALL of these (and NOT "ia" or "tool"):
- Transformation is trivial and specific to this project's data shape
- Would be 1–3 lines inline in the pipeline, not worth its own library
- Another project would never reuse it as-is

**Rule of thumb**: when in doubt between "tool" and "code", prefer "tool" if the function has a
clear name and could be documented with a docstring. Prefer "code" only for throwaway glue logic.

**Concrete examples**:
| Bloc description                          | Type   | Reason                                          |
|-------------------------------------------|--------|-------------------------------------------------|
| Classify ticket priority from text        | ia     | semantic, ambiguous, LLM needed                 |
| Format agent output as structured JSON    | tool   | deterministic, reusable, testable               |
| Score confidence from probability dict    | tool   | deterministic, reusable across projects         |
| Validate output against JSON schema       | tool   | deterministic, reusable library                 |
| Append run_id to log filename             | code   | trivial glue, project-specific, 1 line          |

## Criteria generation rules

For each bloc, generate 2 to 4 evaluation criteria as assertion strings.
- For "ia" blocs: assert semantic quality, completeness, absence of hallucination, alignment with CDC intent
- For "tool" blocs: assert output contract, determinism, edge-case handling, reusability
- For "code" blocs: assert output format, field presence, value constraints (deterministic, verifiable)
- Each criterion must be a single sentence starting with a verb ("Output must...", "Response must...", "All fields must...")
- Criteria must be specific to the bloc content — never generic

## Available ATLAS agents (for "ia" blocs — use exact name in agent_suggested if applicable)
{kg_agents}

## Available ATLAS tools (for "tool" blocs — reuse exact name in agent_suggested if applicable)
{kg_tools}

## Similar CDCs from past experience
{rag_examples}

## CDC to analyze
{cdc_text}

## Output format — return ONLY valid JSON, no markdown, no explanation

IMPORTANT for agent_suggested and called_by:
- type "ia"   → agent_suggested REQUIRED (PascalCase agent name). called_by: null.
- type "tool" → agent_suggested REQUIRED (PascalCase tool name). called_by REQUIRED: PascalCase name of the "ia" agent that will call this tool (from blocs above).
- type "code" → agent_suggested: null. called_by: null.

{{
  "blocs": [
    {{
      "bloc": "<short description of the functional bloc>",
      "type": "ia" or "tool" or "code",
      "agent_suggested": "<PascalCase name — REQUIRED for ia and tool, null ONLY for code>",
      "called_by": "<PascalCase agent name — REQUIRED for tool (who calls this tool), null for ia and code>",
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
      "agent_suggested": "<PascalCase name — REQUIRED for ia and tool, null ONLY for code>",
      "called_by": "<PascalCase agent — REQUIRED for tool, null for ia and code>",
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
      "agent_suggested": "<PascalCase name — REQUIRED for ia and tool, null ONLY for code>",
      "called_by": "<PascalCase agent — REQUIRED for tool, null for ia and code>",
      "justification": "...",
      "criteria": ["...", "..."]
    }}
  ]
}}

Previous response:
{previous_response}
"""
