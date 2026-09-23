"""Smoke tests for atlas-ingest."""

from __future__ import annotations

import tempfile
from pathlib import Path

from atlas_ingest.agent import AtlasIngestAgent


def test_import():
    assert AtlasIngestAgent is not None


def test_txt_extraction():
    agent = AtlasIngestAgent()
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False, encoding="utf-8") as f:
        f.write("Ticket #001\nL'utilisateur ne peut pas se connecter.")
        path = Path(f.name)
    events = list(agent.run(files=[path]))
    summary = next(e for e in events if e.get("summary"))
    assert "Ticket #001" in summary["output"]
    path.unlink(missing_ok=True)


def test_md_extraction():
    agent = AtlasIngestAgent()
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False, encoding="utf-8") as f:
        f.write("# CDC\n\n## Objectif\nClassifier les tickets support.")
        path = Path(f.name)
    events = list(agent.run(files=[path]))
    summary = next(e for e in events if e.get("summary"))
    assert "Classifier" in summary["output"]
    path.unlink(missing_ok=True)


def test_html_extraction():
    agent = AtlasIngestAgent()
    with tempfile.NamedTemporaryFile(suffix=".html", mode="w", delete=False, encoding="utf-8") as f:
        f.write("<html><body><h1>Titre</h1><p>Contenu important</p></body></html>")
        path = Path(f.name)
    events = list(agent.run(files=[path]))
    summary = next(e for e in events if e.get("summary"))
    assert "Contenu important" in summary["output"]
    path.unlink(missing_ok=True)


def test_empty_files_list():
    agent = AtlasIngestAgent()
    events = list(agent.run(files=[]))
    summary = next(e for e in events if e.get("summary"))
    assert summary["output"] == ""


def test_unsupported_format():
    agent = AtlasIngestAgent()
    with tempfile.NamedTemporaryFile(suffix=".xyz", mode="w", delete=False) as f:
        path = Path(f.name)
    events = list(agent.run(files=[path]))
    warnings = [e for e in events if e.get("level") == "warning"]
    assert any("extraction failed" in w["msg"] for w in warnings)
    path.unlink(missing_ok=True)
