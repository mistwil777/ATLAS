"""AtlasIngestAgent — multiformat document ingestion for ATLAS.

Accepts one or more file paths or raw bytes, detects format, extracts
structured text, and returns a single consolidated string ready for
injection into a CDC or scaffold context.

Supported formats: pdf, html, htm, txt, md, csv, json, png, jpg, jpeg,
                   tiff, bmp, webp.

Bus contract: run(files, ...) → Iterator[dict]
Final dict contains key "summary": True and "output": extracted_text.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

_log = logging.getLogger("atlas_ingest")

_TEXT_FORMATS = {".txt", ".md", ".csv", ".json"}
_HTML_FORMATS = {".html", ".htm"}
_PDF_FORMATS = {".pdf"}
_IMAGE_FORMATS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}


class AtlasIngestAgent:
    """Multiformat document ingestion — extracts text from any supported file.

    Args:
        ocr_lang: Tesseract language string (default: French + English).
    """

    def __init__(
        self,
        ocr_lang: str = "fra+eng",
        config: dict | None = None,
        llm_fn=None,
        llm_cfg: dict | None = None,
    ) -> None:
        self.ocr_lang = ocr_lang
        self.config = config or {}

    def run(
        self,
        files: list[str | Path] | None = None,
        cdc_bloc: str = "",
        context: str = "",
    ) -> Iterator[dict]:
        """Extract text from a list of file paths.

        Args:
            files: list of file paths (str or Path).
            cdc_bloc: unused — kept for ATLAS Bus compatibility.
            context:  unused — kept for ATLAS Bus compatibility.

        Yields log dicts then a summary dict with key "output": str.
        """
        if not files:
            yield self._warn("No files provided — nothing to ingest.")
            yield {"summary": True, "output": ""}
            return

        parts: list[str] = []
        for raw_path in files:
            path = Path(raw_path)
            yield self._info(f"Ingesting {path.name} ({path.suffix})")
            try:
                text = self._extract(path)
                if text:
                    parts.append(f"### {path.name}\n\n{text}")
                    yield self._info(f"{path.name}: {len(text)} chars extracted")
                else:
                    yield self._warn(f"{path.name}: no text extracted")
            except Exception as exc:  # noqa: BLE001
                yield self._warn(f"{path.name}: extraction failed — {exc}")

        output = "\n\n---\n\n".join(parts)
        yield {"summary": True, "output": output}

    def _extract(self, path: Path) -> str:
        suffix = path.suffix.lower()

        if suffix in _TEXT_FORMATS:
            from atlas_ingest.tools.text import extract  # noqa: PLC0415

            return extract(path)

        if suffix in _HTML_FORMATS:
            from atlas_ingest.tools.html import extract  # noqa: PLC0415

            return extract(path)

        if suffix in _PDF_FORMATS:
            from atlas_ingest.tools.pdf import extract  # noqa: PLC0415

            return extract(path, ocr_lang=self.ocr_lang)

        if suffix in _IMAGE_FORMATS:
            from atlas_ingest.tools.vision_ocr import extract  # noqa: PLC0415

            return extract(path, lang=self.ocr_lang)

        raise ValueError(f"Unsupported format: {suffix}")

    def _info(self, msg: str) -> dict:
        _log.info(msg)
        return {"level": "info", "msg": msg}

    def _warn(self, msg: str) -> dict:
        _log.warning(msg)
        return {"level": "warning", "msg": msg}
