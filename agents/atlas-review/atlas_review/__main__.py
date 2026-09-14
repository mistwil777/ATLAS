"""Entry point — atlas-review <run_id>."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-review")
    parser.add_argument("run_id", help="ID of the rejected run to re-process")
    args = parser.parse_args()

    from atlas_review.agent import AtlasReviewAgent

    agent = AtlasReviewAgent()
    for event in agent.run(args.run_id):
        if event.get("summary"):
            print(f"\nDone. New run: {event['new_run_id']}")
            print(f"Re-processed: {event['reprocessed_blocs']} bloc(s)")
        else:
            level = event.get("level", "info")
            print(f"[{level.upper()}] {event.get('msg', '')}")

    sys.exit(0)


if __name__ == "__main__":
    main()
