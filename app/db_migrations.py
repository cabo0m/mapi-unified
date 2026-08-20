from __future__ import annotations

import sqlite3
from typing import Callable

from app import timeline
from mapi_core.memory.project_keys import ensure_project_key_alias_schema, seed_default_project_key_aliases

MigrationFn = Callable[[sqlite3.Connection], None]

MIGRATION_SEQUENCE: list[tuple[str, MigrationFn]] = []


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_sql: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if column_name not in existing:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")


def ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def applied_migration_versions(conn: sqlite3.Connection) -> set[str]:
    ensure_schema_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC").fetchall()
    return {str(row["version"]) for row in rows}


def _migration_0001_memory_core(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
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
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_memory_id INTEGER NOT NULL,
            to_memory_id INTEGER NOT NULL,
            relation_type TEXT NOT NULL,
            weight REAL NOT NULL,
            origin TEXT,
            FOREIGN KEY (from_memory_id) REFERENCES memories(id),
            FOREIGN KEY (to_memory_id) REFERENCES memories(id)
        )
        """
    )
    _ensure_column(conn, "memories", "created_at", "created_at TEXT")
    _ensure_column(conn, "memories", "last_accessed_at", "last_accessed_at TEXT")
    _ensure_column(conn, "memories", "activity_state", "activity_state TEXT NOT NULL DEFAULT 'active'")
    _ensure_column(conn, "memories", "evidence_count", "evidence_count INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "memories", "contradiction_flag", "contradiction_flag INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "memories", "archived_at", "archived_at TEXT")
    _ensure_column(conn, "memories", "sandman_note", "sandman_note TEXT")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sleep_runs (
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
            created_summary_count INTEGER NOT NULL DEFAULT 0,
            rollback_of_run_id INTEGER,
            FOREIGN KEY (rollback_of_run_id) REFERENCES sleep_runs(id)
        )
        """
    )
    _ensure_column(conn, "sleep_runs", "rollback_of_run_id", "rollback_of_run_id INTEGER")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sleep_run_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            memory_id INTEGER,
            action_type TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES sleep_runs(id)
        )
        """
    )

    cursor.execute("UPDATE memories SET created_at = COALESCE(created_at, last_recalled_at, CURRENT_TIMESTAMP) WHERE created_at IS NULL")
    cursor.execute("UPDATE memories SET last_accessed_at = COALESCE(last_accessed_at, last_recalled_at, created_at, CURRENT_TIMESTAMP) WHERE last_accessed_at IS NULL")
    cursor.execute("UPDATE memories SET activity_state = 'active' WHERE activity_state IS NULL OR trim(activity_state) = ''")
    cursor.execute("UPDATE memories SET evidence_count = 1 WHERE evidence_count IS NULL OR evidence_count < 1")
    cursor.execute("UPDATE memories SET contradiction_flag = 0 WHERE contradiction_flag IS NULL")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_activity_state ON memories(activity_state)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_memory_type ON memories(memory_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_importance_score ON memories(importance_score)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_last_accessed_at ON memories(last_accessed_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_links_from_memory_id ON memory_links(from_memory_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_links_to_memory_id ON memory_links(to_memory_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_links_relation_type ON memory_links(relation_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sleep_runs_started_at ON sleep_runs(started_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sleep_runs_status ON sleep_runs(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sleep_runs_rollback_of_run_id ON sleep_runs(rollback_of_run_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sleep_run_actions_run_id ON sleep_run_actions(run_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sleep_run_actions_memory_id ON sleep_run_actions(memory_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sleep_run_actions_action_type ON sleep_run_actions(action_type)")


def _migration_0002_timeline_schema(conn: sqlite3.Connection) -> None:
    timeline.ensure_timeline_schema(conn)


def _migration_0003_timeline_schema_hardening(conn: sqlite3.Connection) -> None:
    timeline.ensure_timeline_schema(conn)


def _migration_0004_project_timeline_semantics(conn: sqlite3.Connection) -> None:
    timeline.ensure_timeline_schema(conn)


def _migration_0005_memory_layer_area_metadata(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    for column_name, column_sql in (
        ("layer_code", "layer_code TEXT"),
        ("area_code", "area_code TEXT"),
        ("state_code", "state_code TEXT"),
        ("scope_code", "scope_code TEXT"),
        ("parent_memory_id", "parent_memory_id INTEGER"),
        ("version", "version INTEGER NOT NULL DEFAULT 1"),
        ("promoted_from_id", "promoted_from_id INTEGER"),
        ("demoted_from_id", "demoted_from_id INTEGER"),
        ("supersedes_memory_id", "supersedes_memory_id INTEGER"),
        ("valid_from", "valid_from TEXT"),
        ("valid_to", "valid_to TEXT"),
        ("decay_score", "decay_score REAL NOT NULL DEFAULT 0.0"),
        ("emotional_weight", "emotional_weight REAL NOT NULL DEFAULT 0.0"),
        ("identity_weight", "identity_weight REAL NOT NULL DEFAULT 0.0"),
        ("project_key", "project_key TEXT"),
        ("conversation_key", "conversation_key TEXT"),
        ("last_validated_at", "last_validated_at TEXT"),
        ("validation_source", "validation_source TEXT"),
    ):
        _ensure_column(conn, "memories", column_name, column_sql)


    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_layer_rules (
            layer_code TEXT PRIMARY KEY,
            description TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_area_rules (
            area_code TEXT PRIMARY KEY,
            description TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER,
            event_type TEXT NOT NULL,
            payload_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (memory_id) REFERENCES memories(id)
        )
        """
    )

    cursor.executemany(
        "INSERT OR IGNORE INTO memory_layer_rules (layer_code, description) VALUES (?, ?)",
        [
            ("core", "Most protected memory layer."),
            ("identity", "Identity and stable traits."),
            ("autobio", "Autobiographic knowledge and durable facts."),
            ("projects", "Active projects and project decisions."),
            ("working", "Current working context."),
            ("buffer", "Temporary buffer and draft memories."),
        ],
    )
    cursor.executemany(
        "INSERT OR IGNORE INTO memory_area_rules (area_code, description) VALUES (?, ?)",
        [
            ("identity", "Who the system is."),
            ("relation", "User relationship."),
            ("projects", "Project context."),
            ("knowledge", "Facts and knowledge."),
            ("preferences", "Preferences and work style."),
            ("history", "History and milestones."),
            ("rumination", "Hypotheses and drafts."),
            ("meta", "Memory system rules."),
        ],
    )
    cursor.execute("UPDATE memories SET version = 1 WHERE version IS NULL OR version < 1")
    cursor.execute("UPDATE memories SET decay_score = 0.0 WHERE decay_score IS NULL")
    cursor.execute("UPDATE memories SET emotional_weight = 0.0 WHERE emotional_weight IS NULL")
    cursor.execute("UPDATE memories SET identity_weight = 0.0 WHERE identity_weight IS NULL")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_layer_code ON memories(layer_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_area_code ON memories(area_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_state_code ON memories(state_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_scope_code ON memories(scope_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_project_key ON memories(project_key)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_conversation_key ON memories(conversation_key)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_events_memory_id ON memory_events(memory_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_events_event_type ON memory_events(event_type)")


def _migration_0006_feature_flags(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS feature_flags (
            flag_key TEXT PRIMARY KEY,
            is_enabled INTEGER NOT NULL DEFAULT 1,
            rollout_mode TEXT NOT NULL DEFAULT 'all',
            allowed_project_keys TEXT,
            allowed_scope_codes TEXT,
            read_only_mode INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        INSERT OR IGNORE INTO feature_flags (
            flag_key,
            is_enabled,
            rollout_mode,
            allowed_project_keys,
            allowed_scope_codes,
            read_only_mode,
            notes
        )
        VALUES (?, 1, 'all', NULL, NULL, 0, ?)
        """,
        (
            "cross_project_knowledge_layer",
            "Default rollout for Cross-Project Knowledge Layer",
        ),
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_feature_flags_rollout_mode ON feature_flags(rollout_mode)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_feature_flags_is_enabled ON feature_flags(is_enabled)")



def _migration_0007_ownership_sla(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    for column_name, column_sql in (
        ("owner_role", "owner_role TEXT"),
        ("owner_id", "owner_id TEXT"),
        ("review_due_at", "review_due_at TEXT"),
        ("revalidation_due_at", "revalidation_due_at TEXT"),
    ):
        _ensure_column(conn, "memories", column_name, column_sql)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_owner_role ON memories(owner_role)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_owner_id ON memories(owner_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_review_due_at ON memories(review_due_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_revalidation_due_at ON memories(revalidation_due_at)")


def _migration_0008_expired_duplicate_sla(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    _ensure_column(conn, "memories", "expired_due_at", "expired_due_at TEXT")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS duplicate_review_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_memory_id INTEGER NOT NULL,
            duplicate_memory_id INTEGER NOT NULL,
            owner_role TEXT,
            owner_id TEXT,
            duplicate_due_at TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(canonical_memory_id, duplicate_memory_id),
            FOREIGN KEY (canonical_memory_id) REFERENCES memories(id),
            FOREIGN KEY (duplicate_memory_id) REFERENCES memories(id)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_duplicate_review_items_due_at ON duplicate_review_items(duplicate_due_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_duplicate_review_items_owner_role ON duplicate_review_items(owner_role)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_duplicate_review_items_status ON duplicate_review_items(status)")


def _migration_0009_owner_resolution_layer(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS owner_directory_items (
            owner_key TEXT PRIMARY KEY,
            owner_type TEXT NOT NULL,
            display_name TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            routing_metadata_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS owner_role_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_role TEXT NOT NULL,
            owner_key TEXT NOT NULL,
            project_key TEXT,
            scope_code TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(owner_role, project_key, scope_code),
            FOREIGN KEY (owner_key) REFERENCES owner_directory_items(owner_key)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_owner_directory_items_owner_type ON owner_directory_items(owner_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_owner_directory_items_is_active ON owner_directory_items(is_active)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_owner_role_mappings_owner_role ON owner_role_mappings(owner_role)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_owner_role_mappings_project_key ON owner_role_mappings(project_key)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_owner_role_mappings_scope_code ON owner_role_mappings(scope_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_owner_role_mappings_is_active ON owner_role_mappings(is_active)")

    directory_seed = [
        ("maintainer", "team", "Memory Maintainer"),
        ("knowledge_curator", "team", "Knowledge Curator"),
        ("review_team", "team", "Review Team"),
        ("project_maintainer", "team", "Project Maintainer"),
    ]
    cursor.executemany(
        "INSERT OR IGNORE INTO owner_directory_items (owner_key, owner_type, display_name, is_active) VALUES (?, ?, ?, 1)",
        directory_seed,
    )
    mapping_seed = [
        ("maintainer", "maintainer", None, None, "Bootstrap global mapping"),
        ("knowledge_curator", "knowledge_curator", None, None, "Bootstrap global mapping"),
        ("review_team", "review_team", None, None, "Bootstrap global mapping"),
        ("project_maintainer", "project_maintainer", None, None, "Bootstrap global mapping"),
    ]
    cursor.executemany(
        "INSERT OR IGNORE INTO owner_role_mappings (owner_role, owner_key, project_key, scope_code, is_active, notes) VALUES (?, ?, ?, ?, 1, ?)",
        mapping_seed,
    )


def _migration_0010_multiuser_identity_foundation(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()

    # 1. Tabela users
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_user_key TEXT NOT NULL UNIQUE,
            display_name TEXT,
            email TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT
        )
        """
    )

    # 2. Tabela workspaces
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 3. Tabela workspace_memberships
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_memberships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_code TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            invited_by_user_id INTEGER,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (invited_by_user_id) REFERENCES users(id),
            UNIQUE (workspace_id, user_id)
        )
        """
    )

    # 4. Nowe kolumny w memories
    for col, col_sql in [
        ("owner_user_id", "owner_user_id INTEGER"),
        ("workspace_id", "workspace_id INTEGER"),
        ("visibility_scope", "visibility_scope TEXT NOT NULL DEFAULT 'private'"),
        ("access_role_min", "access_role_min TEXT"),
        ("created_by_user_id", "created_by_user_id INTEGER"),
        ("last_modified_by_user_id", "last_modified_by_user_id INTEGER"),
        ("subject_user_id", "subject_user_id INTEGER"),
        ("sharing_policy", "sharing_policy TEXT NOT NULL DEFAULT 'explicit'"),
    ]:
        _ensure_column(conn, "memories", col, col_sql)

    # 5. Nowe kolumny w memory_links
    for col, col_sql in [
        ("workspace_id", "workspace_id INTEGER"),
        ("visibility_scope", "visibility_scope TEXT NOT NULL DEFAULT 'inherited'"),
        ("created_by_user_id", "created_by_user_id INTEGER"),
    ]:
        _ensure_column(conn, "memory_links", col, col_sql)

    # 6. Nowe kolumny w timeline_events
    for col, col_sql in [
        ("actor_user_id", "actor_user_id INTEGER"),
        ("workspace_id", "workspace_id INTEGER"),
        ("actor_type", "actor_type TEXT NOT NULL DEFAULT 'system'"),
    ]:
        _ensure_column(conn, "timeline_events", col, col_sql)

    # 7. Seed: default workspace
    cursor.execute(
        """
        INSERT INTO workspaces (workspace_key, name)
        SELECT 'default', 'Default Workspace'
        WHERE NOT EXISTS (SELECT 1 FROM workspaces WHERE workspace_key = 'default')
        """
    )

    # 8. Seed: system:legacy user
    cursor.execute(
        """
        INSERT INTO users (external_user_key, display_name, status)
        SELECT 'system:legacy', 'Legacy System User', 'active'
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE external_user_key = 'system:legacy')
        """
    )

    # 9. Seed: użytkownicy legacy z owner_id
    cursor.execute(
        """
        INSERT INTO users (external_user_key, display_name, status)
        SELECT DISTINCT
            'legacy:' || owner_id,
            owner_id,
            'active'
        FROM memories
        WHERE owner_id IS NOT NULL
          AND TRIM(owner_id) <> ''
          AND owner_role = 'user'
          AND NOT EXISTS (
              SELECT 1 FROM users u WHERE u.external_user_key = 'legacy:' || memories.owner_id
          )
        """
    )

    # 10. Membership: system:legacy jako owner default workspace
    cursor.execute(
        """
        INSERT INTO workspace_memberships (workspace_id, user_id, role_code, status)
        SELECT w.id, u.id, 'owner', 'active'
        FROM workspaces w
        JOIN users u ON u.external_user_key = 'system:legacy'
        WHERE w.workspace_key = 'default'
          AND NOT EXISTS (
              SELECT 1 FROM workspace_memberships wm
              WHERE wm.workspace_id = w.id AND wm.user_id = u.id
          )
        """
    )

    # 11. Membership: legacy userzy jako editor default workspace
    cursor.execute(
        """
        INSERT INTO workspace_memberships (workspace_id, user_id, role_code, status)
        SELECT w.id, u.id, 'editor', 'active'
        FROM workspaces w
        JOIN users u ON u.external_user_key LIKE 'legacy:%'
        WHERE w.workspace_key = 'default'
          AND NOT EXISTS (
              SELECT 1 FROM workspace_memberships wm
              WHERE wm.workspace_id = w.id AND wm.user_id = u.id
          )
        """
    )

    # 12. Backfill: workspace_id w memories
    cursor.execute(
        """
        UPDATE memories
        SET workspace_id = (SELECT id FROM workspaces WHERE workspace_key = 'default')
        WHERE workspace_id IS NULL
        """
    )

    # 13A. Backfill: owner_user_id z owner_id
    cursor.execute(
        """
        UPDATE memories
        SET owner_user_id = (
            SELECT u.id FROM users u
            WHERE u.external_user_key = 'legacy:' || memories.owner_id
        )
        WHERE owner_user_id IS NULL
          AND owner_role = 'user'
          AND owner_id IS NOT NULL
          AND TRIM(owner_id) <> ''
        """
    )

    # 13B. Fallback owner_user_id dla typów osobistych
    cursor.execute(
        """
        UPDATE memories
        SET owner_user_id = (SELECT id FROM users WHERE external_user_key = 'system:legacy')
        WHERE owner_user_id IS NULL
          AND memory_type IN (
              'preference', 'interaction_preference', 'workflow_preference',
              'profile', 'profile_note', 'personal_note', 'working'
          )
        """
    )

    # 14A. Backfill visibility_scope: project dla typów projektowych z project_key
    cursor.execute(
        """
        UPDATE memories
        SET visibility_scope = 'project'
        WHERE project_key IS NOT NULL
          AND TRIM(project_key) <> ''
          AND memory_type IN (
              'project', 'project_note', 'project_context', 'project_direction',
              'project_design', 'project_architecture', 'project_milestone'
          )
        """
    )

    # 14B. Backfill visibility_scope: workspace dla fact/summary bez project_key
    cursor.execute(
        """
        UPDATE memories
        SET visibility_scope = 'workspace'
        WHERE visibility_scope = 'private'
          AND memory_type IN ('fact', 'consolidated_summary')
          AND (project_key IS NULL OR TRIM(project_key) = '')
        """
    )

    # 14C. Backfill visibility_scope: private dla typów osobistych
    cursor.execute(
        """
        UPDATE memories
        SET visibility_scope = 'private'
        WHERE memory_type IN (
              'preference', 'interaction_preference', 'workflow_preference',
              'profile', 'profile_note', 'personal_note', 'interest', 'working'
          )
        """
    )

    # 14D. Fallback: project dla rekordów z project_key
    cursor.execute(
        """
        UPDATE memories
        SET visibility_scope = 'project'
        WHERE visibility_scope = 'private'
          AND project_key IS NOT NULL
          AND TRIM(project_key) <> ''
        """
    )

    # 15. Backfill created_by_user_id i last_modified_by_user_id
    cursor.execute(
        """
        UPDATE memories
        SET created_by_user_id = COALESCE(
            owner_user_id,
            (SELECT id FROM users WHERE external_user_key = 'system:legacy')
        )
        WHERE created_by_user_id IS NULL
        """
    )
    cursor.execute(
        """
        UPDATE memories
        SET last_modified_by_user_id = COALESCE(
            owner_user_id,
            created_by_user_id,
            (SELECT id FROM users WHERE external_user_key = 'system:legacy')
        )
        WHERE last_modified_by_user_id IS NULL
        """
    )

    # 16. Backfill memory_links: workspace_id i visibility_scope
    cursor.execute(
        """
        UPDATE memory_links
        SET workspace_id = (
            SELECT m.workspace_id FROM memories m WHERE m.id = memory_links.from_memory_id
        )
        WHERE workspace_id IS NULL
        """
    )
    cursor.execute(
        """
        UPDATE memory_links
        SET visibility_scope = (
            SELECT CASE
                WHEN m1.visibility_scope = m2.visibility_scope THEN m1.visibility_scope
                ELSE 'inherited'
            END
            FROM memories m1
            JOIN memories m2 ON m2.id = memory_links.to_memory_id
            WHERE m1.id = memory_links.from_memory_id
        )
        WHERE visibility_scope = 'inherited'
        """
    )

    # 17. Backfill timeline_events: workspace_id i actor_user_id
    cursor.execute(
        """
        UPDATE timeline_events
        SET workspace_id = (SELECT id FROM workspaces WHERE workspace_key = 'default')
        WHERE workspace_id IS NULL
        """
    )
    cursor.execute(
        """
        UPDATE timeline_events
        SET actor_user_id = (SELECT id FROM users WHERE external_user_key = 'system:legacy')
        WHERE actor_user_id IS NULL
        """
    )

    # 18. Indeksy
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_workspace_memberships_workspace ON workspace_memberships(workspace_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_workspace_memberships_user ON workspace_memberships(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_workspace_memberships_role ON workspace_memberships(role_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_workspace_id ON memories(workspace_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_owner_user_id ON memories(owner_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_visibility_scope ON memories(visibility_scope)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_workspace_scope ON memories(workspace_id, visibility_scope)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_workspace_project_scope ON memories(workspace_id, project_key, visibility_scope)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_subject_user_id ON memories(subject_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_created_by_user_id ON memories(created_by_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_links_workspace_id ON memory_links(workspace_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_links_visibility_scope ON memory_links(visibility_scope)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_timeline_events_workspace_id ON timeline_events(workspace_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_timeline_events_actor_user_id ON timeline_events(actor_user_id)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_timeline_events_workspace_event_time ON timeline_events(workspace_id, event_time)"
    )

    # 19. Feature flags dla multiuser
    for flag_key, notes in [
        ("multiuser_identity_enabled", "Controls multi-user identity foundation (workspace, visibility_scope)"),
        ("multiuser_scope_retrieval_enabled", "Controls scope-aware memory retrieval filtering"),
        ("multiuser_timeline_actor_enabled", "Controls actor/workspace logging in timeline events"),
    ]:
        cursor.execute(
            "INSERT OR IGNORE INTO feature_flags (flag_key, is_enabled, rollout_mode, notes) VALUES (?, 1, 'all', ?)",
            (flag_key, notes),
        )


def _migration_0011_scope_aware_maintenance(conn: sqlite3.Connection) -> None:
    """Faza 3 + Faza 4: scope-aware maintenance i scope promotion governance."""
    cursor = conn.cursor()

    # --- Faza 3: sleep_runs scope context ---
    _ensure_column(conn, "sleep_runs", "workspace_id", "workspace_id INTEGER")
    _ensure_column(conn, "sleep_runs", "project_key", "project_key TEXT")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sleep_runs_workspace_id ON sleep_runs(workspace_id)")

    # --- Faza 4: scope promotion governance ---
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS scope_promotion_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL,
            proposed_by_user_id INTEGER,
            current_scope TEXT NOT NULL,
            target_scope TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            workspace_id INTEGER,
            project_key TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT,
            reviewed_by_user_id INTEGER,
            review_note TEXT,
            FOREIGN KEY (memory_id) REFERENCES memories(id),
            FOREIGN KEY (proposed_by_user_id) REFERENCES users(id),
            FOREIGN KEY (reviewed_by_user_id) REFERENCES users(id)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_scope_proposals_memory_id ON scope_promotion_proposals(memory_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_scope_proposals_status ON scope_promotion_proposals(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_scope_proposals_workspace_id ON scope_promotion_proposals(workspace_id)")

    # Feature flags
    for flag_key, notes in [
        ("multiuser_scope_maintenance_enabled", "Controls workspace-scoped Sandman runs (Faza 3)"),
        ("multiuser_scope_promotion_enabled", "Controls scope promotion governance workflow (Faza 4)"),
    ]:
        cursor.execute(
            "INSERT OR IGNORE INTO feature_flags (flag_key, is_enabled, rollout_mode, notes) VALUES (?, 1, 'all', ?)",
            (flag_key, notes),
        )


def _migration_0012_priority_and_sla_policies(conn: sqlite3.Connection) -> None:
    """Epic 4: priority model na memories/duplicate_review_items + tabela polityk SLA."""
    cursor = conn.cursor()
    _ensure_column(conn, "memories", "priority", "priority TEXT NOT NULL DEFAULT 'normal'")
    _ensure_column(conn, "duplicate_review_items", "priority", "priority TEXT NOT NULL DEFAULT 'normal'")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sla_policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_type TEXT NOT NULL,
            sla_days INTEGER NOT NULL,
            priority TEXT,
            memory_type TEXT,
            scope_code TEXT,
            project_key TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sla_policies_queue_type ON sla_policies(queue_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sla_policies_is_active ON sla_policies(is_active)")


def _migration_0013_escalation_history(conn: sqlite3.Connection) -> None:
    """Epic 3: tabela historii eskalacji dla overdue items."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS escalation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            escalation_level INTEGER NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            owner_role TEXT,
            project_key TEXT,
            scope_code TEXT,
            reason TEXT NOT NULL,
            days_overdue INTEGER,
            priority TEXT,
            escalated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            resolved_by TEXT,
            UNIQUE(entity_type, entity_id, escalation_level, reason)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_escalation_history_entity ON escalation_history(entity_type, entity_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_escalation_history_level ON escalation_history(escalation_level)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_escalation_history_resolved ON escalation_history(resolved_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_escalation_history_escalated_at ON escalation_history(escalated_at)")


def _migration_0014_research_ingest_quarantine(conn: sqlite3.Connection) -> None:
    """Research ingest MVP: quarantine + evidence pipeline, isolated from normal memories."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_ref TEXT NOT NULL UNIQUE,
            source_type TEXT NOT NULL,
            title TEXT,
            reliability_score REAL NOT NULL DEFAULT 0.5,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER,
            source_ref TEXT,
            source_type TEXT NOT NULL,
            title TEXT,
            raw_text TEXT NOT NULL,
            normalized_text TEXT,
            extracted_claims_json TEXT,
            project_key TEXT,
            tags TEXT,
            ingest_status TEXT NOT NULL DEFAULT 'new',
            quality_score REAL NOT NULL DEFAULT 0.5,
            source_reliability_score REAL NOT NULL DEFAULT 0.5,
            duplicate_of_ingest_id INTEGER,
            promoted_memory_id INTEGER,
            rejection_reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT,
            reviewed_by TEXT,
            FOREIGN KEY (source_id) REFERENCES ingest_sources(id),
            FOREIGN KEY (duplicate_of_ingest_id) REFERENCES ingest_items(id),
            FOREIGN KEY (promoted_memory_id) REFERENCES memories(id)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ingest_sources_source_type ON ingest_sources(source_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ingest_items_status ON ingest_items(ingest_status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ingest_items_project_key ON ingest_items(project_key)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ingest_items_source_type ON ingest_items(source_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ingest_items_created_at ON ingest_items(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ingest_items_promoted_memory_id ON ingest_items(promoted_memory_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ingest_items_duplicate_of ON ingest_items(duplicate_of_ingest_id)")
    cursor.execute(
        """
        INSERT OR IGNORE INTO feature_flags (flag_key, is_enabled, rollout_mode, notes)
        VALUES ('research_ingest_enabled', 1, 'all', 'Controls research ingest quarantine + evidence pipeline MVP')
        """
    )


def _migration_0015_conversation_archive(conn: sqlite3.Connection) -> None:
    """Conversation archive table + FTS5 verbatim search over memories and conversations."""
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL UNIQUE,
            title TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            content TEXT NOT NULL,
            project_key TEXT,
            workspace_key TEXT NOT NULL DEFAULT 'default',
            user_key TEXT NOT NULL DEFAULT 'owner',
            tags TEXT,
            word_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_conv_archives_conversation_id ON conversation_archives(conversation_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_conv_archives_project_key ON conversation_archives(project_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_conv_archives_user_key ON conversation_archives(user_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_conv_archives_archived_at ON conversation_archives(archived_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_conv_archives_source ON conversation_archives(source)"
    )

    # FTS5 for memories verbatim search
    cursor.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
        USING fts5(content, summary_short, tags, content=memories, content_rowid=id)
        """
    )
    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, summary_short, tags)
            VALUES (new.id, new.content, COALESCE(new.summary_short, ''), COALESCE(new.tags, ''));
        END
        """
    )
    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS memories_fts_delete BEFORE DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, summary_short, tags)
            VALUES ('delete', old.id, old.content, COALESCE(old.summary_short, ''), COALESCE(old.tags, ''));
        END
        """
    )
    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, summary_short, tags)
            VALUES ('delete', old.id, old.content, COALESCE(old.summary_short, ''), COALESCE(old.tags, ''));
            INSERT INTO memories_fts(rowid, content, summary_short, tags)
            VALUES (new.id, new.content, COALESCE(new.summary_short, ''), COALESCE(new.tags, ''));
        END
        """
    )
    # Backfill existing active memories into FTS
    cursor.execute(
        """
        INSERT INTO memories_fts(rowid, content, summary_short, tags)
        SELECT id, content, COALESCE(summary_short, ''), COALESCE(tags, '')
        FROM memories
        WHERE archived_at IS NULL
        """
    )

    # FTS5 for conversation_archives verbatim search
    cursor.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts
        USING fts5(title, content, tags, content=conversation_archives, content_rowid=id)
        """
    )
    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS conversations_fts_insert AFTER INSERT ON conversation_archives BEGIN
            INSERT INTO conversations_fts(rowid, title, content, tags)
            VALUES (new.id, COALESCE(new.title, ''), new.content, COALESCE(new.tags, ''));
        END
        """
    )
    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS conversations_fts_delete BEFORE DELETE ON conversation_archives BEGIN
            INSERT INTO conversations_fts(conversations_fts, rowid, title, content, tags)
            VALUES ('delete', old.id, COALESCE(old.title, ''), old.content, COALESCE(old.tags, ''));
        END
        """
    )
    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS conversations_fts_update AFTER UPDATE ON conversation_archives BEGIN
            INSERT INTO conversations_fts(conversations_fts, rowid, title, content, tags)
            VALUES ('delete', old.id, COALESCE(old.title, ''), old.content, COALESCE(old.tags, ''));
            INSERT INTO conversations_fts(rowid, title, content, tags)
            VALUES (new.id, COALESCE(new.title, ''), new.content, COALESCE(new.tags, ''));
        END
        """
    )


def _migration_0016_gemma_worker_jobs(conn: sqlite3.Connection) -> None:
    """Gemma Worker job storage for staged planning and execution."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS gemma_worker_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            repo TEXT NOT NULL,
            project_key TEXT,
            task TEXT NOT NULL,
            context TEXT,
            allowed_actions_json TEXT NOT NULL,
            acceptance_criteria_json TEXT NOT NULL,
            plan_json TEXT,
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            approved_at TEXT,
            completed_at TEXT
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_gemma_worker_jobs_status ON gemma_worker_jobs(status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_gemma_worker_jobs_project_key ON gemma_worker_jobs(project_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_gemma_worker_jobs_created_at ON gemma_worker_jobs(created_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_gemma_worker_jobs_updated_at ON gemma_worker_jobs(updated_at)"
    )


def _migration_0017_project_key_aliases(conn: sqlite3.Connection) -> None:
    """Project key alias registry for explicit project-family retrieval."""
    ensure_project_key_alias_schema(conn)
    seed_default_project_key_aliases(conn)


def _migration_0018_bridge_mailbox(conn: sqlite3.Connection) -> None:
    """Bridge Mailbox storage for local agent-to-agent messages."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS bridge_threads (
            id TEXT PRIMARY KEY,
            project_key TEXT NOT NULL DEFAULT 'demo-project',
            run_id TEXT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS bridge_messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            project_key TEXT NOT NULL DEFAULT 'demo-project',
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            message_type TEXT NOT NULL DEFAULT 'note',
            body TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            ref_kind TEXT,
            ref_id TEXT,
            priority INTEGER NOT NULL DEFAULT 3,
            requires_ack INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            seen_at TEXT,
            acked_at TEXT,
            acked_by TEXT,
            ack_note TEXT,
            dedupe_key TEXT,
            FOREIGN KEY(thread_id) REFERENCES bridge_threads(id)
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bridge_messages_recipient_status
        ON bridge_messages(recipient, status, created_at)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bridge_messages_thread
        ON bridge_messages(thread_id, created_at)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bridge_messages_project
        ON bridge_messages(project_key, created_at)
        """
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bridge_messages_dedupe
        ON bridge_messages(dedupe_key)
        WHERE dedupe_key IS NOT NULL
        """
    )


def _migration_0019_memory_entry_v2_foundation(conn: sqlite3.Connection) -> None:
    """MemoryEntry v2 foundation on top of the existing memories table."""
    cursor = conn.cursor()
    for column_name, column_sql in (
        ("schema_version", "schema_version INTEGER NOT NULL DEFAULT 1"),
        ("entry_type", "entry_type TEXT"),
        ("truth_kind", "truth_kind TEXT"),
        ("title", "title TEXT"),
        ("source_context", "source_context TEXT"),
        ("source_event_ref", "source_event_ref TEXT"),
        ("updated_at", "updated_at TEXT"),
        ("last_confirmed_at", "last_confirmed_at TEXT"),
        ("memory_v2_status", "memory_v2_status TEXT"),
        ("importance_level", "importance_level TEXT"),
        ("requires_user_confirmation", "requires_user_confirmation INTEGER NOT NULL DEFAULT 0"),
        ("should_resurface_when_json", "should_resurface_when_json TEXT"),
        ("superseded_by_memory_id", "superseded_by_memory_id INTEGER"),
    ):
        _ensure_column(conn, "memories", column_name, column_sql)

    cursor.execute(
        """
        UPDATE memories
        SET schema_version = COALESCE(schema_version, 1)
        """
    )
    cursor.execute(
        """
        UPDATE memories
        SET title = COALESCE(NULLIF(title, ''), NULLIF(summary_short, ''), memory_type)
        WHERE title IS NULL OR trim(title) = ''
        """
    )
    cursor.execute(
        """
        UPDATE memories
        SET updated_at = COALESCE(updated_at, last_validated_at, created_at, CURRENT_TIMESTAMP)
        WHERE updated_at IS NULL
        """
    )
    cursor.execute(
        """
        UPDATE memories
        SET last_confirmed_at = COALESCE(last_confirmed_at, last_validated_at)
        WHERE last_confirmed_at IS NULL
        """
    )
    cursor.execute(
        """
        UPDATE memories
        SET entry_type = CASE
            WHEN entry_type IS NOT NULL AND trim(entry_type) != '' THEN entry_type
            WHEN memory_type = 'dream' OR area_code = 'sandman' THEN 'dream'
            WHEN memory_type LIKE '%decision%' OR layer_code = 'core' THEN CASE WHEN memory_type LIKE '%decision%' THEN 'decision' ELSE 'core' END
            WHEN memory_type LIKE '%incident%' OR memory_type LIKE '%bug%' OR memory_type LIKE '%error%' THEN 'incident'
            WHEN memory_type LIKE '%experiment%' OR memory_type LIKE '%hypothesis%' THEN 'experiment'
            WHEN memory_type LIKE '%conflict%' THEN 'conflict'
            WHEN memory_type LIKE '%question%' THEN 'open_question'
            WHEN area_code IN ('identity', 'preferences', 'relation') OR memory_type LIKE '%preference%' THEN 'user_profile'
            WHEN project_key IS NOT NULL OR area_code = 'projects' OR layer_code = 'projects' THEN 'project'
            ELSE 'raw_note'
        END
        """
    )
    cursor.execute(
        """
        UPDATE memories
        SET truth_kind = CASE
            WHEN truth_kind IS NOT NULL AND trim(truth_kind) != '' THEN truth_kind
            WHEN entry_type = 'dream' THEN 'dream'
            WHEN entry_type = 'decision' THEN 'decision'
            WHEN entry_type = 'user_profile' THEN 'preference'
            WHEN entry_type IN ('experiment', 'raw_note', 'open_question') THEN 'proposal'
            WHEN entry_type = 'conflict' THEN 'interpretation'
            ELSE 'fact'
        END
        """
    )
    cursor.execute(
        """
        UPDATE memories
        SET memory_v2_status = CASE
            WHEN memory_v2_status IS NOT NULL AND trim(memory_v2_status) != '' THEN memory_v2_status
            WHEN state_code = 'candidate' THEN 'proposed'
            WHEN state_code = 'conflicted' OR contradiction_flag = 1 THEN 'contradicted'
            WHEN state_code = 'archived' OR activity_state = 'archived' THEN 'archived'
            WHEN state_code = 'superseded' THEN 'superseded'
            ELSE 'active'
        END
        """
    )
    cursor.execute(
        """
        UPDATE memories
        SET importance_level = CASE
            WHEN importance_level IS NOT NULL AND trim(importance_level) != '' THEN importance_level
            WHEN COALESCE(importance_score, 0.0) >= 0.85 THEN 'critical'
            WHEN COALESCE(importance_score, 0.0) >= 0.65 THEN 'high'
            WHEN COALESCE(importance_score, 0.0) >= 0.35 THEN 'medium'
            ELSE 'low'
        END
        """
    )
    cursor.execute(
        """
        UPDATE memories
        SET requires_user_confirmation = CASE
            WHEN requires_user_confirmation IS NOT NULL AND requires_user_confirmation != 0 THEN requires_user_confirmation
            WHEN truth_kind IN ('dream', 'interpretation', 'proposal') THEN 1
            ELSE 0
        END
        """
    )
    cursor.execute(
        """
        UPDATE memories
        SET should_resurface_when_json = COALESCE(should_resurface_when_json, '[]')
        WHERE should_resurface_when_json IS NULL
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_schema_version ON memories(schema_version)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_entry_type ON memories(entry_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_truth_kind ON memories(truth_kind)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_memory_v2_status ON memories(memory_v2_status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_importance_level ON memories(importance_level)")
    cursor.execute(
        """
        INSERT OR IGNORE INTO feature_flags (flag_key, is_enabled, rollout_mode, notes)
        VALUES ('memory_v2_enabled', 1, 'all', 'Controls MemoryEntry v2 fields, lifecycle and truth-aware retrieval')
        """
    )


def _migration_0020_memory_consolidation_review_queue(conn: sqlite3.Connection) -> None:
    """Durable review-state for consolidation proposals stored in memories."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_consolidation_review_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_memory_id INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_at TEXT,
            reviewed_by TEXT,
            review_note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (proposal_memory_id) REFERENCES memories(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_consolidation_review_items_status "
        "ON memory_consolidation_review_items(status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_consolidation_review_items_reviewed_at "
        "ON memory_consolidation_review_items(reviewed_at)"
    )


def _migration_0021_consolidation_apply_preview_snapshots(conn: sqlite3.Connection) -> None:
    """Immutable preview snapshots stored for consolidation apply runs."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_consolidation_apply_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL UNIQUE,
            proposal_memory_id INTEGER,
            schema_version TEXT NOT NULL DEFAULT 'consolidation_apply_preview_snapshot.v1',
            preview_source TEXT NOT NULL DEFAULT 'stored_at_apply',
            preview_hash TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES sleep_runs(id),
            FOREIGN KEY (proposal_memory_id) REFERENCES memories(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_consolidation_apply_snapshots_proposal "
        "ON memory_consolidation_apply_snapshots(proposal_memory_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_consolidation_apply_snapshots_created_at "
        "ON memory_consolidation_apply_snapshots(created_at)"
    )


def _migration_0022_consolidation_rollback_preview_snapshots(conn: sqlite3.Connection) -> None:
    """Immutable preview snapshots stored for successful consolidation apply rollbacks."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_consolidation_rollback_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_apply_run_id INTEGER NOT NULL UNIQUE,
            rollback_run_id INTEGER NOT NULL UNIQUE,
            schema_version TEXT NOT NULL DEFAULT 'consolidation_apply_rollback_preview_snapshot.v1',
            preview_source TEXT NOT NULL DEFAULT 'stored_at_rollback',
            rollback_preview_hash TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (original_apply_run_id) REFERENCES sleep_runs(id),
            FOREIGN KEY (rollback_run_id) REFERENCES sleep_runs(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_consolidation_rollback_snapshots_rollback_run "
        "ON memory_consolidation_rollback_snapshots(rollback_run_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_consolidation_rollback_snapshots_created_at "
        "ON memory_consolidation_rollback_snapshots(created_at)"
    )


def _migration_0023_memory_v3_lifecycle_snapshots(conn: sqlite3.Connection) -> None:
    """Durable lifecycle snapshots for guarded Memory v3 supersession apply and rollback."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_lifecycle_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_key TEXT NOT NULL UNIQUE,
            operation_type TEXT NOT NULL DEFAULT 'supersession',
            status TEXT NOT NULL DEFAULT 'applied',
            new_memory_id INTEGER NOT NULL,
            old_memory_id INTEGER NOT NULL,
            relation_kind TEXT NOT NULL,
            reason TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            candidate_set_fingerprint TEXT NOT NULL,
            preview_hash TEXT NOT NULL,
            before_snapshot_json TEXT NOT NULL,
            after_snapshot_json TEXT NOT NULL,
            link_snapshot_json TEXT NOT NULL,
            event_snapshot_json TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            applied_by TEXT,
            apply_note TEXT,
            rollback_preview_hash TEXT,
            rollback_snapshot_json TEXT,
            rolled_back_at TEXT,
            rolled_back_by TEXT,
            rollback_note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (new_memory_id) REFERENCES memories(id),
            FOREIGN KEY (old_memory_id) REFERENCES memories(id),
            CHECK (operation_type IN ('supersession')),
            CHECK (status IN ('applied', 'rolled_back', 'failed'))
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_snapshots_status "
        "ON memory_lifecycle_snapshots(status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_snapshots_new_memory "
        "ON memory_lifecycle_snapshots(new_memory_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_snapshots_old_memory "
        "ON memory_lifecycle_snapshots(old_memory_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_snapshots_created_at "
        "ON memory_lifecycle_snapshots(created_at)"
    )


def _migration_0024_memory_capture_review_queue(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_capture_review_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            proposal_json TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            project_key TEXT,
            scope_code TEXT,
            conversation_key TEXT,
            source_context TEXT,
            source_event_ref TEXT,
            recommended_action TEXT,
            matched_memory_ids_json TEXT NOT NULL DEFAULT '[]',
            reconciliation_json TEXT,
            candidate_set_fingerprint TEXT,
            reconciliation_preview_hash TEXT,
            created_memory_id INTEGER,
            reviewed_at TEXT,
            reviewed_by TEXT,
            review_note TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_memory_id) REFERENCES memories(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_capture_review_items_status "
        "ON memory_capture_review_items(status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_capture_review_items_project_key "
        "ON memory_capture_review_items(project_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_capture_review_items_created_at "
        "ON memory_capture_review_items(created_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_capture_review_items_input_fingerprint "
        "ON memory_capture_review_items(input_fingerprint)"
    )
    cursor.execute(
        """
        INSERT OR IGNORE INTO feature_flags (flag_key, is_enabled, rollout_mode, notes)
        VALUES (
            'memory_v3_capture_reconciliation_enabled',
            0,
            'off',
            'Guards durable capture queue and deterministic reconciliation preview for Memory v3'
        )
        """
    )


def _migration_0025_memory_v3_policy_metadata(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_retention_review_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_key TEXT NOT NULL UNIQUE,
            memory_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'rolled_back', 'expired')),
            project_key TEXT,
            scope_code TEXT,
            workspace_id INTEGER,
            as_of TEXT NOT NULL,
            sensitivity_class TEXT NOT NULL
                CHECK (sensitivity_class IN ('public', 'internal', 'personal', 'health_sensitive', 'financial_sensitive', 'credential_secret', 'private_key', 'never_store')),
            retention_class TEXT NOT NULL
                CHECK (retention_class IN ('core_protected', 'durable', 'operational', 'temporary', 'dream', 'sensitive_restricted', 'never_store')),
            policy_outcome TEXT NOT NULL
                CHECK (policy_outcome IN ('retain', 'revalidate', 'archive_candidate', 'expire_candidate', 'protected', 'blocked_never_store')),
            proposed_action TEXT
                CHECK (proposed_action IS NULL OR proposed_action IN ('revalidate', 'archive_candidate', 'expire_candidate')),
            protected_reasons_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(protected_reasons_json)),
            reason_codes_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(reason_codes_json)),
            input_fingerprint TEXT NOT NULL,
            preview_hash TEXT NOT NULL,
            preview_json TEXT NOT NULL CHECK (json_valid(preview_json)),
            reviewed_at TEXT,
            reviewed_by TEXT,
            review_note TEXT,
            applied_at TEXT,
            applied_by TEXT,
            apply_note TEXT,
            before_snapshot_json TEXT CHECK (before_snapshot_json IS NULL OR json_valid(before_snapshot_json)),
            applied_snapshot_json TEXT CHECK (applied_snapshot_json IS NULL OR json_valid(applied_snapshot_json)),
            created_event_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(created_event_ids_json)),
            apply_result_fingerprint TEXT,
            rollback_preview_hash TEXT,
            rollback_snapshot_json TEXT CHECK (rollback_snapshot_json IS NULL OR json_valid(rollback_snapshot_json)),
            rolled_back_at TEXT,
            rolled_back_by TEXT,
            rollback_note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (memory_id) REFERENCES memories(id),
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_retention_review_items_status "
        "ON memory_retention_review_items(status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_retention_review_items_memory_id "
        "ON memory_retention_review_items(memory_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_retention_review_items_project_key "
        "ON memory_retention_review_items(project_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_retention_review_items_created_at "
        "ON memory_retention_review_items(created_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_retention_review_items_preview_hash "
        "ON memory_retention_review_items(preview_hash)"
    )
    cursor.execute(
        """
        INSERT OR IGNORE INTO feature_flags (flag_key, is_enabled, rollout_mode, notes)
        VALUES (
            'memory_v3_retention_enabled',
            0,
            'off',
            'Guards deterministic sensitivity, retention review, apply and rollback for Memory v3'
        )
        """
    )


