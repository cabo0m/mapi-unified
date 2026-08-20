from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from mapi_core.memory.canonical_truth_review import build_canonical_truth_review_payload


SCHEMA = "mapi_r6c_truth_archive.v1"
ROLLBACK_SCHEMA = "mapi_r6c_truth_archive_rollback.v1"
REPAIR_KEY = "r6c_high_confidence_truth_archive_2026_08_16_v1"
ARCHIVE_LINK_IDS: tuple[int, ...] = (
    3, 6, 7, 8, 9, 10, 11, 60, 61, 62, 63, 64, 65, 74, 75, 80, 84, 88, 89,
    90, 94, 95, 96, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114,
    115, 116, 126, 127, 128, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156,
    157, 158, 159, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172,
    182, 183, 189, 216, 498, 502, 506, 510, 511,
)


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _verify_backup(path: str) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.is_file():
        raise ValueError("backup_path_missing")
    conn = sqlite3.connect(str(candidate))
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    if quick != "ok" or fk:
        raise ValueError("backup_integrity_failed")
    return {
        "path": str(candidate),
        "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "quick_check": quick,
        "fk_violations": len(fk),
    }


def _link_snapshot(conn: Any, link_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id,from_memory_id,to_memory_id,relation_type,weight,origin,created_at,archived_at,workspace_id,visibility_scope FROM memory_links WHERE id=?",
        (int(link_id),),
    ).fetchone()
    if row is None:
        return None
    return dict(row) if isinstance(row, sqlite3.Row) else {
        "id": row[0], "from_memory_id": row[1], "to_memory_id": row[2], "relation_type": row[3],
        "weight": row[4], "origin": row[5], "created_at": row[6], "archived_at": row[7],
        "workspace_id": row[8], "visibility_scope": row[9],
    }


