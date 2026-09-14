"""Generate a Dockerfile for agent-tester from its dependency manifest."""

from __future__ import annotations

import re
from pathlib import Path

from contract_validator.tools import get_manifest

_OUTPUT_PATH = Path("Dockerfile.agent-tester")

_PIP_SYSTEM_EXTRAS: dict[str, list[str]] = {}


def generate_dockerfile(output_path: Path | None = None) -> Path:
    manifest = get_manifest()
    active_pips = [dep["primary"]["pip"] for dep in manifest]
    system_pkgs = []
    for spec in active_pips:
        pkg = re.split(r"[>=<!,\[]", spec)[0].strip().lower().replace("-", "_")
        system_pkgs.extend(_PIP_SYSTEM_EXTRAS.get(pkg, []))

    pip_args = " \\\n    ".join(f'"{s}"' for s in active_pips)
    lines = [
        "FROM python:3.10-slim",
        "",
        "WORKDIR /app",
        "",
        "RUN pip install --no-cache-dir \\",
        f"    {pip_args}",
        "",
        "COPY . .",
        "",
        'CMD ["python", "-m", "contract_validator", "--help"]',
    ]

    out = output_path or _OUTPUT_PATH
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
