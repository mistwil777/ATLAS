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
- Return ONLY valid JSON, no markdown, no explanation

{{
  "mode": "pointwise",
  "criteria_scores": [
    {{"criterion": "<name>", "score": <1-5>, "comment": "<specific observation>"}}
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

REPROMPT = """\
Your previous response was not valid JSON. Return ONLY the JSON object, no markdown, no explanation.

Previous response:
{previous_response}
"""
