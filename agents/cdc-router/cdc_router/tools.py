"""Dependency manifest and health check for cdc-router."""

from __future__ import annotations

_DEPENDENCY_MANIFEST: list[dict] = [
    {
        "capability": "LLM inference (AWS Bedrock)",
        "trigger": "CDC segmentation",
        "smoke_test": "cdc_router.tests.smoke.test_cdc_router",
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
        imp = entry["primary"]["import"]
        try:
            __import__(imp)
        except ImportError:
            missing.append(entry)
    return {"missing": missing, "deprecated": [], "untested_versions": []}
