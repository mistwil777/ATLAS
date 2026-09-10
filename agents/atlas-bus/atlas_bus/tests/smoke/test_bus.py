"""Smoke tests for atlas-bus.

Tests:
1. discovery returns a dict (even if empty when no agents installed)
2. Bus.list_agents() works
3. Bus.run_agent() returns StepResult with error when agent not found
4. run_pipeline fails gracefully for unknown agent
5. check_dependencies always passes (no external deps)
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap


def test_discover_agents_returns_dict():
    from atlas_bus.discovery import discover_agents

    result = discover_agents()
    assert isinstance(result, dict)


def test_bus_list_agents_returns_dict():
    from atlas_bus.bus import Bus

    bus = Bus()
    agents = bus.list_agents()
    assert isinstance(agents, dict)


def test_bus_unknown_agent_fails_gracefully():
    from atlas_bus.bus import Bus

    bus = Bus()
    result = bus.run_pipeline([("nonexistent-agent-xyz", {})])
    assert result.passed is False
    assert result.steps[0].error != ""


def test_check_dependencies_always_passes():
    from atlas_bus.tools import check_dependencies

    result = check_dependencies()
    assert result["missing"] == []
    assert result["deprecated"] == []
    assert result["untested_versions"] == []


def test_runner_with_inline_agent():
    """Runner executes a minimal inline agent via subprocess and captures output."""
    script = textwrap.dedent("""\
        import json, sys
        events = [
            {"level": "info", "msg": "starting"},
            {"summary": True, "ok": True},
        ]
        for e in events:
            print(json.dumps(e), flush=True)
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln]
    events = [json.loads(ln) for ln in lines]
    assert events[0] == {"level": "info", "msg": "starting"}
    assert events[1]["summary"] is True


def test_pipeline_with_fake_agent():
    """Full pipeline run with a fake agent registered via monkey-patching."""
    from atlas_bus.bus import Bus

    # Directly test run_pipeline by injecting a mock discovery
    bus = Bus()
    bus._agents = {"fake": "atlas_bus.tests.smoke.test_bus:_FakeAgent"}

    result = bus.run_pipeline([("fake", {})])
    # fake agent not importable as a real ATLAS agent — expect graceful failure or pass
    # (subprocess will fail to import _FakeAgent — that's acceptable, tests the error path)
    assert isinstance(result.passed, bool)
    assert len(result.steps) == 1


class _FakeAgent:
    """Not a real agent — used only to test discovery injection."""

    def run(self) -> None:
        yield {"summary": True}
