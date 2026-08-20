from __future__ import annotations

"""Helpers for duplicate review queue rows."""

from typing import Any, Callable


def duplicate_review_item_to_dict(row: Any, *, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    item = row_to_dict(row)
    item["canonical_memory_id"] = int(item["canonical_memory_id"])
    item["duplicate_memory_id"] = int(item["duplicate_memory_id"])
    return item


def get_or_create_duplicate_review_item(
    conn: Any,
    canonical_memory_id: int,
    duplicate_memory_id: int,
    *,
    row_to_dict: Callable[[Any], dict[str, Any]],
    utc_now_iso: Callable[[], str],
    utc_offset_days_iso: Callable[[int], str],
    compute_sla_days: Callable[[Any, str], int],
) -> dict[str, Any]:
    canonical_id = int(canonical_memory_id)
    duplicate_id = int(duplicate_memory_id)
    row = conn.execute(
        "SELECT * FROM duplicate_review_items WHERE canonical_memory_id = ? AND duplicate_memory_id = ?",
        (canonical_id, duplicate_id),
    ).fetchone()
    if row is None:
        now_iso = utc_now_iso()
        conn.execute(
            """
            INSERT INTO duplicate_review_items (
                canonical_memory_id, duplicate_memory_id, owner_role, owner_id, duplicate_due_at, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                canonical_id,
                duplicate_id,
                "maintainer",
                None,
                utc_offset_days_iso(compute_sla_days(conn, "duplicate")),
                now_iso,
                now_iso,
            ),
        )
        row = conn.execute(
            "SELECT * FROM duplicate_review_items WHERE canonical_memory_id = ? AND duplicate_memory_id = ?",
            (canonical_id, duplicate_id),
        ).fetchone()
    return duplicate_review_item_to_dict(row, row_to_dict=row_to_dict)


def set_duplicate_candidate_sla_payload(
    conn: Any,
    *,
    canonical_memory_id: int,
    duplicate_memory_id: int,
    duplicate_due_at: str | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    status: str = "open",
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    require_memory_row: Callable[[Any, int], Any],
    get_or_create_duplicate_review_item: Callable[[Any, int, int], dict[str, Any]],
    insert_memory_event: Callable[..., dict[str, Any]],
    duplicate_review_item_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    normalized_due_at = normalize_optional_text(duplicate_due_at)
    normalized_owner_role = normalize_optional_text(owner_role)
    normalized_owner_id = normalize_optional_text(owner_id)
    normalized_status = normalize_optional_text(status) or "open"
    if normalized_status not in {"open", "resolved", "ignored"}:
        return {"status": "error", "error": "status musi byÄ‡ jednym z: open, resolved, ignored"}

    require_memory_row(conn, int(canonical_memory_id))
    require_memory_row(conn, int(duplicate_memory_id))
    get_or_create_duplicate_review_item(conn, int(canonical_memory_id), int(duplicate_memory_id))
    updated_at = utc_now_iso()
    conn.execute(
        """
        UPDATE duplicate_review_items
        SET owner_role = COALESCE(?, owner_role),
            owner_id = COALESCE(?, owner_id),
            duplicate_due_at = COALESCE(?, duplicate_due_at),
            status = ?,
            updated_at = ?
        WHERE canonical_memory_id = ? AND duplicate_memory_id = ?
        """,
        (
            normalized_owner_role,
            normalized_owner_id,
            normalized_due_at,
            normalized_status,
            updated_at,
            int(canonical_memory_id),
            int(duplicate_memory_id),
        ),
    )
    row = conn.execute(
        "SELECT * FROM duplicate_review_items WHERE canonical_memory_id = ? AND duplicate_memory_id = ?",
        (int(canonical_memory_id), int(duplicate_memory_id)),
    ).fetchone()
    event = insert_memory_event(
        conn,
        memory_id=int(duplicate_memory_id),
        event_type="duplicate_review.updated",
        payload={
            "canonical_memory_id": int(canonical_memory_id),
            "duplicate_memory_id": int(duplicate_memory_id),
            "duplicate_due_at": normalized_due_at,
            "owner_role": normalized_owner_role,
            "owner_id": normalized_owner_id,
            "status": normalized_status,
        },
    )
    conn.commit()
    return {
        "status": "duplicate_sla_updated",
        "event": event,
        "duplicate_review": duplicate_review_item_to_dict(row),
    }


def bulk_set_duplicate_candidate_sla_payload(
    conn: Any,
    *,
    pairs: list[dict[str, int]],
    duplicate_due_at: str | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    status: str = "open",
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    require_memory_row: Callable[[Any, int], Any],
    get_or_create_duplicate_review_item: Callable[[Any, int, int], dict[str, Any]],
    insert_memory_event: Callable[..., dict[str, Any]],
    duplicate_review_item_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if not pairs:
        return {"status": "error", "error": "pairs nie mogÄ… byÄ‡ puste"}
    normalized_due_at = normalize_optional_text(duplicate_due_at)
    normalized_owner_role = normalize_optional_text(owner_role)
    normalized_owner_id = normalize_optional_text(owner_id)
    normalized_status = normalize_optional_text(status) or "open"
    if normalized_status not in {"open", "resolved", "ignored"}:
        return {"status": "error", "error": "status musi byÄ‡ jednym z: open, resolved, ignored"}

    normalized_pairs: list[tuple[int, int]] = []
    for pair in pairs:
        canonical_memory_id = int(pair["canonical_memory_id"])
        duplicate_memory_id = int(pair["duplicate_memory_id"])
        normalized_pairs.append((canonical_memory_id, duplicate_memory_id))
    normalized_pairs = list(dict.fromkeys(normalized_pairs))

    updated_at = utc_now_iso()
    items: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for canonical_memory_id, duplicate_memory_id in normalized_pairs:
        require_memory_row(conn, canonical_memory_id)
        require_memory_row(conn, duplicate_memory_id)
        get_or_create_duplicate_review_item(conn, canonical_memory_id, duplicate_memory_id)
        conn.execute(
            """
            UPDATE duplicate_review_items
            SET owner_role = COALESCE(?, owner_role),
                owner_id = COALESCE(?, owner_id),
                duplicate_due_at = COALESCE(?, duplicate_due_at),
                status = ?,
                updated_at = ?
            WHERE canonical_memory_id = ? AND duplicate_memory_id = ?
            """,
            (
                normalized_owner_role,
                normalized_owner_id,
                normalized_due_at,
                normalized_status,
                updated_at,
                canonical_memory_id,
                duplicate_memory_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM duplicate_review_items WHERE canonical_memory_id = ? AND duplicate_memory_id = ?",
            (canonical_memory_id, duplicate_memory_id),
        ).fetchone()
        event = insert_memory_event(
            conn,
            memory_id=duplicate_memory_id,
            event_type="duplicate_review.bulk_updated",
            payload={
                "canonical_memory_id": canonical_memory_id,
                "duplicate_memory_id": duplicate_memory_id,
                "duplicate_due_at": normalized_due_at,
                "owner_role": normalized_owner_role,
                "owner_id": normalized_owner_id,
                "status": normalized_status,
            },
        )
        items.append(duplicate_review_item_to_dict(row))
        events.append(event)
    conn.commit()
    return {
        "status": "bulk_duplicate_sla_updated",
        "count": len(items),
        "pairs": [{"canonical_memory_id": a, "duplicate_memory_id": b} for a, b in normalized_pairs],
        "events": events,
        "items": items,
    }
