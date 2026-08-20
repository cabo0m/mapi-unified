from __future__ import annotations

import sqlite3


def ensure_remote_auth_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remote_auth_authorization_codes (
            code_hash TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            scopes_json TEXT NOT NULL CHECK (json_valid(scopes_json)),
            code_challenge TEXT NOT NULL,
            owner_key TEXT NOT NULL,
            profile TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            consumed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remote_auth_clients (
            client_id TEXT PRIMARY KEY,
            client_json TEXT NOT NULL CHECK (json_valid(client_json)),
            created_at TEXT NOT NULL,
            last_seen_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remote_auth_tokens (
            token_hash TEXT PRIMARY KEY,
            token_kind TEXT NOT NULL CHECK (token_kind IN ('access','refresh','codex','service')),
            client_id TEXT NOT NULL,
            owner_key TEXT NOT NULL,
            profile TEXT NOT NULL,
            scopes_json TEXT NOT NULL CHECK (json_valid(scopes_json)),
            expires_at INTEGER,
            pair_hash TEXT,
            rotated_to_hash TEXT,
            label TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT,
            revoked_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remote_auth_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            channel TEXT NOT NULL,
            client_id TEXT,
            owner_key TEXT,
            profile TEXT,
            outcome TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            token_fingerprint TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remote_auth_rate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket_hash TEXT NOT NULL,
            action TEXT NOT NULL,
            occurred_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_remote_auth_codes_expiry "
        "ON remote_auth_authorization_codes(expires_at, consumed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_remote_auth_clients_created "
        "ON remote_auth_clients(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_remote_auth_tokens_kind_status "
        "ON remote_auth_tokens(token_kind, revoked_at, expires_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_remote_auth_tokens_fingerprint "
        "ON remote_auth_tokens(substr(token_hash, 1, 16))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_remote_auth_audit_created "
        "ON remote_auth_audit_events(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_remote_auth_rate_bucket "
        "ON remote_auth_rate_events(bucket_hash, action, occurred_at)"
    )



def upgrade_remote_auth_service_tokens(conn: sqlite3.Connection) -> None:
    """Expand the token-kind CHECK constraint without losing existing OAuth/legacy rows."""
    ensure_remote_auth_schema(conn)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='remote_auth_tokens'"
    ).fetchone()
    create_sql = str(row[0] or "") if row is not None else ""
    if "'service'" in create_sql:
        return
    conn.execute("ALTER TABLE remote_auth_tokens RENAME TO remote_auth_tokens_pre_service")
    conn.execute(
        """
        CREATE TABLE remote_auth_tokens (
            token_hash TEXT PRIMARY KEY,
            token_kind TEXT NOT NULL CHECK (token_kind IN ('access','refresh','codex','service')),
            client_id TEXT NOT NULL,
            owner_key TEXT NOT NULL,
            profile TEXT NOT NULL,
            scopes_json TEXT NOT NULL CHECK (json_valid(scopes_json)),
            expires_at INTEGER,
            pair_hash TEXT,
            rotated_to_hash TEXT,
            label TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT,
            revoked_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO remote_auth_tokens (
            token_hash, token_kind, client_id, owner_key, profile, scopes_json,
            expires_at, pair_hash, rotated_to_hash, label, created_at, last_seen_at, revoked_at
        )
        SELECT
            token_hash, token_kind, client_id, owner_key, profile, scopes_json,
            expires_at, pair_hash, rotated_to_hash, label, created_at, last_seen_at, revoked_at
        FROM remote_auth_tokens_pre_service
        """
    )
    conn.execute("DROP TABLE remote_auth_tokens_pre_service")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_remote_auth_tokens_kind_status "
        "ON remote_auth_tokens(token_kind, revoked_at, expires_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_remote_auth_tokens_fingerprint "
        "ON remote_auth_tokens(substr(token_hash, 1, 16))"
    )
