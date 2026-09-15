"""Pure-Python agent scaffolding generator — no external dependencies."""

from __future__ import annotations

from pathlib import Path


def _class_name(pkg_name: str) -> str:
    return pkg_name.replace("_", " ").title().replace(" ", "")


def generate(params: dict, output_dir: Path) -> Path:
    """Generate an agent scaffold from params dict.

    Params keys: agent_slug, pkg_name, description, input_type,
                 output_type, domain, uses_llm (bool).
    Returns the root path of the generated agent.
    """
    slug = params["agent_slug"]
    pkg = params["pkg_name"]
    cls = _class_name(pkg)
    desc = params.get("description", "An ATLAS agent.")
    inp = params.get("input_type", "raw data")
    out = params.get("output_type", "processed output")
    domain = params.get("domain", "general")
    uses_llm = bool(params.get("uses_llm", False))

    root = output_dir / slug
    pkg_dir = root / pkg
    test_dir = pkg_dir / "tests" / "smoke" / "fixtures"
    test_dir.mkdir(parents=True, exist_ok=True)

    _write(root / "pyproject.toml", _pyproject(slug, pkg, cls, desc))
    _write(pkg_dir / "__init__.py", _init(slug, desc))
    _write(pkg_dir / "__main__.py", _main(slug, pkg, cls))
    _write(pkg_dir / "agent.py", _agent(slug, pkg, cls, inp, out, domain, uses_llm))
    # collect.py no longer generated — agents import from atlas_core
    _write(pkg_dir / "docker.py", _docker(slug, pkg))
    _write(pkg_dir / "install.py", _install(slug, pkg))
    _write(pkg_dir / "prompts.py", _prompts(slug, domain, out))
    _write(pkg_dir / "tools.py", _tools(slug, pkg))
    _write(pkg_dir / "tests" / "__init__.py", "")
    _write(pkg_dir / "tests" / "smoke" / "__init__.py", "")
    _write(pkg_dir / "tests" / "smoke" / "test_smoke.py", _test_smoke(slug, pkg, cls))
    (test_dir / ".gitkeep").touch()

    return root


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# ── Templates ─────────────────────────────────────────────────────────────────


def _pyproject(slug: str, pkg: str, cls: str, desc: str) -> str:
    return f"""\
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{slug}"
version = "0.1.0"
description = "{desc}"
requires-python = ">=3.10"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
{slug} = "{pkg}.__main__:main"

[project.entry-points."atlas.agents"]
{cls} = "{pkg}.agent:{cls}Agent"

[tool.setuptools.packages.find]
where = ["."]
include = ["{pkg}*"]

[tool.setuptools.package-data]
"{pkg}.tests.smoke.fixtures" = ["*"]
"""


def _init(slug: str, desc: str) -> str:
    return f'"""{slug} — {desc}"""\n\n__version__ = "0.1.0"\n'


def _main(slug: str, pkg: str, cls: str) -> str:
    return f'''\
"""Entry point for `python -m {pkg}` and the `{slug}` CLI."""

from __future__ import annotations

import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"

    if command == "init":
        from {pkg}.docker import generate_dockerfile  # noqa: PLC0415
        path = generate_dockerfile()
        print(f"Dockerfile generated: {{path}}")

    elif command == "check":
        from {pkg}.tools import check_dependencies  # noqa: PLC0415
        result = check_dependencies()
        if not any(result.values()):
            print("All dependencies present and up to date.")
            return
        for item in result.get("missing", []):
            print(f"[MISSING]   {{item['capability']}} — {{item['install_hint']}}")
        for item in result.get("deprecated", []):
            print(f"[DEPRECATED] {{item['capability']}} — replaced by: {{item['replaced_by']}}")
        for item in result.get("untested_versions", []):
            print(f"[UNTESTED]  {{item['capability']}} — tested up to {{item['tested_up_to']}}, found {{item['installed_version']}}")

    elif command == "install":
        from {pkg}.install import run_install  # noqa: PLC0415
        run_install()

    elif command == "run":
        from {pkg}.agent import {cls}Agent  # noqa: PLC0415
        # TODO: parse arguments and run the agent
        print("{slug} run — not yet implemented")

    else:
        print("{slug} commands:")
        print("  run      — run the agent")
        print("  init     — generate Dockerfile from dependency manifest")
        print("  check    — check all dependencies status")
        print("  install  — run smart installation with smoke tests")


if __name__ == "__main__":
    main()
'''


