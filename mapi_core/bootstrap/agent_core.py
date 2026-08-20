from __future__ import annotations

"""Neutral, project-scoped bootstrap context for public MAPI clients."""

from typing import Any, Callable

from mapi_core.memory.project_keys import bootstrap_project_key_values
from memory_bootstrap_policy import BootstrapPolicy, build_project_anchors_sql, build_recent_project_sql


def agent_workshop_index() -> list[dict[str, Any]]:
    return [
        {
            "area": "memory",
            "purpose": "Create, search, inspect and relate durable memories.",
            "audience": "agent",
            "risk": "low",
        },
        {
            "area": "timeline",
            "purpose": "Inspect project and memory history.",
            "audience": "reader",
            "risk": "low",
        },
        {
            "area": "governance",
            "purpose": "Inspect quality, review queues and lifecycle state.",
            "audience": "maintainer",
            "risk": "medium",
        },
    ]


def agent_recommended_next_calls() -> dict[str, str]:
    return {
        "find": "Search before creating a new memory.",
        "read": "Inspect the full memory before relying on it.",
        "links": "Inspect relationships and provenance.",
        "context": "Compose source-bound project context before a complex action.",
        "write": "Use an explicit write or a proposal according to client policy.",
    }


def agent_bootstrap_protocol() -> dict[str, str]:
    return {
        "stage_1": "Select and canonicalize a project key.",
        "stage_2": "Load bounded project anchors and recent project context.",
        "stage_3": "Search relevant memories and inspect their provenance.",
        "stage_4": "Write only through an explicit or proposal path.",
    }


def known_systems_for_project(project_key: str | None) -> list[str]:
    key = str(project_key or "").strip()
    return [key, "MAPI"] if key else ["MAPI"]


def project_purpose_for(project_key: str | None) -> str:
    key = str(project_key or "").strip()
    return f"Durable agent memory and governance context for {key}." if key else "No active project selected."


def _compact_bootstrap_row(row: Any, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    item = row_to_dict(row)
    return {
        "id": item.get("id"),
        "summary_short": item.get("summary_short"),
        "memory_type": item.get("memory_type"),
        "content": item.get("content"),
        "tags": item.get("tags"),
        "importance_score": item.get("importance_score"),
        "confidence_score": item.get("confidence_score"),
        "identity_weight": item.get("identity_weight"),
        "project_key": item.get("project_key"),
        "created_at": item.get("created_at"),
    }


def build_bootstrap_agent_context_payload(
    *,
    project_key: str | None,
    limit: int,
    get_db_connection: Callable[[], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    normalize_optional_text: Callable[[Any], str | None],
) -> dict[str, Any]:
    """Build a bounded, alias-aware and project-scoped bootstrap payload.

    Public MAPI deliberately does not load global identity memories here. Core
    bootstrap rows are restricted to the resolved project namespace so a shared
    deployment cannot blend identity/context across projects.
    """

    requested_project = normalize_optional_text(project_key)
    safe_limit = max(1, min(int(limit or 24), 50))
    if requested_project is None:
        return {
            "status": "ok",
            "schema": "mapi_agent_bootstrap.v2",
            "bootstrap_tool": "bootstrap_agent_context",
            "bootstrap_policy": {
                "name": "shared_memory_bootstrap_policy_v1",
                "requested_project_key": None,
                "project_key": None,
                "project_key_values": [],
                "limit": safe_limit,
                "recent_limit": min(8, safe_limit),
                "project_anchor_tags": [],
            },
            "project": {
                "requested_project_key": None,
                "project_key": None,
                "purpose": "No active project selected.",
                "known_systems": ["MAPI"],
            },
            "current_project": {
                "requested_project_key": None,
                "project_key": None,
                "active_project_key": None,
                "known_systems": ["MAPI"],
            },
            "protocol": agent_bootstrap_protocol(),
            "recommended_next_calls": agent_recommended_next_calls(),
            "workshop_index": agent_workshop_index(),
            "core_memories": [],
            "core_identity": [],
            "project_anchors": [],
            "recent_context": [],
            "recent_project_context": [],
            "recent_memories": [],
            "source_memory_ids": [],
            "no_project_selected": True,
            "safety": {
                "project_scoped": True,
                "global_identity_loaded": False,
                "read_only": True,
            },
        }
    conn = get_db_connection()
    try:
        canonical_project, project_values = bootstrap_project_key_values(
            conn,
            requested_project,
            normalize_optional_text=normalize_optional_text,
        )
        policy = BootstrapPolicy(
            project_key=canonical_project,
            project_key_values=project_values,
            limit=safe_limit,
        )

        placeholders = ",".join("?" for _ in policy.project_key_values)
        core_rows = conn.execute(
            f"""
            SELECT * FROM memories
            WHERE activity_state = 'active'
              AND project_key IN ({placeholders})
            ORDER BY identity_weight DESC, importance_score DESC,
                     confidence_score DESC, id DESC
            LIMIT ?
            """,
            [*policy.project_key_values, policy.safe_limit],
        ).fetchall()

        project_sql, project_params = build_project_anchors_sql(policy)
        recent_sql, recent_params = build_recent_project_sql(policy)
        project_rows = conn.execute(project_sql, project_params).fetchall()
        recent_rows = conn.execute(recent_sql, recent_params).fetchall()
    finally:
        conn.close()

    core_memories = [_compact_bootstrap_row(row, row_to_dict) for row in core_rows]
    project_anchors = [_compact_bootstrap_row(row, row_to_dict) for row in project_rows]
    recent_context = [_compact_bootstrap_row(row, row_to_dict) for row in recent_rows]
    source_memory_ids = sorted(
        {
            int(item["id"])
            for group in (core_memories, project_anchors, recent_context)
            for item in group
            if item.get("id") is not None
        }
    )

    return {
        "status": "ok",
        "schema": "mapi_agent_bootstrap.v2",
        "bootstrap_tool": "bootstrap_agent_context",
        "bootstrap_policy": {
            "name": "shared_memory_bootstrap_policy_v1",
            "requested_project_key": requested_project,
            "project_key": canonical_project,
            "project_key_values": list(policy.project_key_values),
            "limit": policy.safe_limit,
            "recent_limit": policy.safe_recent_limit,
            "project_anchor_tags": list(policy.project_anchor_tags),
        },
        "project": {
            "requested_project_key": requested_project,
            "project_key": canonical_project,
            "purpose": project_purpose_for(canonical_project),
            "known_systems": known_systems_for_project(canonical_project),
        },
        "current_project": {
            "requested_project_key": requested_project,
            "project_key": canonical_project,
            "active_project_key": canonical_project,
            "known_systems": known_systems_for_project(canonical_project),
        },
        "protocol": agent_bootstrap_protocol(),
        "recommended_next_calls": agent_recommended_next_calls(),
        "workshop_index": agent_workshop_index(),
        "core_memories": core_memories,
        "core_identity": core_memories,
        "project_anchors": project_anchors,
        "recent_context": recent_context,
        "recent_project_context": recent_context,
        "recent_memories": recent_context,
        "source_memory_ids": source_memory_ids,
        "safety": {
            "project_scoped": True,
            "global_identity_loaded": False,
            "read_only": True,
        },
    }
