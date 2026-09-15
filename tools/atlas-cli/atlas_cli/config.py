"""Load atlas.toml from the current working directory."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


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

    Supported providers: bedrock (default), openai, anthropic.
    Falls back to a stub that raises if not configured.
    """
    llm_cfg = cfg.get("llm", {})
    provider = llm_cfg.get("provider", "bedrock")

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

    raise ValueError(
        f"Unknown LLM provider: {provider!r}. Supported: bedrock, openai, anthropic, groq, auto"
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

            client = openai.OpenAI(api_key=cfg.get("api_key"))
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
