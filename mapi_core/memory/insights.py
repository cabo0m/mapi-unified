from __future__ import annotations

"""Read-only operational memory insight payloads."""

from typing import Any, Callable


def layer_stats_payload(conn: Any) -> dict[str, Any]:
    layer_rows = conn.execute(
        "SELECT COALESCE(layer_code, 'unknown') AS layer_code, COUNT(*) AS count, "
        "ROUND(AVG(importance_score), 3) AS avg_importance, ROUND(AVG(confidence_score), 3) AS avg_confidence "
        "FROM memories GROUP BY layer_code ORDER BY count DESC"
    ).fetchall()

    area_rows = conn.execute(
        "SELECT COALESCE(area_code, 'unknown') AS area_code, COUNT(*) AS count "
        "FROM memories GROUP BY area_code ORDER BY count DESC"
    ).fetchall()

    state_rows = conn.execute(
        "SELECT COALESCE(state_code, 'unknown') AS state_code, COUNT(*) AS count "
        "FROM memories GROUP BY state_code ORDER BY count DESC"
    ).fetchall()

    total = conn.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
    active_total = conn.execute(
        "SELECT COUNT(*) AS count FROM memories WHERE COALESCE(activity_state, 'active') = 'active'"
    ).fetchone()["count"]

    return {
        "status": "ok",
        "total_memories": total,
        "active_memories": active_total,
        "by_layer": [dict(r) for r in layer_rows],
        "by_area": [dict(r) for r in area_rows],
        "by_state": [dict(r) for r in state_rows],
    }


def version_lineage_payload(
    conn: Any,
    *,
    memory_id: int,
    collect_version_lineage: Callable[[Any, int], list[dict[str, Any]]],
) -> dict[str, Any]:
    lineage = collect_version_lineage(conn, memory_id)
    return {
        "status": "ok",
        "root_memory_id": memory_id,
        "count": len(lineage),
        "lineage": lineage,
    }
