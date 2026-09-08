"""Dependency manifest and health check for agent-forge."""

from __future__ import annotations


_DEPENDENCY_MANIFEST: list[dict] = [
    {
        "capability":       "Agent scaffolding templating",
        "trigger":          "every generate call",
        "smoke_test":       "agent_forge.tests.smoke.test_generate",
        "tested_up_to":     "2.6.0",
        "deprecated_since": None,
        "replaced_by":      None,
        "primary": {
            "pip":    "cookiecutter>=2.5,<3.0",
            "import": "cookiecutter",
            "system": [],
        },
        "alternatives": [],
    },
    {
        "capability":       "LLM spec extraction (Bedrock)",
        "trigger":          "generate --spec <file>",
        "smoke_test":       None,
        "tested_up_to":     "1.35.0",
        "deprecated_since": None,
        "replaced_by":      None,
        "primary": {
            "pip":    "boto3>=1.35,<2.0",
            "import": "boto3",
            "system": [],
        },
        "alternatives": [],
    },
]


def get_manifest() -> list[dict]:
    return _DEPENDENCY_MANIFEST


def check_dependencies() -> dict[str, list[dict]]:
    """Check all agent-forge dependencies.

    Returns dict with keys: missing, deprecated, untested_versions.
    """
    import importlib
    import importlib.metadata

    result: dict[str, list[dict]] = {"missing": [], "deprecated": [], "untested_versions": []}

    for dep in _DEPENDENCY_MANIFEST:
        primary = dep["primary"]

        if not _can_import(primary["import"]):
            result["missing"].append({
                "capability":   dep["capability"],
                "trigger":      dep["trigger"],
                "pip":          primary["pip"],
                "system":       primary["system"],
                "install_hint": f"pip install {primary['pip']}",
                "smoke_test":   dep["smoke_test"],
            })
            continue

        if dep.get("deprecated_since"):
            result["deprecated"].append({
                "capability":       dep["capability"],
                "deprecated_since": dep["deprecated_since"],
                "replaced_by":      dep.get("replaced_by"),
            })
            continue

        tested = dep.get("tested_up_to")
        if tested:
            pkg_name  = primary["pip"].split(">=")[0].strip()
            installed = _installed_version(pkg_name)
            if installed and _version_newer_than(installed, tested):
                result["untested_versions"].append({
                    "capability":        dep["capability"],
                    "tested_up_to":      tested,
                    "installed_version": installed,
                    "smoke_test":        dep["smoke_test"],
                })

    return result


def _can_import(module: str) -> bool:
    import importlib
    try:
        importlib.import_module(module.split(".")[0])
        return True
    except ImportError:
        return False


def _installed_version(package_name: str) -> str | None:
    import importlib.metadata
    try:
        return importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _version_newer_than(installed: str, reference: str) -> bool:
    try:
        def to_tuple(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in v.split(".")[:3] if x.isdigit())
        return to_tuple(installed) > to_tuple(reference)
    except Exception:  # noqa: BLE001
        return False
