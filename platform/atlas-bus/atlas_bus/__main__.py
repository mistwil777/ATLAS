"""Entry point for `python -m atlas_bus` and the `atlas-bus` CLI."""

from __future__ import annotations

import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"

    if command == "init":
        from atlas_bus.docker import generate_dockerfile  # noqa: PLC0415

        path = generate_dockerfile()
        print(f"Dockerfile generated: {path}")

    elif command == "check":
        from atlas_bus.tools import check_dependencies  # noqa: PLC0415

        result = check_dependencies()
        if not any(result.values()):
            print("All dependencies present and up to date.")
            return
        for item in result.get("missing", []):
            print(f"[MISSING]   {item['capability']} — {item['install_hint']}")
        for item in result.get("deprecated", []):
            print(f"[DEPRECATED] {item['capability']} — replaced by: {item['replaced_by']}")
        for item in result.get("untested_versions", []):
            print(
                f"[UNTESTED]  {item['capability']} — tested up to {item['tested_up_to']},"
                f" found {item['installed_version']}"
            )

    elif command == "install":
        from atlas_bus.install import run_install  # noqa: PLC0415

        run_install()

    elif command == "list":
        from atlas_bus.discovery import discover_agents  # noqa: PLC0415

        agents = discover_agents()
        if not agents:
            print("No ATLAS agents discovered. Install agents with pip install <agent>.")
            return
        print("Discovered ATLAS agents:")
        for name, entry in agents.items():
            print(f"  {name:20s} {entry}")

    elif command == "run":
        _cmd_run(sys.argv[2:])

    else:
        print("atlas-bus commands:")
        print("  list     — list discovered ATLAS agents")
        print("  run      — run a pipeline of agents")
        print("  init     — generate Dockerfile")
        print("  check    — check all dependencies status")
        print("  install  — run smart installation")


def _cmd_run(args: list[str]) -> None:
    """Run a pipeline: atlas-bus run agent1,agent2,agent3 [key=value ...]"""
    if not args:
        print("Usage: atlas-bus run <agent1,agent2,...> [key=value ...]")
        print("Example: atlas-bus run agent-forge,contract-validator spec_path=my_spec.md")
        sys.exit(1)

    from atlas_bus.bus import Bus  # noqa: PLC0415

    agent_names = [a.strip() for a in args[0].split(",")]
    inputs: dict = {}
    for kv in args[1:]:
        if "=" in kv:
            k, v = kv.split("=", 1)
            inputs[k] = v

    bus = Bus()
    steps = [(name, dict(inputs)) for name in agent_names]

    print(f"Running pipeline: {' → '.join(agent_names)}")
    result = bus.run_pipeline(steps)

    for step in result.steps:
        status = "PASS" if step.passed else "FAIL"
        print(f"  [{status}] {step.agent_name}")
        if step.error:
            print(f"         Error: {step.error}")
        for log in step.logs:
            level = log.get("level", "info").upper()
            print(f"         [{level}] {log.get('msg', '')}")

    print(f"\nPipeline: {'PASSED' if result.passed else 'FAILED'} ({result.duration_s}s)")
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
