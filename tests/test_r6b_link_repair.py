from __future__ import annotations

import sqlite3
from pathlib import Path

from mapi_core.memory.r6b_link_repair import (
    ALL_TARGET_LINK_IDS,
    INVALID_LINK_SPECS,
    REDUNDANT_LINK_IDS,
    REPAIR_KEY,
    apply,
    build_preview,
    build_rollback_preview,
    rollback,
)


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memory_links (
            id INTEGER PRIMARY KEY,
            from_memory_id INTEGER NOT NULL,
            to_memory_id INTEGER NOT NULL,
            relation_type TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            origin TEXT,
            created_at TEXT,
            archived_at TEXT,
            workspace_id INTEGER,
            visibility_scope TEXT
        );
        CREATE TABLE memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER,
            event_type TEXT NOT NULL,
            payload_json TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    for spec in INVALID_LINK_SPECS:
        conn.execute(
            "INSERT INTO memory_links(id,from_memory_id,to_memory_id,relation_type,weight,origin,created_at,archived_at,workspace_id,visibility_scope) VALUES(?,?,?,?,1.0,?,'2026-04-01T00:00:00Z',NULL,1,'inherited')",
            (spec["link_id"], spec["from_memory_id"], spec["to_memory_id"], spec["relation_type"], spec["origin"]),
        )
    for index, target_id in enumerate(REDUNDANT_LINK_IDS):
        from_id = 5000 + index
        to_id = 6000 + index
        preserved_id = 100 + index
        conn.execute(
            "INSERT INTO memory_links(id,from_memory_id,to_memory_id,relation_type,weight,origin,created_at,archived_at,workspace_id,visibility_scope) VALUES(?,?,?,'dream',1.0,'first','2026-06-01T00:00:00Z',NULL,1,'inherited')",
            (preserved_id, from_id, to_id),
        )
        conn.execute(
            "INSERT INTO memory_links(id,from_memory_id,to_memory_id,relation_type,weight,origin,created_at,archived_at,workspace_id,visibility_scope) VALUES(?,?,?,'dream',1.0,'later','2026-07-01T00:00:00Z',NULL,1,'inherited')",
            (target_id, from_id, to_id),
        )
    conn.commit()
    return conn


def _backup(source: sqlite3.Connection, path: Path) -> None:
    dest = sqlite3.connect(str(path))
    try:
        source.backup(dest)
    finally:
        dest.close()


def test_preview_freezes_exact_26_targets_and_preserved_duplicates() -> None:
    conn = _db()
    try:
        before = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        preview = build_preview(conn)
        second = build_preview(conn)
        after = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
    finally:
        conn.close()

    assert preview["status"] == "preview_ready"
    assert preview["preview_hash"] == second["preview_hash"]
    assert [item["link_id"] for item in preview["candidates"]] == list(ALL_TARGET_LINK_IDS)
    assert len(preview["candidates"]) == 26
    redundant = [item for item in preview["candidates"] if item["classification"] == "redundant"]
    assert len(redundant) == 24
    assert all(int(item["preserved_link"]["id"]) < int(item["link_id"]) for item in redundant)
    assert preview["safety"]["physical_delete"] is False
    assert before == after


def test_preview_blocks_changed_invalid_signature() -> None:
    conn = _db()
    try:
        conn.execute("UPDATE memory_links SET origin='changed' WHERE id=361")
        conn.commit()
        preview = build_preview(conn)
    finally:
        conn.close()
    assert preview["status"] == "blocked"
    assert "link:361:invalid_signature_mismatch:origin" in preview["blockers"]


def test_preview_blocks_redundant_target_without_older_copy() -> None:
    conn = _db()
    try:
        target = REDUNDANT_LINK_IDS[0]
        row = conn.execute("SELECT from_memory_id,to_memory_id FROM memory_links WHERE id=?", (target,)).fetchone()
        conn.execute(
            "DELETE FROM memory_links WHERE id<? AND from_memory_id=? AND to_memory_id=? AND relation_type='dream'",
            (target, row["from_memory_id"], row["to_memory_id"]),
        )
        conn.commit()
        preview = build_preview(conn)
    finally:
        conn.close()
    assert preview["status"] == "blocked"
    assert f"link:{target}:older_active_exact_duplicate_missing" in preview["blockers"]


