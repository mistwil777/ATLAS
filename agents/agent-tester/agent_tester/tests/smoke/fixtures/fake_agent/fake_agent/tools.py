"""Minimal tools.py satisfying the ATLAS agent contract."""

from __future__ import annotations

_DEPENDENCY_MANIFEST: list[dict] = [
    {
        "capability":      "Fake capability",
        "trigger":         "always",
        "smoke_test":      "fake_agent.tests.smoke.test_fake",
        "tested_up_to":    "1.0.0",
        "deprecated_since": None,
        "replaced_by":     None,
        "primary":     {"pip": "pytest>=8.0", "import": "pytest", "system": []},
        "alternatives": [],
    },
]


def get_manifest() -> list[dict]:
    return _DEPENDENCY_MANIFEST


def check_dependencies() -> dict[str, list[dict]]:
    return {"missing": [], "deprecated": [], "untested_versions": []}
