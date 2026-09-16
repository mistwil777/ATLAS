"""tool-forge — ToolForgeAgent.

Generates a reusable Python tool library from a functional spec.
Output goes to libs/ — pure functions, typed, with pytest tests.
No Bus entry-point, no LLM call inside the generated tool.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator
from pathlib import Path


def _default_llm(prompt: str, cfg: dict, max_tokens: int = 2048) -> str | None:
    try:
        from atlas_cli.config import llm_fn_from_config, load  # type: ignore[import]

        fn, _cfg = llm_fn_from_config(load())
        return fn(prompt, _cfg, max_tokens)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("_default_llm failed: %s", exc)
        return None


class ToolForgeAgent:
    """Generates a reusable Python tool library from a functional spec.

    Args:
        output_dir:  Where to write the generated tool (default: libs/).
        config:      Optional config dict. Key: llm (passed to llm_fn).
        llm_fn:      callable(prompt, cfg, max_tokens) -> str | None
    """

    def __init__(
        self,
        output_dir: Path | None = None,
        config: dict | None = None,
        llm_fn: Callable[[str, dict, int], str | None] | None = None,
    ) -> None:
        self.output_dir = output_dir or (Path.cwd() / "libs")
        self.config = config or {}
        self._llm_cfg = self.config.get("llm", {})
        self._llm = llm_fn or _default_llm

    def run(
        self,
        spec_text: str = "",
        spec_path: Path | str | None = None,
    ) -> Generator[dict, None, None]:
        """Generate a tool library from spec. Yields log dicts then summary."""
        try:
            from atlas_core import collect  # noqa: PLC0415
        except ImportError:
            collect = None  # type: ignore[assignment]

        _t0 = time.monotonic()
        yield self._info("tool-forge starting")

        spec = self._load_spec(spec_text, spec_path)
        if not spec:
            yield self._error("No spec provided — pass spec_text or spec_path")
            return

        yield self._info(f"Spec loaded ({len(spec)} chars) — generating tool library")

        from tool_forge.scaffold import generate  # noqa: PLC0415

        result = generate(spec, Path(self.output_dir), self._llm, self._llm_cfg)
        if result is None:
            yield self._error("LLM generation failed — check LLM config or spec content")
            return

        yield self._info(
            f"Tool generated: {result['module_name']} "
            f"({len(result['functions'])} function(s)) → {result['output_path']}"
        )

        if collect is not None:
            collect.emit_metrics(
                "tool-forge",
                {
                    "duration_s": round(time.monotonic() - _t0, 3),
                    "status": "success",
                    "module_name": result["module_name"],
                    "n_functions": len(result["functions"]),
                },
            )

        yield {"summary": True, **result}

    def _load_spec(self, spec_text: str, spec_path: Path | str | None) -> str:
        if spec_text:
            return spec_text.strip()
        if spec_path:
            try:
                return Path(spec_path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                logging.getLogger(__name__).error("Cannot read spec file: %s", exc)
        return ""

    def _info(self, msg: str) -> dict:
        return {"level": "info", "msg": msg}

    def _error(self, msg: str) -> dict:
        return {"level": "error", "msg": msg}
