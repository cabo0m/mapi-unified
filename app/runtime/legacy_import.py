from __future__ import annotations

import sqlite3


def ensure_aurora_import_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS legacy_aurora_import_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_fingerprint TEXT NOT NULL UNIQUE,
            source_path_sha256 TEXT NOT NULL,
            source_schema_tail TEXT,
            target_schema_tail TEXT,
            preview_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
            backup_path TEXT,
            counts_json TEXT NOT NULL CHECK (json_valid(counts_json)),
            warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json)),
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_legacy_aurora_import_runs_status
            ON legacy_aurora_import_runs(status, id DESC);

        CREATE TABLE IF NOT EXISTS legacy_aurora_import_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_run_id INTEGER NOT NULL,
            item_type TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_id TEXT,
            target_id INTEGER,
            status TEXT NOT NULL,
            metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
            FOREIGN KEY (import_run_id) REFERENCES legacy_aurora_import_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_legacy_aurora_import_items_run
            ON legacy_aurora_import_items(import_run_id, id);

        CREATE TABLE IF NOT EXISTS legacy_aurora_import_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_run_id INTEGER NOT NULL,
            source_table TEXT NOT NULL,
            source_id TEXT,
            sensitivity_class TEXT,
            payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
            payload_sha256 TEXT NOT NULL,
            redacted INTEGER NOT NULL CHECK (redacted IN (0,1)),
            created_at TEXT NOT NULL,
            FOREIGN KEY (import_run_id) REFERENCES legacy_aurora_import_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_legacy_aurora_import_archive_run
            ON legacy_aurora_import_archive(import_run_id, source_table, id);
        """
    )
