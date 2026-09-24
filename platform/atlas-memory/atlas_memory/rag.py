"""RAG store — indexes and searches ATLAS agent experiences via Qdrant + fastembed."""

from __future__ import annotations

import logging
import math
import os
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
_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
_log = logging.getLogger("atlas_memory")

# Resolved once per process — name of the active embedding backend
_BACKEND: str = ""


def _normalise(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


# ── Backend implementations ────────────────────────────────────────────────────


def _embed_bedrock(texts: list[str]) -> list[list[float]]:
    import boto3  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    def _single(idx_text: tuple[int, str]) -> tuple[int, list[float]]:
        idx, text = idx_text
        client = boto3.client("bedrock-runtime", region_name=region)
        resp = client.invoke_model(
            modelId="amazon.titan-embed-text-v2:0",
            body=_json.dumps({"inputText": text[:8000], "dimensions": 1024, "normalize": True}),
            contentType="application/json",
            accept="application/json",
        )
        return idx, _json.loads(resp["body"].read())["embedding"]

    results: list[list[float]] = [None] * len(texts)  # type: ignore[list-item]
    with ThreadPoolExecutor(max_workers=min(20, len(texts))) as pool:
        futures = {pool.submit(_single, (i, t)): i for i, t in enumerate(texts)}
        for fut in as_completed(futures):
            idx, vec = fut.result()
            results[idx] = vec
    return results


def _embed_fastembed(texts: list[str]) -> list[list[float]]:
    # Fail fast if ONNX model file is not present locally — avoids hanging downloads
    caches = [
        os.environ.get("FASTEMBED_CACHE_PATH", ""),
        "/tmp/fastembed_cache",
        str(Path.home() / ".cache" / "fastembed"),
    ]
    cached = any(list(Path(b).rglob("*.onnx")) for b in caches if b and Path(b).exists())
    if not cached:
        raise RuntimeError(f"fastembed ONNX model not cached locally ({_FASTEMBED_MODEL})")

    from fastembed import TextEmbedding  # noqa: PLC0415

    model = TextEmbedding(model_name=_FASTEMBED_MODEL)
    return [list(v) for v in model.embed(texts)]


def _embed_tfidf(texts: list[str]) -> list[list[float]]:
    from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: PLC0415

    _log.warning("Using TF-IDF embeddings — semantic similarity will be approximate")
    vec = TfidfVectorizer(max_features=512, sublinear_tf=True)
    mat = vec.fit_transform(texts).toarray()
    return [_normalise(row.tolist()) for row in mat]


# ── Public embed function with fallback chain ──────────────────────────────────


def _embed(texts: list[str]) -> list[list[float]]:
    """Embed texts using best available backend. Priority: Bedrock → fastembed → TF-IDF."""
    global _BACKEND  # noqa: PLW0603

    if not texts:
        return []

    backends = [
        ("bedrock", _embed_bedrock),
        ("fastembed", _embed_fastembed),
        ("tfidf", _embed_tfidf),
    ]

    start = 0
    if _BACKEND:
        for i, (name, _) in enumerate(backends):
            if name == _BACKEND:
                start = i
                break

    for name, fn in backends[start:]:
        try:
            vecs = fn(texts)
            if _BACKEND != name:
                _log.info(
                    "atlas-memory embedding backend: %s (dim=%d)", name, len(vecs[0]) if vecs else 0
                )
                _BACKEND = name
            return vecs
        except Exception as exc:  # noqa: BLE001
            _log.warning("Embedding backend '%s' unavailable: %s — trying next", name, exc)
            if _BACKEND == name:
                _BACKEND = ""

    _log.error("All embedding backends failed — returning zero vectors")
    return [[0.0] * 512 for _ in texts]


class RAGStore:
    """Vector store for all ATLAS agent experiences.

    entry_type values: "cdc", "scaffold", "judge_eval", "pipeline_trace", "contract_violation"

    Args:
        storage_path: Directory for Qdrant local persistence.
        embed_fn: Optional callable(texts: list[str]) -> list[list[float]].
                  Defaults to the fallback chain (Bedrock → fastembed → TF-IDF).
                  Inject a mock in tests to avoid network calls.
    """

    def __init__(
        self,
        storage_path: Path,
        embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    ) -> None:
        storage_path.mkdir(parents=True, exist_ok=True)
        self._client = QdrantClient(path=str(storage_path))
        self._embed_fn = embed_fn or _embed
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        # Probe actual embedding dimension from the active backend
        try:
            probe = self._embed_fn(["probe"])
            dim = len(probe[0]) if probe else 512
        except Exception:  # noqa: BLE001
            dim = 512

        existing = [c.name for c in self._client.get_collections().collections]
        if _COLLECTION in existing:
            info = self._client.get_collection(_COLLECTION)
            existing_dim = info.config.params.vectors.size
            if existing_dim != dim:
                # Dimension mismatch (backend changed) — recreate the index.
                # The RAG is a cache; the KG is the source of truth.
                _log.warning(
                    "RAG collection dim mismatch (%d → %d) — recreating index",
                    existing_dim,
                    dim,
                )
                self._client.delete_collection(_COLLECTION)
            else:
                return

        self._client.create_collection(
            collection_name=_COLLECTION,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
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
