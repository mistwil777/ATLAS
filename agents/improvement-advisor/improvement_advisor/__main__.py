"""Entry point for `python -m improvement_advisor` and the `improvement-advisor` CLI."""

from __future__ import annotations

import json
import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"

    if command == "advise":
        _cmd_advise(sys.argv[2:])
    else:
        print("improvement-advisor commands:")
        print("  advise  — analyse a trend JSON file: advise <trend.json> [--agent <name>]")


def _cmd_advise(args: list[str]) -> None:
    if not args:
        print("Usage: improvement-advisor advise <trend.json> [--agent <agent_name>]")
        sys.exit(1)

    from improvement_advisor.agent import ImprovementAdvisorAgent  # noqa: PLC0415

    trend_path = args[0]
    agent_name = ""
    i = 1
    while i < len(args):
        if args[i] == "--agent" and i + 1 < len(args):
            agent_name = args[i + 1]
            i += 2
        else:
            i += 1

    import pathlib  # noqa: PLC0415

    trend_data = json.loads(pathlib.Path(trend_path).read_text(encoding="utf-8"))
    points = trend_data if isinstance(trend_data, list) else trend_data.get("points", [])
    if not agent_name:
        agent_name = (
            trend_data.get("agent", "unknown") if isinstance(trend_data, dict) else "unknown"
        )

    agent = ImprovementAdvisorAgent()
    events = list(agent.run(agent_name=agent_name, trend_points=points))

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
