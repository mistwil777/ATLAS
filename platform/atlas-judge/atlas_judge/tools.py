"""Dependency manifest and health check for atlas-judge."""

from __future__ import annotations

_DEPENDENCY_MANIFEST: list[dict] = [
    {
        "capability": "LLM inference (AWS Bedrock)",
        "trigger": "all evaluation operations (pointwise, pairwise, reference)",
        "smoke_test": "atlas_judge.tests.smoke.test_judge",
        "tested_up_to": "1.35.0",
        "deprecated_since": None,
        "replaced_by": None,
        "primary": {
            "pip": "boto3>=1.35,<2.0",
            "import": "boto3",
            "system": [],
        },
        "alternatives": [],
    },
]


def get_manifest() -> list[dict]:
    return _DEPENDENCY_MANIFEST


def check_dependencies() -> dict[str, list[dict]]:
    missing = []
    for entry in _DEPENDENCY_MANIFEST:
        try:
            __import__(entry["primary"]["import"])
        except ImportError:
            missing.append(entry)
    return {"missing": missing, "deprecated": [], "untested_versions": []}
