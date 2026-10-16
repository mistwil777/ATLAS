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

from agent_forge.prompts import (
    INTERACTIVE_QUESTIONS,
    PROMPT_GENERATION,
    SPEC_EXTRACTION,
    TOOL_RESOLUTION,
)


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

        # Resolve tools required by this agent
        required_tools = spec_dict.get("required_tools") or []
        if required_tools:
            yield self._info(f"Resolving {len(required_tools)} required tool(s)...")
            resolved_tools = yield from self._resolve_tools(required_tools)
            spec_dict["resolved_tools"] = resolved_tools
        else:
            spec_dict["resolved_tools"] = []

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
            "component_type": spec_dict.get("component_type", "agent"),
            "contract_passed": contract_passed,
            "violations": violations,
            "resolved_tools": spec_dict.get("resolved_tools", []),
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

        # Generate the actual prompt body from the full spec (second LLM call)
        if spec_dict.get("uses_llm"):
            yield self._info("Generating prompt instructions from spec...")
            prompt_gen = PROMPT_GENERATION.format(spec_content=content)
            main_prompt_body = self._llm(prompt_gen, self.llm_cfg, 2048)
            if main_prompt_body:
                spec_dict["main_prompt_body"] = main_prompt_body.strip()
            else:
                yield self._warn("Prompt generation returned no output — using TODO placeholder")

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
            "main_prompt_body": spec_dict.get("main_prompt_body"),
            "component_type": spec_dict.get(
                "component_type", "agent" if spec_dict.get("uses_llm") else "tool"
            ),
        }

        yield self._info("Generating scaffolding...")
        try:
            output_path = scaffold.generate(params, self.output_dir)
        except Exception as exc:  # noqa: BLE001
            yield self._error(f"Scaffolding generation failed: {exc}")
            return None

        # Format and auto-fix generated code with ruff
        import subprocess as _sp  # noqa: PLC0415

        try:
            _sp.run(["ruff", "format", str(output_path)], capture_output=True, timeout=30)
            _sp.run(
                ["ruff", "check", "--fix", str(output_path)],
                capture_output=True,
                timeout=30,
            )
            # Report only what ruff could not auto-fix
            check = _sp.run(
                ["ruff", "check", str(output_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if check.returncode != 0:
                yield self._info("ruff: unfixable errors found — asking LLM to fix...")
                yield from self._llm_fix_ruff_errors(output_path, check.stdout)
                _sp.run(["ruff", "format", str(output_path)], capture_output=True, timeout=30)
        except FileNotFoundError:
            yield self._warn("ruff not found — skipping lint validation")
        except Exception as exc:  # noqa: BLE001
            yield self._warn(f"ruff validation failed: {exc}")

        return output_path

    # ── LLM lint repair ───────────────────────────────────────────────────────

    def _llm_fix_ruff_errors(
        self, output_path: Path, ruff_output: str
    ) -> Generator[dict, None, None]:
        """Ask the LLM to fix ruff errors that --fix could not auto-correct."""
        import re as _re  # noqa: PLC0415

        # Group errors by file
        file_errors: dict[str, list[str]] = {}
        for line in ruff_output.strip().splitlines():
            m = _re.match(r"(.+?\.py):\d+:\d+:", line)
            if m:
                fpath = m.group(1)
                file_errors.setdefault(fpath, []).append(line)

        for fpath, errors in file_errors.items():
            try:
                p = Path(fpath)
                if not p.exists():
                    continue
                original = p.read_text(encoding="utf-8")
                error_list = "\n".join(errors[:10])
                fix_prompt = (
                    f"The following Python file has ruff lint errors that could not be auto-fixed.\n"
                    f"Fix ONLY the reported errors. Return the complete corrected file, nothing else.\n\n"
                    f"Errors:\n{error_list}\n\n"
                    f"File ({p.name}):\n```python\n{original}\n```"
                )
                fixed = self._llm(fix_prompt, self.llm_cfg, 4096)
                if fixed:
                    # Extract code block if wrapped in markdown
                    code_m = _re.search(r"```python\s*(.*?)```", fixed, _re.DOTALL)
                    fixed_code = code_m.group(1) if code_m else fixed
                    p.write_text(fixed_code, encoding="utf-8")
                    yield self._info(f"ruff: LLM fixed {p.name}")
                else:
                    yield self._warn(f"ruff: LLM returned no fix for {p.name}")
            except Exception as exc:  # noqa: BLE001
                yield self._warn(f"ruff: LLM fix failed for {fpath}: {exc}")

    # ── Tool resolution ────────────────────────────────────────────────────────

    def _resolve_tools(self, required_tools: list[dict]) -> Generator[dict, None, list[dict]]:
        """Resolve each required tool via: catalogue → open-source → generate."""
        resolved: list[dict] = []
        catalogue = self._load_tool_catalogue()

        for tool_spec in required_tools:
            name = tool_spec.get("name", "unknown")
            description = tool_spec.get("description", "")

            # Case 1 — catalogue
            match = self._find_in_catalogue(name, description, catalogue)
            if match:
                yield self._info(f"Tool '{name}': found in catalogue → reuse '{match}'")
                resolved.append({**tool_spec, "resolution": "catalogue", "ref": match})
                continue

            # Case 2 — open-source recommendation
            oss = self._find_open_source(name, description)
            if oss:
                yield self._info(
                    f"Tool '{name}': open-source recommendation → {oss['library']} "
                    f"({oss['reason']})"
                )
                resolved.append({**tool_spec, "resolution": "open_source", **oss})
                continue

            # Case 3 — generate with tool-forge + register in KG
            yield self._info(f"Tool '{name}': no existing solution — generating with tool-forge...")
            generated_name = yield from self._generate_and_register_tool(name, description)
            if generated_name:
                resolved.append({**tool_spec, "resolution": "generated", "ref": generated_name})
            else:
                yield self._warn(f"Tool '{name}': generation failed — skipping")

        return resolved

    def _load_tool_catalogue(self) -> list[dict]:
        """Load tool nodes from atlas-memory KG. Returns [] if unavailable."""
        try:
            from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

            mem = AtlasMemoryAgent()
            graph = next(
                (e for e in mem.run(operation="get_graph") if e.get("summary")),
                None,
            )
            if not graph:
                return []
            result = graph.get("result", {})
            nodes = result.get("nodes", [])
            return [n for n in nodes if n.get("node_type") == "tool"]
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"Could not load tool catalogue: {exc}")
            return []

    def _find_in_catalogue(self, name: str, description: str, catalogue: list[dict]) -> str | None:
        """Return the catalogue tool name if a match is found, else None."""
        name_lower = name.lower().replace("-", "_")
        for node in catalogue:
            node_id: str = node.get("id", "")
            node_caps: list[str] = node.get("capabilities", [])
            node_summary: str = node.get("cdc_summary", "")
            # Exact name match
            if node_id.lower().replace("-", "_") == name_lower:
                return node_id
            # Capability keyword overlap
            desc_words = set(description.lower().split())
            caps_text = " ".join(node_caps + [node_summary]).lower()
            overlap = sum(1 for w in desc_words if len(w) > 4 and w in caps_text)
            if overlap >= 3:
                return node_id
        return None

    def _find_open_source(self, name: str, description: str) -> dict | None:
        """Ask LLM whether a known open-source library covers this tool need."""
        prompt = TOOL_RESOLUTION.format(tool_name=name, tool_description=description)
        raw = self._llm(prompt, self.llm_cfg, 256)
        if not raw:
            return None
        try:
            import json as _json  # noqa: PLC0415

            raw = raw.strip()
            start = raw.find("{")
            end = raw.rfind("}") + 1
            data = _json.loads(raw[start:end])
            if data.get("has_oss") and data.get("library"):
                return {
                    "library": data["library"],
                    "install": data.get("install"),
                    "reason": data.get("reason", ""),
                }
        except Exception:  # noqa: BLE001
            pass
        return None

    def _generate_and_register_tool(
        self, name: str, description: str
    ) -> Generator[dict, None, str | None]:
        """Generate tool via tool-forge and register it in the KG."""
        try:
            from tool_forge.agent import ToolForgeAgent  # noqa: PLC0415
        except ImportError:
            yield self._warn("tool-forge not installed — cannot generate tool")
            return None

        libs_dir = Path(self.output_dir).parent / "libs"
        forge = ToolForgeAgent(output_dir=libs_dir, config=self.config, llm_fn=self._llm)

        spec_text = f"Tool name: {name}\nDescription: {description}\n"
        result = None
        for event in forge.run(spec_text=spec_text):
            if event.get("summary"):
                result = event
            else:
                yield event  # bubble up tool-forge logs

        if not result:
            return None

        module_name: str = result.get("module_name", name)

        # Register in KG
        try:
            from atlas_memory.agent import AtlasMemoryAgent  # noqa: PLC0415

            mem = AtlasMemoryAgent()
            list(
                mem.run(
                    operation="register_tool",
                    name=module_name,
                    capabilities=[description],
                    cdc_summary=description,
                    tags=["generated"],
                )
            )
            yield self._info(f"Tool '{module_name}' registered in catalogue")
        except Exception as exc:  # noqa: BLE001
            yield self._warn(f"Could not register tool in KG: {exc}")

        return module_name

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
