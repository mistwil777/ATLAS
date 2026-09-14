"""atlas-orchestrator — AtlasOrchestratorAgent.

Chef d'orchestre de l'Agentic Mesh ATLAS.

Flow:
  1. Segmente le CDC via cdc-router (in-process)
  2. Exécute chaque bloc via atlas-bus (subprocess isolé)
  3. Évalue chaque output via atlas-judge (in-process)
  4. Indexe le rapport dans atlas-memory
  5. Retourne un rapport structuré par bloc

Dégradation gracieuse : chaque dépendance est optionnelle sauf cdc-router.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator
from pathlib import Path


def _default_llm(prompt: str, llm_config: dict, max_tokens: int = 1024) -> str | None:
    try:
        import boto3  # noqa: PLC0415  # type: ignore[import-untyped]
        from botocore.config import Config  # noqa: PLC0415  # type: ignore[import-untyped]

        model_id = llm_config.get("model_id", "us.amazon.nova-pro-v1:0")
        temperature = float(llm_config.get("temperature", 0.1))
        region = llm_config.get("region", "us-east-1")
        client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(connect_timeout=10, read_timeout=120, retries={"max_attempts": 2}),
        )
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        return response["output"]["message"]["content"][0]["text"].strip()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_orchestrator").warning("LLM call failed: %s", exc)
        return None


class AtlasOrchestratorAgent:
    """Orchestrates the full ATLAS Agentic Mesh pipeline from a CDC.

    Args:
        config:               Optional config dict. Key: llm (passed to llm_fn).
        llm_fn:               callable(prompt, llm_config, max_tokens) -> str | None
        memory_storage_path:  Path to atlas-memory storage (optional).
        bus_fn:               Callable returning a Bus instance (injectable for tests).
        judge_fn:             Callable returning an AtlasJudgeAgent (injectable for tests).
    """

    def __init__(
        self,
        config: dict | None = None,
        llm_fn: Callable[[str, dict, int], str | None] | None = None,
        memory_storage_path: Path | None = None,
        bus_fn: Callable | None = None,
        judge_fn: Callable | None = None,
    ) -> None:
        self.config = config or {}
        self.llm_cfg = self.config.get("llm", {})
        self._llm = llm_fn or _default_llm
        self._memory_path = memory_storage_path
        self._bus_fn = bus_fn
        self._judge_fn = judge_fn

    def run(
        self,
        cdc_text: str = "",
        cdc_path: str = "",
    ) -> Generator[dict, None, None]:
        """Orchestrate the full CDC → agents → judge pipeline. Yields log dicts then summary."""
        from atlas_core import collect  # noqa: PLC0415

        _t0 = time.monotonic()
        yield self._info("atlas-orchestrator: starting")

        text = cdc_text
        if not text and cdc_path:
            try:
                text = Path(cdc_path).read_text(encoding="utf-8")
            except OSError as exc:
                yield self._error(f"Cannot read CDC file: {exc}")
                return

        if not text:
            yield self._error("cdc_text or cdc_path required")
            return

        yield self._info("Segmenting CDC via cdc-router...")

        from atlas_orchestrator.pipeline import orchestrate  # noqa: PLC0415

        report = orchestrate(
            cdc_text=text,
            llm_fn=self._llm,
            llm_cfg=self.llm_cfg,
            memory_path=self._memory_path,
            bus_fn=self._bus_fn,
            judge_fn=self._judge_fn,
        )

        if report is None:
            yield self._error("Orchestration failed — cdc-router returned no result")
            return

        blocs_total = report["blocs_total"]
        blocs_passed = report["blocs_passed"]
        yield self._info(f"Orchestration complete — {blocs_passed}/{blocs_total} blocs passed")

        self._index_in_memory(text, report)

        collect.emit_metrics("atlas-orchestrator", {
            "duration_s": round(time.monotonic() - _t0, 3),
            "status": "success",
            "blocs_total": report.get("blocs_total"),
            "blocs_passed": report.get("blocs_passed"),
        })
        yield {"summary": True, **report}

    def _index_in_memory(self, cdc_text: str, report: dict) -> None:
        try:
            from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

            kwargs: dict = {}
            if self._memory_path:
                kwargs["storage_path"] = self._memory_path

            mem = AtlasMemoryAgent(**kwargs)
            list(
                mem.run(
                    operation="index",
                    entry_type="orchestration",
                    content=cdc_text[:500],
                    payload={
                        "blocs_total": report["blocs_total"],
                        "blocs_passed": report["blocs_passed"],
                        "overall_passed": report["overall_passed"],
                        "pipeline": report.get("pipeline", []),
                    },
                )
            )
        except Exception:  # noqa: BLE001
            pass

    def _info(self, msg: str) -> dict:
        return {"level": "info", "msg": msg}

    def _error(self, msg: str) -> dict:
        return {"level": "error", "msg": msg}
