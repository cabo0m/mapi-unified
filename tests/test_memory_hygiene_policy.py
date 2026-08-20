from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import db_migrations
from mapi_core.memory import hygiene


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db_migrations.apply_all_migrations(conn)
    conn.commit()
    return conn


def _insert_memory(
    conn: sqlite3.Connection,
    *,
    content: str,
    memory_type: str,
    project_key: str = "mapi",
    scope_code: str | None = "project",
    layer_code: str | None = "projects",
    area_code: str | None = "projects",
    state_code: str = "validated",
    owner_role: str | None = None,
    importance_score: float = 0.95,
    importance_level: str = "critical",
    priority: str = "critical",
    tags: str | None = None,
    revalidation_due_at: str | None = "2026-07-01T00:00:00Z",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO memories (
            content, summary_short, title, memory_type, project_key, scope_code,
            layer_code, area_code, state_code, activity_state, owner_role,
            importance_score, importance_level, priority, tags,
            revalidation_due_at, memory_v2_status, truth_kind, entry_type,
            created_at, updated_at, last_accessed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?,
                  'active', 'fact', 'project', '2026-06-01T00:00:00Z',
                  '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z')
        """,
        (
            content,
            content[:120],
            content[:120],
            memory_type,
            project_key,
            scope_code,
            layer_code,
            area_code,
            state_code,
            owner_role,
            importance_score,
            importance_level,
            priority,
            tags,
            revalidation_due_at,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def test_migration_0029_creates_ledgers_and_repairs_project_owner_mapping() -> None:
    conn = _conn()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"memory_hygiene_runs", "memory_hygiene_run_items"} <= tables
    mapping = conn.execute(
        """
        SELECT owner_key, project_key, scope_code, is_active
        FROM owner_role_mappings
        WHERE owner_role='project_maintainer'
          AND owner_key='project_agent_maintainers'
          AND project_key='mapi'
        """
    ).fetchone()
    assert tuple(mapping) == (
        "project_agent_maintainers",
        "mapi",
        "project",
        1,
    )
    versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions[-1] == "0036_memory_self_healing"


def test_scope_policy_preserves_semantic_global_and_repairs_missing_project_scope() -> None:
    allowed = {
        "project_key": "mapi",
        "scope_code": "global",
        "memory_type": "identity",
        "layer_code": "core",
        "area_code": "identity",
        "tags": "agent,identity",
    }
    broken = {
        "project_key": "mapi",
        "scope_code": None,
        "memory_type": "project_state",
        "layer_code": "projects",
        "area_code": "projects",
        "tags": "project-state",
    }
    assert hygiene.recommend_scope(allowed)["value"] == "global"
    assert hygiene.recommend_scope(broken)["value"] == "project"


def test_owner_policy_canonicalizes_legacy_roles_and_defaults_by_boundary() -> None:
    legacy = hygiene.recommend_owner_role(
        {"owner_role": "operator", "project_key": "mapi", "scope_code": "project"}
    )
    project = hygiene.recommend_owner_role(
        {"owner_role": None, "project_key": "mapi", "scope_code": "project"}
    )
    global_identity = hygiene.recommend_owner_role(
        {
            "owner_role": None,
            "project_key": "mapi",
            "scope_code": "global",
            "memory_type": "identity",
            "area_code": "identity",
        }
    )
    assert legacy["value"] == "project_maintainer"
    assert project["value"] == "project_maintainer"
    assert global_identity["value"] == "knowledge_curator"


def test_importance_policy_keeps_core_and_decisions_high_but_tests_low() -> None:
    core = hygiene.recommend_importance(
        {
            "memory_type": "operator_preference",
            "layer_code": "core",
            "area_code": "projects",
            "title": "Risk-based test scope",
            "tags": "operator-preference,testing",
        }
    )
    decision = hygiene.recommend_importance(
        {
            "memory_type": "project_decision",
            "layer_code": "projects",
            "title": "Pytest isolation fix accepted",
            "tags": "pytest,accepted",
        }
    )
    test_note = hygiene.recommend_importance(
        {
            "memory_type": "project_note",
            "layer_code": "projects",
            "title": "Smoke test create memory",
            "tags": "test,diagnostic",
        }
    )
    identity = hygiene.recommend_importance(
        {
            "memory_type": "identity",
            "layer_code": "core",
            "area_code": "identity",
            "title": "MAPI identity",
        }
    )
    assert core["level"] == "high"
    assert decision["level"] == "high"
    assert test_note["level"] == "low"
    assert identity["level"] == "critical"


def test_new_write_policy_caps_normal_checkpoint_but_preserves_critical_identity() -> None:
    checkpoint = hygiene.apply_new_write_importance_policy(
        memory_type="project_checkpoint",
        requested_score=0.99,
        project_key="mapi",
        scope_code="project",
        tags="checkpoint,deployed",
        title="Sprint deployed",
        summary_short="Sprint deployed",
        entry_type="project",
        truth_kind="decision",
        source_context="verified tests, SQLite backup and rollback preview",
        source_event_ref="chat:test",
    )
    identity = hygiene.apply_new_write_importance_policy(
        memory_type="identity",
        requested_score=1.0,
        project_key="mapi",
        scope_code="global",
        tags="identity,core",
        title="MAPI identity",
        summary_short="MAPI identity",
        entry_type="core",
        truth_kind="fact",
        source_context="bootstrap",
        source_event_ref="bootstrap:test",
        layer_code="core",
        area_code="identity",
    )
    assert checkpoint["effective_level"] == "high"
    assert checkpoint["effective_score"] == 0.75
    assert checkpoint["capped"] is True
    assert identity["effective_level"] == "critical"
    assert identity["effective_score"] == 1.0


def test_revalidation_policy_separates_immutable_history_from_actionable_state() -> None:
    checkpoint = hygiene.classify_revalidation(
        {"memory_type": "project_checkpoint", "state_code": "validated", "revalidation_due_at": "2026-07-01T00:00:00Z"},
        as_of="2026-07-30T00:00:00Z",
    )
    decision = hygiene.classify_revalidation(
        {"memory_type": "project_decision", "state_code": "validated", "revalidation_due_at": "2026-07-01T00:00:00Z"},
        as_of="2026-07-30T00:00:00Z",
    )
    assert checkpoint == {
        "category": "historical",
        "overdue": True,
        "clear_due_at": True,
        "reason_codes": ["immutable_or_transient_record"],
    }
    assert decision["category"] == "actionable"
    assert decision["overdue"] is True
    assert decision["clear_due_at"] is False


def test_preview_is_deterministic_and_contains_only_allowlisted_metadata() -> None:
    conn = _conn()
    _insert_memory(
        conn,
        content="Legal identity memory",
        memory_type="identity",
        scope_code="global",
        layer_code="core",
        area_code="identity",
        importance_score=1.0,
        importance_level="critical",
        tags="identity,core",
    )
    _insert_memory(
        conn,
        content="Historical checkpoint",
        memory_type="project_checkpoint",
        scope_code=None,
        importance_score=0.99,
        importance_level="critical",
        tags="checkpoint,completed",
    )
    first = hygiene.build_hygiene_preview(
        conn, project_key="mapi", as_of="2026-07-30T00:00:00Z"
    )
    second = hygiene.build_hygiene_preview(
        conn, project_key="mapi", as_of="2026-07-30T00:00:00Z"
    )
    assert first["status"] == "preview_ready"
    assert first["preview_hash"] == second["preview_hash"]
    assert first["candidate_set_fingerprint"] == second["candidate_set_fingerprint"]
    assert first["sentinel_findings"] == []
    for candidate in first["candidates"]:
        assert set(candidate["new"]) <= set(hygiene.MUTABLE_METADATA_FIELDS)
    identity = conn.execute("SELECT id FROM memories WHERE content='Legal identity memory'").fetchone()[0]
    assert all(
        not (item["memory_id"] == identity and item["new"].get("scope_code") == "project")
        for item in first["candidates"]
    )


def test_apply_is_metadata_only_stale_guarded_and_exactly_rollbackable(tmp_path: Path) -> None:
    conn = _conn()
    first_id = _insert_memory(
        conn,
        content="Test diagnostic note",
        memory_type="project_note",
        scope_code=None,
        owner_role=None,
        importance_score=0.99,
        importance_level="critical",
        tags="test,diagnostic",
    )
    second_id = _insert_memory(
        conn,
        content="Durable project decision",
        memory_type="project_decision",
        owner_role="operator",
        importance_score=0.99,
        importance_level="critical",
        tags="decision,accepted",
    )
    conn.execute(
        "INSERT INTO memory_links(from_memory_id,to_memory_id,relation_type,weight,origin) VALUES(?,?,?,?,?)",
        (first_id, second_id, "related_to", 1.0, "pytest"),
    )
    conn.commit()
    before_rows = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM memories WHERE id IN (?,?)", (first_id, second_id))
    }
    before_links = [dict(row) for row in conn.execute("SELECT * FROM memory_links ORDER BY id")]
    preview = hygiene.build_hygiene_preview(
        conn, project_key="mapi", as_of="2026-07-30T00:00:00Z"
    )
    backup = tmp_path / "verified.db"
    backup.write_bytes(b"verified-backup-fixture")
    with pytest.raises(ValueError, match="expected_preview_hash mismatch"):
        hygiene.apply_hygiene_preview(
            conn,
            project_key="mapi",
            expected_preview_hash="sha256:stale",
            applied_by="pytest",
            reason="stale guard",
            backup_path=str(backup),
            confirm_metadata_repair=True,
            as_of="2026-07-30T00:00:00Z",
        )
    result = hygiene.apply_hygiene_preview(
        conn,
        project_key="mapi",
        expected_preview_hash=preview["preview_hash"],
        applied_by="pytest",
        reason="metadata repair fixture",
        backup_path=str(backup),
        confirm_metadata_repair=True,
        as_of="2026-07-30T00:00:00Z",
    )
    conn.commit()
    assert result["status"] == "completed"
    after_rows = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM memories WHERE id IN (?,?)", (first_id, second_id))
    }
    after_links = [dict(row) for row in conn.execute("SELECT * FROM memory_links ORDER BY id")]
    assert after_links == before_links
    for memory_id in (first_id, second_id):
        for field, value in before_rows[memory_id].items():
            if field not in set(hygiene.MUTABLE_METADATA_FIELDS) | {"updated_at"}:
                assert after_rows[memory_id][field] == value
    assert after_rows[first_id]["scope_code"] == "project"
    assert after_rows[first_id]["importance_level"] == "low"
    assert after_rows[second_id]["owner_role"] == "project_maintainer"
    rollback_preview = hygiene.preview_hygiene_rollback(conn, run_id=result["run_id"])
    assert rollback_preview["status"] == "rollback_ready"
    rolled_back = hygiene.rollback_hygiene_run(
        conn,
        run_id=result["run_id"],
        expected_rollback_preview_hash=rollback_preview["rollback_preview_hash"],
        rolled_back_by="pytest",
        notes="exact restore",
    )
    conn.commit()
    assert rolled_back["restored_count"] == result["changed_count"]
    restored_rows = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM memories WHERE id IN (?,?)", (first_id, second_id))
    }
    for memory_id in (first_id, second_id):
        for field in hygiene.MUTABLE_METADATA_FIELDS:
            assert restored_rows[memory_id][field] == before_rows[memory_id][field]
        assert restored_rows[memory_id]["updated_at"] == before_rows[memory_id]["updated_at"]
    assert [dict(row) for row in conn.execute("SELECT * FROM memory_links ORDER BY id")] == before_links
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_events WHERE event_type='memory.hygiene.metadata_applied'"
    ).fetchone()[0] == result["changed_count"]
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_events WHERE event_type='memory.hygiene.metadata_rolled_back'"
    ).fetchone()[0] == result["changed_count"]


def test_quality_scope_counter_ignores_legal_global_identity() -> None:
    from mapi_core.memory.quality import count_project_scope_mismatches, project_scope_mismatch_rows

    conn = _conn()
    _insert_memory(
        conn,
        content="Global identity",
        memory_type="identity",
        scope_code="global",
        layer_code="core",
        area_code="identity",
        tags="identity,core",
    )
    broken_id = _insert_memory(
        conn,
        content="Historical project state without scope",
        memory_type="project_state",
        scope_code=None,
        tags="project-state,accepted",
    )
    assert count_project_scope_mismatches(conn, project_key="mapi") == 1
    rows = project_scope_mismatch_rows(
        conn,
        project_key="mapi",
        limit=50,
        normalize_optional_text=lambda value: str(value).strip() if value is not None and str(value).strip() else None,
        row_to_dict=dict,
    )
    assert [item["id"] for item in rows] == [broken_id]


def test_server_owner_defaults_use_hygiene_policy_for_active_project_memory(server) -> None:
    item = server._apply_ownership_defaults(
        {
            "id": 1,
            "project_key": "mapi",
            "scope_code": "project",
            "state_code": "active",
            "owner_role": None,
            "memory_type": "project_state",
        }
    )
    legacy = server._apply_ownership_defaults(
        {
            "id": 2,
            "project_key": "mapi",
            "scope_code": "project",
            "state_code": "validated",
            "owner_role": "operator",
            "memory_type": "project_decision",
        }
    )
    assert item["owner_role"] == "project_maintainer"
    assert legacy["owner_role"] == "project_maintainer"


def test_governance_workshop_exposes_hygiene_with_admin_only_mutations() -> None:
    from mcp_surface import WORKSHOPS

    actions = {item.action: item for item in WORKSHOPS["governance"].actions}
    assert actions["owner_workload"].payload_schema is not None
    assert actions["scope_mismatches"].payload_schema == {"project_key": "str|null", "limit": "int"}
    assert actions["hygiene_inventory"].min_profile == "reader"
    assert actions["hygiene_preview"].min_profile == "reader"
    assert actions["hygiene_apply"].min_profile == "admin"
    assert actions["hygiene_apply"].risk_class == "R3"
    assert actions["hygiene_apply"].backup_required is True
    assert actions["hygiene_rollback"].min_profile == "admin"
    assert actions["hygiene_rollback"].risk_class == "R3"
    assert actions["hygiene_rollback"].backup_required is True


def test_default_revalidation_queue_requires_due_date(server, memory_factory) -> None:
    unscheduled_id = memory_factory(
        content="Immutable checkpoint without recurring revalidation.",
        memory_type="project_checkpoint",
        summary_short="No recurring due date",
        source="pytest",
        importance_score=0.75,
        confidence_score=1.0,
        tags="checkpoint,completed",
        layer_code="projects",
        area_code="projects",
        state_code="validated",
        scope_code="project",
        project_key="revalidation-due-policy",
        last_validated_at=None,
        revalidation_due_at=None,
    )
    due_id = memory_factory(
        content="Active decision with an explicit revalidation deadline.",
        memory_type="project_decision",
        summary_short="Actionable due date",
        source="pytest",
        importance_score=0.8,
        confidence_score=1.0,
        tags="decision,active",
        layer_code="projects",
        area_code="projects",
        state_code="validated",
        scope_code="project",
        project_key="revalidation-due-policy",
        last_validated_at=None,
        revalidation_due_at="2026-07-01T00:00:00Z",
    )
    queue = server.list_revalidation_queue(
        project_key="revalidation-due-policy",
        scope_code="project",
        limit=20,
    )
    ids = [int(item["id"]) for item in queue["items"]]
    assert due_id in ids
    assert unscheduled_id not in ids


def test_checkpoint_audit_terms_do_not_escalate_to_critical() -> None:
    recommendation = hygiene.recommend_importance(
        {
            "memory_type": "project_checkpoint",
            "project_key": "mapi",
            "scope_code": "project",
            "title": "Sprint deployed after backup and rollback verification",
            "summary_short": "Backup, security and rollback audit completed",
            "source_context": "verified backup, rollback and runtime freshness",
            "source_event_ref": "chat:sprint-checkpoint",
            "tags": "checkpoint,deployed,backup,rollback,security",
        }
    )
    assert recommendation["level"] == "high"
