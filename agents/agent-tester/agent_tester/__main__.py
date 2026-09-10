"""CLI entry point for agent-tester.

Commands:
  run --agent <path> [--level 1|2|3]   Validate an agent
  check                                 Check own dependencies
  init                                  Generate Dockerfile
  install                               Smart install with smoke tests
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-tester",
        description="ATLAS agent validator — structural, prompt, and behavioral tests.",
    )
    sub = parser.add_subparsers(dest="command")

    # run
    run_p = sub.add_parser("run", help="Validate an agent")
    run_p.add_argument("--agent", required=True, help="Path to the agent directory")
    run_p.add_argument("--level", type=int, choices=[1, 2, 3],
                       help="Run only this level (default: all levels)")
    run_p.add_argument("--llm-provider",  default=None)
    run_p.add_argument("--llm-model-id",  default=None)

    # check / init / install
    sub.add_parser("check",   help="Check own dependencies")
    sub.add_parser("init",    help="Generate Dockerfile")
    sub.add_parser("install", help="Smart install with smoke tests")

    args = parser.parse_args()

    if args.command == "check":
        _cmd_check()
    elif args.command == "init":
        _cmd_init()
    elif args.command == "install":
        _cmd_install()
    elif args.command == "run":
        _cmd_run(args)
    else:
        parser.print_help()


def _cmd_check() -> None:
    from agent_tester.tools import check_dependencies  # noqa: PLC0415
    result = check_dependencies()
    if not any(result.values()):
        print("All dependencies present and up to date.")
        return
    for item in result.get("missing", []):
        print(f"[MISSING]   {item['capability']} — {item['install_hint']}")
    for item in result.get("deprecated", []):
        print(f"[DEPRECATED] {item['capability']} — replaced by: {item['replaced_by']}")
    for item in result.get("untested_versions", []):
        print(f"[UNTESTED]  {item['capability']} — tested up to {item['tested_up_to']}, "
              f"found {item['installed_version']}")


def _cmd_init() -> None:
    from agent_tester.docker import generate_dockerfile  # noqa: PLC0415
    path = generate_dockerfile()
    print(f"Dockerfile generated: {path}")


def _cmd_install() -> None:
    from agent_tester.install import run_install  # noqa: PLC0415
    ok = run_install(verbose=True)
    sys.exit(0 if ok else 1)


def _cmd_run(args: argparse.Namespace) -> None:
    from agent_tester.contract import validate_contract  # noqa: PLC0415
    from agent_tester.golden import run_golden_tests  # noqa: PLC0415
    from agent_tester.prompt_tests import run_prompt_tests  # noqa: PLC0415
    from agent_tester.report import build_report, print_report  # noqa: PLC0415

    agent_path = Path(args.agent).resolve()
    if not agent_path.exists():
        print(f"[ERROR] Agent path not found: {agent_path}")
        sys.exit(1)

    level = args.level  # None = all levels

    # Resolve package name from pyproject.toml
    pkg_name = _resolve_pkg_name(agent_path)
    if not pkg_name:
        print("[ERROR] Cannot determine package name from pyproject.toml")
        sys.exit(1)

    contract_report = validate_contract(agent_path) if level in (None, 1) else None
    prompt_report   = None
    golden_report   = None

    if level in (None, 2) and (contract_report is None or contract_report.passed):
        # Add agent path to sys.path so we can import its package
        if str(agent_path) not in sys.path:
            sys.path.insert(0, str(agent_path))
        prompt_report = run_prompt_tests(agent_path, pkg_name)

    if level in (None, 3):
        llm_config = None
        if args.llm_provider and args.llm_model_id:
            llm_config = {"provider": args.llm_provider, "model_id": args.llm_model_id,
                          "temperature": 0.1}
        golden_report = run_golden_tests(agent_path, pkg_name, llm_config)

    if contract_report is None:
        # Ran only level 2 or 3 — create a dummy passed contract report
        from agent_tester.contract import ContractReport  # noqa: PLC0415
        contract_report = ContractReport(agent_path, pkg_name, True)

    report = build_report(agent_path, contract_report, prompt_report, golden_report)
    print_report(report)
    sys.exit(0 if report["overall"] == "PASS" else 1)


def _resolve_pkg_name(agent_path: Path) -> str | None:
    pyproject = agent_path / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        try:
            import tomllib  # noqa: PLC0415
        except ImportError:
            import tomli as tomllib  # noqa: PLC0415
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
        name = data.get("project", {}).get("name", "")
        return name.replace("-", "_") if name else None
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    main()
