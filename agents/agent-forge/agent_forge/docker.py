"""Generate a Dockerfile from the agent-forge dependency manifest."""

from __future__ import annotations

import re
from pathlib import Path

from agent_forge.tools import get_manifest

_OUTPUT_PATH = Path("Dockerfile.agent-forge")

_PIP_SYSTEM_EXTRAS: dict[str, list[str]] = {}


def generate_dockerfile(output_path: Path | None = None) -> Path:
    manifest    = get_manifest()
    active_pips = _collect_active_pip_specs(manifest)
    system_pkgs = _collect_system_packages(active_pips)
    content     = _render(active_pips, system_pkgs)
    out         = output_path or _OUTPUT_PATH
    out.write_text(content, encoding="utf-8")
    return out


def _collect_active_pip_specs(manifest: list[dict]) -> list[str]:
    seen:  set[str] = set()
    specs: list[str] = []
    for dep in manifest:
        spec = dep["primary"]["pip"]
        pkg  = _pkg_name(spec)
        if pkg not in seen:
            seen.add(pkg)
            specs.append(spec)
    return specs


def _collect_system_packages(pip_specs: list[str]) -> list[str]:
    apt: list[str] = []
    for spec in pip_specs:
        pkg = _pkg_name(spec)
        for apt_pkg in _PIP_SYSTEM_EXTRAS.get(pkg, []):
            if apt_pkg not in apt:
                apt.append(apt_pkg)
    return apt


def _pkg_name(spec: str) -> str:
    return re.split(r"[>=<!,\[]", spec)[0].strip().lower().replace("-", "_")


def _render(pip_specs: list[str], system_pkgs: list[str]) -> str:
    lines = ["FROM python:3.10-slim", "", "WORKDIR /app", ""]

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
        'CMD ["python", "-m", "agent_forge", "--help"]',
    ]
    return "\n".join(lines) + "\n"
