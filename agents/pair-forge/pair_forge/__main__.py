"""Entry point for `python -m pair_forge` and the `pair-forge` CLI."""

from __future__ import annotations

import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"

    if command == "init":
        from pair_forge.docker import generate_dockerfile  # noqa: PLC0415
        path = generate_dockerfile()
        print(f"Dockerfile generated: {path}")

    elif command == "check":
        from pair_forge.tools import check_dependencies  # noqa: PLC0415
        result = check_dependencies()
        if not any(result.values()):
            print("All dependencies present and up to date.")
            return
        for item in result.get("missing", []):
            print(f"[MISSING]   {item['capability']} — {item['install_hint']}")
        for item in result.get("deprecated", []):
            print(f"[DEPRECATED] {item['capability']} — replaced by: {item['replaced_by']}")
        for item in result.get("untested_versions", []):
            print(f"[UNTESTED]  {item['capability']} — tested up to {item['tested_up_to']}, found {item['installed_version']}")

    elif command == "install":
        from pair_forge.install import run_install  # noqa: PLC0415
        run_install()

    else:
        print("pair-forge commands:")
        print("  init     — generate Dockerfile from dependency manifest")
        print("  check    — check all dependencies status")
        print("  install  — run smart installation with smoke tests")


if __name__ == "__main__":
    main()
