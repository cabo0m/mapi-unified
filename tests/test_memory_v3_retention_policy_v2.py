from __future__ import annotations

import sqlite3

from app import db_migrations
from mapi_core.memory.retention import (
    RETENTION_POLICY_VERSION,
    RETENTION_PROJECT_PREVIEW_SCHEMA_VERSION,
    preview_memory_retention_policy_payload,
    preview_project_memory_retention_payload,
)

AS_OF = "2026-07-30T08:07:00Z"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db_migrations.apply_all_migrations(conn)
    conn.commit()
    return conn


def _insert(conn: sqlite3.Connection, **values) -> int:
    payload = {
        "content": "retention policy fixture",
        "summary_short": "retention fixture",
        "title": "Retention fixture",
        "memory_type": "project_note",
        "entry_type": "project",
        "truth_kind": "fact",
        "state_code": "validated",
        "memory_v2_status": "active",
        "activity_state": "active",
        "project_key": "mapi",
        "scope_code": "project",
        "workspace_id": 1,
        "importance_score": 0.2,
        "importance_level": "low",
        "priority": "low",
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-01T00:00:00Z",
        "tags": "test,fixture",
    }
    payload.update(values)
    columns = list(payload)
    cursor = conn.execute(
        f"INSERT INTO memories ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [payload[column] for column in columns],
    )
    conn.commit()
    return int(cursor.lastrowid)


def _preview(conn: sqlite3.Connection, memory_id: int) -> dict:
    return preview_memory_retention_policy_payload(
        conn,
        memory_id=memory_id,
        as_of=AS_OF,
        include_debug=True,
        row_to_dict=dict,
        canonical_json_hash=None,
        utc_now_iso=lambda: AS_OF,
    )


def test_migration_0030_enables_exact_review_only_rollout() -> None:
    conn = _conn()
    flag = dict(
        conn.execute(
            "SELECT * FROM feature_flags WHERE flag_key='memory_v3_retention_enabled'"
        ).fetchone()
    )
    assert flag["is_enabled"] == 1
    assert flag["rollout_mode"] == "projects_and_scopes"
    assert flag["allowed_project_keys"] == "mapi"
    assert flag["allowed_scope_codes"] == "project"
    assert flag["read_only_mode"] == 1
    assert db_migrations.MIGRATION_SEQUENCE[-1][0] == "0036_memory_self_healing"


def test_old_low_test_memory_becomes_soft_archive_candidate() -> None:
    conn = _conn()
    memory_id = _insert(
        conn,
        title="Smoke test create_memory",
        tags="test,smoke,diagnostic",
        created_at="2026-04-21T00:00:00Z",
    )
    preview = _preview(conn, memory_id)
    assert preview["policy_version"] == "memory_v3_retention_policy.v2"
    assert preview["policy_outcome"] == "archive_candidate"
    assert preview["proposed_action"] == "archive_candidate"
    assert preview["reason_codes"] == ["aged_low_value_transient"]
    assert preview["age_days"] >= 90
    assert preview["guard"]["apply_eligible"] is True
    assert preview["safety"]["physical_purge_supported"] is False


def test_recent_or_recalled_test_memory_is_retained() -> None:
    conn = _conn()
    recent_id = _insert(
        conn,
        title="Recent smoke test",
        created_at="2026-07-20T00:00:00Z",
    )
    recalled_id = _insert(
        conn,
        title="Old but recalled smoke test",
        created_at="2026-04-01T00:00:00Z",
        last_recalled_at="2026-07-25T00:00:00Z",
    )
    assert _preview(conn, recent_id)["policy_outcome"] == "retain"
    recalled = _preview(conn, recalled_id)
    assert recalled["policy_outcome"] == "retain"
    assert recalled["recently_recalled"] is True


def test_age_alone_does_not_archive_durable_or_medium_memory() -> None:
    conn = _conn()
    durable_id = _insert(
        conn,
        memory_type="project_fact",
        title="Old durable project fact",
        summary_short="Old durable project fact",
        tags="project,fact",
        created_at="2020-01-01T00:00:00Z",
    )
    medium_id = _insert(
        conn,
        title="Old test with medium importance",
        importance_level="medium",
        importance_score=0.5,
        priority="normal",
        created_at="2020-01-01T00:00:00Z",
    )
    assert _preview(conn, durable_id)["policy_outcome"] == "retain"
    assert _preview(conn, medium_id)["policy_outcome"] == "retain"


