"""ImprovementAdvisorAgent — analyzes agent score trajectories and suggests improvements."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Generator

from improvement_advisor.prompts import ADVICE_PROMPT, REPROMPT
from improvement_advisor.stats import compute_stats

_log = logging.getLogger("improvement_advisor")


def _default_llm(prompt: str, config: dict, max_tokens: int = 1024) -> str | None:
    try:
        import boto3  # noqa: PLC0415

        region = config.get("region", "us-east-1")
        model_id = config.get("model_id", "us.amazon.nova-pro-v1:0")
        temperature = config.get("temperature", 0.2)
        client = boto3.client("bedrock-runtime", region_name=region)
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        return resp["output"]["message"]["content"][0]["text"]
    except Exception as exc:  # noqa: BLE001
        _log.error("LLM call failed: %s", exc)
        return None


class ImprovementAdvisorAgent:
    def __init__(
        self,
        config: dict | None = None,
        llm_fn: Callable | None = None,
        memory_storage_path=None,
    ) -> None:
        self._cfg = (config or {}).get("llm", config or {})
        self._llm = llm_fn or _default_llm
        self._memory_path = memory_storage_path

    def run(
        self,
        *,
        agent_name: str,
        trend_points: list[dict],
    ) -> Generator[dict, None, None]:
        yield self._info(f"Analysing trajectory for '{agent_name}' ({len(trend_points)} points)")

        stats = compute_stats(trend_points)
        n = stats["n_points"]
        confidence_tier = stats["confidence_tier"]

        if n == 0:
            yield self._error("No trend data available — cannot advise")
            return

        prompt = ADVICE_PROMPT.format(
            stats_summary=stats["stats_summary"],
            n_points=n,
            trend_json=json.dumps(trend_points, ensure_ascii=False, indent=2),
            confidence_tier=confidence_tier,
        )

        raw = self._llm(prompt, self._cfg, 1500)
        if not raw:
            yield self._info("LLM unavailable — returning deterministic analysis only")
            result = _deterministic_fallback(stats)
        else:
            result = self._parse(raw)
            if result is None:
                yield self._info("First response invalid JSON — re-prompting")
                reprompt = REPROMPT.format(previous_response=raw[:600])
                raw2 = self._llm(reprompt, self._cfg, 1500)
                result = self._parse(raw2) if raw2 else None

            if result is None:
                yield self._info("LLM parse failed — returning deterministic analysis only")
                result = _deterministic_fallback(stats)

        try:
            from atlas_core.collect import emit_metrics  # noqa: PLC0415

            emit_metrics(
                "improvement-advisor",
                {
                    "pain_points": len(result.get("pain_points", [])),
                    "suggested_actions": len(result.get("suggested_actions", [])),
                    "confidence_tier": confidence_tier,
                    "n_points": n,
                },
            )
        except ImportError:
            pass

        yield self._info(
            f"Diagnosis complete — {len(result.get('pain_points', []))} pain points, "
            f"{len(result.get('suggested_actions', []))} actions, confidence={confidence_tier}"
        )
        yield {
            "summary": True,
            "agent_name": agent_name,
            "pain_points": result.get("pain_points", []),
            "suggested_actions": sorted(
                result.get("suggested_actions", []),
                key=lambda a: a.get("priority", 99),
            ),
            "forecast": result.get("forecast", {}),
            "confidence_tier": confidence_tier,
            "n_points": n,
        }

    def _parse(self, raw: str) -> dict | None:
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
            if "pain_points" not in data or "suggested_actions" not in data:
                return None
            return data
        except json.JSONDecodeError:
            return None

    def _info(self, msg: str) -> dict:
        _log.info(msg)
        return {"level": "info", "msg": msg}

    def _error(self, msg: str) -> dict:
        _log.error(msg)
        return {"level": "error", "msg": msg}


def _deterministic_fallback(stats: dict) -> dict:
    """Build a minimal advice result from deterministic stats when LLM is unavailable."""
    pain_points = []
    suggested_actions = []
    priority = 1

    for cname, cdata in stats.get("crit_stats", {}).items():
        if cdata.get("avg", 5) < 3.5 or cdata.get("unaddressed"):
            pain_points.append(
                {
                    "criterion": cname,
                    "observation": f"avg={cdata['avg']:.1f} σ={cdata['std']:.2f} trend={cdata['trend']}",
                    "severity": "high" if cdata.get("avg", 5) < 2.5 else "medium",
                }
            )
            suggested_actions.append(
                {
                    "action": f"Improve criterion '{cname}' (currently avg {cdata['avg']:.1f}/5)",
                    "rationale": "Below threshold"
                    + (" — unaddressed across runs" if cdata.get("unaddressed") else ""),
                    "priority": priority,
                }
            )
            priority += 1

    return {
        "pain_points": pain_points,
        "suggested_actions": suggested_actions,
        "forecast": {
            "note": "LLM unavailable — deterministic analysis only, no probabilistic forecast"
        },
    }
