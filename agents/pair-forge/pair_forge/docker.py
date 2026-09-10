"""Generate a Dockerfile from the pair-forge dependency manifest.

Pure text generation — no LLM required. Reads the manifest, resolves
the active tool per capability (primary unless a replacement was recorded
in pyproject.toml), and emits a ready-to-use Dockerfile.

Usage:
    python -m pair_forge init
    from pair_forge.docker import generate_dockerfile
    path = generate_dockerfile()
"""

from __future__ import annotations

import re
from pathlib import Path

from pair_forge.tools import get_manifest

_OUTPUT_PATH = Path("Dockerfile.pair-forge")

# System packages needed by pair-forge tools (brew name → apt name)
_SYSTEM_APT: dict[str, str] = {
    "tesseract": "tesseract-ocr",
    "pdftoppm": "poppler-utils",
}

# Map pip package names to known system-level extras required at runtime
_PIP_SYSTEM_EXTRAS: dict[str, list[str]] = {
    "pytesseract": ["tesseract-ocr"],
    "pdf2image": ["poppler-utils"],
}


def generate_dockerfile(output_path: Path | None = None) -> Path:
    """Generate a Dockerfile from the current manifest and write it to disk."""
    manifest = get_manifest()
    active_pips = _collect_active_pip_specs(manifest)
    system_pkgs = _collect_system_packages(active_pips)

    content = _render(active_pips, system_pkgs)
    out = output_path or _OUTPUT_PATH
    out.write_text(content, encoding="utf-8")
    return out


# ── Internal helpers ───────────────────────────────────────────────────────────


def _collect_active_pip_specs(manifest: list[dict]) -> list[str]:
    """Return pip specs for the active tool of each capability (no duplicates)."""
    seen: set[str] = set()
    specs: list[str] = []

    for dep in manifest:
        spec = dep["primary"]["pip"]
        pkg = _pkg_name(spec)
        if pkg not in seen:
            seen.add(pkg)
            specs.append(spec)

    return specs


def _collect_system_packages(pip_specs: list[str]) -> list[str]:
    """Return apt package names required by the given pip specs."""
    apt: list[str] = []
    for spec in pip_specs:
        pkg = _pkg_name(spec)
        for apt_pkg in _PIP_SYSTEM_EXTRAS.get(pkg, []):
            if apt_pkg not in apt:
                apt.append(apt_pkg)
    return apt


def _pkg_name(spec: str) -> str:
    """Extract bare package name from a pip spec (e.g. 'pdfplumber>=0.11,<2.0' → 'pdfplumber')."""
    return re.split(r"[>=<!,\[]", spec)[0].strip().lower().replace("-", "_")


def _render(pip_specs: list[str], system_pkgs: list[str]) -> str:
    lines = [
        "FROM python:3.10-slim",
        "",
        "WORKDIR /app",
        "",
    ]

    if system_pkgs:
        apt_line = " ".join(system_pkgs)
        lines += [
            "RUN apt-get update && apt-get install -y --no-install-recommends \\",
            f"    {apt_line} \\",
            "    && rm -rf /var/lib/apt/lists/*",
            "",
        ]

    pip_args = " \\\n    ".join(f'"{s}"' for s in pip_specs)
    lines += [
        "RUN pip install --no-cache-dir \\",
        f"    {pip_args}",
        "",
        "COPY . .",
        "",
        'CMD ["python", "-m", "pair_forge", "--help"]',
    ]

    return "\n".join(lines) + "\n"
