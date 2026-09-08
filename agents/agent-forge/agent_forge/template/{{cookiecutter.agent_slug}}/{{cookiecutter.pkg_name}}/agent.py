"""{{cookiecutter.agent_slug}} — core agent logic.

Input:  {{cookiecutter.input_type}}
Output: {{cookiecutter.output_type}}
Domain: {{cookiecutter.domain}}
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from pathlib import Path

{% if cookiecutter.uses_llm == "true" %}
from {{cookiecutter.pkg_name}}.prompts import MAIN_PROMPT
{% endif %}
from {{cookiecutter.pkg_name}}.tools import check_dependencies


{% if cookiecutter.uses_llm == "true" %}
def _default_llm(prompt: str, llm_config: dict, max_tokens: int = 2048) -> str | None:
    try:
        import boto3  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415

        model_id    = llm_config.get("model_id", "us.amazon.nova-pro-v1:0")
        temperature = float(llm_config.get("temperature", 0.1))
        region      = llm_config.get("region", "us-east-1")
        client = boto3.client(
            "bedrock-runtime", region_name=region,
            config=Config(connect_timeout=10, read_timeout=120, retries={"max_attempts": 2}),
        )
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        return response["output"]["message"]["content"][0]["text"].strip()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("{{cookiecutter.pkg_name}}").warning("LLM call failed: %s", exc)
        return None
{% endif %}


def _default_log(level: str, message: str) -> None:
    getattr(logging.getLogger("{{cookiecutter.pkg_name}}"), level,
            logging.getLogger("{{cookiecutter.pkg_name}}").info)(message)


class {{cookiecutter.pkg_name | title | replace('_', '')}}Agent:
    """{{cookiecutter.description}}

    Args:
        config:  Configuration dict.
        {% if cookiecutter.uses_llm == "true" %}llm_fn:  callable(prompt, llm_config, max_tokens) -> str | None{% endif %}
        log_fn:  callable(level, message) -> None
    """

    def __init__(
        self,
        config: dict | None = None,
        {% if cookiecutter.uses_llm == "true" %}llm_fn=None,{% endif %}
        log_fn=None,
    ) -> None:
        self.config  = config or {}
        {% if cookiecutter.uses_llm == "true" %}self._llm = llm_fn or _default_llm{% endif %}
        self._log    = log_fn or _default_log

    def run(self, input_path: Path) -> Generator[dict, None, None]:
        """Process input and yield log dicts. Final dict contains key 'summary'."""
        import {{cookiecutter.pkg_name}}.collect as collect  # noqa: PLC0415
        t0 = time.monotonic()
        status = "success"

        yield self._info(f"Starting: {input_path.name}")

        dep_status = check_dependencies()
        for item in dep_status.get("missing", []):
            yield self._warn(f"Capability unavailable: {item['capability']} — {item['install_hint']}")

        # TODO: implement agent logic here

        # Emit telemetry — no-op if ATLAS_COLLECT is off
        collect.emit_metrics("{{cookiecutter.agent_slug}}", {
            "duration_s": round(time.monotonic() - t0, 3),
            "status":     status,
            "input":      input_path.name,
        })

        yield {"summary": True, "input": str(input_path)}

    def _info(self, msg: str)  -> dict: return {"level": "info",    "msg": msg}
    def _warn(self, msg: str)  -> dict: return {"level": "warning", "msg": msg}
    def _error(self, msg: str) -> dict: return {"level": "error",   "msg": msg}
