"""CLI entry point for fake-agent."""

import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"

    if command == "check":
        from fake_agent.tools import check_dependencies  # noqa: PLC0415
        result = check_dependencies()
        if not any(result.values()):
            print("All dependencies present.")

    elif command == "init":
        from pathlib import Path  # noqa: PLC0415
        out = Path("Dockerfile.fake-agent")
        out.write_text("FROM python:3.10-slim\n", encoding="utf-8")
        print(f"Dockerfile generated: {out}")

    else:
        print("fake-agent commands: check | init")


if __name__ == "__main__":
    main()
