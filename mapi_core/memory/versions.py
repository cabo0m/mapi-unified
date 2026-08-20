from __future__ import annotations

"""Memory version lineage helpers."""

from typing import Any, Callable


def collect_version_lineage(
    conn: Any,
    memory_id: int,
    *,
    require_memory_row: Callable[[Any, int], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> list[dict[str, Any]]:
    to_visit = [int(memory_id)]
    seen: set[int] = set()
    collected: list[dict[str, Any]] = []

    while to_visit:
        current_id = int(to_visit.pop())
        if current_id in seen:
            continue
        seen.add(current_id)
        row = require_memory_row(conn, current_id)
        item = enrich_memory_dict(row_to_dict(row))
        collected.append(item)

        parent_id = item.get("supersedes_memory_id")
        if parent_id is not None:
            to_visit.append(int(parent_id))

        child_rows = conn.execute(
            "SELECT id FROM memories WHERE supersedes_memory_id = ? ORDER BY version ASC, id ASC",
            (current_id,),
        ).fetchall()
        for child_row in child_rows:
            to_visit.append(int(child_row["id"]))

    collected.sort(key=lambda item: (int(item.get("version") or 1), int(item.get("id") or 0)))
    return collected