def test_identity_relation_decision_milestone_and_checkpoint_are_protected() -> None:
    conn = _conn()
    protected = [
        _insert(conn, memory_type="identity", entry_type="user_profile"),
        _insert(conn, memory_type="relation_note", entry_type="user_profile"),
        _insert(conn, memory_type="project_decision", entry_type="decision", truth_kind="decision"),
        _insert(conn, memory_type="project_milestone"),
        _insert(conn, memory_type="project_checkpoint"),
    ]
    for memory_id in protected:
        preview = _preview(conn, memory_id)
        assert preview["policy_outcome"] == "protected"
        assert preview["proposed_action"] is None
        assert preview["guard"]["apply_eligible"] is False


def test_project_preview_scans_full_namespace_and_limit_only_caps_returned_items() -> None:
    conn = _conn()
    for index in range(205):
        _insert(
            conn,
            content=f"durable {index}",
            title=f"Durable {index}",
            memory_type="project_fact",
            tags="project,fact",
            importance_level="medium",
            importance_score=0.5,
            priority="normal",
        )
    actionable_id = _insert(
        conn,
        title="Late ID smoke test",
        tags="test,smoke",
        created_at="2026-04-01T00:00:00Z",
    )
    result = preview_project_memory_retention_payload(
        conn,
        project_key="mapi",
        as_of=AS_OF,
        limit=5,
        include_retain=False,
        include_debug=False,
        row_to_dict=dict,
        canonical_json_hash=None,
        utc_now_iso=lambda: AS_OF,
    )
    assert result["schema_version"] == RETENTION_PROJECT_PREVIEW_SCHEMA_VERSION
    assert result["policy_version"] == RETENTION_POLICY_VERSION
    assert result["summary"]["scanned_count"] == 206
    assert actionable_id in result["source_memory_ids"]
    assert result["canary_candidates"][0]["memory_id"] == actionable_id
    assert result["summary"]["returned_count"] <= 5
    assert result["safety"]["full_project_scan"] is True


def test_canary_requires_no_links_or_lifecycle_pointer() -> None:
    conn = _conn()
    clean_id = _insert(conn, title="Clean smoke test")
    linked_id = _insert(conn, title="Linked smoke test")
    target_id = _insert(
        conn,
        title="Durable target",
        memory_type="project_fact",
        tags="project,fact",
        importance_level="medium",
        importance_score=0.5,
        priority="normal",
    )
    pointer_id = _insert(conn, title="Pointer smoke test", supersedes_memory_id=target_id)
    conn.execute(
        "INSERT INTO memory_links(from_memory_id,to_memory_id,relation_type,weight) VALUES(?,?,?,?)",
        (linked_id, target_id, "related", 1.0),
    )
    conn.commit()
    result = preview_project_memory_retention_payload(
        conn,
        project_key="mapi",
        as_of=AS_OF,
        limit=50,
        include_retain=False,
        include_debug=False,
        row_to_dict=dict,
        canonical_json_hash=None,
        utc_now_iso=lambda: AS_OF,
    )
    by_id = {int(item["memory_id"]): item for item in result["items"]}
    assert by_id[clean_id]["canary"]["eligible"] is True
    assert "linked_memory" in by_id[linked_id]["canary"]["blockers"]
    assert "lifecycle_pointer_present" in by_id[pointer_id]["canary"]["blockers"]


def test_project_preview_remains_redacted_and_never_supports_purge() -> None:
    conn = _conn()
    raw = "PRIVATE-RAW-CONTENT-MARKER"
    _insert(conn, content=raw, title="Old smoke test")
    result = preview_project_memory_retention_payload(
        conn,
        project_key="mapi",
        as_of=AS_OF,
        limit=20,
        include_retain=False,
        include_debug=True,
        row_to_dict=dict,
        canonical_json_hash=None,
        utc_now_iso=lambda: AS_OF,
    )
    assert raw not in repr(result)
    assert result["safety"]["physical_purge_supported"] is False


def test_project_canary_hash_matches_single_preview_hash() -> None:
    conn = _conn()
    memory_id = _insert(
        conn,
        title="Hash parity smoke test",
        tags="test,smoke",
        created_at="2026-04-01T00:00:00Z",
    )
    single = _preview(conn, memory_id)
    project = preview_project_memory_retention_payload(
        conn,
        project_key="mapi",
        as_of=AS_OF,
        limit=20,
        include_retain=False,
        include_debug=False,
        row_to_dict=dict,
        canonical_json_hash=None,
        utc_now_iso=lambda: AS_OF,
    )
    item = next(entry for entry in project["items"] if int(entry["memory_id"]) == memory_id)
    canary = next(entry for entry in project["canary_candidates"] if int(entry["memory_id"]) == memory_id)
    assert item["preview_hash"] == single["preview_hash"]
    assert canary["preview_hash"] == single["preview_hash"]