def build_preview(conn: Any, *, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    review = build_canonical_truth_review_payload(
        conn,
        project_key=None,
        include_items=True,
        sample_limit=1000,
        row_to_dict=row_to_dict,
    )
    archive_ids = tuple(int(value) for value in review["recommendation_ids"]["archive_from_active_truth"])
    blockers: list[str] = []
    if archive_ids != ARCHIVE_LINK_IDS:
        blockers.append("archive_candidate_set_mismatch")
    review_by_id = {int(item["link_id"]): item for item in review["items"]}
    candidates: list[dict[str, Any]] = []
    for link_id in ARCHIVE_LINK_IDS:
        item = review_by_id.get(int(link_id))
        link = _link_snapshot(conn, int(link_id))
        item_blockers: list[str] = []
        if item is None:
            item_blockers.append("review_item_missing")
        else:
            if item.get("recommendation") != "archive_from_active_truth":
                item_blockers.append("recommendation_changed")
            if item.get("confidence") != "high":
                item_blockers.append("confidence_not_high")
        if link is None:
            item_blockers.append("link_missing")
        elif link.get("archived_at") is not None:
            item_blockers.append("link_already_archived")
        blockers.extend(f"link:{link_id}:{code}" for code in item_blockers)
        candidates.append({
            "link_id": int(link_id),
            "review": item,
            "link": link,
            "blockers": item_blockers,
        })
    plan = {
        "schema": SCHEMA,
        "repair_key": REPAIR_KEY,
        "archive_link_ids": list(ARCHIVE_LINK_IDS),
        "archive_count": len(ARCHIVE_LINK_IDS),
        "preserve_legacy_lineage_ids": list(review["recommendation_ids"]["preserve_legacy_lineage"]),
        "operator_review_ids": list(review["recommendation_ids"]["requires_operator_review"]),
        "mutation": "set_archived_at_only_plus_append_only_memory_events",
        "physical_delete": False,
    }
    fingerprint_payload = {
        "plan": plan,
        "candidates": candidates,
        "review_summary": review["summary"],
        "status": "preview_ready" if not blockers else "blocked",
        "blockers": blockers,
    }
    return {
        "status": "preview_ready" if not blockers else "blocked",
        "schema": SCHEMA,
        "repair_key": REPAIR_KEY,
        "plan": plan,
        "review_summary": review["summary"],
        "candidates": candidates,
        "blockers": blockers,
        "preview_hash": _hash(fingerprint_payload),
        "hash_algorithm": "sha256:canonical-json:v1",
        "safety": {
            "read_only": True,
            "mutations_performed": 0,
            "backup_required": True,
            "rollback_supported": True,
            "physical_delete": False,
            "semantic_similarity_used": False,
            "content_used_for_classification": False,
        },
    }


def apply(
    conn: Any,
    *,
    expected_preview_hash: str,
    backup_path: str,
    applied_by: str,
    reason: str,
    confirm_data_repair: bool,
    now_iso: str,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if not confirm_data_repair:
        return {"status": "blocked", "schema": SCHEMA, "blocking_reasons": ["confirm_data_repair_required"]}
    backup = _verify_backup(backup_path)
    preview = build_preview(conn, row_to_dict=row_to_dict)
    if preview["status"] != "preview_ready":
        return {"status": "blocked", "schema": SCHEMA, "blocking_reasons": preview["blockers"], "preview_hash": preview["preview_hash"]}
    if str(expected_preview_hash) != str(preview["preview_hash"]):
        return {
            "status": "stale_preview",
            "schema": SCHEMA,
            "blocking_reasons": ["expected_preview_hash_mismatch"],
            "expected_preview_hash": str(expected_preview_hash),
            "current_preview_hash": str(preview["preview_hash"]),
        }
    actor = str(applied_by or "").strip()
    why = str(reason or "").strip()
    if not actor or not why:
        return {"status": "blocked", "schema": SCHEMA, "blocking_reasons": ["applied_by_and_reason_required"]}

    archived: list[dict[str, Any]] = []
    event_ids: list[int] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in preview["candidates"]:
            link = item["link"]
            cursor = conn.execute(
                """
                UPDATE memory_links SET archived_at=?
                WHERE id=? AND archived_at IS NULL AND from_memory_id=? AND to_memory_id=?
                  AND relation_type=? AND COALESCE(origin,'')=COALESCE(?, '')
                """,
                (
                    now_iso, int(link["id"]), int(link["from_memory_id"]), int(link["to_memory_id"]),
                    str(link["relation_type"]), link.get("origin"),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"link_changed:{link['id']}")
            review_item = item["review"] or {}
            payload = {
                "repair_key": REPAIR_KEY,
                "link_id": int(link["id"]),
                "from_memory_id": int(link["from_memory_id"]),
                "to_memory_id": int(link["to_memory_id"]),
                "relation_type": str(link["relation_type"]),
                "origin": link.get("origin"),
                "recommendation": review_item.get("recommendation"),
                "confidence": review_item.get("confidence"),
                "reason_codes": list(review_item.get("reason_codes") or []),
                "consumer_impact": review_item.get("consumer_impact"),
                "preview_hash": preview["preview_hash"],
                "backup_path": backup["path"],
                "backup_sha256": backup["sha256"],
                "applied_by": actor,
                "reason": why,
            }
            cur = conn.execute(
                "INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (int(link["from_memory_id"]), "memory.link.archived_r6c_truth_review", json.dumps(payload, ensure_ascii=False, sort_keys=True), now_iso),
            )
            event_ids.append(int(cur.lastrowid))
            archived.append({"link_id": int(link["id"]), "relation_type": link["relation_type"]})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "status": "applied",
        "schema": SCHEMA,
        "repair_key": REPAIR_KEY,
        "preview_hash": preview["preview_hash"],
        "backup": backup,
        "archived": archived,
        "event_ids": event_ids,
        "archived_at": now_iso,
        "rollback_available": True,
    }


def build_rollback_preview(conn: Any) -> dict[str, Any]:
    blockers: list[str] = []
    candidates: list[dict[str, Any]] = []
    for link_id in ARCHIVE_LINK_IDS:
        link = _link_snapshot(conn, int(link_id))
        item_blockers: list[str] = []
        if link is None:
            item_blockers.append("link_missing")
        elif link.get("archived_at") is None:
            item_blockers.append("link_not_archived")
        event = conn.execute(
            "SELECT id FROM memory_events WHERE event_type='memory.link.archived_r6c_truth_review' AND payload_json LIKE ? ORDER BY id DESC LIMIT 1",
            (f'%"link_id": {int(link_id)}%',),
        ).fetchone()
        if event is None:
            item_blockers.append("archive_event_missing")
        blockers.extend(f"link:{link_id}:{code}" for code in item_blockers)
        candidates.append({
            "link_id": int(link_id),
            "current": link,
            "archive_event_id": None if event is None else int(event[0]),
            "blockers": item_blockers,
        })
    payload = {
        "schema": ROLLBACK_SCHEMA,
        "repair_key": REPAIR_KEY,
        "candidates": candidates,
        "status": "preview_ready" if not blockers else "blocked",
        "blockers": blockers,
    }
    return {
        **payload,
        "rollback_preview_hash": _hash(payload),
        "hash_algorithm": "sha256:canonical-json:v1",
        "safety": {"read_only": True, "mutations_performed": 0, "restores_archived_at_to_null": True},
    }


def rollback(
    conn: Any,
    *,
    expected_rollback_preview_hash: str,
    rolled_back_by: str,
    notes: str,
    now_iso: str,
) -> dict[str, Any]:
    preview = build_rollback_preview(conn)
    if preview["status"] != "preview_ready":
        return {"status": "blocked", "schema": ROLLBACK_SCHEMA, "blocking_reasons": preview["blockers"]}
    if str(expected_rollback_preview_hash) != str(preview["rollback_preview_hash"]):
        return {
            "status": "stale_rollback_preview", "schema": ROLLBACK_SCHEMA,
            "blocking_reasons": ["expected_rollback_preview_hash_mismatch"],
            "current_rollback_preview_hash": preview["rollback_preview_hash"],
        }
    actor = str(rolled_back_by or "").strip()
    note = str(notes or "").strip()
    if not actor or not note:
        return {"status": "blocked", "schema": ROLLBACK_SCHEMA, "blocking_reasons": ["rolled_back_by_and_notes_required"]}
    event_ids: list[int] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in preview["candidates"]:
            link = item["current"]
            archived_at = link.get("archived_at")
            cursor = conn.execute(
                "UPDATE memory_links SET archived_at=NULL WHERE id=? AND archived_at=?",
                (int(link["id"]), archived_at),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"rollback_link_changed:{link['id']}")
            payload = {
                "repair_key": REPAIR_KEY,
                "link_id": int(link["id"]),
                "rolled_back_by": actor,
                "notes": note,
                "rollback_preview_hash": preview["rollback_preview_hash"],
                "previous_archived_at": archived_at,
            }
            cur = conn.execute(
                "INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (int(link["from_memory_id"]), "memory.link.archive_r6c_rolled_back", json.dumps(payload, ensure_ascii=False, sort_keys=True), now_iso),
            )
            event_ids.append(int(cur.lastrowid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "status": "rolled_back",
        "schema": ROLLBACK_SCHEMA,
        "repair_key": REPAIR_KEY,
        "rollback_preview_hash": preview["rollback_preview_hash"],
        "event_ids": event_ids,
    }
