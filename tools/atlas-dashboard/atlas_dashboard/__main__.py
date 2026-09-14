"""Entry point — atlas-dashboard serve."""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8050)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run("atlas_dashboard.app:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
