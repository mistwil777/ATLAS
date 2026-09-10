"""Generate a Dockerfile for atlas-judge."""

from __future__ import annotations

from pathlib import Path

from atlas_judge.tools import get_manifest

_OUTPUT_PATH = Path("Dockerfile.atlas-judge")


def generate_dockerfile(output_path: Path | None = None) -> Path:
    pip_specs = [dep["primary"]["pip"] for dep in get_manifest()]
    out = output_path or _OUTPUT_PATH
    out.write_text(_render(pip_specs), encoding="utf-8")
    return out


def _render(pip_specs: list[str]) -> str:
    lines = ["FROM python:3.10-slim", "", "WORKDIR /app", ""]
    if pip_specs:
        pip_args = " \\\n    ".join(f'"{s}"' for s in pip_specs)
        lines += ["RUN pip install --no-cache-dir \\", f"    {pip_args}", ""]
    lines += ["COPY . .", "", 'CMD ["python", "-m", "atlas_judge", "--help"]']
    return "\n".join(lines) + "\n"
