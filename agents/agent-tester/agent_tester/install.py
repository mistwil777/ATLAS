"""Smart installation for agent-tester — same pattern as pair-forge."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from agent_tester.tools import get_manifest

_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def run_install(verbose: bool = True) -> bool:
    manifest = get_manifest()
    failures: list[dict] = []

    for dep in manifest:
        capability = dep["capability"]
        primary = dep["primary"]
        smoke_mod = dep["smoke_test"]

        if verbose:
            print(f"\n[INSTALL] {capability}")

        _pip_install(primary["pip"])

        if smoke_mod and not _run_smoke(smoke_mod):
            if verbose:
                print("  [FAIL] primary tool failed smoke test")
            resolved = _try_alternatives(dep, verbose)
            if resolved:
                _promote_alternative(dep, resolved, verbose)
            else:
                failures.append(
                    {
                        "capability": capability,
                        "primary": primary["pip"],
                        "tried": [a["pip"] for a in dep.get("alternatives", [])],
                    }
                )
                continue

        if verbose:
            print("  [OK]")

    if failures:
        print("\n[BLOCKED] The following capabilities could not be installed:\n")
        for f in failures:
            print(f"  - {f['capability']}")
            print(f"    primary: {f['primary']}")
            print(f"    tried:   {', '.join(f['tried']) or 'none'}")
        return False

    if verbose:
        print("\n[DONE] All dependencies installed and smoke-tested.")
    return True


def _pip_install(pip_spec: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", pip_spec, "--quiet"], check=True)


def _run_smoke(module_path: str) -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--tb=short", "-q", "--pyargs", module_path],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _try_alternatives(dep: dict, verbose: bool) -> dict | None:
    for alt in dep.get("alternatives", []):
        if verbose:
            print(f"  [TRY] {alt['pip']}")
        with tempfile.TemporaryDirectory() as tmp_dir:
            venv_python = _create_isolated_venv(tmp_dir)
            if venv_python is None:
                continue
            try:
                subprocess.run(
                    [venv_python, "-m", "pip", "install", alt["pip"], "--quiet"], check=True
                )
                ok = _run_smoke_in_venv(venv_python, dep["smoke_test"])
            except Exception:  # noqa: BLE001
                ok = False
        if ok:
            if verbose:
                print(f"  [PASS] {alt['pip']}")
            return alt
        if verbose:
            print(f"  [FAIL] {alt['pip']}")
    return None


def _create_isolated_venv(base_dir: str) -> str | None:
    venv_path = Path(base_dir) / "test_venv"
    try:
        subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True)
        python = venv_path / "bin" / "python"
        if not python.exists():
            python = venv_path / "Scripts" / "python.exe"
        return str(python) if python.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _run_smoke_in_venv(python_path: str, module_path: str) -> bool:
    try:
        subprocess.run(
            [python_path, "-m", "pip", "install", "pytest", "agent-tester", "--quiet"], check=True
        )
        result = subprocess.run(
            [python_path, "-m", "pytest", "--tb=short", "-q", "--pyargs", module_path],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _promote_alternative(dep: dict, alt: dict, verbose: bool) -> None:
    _pip_install(alt["pip"])
    if _PYPROJECT.exists():
        text = _PYPROJECT.read_text(encoding="utf-8")
        old_pkg = dep["primary"]["pip"].split(">=")[0].strip()
        lines = []
        for line in text.splitlines():
            stripped = line.strip().strip('"').strip("'")
            if stripped.startswith(old_pkg + ">=") or stripped.startswith(old_pkg + "=="):
                indent = line[: len(line) - len(line.lstrip())]
                quote = '"' if '"' in line else "'"
                lines.append(f'{indent}{quote}{alt["pip"]}{quote},')
            else:
                lines.append(line)
        _PYPROJECT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if verbose:
        print(f"  [PROMOTED] {alt['pip']}")
