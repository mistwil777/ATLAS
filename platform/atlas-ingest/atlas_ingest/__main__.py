"""CLI entry point: python -m atlas_ingest file1 file2 ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atlas_ingest.agent import AtlasIngestAgent


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="atlas-ingest",
        description="Extract text from documents for ATLAS context enrichment.",
    )
    parser.add_argument("files", nargs="+", type=Path, help="Files to ingest")
    parser.add_argument("--cdc", default="", help="CDC bloc name for context")
    parser.add_argument("--context", default="", help="Additional context")
    parser.add_argument("--lang", default="fra+eng", help="OCR language(s)")
    args = parser.parse_args()

    agent = AtlasIngestAgent(ocr_lang=args.lang)
    for event in agent.run(files=args.files, cdc_bloc=args.cdc, context=args.context):
        if event.get("summary"):
            print(event["output"])
        elif event.get("level") == "warning":
            print(f"[WARN] {event['msg']}", file=sys.stderr)


if __name__ == "__main__":
    main()
