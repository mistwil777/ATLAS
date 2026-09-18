"""Prompts for atlas-judge LLM evaluation calls."""

POINTWISE_PROMPT = """\
You are an expert evaluator. Your task is to assess the quality of an AI agent's output \
against explicit criteria. Score each criterion from 1 to 5.

## Evaluation criteria
{criteria_block}

## Context (what the agent was asked to do)
{context}

## Output to evaluate
{output}

## Instructions
- Score each criterion independently from 1 to 5 (1=very poor, 3=acceptable, 5=excellent)
- A score >= 4 on all criteria means the output passed
- Be specific in your comments — reference the actual output content
- For each criterion with score < 3, classify the failure cause:
  - "model_error"  : the context contained an explicit instruction the agent did not follow
  - "code_error"   : the failure is a deterministic logic error (wrong calculation, bad parsing, incorrect regex)
  - "missing_spec" : the context did not specify this behavior — only the client can decide
  Rule: when in doubt between model_error and missing_spec, choose missing_spec.
- For criteria with score >= 3, set cause to null.
- Return ONLY valid JSON, no markdown, no explanation

{{
  "mode": "pointwise",
  "criteria_scores": [
    {{"criterion": "<name>", "score": <1-5>, "comment": "<specific observation>", "cause": "model_error"|"code_error"|"missing_spec"|null}}
  ],
  "feedback": "<one sentence overall assessment>",
  "winner": null
}}
"""

PAIRWISE_PROMPT = """\
You are an expert evaluator. Compare two outputs (A and B) and decide which is better \
according to the criteria below.

## Evaluation criteria
{criteria_block}

## Context (what the agent was asked to do)
{context}

## Output A
{output_a}

## Output B
{output_b}

## Instructions
- Evaluate each criterion for both outputs
- Choose the winner: "A", "B", or "tie"
- Do NOT prefer longer answers — judge quality, not quantity
- Return ONLY valid JSON, no markdown, no explanation

{{
  "mode": "pairwise",
  "criteria_scores": [
    {{"criterion": "<name>", "score_a": <1-5>, "score_b": <1-5>, "comment": "<observation>"}}
  ],
  "feedback": "<one sentence comparing the two outputs>",
  "winner": "A" or "B" or "tie"
}}
"""

REFERENCE_PROMPT = """\
You are an expert evaluator. Compare an AI agent's output against a validated reference \
and assess how well the output matches it according to the criteria below.

## Evaluation criteria
{criteria_block}

## Context (what the agent was asked to do)
{context}

## Reference (validated correct output)
{reference}

## Output to evaluate
{output}

## Instructions
- Score each criterion from 1 to 5 based on how closely the output matches the reference
- Score 5 = matches reference perfectly on this criterion
- Return ONLY valid JSON, no markdown, no explanation

{{
  "mode": "reference",
  "criteria_scores": [
    {{"criterion": "<name>", "score": <1-5>, "comment": "<observation vs reference>"}}
  ],
  "feedback": "<one sentence on overall alignment with reference>",
  "winner": null
}}
"""

DIAGNOSE_PROMPT = """\
You are an expert AI systems diagnostician. An agent failed to meet quality standards after \
{n_attempts} scaffold attempts. Analyze the repeated failures and produce an actionable diagnosis.

## Agent
{agent_name}

## Context (what the agent was asked to do)
{context}

## Attempts history
{attempts_block}

## Instructions
- Identify recurring problems across attempts (not one-off issues)
- Formulate targeted questions whose answers would resolve the ambiguities
- Propose concrete patches that can be applied to the agent spec before the next scaffold attempt
- Be specific — reference actual criterion names and observed failure patterns
- Return ONLY valid JSON, no markdown, no explanation

{{
  "mode": "diagnose",
  "problems": ["<recurring problem 1>", "<recurring problem 2>"],
  "questions": ["<targeted question 1>", "<targeted question 2>"],
  "proposed_patches": ["<actionable spec patch 1>", "<actionable spec patch 2>"]
}}
"""

REPROMPT = """\
Your previous response was not valid JSON. Return ONLY the JSON object, no markdown, no explanation.

Previous response:
{previous_response}
"""
