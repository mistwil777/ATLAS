"""Evaluation criteria for cdc-router outputs — used by atlas-judge."""

CRITERIA = {
    "binary_rules": (
        "The ia/tool/code decision follows the three-type rules strictly: "
        "ia for unstructured natural language input or semantic interpretation tasks requiring LLM reasoning; "
        "tool for fully deterministic, reusable, unit-testable functions; "
        "code for trivial project-specific glue (1-3 lines, not reusable). "
        "No name appears as both ia and tool across blocs."
    ),
    "json_schema_valid": (
        "All required fields are present and correctly typed: "
        "bloc (str), type ('ia'|'tool'|'code'), agent_suggested (str|null), "
        "called_by (str|null — required for tool blocs), justification (str), "
        "criteria (non-empty list of strings)."
    ),
    "justification_coherent": (
        "Each justification explicitly references the binary rule that was applied "
        "and relates it to the specific content of the bloc."
    ),
    "no_hallucination": (
        "No agent is suggested that is not present in the provided KG "
        "or clearly described in the CDC."
    ),
    "completeness": (
        "All functional blocs described in the CDC are addressed in the output — "
        "nothing is silently skipped."
    ),
    "criteria_quality": (
        "Each bloc contains 2 to 4 evaluation criteria. "
        "Each criterion is a specific, verifiable assertion starting with a verb "
        "(Output must, Response must, All fields must...). "
        "Criteria are derived from the bloc content — never generic."
    ),
}
