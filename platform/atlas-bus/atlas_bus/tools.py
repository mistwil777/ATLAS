"""Dependency manifest and health check for atlas-bus."""

from __future__ import annotations

_DEPENDENCY_MANIFEST: list[dict] = []


def get_manifest() -> list[dict]:
    return _DEPENDENCY_MANIFEST


def check_dependencies() -> dict[str, list[dict]]:
    """Check all atlas-bus dependencies.

    Returns dict with keys: missing, deprecated, untested_versions.
    atlas-bus has no external dependencies — always returns empty lists.
    """
    return {"missing": [], "deprecated": [], "untested_versions": []}
