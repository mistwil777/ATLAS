"""Smoke test — incomplete agents fail the ATLAS contract (negative case)."""

import tempfile
from pathlib import Path


def _make_agent(tmp: str, files: dict[str, str]) -> Path:
    """Create a minimal agent directory from a dict of relative_path → content."""
    root = Path(tmp)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def test_missing_pyproject_fails():
    from contract_validator.contract import validate_contract

    with tempfile.TemporaryDirectory() as tmp:
        agent_path = Path(tmp) / "empty_agent"
        agent_path.mkdir()
        report = validate_contract(agent_path)
        assert not report.passed
        assert any("pyproject.toml" in v for v in report.violations)


def test_missing_tools_py_fails():
    from contract_validator.contract import validate_contract

    with tempfile.TemporaryDirectory() as tmp:
        files = {
            "pyproject.toml": '[project]\nname = "bad-agent"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n',
            "bad_agent/__init__.py": "",
            "bad_agent/__main__.py": 'import sys\nif sys.argv[1:] == ["check"]: print("ok")\nif sys.argv[1:] == ["init"]: print("ok")',
            "bad_agent/tests/smoke/test_dummy.py": "def test_pass(): pass\n",
        }
        report = validate_contract(_make_agent(tmp, files))
        assert not report.passed
        assert any("tools.py" in v for v in report.violations)


def test_missing_smoke_tests_fails():
    from contract_validator.contract import validate_contract

    with tempfile.TemporaryDirectory() as tmp:
        files = {
            "pyproject.toml": '[project]\nname = "bad-agent"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n',
            "bad_agent/__init__.py": "",
            "bad_agent/__main__.py": 'import sys\nif sys.argv[1:] == ["check"]: print("ok")\nif sys.argv[1:] == ["init"]: print("ok")',
            "bad_agent/tools.py": 'def get_manifest(): return [{"capability":"x","primary":{"pip":"p","import":"p","system":[]},"alternatives":[],"smoke_test":None,"tested_up_to":"1.0"}]\ndef check_dependencies(): return {"missing":[],"deprecated":[],"untested_versions":[]}',
        }
        report = validate_contract(_make_agent(tmp, files))
        assert not report.passed
        assert any("smoke" in v.lower() for v in report.violations)


def test_missing_check_command_fails():
    from contract_validator.contract import validate_contract

    with tempfile.TemporaryDirectory() as tmp:
        files = {
            "pyproject.toml": '[project]\nname = "bad-agent"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n',
            "bad_agent/__init__.py": "",
            "bad_agent/__main__.py": "# no check or init here\nprint('hello')\n",
            "bad_agent/tools.py": 'def get_manifest(): return [{"capability":"x","primary":{"pip":"p","import":"p","system":[]},"alternatives":[],"smoke_test":None,"tested_up_to":"1.0"}]\ndef check_dependencies(): return {"missing":[],"deprecated":[],"untested_versions":[]}',
            "bad_agent/tests/smoke/test_dummy.py": "def test_pass(): pass\n",
        }
        report = validate_contract(_make_agent(tmp, files))
        assert not report.passed
        assert any("check" in v or "init" in v for v in report.violations)
