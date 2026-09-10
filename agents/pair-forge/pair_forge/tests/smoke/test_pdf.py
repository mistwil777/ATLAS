"""Smoke test — PDF text extraction capability.

Validates that the active PDF extraction tool (primary or replacement)
produces readable text content from a known reference file.
The same assertions are checked regardless of which tool is installed.
"""

from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "reference.pdf"


def test_pdf_extraction_imports():
    import pdfplumber  # noqa: F401


def test_pdf_extraction_content():
    from pair_forge.extractors import extract_content

    content, method = extract_content(FIXTURE)

    assert method == "pdf", f"Expected method 'pdf', got '{method}'"
    assert len(content) > 20, "Extracted content is too short"
    assert "Java" in content or "Python" in content, (
        f"Reference keywords not found in extracted content: {content[:200]!r}"
    )
