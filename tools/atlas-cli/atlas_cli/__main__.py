"""atlas — single entry point CLI for ATLAS agentic mesh.

Commands:
  atlas run --cdc <path>     Analyse a CDC and run the full agent pipeline
  atlas dashboard            Start the HITL review dashboard
  atlas check                Check installed agents and LLM connectivity
  atlas init                 Create a starter atlas.toml in the current directory
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import webbrowser
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
_log = logging.getLogger("atlas")

_TOML_TEMPLATE = """\
# atlas.toml — ATLAS project configuration

[llm]
# provider: bedrock | openai | anthropic
provider = "bedrock"

# --- AWS Bedrock (default) ---
model_id  = "us.amazon.nova-pro-v1:0"
region    = "us-east-1"
temperature = 0.1

# --- OpenAI (uncomment to use) ---
# provider   = "openai"
# model      = "gpt-4o-mini"
# api_key    = "sk-..."
# temperature = 0.1

# --- Anthropic (uncomment to use) ---
# provider = "anthropic"
# model    = "claude-haiku-4-5-20251001"
# api_key  = "sk-ant-..."

[dashboard]
port = 8050
auto_open = true

[collect]
# off | internal | client
mode = "internal"
"""


def cmd_init(args: argparse.Namespace) -> None:
    target = Path.cwd() / "atlas.toml"
    if target.exists():
        print(f"atlas.toml already exists at {target}")
        return
    target.write_text(_TOML_TEMPLATE, encoding="utf-8")
    print(f"Created {target}")
    print("Edit [llm] section to configure your LLM provider, then run: atlas run --cdc <file>")


def cmd_check(args: argparse.Namespace) -> None:
    from atlas_cli.config import load  # noqa: PLC0415

    cfg = load()
    print("ATLAS health check\n")

    agents = [
        ("atlas-core", "atlas_core"),
        ("atlas-memory", "atlas_memory"),
        ("atlas-bus", "atlas_bus"),
        ("atlas-judge", "atlas_judge"),
        ("atlas-orchestrator", "atlas_orchestrator"),
        ("cdc-router", "cdc_router"),
        ("agent-forge", "agent_forge"),
        ("pair-forge", "pair_forge"),
        ("atlas-dashboard", "atlas_dashboard"),
    ]
    all_ok = True
    for name, module in agents:
        try:
            __import__(module)
            print(f"  OK  {name}")
        except ImportError:
            print(f"  ✗   {name}  (not installed)")
            all_ok = False

    print()
    provider = cfg.get("llm", {}).get("provider", "bedrock")
    print(f"LLM provider: {provider}")
    if not cfg:
        print("  No atlas.toml found — run: atlas init")

    sys.exit(0 if all_ok else 1)


def cmd_dashboard(args: argparse.Namespace) -> None:
    from atlas_cli.config import load  # noqa: PLC0415

    cfg = load()
    port = args.port or cfg.get("dashboard", {}).get("port", 8050)
    url = f"http://127.0.0.1:{port}"
    print(f"Starting ATLAS Dashboard at {url}")
    if cfg.get("dashboard", {}).get("auto_open", True) and not args.no_browser:
        webbrowser.open(url)
    try:
        import uvicorn  # noqa: PLC0415
        from atlas_dashboard.app import app  # noqa: PLC0415

        uvicorn.run(app, host="127.0.0.1", port=port)
    except ImportError:
        _log.error("atlas-dashboard not installed")
        sys.exit(1)


def cmd_run(args: argparse.Namespace) -> None:
    import os  # noqa: PLC0415

    from atlas_cli.config import llm_fn_from_config, load  # noqa: PLC0415

    cdc_path = Path(args.cdc)
    if not cdc_path.exists():
        _log.error("CDC file not found: %s", cdc_path)
        sys.exit(1)

    cfg = load()
    if not cfg:
        _log.warning(
            "No atlas.toml found — using defaults (Bedrock). Run 'atlas init' to configure."
        )

    # Set collect mode from config
    collect_mode = cfg.get("collect", {}).get("mode", "off")
    os.environ.setdefault("ATLAS_COLLECT", collect_mode)

    llm_fn, llm_cfg = llm_fn_from_config(cfg)

    # Start dashboard in background if requested
    dashboard_proc = None
    if not args.no_dashboard:
        port = cfg.get("dashboard", {}).get("port", 8050)
        dashboard_proc = _start_dashboard(port, cfg)

    # Run orchestration
    print(f"\nATLAS — analysing {cdc_path.name}\n")
    try:
        from atlas_orchestrator.pipeline import orchestrate  # noqa: PLC0415

        cdc_text = cdc_path.read_text(encoding="utf-8")
        report = orchestrate(cdc_text=cdc_text, llm_fn=llm_fn, llm_cfg=llm_cfg)
    except ImportError:
        _log.error("atlas-orchestrator not installed")
        sys.exit(1)

    if report is None:
        _log.error("Orchestration failed — no report produced")
        sys.exit(1)

    _print_report(report)

    if dashboard_proc and not args.no_dashboard:
        port = cfg.get("dashboard", {}).get("port", 8050)
        url = f"http://127.0.0.1:{port}"
        print(f"\nDashboard running at {url}")
        if cfg.get("dashboard", {}).get("auto_open", True):
            webbrowser.open(url)
        print("Press Ctrl+C to stop.\n")
        try:
            dashboard_proc.wait()
        except KeyboardInterrupt:
            dashboard_proc.terminate()


def _start_dashboard(port: int, cfg: dict) -> subprocess.Popen | None:
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "atlas_dashboard.__main__", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import time

        time.sleep(1.5)
        return proc
    except Exception as exc:  # noqa: BLE001
        _log.warning("Could not start dashboard: %s", exc)
        return None


def _print_report(report: dict) -> None:
    run_id = report.get("run_id", "—")
    requires_review = report.get("requires_review", False)
    blocs = report.get("bloc_results", [])

    print(f"Run ID : {run_id}")
    print(f"Blocs  : {len(blocs)}")
    print(f"Review : {'YES — open dashboard to approve/reject' if requires_review else 'no'}")
    print()

    for i, b in enumerate(blocs, 1):
        status = b.get("status", "?")
        agent = b.get("agent") or "—"
        score = b.get("judge_score")
        score_str = f"  score={score:.2f}" if score is not None else ""
        review_flag = "  [REVIEW REQUIRED]" if b.get("requires_review") else ""
        print(f"  Bloc {i}  {agent:<20}  {status}{score_str}{review_flag}")

    print()
    summary = report.get("summary", {})
    if summary:
        print("Full report:")
        print(json.dumps(summary, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="ATLAS agentic mesh — analyse a CDC and run the full agent pipeline",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Create atlas.toml in current directory")
    p_init.set_defaults(func=cmd_init)

    # check
    p_check = sub.add_parser("check", help="Check installed agents and LLM connectivity")
    p_check.set_defaults(func=cmd_check)

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Start the HITL review dashboard")
    p_dash.add_argument("--port", type=int, default=None)
    p_dash.add_argument("--no-browser", action="store_true")
    p_dash.set_defaults(func=cmd_dashboard)

    # run
    p_run = sub.add_parser("run", help="Analyse a CDC and run the full agent pipeline")
    p_run.add_argument("--cdc", required=True, help="Path to the CDC file (.md, .txt, .pdf)")
    p_run.add_argument("--no-dashboard", action="store_true", help="Do not start the dashboard")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
