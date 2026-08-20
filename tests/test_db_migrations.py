from __future__ import annotations

import sqlite3

from app import db_migrations


EXPECTED_VERSIONS = {
    "0001_memory_core",
    "0002_timeline_schema",
    "0003_timeline_schema_hardening",
    "0004_project_timeline_semantics",
    "0005_memory_layer_area_metadata",
    "0006_feature_flags",
    "0007_ownership_sla",
    "0008_expired_duplicate_sla",
    "0009_owner_resolution_layer",
    "0010_multiuser_identity_foundation",
    "0011_scope_aware_maintenance",
    "0012_priority_and_sla_policies",
    "0013_escalation_history",
    "0014_research_ingest_quarantine",
    "0015_conversation_archive",
    "0016_gemma_worker_jobs",
    "0017_project_key_aliases",
    "0018_bridge_mailbox",
    "0019_memory_entry_v2_foundation",
    "0020_memory_consolidation_review_queue",
    "0021_consolidation_apply_preview_snapshots",
    "0022_consolidation_rollback_preview_snapshots",
    "0023_memory_v3_lifecycle_snapshots",
    "0024_memory_capture_review_queue",
    "0025_memory_v3_policy_metadata",
    "0026_sandman_semantic_shadow_runs",
    "0027_memory_v3_pointer_lifecycle_execution",
    "0028_sandman_canonical_scheduler",
    "0029_memory_hygiene_metadata_repair",
    "0030_memory_retention_policy_v2",
    "0031_private_remote_auth",
    "0032_retire_bridge_mailbox",
    "0033_mcp_idempotency_requests",
    "0034_recall_importance_decoupling",
    "0035_polaris_onboarding",
    "0036_memory_self_healing",
    "0037_common_file_operations",
    "0038_common_git_commit_operations",
    "0039_common_git_stage_operations",
    "0040_common_command_runs",
    "0041_revocable_service_auth",
}


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_apply_all_migrations_creates_version_table_and_expected_versions() -> None:
    conn = make_conn()
    ran = db_migrations.apply_all_migrations(conn)
    versions = db_migrations.applied_migration_versions(conn)

    assert set(ran) == EXPECTED_VERSIONS
    assert versions == EXPECTED_VERSIONS


def test_apply_all_migrations_is_stable_on_second_run() -> None:
    conn = make_conn()
    first = db_migrations.apply_all_migrations(conn)
    second = db_migrations.apply_all_migrations(conn)

    assert set(first) == EXPECTED_VERSIONS
    assert second == []


def test_migrations_create_timeline_and_link_audit_columns() -> None:
    conn = make_conn()
    db_migrations.apply_all_migrations(conn)

    timeline_columns = {row["name"] for row in conn.execute("PRAGMA table_info(timeline_events)").fetchall()}
    link_columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_links)").fetchall()}
    index_names = {row[1] for row in conn.execute("PRAGMA index_list(timeline_events)").fetchall()}

    assert {
        "event_time",
        "event_type",
        "payload_json",
        "created_at",
        "operation_id",
        "timeline_scope",
        "semantic_kind",
        "title",
        "project_key",
        "valid_at",
    }.issubset(timeline_columns)
    assert {"created_at", "archived_at"}.issubset(link_columns)
    assert "idx_timeline_events_operation" in index_names
    assert "idx_timeline_events_project_key" in index_names


def test_migrations_create_sprint2_memory_metadata_columns() -> None:
    conn = make_conn()
    db_migrations.apply_all_migrations(conn)

    memory_columns = {row["name"] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    layer_rule_columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_layer_rules)").fetchall()}
    area_rule_columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_area_rules)").fetchall()}
    event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_events)").fetchall()}

    assert {
        "layer_code",
        "area_code",
        "state_code",
        "scope_code",
        "parent_memory_id",
        "version",
        "promoted_from_id",
        "demoted_from_id",
        "supersedes_memory_id",
        "valid_from",
        "valid_to",
        "decay_score",
        "emotional_weight",
        "identity_weight",
        "project_key",
        "conversation_key",
        "last_validated_at",
        "validation_source",
    }.issubset(memory_columns)
    assert {"layer_code", "description", "created_at"}.issubset(layer_rule_columns)
    assert {"area_code", "description", "created_at"}.issubset(area_rule_columns)
    assert {"memory_id", "event_type", "payload_json", "created_at"}.issubset(event_columns)


