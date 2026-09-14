"""Entry point for `python -m atlas_judge` and the `atlas-judge` CLI."""

from __future__ import annotations

import json
import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"

    if command == "check":
        from atlas_judge.tools import check_dependencies  # noqa: PLC0415

        result = check_dependencies()
        if not any(result.values()):
            print("All dependencies present and up to date.")
            return
        for item in result.get("missing", []):
            print(f"[MISSING]    {item['capability']} — pip install {item['primary']['pip']}")

    elif command == "init":
        from atlas_judge.docker import generate_dockerfile  # noqa: PLC0415

        path = generate_dockerfile()
        print(f"Dockerfile generated: {path}")

    elif command == "install":
        from atlas_judge.install import run_install  # noqa: PLC0415

        run_install()

    elif command == "evaluate":
        _cmd_evaluate(sys.argv[2:])

    else:
        print("atlas-judge commands:")
        print("  evaluate  — evaluate an output: evaluate <output> [--agent cdc-router]")
        print("  check     — check dependencies")
        print("  init      — generate Dockerfile")
        print("  install   — run smart installation")


def _cmd_evaluate(args: list[str]) -> None:
    if not args:
        print(
            "Usage: atlas-judge evaluate <output_text> [--agent <agent_name>] [--mode pointwise|pairwise|reference]"
        )
        sys.exit(1)

    from atlas_judge.agent import AtlasJudgeAgent  # noqa: PLC0415

    output = args[0]
    agent_name = ""
    mode = "pointwise"
    i = 1
    while i < len(args):
        if args[i] == "--agent" and i + 1 < len(args):
            agent_name = args[i + 1]
            i += 2
        elif args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1]
            i += 2
        else:
            i += 1

    agent = AtlasJudgeAgent()
    events = list(agent.run(operation=mode, output=output, agent_name=agent_name))

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
