"""Prompt templates for {{cookiecutter.agent_slug}}."""

from __future__ import annotations


MAIN_PROMPT = """\
You are an agent specialized in {{cookiecutter.domain}}.

Input: {input}

Instructions:
- TODO: define what the agent should do with the input
- Output format: {{cookiecutter.output_type}}

Output:"""