def _agent(slug: str, pkg: str, cls: str, inp: str, out: str, domain: str, uses_llm: bool) -> str:
    llm_import = f"from {pkg}.prompts import MAIN_PROMPT\n" if uses_llm else ""
    llm_param_doc = (
        "        llm_fn:  callable(prompt, cfg, max_tokens) -> str | None  — injected by ATLAS Bus\n"
        if uses_llm
        else ""
    )
    llm_param_init = "        llm_fn=None,\n" if uses_llm else ""
    llm_cfg_init   = "        llm_cfg=None,\n" if uses_llm else ""
    llm_self       = "        self._llm     = llm_fn\n        self._llm_cfg = llm_cfg or {{}}\n" if uses_llm else ""

    llm_call = ""
    if uses_llm:
        llm_call = """\
        if self._llm is None:
            yield self._error("llm_fn not provided — agent requires an LLM")
            return
        prompt   = MAIN_PROMPT.format(input=cdc_bloc)
        response = self._llm(prompt, self._llm_cfg, 1024)
        if not response:
            yield self._error("LLM returned no output")
            return
        yield {"summary": True, "output": response}"""
    else:
        llm_call = """\
        # TODO: implement agent logic here using cdc_bloc and context
        yield {"summary": True, "output": cdc_bloc}"""

    return f'''\
"""{slug} — ATLAS Bus-compatible agent.

Input:  {inp}
Output: {out}
Domain: {domain}

Bus contract: run(cdc_bloc, context) → Iterator[dict]
Final dict must contain key "summary": True.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

{llm_import}
def _default_log(level: str, message: str) -> None:
    getattr(logging.getLogger("{pkg}"), level,
            logging.getLogger("{pkg}").info)(message)


class {cls}Agent:
    """{domain} agent — ATLAS Bus-compatible.

    Args:
        config:  Configuration dict (forwarded from atlas.toml [llm] section).
{llm_param_doc}        log_fn:  callable(level, message) -> None
    """

    def __init__(
        self,
        config: dict | None = None,
{llm_param_init}{llm_cfg_init}        log_fn=None,
    ) -> None:
        self.config  = config or {{}}
{llm_self}        self._log    = log_fn or _default_log

    def run(self, cdc_bloc: str, context: str = "") -> Iterator[dict]:
        """Process a CDC bloc and yield log dicts.

        Args:
            cdc_bloc: The CDC bloc text assigned to this agent.
            context:  Full CDC text for additional context.
        Final dict must contain key "summary": True.
        """
        yield self._info(f"Starting {cls}Agent")
        {llm_call}

    def _info(self, msg: str)  -> dict: return {{"level": "info",    "msg": msg}}
    def _warn(self, msg: str)  -> dict: return {{"level": "warning", "msg": msg}}
    def _error(self, msg: str) -> dict: return {{"level": "error",   "msg": msg}}
'''


