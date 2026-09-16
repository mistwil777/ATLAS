"""Prompts for tool-forge LLM calls."""

TOOL_GENERATION_PROMPT = """\
You are an expert Python developer. Generate a clean, reusable Python tool library \
from the functional specification below.

## Rules
- Pure functions only — no side effects, no global state, no LLM calls
- Full type hints on all parameters and return values
- One-line docstring per function (what it does, not how)
- Handle edge cases explicitly (empty input, None, wrong type) — raise ValueError with a clear message
- No external dependencies unless explicitly required by the spec
- The module must be importable standalone

## Spec
{spec}

## Output format — return ONLY valid JSON, no markdown, no explanation

{{
  "module_name": "<snake_case module name, e.g. output_formatter>",
  "description": "<one sentence describing the tool's purpose>",
  "functions": [
    {{
      "name": "<function_name>",
      "signature": "<def function_name(param: type, ...) -> return_type:>",
      "docstring": "<one-line description>",
      "body": "<full function body, indented with 4 spaces, no def line>"
    }}
  ],
  "imports": ["<import statement 1>", "<import statement 2>"],
  "tests": [
    {{
      "function": "<function_name>",
      "description": "<what this test verifies>",
      "code": "<full pytest test function body, indented with 4 spaces, no def line>"
    }}
  ]
}}
"""

REPROMPT = """\
Your previous response was not valid JSON. Return ONLY the JSON object, no markdown, no explanation.

Previous response:
{previous_response}
"""
