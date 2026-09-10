"""Generate a Dockerfile for cdc-router."""

from __future__ import annotations

from pathlib import Path

from cdc_router.tools import get_manifest

_OUTPUT_PATH = Path("Dockerfile.cdc-router")


def generate_dockerfile(output_path: Path | None = None) -> Path:
    manifest = get_manifest()
    pip_specs = [dep["primary"]["pip"] for dep in manifest]
    content = _render(pip_specs)
    out = output_path or _OUTPUT_PATH
    out.write_text(content, encoding="utf-8")
    return out


def _render(pip_specs: list[str]) -> str:
    lines = ["FROM python:3.10-slim", "", "WORKDIR /app", ""]
    if pip_specs:
        pip_args = " \\\n    ".join(f'"{s}"' for s in pip_specs)
        lines += ["RUN pip install --no-cache-dir \\", f"    {pip_args}", ""]
    lines += ["COPY . .", "", 'CMD ["python", "-m", "cdc_router", "--help"]']
    return "\n".join(lines) + "\n"
