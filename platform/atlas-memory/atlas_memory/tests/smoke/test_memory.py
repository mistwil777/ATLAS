"""Smoke tests for atlas-memory — 100% offline (no AWS, no network)."""

from __future__ import annotations

import random
import tempfile
from pathlib import Path


def _mock_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic fake embeddings — no network, no fastembed download."""
    rng = random.Random(42)
    return [[rng.uniform(-1, 1) for _ in range(384)] for _ in texts]


# ── KG tests ─────────────────────────────────────────────────────────────────


def test_kg_register_and_query():
    from atlas_memory.kg import KGStore

    with tempfile.TemporaryDirectory() as tmp:
        kg = KGStore(Path(tmp) / "graph.json")
        kg.register_agent(
            "pair-forge",
            capabilities=["document extraction"],
            inputs=["document_path"],
            outputs=["extracted_text"],
            tags=["nlp", "document"],
        )
        kg.register_agent(
            "contract-validator",
            capabilities=["contract validation"],
            inputs=["agent_path"],
            outputs=["report"],
            tags=["validation"],
        )
        kg.add_edge("pair-forge", "contract-validator", "CHAINS_WITH")

        composable = kg.query_composable("pair-forge")
        assert len(composable) == 1
        assert composable[0]["name"] == "contract-validator"
        assert composable[0]["relation"] == "CHAINS_WITH"


def test_kg_edge_and_graph():
    from atlas_memory.kg import KGStore

    with tempfile.TemporaryDirectory() as tmp:
        kg = KGStore(Path(tmp) / "graph.json")
        kg.register_agent(
            "agent-forge",
            capabilities=["scaffold"],
            inputs=["spec"],
            outputs=["agent_path"],
            tags=[],
        )
        kg.register_agent(
            "contract-validator",
            capabilities=["validate"],
            inputs=["agent_path"],
            outputs=["report"],
            tags=[],
        )
        kg.add_edge("agent-forge", "contract-validator", "PRODUCES")

        graph = kg.get_graph()
        assert "nodes" in graph
        assert "links" in graph
        names = [n["id"] for n in graph["nodes"]]
        assert "agent-forge" in names
        assert "contract-validator" in names


def test_kg_persistence():
    from atlas_memory.kg import KGStore

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "graph.json"
        kg = KGStore(path)
        kg.register_agent("pair-forge", capabilities=[], inputs=[], outputs=[], tags=["test"])

        kg2 = KGStore(path)
        graph = kg2.get_graph()
        names = [n["id"] for n in graph["nodes"]]
        assert "pair-forge" in names


def test_kg_unknown_agent_returns_empty():
    from atlas_memory.kg import KGStore

    with tempfile.TemporaryDirectory() as tmp:
        kg = KGStore(Path(tmp) / "graph.json")
        result = kg.query_composable("nonexistent")
        assert result == []


# ── RAG tests ─────────────────────────────────────────────────────────────────


def test_rag_index_cdc():
    from atlas_memory.rag import RAGStore

    with tempfile.TemporaryDirectory() as tmp:
        rag = RAGStore(Path(tmp) / "vectors", embed_fn=_mock_embed)
        point_id = rag.index(
            entry_type="cdc",
            content="Extract customer names from incoming emails and store them in CRM.",
            payload={"segmentation": [{"bloc": "extraction", "type": "ia"}]},
        )
        assert isinstance(point_id, str)
        assert len(point_id) > 0


def test_rag_index_scaffold():
    from atlas_memory.rag import RAGStore

    with tempfile.TemporaryDirectory() as tmp:
        rag = RAGStore(Path(tmp) / "vectors", embed_fn=_mock_embed)
        point_id = rag.index(
            entry_type="scaffold",
            content="agent-forge generated scaffold for email-extractor approved by dev.",
            metadata={"agent_name": "email-extractor", "approved": True},
        )
        assert isinstance(point_id, str)


def test_rag_search_all_types():
    from atlas_memory.rag import RAGStore

    with tempfile.TemporaryDirectory() as tmp:
        rag = RAGStore(Path(tmp) / "vectors", embed_fn=_mock_embed)
        rag.index("cdc", "Extract data from PDF invoices.")
        rag.index("scaffold", "Scaffold for invoice parser agent.")

        results = rag.search("extract data from documents", top_k=5)
        assert isinstance(results, list)
        assert len(results) >= 1


def test_rag_search_filtered_by_type():
    from atlas_memory.rag import RAGStore

    with tempfile.TemporaryDirectory() as tmp:
        rag = RAGStore(Path(tmp) / "vectors", embed_fn=_mock_embed)
        rag.index("cdc", "Classify support tickets by urgency.")
        rag.index("judge_eval", "Judge eval: classification respects binary rules, score 4/5.")

        results_cdc = rag.search("ticket classification", entry_type="cdc", top_k=5)
        assert all(r["entry_type"] == "cdc" for r in results_cdc)

        results_eval = rag.search("ticket classification", entry_type="judge_eval", top_k=5)
        assert all(r["entry_type"] == "judge_eval" for r in results_eval)


# ── Agent run() tests ─────────────────────────────────────────────────────────


def test_check_dependencies():
    from atlas_memory.tools import check_dependencies

    result = check_dependencies()
    assert result["missing"] == []
    assert result["deprecated"] == []
    assert result["untested_versions"] == []


def test_run_register_agent():
    from atlas_memory.agent import AtlasMemoryAgent

    with tempfile.TemporaryDirectory() as tmp:
        agent = AtlasMemoryAgent(storage_path=Path(tmp), embed_fn=_mock_embed)
        events = list(
            agent.run(
                operation="register_agent",
                name="pair-forge",
                capabilities=["document extraction"],
                inputs=["document_path"],
                outputs=["extracted_text"],
                tags=["nlp"],
            )
        )
        summary = next(e for e in events if e.get("summary"))
        assert summary["result"]["registered"] == "pair-forge"


def test_run_get_graph():
    from atlas_memory.agent import AtlasMemoryAgent

    with tempfile.TemporaryDirectory() as tmp:
        agent = AtlasMemoryAgent(storage_path=Path(tmp), embed_fn=_mock_embed)
        events = list(agent.run(operation="get_graph"))
        summary = next(e for e in events if e.get("summary"))
        assert "nodes" in summary["result"]


def test_run_index():
    from atlas_memory.agent import AtlasMemoryAgent

    with tempfile.TemporaryDirectory() as tmp:
        agent = AtlasMemoryAgent(storage_path=Path(tmp), embed_fn=_mock_embed)
        events = list(
            agent.run(
                operation="index",
                entry_type="cdc",
                content="Process PDF invoices and extract line items.",
            )
        )
        summary = next(e for e in events if e.get("summary"))
        assert summary["result"]["entry_type"] == "cdc"
        assert "id" in summary["result"]


def test_run_search():
    from atlas_memory.agent import AtlasMemoryAgent

    with tempfile.TemporaryDirectory() as tmp:
        agent = AtlasMemoryAgent(storage_path=Path(tmp), embed_fn=_mock_embed)
        list(agent.run(operation="index", entry_type="cdc", content="Extract text from PDF files."))

        events = list(agent.run(operation="search", query="extract text from documents"))
        summary = next(e for e in events if e.get("summary"))
        assert "results" in summary["result"]
        assert isinstance(summary["result"]["results"], list)


def test_run_unknown_operation():
    from atlas_memory.agent import AtlasMemoryAgent

    with tempfile.TemporaryDirectory() as tmp:
        agent = AtlasMemoryAgent(storage_path=Path(tmp), embed_fn=_mock_embed)
        events = list(agent.run(operation="nonexistent"))
        assert any(e.get("level") == "error" for e in events)
        assert not any(e.get("summary") for e in events)


# ── Confidence & invalidation tests ──────────────────────────────────────────


def test_rag_index_with_confidence():
    from atlas_memory.rag import RAGStore

    with tempfile.TemporaryDirectory() as tmp:
        rag = RAGStore(Path(tmp) / "vectors", embed_fn=_mock_embed)
        point_id = rag.index("scaffold", "Good scaffold.", confidence=0.9)
        results = rag.search("Good scaffold", top_k=5)
        hit = next((r for r in results if r["id"] == point_id), None)
        assert hit is not None
        assert hit["confidence"] == 0.9


def test_rag_update_confidence():
    from atlas_memory.rag import RAGStore

    with tempfile.TemporaryDirectory() as tmp:
        rag = RAGStore(Path(tmp) / "vectors", embed_fn=_mock_embed)
        point_id = rag.index("scaffold", "Mediocre scaffold.", confidence=0.8)
        rag.update_confidence(point_id, 0.3)
        results = rag.search("Mediocre scaffold", top_k=5)
        hit = next((r for r in results if r["id"] == point_id), None)
        assert hit is not None
        assert hit["confidence"] == 0.3


def test_rag_invalidate():
    from atlas_memory.rag import RAGStore

    with tempfile.TemporaryDirectory() as tmp:
        rag = RAGStore(Path(tmp) / "vectors", embed_fn=_mock_embed)
        pid_bad = rag.index("scaffold", "Bad scaffold for tickets.", confidence=0.9)
        pid_good = rag.index("scaffold", "Good scaffold for tickets.", confidence=1.0)
        rag.invalidate(pid_bad, invalidated_by="user_delete")
        results = rag.search("scaffold for tickets", top_k=5)
        scores = {r["id"]: r["cosine_score"] * r.get("confidence", 1.0) for r in results}
        # good entry should score higher than invalidated
        assert scores.get(pid_good, 0) > scores.get(pid_bad, 0)


def test_rag_combined_score_ranking():
    """High-confidence mediocre cosine should lose to low-confidence high cosine only if combined wins."""
    from atlas_memory.rag import RAGStore

    with tempfile.TemporaryDirectory() as tmp:
        rag = RAGStore(Path(tmp) / "vectors", embed_fn=_mock_embed)
        pid_excellent = rag.index(
            "scaffold", "classify support tickets priority category", confidence=1.0
        )
        pid_bad = rag.index(
            "scaffold", "classify support tickets priority category bad version", confidence=0.0
        )
        results = rag.search("classify support tickets", top_k=5)
        ids = [r["id"] for r in results]
        assert ids.index(pid_excellent) < ids.index(pid_bad)


def test_kg_update_agent_score():
    from atlas_memory.kg import KGStore

    with tempfile.TemporaryDirectory() as tmp:
        kg = KGStore(Path(tmp) / "graph.json")
        kg.register_agent("TestAgent", capabilities=[], inputs=[], outputs=[], tags=[])
        kg.update_agent_score("TestAgent", judge_score=5.0, run_id="run-abc")
        graph = kg.get_graph()
        node = next(n for n in graph["nodes"] if n["id"] == "TestAgent")
        assert node["judge_score"] == 5.0
        assert node["confidence"] == 1.0
        assert len(node["score_history"]) == 1
        assert node["score_history"][0]["score"] == 5.0


def test_kg_score_history_accumulates():
    from atlas_memory.kg import KGStore

    with tempfile.TemporaryDirectory() as tmp:
        kg = KGStore(Path(tmp) / "graph.json")
        kg.register_agent("TestAgent", capabilities=[], inputs=[], outputs=[], tags=[])
        kg.update_agent_score("TestAgent", 1.0, "run-1")
        kg.update_agent_score("TestAgent", 3.5, "run-2")
        kg.update_agent_score("TestAgent", 5.0, "run-3")
        graph = kg.get_graph()
        node = next(n for n in graph["nodes"] if n["id"] == "TestAgent")
        assert len(node["score_history"]) == 3
        assert node["judge_score"] == 5.0
        assert node["confidence"] == 1.0


def test_run_update_agent_score():
    from atlas_memory.agent import AtlasMemoryAgent

    with tempfile.TemporaryDirectory() as tmp:
        agent = AtlasMemoryAgent(storage_path=Path(tmp), embed_fn=_mock_embed)
        list(
            agent.run(
                operation="register_agent",
                name="TestAgent",
                capabilities=[],
                inputs=[],
                outputs=[],
                tags=[],
            )
        )
        events = list(
            agent.run(
                operation="update_agent_score", name="TestAgent", judge_score=4.0, run_id="run-x"
            )
        )
        summary = next(e for e in events if e.get("summary"))
        assert summary["result"]["judge_score"] == 4.0


def test_run_invalidate():
    from atlas_memory.agent import AtlasMemoryAgent

    with tempfile.TemporaryDirectory() as tmp:
        agent = AtlasMemoryAgent(storage_path=Path(tmp), embed_fn=_mock_embed)
        idx = list(
            agent.run(
                operation="index",
                entry_type="scaffold",
                content="test invalidation",
                confidence=0.8,
            )
        )
        point_id = next(e for e in idx if e.get("summary"))["result"]["id"]
        events = list(agent.run(operation="invalidate", point_id=point_id, invalidated_by="test"))
        summary = next(e for e in events if e.get("summary"))
        assert summary["result"]["invalidated_by"] == "test"
