from __future__ import annotations

"""Memory validation and review event payloads."""

import json
from typing import Any, Callable


def memory_event_to_dict(row: Any, *, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    event = row_to_dict(row)
    payload_json = event.get("payload_json")
    event["payload"] = json.loads(payload_json) if isinstance(payload_json, str) and payload_json.strip() else None
    return event


def insert_memory_event_payload(
    conn: Any,
    *,
    memory_id: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
    utc_now_iso: Callable[[], str],
    normalize_required_text: Callable[[Any, str], str],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    created_at = utc_now_iso()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO memory_events (memory_id, event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            int(memory_id),
            normalize_required_text(event_type, "event_type"),
            None if payload is None else json.dumps(payload, ensure_ascii=False),
            created_at,
        ),
    )
    event_id = int(cursor.lastrowid)
    row = conn.execute("SELECT * FROM memory_events WHERE id = ?", (event_id,)).fetchone()
    return memory_event_to_dict(row, row_to_dict=row_to_dict)


def add_validation_event_payload(
    conn: Any,
    *,
    memory_id: int,
    verdict: str,
    notes: str | None = None,
    source: str | None = "manual_review",
    confidence_score: float | None = None,
    importance_score: float | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    review_due_at: str | None = None,
    revalidation_due_at: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_score: Callable[[float], float],
    utc_now_iso: Callable[[], str],
    utc_offset_days_iso: Callable[[int], str],
    compute_sla_days: Callable[..., int],
    default_owner_role: Callable[..., str | None],
    require_memory_row: Callable[[Any, int], Any],
    insert_memory_event: Callable[..., dict[str, Any]],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    normalized_verdict = normalize_optional_text(verdict)
    if normalized_verdict not in {"validated", "stale", "risky", "needs_review"}:
        return {"status": "error", "error": 'verdict musi byÄ‡ jednym z: validated, stale, risky, needs_review'}

    normalized_notes = normalize_optional_text(notes)
    normalized_source = normalize_optional_text(source) or "manual_review"
    memory = require_memory_row(conn, int(memory_id))
    old_memory = enrich_memory_dict(row_to_dict(memory))
    event_time = utc_now_iso()

    updates: list[str] = ["last_accessed_at = ?", "validation_source = ?"]
    params: list[Any] = [event_time, normalized_source]

    if confidence_score is not None:
        updates.append("confidence_score = ?")
        params.append(normalize_score(float(confidence_score)))
    if importance_score is not None:
        updates.append("importance_score = ?")
        params.append(normalize_score(float(importance_score)))
    if normalized_verdict == "validated":
        updates.append("state_code = ?")
        params.append("validated")
        updates.append("last_validated_at = ?")
        params.append(event_time)
        updates.append("review_due_at = NULL")
        updates.append("revalidation_due_at = ?")
        params.append(
            normalize_optional_text(revalidation_due_at)
            or utc_offset_days_iso(
                compute_sla_days(conn, "revalidation", old_memory.get("priority") or "normal", old_memory.get("memory_type"), old_memory.get("scope_code"), old_memory.get("project_key"))
            )
        )
        updates.append("owner_role = ?")
        params.append(normalize_optional_text(owner_role) or old_memory.get("owner_role") or default_owner_role(state_code="validated", scope_code=old_memory.get("scope_code"), project_key=old_memory.get("project_key")))
        updates.append("owner_id = ?")
        params.append(normalize_optional_text(owner_id) or old_memory.get("owner_id"))
    elif normalized_verdict == "needs_review":
        updates.append("state_code = ?")
        params.append("candidate")
        updates.append("review_due_at = ?")
        params.append(
            normalize_optional_text(review_due_at)
            or utc_offset_days_iso(
                compute_sla_days(conn, "review", old_memory.get("priority") or "normal", old_memory.get("memory_type"), old_memory.get("scope_code"), old_memory.get("project_key"))
            )
        )
        updates.append("revalidation_due_at = NULL")
        updates.append("owner_role = ?")
        params.append(normalize_optional_text(owner_role) or old_memory.get("owner_role") or default_owner_role(state_code="candidate", scope_code=old_memory.get("scope_code"), project_key=old_memory.get("project_key")))
        updates.append("owner_id = ?")
        params.append(normalize_optional_text(owner_id) or old_memory.get("owner_id"))

    params.append(int(memory_id))
    conn.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id = ?", params)

    event = insert_memory_event(
        conn,
        memory_id=int(memory_id),
        event_type=f"validation.{normalized_verdict}",
        payload={
            "verdict": normalized_verdict,
            "notes": normalized_notes,
            "source": normalized_source,
            "old_state_code": old_memory.get("state_code"),
        },
    )
    conn.commit()
    updated_row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    updated_memory = apply_ownership_defaults(enrich_memory_dict(row_to_dict(updated_row)))
    return {
        "status": "validation_recorded",
        "memory_id": int(memory_id),
        "event": event,
        "old_state_code": old_memory.get("state_code"),
        "new_state_code": updated_memory.get("state_code"),
        "memory": updated_memory,
    }


def list_validation_events_payload(
    conn: Any,
    *,
    memory_id: int,
    limit: int = 20,
    verdict: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    require_memory_row: Callable[[Any, int], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": 'limit musi byÄ‡ w zakresie 1..1000'}
    normalized_verdict = normalize_optional_text(verdict)
    require_memory_row(conn, int(memory_id))
    sql = "SELECT * FROM memory_events WHERE memory_id = ? AND event_type LIKE 'validation.%'"
    params: list[Any] = [int(memory_id)]
    if normalized_verdict:
        sql += " AND event_type = ?"
        params.append(f"validation.{normalized_verdict}")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    items = [memory_event_to_dict(row, row_to_dict=row_to_dict) for row in rows]
    return {"memory_id": int(memory_id), "count": len(items), "items": items, "limit": int(limit), "verdict": normalized_verdict}


def add_review_note_payload(
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
) -> dict[str, Any]:
    normalized_notes = normalize_required_text(notes, "notes")
    normalized_source = normalize_optional_text(source) or "manual_review"
    require_memory_row(conn, int(memory_id))
    noted_at = utc_now_iso()
    conn.execute(
        "UPDATE memories SET last_accessed_at = ?, validation_source = ? WHERE id = ?",
        (noted_at, normalized_source, int(memory_id)),
    )
    event = insert_memory_event(
        conn,
        memory_id=int(memory_id),
        event_type="review.note",
        payload={"notes": normalized_notes, "source": normalized_source},
    )
    conn.commit()
    updated_row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    return {"status": "review_note_added", "memory_id": int(memory_id), "event": event, "memory": enrich_memory_dict(row_to_dict(updated_row))}


def list_review_events_payload(
    conn: Any,
    *,
    memory_id: int,
    limit: int = 20,
    event_type: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    require_memory_row: Callable[[Any, int], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": 'limit musi byÄ‡ w zakresie 1..1000'}
    normalized_event_type = normalize_optional_text(event_type)
    require_memory_row(conn, int(memory_id))
    sql = "SELECT * FROM memory_events WHERE memory_id = ? AND event_type LIKE 'review.%'"
    params: list[Any] = [int(memory_id)]
    if normalized_event_type:
        sql += " AND event_type = ?"
        params.append(normalized_event_type)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    items = [memory_event_to_dict(row, row_to_dict=row_to_dict) for row in rows]
    return {"memory_id": int(memory_id), "count": len(items), "items": items, "limit": int(limit), "event_type": normalized_event_type}
