from __future__ import annotations

import sqlite3

from mapi_core.memory.retrieval_baseline import collect_embedding_coverage, evaluate_golden_cases, fingerprint


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            archived_at TEXT,
            state_code TEXT,
            memory_v2_status TEXT
        );
        CREATE TABLE memory_embeddings_meta (
            memory_id INTEGER PRIMARY KEY,
            model_name TEXT NOT NULL
        );
        INSERT INTO memories VALUES
            (1, NULL, 'active', 'active'),
            (2, NULL, 'validated', 'active'),
            (3, NULL, 'review', 'active'),
            (4, NULL, 'superseded', 'superseded'),
            (5, '2026-01-01T00:00:00Z', 'archived', 'archived');
        INSERT INTO memory_embeddings_meta VALUES
            (1, 'm'),
            (3, 'm'),
            (5, 'm');
        """
    )
    return conn


def test_collect_embedding_coverage_separates_storage_from_retrieval_eligibility() -> None:
    conn = _db()
    result = collect_embedding_coverage(conn)

    assert result["storage_coverage"] == {
        "definition": "memories.archived_at IS NULL",
        "total": 4,
        "with_embedding": 2,
        "without_embedding": 2,
        "coverage_pct": 50.0,
    }
    assert result["retrieval_eligible_coverage"] == {
        "definition": "non-archived AND state_code in active|validated AND memory_v2_status=active",
        "total": 2,
        "with_embedding": 1,
        "without_embedding": 1,
        "coverage_pct": 50.0,
    }
    assert result["orphan_or_archived_embedding_rows"] == 1


def test_fingerprint_is_deterministic_for_equivalent_json() -> None:
    assert fingerprint({"b": 2, "a": 1}) == fingerprint({"a": 1, "b": 2})


def test_evaluate_golden_cases_checks_expected_forbidden_scope_and_latency() -> None:
    def lexical_search(**kwargs):
        assert kwargs["include_history"] is False
        return {
            "count": 1,
            "items": [{"id": 20, "project_key": "p"}],
            "debug": {"retrieval_strategy": ["phrase"]},
        }

    def semantic_search(**kwargs):
        return {
            "status": "ok",
            "results_count": 1,
            "results": [{"memory_id": 20, "project_key": "p"}],
        }

    result = evaluate_golden_cases(
        [
            {
                "case_id": "current",
                "query": "needle",
                "project_key": "p",
                "expected_ids": [20],
                "forbidden_ids": [10],
                "channels": ["lexical", "semantic"],
            }
        ],
        lexical_search=lexical_search,
        semantic_search=semantic_search,
        latency_runs=2,
    )

    assert result["all_passed"] is True
    assert result["passed_count"] == 1
    assert result["cases"][0]["channels"]["lexical"]["latency"]["runs"] == 2
    assert result["cases"][0]["channels"]["semantic"]["returned_ids"] == [20]


def test_evaluate_golden_cases_fails_on_scope_leakage() -> None:
    def lexical_search(**_kwargs):
        return {"count": 1, "items": [{"id": 1, "project_key": "foreign"}], "debug": {}}

    result = evaluate_golden_cases(
        [
            {
                "case_id": "scope",
                "query": "needle",
                "project_key": "expected",
                "expected_ids": [],
                "forbidden_ids": [],
                "channels": ["lexical"],
            }
        ],
        lexical_search=lexical_search,
        semantic_search=lambda **_kwargs: {},
        latency_runs=1,
    )

    channel = result["cases"][0]["channels"]["lexical"]
    assert result["all_passed"] is False
    assert channel["wrong_project_keys"] == ["foreign"]
