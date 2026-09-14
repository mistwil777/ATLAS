"""Bus — pipeline orchestrator for ATLAS agents."""

from __future__ import annotations

import time
from collections.abc import Generator
from dataclasses import dataclass, field

from atlas_bus.discovery import discover_agents
from atlas_bus.runner import StepResult, run_agent


@dataclass
class PipelineResult:
    steps: list[StepResult] = field(default_factory=list)
    passed: bool = False
    duration_s: float = 0.0


class Bus:
    """Discover installed ATLAS agents and run them in isolated subprocesses.

    Usage::

        bus = Bus()
        result = bus.run_pipeline([
            ("agent-forge", {"spec_path": "my_spec.md"}),
            ("agent-tester", {}),
        ])
    """

    def __init__(self) -> None:
        self._agents: dict[str, str] = {}

    def refresh(self) -> dict[str, str]:
        """Re-scan installed ATLAS agents. Returns {name: entry}."""
        self._agents = discover_agents()
        return self._agents

    def list_agents(self) -> dict[str, str]:
        """Return discovered agents, scanning if not yet loaded."""
        if not self._agents:
            self.refresh()
        return self._agents

    def run_pipeline(
        self,
        steps: list[tuple[str, dict]],
        log_fn: object = None,
    ) -> PipelineResult:
        """Run a sequence of agents, passing each summary to the next step.

        Args:
            steps:  list of (agent_name, inputs) tuples.
            log_fn: optional callable(level, msg) for progress logging.
        """
        agents = self.list_agents()
        result = PipelineResult()
        t0 = time.monotonic()
        previous_summary: dict = {}

        for agent_name, inputs in steps:
            if agent_name not in agents:
                step = StepResult(
                    agent_name=agent_name,
                    error=f"Agent '{agent_name}' not found. Installed: {list(agents)}",
                )
                result.steps.append(step)
                result.passed = False
                result.duration_s = round(time.monotonic() - t0, 3)
                return result

            merged_inputs = {**inputs}
            if previous_summary:
                merged_inputs.setdefault("previous_summary", previous_summary)

            if log_fn:
                log_fn("info", f"Running {agent_name}...")  # type: ignore[operator]

            step = run_agent(agent_name, agents[agent_name], merged_inputs)
            result.steps.append(step)

            if not step.passed:
                result.passed = False
                result.duration_s = round(time.monotonic() - t0, 3)
                return result

            previous_summary = step.summary

        result.passed = True
        result.duration_s = round(time.monotonic() - t0, 3)
        return result

    def run_agent(self, agent_name: str, inputs: dict) -> StepResult:
        """Run a single agent outside of a pipeline."""
        agents = self.list_agents()
        if agent_name not in agents:
            return StepResult(
                agent_name=agent_name,
                error=f"Agent '{agent_name}' not found. Installed: {list(agents)}",
            )
        return run_agent(agent_name, agents[agent_name], inputs)

    def stream_pipeline(
        self,
        steps: list[tuple[str, dict]],
    ) -> Generator[dict, None, PipelineResult]:
        """Like run_pipeline but yields log events as they arrive.

        Yields dicts with {"level": ..., "msg": ..., "agent": agent_name}.
        Returns PipelineResult via StopIteration value (generator return).
        """
        agents = self.list_agents()
        result = PipelineResult()
        t0 = time.monotonic()
        previous_summary: dict = {}

        for agent_name, inputs in steps:
            if agent_name not in agents:
                step = StepResult(
                    agent_name=agent_name,
                    error=f"Agent '{agent_name}' not found.",
                )
                result.steps.append(step)
                result.passed = False
                result.duration_s = round(time.monotonic() - t0, 3)
                return result

            merged_inputs = {**inputs}
            if previous_summary:
                merged_inputs.setdefault("previous_summary", previous_summary)

            step = run_agent(agent_name, agents[agent_name], merged_inputs)
            for log in step.logs:
                yield {**log, "agent": agent_name}

            result.steps.append(step)

            if not step.passed:
                result.passed = False
                result.duration_s = round(time.monotonic() - t0, 3)
                return result

            previous_summary = step.summary

        result.passed = True
        result.duration_s = round(time.monotonic() - t0, 3)
        return result
