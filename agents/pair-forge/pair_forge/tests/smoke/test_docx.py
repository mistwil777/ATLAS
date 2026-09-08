"""Smoke test — Word document extraction capability."""

from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "reference.docx"


def test_docx_extraction_imports():
    import docx  # noqa: F401


def test_docx_extraction_content():
    from pair_forge.extractors import extract_content

    content, method = extract_content(FIXTURE)

    assert method == "docx", f"Expected method 'docx', got '{method}'"
    assert len(content) > 10, "Extracted content is too short"
    assert "Java" in content or "Python" in content, (
        f"Reference keywords not found: {content[:200]!r}"
    )
