from __future__ import annotations

import sqlite3
from pathlib import Path

from mapi_core.memory.r6c_truth_archive import (
    ARCHIVE_LINK_IDS,
    REPAIR_KEY,
    apply,
    build_preview,
    build_rollback_preview,
    rollback,
)


_SUPPORT_IDS = {
    6, 7, 60, 61, 62, 63, 64, 65, 74, 75, 80, 84, 88, 89, 90, 94, 95, 96,
    114, 115, 116, 126, 127, 128, 182, 183, 189, 216, 498, 502, 506, 510, 511,
}


def _row_to_dict(row):
    return dict(row)


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            project_key TEXT,
            scope_code TEXT,
            workspace_id INTEGER,
            state_code TEXT,
            memory_v2_status TEXT,
            activity_state TEXT,
            archived_at TEXT,
            source_event_ref TEXT
        );
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
    memory_ids: set[int] = set()
    for link_id in ARCHIVE_LINK_IDS:
        from_id = 10000 + int(link_id) * 2
        to_id = from_id + 1
        memory_ids.update((from_id, to_id))
        if link_id in _SUPPORT_IDS:
            relation = "supports"
            origin = "consolidation_v1_auto"
        else:
            relation = "contradicts"
            origin = "manual_fallback_after_run_conflicts_error" if link_id == 3 else "conflicts_v1_auto"
        conn.execute(
            "INSERT INTO memory_links(id,from_memory_id,to_memory_id,relation_type,weight,origin,created_at,archived_at,workspace_id,visibility_scope) VALUES(?,?,?,?,1.0,?,'2026-04-01T00:00:00Z',NULL,1,'inherited')",
            (int(link_id), from_id, to_id, relation, origin),
        )
    for memory_id in sorted(memory_ids):
        conn.execute(
            "INSERT INTO memories(id,project_key,scope_code,workspace_id,state_code,memory_v2_status,activity_state,archived_at,source_event_ref) VALUES(?, 'jagoda-memory-api','project',1,'archived','archived','archived','2026-05-01T00:00:00Z',NULL)",
            (memory_id,),
        )
    conn.commit()
    return conn


def _backup(source: sqlite3.Connection, path: Path) -> None:
    dest = sqlite3.connect(str(path))
    try:
        source.backup(dest)
    finally:
        dest.close()


def test_preview_requires_exact_76_high_confidence_archive_set() -> None:
    conn = _db()
    try:
        first = build_preview(conn, row_to_dict=_row_to_dict)
        second = build_preview(conn, row_to_dict=_row_to_dict)
    finally:
        conn.close()

    assert first["status"] == "preview_ready"
    assert first["preview_hash"] == second["preview_hash"]
    assert first["plan"]["archive_link_ids"] == list(ARCHIVE_LINK_IDS)
    assert first["plan"]["archive_count"] == 76
    assert first["review_summary"]["archive_from_active_truth_count"] == 76
    assert len(first["candidates"]) == 76
    assert first["safety"]["physical_delete"] is False
    assert first["safety"]["semantic_similarity_used"] is False
    assert first["safety"]["content_used_for_classification"] is False


def test_preview_blocks_if_review_recommendation_changes() -> None:
    conn = _db()
    try:
        support_id = min(_SUPPORT_IDS)
        link = conn.execute("SELECT from_memory_id FROM memory_links WHERE id=?", (support_id,)).fetchone()
        conn.execute(
            "INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES(?, 'memory_v3.capture_reinforced', '{}', '2026-08-16T00:00:00Z')",
            (int(link["from_memory_id"]),),
        )
        conn.commit()
        preview = build_preview(conn, row_to_dict=_row_to_dict)
    finally:
        conn.close()

    assert preview["status"] == "blocked"
    assert "archive_candidate_set_mismatch" in preview["blockers"]
    assert any("recommendation_changed" in blocker for blocker in preview["blockers"])