def _docker(slug: str, pkg: str) -> str:
    return f'''\
"""Generate a Dockerfile from the {slug} dependency manifest."""

from __future__ import annotations

import re
from pathlib import Path

from {pkg}.tools import get_manifest


_OUTPUT_PATH = Path("Dockerfile.{slug}")

_PIP_SYSTEM_EXTRAS: dict[str, list[str]] = {{
    "pytesseract": ["tesseract-ocr"],
    "pdf2image":   ["poppler-utils"],
}}


def generate_dockerfile(output_path: Path | None = None) -> Path:
    manifest    = get_manifest()
    active_pips = _collect_active_pip_specs(manifest)
    system_pkgs = _collect_system_packages(active_pips)
    content     = _render(active_pips, system_pkgs)
    out         = output_path or _OUTPUT_PATH
    out.write_text(content, encoding="utf-8")
    return out


def _collect_active_pip_specs(manifest: list[dict]) -> list[str]:
    seen:  set[str] = set()
    specs: list[str] = []
    for dep in manifest:
        spec = dep["primary"]["pip"]
        pkg  = _pkg_name(spec)
        if pkg not in seen:
            seen.add(pkg)
            specs.append(spec)
    return specs


def _collect_system_packages(pip_specs: list[str]) -> list[str]:
    apt: list[str] = []
    for spec in pip_specs:
        pkg = _pkg_name(spec)
        for apt_pkg in _PIP_SYSTEM_EXTRAS.get(pkg, []):
            if apt_pkg not in apt:
                apt.append(apt_pkg)
    return apt


def _pkg_name(spec: str) -> str:
    return re.split(r"[>=<!,\\[]", spec)[0].strip().lower().replace("-", "_")


def _render(pip_specs: list[str], system_pkgs: list[str]) -> str:
    lines = ["FROM python:3.10-slim", "", "WORKDIR /app", ""]

    if system_pkgs:
        apt_line = " ".join(system_pkgs)
        lines += [
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\\",
            f"    {{apt_line}} \\\\",
            "    && rm -rf /var/lib/apt/lists/*",
            "",
        ]

    if pip_specs:
        pip_args = " \\\\\\n    ".join(f\'"{{s}}"\' for s in pip_specs)
        lines += [
            "RUN pip install --no-cache-dir \\\\",
            f"    {{pip_args}}",
            "",
        ]

    lines += [
        "COPY . .",
        "",
        f\'CMD ["python", "-m", "{pkg}", "--help"]\',
    ]
    return "\\n".join(lines) + "\\n"
'''


def _install(slug: str, pkg: str) -> str:
    return f'''\
"""Smart installation script for {slug}."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from {pkg}.tools import get_manifest


_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def run_install(verbose: bool = True) -> bool:
    """Install all dependencies with smoke tests and auto-replacement.

    Returns True if all capabilities are satisfied.
    """
    manifest  = get_manifest()
    failures: list[dict] = []

    for dep in manifest:
        capability = dep["capability"]
        primary    = dep["primary"]
        smoke_mod  = dep["smoke_test"]

        if verbose:
            print(f"\\n[INSTALL] {{capability}}")

        _pip_install(primary["pip"])

        if smoke_mod and not _run_smoke(smoke_mod):
            if verbose:
                print("  [FAIL] primary tool failed smoke test")
            resolved = _try_alternatives(dep, verbose)
            if resolved:
                _promote_alternative(dep, resolved, verbose)
            else:
                failures.append({{
                    "capability": capability,
                    "primary":    primary["pip"],
                    "tried":      [a["pip"] for a in dep.get("alternatives", [])],
                }})
                continue

        if verbose:
            print("  [OK]")

    if failures:
        print("\\n[BLOCKED] The following capabilities could not be installed:\\n")
        for f in failures:
            print(f"  - {{f['capability']}}")
            print(f"    primary:  {{f['primary']}}")
            print(f"    tried:    {{', '.join(f['tried']) or 'none'}}")
        return False

    if verbose:
        print("\\n[DONE] All dependencies installed and smoke-tested.")
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
            capture_output=True, text=True,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _try_alternatives(dep: dict, verbose: bool) -> dict | None:
    for alt in dep.get("alternatives", []):
        if verbose:
            print(f"  [TRY] {{alt['pip']}}")
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
                print(f"  [PASS] {{alt['pip']}}")
            return alt
        if verbose:
            print(f"  [FAIL] {{alt['pip']}}")
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
            [python_path, "-m", "pip", "install", "pytest", "{slug}", "--quiet"],
            check=True,
        )
        result = subprocess.run(
            [python_path, "-m", "pytest", "--tb=short", "-q", "--pyargs", module_path],
            capture_output=True, text=True,
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
        print(f"  [PROMOTED] {{alt['pip']}} — pyproject.toml updated, git commit + tag created")


def _replace_dep_in_pyproject(old_pip: str, new_pip: str) -> None:
    text    = _PYPROJECT.read_text(encoding="utf-8")
    old_pkg = old_pip.split(">=")[0].split("==")[0].strip()
    lines   = text.splitlines()
    out     = []
    for line in lines:
        stripped = line.strip().strip(\'"\').strip("\'")
        if stripped.startswith(old_pkg + ">=") or stripped.startswith(old_pkg + "=="):
            indent = line[: len(line) - len(line.lstrip())]
            quote  = \'"\' if \'"\' in line else "\'"
            out.append(f\'{{indent}}{{quote}}{{new_pip}}{{quote}},\')
        else:
            out.append(line)
    _PYPROJECT.write_text("\\n".join(out) + "\\n", encoding="utf-8")


def _git_commit_and_tag(capability: str, new_pip: str) -> None:
    pkg_name = new_pip.split(">=")[0].split("==")[0].strip().lower().replace("-", "_")
    try:
        subprocess.run(["git", "add", str(_PYPROJECT)], check=True)
        subprocess.run(
            ["git", "commit", "-m",
             f"chore({slug}): replace {{capability}} dep -> {{pkg_name}}"],
            check=True,
        )
        try:
            import importlib.metadata  # noqa: PLC0415
            current = importlib.metadata.version("{slug}")
            parts   = current.split(".")
            parts[-1] = str(int(parts[-1]) + 1)
            tag_version = ".".join(parts)
        except Exception:  # noqa: BLE001
            tag_version = "0.0.0"
        subprocess.run(
            ["git", "tag", f"{slug}/v{{tag_version}}", "-m",
             f"Auto-replace {{capability}} dep -> {{pkg_name}}"],
            check=True,
        )
    except subprocess.CalledProcessError:
        pass
'''


