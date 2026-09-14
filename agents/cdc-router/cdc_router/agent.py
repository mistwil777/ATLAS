"""cdc-router — CdcRouterAgent.

Segments a CDC into agentic blocs, routes to existing ATLAS agents,
and proposes creation of missing agents via agent-forge.

Flow:
  read CDC → fetch RAG examples + KG agents (atlas-memory, optional)
  → LLM segmentation (binary rules) → route blocs → yield summary
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
        logging.getLogger("cdc_router").warning("LLM call failed: %s", exc)
        return None


class CdcRouterAgent:
    """Routes a CDC to ATLAS agents or proposes new ones.

    Args:
        config:   Optional config dict. Key: llm (passed to llm_fn).
        llm_fn:   callable(prompt, llm_config, max_tokens) -> str | None
        memory_storage_path: Path to atlas-memory storage (optional).
    """

    def __init__(
        self,
        config: dict | None = None,
        llm_fn: Callable[[str, dict, int], str | None] | None = None,
        memory_storage_path: Path | None = None,
    ) -> None:
        self.config = config or {}
        self.llm_cfg = self.config.get("llm", {})
        self._llm = llm_fn or _default_llm
        self._memory_path = memory_storage_path

    def run(
        self,
        cdc_text: str = "",
        cdc_path: str = "",
    ) -> Generator[dict, None, None]:
        """Segment a CDC and produce a routing plan. Yields log dicts then summary."""
        from atlas_core import collect  # noqa: PLC0415

        _t0 = time.monotonic()
        yield self._info("cdc-router starting")

        text = self._load_cdc(cdc_text, cdc_path)
        if not text:
            yield self._error("No CDC text provided — pass cdc_text or cdc_path")
            return

        yield self._info(f"CDC loaded ({len(text)} chars)")

        rag_examples, kg_graph = self._fetch_memory(text)
        if rag_examples:
            yield self._info(f"RAG: {len(rag_examples)} similar CDCs found")
        if kg_graph.get("nodes"):
            yield self._info(f"KG: {len(kg_graph['nodes'])} agents in graph")

        yield self._info("Segmenting via LLM...")
        from cdc_router.segmenter import segment  # noqa: PLC0415

        segmentation, judge_verdict, attempts = segment(
            text, rag_examples, kg_graph, self._llm, self.llm_cfg
        )
        if segmentation is None:
            yield self._error("LLM segmentation failed — check LLM config or CDC content")
            return

        blocs = segmentation.get("blocs", [])
        yield self._info(f"Segmented into {len(blocs)} blocs")

        if judge_verdict is not None:
            passed = judge_verdict.get("passed", False)
            score = judge_verdict.get("score")
            yield self._info(
                f"Quality check: {'passed' if passed else 'failed'}"
                + (
                    f" (score {score}/5" + (f", {attempts} attempt(s))" if attempts > 1 else ")")
                    if score
                    else ""
                )
            )
        else:
            yield self._info("Quality check: skipped (atlas-judge unavailable)")

        from cdc_router.router import route  # noqa: PLC0415

        routing = route(blocs, kg_graph)

        if routing["requires_creation"]:
            for name in routing["requires_creation"]:
                yield self._info(f"Agent to create: {name} (will need agent-forge)")

        if routing["atlas_agents_reused"]:
            for name in routing["atlas_agents_reused"]:
                yield self._info(f"Existing agent reused: {name}")

        self._index_in_memory(text, routing)

        collect.emit_metrics(
            "cdc-router",
            {
                "duration_s": round(time.monotonic() - _t0, 3),
                "status": "success",
                "blocs": len(blocs),
                "requires_creation": len(routing["requires_creation"]),
            },
        )
        yield {
            "summary": True,
            "blocs": routing["blocs"],
            "pipeline": routing["pipeline"],
            "requires_creation": routing["requires_creation"],
            "atlas_agents_reused": routing["atlas_agents_reused"],
        }

    def _load_cdc(self, cdc_text: str, cdc_path: str) -> str:
        if cdc_text:
            return cdc_text.strip()
        if cdc_path:
            try:
                return Path(cdc_path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                logging.getLogger("cdc_router").error("Cannot read CDC file: %s", exc)
        return ""

    def _fetch_memory(self, cdc_text: str) -> tuple[list[dict], dict]:
        try:
            from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

            kwargs = {}
            if self._memory_path:
                kwargs["storage_path"] = self._memory_path

            mem = AtlasMemoryAgent(**kwargs)

            rag_events = list(
                mem.run(operation="search", query=cdc_text, entry_type="cdc", top_k=3)
            )
            rag_summary = next((e for e in rag_events if e.get("summary")), None)
            examples = rag_summary["result"]["results"] if rag_summary else []

            kg_events = list(mem.run(operation="get_graph"))
            kg_summary = next((e for e in kg_events if e.get("summary")), None)
            graph = kg_summary["result"] if kg_summary else {}

            return examples, graph
        except ImportError:
            return [], {}
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("cdc_router").warning("atlas-memory unavailable: %s", exc)
            return [], {}

    def _index_in_memory(self, cdc_text: str, routing: dict) -> None:
        try:
            from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

            kwargs = {}
            if self._memory_path:
                kwargs["storage_path"] = self._memory_path

            mem = AtlasMemoryAgent(**kwargs)
            list(
                mem.run(
                    operation="index",
                    entry_type="cdc",
                    content=cdc_text,
                    payload={"segmentation": routing["blocs"], "pipeline": routing["pipeline"]},
                )
            )
        except Exception:  # noqa: BLE001
            pass

    def _info(self, msg: str) -> dict:
        return {"level": "info", "msg": msg}

    def _error(self, msg: str) -> dict:
        return {"level": "error", "msg": msg}
