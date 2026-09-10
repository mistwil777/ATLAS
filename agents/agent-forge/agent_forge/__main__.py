"""Entry point for `python -m agent_forge` and the `agent-forge` CLI."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"

    if command == "generate":
        spec_path: Path | None = None
        args = sys.argv[2:]
        if "--spec" in args:
            idx = args.index("--spec")
            if idx + 1 < len(args):
                spec_path = Path(args[idx + 1])
            else:
                print("Error: --spec requires a file path")
                sys.exit(1)

        output_dir: Path | None = None
        if "--output-dir" in args:
            idx = args.index("--output-dir")
            if idx + 1 < len(args):
                output_dir = Path(args[idx + 1])

        from agent_forge.agent import AgentForgeAgent  # noqa: PLC0415

        agent = AgentForgeAgent(output_dir=output_dir)
        for log in agent.run(spec_path=spec_path):
            if log.get("summary"):
                print(f"\n[DONE] Agent '{log['agent_name']}' generated at {log['output_path']}")
                if not log["contract_passed"]:
                    print("[WARN] Contract violations:")
                    for v in log["violations"]:
                        print(f"  - {v}")
            else:
                level = log.get("level", "info").upper()
                print(f"[{level}] {log.get('msg', '')}")

    elif command == "check":
        from agent_forge.tools import check_dependencies  # noqa: PLC0415

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
                f"[UNTESTED]  {item['capability']} — tested up to {item['tested_up_to']}, found {item['installed_version']}"
            )

    elif command == "init":
        from agent_forge.docker import generate_dockerfile  # noqa: PLC0415

        path = generate_dockerfile()
        print(f"Dockerfile generated: {path}")

    elif command == "install":
        from agent_forge.install import run_install  # noqa: PLC0415

        run_install()

    else:
        print("agent-forge commands:")
        print("  generate            — interactive mode: asks 4-5 questions then generates")
        print("  generate --spec <f> — CDC mode: reads spec file (MD, PDF, DOCX), generates")
        print("  generate --spec <f> --output-dir <dir>  — custom output directory")
        print("  check               — check all dependencies status")
        print("  init                — generate Dockerfile from dependency manifest")
        print("  install             — run smart installation with smoke tests")


if __name__ == "__main__":
    main()
