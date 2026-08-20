from __future__ import annotations

"""Read-only sleep run payloads."""

from typing import Any, Callable


def list_sleep_runs_payload(
    conn: Any,
    *,
    limit: int = 20,
    status: str | None = None,
    mode: str | None = None,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": 'limit musi byÄ‡ w zakresie 1..1000'}
    sql = "SELECT * FROM sleep_runs WHERE 1 = 1"
    params: list[Any] = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if mode:
        sql += " AND mode = ?"
        params.append(mode)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return {"count": len(rows), "items": [row_to_dict(row) for row in rows], "filters": {"limit": limit, "status": status, "mode": mode}}


def get_sleep_run_payload(
    conn: Any,
    *,
    run_id: int,
    require_sleep_run_row: Callable[[Any, int], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    run = require_sleep_run_row(conn, int(run_id))
    action_summary_rows = conn.execute(
        "SELECT action_type, COUNT(*) AS count FROM sleep_run_actions WHERE run_id = ? GROUP BY action_type ORDER BY action_type ASC",
        (int(run_id),),
    ).fetchall()
    return {"sleep_run": row_to_dict(run), "action_summary": [row_to_dict(row) for row in action_summary_rows]}


def get_sleep_run_actions_payload(
    conn: Any,
    *,
    run_id: int,
    limit: int = 200,
    require_sleep_run_row: Callable[[Any, int], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": 'limit musi byÄ‡ w zakresie 1..1000'}
    require_sleep_run_row(conn, int(run_id))
    rows = conn.execute("SELECT * FROM sleep_run_actions WHERE run_id = ? ORDER BY id ASC LIMIT ?", (int(run_id), limit)).fetchall()
    return {"run_id": int(run_id), "count": len(rows), "items": [row_to_dict(row) for row in rows], "limit": limit}
