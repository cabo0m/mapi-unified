from __future__ import annotations

"""Memory ownership mutation payloads."""

from typing import Any, Callable


def set_memory_owner_payload(
    conn: Any,
    *,
    memory_id: int,
    owner_role: str,
    owner_id: str | None = None,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    require_memory_row: Callable[[Any, int], Any],
    insert_memory_event: Callable[..., dict[str, Any]],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    normalized_owner_role = normalize_required_text(owner_role, "owner_role")
    normalized_owner_id = normalize_optional_text(owner_id)
    require_memory_row(conn, int(memory_id))
    updated_at = utc_now_iso()
    conn.execute(
        "UPDATE memories SET owner_role = ?, owner_id = ?, last_accessed_at = ? WHERE id = ?",
        (normalized_owner_role, normalized_owner_id, updated_at, int(memory_id)),
    )
    event = insert_memory_event(
        conn,
        memory_id=int(memory_id),
        event_type="ownership.updated",
        payload={"owner_role": normalized_owner_role, "owner_id": normalized_owner_id},
    )
    conn.commit()
    updated_row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    return {
        "status": "owner_updated",
        "event": event,
        "memory": apply_ownership_defaults(enrich_memory_dict(row_to_dict(updated_row))),
    }


def bulk_set_memory_owner_payload(
    conn: Any,
    *,
    memory_ids: list[int],
    owner_role: str,
    owner_id: str | None = None,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    require_memory_row: Callable[[Any, int], Any],
    insert_memory_event: Callable[..., dict[str, Any]],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    if not memory_ids:
        return {"status": "error", "error": "memory_ids nie mogÄ… byÄ‡ puste"}
    normalized_owner_role = normalize_required_text(owner_role, "owner_role")
    normalized_owner_id = normalize_optional_text(owner_id)
    unique_ids = [int(memory_id) for memory_id in dict.fromkeys(memory_ids)]
    updated_at = utc_now_iso()
    items: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for memory_id in unique_ids:
        require_memory_row(conn, memory_id)
        conn.execute(
            "UPDATE memories SET owner_role = ?, owner_id = ?, last_accessed_at = ? WHERE id = ?",
            (normalized_owner_role, normalized_owner_id, updated_at, memory_id),
        )
        event = insert_memory_event(
            conn,
            memory_id=memory_id,
            event_type="ownership.bulk_updated",
            payload={"owner_role": normalized_owner_role, "owner_id": normalized_owner_id},
        )
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        items.append(apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        events.append(event)
    conn.commit()
    return {
        "status": "bulk_owner_updated",
        "count": len(items),
        "memory_ids": unique_ids,
        "events": events,
        "items": items,
    }
