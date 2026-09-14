"""Entry point for `python -m atlas_orchestrator` and the `atlas-orchestrator` CLI."""

from __future__ import annotations

import json
import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"

    if command == "run":
        _cmd_run(sys.argv[2:])

    elif command == "check":
        from atlas_orchestrator.tools import check_dependencies  # noqa: PLC0415

        result = check_dependencies()
        if not result.get("missing"):
            print("All dependencies present and up to date.")
            return
        for item in result.get("missing", []):
            print(f"[MISSING]    {item['capability']} — pip install {item['primary']['pip']}")

    elif command == "init":
        from atlas_orchestrator.docker import generate_dockerfile  # noqa: PLC0415

        path = generate_dockerfile()
        print(f"Dockerfile generated: {path}")

    elif command == "install":
        from atlas_orchestrator.install import run_install  # noqa: PLC0415

        run_install()

    else:
        print("atlas-orchestrator commands:")
        print("  run       — orchestrate a CDC: run <cdc_text_or_path>")
        print("  check     — check dependencies")
        print("  init      — generate Dockerfile")
        print("  install   — run smart installation")


def _cmd_run(args: list[str]) -> None:
    if not args:
        print("Usage: atlas-orchestrator run <cdc_text_or_path>")
        sys.exit(1)

    from pathlib import Path  # noqa: PLC0415

    from atlas_orchestrator.agent import AtlasOrchestratorAgent  # noqa: PLC0415

    arg = args[0]
    cdc_path = arg if Path(arg).exists() else ""
    cdc_text = "" if cdc_path else arg

    agent = AtlasOrchestratorAgent()
    events = list(agent.run(cdc_text=cdc_text, cdc_path=cdc_path))

    for event in events:
        if event.get("summary"):
            print(
                json.dumps(
                    {k: v for k, v in event.items() if k != "summary"}, indent=2, ensure_ascii=False
                )
            )
        elif event.get("level") == "error":
            print(f"[ERROR] {event['msg']}", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"[{event.get('level', 'info').upper()}] {event.get('msg', '')}")


if __name__ == "__main__":
    main()
