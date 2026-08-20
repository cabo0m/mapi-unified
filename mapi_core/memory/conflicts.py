from __future__ import annotations

"""Read-only memory conflict payloads."""

from typing import Any, Callable


def list_conflicted_memories_payload(
    conn: Any,
    *,
    limit: int = 20,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": 'limit musi byÄ‡ w zakresie 1..1000'}
    rows = conn.execute(
        "SELECT * FROM memories WHERE COALESCE(contradiction_flag, 0) = 1 ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {"count": len(rows), "items": [row_to_dict(row) for row in rows], "limit": limit}


def get_conflict_pairs_payload(
    conn: Any,
    *,
    memory_id: int | None = None,
    limit: int = 100,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": 'limit musi byÄ‡ w zakresie 1..1000'}
    sql = "SELECT * FROM memory_links WHERE relation_type = 'contradicts'"
    params: list[Any] = []
    if memory_id is not None:
        sql += " AND (from_memory_id = ? OR to_memory_id = ?)"
        params.extend([memory_id, memory_id])
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return {"count": len(rows), "items": [row_to_dict(row) for row in rows], "memory_id": memory_id, "limit": limit}
