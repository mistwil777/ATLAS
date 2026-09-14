"""ATLAS contract — dependency manifest and dependency checker for agent-tester.

agent-tester is itself an ATLAS agent: it exposes get_manifest(),
check_dependencies(), and the standard CLI commands (check / init / install).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import shutil

_DEPENDENCY_MANIFEST: list[dict] = [
    {
        "capability": "JSON schema validation (prompt contracts)",
        "trigger": "level-2 prompt contract tests",
        "smoke_test": "contract_validator.tests.smoke.test_contract_valid",
        "tested_up_to": "4.23.0",
        "deprecated_since": None,
        "replaced_by": None,
        "primary": {
            "pip": "jsonschema>=4.0,<5.0",
            "import": "jsonschema",
            "system": [],
        },
        "alternatives": [
            {"pip": "pydantic>=2.0,<3.0", "import": "pydantic", "system": []},
        ],
    },
    {
        "capability": "Behavioral golden tests (inspect-ai)",
        "trigger": "level-3 golden tests with a real LLM",
        "smoke_test": None,
        "tested_up_to": "0.3.45",
        "deprecated_since": None,
        "replaced_by": None,
        "primary": {
            "pip": "inspect-ai>=0.3,<1.0",
            "import": "inspect_ai",
            "system": [],
        },
        "alternatives": [],
    },
    {
        "capability": "Pytest test runner",
        "trigger": "running smoke tests of the target agent",
        "smoke_test": None,
        "tested_up_to": "8.3.5",
        "deprecated_since": None,
        "replaced_by": None,
        "primary": {
            "pip": "pytest>=8.0,<9.0",
            "import": "pytest",
            "system": [],
        },
        "alternatives": [],
    },
]


def get_manifest() -> list[dict]:
    return _DEPENDENCY_MANIFEST


def check_dependencies() -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {"missing": [], "deprecated": [], "untested_versions": []}

    for dep in _DEPENDENCY_MANIFEST:
        primary = dep["primary"]

        if not _can_import(primary["import"]):
            system_missing = [b for b in primary["system"] if shutil.which(b) is None]
            hint_parts = [f"pip install {primary['pip']}"]
            if system_missing:
                hint_parts.append(f"brew install {' '.join(system_missing)}")
            result["missing"].append(
                {
                    "capability": dep["capability"],
                    "trigger": dep["trigger"],
                    "pip": primary["pip"],
                    "system": primary["system"],
                    "install_hint": " && ".join(hint_parts),
                    "smoke_test": dep["smoke_test"],
                }
            )
            continue

        if dep.get("deprecated_since"):
            result["deprecated"].append(
                {
                    "capability": dep["capability"],
                    "deprecated_since": dep["deprecated_since"],
                    "replaced_by": dep.get("replaced_by"),
                }
            )
            continue

        tested = dep.get("tested_up_to")
        if tested:
            pkg_name = dep["primary"]["pip"].split(">=")[0].strip()
            installed = _installed_version(pkg_name)
            if installed and _version_newer_than(installed, tested):
                result["untested_versions"].append(
                    {
                        "capability": dep["capability"],
                        "tested_up_to": tested,
                        "installed_version": installed,
                        "smoke_test": dep["smoke_test"],
                    }
                )

    return result


def _can_import(module: str) -> bool:
    try:
        importlib.import_module(module.split(".")[0])
        return True
    except ImportError:
        return False


def _installed_version(package_name: str) -> str | None:
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