def test_migrations_create_gemma_worker_jobs_table() -> None:
    conn = make_conn()
    db_migrations.apply_all_migrations(conn)

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(gemma_worker_jobs)").fetchall()
    }
    index_names = {
        row["name"]
        for row in conn.execute("PRAGMA index_list(gemma_worker_jobs)").fetchall()
    }

    assert {
        "id",
        "status",
        "repo",
        "project_key",
        "task",
        "context",
        "allowed_actions_json",
        "acceptance_criteria_json",
        "plan_json",
        "result_json",
        "error",
        "created_at",
        "updated_at",
        "approved_at",
        "completed_at",
    }.issubset(columns)
    assert {
        "idx_gemma_worker_jobs_status",
        "idx_gemma_worker_jobs_project_key",
        "idx_gemma_worker_jobs_created_at",
        "idx_gemma_worker_jobs_updated_at",
    }.issubset(index_names)


def test_migrations_create_project_key_aliases_table() -> None:
    conn = make_conn()
    db_migrations.apply_all_migrations(conn)

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(project_key_aliases)").fetchall()
    }
    index_names = {
        row["name"]
        for row in conn.execute("PRAGMA index_list(project_key_aliases)").fetchall()
    }
    aliases = {
        row["alias_project_key"]: row["canonical_project_key"]
        for row in conn.execute("SELECT alias_project_key, canonical_project_key FROM project_key_aliases").fetchall()
    }

    assert {
        "alias_project_key",
        "canonical_project_key",
        "alias_kind",
        "status",
        "notes",
        "created_at",
        "updated_at",
    }.issubset(columns)
    assert {
        "idx_project_key_aliases_canonical",
        "idx_project_key_aliases_status",
    }.issubset(index_names)
    assert aliases["demo-project"] == "demo-project"
    assert aliases["demo"] == "demo-project"
    assert aliases["sample-research"] == "sample-research"
    assert aliases["research-demo"] == "sample-research"


def test_historical_migration_0018_creates_bridge_mailbox_tables() -> None:
    conn = make_conn()
    ran = db_migrations.apply_migrations_through(conn, "0018_bridge_mailbox")

    thread_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(bridge_threads)").fetchall()
    }
    message_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(bridge_messages)").fetchall()
    }
    message_indexes = {
        row["name"]
        for row in conn.execute("PRAGMA index_list(bridge_messages)").fetchall()
    }

    assert {
        "id",
        "project_key",
        "run_id",
        "title",
        "status",
        "created_by",
        "created_at",
        "updated_at",
        "metadata_json",
    }.issubset(thread_columns)
    assert {
        "id",
        "thread_id",
        "project_key",
        "sender",
        "recipient",
        "message_type",
        "body",
        "payload_json",
        "ref_kind",
        "ref_id",
        "priority",
        "requires_ack",
        "status",
        "created_at",
        "seen_at",
        "acked_at",
        "acked_by",
        "ack_note",
        "dedupe_key",
    }.issubset(message_columns)
    assert {
        "idx_bridge_messages_recipient_status",
        "idx_bridge_messages_thread",
        "idx_bridge_messages_project",
        "idx_bridge_messages_dedupe",
    }.issubset(message_indexes)
    assert ran[-1] == "0018_bridge_mailbox"


