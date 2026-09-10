"""Smoke test — fake_agent passes the ATLAS contract (positive case)."""

from pathlib import Path

FAKE_AGENT = Path(__file__).parent / "fixtures" / "fake_agent"


def test_contract_passed():
    from agent_tester.contract import validate_contract
    report = validate_contract(FAKE_AGENT)
    assert report.passed, (
        "fake_agent should pass the contract but got violations:\n"
        + "\n".join(f"  - {v}" for v in report.violations)
    )


def test_contract_no_violations():
    from agent_tester.contract import validate_contract
    report = validate_contract(FAKE_AGENT)
    assert report.violations == [], f"Unexpected violations: {report.violations}"


def test_contract_agent_name_resolved():
    from agent_tester.contract import validate_contract
    report = validate_contract(FAKE_AGENT)
    assert report.agent_name == "fake_agent"
