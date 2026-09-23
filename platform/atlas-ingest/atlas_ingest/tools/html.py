"""HTML extraction — strips tags, preserves structure."""

from __future__ import annotations

from pathlib import Path


def extract(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    return extract_string(raw)


def extract_string(html: str) -> str:
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "head", "meta", "link"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except ImportError:
        # Fallback: naive tag stripping without bs4
        import re  # noqa: PLC0415

        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r" {2,}", " ", text).strip()
