"""AtlasCoherenceAgent — detects semantic overlap between CDC decomposition blocs."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Generator

from atlas_coherence.prompts import COHERENCE_CHECK_PROMPT

_log = logging.getLogger("atlas_coherence")


class AtlasCoherenceAgent:
    """Checks a list of decomposed blocs for semantic redundancy.

    Yields event dicts; the last event has summary=True and contains:
      {clean: bool, conflicts: list[dict]}
    """

    def __init__(
        self,
        llm_fn: Callable[[str, dict, int], str | None],
        config: dict | None = None,
    ) -> None:
        self._llm_fn = llm_fn
        self._llm_cfg = (config or {}).get("llm", {})

    def run(self, blocs: list[dict]) -> Generator[dict, None, None]:
        yield {"level": "info", "msg": f"atlas-coherence: checking {len(blocs)} blocs"}

        if len(blocs) < 2:
            yield {
                "summary": True,
                "clean": True,
                "conflicts": [],
            }
            return

        prompt = COHERENCE_CHECK_PROMPT.format(
            blocs_json=json.dumps(blocs, ensure_ascii=False, indent=2)
        )
        raw = self._llm_fn(prompt, self._llm_cfg, 2048)

        if not raw:
            _log.warning("atlas-coherence: LLM returned no output — assuming clean")
            yield {"summary": True, "clean": True, "conflicts": []}
            return

        result = _parse(raw)
        if result is None:
            _log.warning("atlas-coherence: invalid JSON response — assuming clean")
            yield {"summary": True, "clean": True, "conflicts": []}
            return

        conflicts = result.get("conflicts") or []
        clean = len(conflicts) == 0

        for c in conflicts:
            _log.info(
                "atlas-coherence: %s overlap — %s vs %s: %s",
                c.get("type"),
                c.get("primary"),
                c.get("redundant"),
                c.get("issue"),
            )

        yield {
            "summary": True,
            "clean": clean,
            "conflicts": conflicts,
        }


def _parse(raw: str) -> dict | None:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end <= 0:
        return None
    try:
        data = json.loads(raw[start:end])
    except json.JSONDecodeError:
        return None
    if not isinstance(data.get("conflicts"), list):
        return None
    return data
