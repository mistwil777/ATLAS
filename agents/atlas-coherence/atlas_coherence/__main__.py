"""CLI entry point — atlas-coherence check <blocs_json_file>"""

from __future__ import annotations

import json
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: atlas-coherence <blocs.json>", file=sys.stderr)
        sys.exit(1)

    blocs = json.loads(open(sys.argv[1]).read())  # noqa: PTH123, SIM115
    if isinstance(blocs, dict) and "blocs" in blocs:
        blocs = blocs["blocs"]

    def _llm_fn(prompt: str, cfg: dict, max_tokens: int = 2048) -> str | None:
        print("LLM not configured — set up a provider to use atlas-coherence CLI", file=sys.stderr)
        return None

    from atlas_coherence.agent import AtlasCoherenceAgent  # noqa: PLC0415

    agent = AtlasCoherenceAgent(llm_fn=_llm_fn)
    for event in agent.run(blocs=blocs):
        if event.get("summary"):
            print(json.dumps(event, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
