from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = "mapi_r6c_manual_support_repair.v1"
ROLLBACK_SCHEMA = "mapi_r6c_manual_support_repair_rollback.v1"
REPAIR_KEY = "r6c_manual_support_archive_2026_08_16_v1"
TARGETS: tuple[dict[str, Any], ...] = (
    {"link_id": 521, "from_memory_id": 290, "to_memory_id": 144, "relation_type": "supports", "origin": "manual_forced_linking_pass"},
    {"link_id": 522, "from_memory_id": 290, "to_memory_id": 145, "relation_type": "supports", "origin": "manual_forced_linking_pass"},
)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _link(conn: Any, link_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id,from_memory_id,to_memory_id,relation_type,weight,origin,created_at,archived_at,workspace_id,visibility_scope FROM memory_links WHERE id=?",
        (int(link_id),),
    ).fetchone()
    return None if row is None else dict(row)


def _memory(conn: Any, memory_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id,project_key,scope_code,memory_type,summary_short,title,tags,source,state_code,memory_v2_status,archived_at FROM memories WHERE id=?",
        (int(memory_id),),
    ).fetchone()
    return None if row is None else dict(row)


def build_preview(conn: Any) -> dict[str, Any]:
    blockers: list[str] = []
    candidates: list[dict[str, Any]] = []
    for spec in TARGETS:
        link = _link(conn, int(spec["link_id"]))
        item_blockers: list[str] = []
        if link is None:
            item_blockers.append("link_missing")
        else:
            for field in ("from_memory_id", "to_memory_id", "relation_type", "origin"):
                if link.get(field) != spec[field]:
                    item_blockers.append(f"signature_mismatch:{field}")
            if link.get("archived_at") is not None:
                item_blockers.append("already_archived")
        source = _memory(conn, int(spec["from_memory_id"]))
        target = _memory(conn, int(spec["to_memory_id"]))
        if source is None or target is None:
            item_blockers.append("memory_endpoint_missing")
        blockers.extend(f"link:{spec['link_id']}:{code}" for code in item_blockers)
        candidates.append({
            "link_id": int(spec["link_id"]),
            "link": link,
            "from_memory": source,
            "to_memory": target,
            "manual_review": {
                "decision": "archive_from_active_truth",
                "reason_codes": [
                    "manual_support_lacks_current_evidence_contract",
                    "relation_direction_not_supported_by_current_contract",
                    "more_specific_non_truth_links_already_preserve_context",
                ],
                "review_note": "#290 is an MAPI graph/link-array requirement while #144/#145 document Sandman Agent V2/link tooling. They are related, but 290->supports->144/145 is not a current evidence-bound support assertion.",
            },
            "blockers": item_blockers,
        })
    payload = {
        "schema": SCHEMA,
        "repair_key": REPAIR_KEY,
        "targets": candidates,
        "status": "preview_ready" if not blockers else "blocked",
        "blockers": blockers,
        "mutation": "set_archived_at_only_plus_append_only_memory_events",
    }
    return {
        **payload,
        "preview_hash": _hash(payload),
        "hash_algorithm": "sha256:canonical-json:v1",
        "safety": {"read_only": True, "mutations_performed": 0, "physical_delete": False, "rollback_supported": True},
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
    return {"path": str(candidate), "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(), "quick_check": quick, "fk_violations": len(fk)}


def apply(
    conn: Any,
    *,
    expected_preview_hash: str,
    backup_path: str,
    applied_by: str,
    reason: str,
    confirm_manual_review: bool,
    now_iso: str,
) -> dict[str, Any]:
    if not confirm_manual_review:
        return {"status": "blocked", "schema": SCHEMA, "blocking_reasons": ["confirm_manual_review_required"]}
    backup = _verify_backup(backup_path)
    preview = build_preview(conn)
    if preview["status"] != "preview_ready":
        return {"status": "blocked", "schema": SCHEMA, "blocking_reasons": preview["blockers"]}
    if str(expected_preview_hash) != str(preview["preview_hash"]):
        return {"status": "stale_preview", "schema": SCHEMA, "blocking_reasons": ["expected_preview_hash_mismatch"], "current_preview_hash": preview["preview_hash"]}
    actor = str(applied_by or "").strip()
    why = str(reason or "").strip()
    if not actor or not why:
        return {"status": "blocked", "schema": SCHEMA, "blocking_reasons": ["applied_by_and_reason_required"]}
    event_ids: list[int] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in preview["targets"]:
            link = item["link"]
            cursor = conn.execute(
                "UPDATE memory_links SET archived_at=? WHERE id=? AND archived_at IS NULL AND from_memory_id=? AND to_memory_id=? AND relation_type=? AND origin=?",
                (now_iso, int(link["id"]), int(link["from_memory_id"]), int(link["to_memory_id"]), link["relation_type"], link["origin"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"link_changed:{link['id']}")
            payload = {
                "repair_key": REPAIR_KEY,
                "link_id": int(link["id"]),
                "decision": item["manual_review"]["decision"],
                "reason_codes": item["manual_review"]["reason_codes"],
                "review_note": item["manual_review"]["review_note"],
                "preview_hash": preview["preview_hash"],
                "backup_path": backup["path"],
                "backup_sha256": backup["sha256"],
                "applied_by": actor,
                "reason": why,
            }
            cur = conn.execute(
                "INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (int(link["from_memory_id"]), "memory.link.archived_r6c_manual_review", json.dumps(payload, ensure_ascii=False, sort_keys=True), now_iso),
            )
            event_ids.append(int(cur.lastrowid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"status": "applied", "schema": SCHEMA, "repair_key": REPAIR_KEY, "preview_hash": preview["preview_hash"], "backup": backup, "archived_link_ids": [521, 522], "event_ids": event_ids, "rollback_available": True}


def build_rollback_preview(conn: Any) -> dict[str, Any]:
    blockers: list[str] = []
    candidates: list[dict[str, Any]] = []
    for spec in TARGETS:
        link = _link(conn, int(spec["link_id"]))
        item_blockers: list[str] = []
        if link is None:
            item_blockers.append("link_missing")
        elif link.get("archived_at") is None:
            item_blockers.append("link_not_archived")
        event = conn.execute(
            "SELECT id FROM memory_events WHERE event_type='memory.link.archived_r6c_manual_review' AND payload_json LIKE ? ORDER BY id DESC LIMIT 1",
            (f'%"link_id": {int(spec["link_id"])}%',),
        ).fetchone()
        if event is None:
            item_blockers.append("archive_event_missing")
        blockers.extend(f"link:{spec['link_id']}:{code}" for code in item_blockers)
        candidates.append({"link_id": int(spec["link_id"]), "current": link, "archive_event_id": None if event is None else int(event[0]), "blockers": item_blockers})
    payload = {"schema": ROLLBACK_SCHEMA, "repair_key": REPAIR_KEY, "candidates": candidates, "status": "preview_ready" if not blockers else "blocked", "blockers": blockers}
    return {**payload, "rollback_preview_hash": _hash(payload), "hash_algorithm": "sha256:canonical-json:v1", "safety": {"read_only": True, "mutations_performed": 0}}


def rollback(conn: Any, *, expected_rollback_preview_hash: str, rolled_back_by: str, notes: str, now_iso: str) -> dict[str, Any]:
    preview = build_rollback_preview(conn)
    if preview["status"] != "preview_ready":
        return {"status": "blocked", "schema": ROLLBACK_SCHEMA, "blocking_reasons": preview["blockers"]}
    if str(expected_rollback_preview_hash) != str(preview["rollback_preview_hash"]):
        return {"status": "stale_rollback_preview", "schema": ROLLBACK_SCHEMA, "blocking_reasons": ["expected_rollback_preview_hash_mismatch"], "current_rollback_preview_hash": preview["rollback_preview_hash"]}
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
            cursor = conn.execute("UPDATE memory_links SET archived_at=NULL WHERE id=? AND archived_at=?", (int(link["id"]), archived_at))
            if cursor.rowcount != 1:
                raise RuntimeError(f"rollback_link_changed:{link['id']}")
            payload = {"repair_key": REPAIR_KEY, "link_id": int(link["id"]), "rolled_back_by": actor, "notes": note, "rollback_preview_hash": preview["rollback_preview_hash"]}
            cur = conn.execute("INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES(?,?,?,?)", (int(link["from_memory_id"]), "memory.link.archive_r6c_manual_review_rolled_back", json.dumps(payload, ensure_ascii=False, sort_keys=True), now_iso))
            event_ids.append(int(cur.lastrowid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"status": "rolled_back", "schema": ROLLBACK_SCHEMA, "repair_key": REPAIR_KEY, "rollback_preview_hash": preview["rollback_preview_hash"], "event_ids": event_ids}