def _prompts(slug: str, domain: str, out: str) -> str:
    return f'''\
"""Prompt templates for {slug}."""

from __future__ import annotations


MAIN_PROMPT = """\\
You are an agent specialized in {domain}.

Input: {{input}}

Instructions:
- TODO: define what the agent should do with the input
- Output format: {out}

Output:"""
'''


def _tools(slug: str, pkg: str) -> str:
    return f'''\
"""Dependency manifest and health check for {slug}."""

from __future__ import annotations


_DEPENDENCY_MANIFEST: list[dict] = [
    # TODO: declare capabilities and their dependencies
    # Example:
    # {{
    #     "capability":       "PDF text extraction",
    #     "trigger":          "files with .pdf extension",
    #     "smoke_test":       "{pkg}.tests.smoke.test_pdf",
    #     "tested_up_to":     "0.11.4",
    #     "deprecated_since": None,
    #     "replaced_by":      None,
    #     "primary": {{
    #         "pip":    "pdfplumber>=0.11,<2.0",
    #         "import": "pdfplumber",
    #         "system": [],
    #     }},
    #     "alternatives": [],
    # }},
]


def get_manifest() -> list[dict]:
    return _DEPENDENCY_MANIFEST


def check_dependencies() -> dict[str, list[dict]]:
    """Check all agent dependencies.

    Returns dict with keys: missing, deprecated, untested_versions.
    """
    import importlib
    import importlib.metadata

    result: dict[str, list[dict]] = {{"missing": [], "deprecated": [], "untested_versions": []}}

    for dep in _DEPENDENCY_MANIFEST:
        primary = dep["primary"]

        if not _can_import(primary["import"]):
            result["missing"].append({{
                "capability":   dep["capability"],
                "trigger":      dep["trigger"],
                "pip":          primary["pip"],
                "system":       primary["system"],
                "install_hint": f"pip install {{primary['pip']}}",
                "smoke_test":   dep["smoke_test"],
            }})
            continue

        if dep.get("deprecated_since"):
            result["deprecated"].append({{
                "capability":       dep["capability"],
                "deprecated_since": dep["deprecated_since"],
                "replaced_by":      dep.get("replaced_by"),
            }})
            continue

        tested = dep.get("tested_up_to")
        if tested:
            pkg_name  = primary["pip"].split(">=")[0].strip()
            installed = _installed_version(pkg_name)
            if installed and _version_newer_than(installed, tested):
                result["untested_versions"].append({{
                    "capability":        dep["capability"],
                    "tested_up_to":      tested,
                    "installed_version": installed,
                    "smoke_test":        dep["smoke_test"],
                }})

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
'''


def _test_smoke(slug: str, pkg: str, cls: str) -> str:
    return f'''\
"""Smoke tests for {slug}."""

from __future__ import annotations

import pytest
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures"


def test_import():
    from {pkg}.agent import {cls}Agent  # noqa: PLC0415
    assert {cls}Agent is not None


def test_check_dependencies():
    from {pkg}.tools import check_dependencies  # noqa: PLC0415
    result = check_dependencies()
    assert isinstance(result, dict)
    assert "missing" in result
'''
