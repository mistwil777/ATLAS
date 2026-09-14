"""Level 1 — Structural contract validator.

Checks that an agent directory conforms to the ATLAS agent contract:
  - Has pyproject.toml with required fields
  - Package directory exists with tools.py (get_manifest + check_dependencies)
  - Has at least one smoke test
  - CLI commands 'check' and 'init' are implemented in __main__.py
  - Manifest entries have the required fields

Usage:
    from contract_validator.contract import validate_contract
    report = validate_contract(Path("agents/pair-forge"))
    if not report.passed:
        for v in report.violations:
            print(f"[VIOLATION] {v}")
"""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── Required fields per manifest entry ────────────────────────────────────────

_REQUIRED_MANIFEST_FIELDS = {
    "capability",
    "primary",
    "alternatives",
    "smoke_test",
    "tested_up_to",
}

_REQUIRED_PRIMARY_FIELDS = {"pip", "import", "system"}

_REQUIRED_PYPROJECT_FIELDS = {"name", "version", "requires-python"}

_REQUIRED_CLI_COMMANDS = {"check", "init"}


@dataclass
class ContractReport:
    agent_path: Path
    agent_name: str
    passed: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_contract(agent_path: Path) -> ContractReport:
    """Validate an agent directory against the ATLAS contract.

    Returns a ContractReport with passed=True if all required checks pass.
    Warnings are informational — they do not fail the contract.
    """
    path = Path(agent_path).resolve()
    violations: list[str] = []
    warnings: list[str] = []

    # 1. pyproject.toml
    pyproject = path / "pyproject.toml"
    if not pyproject.exists():
        violations.append("Missing pyproject.toml")
        return ContractReport(path, path.name, False, violations, warnings)

    pkg_name = _check_pyproject(pyproject, violations, warnings)
    if not pkg_name:
        return ContractReport(path, path.name, False, violations, warnings)

    pkg_dir = path / pkg_name
    if not pkg_dir.is_dir():
        violations.append(f"Package directory '{pkg_name}/' not found")
        return ContractReport(path, path.name, False, violations, warnings)

    # 2. tools.py with required functions
    _check_tools_py(pkg_dir, violations, warnings)

    # 3. Smoke tests
    _check_smoke_tests(pkg_dir, violations, warnings)

    # 4. CLI (__main__.py with check + init)
    _check_main_py(pkg_dir, violations, warnings)

    # 5. Manifest entries (static analysis)
    _check_manifest_fields(pkg_dir / "tools.py", violations, warnings)

    # 6. CLI execution check (subprocess)
    _check_cli_execution(pkg_dir, pkg_name, violations, warnings)

    passed = len(violations) == 0
    return ContractReport(path, pkg_name, passed, violations, warnings)


# ── Internal checkers ──────────────────────────────────────────────────────────


def _check_pyproject(path: Path, violations: list, warnings: list) -> str | None:
    try:
        import tomllib  # noqa: PLC0415
    except ImportError:
        try:
            import tomli as tomllib  # noqa: PLC0415
        except ImportError:
            warnings.append("Cannot parse pyproject.toml — tomllib/tomli not available")
            return _guess_pkg_name(path.parent)

    with path.open("rb") as f:
        data = tomllib.load(f)

    project = data.get("project", {})
    for field_name in _REQUIRED_PYPROJECT_FIELDS:
        if field_name not in project:
            violations.append(f"pyproject.toml missing field: [project].{field_name}")

    name = project.get("name", "")
    if not name:
        return None

    # Convention: package name = project name with hyphens → underscores
    return name.replace("-", "_")


def _guess_pkg_name(agent_path: Path) -> str | None:
    for p in agent_path.iterdir():
        if p.is_dir() and (p / "__init__.py").exists() and p.name != "tests":
            return p.name
    return None


def _check_tools_py(pkg_dir: Path, violations: list, warnings: list) -> None:
    tools_py = pkg_dir / "tools.py"
    if not tools_py.exists():
        violations.append(f"Missing {pkg_dir.name}/tools.py")
        return

    source = tools_py.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fns = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    if "get_manifest" not in fns:
        violations.append(f"{pkg_dir.name}/tools.py missing function: get_manifest()")
    if "check_dependencies" not in fns:
        violations.append(f"{pkg_dir.name}/tools.py missing function: check_dependencies()")


def _check_smoke_tests(pkg_dir: Path, violations: list, warnings: list) -> None:
    smoke_dir = pkg_dir / "tests" / "smoke"
    if not smoke_dir.is_dir():
        violations.append(f"Missing {pkg_dir.name}/tests/smoke/ directory")
        return

    tests = list(smoke_dir.glob("test_*.py"))
    if not tests:
        violations.append(f"{pkg_dir.name}/tests/smoke/ has no test_*.py files")
    else:
        fixtures = smoke_dir / "fixtures"
        if not fixtures.is_dir():
            warnings.append(
                f"{pkg_dir.name}/tests/smoke/fixtures/ missing — smoke tests may lack reference files"
            )


def _check_main_py(pkg_dir: Path, violations: list, warnings: list) -> None:
    main_py = pkg_dir / "__main__.py"
    if not main_py.exists():
        violations.append(f"Missing {pkg_dir.name}/__main__.py")
        return

    source = main_py.read_text(encoding="utf-8")
    for cmd in _REQUIRED_CLI_COMMANDS:
        if f'"{cmd}"' not in source and f"'{cmd}'" not in source:
            violations.append(f"{pkg_dir.name}/__main__.py does not implement CLI command: {cmd}")


def _check_manifest_fields(tools_py: Path, violations: list, warnings: list) -> None:
    if not tools_py.exists():
        return

    source = tools_py.read_text(encoding="utf-8")

    for field_name in _REQUIRED_MANIFEST_FIELDS:
        if f'"{field_name}"' not in source and f"'{field_name}'" not in source:
            violations.append(f"tools.py manifest appears to be missing field: '{field_name}'")


def _check_cli_execution(pkg_dir: Path, pkg_name: str, violations: list, warnings: list) -> None:
    result = subprocess.run(
        [sys.executable, "-m", pkg_name, "check"],
        cwd=str(pkg_dir.parent),
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        violations.append(
            f"`python -m {pkg_name} check` exited with code {result.returncode} — "
            f"stderr: {result.stderr[:200]}"
        )
