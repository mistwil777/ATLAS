"""Load atlas.toml from the current working directory."""

from __future__ import annotations

import os
import sys
from enum import Enum
from pathlib import Path
from typing import Any


class DeploymentMode(str, Enum):
    SAAS = "saas"
    ONPREMISE = "onpremise"
    AIRGAP = "airgap"


def get_deployment_mode(cfg: dict) -> DeploymentMode:
    """Read deployment_mode from ATLAS_MODE env var or atlas.toml.

    Checks [project].deployment_mode first, then [deployment].deployment_mode as fallback.
    Warns if found under [deployment] — correct section is [project].
    """
    import logging  # noqa: PLC0415

    env_val = os.environ.get("ATLAS_MODE")
    if env_val:
        try:
            return DeploymentMode(env_val.lower())
        except ValueError:
            return DeploymentMode.SAAS

    raw = cfg.get("project", {}).get("deployment_mode")
    if raw is None:
        fallback = cfg.get("deployment", {}).get("deployment_mode")
        if fallback:
            logging.getLogger("atlas_cli").warning(
                "deployment_mode found under [deployment] section — move it to [project] in atlas.toml"
            )
            raw = fallback
    raw = raw or "saas"
    try:
        return DeploymentMode(raw.lower())
    except ValueError:
        return DeploymentMode.SAAS


def _check_provider_allowed(provider: str, mode: DeploymentMode) -> None:
    _EXTERNAL_PROVIDERS = {"bedrock", "openai", "anthropic", "groq", "auto"}
    if mode == DeploymentMode.AIRGAP and provider in _EXTERNAL_PROVIDERS:
        raise RuntimeError(
            f"Provider '{provider}' requires network access — "
            f"not allowed in deployment_mode=airgap. "
            f"Configure a local LLM provider (e.g. provider='local' with Ollama)."
        )


def load(project_dir: Path | None = None) -> dict[str, Any]:
    """Return config dict from atlas.toml, or empty dict if not found."""
    base = project_dir or Path.cwd()
    toml_path = base / "atlas.toml"
    if not toml_path.exists():
        return {}

    if sys.version_info >= (3, 11):
        import tomllib

        return tomllib.loads(toml_path.read_text(encoding="utf-8"))
    else:
        import tomli

        return tomli.loads(toml_path.read_text(encoding="utf-8"))


def llm_fn_from_config(cfg: dict):
    """Return an LLM callable from atlas.toml [llm] section.

    Supported providers: bedrock (default), openai, anthropic, groq, auto, local.
    Raises RuntimeError if an external provider is used in deployment_mode=airgap.
    """
    llm_cfg = cfg.get("llm", {})
    provider = llm_cfg.get("provider", "bedrock")
    mode = get_deployment_mode(cfg)
    _check_provider_allowed(provider, mode)
    # propagate mode to env so downstream components (mlflow, kg) respect it
    os.environ.setdefault("ATLAS_MODE", mode.value)

    if provider == "bedrock":
        return _bedrock_fn(llm_cfg), llm_cfg
    if provider == "openai":
        return _openai_fn(llm_cfg), llm_cfg
    if provider == "anthropic":
        return _anthropic_fn(llm_cfg), llm_cfg
    if provider == "groq":
        return _groq_fn(llm_cfg), llm_cfg
    if provider == "auto":
        return _auto_fn(llm_cfg), llm_cfg
    if provider == "local":
        raise NotImplementedError(
            "Local LLM provider not yet implemented. "
            "Use provider='openai' with base_url pointing to your Ollama or local endpoint."
        )

    raise ValueError(
        f"Unknown LLM provider: {provider!r}. "
        f"Supported: bedrock, openai, anthropic, groq, auto, local"
    )


# ── Provider implementations ───────────────────────────────────────────────────


def _bedrock_fn(llm_cfg: dict):
    def call(prompt: str, cfg: dict, max_tokens: int = 1024) -> str | None:
        try:
            import boto3
            from botocore.config import Config

            model_id = cfg.get("model_id", "us.amazon.nova-pro-v1:0")
            client = boto3.client(
                "bedrock-runtime",
                region_name=cfg.get("region", "us-east-1"),
                config=Config(connect_timeout=10, read_timeout=120, retries={"max_attempts": 2}),
            )
            resp = client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": float(cfg.get("temperature", 0.1)),
                },
            )
            return resp["output"]["message"]["content"][0]["text"].strip()
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger("atlas_cli").warning("Bedrock call failed: %s", exc)
            return None

    return call


def _openai_fn(llm_cfg: dict):
    def call(prompt: str, cfg: dict, max_tokens: int = 1024) -> str | None:
        try:
            import openai

            client = openai.OpenAI(
                api_key=cfg.get("api_key"),
                base_url=cfg.get("base_url") or None,
            )
            resp = client.chat.completions.create(
                model=cfg.get("model", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=float(cfg.get("temperature", 0.1)),
            )
            return resp.choices[0].message.content
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger("atlas_cli").warning("OpenAI call failed: %s", exc)
            return None

    return call


def _auto_fn(llm_cfg: dict):
    """Try Anthropic (auth_token) first, fall back to Groq (GROQ_API_KEY)."""
    anthropic_fn = _anthropic_fn(llm_cfg)
    groq_fn = _groq_fn(llm_cfg)

    def call(prompt: str, cfg: dict, max_tokens: int = 1024) -> str | None:
        import logging

        result = anthropic_fn(prompt, cfg, max_tokens)
        if result is not None:
            return result
        logging.getLogger("atlas_cli").warning("Anthropic failed, trying Groq fallback")
        return groq_fn(prompt, cfg, max_tokens)

    return call


def _groq_fn(llm_cfg: dict):
    def call(prompt: str, cfg: dict, max_tokens: int = 1024) -> str | None:
        try:
            import os

            import openai

            api_key = cfg.get("api_key") or os.environ.get("GROQ_API_KEY")
            client = openai.OpenAI(
                api_key=api_key,
                base_url="https://api.groq.com/openai/v1",
            )
            resp = client.chat.completions.create(
                model=cfg.get("model", "llama-3.3-70b-versatile"),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=float(cfg.get("temperature", 0.1)),
            )
            return resp.choices[0].message.content
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger("atlas_cli").error("Groq call failed: %s", exc)
            return None

    return call


def _anthropic_fn(llm_cfg: dict):
    def call(prompt: str, cfg: dict, max_tokens: int = 1024) -> str | None:
        try:
            import os

            import anthropic

            api_key = cfg.get("api_key")
            auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
            if auth_token and not api_key:
                client = anthropic.Anthropic(auth_token=auth_token)
            else:
                client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=cfg.get("model", "claude-haiku-4-5-20251001"),
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger("atlas_cli").error("Anthropic call failed: %s", exc)
            return None

    return call
