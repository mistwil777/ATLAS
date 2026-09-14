"""agent-tester — ATLAS agent validator."""

from contract_validator.contract import ContractReport, validate_contract
from contract_validator.golden import GoldenReport, run_golden_tests
from contract_validator.prompt_tests import PromptTestReport, run_prompt_tests
from contract_validator.report import build_report, print_report

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
