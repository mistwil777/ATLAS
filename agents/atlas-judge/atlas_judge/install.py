"""Smart installation for atlas-judge."""

from __future__ import annotations

import subprocess
import sys

from atlas_judge.tools import get_manifest


def run_install(verbose: bool = True) -> bool:
    failed = []
    for entry in get_manifest():
        pip_spec = entry["primary"]["pip"]
        if verbose:
            print(f"Installing {pip_spec}...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pip_spec],
            capture_output=not verbose,
        )
        if result.returncode != 0:
            failed.append(pip_spec)
    if failed:
        print(f"[FAILED] Could not install: {', '.join(failed)}")
        return False
    if verbose:
        print("[DONE] All dependencies installed.")
    return True
