"""Entry point for `python -m cdc_router` and the `cdc-router` CLI."""

from __future__ import annotations

import json
import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"

    if command == "check":
        from cdc_router.tools import check_dependencies  # noqa: PLC0415

        result = check_dependencies()
        if not any(result.values()):
            print("All dependencies present and up to date.")
            return
        for item in result.get("missing", []):
            print(f"[MISSING]    {item['capability']} — pip install {item['primary']['pip']}")

    elif command == "init":
        from cdc_router.docker import generate_dockerfile  # noqa: PLC0415

        path = generate_dockerfile()
        print(f"Dockerfile generated: {path}")

    elif command == "install":
        from cdc_router.install import run_install  # noqa: PLC0415

        run_install()

    elif command == "run":
        _cmd_run(sys.argv[2:])

    else:
        print("cdc-router commands:")
        print("  run      — route a CDC: run <cdc_file_or_text>")
        print("  check    — check dependencies")
        print("  init     — generate Dockerfile")
        print("  install  — run smart installation")


def _cmd_run(args: list[str]) -> None:
    if not args:
        print("Usage: cdc-router run <cdc_file_or_text>")
        sys.exit(1)

    from pathlib import Path  # noqa: PLC0415

    from cdc_router.agent import CdcRouterAgent  # noqa: PLC0415

    arg = args[0]
    path = Path(arg)
    agent = CdcRouterAgent()

    events = list(agent.run(cdc_path=str(path))) if path.exists() else list(agent.run(cdc_text=arg))

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
