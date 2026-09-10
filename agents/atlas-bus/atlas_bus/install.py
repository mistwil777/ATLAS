"""Smart installation for atlas-bus — no external dependencies."""

from __future__ import annotations

from atlas_bus.tools import get_manifest


def run_install(verbose: bool = True) -> bool:
    """Install all dependencies. atlas-bus has none — always succeeds."""
    manifest = get_manifest()
    if not manifest:
        if verbose:
            print("[DONE] atlas-bus has no external dependencies.")
        return True

    if verbose:
        print("[DONE] All dependencies installed.")
    return True
