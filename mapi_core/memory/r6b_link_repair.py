from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = "mapi_r6b_link_repair.v1"
ROLLBACK_SCHEMA = "mapi_r6b_link_repair_rollback.v1"
REPAIR_KEY = "r6b_legacy_graph_p0_p1_archive_2026_08_16_v1"

INVALID_LINK_SPECS: tuple[dict[str, Any], ...] = (
    {"link_id": 361, "from_memory_id": 201, "to_memory_id": 249, "relation_type": "same_project", "origin": "sandman_v1_dream"},
    {"link_id": 523, "from_memory_id": 290, "to_memory_id": 67, "relation_type": "supports", "origin": "sandman_agent"},
)

REDUNDANT_LINK_IDS: tuple[int, ...] = (
    1180, 1205, 1206, 1221, 1238, 1254, 1257, 1294, 1304, 1342, 1353, 1361,
    1363, 1377, 1390, 1391, 1394, 1403, 1453, 1465, 1466, 1483, 1563, 1568,
)

ALL_TARGET_LINK_IDS = tuple(spec["link_id"] for spec in INVALID_LINK_SPECS) + REDUNDANT_LINK_IDS


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row(conn: sqlite3.Connection, link_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id,from_memory_id,to_memory_id,relation_type,weight,origin,created_at,archived_at,workspace_id,visibility_scope FROM memory_links WHERE id=?",
        (int(link_id),),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return {
        "id": row[0], "from_memory_id": row[1], "to_memory_id": row[2], "relation_type": row[3],
        "weight": row[4], "origin": row[5], "created_at": row[6], "archived_at": row[7],
        "workspace_id": row[8], "visibility_scope": row[9],
    }


def _preserved_duplicate(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any] | None:
    preserved = conn.execute(
        """
        SELECT id,from_memory_id,to_memory_id,relation_type,origin,created_at,archived_at
        FROM memory_links
        WHERE archived_at IS NULL AND from_memory_id=? AND to_memory_id=? AND relation_type=? AND id<?
        ORDER BY id ASC LIMIT 1
        """,
        (int(row["from_memory_id"]), int(row["to_memory_id"]), str(row["relation_type"]), int(row["id"])),
    ).fetchone()
    if preserved is None:
        return None
    if isinstance(preserved, sqlite3.Row):
        return dict(preserved)
    return {
        "id": preserved[0], "from_memory_id": preserved[1], "to_memory_id": preserved[2],
        "relation_type": preserved[3], "origin": preserved[4], "created_at": preserved[5], "archived_at": preserved[6],
    }


def build_preview(conn: sqlite3.Connection) -> dict[str, Any]:
    blockers: list[str] = []
    candidates: list[dict[str, Any]] = []
    invalid_by_id = {int(spec["link_id"]): spec for spec in INVALID_LINK_SPECS}
    target_set = set(ALL_TARGET_LINK_IDS)
    if len(target_set) != len(ALL_TARGET_LINK_IDS):
        blockers.append("target_link_set_not_unique")

    for link_id in ALL_TARGET_LINK_IDS:
        row = _row(conn, link_id)
        item: dict[str, Any] = {
            "link_id": int(link_id),
            "classification": "invalid" if link_id in invalid_by_id else "redundant",
            "current": row,
            "blockers": [],
            "preserved_link": None,
        }
        item_blockers: list[str] = []
        if row is None:
            item_blockers.append("link_missing")
        else:
            if row.get("archived_at") is not None:
                item_blockers.append("link_already_archived")
            if link_id in invalid_by_id:
                expected = invalid_by_id[link_id]
                for field in ("from_memory_id", "to_memory_id", "relation_type", "origin"):
                    if row.get(field) != expected[field]:
                        item_blockers.append(f"invalid_signature_mismatch:{field}")
            else:
                if row.get("relation_type") != "dream":
                    item_blockers.append("redundant_target_not_dream")
                preserved = _preserved_duplicate(conn, row)
                item["preserved_link"] = preserved
                if preserved is None:
                    item_blockers.append("older_active_exact_duplicate_missing")
        item["blockers"] = item_blockers
        blockers.extend(f"link:{link_id}:{code}" for code in item_blockers)
        candidates.append(item)

    plan = {
        "schema": SCHEMA,
        "repair_key": REPAIR_KEY,
        "invalid_link_ids": [int(spec["link_id"]) for spec in INVALID_LINK_SPECS],
        "redundant_link_ids": list(REDUNDANT_LINK_IDS),
        "all_target_link_ids": list(ALL_TARGET_LINK_IDS),
        "mutation": "set_archived_at_only_plus_append_only_memory_events",
        "delete_links": False,
    }
    fingerprint = {
        "plan": plan,
        "candidates": candidates,
        "status": "preview_ready" if not blockers else "blocked",
        "blockers": blockers,
    }
    return {
        "status": "preview_ready" if not blockers else "blocked",
        "schema": SCHEMA,
        "repair_key": REPAIR_KEY,
        "plan": plan,
        "candidates": candidates,
        "blockers": blockers,
        "preview_hash": _hash(fingerprint),
        "hash_algorithm": "sha256:canonical-json:v1",
        "safety": {
            "read_only": True,
            "mutations_performed": 0,
            "backup_required": True,
            "rollback_supported": True,
            "physical_delete": False,
        },
    }


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


def apply(
    conn: sqlite3.Connection,
    *,
    expected_preview_hash: str,
    backup_path: str,
    applied_by: str,
    reason: str,
    confirm_data_repair: bool,
    now_iso: str,
) -> dict[str, Any]:
    if not confirm_data_repair:
        return {"status": "blocked", "schema": SCHEMA, "blocking_reasons": ["confirm_data_repair_required"]}
    backup = _verify_backup(backup_path)
    preview = build_preview(conn)
    if preview["status"] != "preview_ready":
        return {"status": "blocked", "schema": SCHEMA, "blocking_reasons": preview["blockers"], "preview_hash": preview["preview_hash"]}
    if str(expected_preview_hash) != str(preview["preview_hash"]):
        return {
            "status": "stale_preview", "schema": SCHEMA,
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
            row = item["current"]
            cursor = conn.execute(
                """
                UPDATE memory_links SET archived_at=?
                WHERE id=? AND archived_at IS NULL AND from_memory_id=? AND to_memory_id=? AND relation_type=? AND COALESCE(origin,'')=COALESCE(?, '')
                """,
                (
                    now_iso, int(row["id"]), int(row["from_memory_id"]), int(row["to_memory_id"]),
                    str(row["relation_type"]), row.get("origin"),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"link_changed:{row['id']}")
            payload = {
                "repair_key": REPAIR_KEY,
                "link_id": int(row["id"]),
                "classification": item["classification"],
                "from_memory_id": int(row["from_memory_id"]),
                "to_memory_id": int(row["to_memory_id"]),
                "relation_type": str(row["relation_type"]),
                "origin": row.get("origin"),
                "preserved_link_id": None if item.get("preserved_link") is None else int(item["preserved_link"]["id"]),
                "preview_hash": preview["preview_hash"],
                "backup_path": backup["path"],
                "backup_sha256": backup["sha256"],
                "applied_by": actor,
                "reason": why,
            }
            cur = conn.execute(
                "INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (int(row["from_memory_id"]), "memory.link.archived_r6b", json.dumps(payload, ensure_ascii=False, sort_keys=True), now_iso),
            )
            event_ids.append(int(cur.lastrowid))
            archived.append({
                "link_id": int(row["id"]),
                "classification": item["classification"],
                "preserved_link_id": payload["preserved_link_id"],
            })
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


def build_rollback_preview(conn: sqlite3.Connection) -> dict[str, Any]:
    blockers: list[str] = []
    candidates: list[dict[str, Any]] = []
    for link_id in ALL_TARGET_LINK_IDS:
        row = _row(conn, link_id)
        item_blockers: list[str] = []
        if row is None:
            item_blockers.append("link_missing")
        elif row.get("archived_at") is None:
            item_blockers.append("link_not_archived")
        event = conn.execute(
            "SELECT id FROM memory_events WHERE event_type='memory.link.archived_r6b' AND payload_json LIKE ? ORDER BY id DESC LIMIT 1",
            (f'%"link_id": {int(link_id)}%',),
        ).fetchone()
        if event is None:
            item_blockers.append("archive_event_missing")
        blockers.extend(f"link:{link_id}:{code}" for code in item_blockers)
        candidates.append({
            "link_id": int(link_id),
            "current": row,
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
    conn: sqlite3.Connection,
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
            row = item["current"]
            archived_at = row.get("archived_at")
            cursor = conn.execute(
                "UPDATE memory_links SET archived_at=NULL WHERE id=? AND archived_at=?",
                (int(row["id"]), archived_at),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"rollback_link_changed:{row['id']}")
            payload = {
                "repair_key": REPAIR_KEY,
                "link_id": int(row["id"]),
                "rolled_back_by": actor,
                "notes": note,
                "rollback_preview_hash": preview["rollback_preview_hash"],
                "previous_archived_at": archived_at,
            }
            cur = conn.execute(
                "INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (int(row["from_memory_id"]), "memory.link.archive_r6b_rolled_back", json.dumps(payload, ensure_ascii=False, sort_keys=True), now_iso),
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
