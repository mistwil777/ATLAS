"""Subprocess runner — executes an ATLAS agent in an isolated process.

Each agent runs in its own Python subprocess. Communication uses JSON Lines
on stdout: one JSON object per line, matching the agent's yield protocol.
The dict with "summary": True marks the end of a successful run.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StepResult:
    agent_name: str
    logs: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    passed: bool = False
    error: str = ""


def run_agent(agent_name: str, entry: str, inputs: dict) -> StepResult:
    """Run an ATLAS agent in a subprocess and collect its output.

    Args:
        agent_name: human-readable name (e.g. "pair-forge")
        entry:      "module:ClassName" from the entry-point
        inputs:     kwargs passed to agent.run()
    """
    result = StepResult(agent_name=agent_name)
    module_path, class_name = entry.split(":")

    # Serialize Path values to strings for JSON transport
    serialized_inputs = _serialize_inputs(inputs)

    script = textwrap.dedent(f"""
        import json, sys
        from {module_path} import {class_name}
        agent = {class_name}()
        inputs = json.loads({json.dumps(json.dumps(serialized_inputs))!r})
        _inputs = _deserialize(inputs)
        for event in agent.run(**_inputs):
            print(json.dumps(event, default=str), flush=True)

        def _deserialize(data):
            from pathlib import Path
            return {{
                k: Path(v) if isinstance(v, str) and k.endswith("_path") else v
                for k, v in data.items()
            }}
    """)

    # Rewrite with correct deserialize placement
    script = textwrap.dedent(f"""\
        import json, sys
        from pathlib import Path
        from {module_path} import {class_name}

        def _deserialize(data):
            return {{
                k: Path(v) if isinstance(v, str) and k.endswith("_path") else v
                for k, v in data.items()
            }}

        agent = {class_name}()
        raw_inputs = json.loads({json.dumps(json.dumps(serialized_inputs))!r})
        inputs = _deserialize(raw_inputs)
        for event in agent.run(**inputs):
            print(json.dumps(event, default=str), flush=True)
    """)

    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("summary"):
                result.summary = event
                result.passed = True
            else:
                result.logs.append(event)

        proc.wait()
        if proc.returncode != 0 and not result.passed:
            stderr = proc.stderr.read() if proc.stderr else ""
            result.error = stderr[:500]

    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)

    return result


def _serialize_inputs(inputs: dict) -> dict:
    """Convert Path values to strings for JSON transport."""
    return {k: str(v) if isinstance(v, Path) else v for k, v in inputs.items()}
