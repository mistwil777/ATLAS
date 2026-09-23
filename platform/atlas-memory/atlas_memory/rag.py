"""RAG store — indexes and searches ATLAS agent experiences via Qdrant + fastembed."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

_COLLECTION = "atlas_memory"
_DIM = 384  # BAAI/bge-small-en-v1.5
_MODEL = "BAAI/bge-small-en-v1.5"


def _check_onnx_cached() -> bool:
    """Return True if the fastembed ONNX model is already present in one of the local caches."""
    import os as _os  # noqa: PLC0415

    # fastembed caches in $FASTEMBED_CACHE_PATH, /tmp/fastembed_cache, or ~/.cache/fastembed
    candidates = [
        _os.environ.get("FASTEMBED_CACHE_PATH", ""),
        "/tmp/fastembed_cache",
        str(Path.home() / ".cache" / "fastembed"),
    ]
    model_slug = _MODEL.replace("/", "--").replace("_", "-").lower()
    for base in candidates:
        if not base:
            continue
        for p in Path(base).rglob("*.onnx"):
            if model_slug in str(p).lower() or "bge-small" in str(p).lower():
                return True
    return False


def _fastembed(texts: list[str]) -> list[list[float]]:
    import logging  # noqa: PLC0415

    from fastembed import TextEmbedding  # noqa: PLC0415

    # Fail fast if the ONNX model file is not cached locally.
    # Avoids triggering a network download that hangs in airgap environments.
    if not _check_onnx_cached():
        logging.getLogger("atlas_memory").warning(
            "fastembed ONNX model not cached locally — RAG indexing skipped. "
            "Run: python -m fastembed download --model %s",
            _MODEL,
        )
        raise RuntimeError(f"fastembed model {_MODEL} not cached locally")

    try:
        model = TextEmbedding(model_name=_MODEL)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("atlas_memory").warning(
            "fastembed model unavailable (%s) — RAG search disabled.",
            exc.__class__.__name__,
        )
        raise
    return [list(v) for v in model.embed(texts)]


class RAGStore:
    """Vector store for all ATLAS agent experiences.

    entry_type values: "cdc", "scaffold", "judge_eval", "pipeline_trace", "contract_violation"

    Args:
        storage_path: Directory for Qdrant local persistence.
        embed_fn: Optional callable(texts: list[str]) -> list[list[float]].
                  Defaults to fastembed BAAI/bge-small-en-v1.5.
                  Inject a mock in tests to avoid network calls.
    """

    def __init__(
        self,
        storage_path: Path,
        embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    ) -> None:
        storage_path.mkdir(parents=True, exist_ok=True)
        self._client = QdrantClient(path=str(storage_path))
        self._embed_fn = embed_fn or _fastembed
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        existing = [c.name for c in self._client.get_collections().collections]
        if _COLLECTION not in existing:
            self._client.create_collection(
                collection_name=_COLLECTION,
                vectors_config=VectorParams(size=_DIM, distance=Distance.COSINE),
            )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return self._embed_fn(texts)

    def index(
        self,
        entry_type: str,
        content: str,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        confidence: float = 1.0,
    ) -> str:
        point_id = str(uuid.uuid4())
        vectors = self._embed([content])
        self._client.upsert(
            collection_name=_COLLECTION,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vectors[0],
                    payload={
                        "entry_type": entry_type,
                        "content": content,
                        "payload": payload or {},
                        "metadata": metadata or {},
                        "confidence": max(0.0, min(1.0, confidence)),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
            ],
        )
        return point_id

    def update_confidence(self, point_id: str, confidence: float) -> None:
        """Update the confidence score of an existing point (0.0–1.0)."""
        self._client.set_payload(
            collection_name=_COLLECTION,
            payload={"confidence": max(0.0, min(1.0, confidence))},
            points=[point_id],
        )

    def invalidate(self, point_id: str, invalidated_by: str) -> None:
        """Mark a point as superseded — confidence → 0.0, kept for audit."""
        self._client.set_payload(
            collection_name=_COLLECTION,
            payload={
                "confidence": 0.0,
                "invalidated_by": invalidated_by,
                "invalidated_at": datetime.now(timezone.utc).isoformat(),
            },
            points=[point_id],
        )

    def invalidate_by_run(self, run_id: str, invalidated_by: str) -> int:
        """Invalidate all points whose payload.run_id matches. Returns count."""
        invalidated = 0
        offset = None
        while True:
            result = self._client.scroll(
                collection_name=_COLLECTION,
                scroll_filter=Filter(
                    must=[FieldCondition(key="payload.run_id", match=MatchValue(value=run_id))]
                ),
                limit=100,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            points, next_offset = result
            for p in points:
                self.invalidate(str(p.id), invalidated_by)
                invalidated += 1
            if next_offset is None:
                break
            offset = next_offset
        return invalidated

    def search(
        self,
        query: str,
        entry_type: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        vectors = self._embed([query])
        must = []
        if entry_type is not None:
            must.append(FieldCondition(key="entry_type", match=MatchValue(value=entry_type)))
        query_filter = Filter(must=must) if must else None
        # Fetch more candidates to allow re-ranking by combined score
        fetch_k = min(top_k * 4, 100)
        results = self._client.query_points(
            collection_name=_COLLECTION,
            query=vectors[0],
            query_filter=query_filter,
            limit=fetch_k,
        )
        # Re-rank: combined_score = cosine_similarity × confidence
        ranked = sorted(
            [{"id": h.id, "cosine_score": h.score, **h.payload} for h in results.points],
            key=lambda r: r["cosine_score"] * r.get("confidence", 1.0),
            reverse=True,
        )
        return ranked[:top_k]
