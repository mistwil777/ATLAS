"""Smoke test — RTF extraction capability."""

from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "reference.rtf"


def test_rtf_extraction_imports():
    from striprtf.striprtf import rtf_to_text  # noqa: F401


def test_rtf_extraction_content():
    from pair_forge.extractors import extract_content

    content, method = extract_content(FIXTURE)

    assert method == "rtf", f"Expected method 'rtf', got '{method}'"
    assert len(content) > 10, "Extracted content is too short"
    assert (
        "Java" in content or "Python" in content
    ), f"Reference keywords not found: {content[:200]!r}"