def test_apply_archives_only_target_edges_and_appends_audit_events(tmp_path: Path) -> None:
    db_path = tmp_path / "source.db"
    conn = _db(db_path)
    backup_path = tmp_path / "backup.db"
    _backup(conn, backup_path)
    try:
        preview = build_preview(conn, row_to_dict=_row_to_dict)
        total_before = int(conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0])
        result = apply(
            conn,
            expected_preview_hash=preview["preview_hash"],
            backup_path=str(backup_path),
            applied_by="pytest",
            reason="R6C exact high-confidence truth archive",
            confirm_data_repair=True,
            now_iso="2026-08-16T00:30:00Z",
            row_to_dict=_row_to_dict,
        )
        total_after = int(conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0])
        active_targets = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_links WHERE archived_at IS NULL AND id IN (%s)" % ",".join("?" for _ in ARCHIVE_LINK_IDS),
                ARCHIVE_LINK_IDS,
            ).fetchone()[0]
        )
        archive_events = int(conn.execute("SELECT COUNT(*) FROM memory_events WHERE event_type='memory.link.archived_r6c_truth_review'").fetchone()[0])
    finally:
        conn.close()

    assert result["status"] == "applied"
    assert result["repair_key"] == REPAIR_KEY
    assert len(result["archived"]) == 76
    assert len(result["event_ids"]) == 76
    assert total_before == total_after
    assert active_targets == 0
    assert archive_events == 76


def test_apply_requires_confirmation_fresh_hash_and_backup(tmp_path: Path) -> None:
    conn = _db()
    backup_path = tmp_path / "backup.db"
    _backup(conn, backup_path)
    try:
        preview = build_preview(conn, row_to_dict=_row_to_dict)
        blocked = apply(
            conn,
            expected_preview_hash=preview["preview_hash"],
            backup_path=str(backup_path),
            applied_by="pytest",
            reason="test",
            confirm_data_repair=False,
            now_iso="2026-08-16T00:30:00Z",
            row_to_dict=_row_to_dict,
        )
        stale = apply(
            conn,
            expected_preview_hash="deadbeef",
            backup_path=str(backup_path),
            applied_by="pytest",
            reason="test",
            confirm_data_repair=True,
            now_iso="2026-08-16T00:30:00Z",
            row_to_dict=_row_to_dict,
        )
    finally:
        conn.close()

    assert blocked["status"] == "blocked"
    assert blocked["blocking_reasons"] == ["confirm_data_repair_required"]
    assert stale["status"] == "stale_preview"
    assert stale["blocking_reasons"] == ["expected_preview_hash_mismatch"]


def test_rollback_restores_archived_edges_and_keeps_audit_history(tmp_path: Path) -> None:
    db_path = tmp_path / "source.db"
    conn = _db(db_path)
    backup_path = tmp_path / "backup.db"
    _backup(conn, backup_path)
    try:
        preview = build_preview(conn, row_to_dict=_row_to_dict)
        applied = apply(
            conn,
            expected_preview_hash=preview["preview_hash"],
            backup_path=str(backup_path),
            applied_by="pytest",
            reason="rollback test",
            confirm_data_repair=True,
            now_iso="2026-08-16T00:30:00Z",
            row_to_dict=_row_to_dict,
        )
        rollback_preview = build_rollback_preview(conn)
        rolled = rollback(
            conn,
            expected_rollback_preview_hash=rollback_preview["rollback_preview_hash"],
            rolled_back_by="pytest",
            notes="restore exact active truth state",
            now_iso="2026-08-16T00:31:00Z",
        )
        active_targets = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_links WHERE archived_at IS NULL AND id IN (%s)" % ",".join("?" for _ in ARCHIVE_LINK_IDS),
                ARCHIVE_LINK_IDS,
            ).fetchone()[0]
        )
        archive_events = int(conn.execute("SELECT COUNT(*) FROM memory_events WHERE event_type='memory.link.archived_r6c_truth_review'").fetchone()[0])
        rollback_events = int(conn.execute("SELECT COUNT(*) FROM memory_events WHERE event_type='memory.link.archive_r6c_rolled_back'").fetchone()[0])
    finally:
        conn.close()

    assert applied["status"] == "applied"
    assert rollback_preview["status"] == "preview_ready"
    assert rolled["status"] == "rolled_back"
    assert active_targets == 76
    assert archive_events == 76
    assert rollback_events == 76


def test_apply_rejects_missing_backup(tmp_path: Path) -> None:
    conn = _db()
    try:
        preview = build_preview(conn, row_to_dict=_row_to_dict)
        try:
            apply(
                conn,
                expected_preview_hash=preview["preview_hash"],
                backup_path=str(tmp_path / "missing.db"),
                applied_by="pytest",
                reason="test",
                confirm_data_repair=True,
                now_iso="2026-08-16T00:30:00Z",
                row_to_dict=_row_to_dict,
            )
        except ValueError as exc:
            error = str(exc)
        else:
            error = None
    finally:
        conn.close()
    assert error == "backup_path_missing"
