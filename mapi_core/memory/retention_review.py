from __future__ import annotations

import json
from typing import Any, Callable

from mapi_core.memory.retention import RETENTION_POLICY_VERSION, SUPPORTED_RETENTION_ACTIONS


RETENTION_REVIEW_ITEM_SCHEMA_VERSION = "memory_v3_retention_review_item.v1"
RETENTION_REVIEW_STATUSES = frozenset({"pending", "approved", "rejected", "applied", "rolled_back", "expired"})


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: Any, *, fallback: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return json.loads(value)


def retention_review_item_to_dict(row: Any, *, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    item = row_to_dict(row)
    for field, fallback in (
        ("protected_reasons_json", []),
        ("reason_codes_json", []),
        ("preview_json", {}),
        ("before_snapshot_json", None),
        ("applied_snapshot_json", None),
        ("created_event_ids_json", []),
        ("rollback_snapshot_json", None),
    ):
        item[field.removesuffix("_json")] = _loads(item.get(field), fallback=fallback)
    item["schema_version"] = RETENTION_REVIEW_ITEM_SCHEMA_VERSION
    return item


def get_retention_review_item(
    conn: Any,
    *,
    review_item_id: int,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM memory_retention_review_items WHERE id = ?", (int(review_item_id),)).fetchone()
    if row is None:
        raise FileNotFoundError(f"memory_retention_review_item not found: {review_item_id}")
    return retention_review_item_to_dict(row, row_to_dict=row_to_dict)


def list_retention_review_items(
    conn: Any,
    *,
    status: str | None = None,
    project_key: str | None = None,
    memory_id: int | None = None,
    limit: int = 50,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 200:
        return {"status": "error", "error": "limit must be in range 1..200"}
    sql = "SELECT * FROM memory_retention_review_items WHERE 1=1"
    params: list[Any] = []
    if status is not None:
        normalized_status = str(status).strip().lower()
        if normalized_status not in RETENTION_REVIEW_STATUSES:
            return {"status": "error", "error": "invalid status"}
        sql += " AND status = ?"
        params.append(normalized_status)
    if project_key is not None:
        sql += " AND project_key = ?"
        params.append(str(project_key).strip())
    if memory_id is not None:
        sql += " AND memory_id = ?"
        params.append(int(memory_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    items = [retention_review_item_to_dict(row, row_to_dict=row_to_dict) for row in conn.execute(sql, params).fetchall()]
    return {
        "status": "ok",
        "schema_version": "memory_v3_retention_review_items.v1",
        "items": items,
        "count": len(items),
        "safety": {"read_only": True, "raw_secret_exposed": False},
    }


def save_retention_review_item(
    conn: Any,
    *,
    memory_id: int,
    expected_preview_hash: str,
    as_of: str | None,
    preview_func: Callable[..., dict[str, Any]],
    canonical_json_hash: Callable[[Any], str],
    utc_now_iso: Callable[[], str],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    fresh = preview_func(conn, memory_id=int(memory_id), as_of=as_of, include_debug=False)
    if fresh.get("status") != "preview_ready":
        return {"status": "blocked", "error": "preview_not_ready", "preview": fresh}
    if str(fresh.get("preview_hash") or "") != str(expected_preview_hash or "").strip():
        return {
            "status": "stale_preview",
            "expected_preview_hash": str(expected_preview_hash or "").strip(),
            "current_preview_hash": fresh.get("preview_hash"),
        }
    action = fresh.get("proposed_action")
    if action not in SUPPORTED_RETENTION_ACTIONS:
        return {"status": "no_review_needed", "preview": fresh}
    operation_key = canonical_json_hash(
        {
            "policy_version": RETENTION_POLICY_VERSION,
            "memory_id": int(memory_id),
            "preview_hash": fresh["preview_hash"],
        }
    )
    existing = conn.execute(
        "SELECT * FROM memory_retention_review_items WHERE operation_key = ?",
        (operation_key,),
    ).fetchone()
    if existing is not None:
        return {
            "status": "already_exists",
            "item": retention_review_item_to_dict(existing, row_to_dict=row_to_dict),
        }
    now = utc_now_iso()
    cursor = conn.execute(
        """
        INSERT INTO memory_retention_review_items (
            operation_key, memory_id, status, project_key, scope_code, workspace_id,
            as_of, sensitivity_class, retention_class, policy_outcome, proposed_action,
            protected_reasons_json, reason_codes_json, input_fingerprint, preview_hash,
            preview_json, created_at, updated_at
        ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation_key,
            int(memory_id),
            fresh.get("project_key"),
            fresh.get("scope_code"),
            fresh.get("workspace_id"),
            fresh["as_of"],
            fresh["sensitivity"]["sensitivity_class"],
            fresh["retention_class"],
            fresh["policy_outcome"],
            action,
            _dumps(fresh["protected_reasons"]),
            _dumps(fresh["reason_codes"]),
            fresh["input_fingerprint"],
            fresh["preview_hash"],
            _dumps(fresh),
            now,
            now,
        ),
    )
    return {
        "status": "created",
        "item": get_retention_review_item(conn, review_item_id=int(cursor.lastrowid), row_to_dict=row_to_dict),
    }


def decide_retention_review_item(
    conn: Any,
    *,
    review_item_id: int,
    decision: str,
    reviewed_by: str,
    review_note: str | None,
    utc_now_iso: Callable[[], str],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"approve", "reject"}:
        return {"status": "error", "error": "decision must be approve or reject"}
    normalized_reviewer = str(reviewed_by or "").strip()
    if not normalized_reviewer:
        return {"status": "error", "error": "reviewed_by is required"}
    normalized_note = str(review_note).strip() if review_note is not None else None
    if normalized_decision == "reject" and not normalized_note:
        return {"status": "error", "error": "reject requires review_note"}
    item = get_retention_review_item(conn, review_item_id=int(review_item_id), row_to_dict=row_to_dict)
    next_status = "approved" if normalized_decision == "approve" else "rejected"
    if item["status"] == next_status:
        return {"status": "already_decided", "item": item}
    if item["status"] != "pending":
        return {"status": "blocked", "error": f"item_in_terminal_state:{item['status']}", "item": item}
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE memory_retention_review_items
        SET status = ?, reviewed_at = ?, reviewed_by = ?, review_note = ?, updated_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (next_status, now, normalized_reviewer, normalized_note, now, int(review_item_id)),
    )
    return {
        "status": "updated",
        "item": get_retention_review_item(conn, review_item_id=int(review_item_id), row_to_dict=row_to_dict),
    }
