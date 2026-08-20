from __future__ import annotations

import sqlite3

from app import db_migrations
from mapi_core.memory.retention import preview_memory_retention_policy_payload, preview_project_memory_retention_payload


def _setup() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db_migrations.apply_all_migrations(conn)
    return conn


def _insert(conn: sqlite3.Connection, content: str, **values) -> int:
    base = {"memory_type": "temporary", "state_code": "validated", "memory_v2_status": "active", "activity_state": "active", "project_key": "p", "valid_to": "2026-01-01T00:00:00Z"}
    base.update(values)
    columns = ["content", *base]
    cursor = conn.execute(
        f"INSERT INTO memories ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [content, *base.values()],
    )
    return int(cursor.lastrowid)


def _preview(conn, memory_id, as_of="2026-07-01T00:00:00Z"):
    return preview_memory_retention_policy_payload(
        conn, memory_id=memory_id, as_of=as_of, row_to_dict=dict,
        canonical_json_hash=None, utc_now_iso=lambda: as_of, include_debug=True,
    )


def test_preview_is_redacted_deterministic_and_explicit_due() -> None:
    conn = _setup()
    content = "temporary project note"
    memory_id = _insert(conn, content)
    first = _preview(conn, memory_id)
    second = _preview(conn, memory_id)
    assert first["policy_outcome"] == "expire_candidate"
    assert first["preview_hash"] == second["preview_hash"]
    assert content not in repr(first)
    assert first["safety"] == {"read_only": True, "raw_secret_exposed": False, "physical_purge_supported": False}


def test_protected_and_durable_without_due_are_not_archived_by_age() -> None:
    conn = _setup()
    core_id = _insert(conn, "core", entry_type="core", valid_to=None)
    durable_id = _insert(conn, "old durable", memory_type="project_fact", valid_to=None, created_at="2020-01-01")
    assert _preview(conn, core_id)["policy_outcome"] == "protected"
    assert _preview(conn, durable_id)["policy_outcome"] == "retain"


def test_project_preview_uses_exact_namespace_and_counts() -> None:
    conn = _setup()
    wanted = _insert(conn, "one")
    _insert(conn, "other", project_key="p-child")
    result = preview_project_memory_retention_payload(
        conn, project_key="p", as_of="2026-07-01T00:00:00Z", limit=50,
        include_retain=True, include_debug=False, row_to_dict=dict,
        canonical_json_hash=None, utc_now_iso=lambda: "unused",
    )
    assert result["source_memory_ids"] == [wanted]
    assert result["summary"]["scanned_count"] == 1
