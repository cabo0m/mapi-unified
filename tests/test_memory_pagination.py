from __future__ import annotations

import base64
import json
import sqlite3

import mcp_surface

from mapi_core.memory.pagination import (
    COMPACT_FIELDS,
    DEFAULT_FIELDS,
    MEMORY_LIST_CURSOR_SCHEMA,
    MEMORY_LIST_ORDER,
    PROJECTION_FIELDS,
    decode_cursor,
    list_memory_page,
    normalize_projection,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            title TEXT,
            summary_short TEXT,
            content TEXT,
            memory_type TEXT,
            entry_type TEXT,
            truth_kind TEXT,
            project_key TEXT,
            scope_code TEXT,
            state_code TEXT,
            memory_v2_status TEXT,
            importance_score REAL,
            confidence_score REAL,
            tags TEXT,
            source TEXT,
            source_context TEXT,
            source_event_ref TEXT,
            conversation_key TEXT,
            created_at TEXT,
            updated_at TEXT,
            valid_from TEXT,
            valid_to TEXT,
            archived_at TEXT
        );
        """
    )
    rows = [
        (1, "one", "one", "content one", "checkpoint", "project", "fact", "jwst-research", "project", "validated", "active", 0.7, 1.0, "ARC,Q1", "pytest", None, None, None, "2026-08-15T10:00:00Z", None, None, None, None),
        (2, "two", "two", "content two", "checkpoint", "project", "fact", "jwst-research", "project", "validated", "active", 0.7, 1.0, "ARC,Q2", "pytest", None, None, None, "2026-08-15T11:00:00Z", None, None, None, None),
        (3, "three", "three", "content three", "checkpoint", "project", "fact", "jwst-research", "project", "validated", "active", 0.7, 1.0, "ARC,Q3", "pytest", None, None, None, None, None, None, None, None),
        (4, "four", "four", "content four", "checkpoint", "project", "fact", "jwst-research", "project", "validated", "active", 0.7, 1.0, "ARC,Q4", "pytest", None, None, None, "2026-08-15T12:00:00Z", None, None, None, None),
        (5, "other", "other", "content other", "checkpoint", "project", "fact", "morenatech", "project", "validated", "active", 0.7, 1.0, "MAPI", "pytest", None, None, None, "2026-08-15T13:00:00Z", None, None, None, None),
    ]
    conn.executemany(
        """
        INSERT INTO memories (
            id,title,summary_short,content,memory_type,entry_type,truth_kind,project_key,scope_code,
            state_code,memory_v2_status,importance_score,confidence_score,tags,source,source_context,
            source_event_ref,conversation_key,created_at,updated_at,valid_from,valid_to,archived_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    return conn


def _filters(project_key_values: list[str] | None = None) -> dict:
    return {
        "requested_project_key": "ARC" if project_key_values else None,
        "canonical_project_key": "jwst-research" if project_key_values else None,
        "project_key_mode": "aliases" if project_key_values else "exact",
        "project_key_values": project_key_values or [],
        "scope_code": None,
        "memory_type": None,
        "state_code": None,
        "truth_kind": None,
        "tag": None,
        "include_archived": False,
    }


def test_projection_defaults_compact_and_custom_are_allowlisted() -> None:
    assert normalize_projection(fields=None, compact=False) == DEFAULT_FIELDS
    assert normalize_projection(fields=None, compact=True) == COMPACT_FIELDS
    assert normalize_projection(fields=["summary_short", "project_key"], compact=False) == (
        "id",
        "summary_short",
        "project_key",
    )

    try:
        normalize_projection(fields=["id", "drop table memories"], compact=False)
    except ValueError as exc:
        assert str(exc) == "unsupported_projection_field:drop table memories"
    else:
        raise AssertionError("unsupported projection field must fail closed")


def test_keyset_pages_are_stable_and_cover_null_created_at() -> None:
    conn = _conn()
    try:
        first = list_memory_page(
            conn,
            filters=_filters(["jwst-research"]),
            fields=("id", "created_at"),
            compact=False,
            page_size=2,
            cursor=None,
        )
        assert first["status"] == "ok"
        assert [item["id"] for item in first["items"]] == [4, 2]
        assert first["has_more"] is True
        assert first["next_cursor"]

        second = list_memory_page(
            conn,
            filters=_filters(["jwst-research"]),
            fields=("id", "created_at"),
            compact=False,
            page_size=2,
            cursor=first["next_cursor"],
        )
        assert [item["id"] for item in second["items"]] == [1, 3]
        assert second["has_more"] is False
        assert second["next_cursor"] is None
        assert set(item["id"] for item in first["items"]) .isdisjoint(item["id"] for item in second["items"])
    finally:
        conn.close()


def test_snapshot_max_id_excludes_insert_between_pages_even_if_backdated() -> None:
    conn = _conn()
    try:
        first = list_memory_page(
            conn,
            filters=_filters(["jwst-research"]),
            fields=("id", "created_at"),
            compact=False,
            page_size=1,
            cursor=None,
        )
        assert first["items"][0]["id"] == 4
        assert first["snapshot_max_id"] == 5

        conn.execute(
            """
            INSERT INTO memories (id,title,summary_short,content,memory_type,entry_type,truth_kind,project_key,scope_code,state_code,memory_v2_status,importance_score,confidence_score,tags,source,created_at)
            VALUES (6,'late insert','late insert','late insert','checkpoint','project','fact','jwst-research','project','validated','active',0.7,1.0,'ARC,QX','pytest','2026-08-15T11:30:00Z')
            """
        )
        conn.commit()

        seen = [4]
        cursor = first["next_cursor"]
        while cursor:
            page = list_memory_page(
                conn,
                filters=_filters(["jwst-research"]),
                fields=("id", "created_at"),
                compact=False,
                page_size=1,
                cursor=cursor,
            )
            seen.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
        assert seen == [4, 2, 1, 3]
        assert 6 not in seen
    finally:
        conn.close()


def test_cursor_is_bound_to_query_and_projection() -> None:
    conn = _conn()
    try:
        first = list_memory_page(
            conn,
            filters=_filters(["jwst-research"]),
            fields=("id", "summary_short"),
            compact=False,
            page_size=1,
            cursor=None,
        )
        mismatch_filter = list_memory_page(
            conn,
            filters=_filters(["morenatech"]),
            fields=("id", "summary_short"),
            compact=False,
            page_size=1,
            cursor=first["next_cursor"],
        )
        mismatch_fields = list_memory_page(
            conn,
            filters=_filters(["jwst-research"]),
            fields=("id", "title"),
            compact=False,
            page_size=1,
            cursor=first["next_cursor"],
        )
        assert mismatch_filter["error"] == "cursor_query_mismatch"
        assert mismatch_fields["error"] == "cursor_query_mismatch"
    finally:
        conn.close()


def test_cursor_checksum_rejects_tampering() -> None:
    conn = _conn()
    try:
        first = list_memory_page(
            conn,
            filters=_filters(["jwst-research"]),
            fields=("id",),
            compact=True,
            page_size=1,
            cursor=None,
        )
        cursor = first["next_cursor"]
        assert cursor
        padding = "=" * (-len(cursor) % 4)
        envelope = json.loads(base64.urlsafe_b64decode((cursor + padding).encode("ascii")).decode("utf-8"))
        envelope["payload"]["snapshot_max_id"] = 999999
        raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        tampered = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        try:
            decode_cursor(tampered)
        except ValueError as exc:
            assert str(exc) == "invalid_cursor_checksum"
        else:
            raise AssertionError("tampered cursor must fail closed")
    finally:
        conn.close()


def test_server_list_page_resolves_arc_alias_and_compact_projection(server, memory_factory) -> None:
    alias = server.upsert_project_key_alias(
        canonical_project_key="jwst-research",
        alias_project_key="ARC",
        alias_kind="test",
        status="active",
        notes="pytest alias for pagination parity",
    )
    assert alias.get("status") in {"ok", "created", "updated"}
    target = memory_factory(
        content="ARC pagination target with content that compact mode must omit",
        memory_type="project_checkpoint",
        summary_short="ARC pagination target",
        source="pytest",
        importance_score=0.8,
        confidence_score=1.0,
        tags="ARC,pagination",
        layer_code="projects",
        area_code="projects",
        state_code="validated",
        scope_code="project",
        project_key="jwst-research",
    )
    result = server.list_memories_page(
        page_size=20,
        project_key="ARC",
        project_key_mode="aliases",
        tag="pagination",
        compact=True,
    )

    assert result["status"] == "ok"
    assert result["requested_project_key"] == "ARC"
    assert result["canonical_project_key"] == "jwst-research"
    assert target in {int(item["id"]) for item in result["items"]}
    assert result["projection"]["fields"] == list(COMPACT_FIELDS)
    assert all("content" not in item for item in result["items"])
    assert result["safety"]["offset_pagination_used"] is False


def test_server_list_page_rejects_invalid_projection_and_page_size(server) -> None:
    projection = server.list_memories_page(fields=["summary_short", "not_a_field"])
    page_size = server.list_memories_page(page_size=101)

    assert projection["error"] == "unsupported_projection_field"
    assert projection["field"] == "not_a_field"
    assert set(COMPACT_FIELDS).issubset(set(projection["allowed_fields"]))
    assert page_size == {
        "status": "error",
        "error": "page_size_out_of_range",
        "allowed_range": [1, 100],
        "actual": 101,
    }


def test_memory_workshop_exposes_list_page_and_capabilities_report_r4b(server) -> None:
    workshop = mcp_surface.open_workshop_payload("memory", profile="reader")
    actions = {item["action"]: item for item in workshop["actions"]}
    action = actions["list_page"]
    assert action["tool_name"] == "list_memories_page"
    assert action["risk_class"] == "R0"
    assert action["payload_schema"]["fields"] == "array|null"
    assert action["payload_constraints"]["project_key_mode"]["enum"] == ["exact", "aliases"]

    capabilities = server.get_mapi_capabilities()
    assert capabilities["features"]["cursor_pagination"] is True
    assert capabilities["features"]["field_projection"] is True
    assert capabilities["features"]["compact_responses"] is True
    assert capabilities["contracts"]["memory.list_page"]["order"] == MEMORY_LIST_ORDER


def test_workshop_list_page_returns_compact_response(server, memory_factory) -> None:
    memory_factory(
        content="Workshop compact pagination target",
        memory_type="project_checkpoint",
        summary_short="Workshop compact pagination target",
        source="pytest",
        importance_score=0.8,
        confidence_score=1.0,
        tags="R4B,workshop-page",
        layer_code="projects",
        area_code="projects",
        state_code="validated",
        scope_code="project",
        project_key="jagoda-memory-api",
    )
    result = server.run_workshop_action(
        "memory",
        "list_page",
        payload={
            "page_size": 5,
            "project_key": "jagoda-memory-api",
            "project_key_mode": "exact",
            "tag": "R4B",
            "compact": True,
        },
    )
    assert result["status"] == "ok"
    page = result["result"]
    assert page["status"] == "ok"
    assert page["returned_count"] >= 1
    assert all(set(item).issubset(set(COMPACT_FIELDS)) for item in page["items"])
    assert all("id" in item for item in page["items"])


def test_projection_allowlist_is_not_empty() -> None:
    assert "id" in PROJECTION_FIELDS
    assert "content" in PROJECTION_FIELDS
    assert len(PROJECTION_FIELDS) > len(COMPACT_FIELDS)
