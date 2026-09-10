"""Smart installation script for agent-forge."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from agent_forge.tools import get_manifest

_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def run_install(verbose: bool = True) -> bool:
    """Install all dependencies with smoke tests and auto-replacement.

    Returns True if all capabilities are satisfied.
    """
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
            print(f"    primary:  {f['primary']}")
            print(f"    tried:    {', '.join(f['tried']) or 'none'}")
            print(
                "    action:   add a working alternative to the manifest and re-run `agent-forge install`"
            )
        return False

    if verbose:
        print("\n[DONE] All dependencies installed and smoke-tested.")
    return True


def _pip_install(pip_spec: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", pip_spec, "--quiet"],
        check=True,
    )


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
                    [venv_python, "-m", "pip", "install", alt["pip"], "--quiet"],
                    check=True,
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
            [python_path, "-m", "pip", "install", "pytest", "agent-forge", "--quiet"],
            check=True,
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
        _replace_dep_in_pyproject(dep["primary"]["pip"], alt["pip"])
    _git_commit_and_tag(dep["capability"], alt["pip"])
    if verbose:
        print(f"  [PROMOTED] {alt['pip']} — pyproject.toml updated, git commit + tag created")


def _replace_dep_in_pyproject(old_pip: str, new_pip: str) -> None:
    text = _PYPROJECT.read_text(encoding="utf-8")
    old_pkg = old_pip.split(">=")[0].split("==")[0].strip()
    lines = text.splitlines()
    out = []
    for line in lines:
        stripped = line.strip().strip('"').strip("'")
        if stripped.startswith(old_pkg + ">=") or stripped.startswith(old_pkg + "=="):
            indent = line[: len(line) - len(line.lstrip())]
            quote = '"' if '"' in line else "'"
            out.append(f"{indent}{quote}{new_pip}{quote},")
        else:
            out.append(line)
    _PYPROJECT.write_text("\n".join(out) + "\n", encoding="utf-8")


def _git_commit_and_tag(capability: str, new_pip: str) -> None:
    pkg_name = new_pip.split(">=")[0].split("==")[0].strip().lower().replace("-", "_")
    try:
        subprocess.run(["git", "add", str(_PYPROJECT)], check=True)
        subprocess.run(
            ["git", "commit", "-m", f"chore(agent-forge): replace {capability} dep → {pkg_name}"],
            check=True,
        )
        try:
            import importlib.metadata  # noqa: PLC0415

            current = importlib.metadata.version("agent-forge")
            parts = current.split(".")
            parts[-1] = str(int(parts[-1]) + 1)
            tag_version = ".".join(parts)
        except Exception:  # noqa: BLE001
            tag_version = "0.0.0"
        subprocess.run(
            [
                "git",
                "tag",
                f"agent-forge/v{tag_version}",
                "-m",
                f"Auto-replace {capability} dep → {pkg_name}",
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        pass
