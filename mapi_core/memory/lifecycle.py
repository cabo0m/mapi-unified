from __future__ import annotations

"""Memory lifecycle mutation payloads."""

from typing import Any, Callable


def reject_memory_payload(
    conn: Any,
    *,
    memory_id: int,
    notes: str,
    source: str | None = "manual_review",
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    require_memory_row: Callable[[Any, int], Any],
    insert_memory_event: Callable[..., dict[str, Any]],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    normalized_notes = normalize_required_text(notes, "notes")
    normalized_source = normalize_optional_text(source) or "manual_review"
    memory = require_memory_row(conn, int(memory_id))
    old_memory = enrich_memory_dict(row_to_dict(memory))
    rejected_at = utc_now_iso()
    conn.execute(
        """
        UPDATE memories
        SET state_code = ?,
            activity_state = ?,
            archived_at = ?,
            validation_source = ?,
            last_accessed_at = ?
        WHERE id = ?
        """,
        ("archived", "archived", rejected_at, normalized_source, rejected_at, int(memory_id)),
    )
    event = insert_memory_event(
        conn,
        memory_id=int(memory_id),
        event_type="review.rejected",
        payload={
            "notes": normalized_notes,
            "source": normalized_source,
            "old_state_code": old_memory.get("state_code"),
        },
    )
    conn.commit()
    updated_row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    updated_memory = apply_ownership_defaults(enrich_memory_dict(row_to_dict(updated_row)))
    return {
        "status": "rejected",
        "memory_id": int(memory_id),
        "old_state_code": old_memory.get("state_code"),
        "new_state_code": updated_memory.get("state_code"),
        "event": event,
        "memory": updated_memory,
    }


def return_memory_to_review_payload(
    conn: Any,
    *,
    memory_id: int,
    notes: str | None = None,
    source: str | None = "manual_review",
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    require_memory_row: Callable[[Any, int], Any],
    insert_memory_event: Callable[..., dict[str, Any]],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    normalized_notes = normalize_optional_text(notes)
    normalized_source = normalize_optional_text(source) or "manual_review"
    memory = require_memory_row(conn, int(memory_id))
    old_memory = enrich_memory_dict(row_to_dict(memory))
    returned_at = utc_now_iso()
    conn.execute(
        """
        UPDATE memories
        SET state_code = ?,
            activity_state = ?,
            archived_at = NULL,
            validation_source = ?,
            last_accessed_at = ?
        WHERE id = ?
        """,
        ("candidate", "active", normalized_source, returned_at, int(memory_id)),
    )
    event = insert_memory_event(
        conn,
        memory_id=int(memory_id),
        event_type="review.returned",
        payload={
            "notes": normalized_notes,
            "source": normalized_source,
            "old_state_code": old_memory.get("state_code"),
        },
    )
    conn.commit()
    updated_row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    updated_memory = apply_ownership_defaults(enrich_memory_dict(row_to_dict(updated_row)))
    return {
        "status": "returned_to_review",
        "memory_id": int(memory_id),
        "old_state_code": old_memory.get("state_code"),
        "new_state_code": updated_memory.get("state_code"),
        "event": event,
        "memory": updated_memory,
    }


def deprecate_memory_payload(
    conn: Any,
    *,
    memory_id: int,
    reason: str,
    source: str | None = "manual_review",
    replacement_memory_id: int | None = None,
    valid_to: str | None = None,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    shift_iso_days: Callable[[str, int], str],
    require_memory_row: Callable[[Any, int], Any],
    insert_memory_event: Callable[..., dict[str, Any]],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    normalized_reason = normalize_required_text(reason, "reason")
    normalized_source = normalize_optional_text(source) or "manual_review"
    normalized_valid_to = normalize_optional_text(valid_to) or utc_now_iso()
    memory = require_memory_row(conn, int(memory_id))
    old_memory = enrich_memory_dict(row_to_dict(memory))
    if replacement_memory_id is not None:
        require_memory_row(conn, int(replacement_memory_id))
    conn.execute(
        """
        UPDATE memories
        SET state_code = ?,
            valid_to = ?,
            expired_due_at = ?,
            validation_source = ?,
            last_accessed_at = ?
        WHERE id = ?
        """,
        ("superseded", normalized_valid_to, shift_iso_days(normalized_valid_to, 2), normalized_source, normalized_valid_to, int(memory_id)),
    )
    event = insert_memory_event(
        conn,
        memory_id=int(memory_id),
        event_type="version.deprecated",
        payload={
            "source": normalized_source,
            "reason": normalized_reason,
            "replacement_memory_id": None if replacement_memory_id is None else int(replacement_memory_id),
            "old_state_code": old_memory.get("state_code"),
            "new_state_code": "superseded",
        },
    )
    conn.commit()
    updated_row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    updated_memory = apply_ownership_defaults(enrich_memory_dict(row_to_dict(updated_row)))
    return {
        "status": "deprecated",
        "memory_id": int(memory_id),
        "replacement_memory_id": None if replacement_memory_id is None else int(replacement_memory_id),
        "event": event,
        "memory": updated_memory,
    }
