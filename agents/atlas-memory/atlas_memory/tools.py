"""Dependency manifest and health check for atlas-memory."""

from __future__ import annotations

_DEPENDENCY_MANIFEST: list[dict] = [
    {
        "capability": "Vector search (Qdrant + fastembed)",
        "trigger": "all RAG operations (index, search)",
        "smoke_test": "atlas_memory.tests.smoke.test_memory",
        "tested_up_to": "1.12.0",
        "deprecated_since": None,
        "replaced_by": None,
        "primary": {
            "pip": "qdrant-client[fastembed]>=1.12,<2.0",
            "import": "qdrant_client",
            "system": [],
        },
        "alternatives": [],
    },
    {
        "capability": "Knowledge Graph (networkx)",
        "trigger": "all KG operations (register_agent, add_edge, query_composable, get_graph)",
        "smoke_test": "atlas_memory.tests.smoke.test_memory",
        "tested_up_to": "3.4.0",
        "deprecated_since": None,
        "replaced_by": None,
        "primary": {
            "pip": "networkx>=3.0,<4.0",
            "import": "networkx",
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
