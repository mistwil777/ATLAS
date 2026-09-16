"""CLI entry-point for tool-forge."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="tool-forge — generate a Python tool library")
    parser.add_argument("--spec", type=Path, help="Path to spec file (.md or .txt)")
    parser.add_argument("--spec-text", type=str, help="Spec as inline text")
    parser.add_argument("--output-dir", type=Path, default=Path.cwd() / "libs")
    args = parser.parse_args()

    from tool_forge.agent import ToolForgeAgent

    agent = ToolForgeAgent(output_dir=args.output_dir)
    for event in agent.run(spec_text=args.spec_text or "", spec_path=args.spec):
        level = event.get("level", "")
        msg = event.get("msg", "")
        if msg:
            print(f"[{level}] {msg}" if level else msg)
        if event.get("summary"):
            print(f"\nDone — {event.get('output_path')}")
            sys.exit(0)

    sys.exit(1)


if __name__ == "__main__":
    main()
