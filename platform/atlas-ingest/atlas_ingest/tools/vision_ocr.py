"""OCR extraction for images using Tesseract."""

from __future__ import annotations

from pathlib import Path


def extract(path: Path, lang: str = "fra+eng") -> str:
    """Extract text from an image file using Tesseract OCR.

    lang: Tesseract language string — default covers French + English.
    """
    import pytesseract  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    image = Image.open(path)
    text = pytesseract.image_to_string(image, lang=lang)
    return text.strip()


def extract_from_bytes(data: bytes, lang: str = "fra+eng") -> str:
    import io  # noqa: PLC0415

    import pytesseract  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    image = Image.open(io.BytesIO(data))
    return pytesseract.image_to_string(image, lang=lang).strip()
