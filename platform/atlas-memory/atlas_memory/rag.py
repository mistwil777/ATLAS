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


def _fastembed(texts: list[str]) -> list[list[float]]:
    from fastembed import TextEmbedding  # noqa: PLC0415

    model = TextEmbedding(model_name=_MODEL)
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
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
            ],
        )
        return point_id

    def search(
        self,
        query: str,
        entry_type: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        vectors = self._embed([query])
        query_filter = None
        if entry_type is not None:
            query_filter = Filter(
                must=[FieldCondition(key="entry_type", match=MatchValue(value=entry_type))]
            )
        results = self._client.query_points(
            collection_name=_COLLECTION,
            query=vectors[0],
            query_filter=query_filter,
            limit=top_k,
        )
        return [{"id": h.id, "score": h.score, **h.payload} for h in results.points]
