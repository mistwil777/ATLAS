"""agent-tester — ATLAS agent validator."""

from agent_tester.contract import ContractReport, validate_contract
from agent_tester.golden import GoldenReport, run_golden_tests
from agent_tester.prompt_tests import PromptTestReport, run_prompt_tests
from agent_tester.report import build_report, print_report

__version__ = "1.0.0"
__all__ = [
    "validate_contract",
    "ContractReport",
    "run_prompt_tests",
    "PromptTestReport",
    "run_golden_tests",
    "GoldenReport",
    "build_report",
    "print_report",
]
