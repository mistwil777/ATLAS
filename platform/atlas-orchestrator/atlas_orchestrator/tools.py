"""Dependency manifest and health check for atlas-orchestrator."""

from __future__ import annotations

_DEPENDENCY_MANIFEST: list[dict] = [
    {
        "capability": "LLM inference (AWS Bedrock)",
        "trigger": "CDC segmentation via cdc-router, evaluation via atlas-judge",
        "smoke_test": "atlas_orchestrator.tests.smoke.test_orchestrator",
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
    {
        "capability": "CDC segmentation",
        "trigger": "orchestration flow",
        "smoke_test": "atlas_orchestrator.tests.smoke.test_orchestrator",
        "tested_up_to": "1.0.0",
        "deprecated_since": None,
        "replaced_by": None,
        "primary": {
            "pip": "cdc-router",
            "import": "cdc_router",
            "system": [],
        },
        "alternatives": [],
    },
    {
        "capability": "Agent subprocess execution",
        "trigger": "bloc execution",
        "smoke_test": "atlas_orchestrator.tests.smoke.test_orchestrator",
        "tested_up_to": "1.0.0",
        "deprecated_since": None,
        "replaced_by": None,
        "primary": {
            "pip": "atlas-bus",
            "import": "atlas_bus",
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
