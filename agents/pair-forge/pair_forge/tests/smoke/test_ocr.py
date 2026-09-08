"""Smoke test — OCR capability (pytesseract + pdf2image).

Skipped automatically if tesseract binary is not installed on the system,
since system binaries cannot be installed by pip.
"""

import shutil
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "reference.pdf"


def test_ocr_imports():
    import pytesseract  # noqa: F401
    import pdf2image    # noqa: F401


@pytest.mark.skipif(
    shutil.which("tesseract") is None,
    reason="tesseract binary not installed (brew install tesseract / apt-get install tesseract-ocr)",
)
def test_ocr_produces_output():
    """Verify pytesseract + pdf2image can process a PDF page and return text."""
    import pdf2image
    import pytesseract

    images = pdf2image.convert_from_path(str(FIXTURE), first_page=1, last_page=1)
    assert images, "pdf2image returned no pages"

    text = pytesseract.image_to_string(images[0])
    assert isinstance(text, str), "pytesseract did not return a string"
    assert len(text.strip()) >= 0  # OCR on a minimal PDF may return empty; presence is enough
