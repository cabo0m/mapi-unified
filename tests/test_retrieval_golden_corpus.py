from __future__ import annotations

from pathlib import Path

from mapi_core.memory.retrieval_baseline import (
    RETRIEVAL_GOLDEN_CORPUS_SCHEMA,
    load_retrieval_golden_corpus,
    materialize_golden_cases,
)
from mapi.seed import seed_demo_database


def test_public_golden_corpus_is_portable_and_has_no_database_ids() -> None:
    corpus = load_retrieval_golden_corpus()
    assert corpus["schema"] == RETRIEVAL_GOLDEN_CORPUS_SCHEMA
    assert len(corpus["cases"]) >= 5
    serialized = repr(corpus)
    assert "expected_ids" not in serialized
    assert "2550" not in serialized
    assert all("expected_selectors" in case for case in corpus["cases"])


def test_materialization_resolves_seed_fixtures_without_hardcoded_ids(server) -> None:
    seeded = seed_demo_database(Path(server.DB_PATH))
    assert seeded["status"] in {"seeded", "already_seeded"}
    conn = server.get_db_connection()
    try:
        materialized = materialize_golden_cases(conn)
    finally:
        conn.close()
    assert materialized["case_count"] == 5
    assert materialized["applicable_count"] == 5
    assert materialized["skipped_count"] == 0
    cases = {case["case_id"]: case for case in materialized["cases"]}
    assert cases["demo_lifecycle_preview"]["expected_ids"]
    assert cases["sample_research_alias_resolution"]["expected_ids"]
    assert cases["sample_research_alias_resolution"]["forbidden_ids"]


def test_search_qa_report_executes_golden_corpus_on_seeded_database(server) -> None:
    seed_demo_database(Path(server.DB_PATH))
    result = server.search_qa_report(limit_per_case=10)
    golden = result["golden_corpus"]
    assert golden["schema"] == RETRIEVAL_GOLDEN_CORPUS_SCHEMA
    assert golden["case_count"] == 5
    assert golden["failed_count"] == 0
    assert golden["all_passed"] is True
    assert not [failure for failure in result["failures"] if str(failure.get("case_id", "")).startswith("golden:")]


def test_missing_demo_fixture_is_skipped_not_fabricated(server) -> None:
    conn = server.get_db_connection()
    try:
        materialized = materialize_golden_cases(conn)
    finally:
        conn.close()
    assert materialized["skipped_count"] >= 4
    assert all(item["reason"] == "required_fixture_missing" for item in materialized["skipped"])
