"""Smoke tests for tool-forge — 100% offline (no LLM calls)."""

from __future__ import annotations

import json
from pathlib import Path

_FIXTURES = Path(__file__).parent / "fixtures"

_VALID_TOOL_RESPONSE = json.dumps(
    {
        "module_name": "output_formatter",
        "description": "Formats a classification result dict as a JSON string.",
        "functions": [
            {
                "name": "format_result",
                "signature": "def format_result(result: dict) -> str:",
                "docstring": "Format a classification result dict as a JSON string.",
                "body": (
                    "required = {'ticket_id', 'priority', 'category', 'confidence', 'justification'}\n"
                    "missing = required - result.keys()\n"
                    "if missing:\n"
                    "    raise ValueError(f'Missing required keys: {missing}')\n"
                    "if not 0.0 <= result['confidence'] <= 1.0:\n"
                    "    raise ValueError('confidence must be between 0.0 and 1.0')\n"
                    "return json.dumps({k: result[k] for k in required})"
                ),
            }
        ],
        "imports": ["import json"],
        "tests": [
            {
                "function": "format_result",
                "description": "valid input returns JSON string",
                "code": (
                    "result = format_result({\n"
                    "    'ticket_id': 'T001', 'priority': 'haute',\n"
                    "    'category': 'bug', 'confidence': 0.9, 'justification': 'ok'\n"
                    "})\n"
                    "parsed = json.loads(result)\n"
                    "assert parsed['ticket_id'] == 'T001'"
                ),
            },
            {
                "function": "format_result",
                "description": "missing key raises ValueError",
                "code": (
                    "import pytest\n"
                    "with pytest.raises(ValueError):\n"
                    "    format_result({'ticket_id': 'T001'})"
                ),
            },
        ],
    }
)


def _mock_llm_ok(prompt: str, cfg: dict, max_tokens: int) -> str:
    return _VALID_TOOL_RESPONSE


def _mock_llm_invalid_then_ok(prompt: str, cfg: dict, max_tokens: int) -> str:
    if not hasattr(_mock_llm_invalid_then_ok, "_called"):
        _mock_llm_invalid_then_ok._called = True
        return "not json"
    del _mock_llm_invalid_then_ok._called
    return _VALID_TOOL_RESPONSE


def _mock_llm_always_fail(prompt: str, cfg: dict, max_tokens: int) -> str:
    return "not json"


# ── scaffold tests ────────────────────────────────────────────────────────────


def test_scaffold_writes_files(tmp_path):
    from tool_forge.scaffold import generate

    result = generate("Format output as JSON.", tmp_path, _mock_llm_ok, {})
    assert result is not None
    assert result["module_name"] == "output_formatter"

    tool_dir = tmp_path / "output_formatter"
    assert tool_dir.exists()
    assert (tool_dir / "output_formatter.py").exists()
    assert (tool_dir / "pyproject.toml").exists()
    assert (tool_dir / "tests" / "test_output_formatter.py").exists()


def test_scaffold_generated_code_is_valid_python(tmp_path):
    from tool_forge.scaffold import generate

    result = generate("Format output as JSON.", tmp_path, _mock_llm_ok, {})
    assert result is not None
    code = (tmp_path / "output_formatter" / "output_formatter.py").read_text()
    compile(code, "output_formatter.py", "exec")


def test_scaffold_reprompt_on_invalid_json(tmp_path):
    from tool_forge.scaffold import generate

    result = generate("Format output.", tmp_path, _mock_llm_invalid_then_ok, {})
    assert result is not None


def test_scaffold_returns_none_on_always_fail(tmp_path):
    from tool_forge.scaffold import generate

    result = generate("Format output.", tmp_path, _mock_llm_always_fail, {})
    assert result is None


def test_scaffold_pyproject_has_no_atlas_agents_entry(tmp_path):
    from tool_forge.scaffold import generate

    generate("Format output.", tmp_path, _mock_llm_ok, {})
    pyproject = (tmp_path / "output_formatter" / "pyproject.toml").read_text()
    assert "atlas.agents" not in pyproject
    assert "atlas.tools" not in pyproject


# ── agent tests ───────────────────────────────────────────────────────────────


def test_run_yields_summary(tmp_path):
    from tool_forge.agent import ToolForgeAgent

    agent = ToolForgeAgent(output_dir=tmp_path, llm_fn=_mock_llm_ok)
    events = list(agent.run(spec_text="Format output as JSON."))

    summary = next((e for e in events if e.get("summary")), None)
    assert summary is not None
    assert summary["module_name"] == "output_formatter"
    assert "output_path" in summary
    assert "functions" in summary


def test_run_empty_spec_returns_error(tmp_path):
    from tool_forge.agent import ToolForgeAgent

    agent = ToolForgeAgent(output_dir=tmp_path, llm_fn=_mock_llm_ok)
    events = list(agent.run(spec_text=""))

    assert any(e.get("level") == "error" for e in events)
    assert not any(e.get("summary") for e in events)


def test_run_with_spec_file(tmp_path):
    from tool_forge.agent import ToolForgeAgent

    fixture = _FIXTURES / "sample_spec.md"
    agent = ToolForgeAgent(output_dir=tmp_path, llm_fn=_mock_llm_ok)
    events = list(agent.run(spec_path=str(fixture)))

    summary = next((e for e in events if e.get("summary")), None)
    assert summary is not None


def test_run_llm_failure_returns_error(tmp_path):
    from tool_forge.agent import ToolForgeAgent

    agent = ToolForgeAgent(output_dir=tmp_path, llm_fn=_mock_llm_always_fail)
    events = list(agent.run(spec_text="Format output."))

    assert any(e.get("level") == "error" for e in events)
    assert not any(e.get("summary") for e in events)


# ── tools / deps ──────────────────────────────────────────────────────────────


def test_check_dependencies():
    from tool_forge.tools import check_dependencies

    result = check_dependencies()
    assert result["missing"] == []
    assert result["deprecated"] == []
    assert result["untested_versions"] == []


def test_prompts_contain_key_constraints():
    from tool_forge.prompts import TOOL_GENERATION_PROMPT

    assert "Pure functions" in TOOL_GENERATION_PROMPT
    assert "type hints" in TOOL_GENERATION_PROMPT
    assert "edge cases" in TOOL_GENERATION_PROMPT.lower()
    assert "module_name" in TOOL_GENERATION_PROMPT
