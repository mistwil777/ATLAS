"""agent-forge — AgentForgeAgent.

Generates ATLAS agent scaffolding from a spec file (CDC mode) or
interactive CLI dialogue (interactive mode).

Flow:
  parse spec / ask questions → extract params → render scaffold (scaffold.py)
  → validate contract (contract-validator level 1) → yield summary

LLM and logging backends are injected at construction time.
Default LLM: AWS Bedrock (same pattern as pair-forge).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Generator
from pathlib import Path

from agent_forge.prompts import INTERACTIVE_QUESTIONS, SPEC_EXTRACTION


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
        logging.getLogger("agent_forge").warning("Default LLM call failed: %s", exc)
        return None


def _default_log(level: str, message: str) -> None:
    getattr(logging.getLogger("agent_forge"), level, logging.getLogger("agent_forge").info)(message)


class AgentForgeAgent:
    """Generates ATLAS agent scaffolding from a spec or interactive dialogue.

    Args:
        output_dir: Parent directory where the new agent folder will be created.
                    Defaults to agents/ relative to cwd.
        config:     Optional config dict. Key: llm (passed to llm_fn).
        llm_fn:     callable(prompt, llm_config, max_tokens) -> str | None
        log_fn:     callable(level, message) -> None
    """

    def __init__(
        self,
        output_dir: Path | None = None,
        config: dict | None = None,
        llm_fn: Callable[[str, dict, int], str | None] | None = None,
        log_fn: Callable[[str, str], None] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir) if output_dir else Path("agents")
        self.config = config or {}
        self.llm_cfg = self.config.get("llm", {})
        self._llm = llm_fn or _default_llm
        self._log = log_fn or _default_log

    # ── Public entry point ─────────────────────────────────────────────────────

    def run(self, spec_path: Path | None = None) -> Generator[dict, None, None]:
        """Yield log dicts throughout scaffolding. Final dict contains key 'summary'."""
        _t0 = time.monotonic()
        yield self._info("agent-forge starting")

        if spec_path is not None:
            yield self._info(f"Mode: CDC — reading {spec_path.name}")
            spec_dict = yield from self._run_from_spec(spec_path)
        else:
            yield self._info("Mode: interactive")
            spec_dict = self._run_interactive()

        if spec_dict is None:
            yield self._error("Failed to extract agent parameters — aborting")
            return

        yield self._info(f"Agent name: {spec_dict.get('agent_name', '?')}")

        output_path = yield from self._generate_scaffolding(spec_dict)
        if output_path is None:
            yield self._error("Scaffolding generation failed — aborting")
            return

        yield self._info(f"Scaffolding written to: {output_path}")

        contract_passed, violations = self._validate(output_path)
        if contract_passed:
            yield self._info("Contract validation: PASSED")
        else:
            for v in violations:
                yield self._warn(f"Contract violation: {v}")

        yield {
            "summary": True,
            "agent_name": spec_dict.get("agent_name", ""),
            "output_path": str(output_path),
            "uses_llm": spec_dict.get("uses_llm", False),
            "contract_passed": contract_passed,
            "violations": violations,
        }

    # ── CDC mode ───────────────────────────────────────────────────────────────

    def _run_from_spec(self, spec_path: Path) -> Generator[dict, None, dict | None]:
        try:
            content = spec_path.read_text(encoding="utf-8")
        except OSError as exc:
            yield self._error(f"Cannot read spec file: {exc}")
            return None

        yield self._info("Extracting parameters from spec via LLM...")

        prompt = SPEC_EXTRACTION.format(spec_content=content)
        raw = self._llm(prompt, self.llm_cfg, 1024)
        if not raw:
            yield self._error("LLM returned no output for spec extraction")
            return None

        try:
            from atlas_core.judge_retry import judge_and_retry  # noqa: PLC0415

            from agent_forge.criteria import CRITERIA  # noqa: PLC0415

            def _reprompt(original: str, failed: list[dict]) -> str:
                failed_lines = "\n".join(f"- {f['criterion']}" for f in failed)
                return (
                    f"{prompt}\n\n"
                    f"Previous attempt did not satisfy:\n{failed_lines}\n"
                    "Fix the output and return valid JSON only."
                )

            result, verdict, attempts = judge_and_retry(
                output=raw,
                context=content,
                llm_fn=self._llm,
                llm_cfg=self.llm_cfg,
                reprompt_fn=_reprompt,
                agent_name="agent-forge",
                criteria=CRITERIA,
                parse_fn=self._parse_json_object,
                max_attempts=2,
            )
            if attempts > 1:
                yield self._info(f"Spec extraction passed after {attempts} attempt(s)")
            raw_parsed = result
        except ImportError:
            raw_parsed = self._parse_json_object(raw)

        if not raw_parsed:
            yield self._error("Could not extract valid spec parameters from LLM output")
            return None

        spec_dict = raw_parsed
        # Normalize uses_llm to bool
        spec_dict["uses_llm"] = str(spec_dict.get("uses_llm", "false")).lower() in (
            "true",
            "yes",
            "1",
        )
        return spec_dict

    # ── Interactive mode ───────────────────────────────────────────────────────

    def _run_interactive(self) -> dict | None:
        """Ask 4-5 questions via CLI input(). Isolated for future UI rebinding."""
        answers: dict = {}
        print("\nagent-forge — new agent setup\n")
        for key, question in INTERACTIVE_QUESTIONS:
            try:
                value = input(question).strip()
            except (EOFError, KeyboardInterrupt):
                return None
            answers[key] = value

        answers["uses_llm"] = answers.get("uses_llm", "no").lower() in ("yes", "y", "true", "1")
        answers.setdefault("description", "")
        answers.setdefault("domain", "")
        answers.setdefault("constraints", [])
        return answers

    # ── Scaffolding ────────────────────────────────────────────────────────────

    def _generate_scaffolding(self, spec_dict: dict) -> Generator[dict, None, Path | None]:
        from agent_forge import scaffold  # noqa: PLC0415

        agent_name = spec_dict.get("agent_name", "new-agent").lower().replace(" ", "-")
        pkg_name = agent_name.replace("-", "_")

        params = {
            "agent_slug": agent_name,
            "pkg_name": pkg_name,
            "description": spec_dict.get("description", ""),
            "input_type": spec_dict.get("input_type", ""),
            "output_type": spec_dict.get("output_type", ""),
            "domain": spec_dict.get("domain", ""),
            "uses_llm": bool(spec_dict.get("uses_llm", False)),
        }

        yield self._info("Generating scaffolding...")
        try:
            output_path = scaffold.generate(params, self.output_dir)
        except Exception as exc:  # noqa: BLE001
            yield self._error(f"Scaffolding generation failed: {exc}")
            return None

        return output_path

    # ── Contract validation ────────────────────────────────────────────────────

    def _validate(self, output_path: Path) -> tuple[bool, list[str]]:
        try:
            from contract_validator.contract import validate_contract  # noqa: PLC0415

            report = validate_contract(output_path)
            return report.passed, report.violations
        except ImportError:
            self._log("warning", "contract-validator not installed — skipping contract validation")
            return True, []
        except Exception as exc:  # noqa: BLE001
            return False, [str(exc)]

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _info(self, msg: str) -> dict:
        return {"level": "info", "msg": msg}

    def _warn(self, msg: str) -> dict:
        return {"level": "warning", "msg": msg}

    def _error(self, msg: str) -> dict:
        return {"level": "error", "msg": msg}

    @staticmethod
    def _strip_markdown(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        return raw.strip()

    def _parse_json_object(self, raw: str) -> dict | None:
        try:
            raw = self._strip_markdown(raw)
            start = raw.find("{")
            end = raw.rfind("}") + 1
            return json.loads(raw[start:end]) if start != -1 and end > 0 else None
        except Exception:  # noqa: BLE001
            return None
