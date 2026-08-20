from __future__ import annotations

import sqlite3
from pathlib import Path

from mapi_core.memory.r6c_manual_support_repair import apply, build_preview, build_rollback_preview, rollback


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            project_key TEXT, scope_code TEXT, memory_type TEXT, summary_short TEXT, title TEXT,
            tags TEXT, source TEXT, state_code TEXT, memory_v2_status TEXT, archived_at TEXT
        );
        CREATE TABLE memory_links (
            id INTEGER PRIMARY KEY, from_memory_id INTEGER, to_memory_id INTEGER, relation_type TEXT,
            weight REAL, origin TEXT, created_at TEXT, archived_at TEXT, workspace_id INTEGER, visibility_scope TEXT
        );
        CREATE TABLE memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER, event_type TEXT, payload_json TEXT, created_at TEXT
        );
        """
    )
    rows = [
        (290, "project_requirement", "MAPI link arrays requirement"),
        (144, "project", "Sandman Agent V2"),
        (145, "fact", "Sandman Agent link tools"),
    ]
    for memory_id, memory_type, summary in rows:
        conn.execute(
            "INSERT INTO memories(id,project_key,scope_code,memory_type,summary_short,title,tags,source,state_code,memory_v2_status,archived_at) VALUES(?, 'jagoda-memory-api','project',?,?,?,'r6c','pytest','active','active',NULL)",
            (memory_id, memory_type, summary, summary),
        )
    conn.execute("INSERT INTO memory_links VALUES(521,290,144,'supports',0.84,'manual_forced_linking_pass','2026-04-26T14:59:19Z',NULL,1,'inherited')")
    conn.execute("INSERT INTO memory_links VALUES(522,290,145,'supports',0.83,'manual_forced_linking_pass','2026-04-26T14:59:26Z',NULL,1,'inherited')")
    conn.commit()
    return conn


def _backup(source: sqlite3.Connection, path: Path) -> None:
    dest = sqlite3.connect(str(path))
    try:
        source.backup(dest)
    finally:
        dest.close()


def test_preview_is_exact_read_only_and_deterministic() -> None:
    conn = _db()
    try:
        before = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        first = build_preview(conn)
        second = build_preview(conn)
        after = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
    finally:
        conn.close()
    assert first["status"] == "preview_ready"
    assert first["preview_hash"] == second["preview_hash"]
    assert [item["link_id"] for item in first["targets"]] == [521, 522]
    assert all(item["manual_review"]["decision"] == "archive_from_active_truth" for item in first["targets"])
    assert first["safety"]["physical_delete"] is False
    assert before == after


def test_preview_blocks_signature_change() -> None:
    conn = _db()
    try:
        conn.execute("UPDATE memory_links SET origin='changed' WHERE id=521")
        conn.commit()
        preview = build_preview(conn)
    finally:
        conn.close()
    assert preview["status"] == "blocked"
    assert "link:521:signature_mismatch:origin" in preview["blockers"]


def test_apply_and_rollback_archive_only_two_links(tmp_path: Path) -> None:
    db_path = tmp_path / "source.db"
    conn = _db(db_path)
    backup = tmp_path / "backup.db"
    _backup(conn, backup)
    try:
        preview = build_preview(conn)
        total_before = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
        applied = apply(
            conn,
            expected_preview_hash=preview["preview_hash"],
            backup_path=str(backup),
            applied_by="pytest",
            reason="manual review says supports assertions are not current-contract evidence",
            confirm_manual_review=True,
            now_iso="2026-08-16T00:40:00Z",
        )
        active = conn.execute("SELECT COUNT(*) FROM memory_links WHERE id IN (521,522) AND archived_at IS NULL").fetchone()[0]
        total_after = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
        rollback_preview = build_rollback_preview(conn)
        rolled = rollback(
            conn,
            expected_rollback_preview_hash=rollback_preview["rollback_preview_hash"],
            rolled_back_by="pytest",
            notes="restore manual support review state",
            now_iso="2026-08-16T00:41:00Z",
        )
        restored = conn.execute("SELECT COUNT(*) FROM memory_links WHERE id IN (521,522) AND archived_at IS NULL").fetchone()[0]
        archive_events = conn.execute("SELECT COUNT(*) FROM memory_events WHERE event_type='memory.link.archived_r6c_manual_review'").fetchone()[0]
        rollback_events = conn.execute("SELECT COUNT(*) FROM memory_events WHERE event_type='memory.link.archive_r6c_manual_review_rolled_back'").fetchone()[0]
    finally:
        conn.close()
    assert applied["status"] == "applied"
    assert active == 0
    assert total_before == total_after
    assert rollback_preview["status"] == "preview_ready"
    assert rolled["status"] == "rolled_back"
    assert restored == 2
    assert archive_events == 2
    assert rollback_events == 2


def test_apply_requires_explicit_manual_review_confirmation(tmp_path: Path) -> None:
    conn = _db()
    backup = tmp_path / "backup.db"
    _backup(conn, backup)
    try:
        preview = build_preview(conn)
        blocked = apply(
            conn,
            expected_preview_hash=preview["preview_hash"],
            backup_path=str(backup),
            applied_by="pytest",
            reason="test",
            confirm_manual_review=False,
            now_iso="2026-08-16T00:40:00Z",
        )
    finally:
        conn.close()
    assert blocked["status"] == "blocked"
    assert blocked["blocking_reasons"] == ["confirm_manual_review_required"]