def test_fresh_migration_chain_retires_bridge_mailbox_tables_and_indexes() -> None:
    conn = make_conn()
    db_migrations.apply_all_migrations(conn)

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    bridge_indexes = {
        row["name"]
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='index'
              AND (name LIKE '%bridge%' OR tbl_name LIKE 'bridge_%')
            """
        ).fetchall()
    }

    assert "bridge_messages" not in tables
    assert "bridge_threads" not in tables
    assert bridge_indexes == set()
    assert {
        "memories",
        "memory_links",
        "memory_events",
        "feature_flags",
        "users",
        "workspaces",
        "workspace_memberships",
        "memory_lifecycle_snapshots",
        "memory_capture_review_items",
        "memory_retention_review_items",
        "memory_hygiene_runs",
        "sandman_scheduler_runs",
        "remote_auth_tokens",
        "remote_auth_audit_events",
    }.issubset(tables)


def test_bridge_retirement_upgrade_removes_only_bridge_tables() -> None:
    conn = make_conn()
    db_migrations.apply_migrations_through(conn, "0031_private_remote_auth")
    conn.execute(
        """
        INSERT INTO bridge_threads (
            id, title, status, created_by, created_at, updated_at
        ) VALUES ('thread-retirement-test', 'retirement test', 'open', 'codex', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO bridge_messages (
            id, thread_id, sender, recipient, message_type, body, priority,
            requires_ack, status, created_at
        ) VALUES (
            'message-retirement-test', 'thread-retirement-test', 'codex',
            'agent', 'note', 'fixture', 3, 0, 'pending', CURRENT_TIMESTAMP
        )
        """
    )
    tables_before = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    protected_counts_before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("memories", "memory_links", "memory_events", "users", "workspaces")
    }

    ran = db_migrations.apply_all_migrations(conn)

    tables_after = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    protected_counts_after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected_counts_before
    }
    assert ran == [
        "0032_retire_bridge_mailbox",
        "0033_mcp_idempotency_requests",
        "0034_recall_importance_decoupling",
        "0035_polaris_onboarding",
        "0036_memory_self_healing",
        "0037_common_file_operations",
        "0038_common_git_commit_operations",
        "0039_common_git_stage_operations",
        "0040_common_command_runs",
        "0041_revocable_service_auth",
    ]
    assert tables_after == (tables_before - {"bridge_messages", "bridge_threads"}) | {
        "mcp_idempotency_requests",
        "polaris_onboarding",
        "memory_self_healing_issues",
        "file_operations",
        "git_commit_operations",
        "git_stage_operations",
        "command_recipe_runs",
    }
    assert protected_counts_after == protected_counts_before
    assert "0018_bridge_mailbox" in db_migrations.applied_migration_versions(conn)
    assert "0032_retire_bridge_mailbox" in db_migrations.applied_migration_versions(conn)
    assert "0033_mcp_idempotency_requests" in db_migrations.applied_migration_versions(conn)
    assert "0034_recall_importance_decoupling" in db_migrations.applied_migration_versions(conn)
    assert "0035_polaris_onboarding" in db_migrations.applied_migration_versions(conn)
    assert "0036_memory_self_healing" in db_migrations.applied_migration_versions(conn)
    assert "0041_revocable_service_auth" in db_migrations.applied_migration_versions(conn)


def test_bridge_retirement_drop_is_idempotent() -> None:
    conn = make_conn()
    db_migrations.apply_migrations_through(conn, "0031_private_remote_auth")

    db_migrations._migration_0032_retire_bridge_mailbox(conn)
    db_migrations._migration_0032_retire_bridge_mailbox(conn)

    bridge_objects = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE name LIKE '%bridge%' OR tbl_name LIKE 'bridge_%'
        """
    ).fetchall()
    assert bridge_objects == []


def test_migrations_create_consolidation_apply_snapshot_table() -> None:
    conn = make_conn()
    db_migrations.apply_all_migrations(conn)

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(memory_consolidation_apply_snapshots)").fetchall()
    }
    index_names = {
        row["name"]
        for row in conn.execute("PRAGMA index_list(memory_consolidation_apply_snapshots)").fetchall()
    }

    assert {
        "id",
        "run_id",
        "proposal_memory_id",
        "schema_version",
        "preview_source",
        "preview_hash",
        "snapshot_json",
        "created_at",
    }.issubset(columns)
    assert {
        "idx_memory_consolidation_apply_snapshots_proposal",
        "idx_memory_consolidation_apply_snapshots_created_at",
    }.issubset(index_names)


def test_migrations_create_memory_v3_lifecycle_snapshots_table() -> None:
    conn = make_conn()
    db_migrations.apply_all_migrations(conn)

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(memory_lifecycle_snapshots)").fetchall()
    }
    index_names = {
        row["name"]
        for row in conn.execute("PRAGMA index_list(memory_lifecycle_snapshots)").fetchall()
    }
    foreign_keys = {
        (row["table"], row["from"], row["to"])
        for row in conn.execute("PRAGMA foreign_key_list(memory_lifecycle_snapshots)").fetchall()
    }

    assert {
        "id",
        "operation_key",
        "operation_type",
        "status",
        "new_memory_id",
        "old_memory_id",
        "relation_kind",
        "reason",
        "input_fingerprint",
        "candidate_set_fingerprint",
        "preview_hash",
        "before_snapshot_json",
        "after_snapshot_json",
        "link_snapshot_json",
        "event_snapshot_json",
        "applied_at",
        "started_at",
        "applied_by",
        "apply_note",
        "rollback_preview_hash",
        "rollback_snapshot_json",
        "rolled_back_at",
        "rolled_back_by",
        "rollback_note",
        "created_at",
        "updated_at",
    }.issubset(columns)
    assert {
        "sqlite_autoindex_memory_lifecycle_snapshots_1",
        "idx_memory_lifecycle_snapshots_status",
        "idx_memory_lifecycle_snapshots_new_memory",
        "idx_memory_lifecycle_snapshots_old_memory",
        "idx_memory_lifecycle_snapshots_created_at",
    }.issubset(index_names)
    assert {
        ("memories", "new_memory_id", "id"),
        ("memories", "old_memory_id", "id"),
    }.issubset(foreign_keys)


