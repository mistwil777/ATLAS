"""Entry point for `python -m atlas_memory` and the `atlas-memory` CLI."""

from __future__ import annotations

import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"

    if command == "check":
        from atlas_memory.tools import check_dependencies  # noqa: PLC0415

        result = check_dependencies()
        if not any(result.values()):
            print("All dependencies present and up to date.")
            return
        for item in result.get("missing", []):
            print(f"[MISSING]    {item['capability']} — pip install {item['primary']['pip']}")
        for item in result.get("deprecated", []):
            print(f"[DEPRECATED] {item['capability']} — replaced by: {item['replaced_by']}")
        for item in result.get("untested_versions", []):
            print(
                f"[UNTESTED]   {item['capability']} — tested up to {item['tested_up_to']},"
                f" found {item.get('installed_version', '?')}"
            )

    elif command == "init":
        from atlas_memory.docker import generate_dockerfile  # noqa: PLC0415

        path = generate_dockerfile()
        print(f"Dockerfile generated: {path}")

    elif command == "install":
        from atlas_memory.install import run_install  # noqa: PLC0415

        run_install()

    elif command == "index":
        _cmd_index(sys.argv[2:])

    elif command == "search":
        _cmd_search(sys.argv[2:])

    elif command == "register":
        _cmd_register(sys.argv[2:])

    elif command == "graph":
        _cmd_graph()

    else:
        print("atlas-memory commands:")
        print("  check    — check dependencies")
        print("  init     — generate Dockerfile")
        print("  install  — run smart installation")
        print("  index    — index an experience: index <entry_type> <content>")
        print("  search   — search experiences: search <query> [--type cdc]")
        print("  register — register an agent in the KG: register <name> [--tags tag1,tag2]")
        print("  graph    — dump the full Knowledge Graph")


def _cmd_index(args: list[str]) -> None:
    if len(args) < 2:
        print("Usage: atlas-memory index <entry_type> <content>")
        sys.exit(1)
    from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

    agent = AtlasMemoryAgent()
    for event in agent.run(operation="index", entry_type=args[0], content=" ".join(args[1:])):
        if event.get("summary"):
            print(f"Indexed: {event['result']['id']}")
        elif event.get("level") == "error":
            print(f"Error: {event['msg']}")
            sys.exit(1)


def _cmd_search(args: list[str]) -> None:
    if not args:
        print("Usage: atlas-memory search <query> [--type <entry_type>]")
        sys.exit(1)
    import json  # noqa: PLC0415

    from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

    entry_type = None
    query_parts = []
    i = 0
    while i < len(args):
        if args[i] == "--type" and i + 1 < len(args):
            entry_type = args[i + 1]
            i += 2
        else:
            query_parts.append(args[i])
            i += 1
    agent = AtlasMemoryAgent()
    for event in agent.run(operation="search", query=" ".join(query_parts), entry_type=entry_type):
        if event.get("summary"):
            print(json.dumps(event["result"], indent=2, ensure_ascii=False))
        elif event.get("level") == "error":
            print(f"Error: {event['msg']}")
            sys.exit(1)


def _cmd_register(args: list[str]) -> None:
    if not args:
        print("Usage: atlas-memory register <name> [--tags tag1,tag2]")
        sys.exit(1)
    from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

    name = args[0]
    tags: list[str] = []
    i = 1
    while i < len(args):
        if args[i] == "--tags" and i + 1 < len(args):
            tags = args[i + 1].split(",")
            i += 2
        else:
            i += 1
    agent = AtlasMemoryAgent()
    for event in agent.run(operation="register_agent", name=name, tags=tags):
        if event.get("summary"):
            print(f"Registered: {event['result']['registered']}")
        elif event.get("level") == "error":
            print(f"Error: {event['msg']}")
            sys.exit(1)


def _cmd_graph() -> None:
    import json  # noqa: PLC0415

    from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

    agent = AtlasMemoryAgent()
    for event in agent.run(operation="get_graph"):
        if event.get("summary"):
            print(json.dumps(event["result"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
