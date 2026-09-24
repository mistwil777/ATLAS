"""PDF extraction — text layer first, Tesseract fallback for scanned pages."""

from __future__ import annotations

from pathlib import Path


def extract(path: Path, ocr_lang: str = "fra+eng") -> str:
    """Extract text from a PDF.

    Tries pdfplumber for the text layer. Falls back to Tesseract OCR page by
    page when a page has no selectable text (scanned PDF).
    """
    try:
        import pdfplumber  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError("pdfplumber required: pip install pdfplumber") from exc

    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if _is_empty(text):
                # Scanned page — render to image and OCR
                text = _ocr_page(page, ocr_lang)
            if text:
                pages.append(text.strip())

    return "\n\n".join(pages)


def _is_empty(text: str) -> bool:
    return not text or len(text.strip()) < 10


def _ocr_page(page, lang: str) -> str:
    try:
        import pytesseract  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        img = page.to_image(resolution=200).original
        if not isinstance(img, Image.Image):
            import io  # noqa: PLC0415

            from PIL import Image as _PIL  # noqa: PLC0415

            img = _PIL.open(io.BytesIO(img))
        return pytesseract.image_to_string(img, lang=lang).strip()
    except Exception:  # noqa: BLE001
        return ""
