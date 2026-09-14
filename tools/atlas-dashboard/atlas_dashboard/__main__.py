"""Entry point — atlas-dashboard serve."""

from __future__ import annotations

import argparse


def main() -> None:
    import os

    parser = argparse.ArgumentParser(prog="atlas-dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--token", default=None, help="Auth token (or set ATLAS_DASHBOARD_TOKEN)")
    args = parser.parse_args()

    if args.token:
        os.environ["ATLAS_DASHBOARD_TOKEN"] = args.token

    import uvicorn

    uvicorn.run("atlas_dashboard.app:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
