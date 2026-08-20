from __future__ import annotations

"""Memory provenance read payloads."""

import json
from typing import Any, Callable


def get_memory_provenance_payload(
    conn: Any,
    *,
    memory_id: int,
    require_memory_row: Callable[[Any, int], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
    normalize_optional_text: Callable[[Any], str | None],
) -> dict[str, Any]:
    memory = require_memory_row(conn, int(memory_id))
    memory_dict = apply_ownership_defaults(enrich_memory_dict(row_to_dict(memory)))
    rows = conn.execute(
        "SELECT * FROM memory_events WHERE memory_id = ? ORDER BY id ASC",
        (int(memory_id),),
    ).fetchall()

    audit_items: list[dict[str, Any]] = []
    review_sources: set[str] = set()
    validation_sources: set[str] = set()
    last_review_event: dict[str, Any] | None = None
    last_validation_event: dict[str, Any] | None = None

    for row in rows:
        item = row_to_dict(row)
        payload_json = item.get("payload_json")
        payload = json.loads(payload_json) if isinstance(payload_json, str) and payload_json.strip() else None
        item["payload"] = payload
        audit_items.append(item)
        source_value = None
        if isinstance(payload, dict):
            source_value = normalize_optional_text(payload.get("source"))
        if str(item.get("event_type", "")).startswith("review."):
            last_review_event = item
            if source_value:
                review_sources.add(source_value)
        if str(item.get("event_type", "")).startswith("validation."):
            last_validation_event = item
            if source_value:
                validation_sources.add(source_value)

    return {
        "memory_id": int(memory_id),
        "memory": memory_dict,
        "created_at": memory_dict.get("created_at"),
        "created_source": memory_dict.get("source"),
        "validation_source": memory_dict.get("validation_source"),
        "last_validated_at": memory_dict.get("last_validated_at"),
        "supersedes_memory_id": memory_dict.get("supersedes_memory_id"),
        "promoted_from_id": memory_dict.get("promoted_from_id"),
        "demoted_from_id": memory_dict.get("demoted_from_id"),
        "parent_memory_id": memory_dict.get("parent_memory_id"),
        "project_key": memory_dict.get("project_key"),
        "conversation_key": memory_dict.get("conversation_key"),
        "audit_event_count": len(audit_items),
        "review_sources": sorted(review_sources),
        "validation_sources": sorted(validation_sources),
        "last_review_event": last_review_event,
        "last_validation_event": last_validation_event,
    }


def list_memory_audit_payload(
    conn: Any,
    *,
    memory_id: int,
    limit: int = 50,
    event_type_prefix: str | None = None,
    require_memory_row: Callable[[Any, int], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
    normalize_optional_text: Callable[[Any], str | None],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}
    normalized_prefix = normalize_optional_text(event_type_prefix)
    memory = require_memory_row(conn, int(memory_id))
    sql = "SELECT * FROM memory_events WHERE memory_id = ?"
    params: list[Any] = [int(memory_id)]
    if normalized_prefix:
        sql += " AND event_type LIKE ?"
        params.append(f"{normalized_prefix}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = row_to_dict(row)
        payload_json = item.get("payload_json")
        item["payload"] = json.loads(payload_json) if isinstance(payload_json, str) and payload_json.strip() else None
        items.append(item)
    return {
        "memory_id": int(memory_id),
        "count": len(items),
        "items": items,
        "limit": int(limit),
        "event_type_prefix": normalized_prefix,
        "memory": apply_ownership_defaults(enrich_memory_dict(row_to_dict(memory))),
    }
