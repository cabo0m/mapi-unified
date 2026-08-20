from __future__ import annotations

"""Workspace read-only payload helpers."""

from typing import Any


def get_workspace_info_payload(conn: Any, *, workspace_key: str = "default") -> dict[str, Any]:
    ws_row = conn.execute(
        "SELECT * FROM workspaces WHERE workspace_key = ?",
        (workspace_key,),
    ).fetchone()
    if ws_row is None:
        return {"status": "not_found", "workspace_key": workspace_key}
    ws = dict(ws_row)
    members_rows = conn.execute(
        """
        SELECT u.external_user_key, u.display_name, u.status AS user_status,
               wm.role_code, wm.status AS membership_status, wm.created_at AS joined_at
        FROM workspace_memberships wm
        JOIN users u ON u.id = wm.user_id
        WHERE wm.workspace_id = ?
        ORDER BY wm.created_at ASC
        """,
        (ws["id"],),
    ).fetchall()
    members = [dict(r) for r in members_rows]
    memory_counts = conn.execute(
        """
        SELECT visibility_scope, COUNT(*) AS cnt
        FROM memories WHERE workspace_id = ?
        GROUP BY visibility_scope
        """,
        (ws["id"],),
    ).fetchall()
    scope_distribution = {r["visibility_scope"]: r["cnt"] for r in memory_counts}
    return {
        "workspace": ws,
        "member_count": len(members),
        "members": members,
        "memory_scope_distribution": scope_distribution,
    }
