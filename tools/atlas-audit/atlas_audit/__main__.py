"""atlas-audit — HITL CLI for reviewing ATLAS pipeline runs.

Commands:
  atlas-audit list [--review]          List all runs (--review: only pending review)
  atlas-audit show <run_id>            Print full run details
  atlas-audit approve <run_id>         Approve a run
  atlas-audit reject <run_id>          Reject a run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_DEFAULT_RUN_STORE_DIR = Path.home() / ".atlas" / "runs"


def _store(store_dir: Path):
    from atlas_memory.run_store import RunStore

    return RunStore(store_dir)


def cmd_list(args: argparse.Namespace) -> None:
    store = _store(args.store_dir)
    runs = store.list_runs(only_review=args.review)
    if not runs:
        print("No runs found.")
        return
    print(f"{'RUN ID':<38}  {'CREATED':<26}  {'STATUS':<10}  REVIEW")
    print("-" * 90)
    for r in runs:
        flag = "YES" if r.get("requires_review") else "-"
        print(f"{r['run_id']:<38}  {r['created_at']:<26}  {r['status']:<10}  {flag}")


def cmd_show(args: argparse.Namespace) -> None:
    store = _store(args.store_dir)
    run = store.get_run(args.run_id)
    if run is None:
        print(f"Run {args.run_id!r} not found.", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(run, indent=2, ensure_ascii=False))


def cmd_approve(args: argparse.Namespace) -> None:
    store = _store(args.store_dir)
    store.set_review_decision(args.run_id, "approved")
    print(f"Run {args.run_id} approved.")


def cmd_reject(args: argparse.Namespace) -> None:
    store = _store(args.store_dir)
    store.set_review_decision(args.run_id, "rejected")
    print(f"Run {args.run_id} rejected.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-audit", description="ATLAS HITL audit CLI")
    parser.add_argument(
        "--store-dir",
        type=Path,
        default=_DEFAULT_RUN_STORE_DIR,
        help="Run store directory (default: ~/.atlas/runs)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List runs")
    p_list.add_argument("--review", action="store_true", help="Only runs requiring review")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show run details")
    p_show.add_argument("run_id")
    p_show.set_defaults(func=cmd_show)

    p_approve = sub.add_parser("approve", help="Approve a run")
    p_approve.add_argument("run_id")
    p_approve.set_defaults(func=cmd_approve)

    p_reject = sub.add_parser("reject", help="Reject a run")
    p_reject.add_argument("run_id")
    p_reject.set_defaults(func=cmd_reject)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
