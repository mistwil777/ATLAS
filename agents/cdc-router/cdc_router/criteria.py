"""Evaluation criteria for cdc-router outputs — used by atlas-judge."""

CRITERIA = {
    "binary_rules": (
        "The ia/code decision follows the binary rules strictly: "
        "ia for unstructured natural language input or semantic interpretation tasks, "
        "code for deterministic transformations on known structured formats."
    ),
    "json_schema_valid": (
        "All required fields are present and correctly typed: "
        "bloc (str), type ('ia'|'code'), agent_suggested (str|null), justification (str)."
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
}
