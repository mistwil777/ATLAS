"""atlas-judge — AtlasJudgeAgent.

LLM-as-judge evaluator for all ATLAS agent outputs.

Modes:
  pointwise  — score an output against explicit criteria (1-5 per criterion)
  pairwise   — compare two outputs, choose the better one (position-bias mitigated)
  reference  — compare an output to a validated reference

Criteria are loaded from the evaluated agent's own criteria.py if agent_name is provided,
or passed directly as a list of strings.

Evaluations are indexed in atlas-memory for continuous improvement.
"""

from __future__ import annotations

import importlib
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
        logging.getLogger("atlas_judge").warning("LLM call failed: %s", exc)
        return None


class AtlasJudgeAgent:
    """Evaluates ATLAS agent outputs using LLM-as-judge.

    Args:
        config:               Optional config dict. Key: llm (passed to llm_fn).
        llm_fn:               callable(prompt, llm_config, max_tokens) -> str | None
        memory_storage_path:  Path to atlas-memory storage (optional).
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
        operation: str = "pointwise",
        output: str = "",
        output_a: str = "",
        output_b: str = "",
        reference: str = "",
        criteria: list[str] | None = None,
        agent_name: str = "",
        context: str = "",
    ) -> Generator[dict, None, None]:
        """Evaluate an output. Yields log dicts then summary."""
        from atlas_core import collect  # noqa: PLC0415

        _t0 = time.monotonic()
        yield self._info(f"atlas-judge: {operation}")

        resolved_criteria = self._resolve_criteria(criteria, agent_name)
        if not resolved_criteria:
            yield self._error("No criteria provided — pass criteria list or agent_name")
            return

        yield self._info(f"Criteria: {len(resolved_criteria)} loaded")

        from atlas_judge.evaluator import pairwise, pointwise, reference_based  # noqa: PLC0415

        result = None
        if operation == "pointwise":
            if not output:
                yield self._error("pointwise requires output")
                return
            result = pointwise(output, resolved_criteria, context, self._llm, self.llm_cfg)

        elif operation == "pairwise":
            if not output_a or not output_b:
                yield self._error("pairwise requires output_a and output_b")
                return
            result = pairwise(
                output_a, output_b, resolved_criteria, context, self._llm, self.llm_cfg
            )

        elif operation == "reference":
            if not output or not reference:
                yield self._error("reference requires output and reference")
                return
            result = reference_based(
                output, reference, resolved_criteria, context, self._llm, self.llm_cfg
            )

        else:
            yield self._error(
                f"Unknown operation: {operation}. Use pointwise, pairwise, or reference."
            )
            return

        if result is None:
            yield self._error("Evaluation failed — LLM did not return valid JSON")
            return

        passed = result.get("passed", False)
        score = result.get("score")
        yield self._info(
            f"Evaluation complete — passed={passed}" + (f", score={score}/5" if score else "")
        )

        self._index_in_memory(output or output_a, result, agent_name)

        collect.emit_metrics("atlas-judge", {
            "duration_s": round(time.monotonic() - _t0, 3),
            "status": "success",
            "operation": operation,
            "passed": result.get("passed"),
            "score": result.get("score"),
        })
        yield {"summary": True, **result}

    def _resolve_criteria(self, criteria: list[str] | None, agent_name: str) -> list[str]:
        if criteria:
            return criteria

        if agent_name:
            module_name = agent_name.replace("-", "_")
            try:
                mod = importlib.import_module(f"{module_name}.criteria")
                agent_criteria = getattr(mod, "CRITERIA", {})
                if agent_criteria:
                    return list(agent_criteria.values())
            except ImportError:
                logging.getLogger("atlas_judge").warning(
                    "No criteria.py found for agent %s", agent_name
                )

        return []

    def _index_in_memory(self, content: str, result: dict, agent_name: str) -> None:
        try:
            from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

            kwargs = {}
            if self._memory_path:
                kwargs["storage_path"] = self._memory_path

            mem = AtlasMemoryAgent(**kwargs)
            list(
                mem.run(
                    operation="index",
                    entry_type="judge_eval",
                    content=content[:500],
                    payload={
                        "score": result.get("score"),
                        "passed": result.get("passed"),
                        "criteria_scores": result.get("criteria_scores", []),
                        "feedback": result.get("feedback", ""),
                        "mode": result.get("mode"),
                        "winner": result.get("winner"),
                    },
                    metadata={"agent": agent_name} if agent_name else {},
                )
            )
        except Exception:  # noqa: BLE001
            pass

    def _info(self, msg: str) -> dict:
        return {"level": "info", "msg": msg}

    def _error(self, msg: str) -> dict:
        return {"level": "error", "msg": msg}
