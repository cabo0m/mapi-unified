from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from app.runtime.remote_auth_store import ensure_remote_auth_schema, upgrade_remote_auth_service_tokens

ROOT = Path(__file__).resolve().parents[1]


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT) if not current else str(ROOT) + os.pathsep + current
    return env


def test_service_token_issue_verify_and_revoke(tmp_path: Path) -> None:
    code = r'''
import asyncio
import sqlite3
from pathlib import Path

from app.runtime.owner_credentials import hash_owner_password
from app.runtime.remote_auth import ServiceBearerVerifier, issue_service_bearer_token, revoke_token_fingerprint
from app.runtime.remote_auth_config import RemoteAuthConfig
from app.runtime.remote_auth_store import ensure_remote_auth_schema, upgrade_remote_auth_service_tokens

db = Path("auth.db")
with sqlite3.connect(db) as conn:
    ensure_remote_auth_schema(conn)
    upgrade_remote_auth_service_tokens(conn)
    conn.commit()
config = RemoteAuthConfig(
    enabled=True,
    base_url="https://mapi.example.test",
    owner_key="owner",
    oauth_client_id="chatgpt-private",
    oauth_redirect_uris=(),
    owner_login="owner",
    owner_password_hash=hash_owner_password("a sufficiently long owner password"),
)
issued = issue_service_bearer_token(db_path=db, label="automation", ttl_seconds=3600)
assert issued["status"] == "issued"
assert issued["profile"] == "admin"
assert issued["scopes"] == ["mapi:read", "mapi:write", "mapi:admin"]
assert issued["token"].startswith("mapi_sv_")
raw = issued["token"]
with sqlite3.connect(db) as conn:
    row = conn.execute("SELECT token_hash,token_kind,profile,label FROM remote_auth_tokens WHERE token_kind='service'").fetchone()
    assert row is not None
    assert raw not in row[0]
    assert row[1:] == ("service", "admin", "automation")
verifier = ServiceBearerVerifier(config=config, db_path=db)
token = asyncio.run(verifier.verify_token(raw))
assert token is not None
assert token.claims["auth_channel"] == "service"
assert token.claims["profile"] == "admin"
revoked = revoke_token_fingerprint(db_path=db, fingerprint=issued["token_fingerprint"])
assert revoked["status"] == "revoked"
assert asyncio.run(verifier.verify_token(raw)) is None
'''
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=_subprocess_env(),
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_service_token_upgrade_preserves_existing_oauth_rows(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("""
            CREATE TABLE remote_auth_tokens (
                token_hash TEXT PRIMARY KEY,
                token_kind TEXT NOT NULL CHECK (token_kind IN ('access','refresh','codex')),
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
        """)
        conn.execute(
            "INSERT INTO remote_auth_tokens VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("hash-a", "access", "client", "owner", "admin", '["mapi:read"]', None, None, None, None, "now", None, None),
        )
        conn.commit()
        upgrade_remote_auth_service_tokens(conn)
        conn.commit()
        assert conn.execute("SELECT token_kind,owner_key FROM remote_auth_tokens WHERE token_hash='hash-a'").fetchone() == ("access", "owner")
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='remote_auth_tokens'").fetchone()[0]
        assert "'service'" in sql
    finally:
        conn.close()
