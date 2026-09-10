"""Smoke test for agent-forge generate.

Verifies that generating a scaffolding from a minimal spec:
1. Produces the expected files
2. Passes validate_contract() (level 1)
3. Has the correct agent name in the generated pyproject.toml
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_SPEC = FIXTURES / "sample_spec.md"

EXPECTED_FILES = [
    "pyproject.toml",
    "{pkg}/tools.py",
    "{pkg}/tests/smoke/test_smoke.py",
    "{pkg}/__main__.py",
]


def _make_fake_llm(agent_name: str = "text-classifier"):
    """Return a llm_fn stub that returns a valid spec JSON."""
    import json

    def fake_llm(prompt: str, llm_config: dict, max_tokens: int = 1024) -> str:
        return json.dumps({
            "agent_name":   agent_name,
            "description":  "Classifies text documents.",
            "input_type":   "raw text documents (TXT, PDF)",
            "output_type":  "JSON with label and confidence score",
            "domain":       "document classification",
            "constraints":  ["offline", "no external API calls"],
            "uses_llm":     False,
        })

    return fake_llm


def test_generate_from_spec_produces_expected_files():
    """Step 1: all expected files exist after generation."""
    from agent_forge.agent import AgentForgeAgent

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        agent = AgentForgeAgent(
            output_dir=output_dir,
            llm_fn=_make_fake_llm("text-classifier"),
        )
        logs = list(agent.run(spec_path=SAMPLE_SPEC))

        summary = next((log for log in logs if log.get("summary")), None)
        assert summary is not None, f"No summary in logs: {logs}"

        agent_dir = output_dir / "text-classifier"
        pkg = "text_classifier"

        for pattern in EXPECTED_FILES:
            path = agent_dir / pattern.format(pkg=pkg)
            assert path.exists(), f"Expected file missing: {path}"


def test_generate_from_spec_passes_contract():
    """Step 2: validate_contract() returns passed=True."""
    pytest.importorskip("agent_tester", reason="agent-tester not installed — skipping contract check")

    from agent_tester.contract import validate_contract

    from agent_forge.agent import AgentForgeAgent

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        agent = AgentForgeAgent(
            output_dir=output_dir,
            llm_fn=_make_fake_llm("text-classifier"),
        )
        list(agent.run(spec_path=SAMPLE_SPEC))

        agent_dir = output_dir / "text-classifier"
        report = validate_contract(agent_dir)
        assert report.passed, f"Contract violations: {report.violations}"


def test_generate_from_spec_correct_name_in_pyproject():
    """Step 3: generated pyproject.toml contains the correct agent name."""
    from agent_forge.agent import AgentForgeAgent

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        agent = AgentForgeAgent(
            output_dir=output_dir,
            llm_fn=_make_fake_llm("text-classifier"),
        )
        list(agent.run(spec_path=SAMPLE_SPEC))

        pyproject = output_dir / "text-classifier" / "pyproject.toml"
        content = pyproject.read_text(encoding="utf-8")
        assert 'name = "text-classifier"' in content, (
            f"Expected 'name = \"text-classifier\"' in pyproject.toml:\n{content}"
        )