def _migration_0026_sandman_semantic_shadow_runs(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sandman_semantic_shadow_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_key TEXT NOT NULL UNIQUE,
            request_id TEXT NOT NULL,
            provider_name TEXT NOT NULL CHECK (provider_name = 'gemini'),
            provider_kind TEXT NOT NULL CHECK (provider_kind = 'external_model'),
            model_name TEXT NOT NULL,
            model_role TEXT NOT NULL CHECK (model_role IN ('primary', 'escalation')),
            api_mode TEXT NOT NULL CHECK (api_mode = 'interactions'),
            status TEXT NOT NULL
                CHECK (status IN ('planned', 'running', 'completed', 'rejected', 'failed', 'skipped')),
            project_key TEXT NOT NULL,
            scope_code TEXT NOT NULL,
            workspace_id INTEGER,
            request_schema_version TEXT NOT NULL,
            response_schema_version TEXT NOT NULL,
            validation_schema_version TEXT NOT NULL,
            redaction_policy_version TEXT NOT NULL,
            external_data_policy TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            request_manifest_json TEXT NOT NULL CHECK (json_valid(request_manifest_json)),
            candidate_memory_ids_json TEXT NOT NULL CHECK (json_valid(candidate_memory_ids_json)),
            allowed_actions_json TEXT NOT NULL CHECK (json_valid(allowed_actions_json)),
            proposal_budget INTEGER NOT NULL CHECK (proposal_budget BETWEEN 1 AND 8),
            store_requested INTEGER NOT NULL CHECK (store_requested = 0),
            previous_interaction_id_used INTEGER NOT NULL CHECK (previous_interaction_id_used = 0),
            background_used INTEGER NOT NULL CHECK (background_used = 0),
            tools_used INTEGER NOT NULL CHECK (tools_used = 0),
            file_api_used INTEGER NOT NULL CHECK (file_api_used = 0),
            grounding_used INTEGER NOT NULL CHECK (grounding_used = 0),
            started_at TEXT,
            completed_at TEXT,
            latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
            input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
            output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
            total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
            estimated_cost_usd REAL CHECK (estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
            validation_status TEXT,
            validation_reason_codes_json TEXT NOT NULL DEFAULT '[]'
                CHECK (json_valid(validation_reason_codes_json)),
            proposal_counts_json TEXT NOT NULL DEFAULT '{}'
                CHECK (json_valid(proposal_counts_json)),
            abstain INTEGER CHECK (abstain IS NULL OR abstain IN (0, 1)),
            response_fingerprint TEXT,
            error_category TEXT,
            provider_metadata_json TEXT NOT NULL DEFAULT '{}'
                CHECK (json_valid(provider_metadata_json)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        )
        """
    )
    for name, column in (
        ("idx_sandman_shadow_runs_request_id", "request_id"),
        ("idx_sandman_shadow_runs_status", "status"),
        ("idx_sandman_shadow_runs_project_key", "project_key"),
        ("idx_sandman_shadow_runs_model_name", "model_name"),
        ("idx_sandman_shadow_runs_created_at", "created_at"),
        ("idx_sandman_shadow_runs_input_fingerprint", "input_fingerprint"),
        ("idx_sandman_shadow_runs_validation_status", "validation_status"),
    ):
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {name} "
            f"ON sandman_semantic_shadow_runs({column})"
        )
    cursor.executemany(
        """
        INSERT OR IGNORE INTO feature_flags (flag_key, is_enabled, rollout_mode, notes)
        VALUES (?, 0, 'off', ?)
        """,
        (
            (
                "sandman_provider_v3_enabled",
                "Guards the Memory v3 provider layer and exact redacted request boundary",
            ),
            (
                "sandman_gemini_shadow_enabled",
                "Guards stateless Gemini Interactions API shadow analysis for Sandman v3",
            ),
        ),
    )


def _migration_0027_memory_v3_pointer_lifecycle_execution(conn: sqlite3.Connection) -> None:
    """Extend lifecycle snapshots for guarded pointer-lineage execution runs."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE memory_lifecycle_snapshots_0027 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_key TEXT NOT NULL UNIQUE,
            operation_type TEXT NOT NULL DEFAULT 'supersession',
            status TEXT NOT NULL DEFAULT 'applied',
            new_memory_id INTEGER NOT NULL,
            old_memory_id INTEGER NOT NULL,
            relation_kind TEXT NOT NULL,
            reason TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            candidate_set_fingerprint TEXT NOT NULL,
            preview_hash TEXT NOT NULL,
            before_snapshot_json TEXT NOT NULL,
            after_snapshot_json TEXT NOT NULL,
            link_snapshot_json TEXT NOT NULL,
            event_snapshot_json TEXT NOT NULL,
            applied_at TEXT,
            started_at TEXT NOT NULL,
            applied_by TEXT,
            apply_note TEXT,
            rollback_preview_hash TEXT,
            rollback_snapshot_json TEXT,
            rolled_back_at TEXT,
            rolled_back_by TEXT,
            rollback_note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (new_memory_id) REFERENCES memories(id),
            FOREIGN KEY (old_memory_id) REFERENCES memories(id),
            CHECK (operation_type IN ('supersession', 'pointer_lineage_remediation')),
            CHECK (status IN ('applying', 'applied', 'rolled_back', 'failed'))
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO memory_lifecycle_snapshots_0027 (
            id,
            operation_key,
            operation_type,
            status,
            new_memory_id,
            old_memory_id,
            relation_kind,
            reason,
            input_fingerprint,
            candidate_set_fingerprint,
            preview_hash,
            before_snapshot_json,
            after_snapshot_json,
            link_snapshot_json,
            event_snapshot_json,
            applied_at,
            started_at,
            applied_by,
            apply_note,
            rollback_preview_hash,
            rollback_snapshot_json,
            rolled_back_at,
            rolled_back_by,
            rollback_note,
            created_at,
            updated_at
        )
        SELECT
            id,
            operation_key,
            operation_type,
            status,
            new_memory_id,
            old_memory_id,
            relation_kind,
            reason,
            input_fingerprint,
            candidate_set_fingerprint,
            preview_hash,
            before_snapshot_json,
            after_snapshot_json,
            link_snapshot_json,
            event_snapshot_json,
            applied_at,
            COALESCE(applied_at, created_at),
            applied_by,
            apply_note,
            rollback_preview_hash,
            rollback_snapshot_json,
            rolled_back_at,
            rolled_back_by,
            rollback_note,
            created_at,
            updated_at
        FROM memory_lifecycle_snapshots
        ORDER BY id
        """
    )
    cursor.execute("DROP TABLE memory_lifecycle_snapshots")
    cursor.execute(
        "ALTER TABLE memory_lifecycle_snapshots_0027 RENAME TO memory_lifecycle_snapshots"
    )
    cursor.execute(
        "CREATE INDEX idx_memory_lifecycle_snapshots_status "
        "ON memory_lifecycle_snapshots(status)"
    )
    cursor.execute(
        "CREATE INDEX idx_memory_lifecycle_snapshots_new_memory "
        "ON memory_lifecycle_snapshots(new_memory_id)"
    )
    cursor.execute(
        "CREATE INDEX idx_memory_lifecycle_snapshots_old_memory "
        "ON memory_lifecycle_snapshots(old_memory_id)"
    )
    cursor.execute(
        "CREATE INDEX idx_memory_lifecycle_snapshots_created_at "
        "ON memory_lifecycle_snapshots(created_at)"
    )




def _migration_0028_sandman_canonical_scheduler(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sandman_scheduler_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_key TEXT NOT NULL UNIQUE,
            run_type TEXT NOT NULL CHECK (run_type IN ('nightly_preview','canary','morning_report')),
            scheduler_name TEXT NOT NULL,
            scheduler_timezone TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('planned','running','completed','no_op','partial','blocked','failed','missed','timed_out')),
            project_key TEXT NOT NULL,
            scope_code TEXT NOT NULL CHECK (scope_code = 'project'),
            provider_path TEXT NOT NULL,
            deterministic_provider TEXT NOT NULL CHECK (deterministic_provider = 'deterministic'),
            shadow_provider TEXT NOT NULL CHECK (shadow_provider = 'gemini'),
            model_name TEXT,
            model_role TEXT,
            prompt_version TEXT NOT NULL,
            request_schema_version TEXT NOT NULL,
            response_schema_version TEXT NOT NULL,
            validation_schema_version TEXT NOT NULL,
            redaction_policy_version TEXT NOT NULL,
            external_data_policy TEXT NOT NULL,
            input_fingerprint TEXT,
            candidate_memory_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(candidate_memory_ids_json)),
            allowed_actions_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(allowed_actions_json)),
            candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
            deterministic_proposal_count INTEGER NOT NULL DEFAULT 0 CHECK (deterministic_proposal_count >= 0),
            shadow_run_id INTEGER,
            shadow_status TEXT,
            shadow_validation_status TEXT,
            changed_count INTEGER NOT NULL DEFAULT 0 CHECK (changed_count = 0),
            auto_apply INTEGER NOT NULL DEFAULT 0 CHECK (auto_apply = 0),
            network_calls INTEGER NOT NULL DEFAULT 0 CHECK (network_calls >= 0),
            latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
            input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
            output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
            total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
            estimated_cost_usd REAL CHECK (estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),
            report_path TEXT,
            result_summary_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(result_summary_json)),
            reason_codes_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(reason_codes_json)),
            error_category TEXT,
            timeout_seconds INTEGER NOT NULL CHECK (timeout_seconds BETWEEN 1 AND 3600),
            source_run_id INTEGER,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (shadow_run_id) REFERENCES sandman_semantic_shadow_runs(id),
            FOREIGN KEY (source_run_id) REFERENCES sandman_scheduler_runs(id)
        )
        """
    )
    for name, column in (
        ("idx_sandman_scheduler_runs_type", "run_type"),
        ("idx_sandman_scheduler_runs_status", "status"),
        ("idx_sandman_scheduler_runs_project", "project_key"),
        ("idx_sandman_scheduler_runs_started", "started_at"),
        ("idx_sandman_scheduler_runs_source", "source_run_id"),
    ):
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {name} ON sandman_scheduler_runs({column})")

    cursor.execute(
        """
        INSERT OR IGNORE INTO feature_flags (
            flag_key, is_enabled, rollout_mode, allowed_project_keys,
            allowed_scope_codes, read_only_mode, notes
        ) VALUES (
            'sandman_canonical_scheduler_enabled', 1, 'projects_and_scopes',
            'demo-project,mapi', 'project', 1,
            'Canonical proposal-only scheduler: deterministic core plus Gemini shadow'
        )
        """
    )
    cursor.execute(
        """
        INSERT OR IGNORE INTO feature_flags (
            flag_key, is_enabled, rollout_mode, allowed_project_keys,
            allowed_scope_codes, read_only_mode, notes
        ) VALUES (
            'sandman_model_queue_routing_enabled', 0, 'off',
            NULL, NULL, 1,
            'Disabled in canonical scheduler; model output is shadow-only'
        )
        """
    )
    cursor.execute(
        """
        INSERT OR IGNORE INTO feature_flags (
            flag_key, is_enabled, rollout_mode, allowed_project_keys,
            allowed_scope_codes, read_only_mode, notes
        ) VALUES (
            'sandman_gemma_hygiene_enabled', 0, 'off',
            NULL, NULL, 1,
            'Disabled by canonical Sandman migration 0028; legacy local Gemma dormant'
        )
        """
    )
    cursor.execute(
        """
        UPDATE feature_flags
        SET is_enabled=1, rollout_mode='projects_and_scopes',
            allowed_project_keys='demo-project,mapi',
            allowed_scope_codes='project', read_only_mode=1,
            notes='Canonical Sandman deterministic provider boundary',
            updated_at=CURRENT_TIMESTAMP
        WHERE flag_key='sandman_provider_v3_enabled'
        """
    )
    cursor.execute(
        """
        UPDATE feature_flags
        SET is_enabled=1, rollout_mode='projects_and_scopes',
            allowed_project_keys='demo-project,mapi',
            allowed_scope_codes='project', read_only_mode=1,
            notes='Canonical stateless Gemini shadow; store=false',
            updated_at=CURRENT_TIMESTAMP
        WHERE flag_key='sandman_gemini_shadow_enabled'
        """
    )
    cursor.execute(
        """
        UPDATE feature_flags
        SET is_enabled=0, rollout_mode='off', read_only_mode=1,
            notes='Disabled by canonical Sandman migration 0028; legacy local Gemma dormant',
            updated_at=CURRENT_TIMESTAMP
        WHERE flag_key='sandman_gemma_hygiene_enabled'
        """
    )
    cursor.execute(
        """
        UPDATE feature_flags
        SET is_enabled=0, rollout_mode='off', read_only_mode=1,
            notes='Disabled by canonical Sandman migration 0028; no automatic model queue writes',
            updated_at=CURRENT_TIMESTAMP
        WHERE flag_key='sandman_model_queue_routing_enabled'
        """
    )


def _migration_0029_memory_hygiene_metadata_repair(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_hygiene_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            policy_version TEXT NOT NULL,
            project_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running','completed','rolled_back','failed')),
            preview_hash TEXT NOT NULL UNIQUE,
            candidate_set_fingerprint TEXT NOT NULL,
            candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
            changed_count INTEGER NOT NULL DEFAULT 0 CHECK (changed_count >= 0),
            applied_by TEXT NOT NULL,
            reason TEXT NOT NULL,
            backup_path TEXT NOT NULL,
            preview_json TEXT NOT NULL CHECK (json_valid(preview_json)),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            rolled_back_at TEXT,
            rolled_back_by TEXT,
            rollback_note TEXT,
            rollback_preview_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_hygiene_run_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            memory_id INTEGER NOT NULL,
            old_metadata_json TEXT NOT NULL CHECK (json_valid(old_metadata_json)),
            new_metadata_json TEXT NOT NULL CHECK (json_valid(new_metadata_json)),
            reason_codes_json TEXT NOT NULL CHECK (json_valid(reason_codes_json)),
            created_at TEXT NOT NULL,
            UNIQUE(run_id, memory_id),
            FOREIGN KEY (run_id) REFERENCES memory_hygiene_runs(id),
            FOREIGN KEY (memory_id) REFERENCES memories(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_hygiene_runs_project_status "
        "ON memory_hygiene_runs(project_key, status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_hygiene_runs_created "
        "ON memory_hygiene_runs(created_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_hygiene_items_memory "
        "ON memory_hygiene_run_items(memory_id)"
    )
    cursor.execute(
        """
        INSERT OR IGNORE INTO owner_directory_items (
            owner_key, owner_type, display_name, is_active,
            routing_metadata_json
        ) VALUES (
            'project_agent_maintainers', 'team',
            'Project MAPI Maintainers', 1,
            '{"domain":"project_ops","project_key":"mapi","scope":"project"}'
        )
        """
    )
    cursor.execute(
        """
        UPDATE owner_role_mappings
        SET scope_code='project',
            notes='Project-specific operational target for mapi project scope',
            updated_at=CURRENT_TIMESTAMP
        WHERE owner_role='project_maintainer'
          AND owner_key='project_agent_maintainers'
          AND project_key='mapi'
          AND COALESCE(scope_code, '')='global'
        """
    )
    cursor.execute(
        """
        INSERT OR IGNORE INTO owner_role_mappings (
            owner_role, owner_key, project_key, scope_code, is_active, notes
        ) VALUES (
            'project_maintainer', 'project_agent_maintainers',
            'mapi', 'project', 1,
            'Project-specific operational target for mapi project scope'
        )
        """
    )


def _migration_0030_memory_retention_policy_v2(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO feature_flags (
            flag_key, is_enabled, rollout_mode, allowed_project_keys,
            allowed_scope_codes, read_only_mode, notes
        ) VALUES (
            'memory_v3_retention_enabled', 1, 'projects_and_scopes',
            'mapi', 'project', 1,
            'Sprint 10 canonical retention v2 review-only rollout; no purge'
        )
        """
    )
    cursor.execute(
        """
        UPDATE feature_flags
        SET is_enabled=1,
            rollout_mode='projects_and_scopes',
            allowed_project_keys='mapi',
            allowed_scope_codes='project',
            read_only_mode=1,
            notes='Sprint 10 canonical retention v2 review-only rollout; no purge',
            updated_at=CURRENT_TIMESTAMP
        WHERE flag_key='memory_v3_retention_enabled'
        """
    )


def _migration_0031_private_remote_auth(conn: sqlite3.Connection) -> None:
    from app.runtime.remote_auth_store import ensure_remote_auth_schema

    ensure_remote_auth_schema(conn)


def _migration_0032_retire_bridge_mailbox(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS bridge_messages")
    cursor.execute("DROP TABLE IF EXISTS bridge_threads")


def _migration_0033_mcp_idempotency_requests(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_idempotency_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            operation_name TEXT NOT NULL,
            payload_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('started','completed','in_doubt')),
            result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
            error_type TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_idempotency_operation_status "
        "ON mcp_idempotency_requests(operation_name, status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_idempotency_updated "
        "ON mcp_idempotency_requests(updated_at)"
    )


def _migration_0034_recall_importance_decoupling(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_events_recall_recorded "
        "ON memory_events(memory_id, event_type, id DESC)"
    )
    cursor.execute("DROP VIEW IF EXISTS memory_recall_telemetry")
    cursor.execute(
        """
        CREATE VIEW memory_recall_telemetry AS
        SELECT
            m.id AS memory_id,
            m.importance_score AS importance_score,
            COALESCE(m.recall_count, 0) AS recall_count,
            m.last_recalled_at AS last_recalled_at,
            COUNT(CASE WHEN e.event_type = 'recall.recorded' THEN 1 END) AS recorded_event_count,
            MAX(CASE WHEN e.event_type = 'recall.recorded' THEN e.created_at END) AS latest_recorded_event_at,
            MAX(
                COALESCE(m.recall_count, 0)
                - COUNT(CASE WHEN e.event_type = 'recall.recorded' THEN 1 END),
                0
            ) AS legacy_unattributed_recall_count
        FROM memories m
        LEFT JOIN memory_events e ON e.memory_id = m.id
        GROUP BY m.id
        """
    )


def _migration_0035_polaris_onboarding(conn: sqlite3.Connection) -> None:
    from mapi_core.onboarding import ensure_onboarding_schema

    ensure_onboarding_schema(conn)


def _migration_0036_memory_self_healing(conn: sqlite3.Connection) -> None:
    from mapi_core.memory.self_healing import ensure_self_healing_schema

    ensure_self_healing_schema(conn)


def _migration_0037_common_file_operations(conn: sqlite3.Connection) -> None:
    from mapi_capabilities.schema import ensure_file_operation_schema

    ensure_file_operation_schema(conn)


def _migration_0038_common_git_commit_operations(conn: sqlite3.Connection) -> None:
    from mapi_capabilities.schema import ensure_git_commit_operation_schema

    ensure_git_commit_operation_schema(conn)


def _migration_0039_common_git_stage_operations(conn: sqlite3.Connection) -> None:
    from mapi_capabilities.schema import ensure_git_stage_operation_schema

    ensure_git_stage_operation_schema(conn)


def _migration_0040_common_command_runs(conn: sqlite3.Connection) -> None:
    from mapi_capabilities.schema import ensure_command_run_schema

    ensure_command_run_schema(conn)


def _migration_0041_revocable_service_auth(conn: sqlite3.Connection) -> None:
    from app.runtime.remote_auth_store import upgrade_remote_auth_service_tokens

    upgrade_remote_auth_service_tokens(conn)


def _migration_0042_legacy_aurora_import(conn: sqlite3.Connection) -> None:
    from app.runtime.legacy_import import ensure_aurora_import_schema

    ensure_aurora_import_schema(conn)


MIGRATION_SEQUENCE = [
    ("0001_memory_core", _migration_0001_memory_core),
    ("0002_timeline_schema", _migration_0002_timeline_schema),
    ("0003_timeline_schema_hardening", _migration_0003_timeline_schema_hardening),
    ("0004_project_timeline_semantics", _migration_0004_project_timeline_semantics),
    ("0005_memory_layer_area_metadata", _migration_0005_memory_layer_area_metadata),
    ("0006_feature_flags", _migration_0006_feature_flags),
    ("0007_ownership_sla", _migration_0007_ownership_sla),
    ("0008_expired_duplicate_sla", _migration_0008_expired_duplicate_sla),
    ("0009_owner_resolution_layer", _migration_0009_owner_resolution_layer),
    ("0010_multiuser_identity_foundation", _migration_0010_multiuser_identity_foundation),
    ("0011_scope_aware_maintenance", _migration_0011_scope_aware_maintenance),
    ("0012_priority_and_sla_policies", _migration_0012_priority_and_sla_policies),
    ("0013_escalation_history", _migration_0013_escalation_history),
    ("0014_research_ingest_quarantine", _migration_0014_research_ingest_quarantine),
    ("0015_conversation_archive", _migration_0015_conversation_archive),
    ("0016_gemma_worker_jobs", _migration_0016_gemma_worker_jobs),
    ("0017_project_key_aliases", _migration_0017_project_key_aliases),
    ("0018_bridge_mailbox", _migration_0018_bridge_mailbox),
    ("0019_memory_entry_v2_foundation", _migration_0019_memory_entry_v2_foundation),
    ("0020_memory_consolidation_review_queue", _migration_0020_memory_consolidation_review_queue),
    ("0021_consolidation_apply_preview_snapshots", _migration_0021_consolidation_apply_preview_snapshots),
    ("0022_consolidation_rollback_preview_snapshots", _migration_0022_consolidation_rollback_preview_snapshots),
    ("0023_memory_v3_lifecycle_snapshots", _migration_0023_memory_v3_lifecycle_snapshots),
    ("0024_memory_capture_review_queue", _migration_0024_memory_capture_review_queue),
    ("0025_memory_v3_policy_metadata", _migration_0025_memory_v3_policy_metadata),
    ("0026_sandman_semantic_shadow_runs", _migration_0026_sandman_semantic_shadow_runs),
    ("0027_memory_v3_pointer_lifecycle_execution", _migration_0027_memory_v3_pointer_lifecycle_execution),
    ("0028_sandman_canonical_scheduler", _migration_0028_sandman_canonical_scheduler),
    ("0029_memory_hygiene_metadata_repair", _migration_0029_memory_hygiene_metadata_repair),
    ("0030_memory_retention_policy_v2", _migration_0030_memory_retention_policy_v2),
    ("0031_private_remote_auth", _migration_0031_private_remote_auth),
    ("0032_retire_bridge_mailbox", _migration_0032_retire_bridge_mailbox),
    ("0033_mcp_idempotency_requests", _migration_0033_mcp_idempotency_requests),
    ("0034_recall_importance_decoupling", _migration_0034_recall_importance_decoupling),
    ("0035_polaris_onboarding", _migration_0035_polaris_onboarding),
    ("0036_memory_self_healing", _migration_0036_memory_self_healing),
    ("0037_common_file_operations", _migration_0037_common_file_operations),
    ("0038_common_git_commit_operations", _migration_0038_common_git_commit_operations),
    ("0039_common_git_stage_operations", _migration_0039_common_git_stage_operations),
    ("0040_common_command_runs", _migration_0040_common_command_runs),
    ("0041_revocable_service_auth", _migration_0041_revocable_service_auth),
    ("0042_legacy_aurora_import", _migration_0042_legacy_aurora_import),
]


def apply_migrations_through(
    conn: sqlite3.Connection,
    target_version: str,
) -> list[str]:
    known_versions = [version for version, _migration_fn in MIGRATION_SEQUENCE]
    if target_version not in known_versions:
        raise ValueError(f"unknown_migration_target:{target_version}")
    ensure_schema_migrations_table(conn)
    applied = applied_migration_versions(conn)
    ran: list[str] = []
    for version, migration_fn in MIGRATION_SEQUENCE:
        if version not in applied:
            migration_fn(conn)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
            ran.append(version)
        if version == target_version:
            break
    return ran


def apply_all_migrations(conn: sqlite3.Connection) -> list[str]:
    return apply_migrations_through(conn, MIGRATION_SEQUENCE[-1][0])