def test_migrations_create_memory_v3_capture_review_queue_table_and_flag() -> None:
    conn = make_conn()
    db_migrations.apply_all_migrations(conn)

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(memory_capture_review_items)").fetchall()
    }
    index_names = {
        row["name"]
        for row in conn.execute("PRAGMA index_list(memory_capture_review_items)").fetchall()
    }
    foreign_keys = {
        (row["table"], row["from"], row["to"])
        for row in conn.execute("PRAGMA foreign_key_list(memory_capture_review_items)").fetchall()
    }
    flag = conn.execute(
        "SELECT flag_key, is_enabled, rollout_mode FROM feature_flags WHERE flag_key = 'memory_v3_capture_reconciliation_enabled'"
    ).fetchone()

    assert {
        "id",
        "proposal_key",
        "status",
        "proposal_json",
        "input_fingerprint",
        "project_key",
        "scope_code",
        "conversation_key",
        "source_context",
        "source_event_ref",
        "recommended_action",
        "matched_memory_ids_json",
        "reconciliation_json",
        "candidate_set_fingerprint",
        "reconciliation_preview_hash",
        "created_memory_id",
        "reviewed_at",
        "reviewed_by",
        "review_note",
        "expires_at",
        "created_at",
        "updated_at",
    }.issubset(columns)
    assert {
        "sqlite_autoindex_memory_capture_review_items_1",
        "idx_memory_capture_review_items_status",
        "idx_memory_capture_review_items_project_key",
        "idx_memory_capture_review_items_created_at",
        "idx_memory_capture_review_items_input_fingerprint",
    }.issubset(index_names)
    assert {("memories", "created_memory_id", "id")}.issubset(foreign_keys)
    assert flag["flag_key"] == "memory_v3_capture_reconciliation_enabled"
    assert int(flag["is_enabled"]) == 0
    assert flag["rollout_mode"] == "off"


def test_migrations_create_consolidation_rollback_snapshot_table() -> None:
    conn = make_conn()
    db_migrations.apply_all_migrations(conn)

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(memory_consolidation_rollback_snapshots)").fetchall()
    }
    index_names = {
        row["name"]
        for row in conn.execute("PRAGMA index_list(memory_consolidation_rollback_snapshots)").fetchall()
    }

    assert {
        "id",
        "original_apply_run_id",
        "rollback_run_id",
        "schema_version",
        "preview_source",
        "rollback_preview_hash",
        "snapshot_json",
        "created_at",
    }.issubset(columns)
    assert {
        "idx_memory_consolidation_rollback_snapshots_rollback_run",
        "idx_memory_consolidation_rollback_snapshots_created_at",
    }.issubset(index_names)


def test_migrations_can_stamp_existing_legacy_schema_without_breaking() -> None:
    conn = make_conn()
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            summary_short TEXT,
            memory_type TEXT NOT NULL,
            source TEXT,
            importance_score REAL DEFAULT 0.5,
            confidence_score REAL DEFAULT 0.5,
            tags TEXT,
            recall_count INTEGER DEFAULT 0,
            last_recalled_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE memory_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_memory_id INTEGER NOT NULL,
            to_memory_id INTEGER NOT NULL,
            relation_type TEXT NOT NULL,
            weight REAL NOT NULL,
            origin TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE sleep_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'started',
            mode TEXT NOT NULL DEFAULT 'preview',
            freedom_level INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            scanned_count INTEGER NOT NULL DEFAULT 0,
            changed_count INTEGER NOT NULL DEFAULT 0,
            archived_count INTEGER NOT NULL DEFAULT 0,
            downgraded_count INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            conflict_count INTEGER NOT NULL DEFAULT 0,
            created_summary_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE sleep_run_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            memory_id INTEGER,
            action_type TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    ran = db_migrations.apply_all_migrations(conn)
    versions = db_migrations.applied_migration_versions(conn)
    memory_columns = {row["name"] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    timeline_columns = {row["name"] for row in conn.execute("PRAGMA table_info(timeline_events)").fetchall()}

    assert set(ran) == EXPECTED_VERSIONS
    assert versions == EXPECTED_VERSIONS
    assert {
        "created_at",
        "last_accessed_at",
        "activity_state",
        "evidence_count",
        "contradiction_flag",
        "archived_at",
        "sandman_note",
        "layer_code",
        "area_code",
        "state_code",
        "scope_code",
        "version",
        "project_key",
        "conversation_key",
    }.issubset(memory_columns)
    assert {"operation_id", "timeline_scope", "semantic_kind", "title", "project_key", "valid_at"}.issubset(timeline_columns)
