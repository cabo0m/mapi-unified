from __future__ import annotations

"""Memory recall telemetry, decoupled from durable importance."""

from typing import Any, Callable


RECALL_EVENT_SCHEMA = "memory_recall_event.v1"
RECALL_TELEMETRY_SCHEMA = "memory_recall_telemetry.v1"
RECALL_EVENT_TYPE = "recall.recorded"


def _normalize_recall_type(
    value: Any,
    *,
    normalize_optional_text: Callable[[Any], str | None],
) -> str:
    return normalize_optional_text(value) or "manual"


def _normalize_source(
    value: Any,
    *,
    normalize_optional_text: Callable[[Any], str | None],
) -> str:
    return normalize_optional_text(value) or "unspecified"


def recall_memory_payload(
    conn: Any,
    *,
    memory_id: int,
    strength: float = 0.1,
    recall_type: str = "manual",
    source: str | None = None,
    commit: bool = True,
    require_memory_row: Callable[[Any, int], Any],
    normalize_score: Callable[[float], float],
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    insert_memory_event: Callable[..., dict[str, Any]],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Record one recall without mutating durable importance."""
    memory = require_memory_row(conn, int(memory_id))
    current_importance = float(memory["importance_score"] or 0.0)
    old_recall_count = int(memory["recall_count"] or 0)
    normalized_strength = normalize_score(float(strength))
    normalized_recall_type = _normalize_recall_type(
        recall_type,
        normalize_optional_text=normalize_optional_text,
    )
    normalized_source = _normalize_source(
        source,
        normalize_optional_text=normalize_optional_text,
    )
    recalled_at = utc_now_iso()

    conn.execute(
        """
        UPDATE memories
        SET recall_count = recall_count + 1,
            last_recalled_at = ?,
            last_accessed_at = ?
        WHERE id = ?
        """,
        (recalled_at, recalled_at, int(memory_id)),
    )
    event = insert_memory_event(
        conn,
        memory_id=int(memory_id),
        event_type=RECALL_EVENT_TYPE,
        payload={
            "schema": RECALL_EVENT_SCHEMA,
            "recall_type": normalized_recall_type,
            "source": normalized_source,
            "strength": normalized_strength,
            "old_recall_count": old_recall_count,
            "new_recall_count": old_recall_count + 1,
            "importance_score": current_importance,
            "importance_changed": False,
            "recorded_at": recalled_at,
        },
    )
    if commit:
        conn.commit()
    updated = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    updated_memory = enrich_memory_dict(row_to_dict(updated))

    return {
        "status": "recalled",
        "schema": RECALL_EVENT_SCHEMA,
        "memory_id": int(memory_id),
        "recall_type": normalized_recall_type,
        "source": normalized_source,
        "strength": normalized_strength,
        "event": event,
        "updated_memory": updated_memory,
        "telemetry_changes": {
            "old_recall_count": old_recall_count,
            "new_recall_count": old_recall_count + 1,
            "last_recalled_at": recalled_at,
        },
        "importance_decoupling": {
            "enabled": True,
            "old_importance_score": current_importance,
            "new_importance_score": current_importance,
            "importance_changed": False,
            "reason": "recall_is_behavioral_telemetry_not_durable_importance",
        },
        # Compatibility surface for clients that previously inspected this list.
        "activation_changes": [
            {
                "memory_id": int(memory_id),
                "old_importance_score": current_importance,
                "new_importance_score": current_importance,
                "importance_changed": False,
                "old_recall_count": old_recall_count,
                "new_recall_count": old_recall_count + 1,
            }
        ],
    }


def get_memory_recall_telemetry_payload(
    conn: Any,
    *,
    memory_id: int,
    limit: int = 50,
    recall_type: str | None = None,
    require_memory_row: Callable[[Any, int], Any],
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
    memory_event_to_dict: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Return append-only recall events and legacy unattributed recall count."""
    safe_limit = max(1, min(int(limit or 50), 500))
    memory = require_memory_row(conn, int(memory_id))
    current = row_to_dict(memory)
    normalized_recall_type = normalize_optional_text(recall_type)

    sql = "SELECT * FROM memory_events WHERE memory_id = ? AND event_type = ?"
    params: list[Any] = [int(memory_id), RECALL_EVENT_TYPE]
    if normalized_recall_type:
        sql += " AND json_extract(payload_json, '$.recall_type') = ?"
        params.append(normalized_recall_type)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(safe_limit)
    rows = conn.execute(sql, params).fetchall()
    items = [memory_event_to_dict(row, row_to_dict=row_to_dict) for row in rows]

    recorded_event_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM memory_events WHERE memory_id = ? AND event_type = ?",
            (int(memory_id), RECALL_EVENT_TYPE),
        ).fetchone()[0]
    )
    recall_count = int(current.get("recall_count") or 0)
    legacy_unattributed = max(recall_count - recorded_event_count, 0)
    sources = sorted(
        {
            str((item.get("payload") or {}).get("source") or "unspecified")
            for item in items
        }
    )
    types = sorted(
        {
            str((item.get("payload") or {}).get("recall_type") or "manual")
            for item in items
        }
    )

    return {
        "schema": RECALL_TELEMETRY_SCHEMA,
        "status": "ok",
        "read_only": True,
        "memory_id": int(memory_id),
        "importance_score": float(current.get("importance_score") or 0.0),
        "recall_count": recall_count,
        "recorded_event_count": recorded_event_count,
        "legacy_unattributed_recall_count": legacy_unattributed,
        "last_recalled_at": current.get("last_recalled_at"),
        "filter": {"recall_type": normalized_recall_type},
        "sources_in_page": sources,
        "recall_types_in_page": types,
        "count": len(items),
        "limit": safe_limit,
        "items": items,
        "invariants": {
            "importance_is_not_reconstructed": True,
            "legacy_recall_history_is_not_invented": True,
            "events_are_append_only": True,
            "read_only": True,
        },
    }
