"""Evaluation criteria for agent-forge spec extraction output."""

CRITERIA: list[str] = [
    "Output is valid JSON containing an object (not an array or primitive).",
    "The 'agent_name' field is present and is a non-empty string.",
    "The 'description' field is present and describes the agent's purpose.",
    "The 'uses_llm' field is present and its value can be interpreted as a boolean.",
    "No required field (agent_name, description, input_type, output_type, uses_llm) is missing or null.",
]