def test_apply_archives_only_targets_and_keeps_preserved_edges(tmp_path: Path) -> None:
    db_path = tmp_path / "source.db"
    conn = _db(db_path)
    backup_path = tmp_path / "backup.db"
    _backup(conn, backup_path)
    try:
        preview = build_preview(conn)
        before_total = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
        result = apply(
            conn,
            expected_preview_hash=preview["preview_hash"],
            backup_path=str(backup_path),
            applied_by="pytest",
            reason="archive exact R6B P0/P1 targets",
            confirm_data_repair=True,
            now_iso="2026-08-16T01:00:00Z",
        )
        after_total = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
        active_targets = conn.execute(
            "SELECT COUNT(*) FROM memory_links WHERE archived_at IS NULL AND id IN (%s)" % ",".join("?" for _ in ALL_TARGET_LINK_IDS),
            ALL_TARGET_LINK_IDS,
        ).fetchone()[0]
        archived_targets = conn.execute(
            "SELECT COUNT(*) FROM memory_links WHERE archived_at='2026-08-16T01:00:00Z' AND id IN (%s)" % ",".join("?" for _ in ALL_TARGET_LINK_IDS),
            ALL_TARGET_LINK_IDS,
        ).fetchone()[0]
        event_count = conn.execute("SELECT COUNT(*) FROM memory_events WHERE event_type='memory.link.archived_r6b'").fetchone()[0]
        preserved_active = []
        for item in preview["candidates"]:
            if item["classification"] == "redundant":
                preserved_active.append(
                    conn.execute("SELECT archived_at FROM memory_links WHERE id=?", (int(item["preserved_link"]["id"]),)).fetchone()[0]
                )
    finally:
        conn.close()

    assert result["status"] == "applied"
    assert result["repair_key"] == REPAIR_KEY
    assert len(result["archived"]) == 26
    assert len(result["event_ids"]) == 26
    assert before_total == after_total
    assert active_targets == 0
    assert archived_targets == 26
    assert event_count == 26
    assert preserved_active == [None] * 24


def test_apply_requires_confirmation_fresh_hash_and_backup(tmp_path: Path) -> None:
    conn = _db()
    backup_path = tmp_path / "backup.db"
    _backup(conn, backup_path)
    try:
        preview = build_preview(conn)
        blocked = apply(
            conn,
            expected_preview_hash=preview["preview_hash"],
            backup_path=str(backup_path),
            applied_by="pytest",
            reason="test",
            confirm_data_repair=False,
            now_iso="2026-08-16T01:00:00Z",
        )
        stale = apply(
            conn,
            expected_preview_hash="deadbeef",
            backup_path=str(backup_path),
            applied_by="pytest",
            reason="test",
            confirm_data_repair=True,
            now_iso="2026-08-16T01:00:00Z",
        )
    finally:
        conn.close()
    assert blocked["status"] == "blocked"
    assert blocked["blocking_reasons"] == ["confirm_data_repair_required"]
    assert stale["status"] == "stale_preview"


def test_rollback_restores_only_archived_at_and_keeps_audit_events(tmp_path: Path) -> None:
    db_path = tmp_path / "source.db"
    conn = _db(db_path)
    backup_path = tmp_path / "backup.db"
    _backup(conn, backup_path)
    try:
        preview = build_preview(conn)
        applied = apply(
            conn,
            expected_preview_hash=preview["preview_hash"],
            backup_path=str(backup_path),
            applied_by="pytest",
            reason="test rollback",
            confirm_data_repair=True,
            now_iso="2026-08-16T01:00:00Z",
        )
        rollback_preview = build_rollback_preview(conn)
        result = rollback(
            conn,
            expected_rollback_preview_hash=rollback_preview["rollback_preview_hash"],
            rolled_back_by="pytest",
            notes="restore exact edge activity",
            now_iso="2026-08-16T01:01:00Z",
        )
        active_targets = conn.execute(
            "SELECT COUNT(*) FROM memory_links WHERE archived_at IS NULL AND id IN (%s)" % ",".join("?" for _ in ALL_TARGET_LINK_IDS),
            ALL_TARGET_LINK_IDS,
        ).fetchone()[0]
        archive_events = conn.execute("SELECT COUNT(*) FROM memory_events WHERE event_type='memory.link.archived_r6b'").fetchone()[0]
        rollback_events = conn.execute("SELECT COUNT(*) FROM memory_events WHERE event_type='memory.link.archive_r6b_rolled_back'").fetchone()[0]
    finally:
        conn.close()

    assert applied["status"] == "applied"
    assert rollback_preview["status"] == "preview_ready"
    assert result["status"] == "rolled_back"
    assert active_targets == 26
    assert archive_events == 26
    assert rollback_events == 26


def test_apply_rejects_missing_backup(tmp_path: Path) -> None:
    conn = _db()
    try:
        preview = build_preview(conn)
        try:
            apply(
                conn,
                expected_preview_hash=preview["preview_hash"],
                backup_path=str(tmp_path / "missing.db"),
                applied_by="pytest",
                reason="test",
                confirm_data_repair=True,
                now_iso="2026-08-16T01:00:00Z",
            )
        except ValueError as exc:
            error = str(exc)
        else:
            error = None
    finally:
        conn.close()
    assert error == "backup_path_missing"
