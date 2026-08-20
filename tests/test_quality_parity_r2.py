from __future__ import annotations

from pathlib import Path

from mapi_core.memory.context_engine import build_agent_context_payload
from mapi_core.memory.hybrid_retrieval import fuse_hybrid_results
from mapi_core.memory.steward import capture_phase_payload


def _memory(memory_id: int, content: str, project: str = "project-a", created_at: str = "2026-08-18T10:00:00Z") -> dict:
    return {
        "id": memory_id,
        "content": content,
        "summary_short": content,
        "memory_type": "project_note",
        "project_key": project,
        "created_at": created_at,
    }


def test_hybrid_retrieval_is_deterministic_and_project_bound() -> None:
    candidates = {
        1: _memory(1, "alpha", created_at="2026-08-18T10:00:00Z"),
        2: _memory(2, "beta", created_at="2026-08-18T11:00:00Z"),
    }
    lexical = {
        "items": [
            {"id": 1, "match_debug": {"matched_by": ["text"]}},
            {"id": 2, "match_debug": {"matched_by": ["token"]}},
        ]
    }
    semantic = {
        "results": [
            {"memory_id": 2, "similarity": 0.93, "hybrid_score": 0.93, "lexical_coverage": 0.5},
            {"memory_id": 1, "similarity": 0.80, "hybrid_score": 0.80, "lexical_coverage": 0.4},
        ]
    }
    kwargs = dict(
        query="alpha beta",
        requested_project_key="project-a",
        canonical_project_key="project-a",
        lexical_payload=lexical,
        semantic_payload=semantic,
        candidate_items=candidates,
        gravity_block={"status": "disabled", "reason": "test", "items": []},
        limit=10,
    )
    first = fuse_hybrid_results(**kwargs)
    second = fuse_hybrid_results(**kwargs)

    assert first["status"] == "ok"
    assert first["schema"] == "mapi_hybrid_retrieval.v1"
    assert first["retrieval_fingerprint"] == second["retrieval_fingerprint"]
    assert [item["id"] for item in first["items"]] == [2, 1]
    assert all(item["project_key"] == "project-a" for item in first["items"])
    assert first["safety"]["durable_importance_modified"] is False


def test_context_engine_is_source_bound_budgeted_and_neutral() -> None:
    restore = {
        "status": "ok",
        "core_memories": [_memory(10, "Stable project identity")],
        "project_anchors": [_memory(11, "Current architecture checkpoint")],
        "recent_context": [_memory(12, "Recent implementation delta")],
    }
    retrieval = {
        "status": "ok",
        "count": 2,
        "items": [
            {**_memory(13, "Relevant source-bound memory"), "match_debug": {"matched_by": ["text", "semantic"]}},
            {**_memory(99, "Foreign project memory", project="project-b"), "match_debug": {"matched_by": ["semantic"]}},
        ],
    }
    result = build_agent_context_payload(
        intent="prepare the next implementation step",
        requested_project_key="project-a",
        canonical_project_key="project-a",
        token_budget=400,
        restore_payload=restore,
        commitment_ledger={"status": "unavailable", "commitments": []},
        retrieval_payload=retrieval,
        gravity_block={"status": "disabled", "reason": "test", "items": []},
    )

    assert result["status"] == "ok"
    assert result["schema"] == "mapi_context_engine.v1"
    assert result["project_key"] == "project-a"
    assert result["budget"]["used_token_upper_bound"] <= 400
    assert result["invariants"]["all_items_source_bound"] is True
    assert result["invariants"]["writes_performed"] is False
    assert result["invariants"]["model_calls"] is False
    assert 99 not in result["source_memory_ids"]
    assert "Foreign project memory" not in result["context_text"]


def test_memory_steward_blocks_capture_without_evidence() -> None:
    result = capture_phase_payload(
        phase="after_action",
        capture_proposal={"status": "proposed", "proposal": {"input_fingerprint": "abc"}},
        requested_project_key="project-a",
        canonical_project_key="project-a",
        content="Action: x\nOutcome: y\nDurable delta: z",
        source_context=None,
        conversation_key=None,
        source_event_ref=None,
        source_memory_ids=[],
        hint="memory_steward_after_action",
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "source_evidence_required"
    assert result["safety"]["memory_mutations_performed"] == 0


def test_memory_steward_returns_explicit_review_route_with_evidence() -> None:
    result = capture_phase_payload(
        phase="after_action",
        capture_proposal={"status": "proposed", "proposal": {"input_fingerprint": "abc"}},
        requested_project_key="project-a",
        canonical_project_key="project-a",
        content="Action: x\nOutcome: y\nDurable delta: z",
        source_context="test",
        conversation_key="conv-1",
        source_event_ref="event-1",
        source_memory_ids=[1, 1, 2],
        hint="memory_steward_after_action",
    )

    assert result["status"] == "proposal_ready"
    assert result["source_memory_ids"] == [1, 2]
    assert result["review_route"]["area"] == "memory"
    assert result["review_route"]["action"] == "capture_save"
    assert result["safety"]["auto_apply"] is False
    assert result["safety"]["review_required_before_memory_creation"] is True


def test_new_public_quality_modules_do_not_embed_private_identity() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "mapi_core" / "bootstrap" / "agent_core.py",
        root / "mapi_core" / "memory" / "context_engine.py",
        root / "mapi_core" / "memory" / "hybrid_retrieval.py",
        root / "mapi_core" / "memory" / "steward.py",
    ]
    forbidden = ("jagoda", "micha", "morenatech")
    for path in paths:
        text = path.read_text(encoding="utf-8").casefold()
        assert all(marker not in text for marker in forbidden), path
