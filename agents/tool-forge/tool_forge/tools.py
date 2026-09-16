"""Dependency manifest and health check for tool-forge."""

from __future__ import annotations

_DEPENDENCY_MANIFEST: list[dict] = []


def get_manifest() -> list[dict]:
    return _DEPENDENCY_MANIFEST


def check_dependencies() -> dict[str, list[dict]]:
    return {"missing": [], "deprecated": [], "untested_versions": []}
