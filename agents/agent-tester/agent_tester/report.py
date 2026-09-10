"""Aggregates results from the 3 test levels into a structured report.

Provides both a machine-readable dict and a human-readable terminal output.
"""

from __future__ import annotations

from pathlib import Path

from agent_tester.contract import ContractReport
from agent_tester.golden import GoldenReport
from agent_tester.prompt_tests import PromptTestReport


def build_report(
    agent_path: Path,
    contract: ContractReport,
    prompts: PromptTestReport | None = None,
    golden: GoldenReport | None = None,
) -> dict:
    """Combine level results into a single structured report dict."""
    overall = "PASS"

    contract_section = {
        "passed": contract.passed,
        "violations": contract.violations,
        "warnings": contract.warnings,
    }
    if not contract.passed:
        overall = "FAIL"

    prompts_section = None
    if prompts is not None:
        prompts_section = {
            "passed": prompts.passed,
            "total": prompts.total,
            "failed": prompts.failed,
            "tests": [
                {"name": t.name, "passed": t.passed, "error": t.error} for t in prompts.tests
            ],
        }
        if not prompts.passed:
            overall = "FAIL"

    golden_section = None
    if golden is not None:
        golden_section = {
            "passed": golden.passed,
            "skipped": golden.skipped,
            "total": golden.total,
            "failed": golden.failed,
        }
        if not golden.passed:
            overall = "FAIL"

    return {
        "agent": contract.agent_name,
        "path": str(agent_path),
        "contract": contract_section,
        "prompts": prompts_section,
        "golden": golden_section,
        "overall": overall,
    }


def print_report(report: dict) -> None:
    """Print a human-readable summary to stdout."""
    _header(f"agent-tester — {report['agent']}")

    # Contract
    c = report["contract"]
    _section("Level 1 — Contract", c["passed"])
    for v in c["violations"]:
        _line(f"[VIOLATION] {v}", "red")
    for w in c["warnings"]:
        _line(f"[WARNING]   {w}", "yellow")
    if c["passed"] and not c["violations"]:
        _line("All structural checks passed.", "green")

    # Prompts
    p = report.get("prompts")
    if p:
        _section("Level 2 — Prompt contracts", p["passed"])
        for t in p.get("tests", []):
            status = "PASS" if t["passed"] else "FAIL"
            color = "green" if t["passed"] else "red"
            _line(f"  [{status}] {t['name']}", color)
            if t.get("error"):
                _line(f"         {t['error']}", "red")

    # Golden
    g = report.get("golden")
    if g:
        if g["skipped"]:
            _section("Level 3 — Golden tests", None)
            _line("Skipped (inspect-ai or LLM config unavailable)", "yellow")
        else:
            _section("Level 3 — Golden tests", g["passed"])

    # Overall
    color = "green" if report["overall"] == "PASS" else "red"
    overall = report["overall"]
    print()
    _line(f"Overall: {overall}", color)
    print()


# ── Terminal color helpers ─────────────────────────────────────────────────────

_COLORS = {"red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m", "reset": "\033[0m"}


def _header(text: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {text}")
    print(f"{'─' * 60}")


def _section(title: str, passed: bool | None) -> None:
    if passed is None:
        icon = "○"
    elif passed:
        icon = f"{_COLORS['green']}✓{_COLORS['reset']}"
    else:
        icon = f"{_COLORS['red']}✗{_COLORS['reset']}"
    print(f"\n{icon}  {title}")


def _line(text: str, color: str = "reset") -> None:
    c = _COLORS.get(color, "")
    r = _COLORS["reset"]
    print(f"   {c}{text}{r}")
