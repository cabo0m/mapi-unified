from __future__ import annotations

import sqlite3


def ensure_file_operation_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS file_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_key TEXT NOT NULL UNIQUE,
        project_key TEXT,
        root_id TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        operation_kind TEXT NOT NULL CHECK (operation_kind IN ('create','update')),
        status TEXT NOT NULL CHECK (status IN ('applied','rolled_back')),
        preview_hash TEXT NOT NULL,
        old_sha256 TEXT,
        new_sha256 TEXT NOT NULL,
        old_size_bytes INTEGER,
        new_size_bytes INTEGER NOT NULL,
        backup_path TEXT,
        backup_sha256 TEXT,
        applied_at TEXT NOT NULL,
        rolled_back_at TEXT,
        rollback_note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_file_operations_project ON file_operations(project_key, id DESC);
    CREATE INDEX IF NOT EXISTS idx_file_operations_target ON file_operations(root_id, relative_path, id DESC);
    CREATE INDEX IF NOT EXISTS idx_file_operations_status ON file_operations(status, id DESC);
    """)


def ensure_git_commit_operation_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS git_commit_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_key TEXT NOT NULL UNIQUE,
        project_key TEXT,
        repo_id TEXT NOT NULL,
        branch TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('applied','rolled_back')),
        preview_hash TEXT NOT NULL,
        old_head TEXT NOT NULL,
        new_head TEXT NOT NULL,
        index_sha256_before TEXT NOT NULL,
        index_sha256_after TEXT NOT NULL,
        staged_diff_sha256 TEXT NOT NULL,
        commit_message TEXT NOT NULL,
        commit_message_sha256 TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        rolled_back_at TEXT,
        rollback_note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_git_commit_operations_project ON git_commit_operations(project_key, id DESC);
    CREATE INDEX IF NOT EXISTS idx_git_commit_operations_repo ON git_commit_operations(repo_id, id DESC);
    CREATE INDEX IF NOT EXISTS idx_git_commit_operations_status ON git_commit_operations(status, id DESC);
    """)


def ensure_git_stage_operation_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS git_stage_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_key TEXT NOT NULL UNIQUE,
        project_key TEXT,
        repo_id TEXT NOT NULL,
        branch TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('applied','rolled_back')),
        preview_hash TEXT NOT NULL,
        head TEXT NOT NULL,
        paths_json TEXT NOT NULL CHECK (json_valid(paths_json)),
        index_sha256_before TEXT NOT NULL,
        index_sha256_after TEXT NOT NULL,
        prospective_diff_sha256 TEXT NOT NULL,
        backup_path TEXT NOT NULL,
        backup_sha256 TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        rolled_back_at TEXT,
        rollback_note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_git_stage_operations_project ON git_stage_operations(project_key, id DESC);
    CREATE INDEX IF NOT EXISTS idx_git_stage_operations_repo ON git_stage_operations(repo_id, id DESC);
    CREATE INDEX IF NOT EXISTS idx_git_stage_operations_status ON git_stage_operations(status, id DESC);
    """)


def ensure_command_run_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS command_recipe_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_key TEXT NOT NULL UNIQUE,
        project_key TEXT,
        recipe_id TEXT NOT NULL,
        recipe_name TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running','completed','failed','timeout','output_blocked','start_failed')),
        preview_hash TEXT NOT NULL,
        recipe_fingerprint TEXT NOT NULL,
        exit_code INTEGER,
        stdout_sha256 TEXT NOT NULL,
        stderr_sha256 TEXT NOT NULL,
        stdout_bytes INTEGER NOT NULL,
        stderr_bytes INTEGER NOT NULL,
        output_truncated INTEGER NOT NULL CHECK (output_truncated IN (0,1)),
        output_blocked INTEGER NOT NULL CHECK (output_blocked IN (0,1)),
        started_at TEXT NOT NULL,
        completed_at TEXT,
        duration_ms INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_command_runs_project ON command_recipe_runs(project_key, id DESC);
    CREATE INDEX IF NOT EXISTS idx_command_runs_recipe ON command_recipe_runs(recipe_id, id DESC);
    CREATE INDEX IF NOT EXISTS idx_command_runs_status ON command_recipe_runs(status, id DESC);
    """)


def ensure_capability_schema(conn: sqlite3.Connection) -> None:
    ensure_file_operation_schema(conn)
    ensure_git_commit_operation_schema(conn)
    ensure_git_stage_operation_schema(conn)
    ensure_command_run_schema(conn)
