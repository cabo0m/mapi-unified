from __future__ import annotations

import json
from typing import Any, Callable


CAPTURE_REVIEW_ITEM_SCHEMA_VERSION = "memory_v3_capture_review_item.v1"
CAPTURE_REVIEW_ITEM_STATUSES = frozenset(
    {"pending", "approved", "rejected", "expired", "applied", "superseded"}
)
CAPTURE_REVIEW_MUTABLE_STATUSES = frozenset({"pending", "approved"})


def _normalize_status(value: str | None, *, field_name: str = "status") -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized not in CAPTURE_REVIEW_ITEM_STATUSES:
        raise ValueError(
            f"{field_name} must be one of: {', '.join(sorted(CAPTURE_REVIEW_ITEM_STATUSES))}"
        )
    return normalized


def _require_json_object(payload: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return dict(payload)


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    decoded = json.loads(value)
    if decoded is None:
        return None
    if not isinstance(decoded, dict):
        raise ValueError("decoded JSON payload must be an object")
    return decoded


def _decode_json_array(value: Any, *, field_name: str) -> list[Any]:
    if not isinstance(value, str) or not value.strip():
        return []
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise ValueError(f"{field_name} must decode to an array")
    return list(decoded)


def capture_review_item_to_dict(
    row: Any,
    *,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    item = row_to_dict(row)
    item["proposal"] = _decode_json_object(item.get("proposal_json"))
    item["matched_memory_ids"] = [int(value) for value in _decode_json_array(item.get("matched_memory_ids_json"), field_name="matched_memory_ids_json")]
    item["reconciliation"] = _decode_json_object(item.get("reconciliation_json"))
    item["schema_version"] = CAPTURE_REVIEW_ITEM_SCHEMA_VERSION
    return item


def _require_item_row(conn: Any, *, item_id: int) -> Any:
    row = conn.execute(
        "SELECT * FROM memory_capture_review_items WHERE id = ?",
        (int(item_id),),
    ).fetchone()
    if row is None:
        raise FileNotFoundError(f"Nie znaleziono memory_capture_review_item o id={item_id}")
    return row


def _require_transition(
    *,
    current_status: str,
    next_status: str,
    decision: str,
) -> None:
    if next_status in {"applied", "superseded"}:
        raise ValueError("B03 review API cannot set applied or superseded")
    if current_status == next_status:
        return
    if current_status == "pending" and next_status in {"approved", "rejected", "expired"}:
        return
    if current_status == "approved" and next_status == "expired":
        return
    raise ValueError(f"invalid_transition:{current_status}->{next_status} for {decision}")


def get_capture_review_item(
    conn: Any,
    *,
    item_id: int,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    return capture_review_item_to_dict(
        _require_item_row(conn, item_id=int(item_id)),
        row_to_dict=row_to_dict,
    )


def find_capture_review_item_by_proposal_key(
    conn: Any,
    *,
    proposal_key: str,
    normalize_required_text: Callable[[Any, str], str],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any] | None:
    normalized_key = normalize_required_text(proposal_key, "proposal_key")
    row = conn.execute(
        "SELECT * FROM memory_capture_review_items WHERE proposal_key = ?",
        (normalized_key,),
    ).fetchone()
    if row is None:
        return None
    return capture_review_item_to_dict(row, row_to_dict=row_to_dict)


def list_capture_review_items(
    conn: Any,
    *,
    status: str | None = None,
    project_key: str | None = None,
    scope_code: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    limit: int = 50,
    include_expired: bool = False,
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 200:
        return {
            "status": "error",
            "schema_version": "memory_v3_capture_review_items.v1",
            "error": "limit musi byc w zakresie 1..200",
        }
    normalized_status = (
        _normalize_status(status, field_name="status")
        if normalize_optional_text(status) is not None
        else None
    )
    sql = "SELECT * FROM memory_capture_review_items WHERE 1 = 1"
    params: list[Any] = []
    if normalized_status is not None:
        sql += " AND status = ?"
        params.append(normalized_status)
    elif not include_expired:
        sql += " AND status != 'expired'"
    normalized_project_key = normalize_optional_text(project_key)
    if normalized_project_key is not None:
        sql += " AND project_key = ?"
        params.append(normalized_project_key)
    normalized_scope_code = normalize_optional_text(scope_code)
    if normalized_scope_code is not None:
        sql += " AND scope_code = ?"
        params.append(normalized_scope_code)
    normalized_created_after = normalize_optional_text(created_after)
    if normalized_created_after is not None:
        sql += " AND created_at >= ?"
        params.append(normalized_created_after)
    normalized_created_before = normalize_optional_text(created_before)
    if normalized_created_before is not None:
        sql += " AND created_at <= ?"
        params.append(normalized_created_before)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    items = [
        capture_review_item_to_dict(row, row_to_dict=row_to_dict)
        for row in conn.execute(sql, params).fetchall()
    ]
    return {
        "status": "ok",
        "schema_version": "memory_v3_capture_review_items.v1",
        "filters": {
            "status": normalized_status,
            "project_key": normalized_project_key,
            "scope_code": normalized_scope_code,
            "created_after": normalized_created_after,
            "created_before": normalized_created_before,
            "limit": int(limit),
            "include_expired": bool(include_expired),
        },
        "summary": {"total_returned": len(items)},
        "items": items,
        "safety": {"read_only": True, "memory_mutations_performed": 0},
    }


def create_capture_review_item(
    conn: Any,
    *,
    proposal_key: str,
    proposal: dict[str, Any],
    input_fingerprint: str,
    project_key: str | None = None,
    scope_code: str | None = None,
    conversation_key: str | None = None,
    source_context: str | None = None,
    source_event_ref: str | None = None,
    recommended_action: str | None = None,
    expires_at: str | None = None,
    utc_now_iso: Callable[[], str],
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    existing = find_capture_review_item_by_proposal_key(
        conn,
        proposal_key=proposal_key,
        normalize_required_text=normalize_required_text,
        row_to_dict=row_to_dict,
    )
    if existing is not None:
        return {"status": "already_exists", "created": False, "item": existing}

    created_at = utc_now_iso()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO memory_capture_review_items (
            proposal_key,
            status,
            proposal_json,
            input_fingerprint,
            project_key,
            scope_code,
            conversation_key,
            source_context,
            source_event_ref,
            recommended_action,
            matched_memory_ids_json,
            reconciliation_json,
            candidate_set_fingerprint,
            reconciliation_preview_hash,
            created_memory_id,
            reviewed_at,
            reviewed_by,
            review_note,
            expires_at,
            created_at,
            updated_at
        )
        VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, '[]', NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)
        """,
        (
            normalize_required_text(proposal_key, "proposal_key"),
            _json_dumps(_require_json_object(proposal, field_name="proposal")),
            normalize_required_text(input_fingerprint, "input_fingerprint"),
            normalize_optional_text(project_key),
            normalize_optional_text(scope_code),
            normalize_optional_text(conversation_key),
            normalize_optional_text(source_context),
            normalize_optional_text(source_event_ref),
            normalize_optional_text(recommended_action),
            normalize_optional_text(expires_at),
            created_at,
            created_at,
        ),
    )
    row = conn.execute(
        "SELECT * FROM memory_capture_review_items WHERE id = ?",
        (int(cursor.lastrowid),),
    ).fetchone()
    return {
        "status": "created",
        "created": True,
        "item": capture_review_item_to_dict(row, row_to_dict=row_to_dict),
    }


def review_capture_item(
    conn: Any,
    *,
    item_id: int,
    decision: str,
    reviewed_by: str | None = None,
    review_note: str | None = None,
    utc_now_iso: Callable[[], str],
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    normalized_decision = normalize_required_text(decision, "decision").lower().strip()
    if normalized_decision not in {"approve", "reject"}:
        return {"status": "error", "error": "decision must be approve or reject"}
    normalized_review_note = normalize_optional_text(review_note)
    if normalized_decision == "reject" and normalized_review_note is None:
        return {"status": "error", "error": "reject requires non-empty review_note"}

    current = get_capture_review_item(conn, item_id=int(item_id), row_to_dict=row_to_dict)
    next_status = "approved" if normalized_decision == "approve" else "rejected"
    if current["status"] == next_status:
        return {"status": "already_decided", "item": current}
    if current["status"] in {"rejected", "expired", "applied", "superseded"}:
        return {
            "status": "blocked",
            "error": f"item_in_terminal_state:{current['status']}",
            "item": current,
        }
    try:
        _require_transition(
            current_status=str(current["status"]),
            next_status=next_status,
            decision=normalized_decision,
        )
    except ValueError as exc:
        return {"status": "blocked", "error": str(exc), "item": current}

    reviewed_at = utc_now_iso()
    conn.execute(
        """
        UPDATE memory_capture_review_items
        SET status = ?,
            reviewed_at = ?,
            reviewed_by = ?,
            review_note = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            next_status,
            reviewed_at,
            normalize_optional_text(reviewed_by),
            normalized_review_note,
            reviewed_at,
            int(item_id),
        ),
    )
    return {
        "status": "updated",
        "item": get_capture_review_item(conn, item_id=int(item_id), row_to_dict=row_to_dict),
    }


def expire_capture_item(
    conn: Any,
    *,
    item_id: int,
    reason: str,
    expired_by: str | None = None,
    utc_now_iso: Callable[[], str],
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    normalized_reason = normalize_required_text(reason, "reason")
    current = get_capture_review_item(conn, item_id=int(item_id), row_to_dict=row_to_dict)
    if current["status"] == "expired":
        return {"status": "already_expired", "item": current}
    if current["status"] in {"rejected", "applied", "superseded"}:
        return {
            "status": "blocked",
            "error": f"item_in_terminal_state:{current['status']}",
            "item": current,
        }
    try:
        _require_transition(
            current_status=str(current["status"]),
            next_status="expired",
            decision="expire",
        )
    except ValueError as exc:
        return {"status": "blocked", "error": str(exc), "item": current}

    expired_at = utc_now_iso()
    conn.execute(
        """
        UPDATE memory_capture_review_items
        SET status = 'expired',
            reviewed_at = ?,
            reviewed_by = ?,
            review_note = ?,
            expires_at = COALESCE(expires_at, ?),
            updated_at = ?
        WHERE id = ?
        """,
        (
            expired_at,
            normalize_optional_text(expired_by),
            normalized_reason,
            expired_at,
            expired_at,
            int(item_id),
        ),
    )
    return {
        "status": "expired",
        "item": get_capture_review_item(conn, item_id=int(item_id), row_to_dict=row_to_dict),
    }


def update_capture_reconciliation_preview(
    conn: Any,
    *,
    item_id: int,
    recommended_action: str,
    matched_memory_ids: list[int],
    reconciliation: dict[str, Any],
    candidate_set_fingerprint: str,
    reconciliation_preview_hash: str,
    utc_now_iso: Callable[[], str],
    normalize_required_text: Callable[[Any, str], str],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    current = get_capture_review_item(conn, item_id=int(item_id), row_to_dict=row_to_dict)
    if str(current["status"]) not in CAPTURE_REVIEW_MUTABLE_STATUSES:
        return {
            "status": "blocked",
            "error": f"item_status_not_reconcilable:{current['status']}",
            "item": current,
        }

    updated_at = utc_now_iso()
    conn.execute(
        """
        UPDATE memory_capture_review_items
        SET recommended_action = ?,
            matched_memory_ids_json = ?,
            reconciliation_json = ?,
            candidate_set_fingerprint = ?,
            reconciliation_preview_hash = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            normalize_required_text(recommended_action, "recommended_action"),
            _json_dumps([int(value) for value in matched_memory_ids]),
            _json_dumps(_require_json_object(reconciliation, field_name="reconciliation")),
            normalize_required_text(candidate_set_fingerprint, "candidate_set_fingerprint"),
            normalize_required_text(reconciliation_preview_hash, "reconciliation_preview_hash"),
            updated_at,
            int(item_id),
        ),
    )
    return {
        "status": "updated",
        "item": get_capture_review_item(conn, item_id=int(item_id), row_to_dict=row_to_dict),
    }


def mark_capture_review_item_applied(
    conn: Any,
    *,
    item_id: int,
    expected_preview_hash: str,
    outcome: str,
    apply_audit: dict[str, Any],
    created_memory_id: int | None,
    utc_now_iso: Callable[[], str],
    normalize_required_text: Callable[[Any, str], str],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    normalized_hash = normalize_required_text(expected_preview_hash, "expected_preview_hash")
    normalized_outcome = normalize_required_text(outcome, "outcome")
    current = get_capture_review_item(conn, item_id=int(item_id), row_to_dict=row_to_dict)
    reconciliation = dict(current.get("reconciliation") or {})
    existing_audit = reconciliation.get("apply_audit")

    if current["status"] == "applied":
        if not isinstance(existing_audit, dict):
            return {
                "status": "blocked",
                "error": "applied_item_missing_apply_audit",
                "item": current,
            }
        if (
            str(existing_audit.get("expected_preview_hash") or "") == normalized_hash
            and str(existing_audit.get("outcome") or "") == normalized_outcome
            and str(existing_audit.get("result_fingerprint") or "").strip()
        ):
            return {"status": "already_applied", "item": current, "apply_audit": existing_audit}
        return {
            "status": "blocked",
            "error": "applied_item_contract_mismatch",
            "item": current,
            "apply_audit": existing_audit,
        }

    if current["status"] != "approved":
        return {
            "status": "blocked",
            "error": f"item_status_not_approved:{current['status']}",
            "item": current,
        }

    reconciliation["apply_audit"] = _require_json_object(apply_audit, field_name="apply_audit")
    applied_at = normalize_required_text(apply_audit.get("applied_at"), "apply_audit.applied_at")
    conn.execute(
        """
        UPDATE memory_capture_review_items
        SET status = 'applied',
            reconciliation_json = ?,
            created_memory_id = ?,
            updated_at = ?
        WHERE id = ? AND status = 'approved'
        """,
        (
            _json_dumps(reconciliation),
            None if created_memory_id is None else int(created_memory_id),
            applied_at or utc_now_iso(),
            int(item_id),
        ),
    )
    updated = get_capture_review_item(conn, item_id=int(item_id), row_to_dict=row_to_dict)
    if updated["status"] != "applied":
        return {"status": "blocked", "error": "approved_to_applied_transition_failed", "item": updated}
    return {"status": "applied", "item": updated, "apply_audit": reconciliation["apply_audit"]}
