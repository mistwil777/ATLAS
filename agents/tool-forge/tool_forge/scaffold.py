"""Writes a tool library to disk from the LLM-parsed spec."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from tool_forge.prompts import REPROMPT, TOOL_GENERATION_PROMPT


def generate(
    spec: str,
    output_dir: Path,
    llm_fn: Callable[[str, dict, int], str | None],
    llm_cfg: dict,
) -> dict | None:
    """Call LLM, parse response, write files to output_dir/<module_name>/."""
    prompt = TOOL_GENERATION_PROMPT.format(spec=spec)
    raw = llm_fn(prompt, llm_cfg, 2048)
    if not raw:
        return None

    result = _parse(raw)
    if result is None:
        raw2 = llm_fn(REPROMPT.format(previous_response=raw[:500]), llm_cfg, 2048)
        if not raw2:
            return None
        result = _parse(raw2)
        if result is None:
            return None

    return _write(result, output_dir)


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
        if "module_name" not in data or "functions" not in data:
            return None
        return data
    except json.JSONDecodeError:
        return None


def _write(spec: dict, output_dir: Path) -> dict:
    module_name = re.sub(r"[^a-z0-9_]", "_", spec["module_name"].lower())
    tool_dir = output_dir / module_name
    tool_dir.mkdir(parents=True, exist_ok=True)

    # __init__.py
    (tool_dir / "__init__.py").write_text("", encoding="utf-8")

    # module.py
    lines = []
    for imp in spec.get("imports", []):
        lines.append(imp)
    if lines:
        lines.append("")

    for fn in spec.get("functions", []):
        sig = fn["signature"].rstrip(":").strip()
        if not sig.startswith("def "):
            sig = f"def {sig}"
        lines.append(f"{sig}:")
        lines.append(f'    """{fn["docstring"]}"""')
        for body_line in fn["body"].splitlines():
            lines.append(f"    {body_line}" if body_line.strip() else "")
        lines.append("")

    (tool_dir / f"{module_name}.py").write_text("\n".join(lines), encoding="utf-8")

    # tests/
    test_dir = tool_dir / "tests"
    test_dir.mkdir(exist_ok=True)
    (test_dir / "__init__.py").write_text("", encoding="utf-8")

    test_lines = [f"from {module_name}.{module_name} import *  # noqa: F401, F403", ""]
    for i, t in enumerate(spec.get("tests", []), 1):
        test_lines.append(f"def test_{t['function']}_{i}():")
        for body_line in t["code"].splitlines():
            test_lines.append(f"    {body_line}" if body_line.strip() else "")
        test_lines.append("")

    (test_dir / f"test_{module_name}.py").write_text("\n".join(test_lines), encoding="utf-8")

    # pyproject.toml
    pyproject = f"""\
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{module_name.replace('_', '-')}"
version = "0.1.0"
description = "{spec.get('description', '')}"
requires-python = ">=3.10"
dependencies = []

[tool.setuptools.packages.find]
where = ["."]
include = ["{module_name}*"]
"""
    (tool_dir / "pyproject.toml").write_text(pyproject, encoding="utf-8")

    return {
        "output_path": str(tool_dir),
        "module_name": module_name,
        "functions": [f["name"] for f in spec.get("functions", [])],
        "description": spec.get("description", ""),
    }
