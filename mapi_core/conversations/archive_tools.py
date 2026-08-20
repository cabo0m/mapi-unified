from __future__ import annotations

"""Conversation archive payloads."""

import sqlite3
from typing import Any


def archive_conversation_payload(
    conn: Any,
    *,
    content: str,
    title: str | None = None,
    source: str = "manual",
    project_key: str | None = None,
    workspace_key: str = "default",
    user_key: str = "owner",
    tags: str | None = None,
    conversation_id: str | None = None,
    conversation_archive: Any,
) -> dict[str, Any]:
    if not content or not content.strip():
        return {"status": "error", "error": "content nie moĹĽe byÄ‡ pusty"}
    if source not in conversation_archive.VALID_SOURCES:
        return {"status": "error", "error": f"source musi byÄ‡ jednym z: {sorted(conversation_archive.VALID_SOURCES)}"}
    try:
        return conversation_archive.store_conversation(
            conn,
            content=content.strip(),
            title=title,
            source=source,
            project_key=project_key,
            workspace_key=workspace_key,
            user_key=user_key,
            tags=tags,
            conversation_id=conversation_id,
        )
    except sqlite3.IntegrityError:
        return {"status": "error", "error": f"conversation_id '{conversation_id}' juĹĽ istnieje"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def get_conversation_payload(
    conn: Any,
    *,
    conversation_id: str,
    conversation_archive: Any,
) -> dict[str, Any]:
    if not conversation_id:
        return {"status": "error", "error": "conversation_id wymagane"}
    try:
        row = conversation_archive.get_conversation_by_id(conn, conversation_id)
        if row is None:
            return {"status": "not_found", "conversation_id": conversation_id}
        return {"status": "ok", "conversation": row}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def list_conversations_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    workspace_key: str | None = None,
    user_key: str | None = None,
    limit: int = 20,
    offset: int = 0,
    conversation_archive: Any,
) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        limit = 20
    try:
        result = conversation_archive.list_conversations(
            conn,
            project_key=project_key,
            workspace_key=workspace_key,
            user_key=user_key,
            limit=limit,
            offset=offset,
        )
        return {
            "status": "ok",
            "total": result["total"],
            "count": len(result["items"]),
            "offset": offset,
            "conversations": result["items"],
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def search_verbatim_payload(
    conn: Any,
    *,
    query: str,
    scope: str = "all",
    project_key: str | None = None,
    limit: int = 20,
    conversation_archive: Any,
) -> dict[str, Any]:
    if not query or not query.strip():
        return {"status": "error", "error": "query nie moĹĽe byÄ‡ pusty"}
    if scope not in conversation_archive.VALID_SCOPES:
        return {"status": "error", "error": f"scope musi byÄ‡ jednym z: {sorted(conversation_archive.VALID_SCOPES)}"}
    if limit < 1 or limit > 100:
        limit = 20
    q = query.strip()
    try:
        mem_results: list[dict] = []
        conv_results: list[dict] = []
        if scope in ("all", "memories"):
            mem_results = conversation_archive.search_verbatim_memories(conn, q, project_key=project_key, limit=limit)
        if scope in ("all", "conversations"):
            conv_results = conversation_archive.search_verbatim_conversations(conn, q, project_key=project_key, limit=limit)
        return {
            "status": "ok",
            "query": q,
            "scope": scope,
            "memories_count": len(mem_results),
            "conversations_count": len(conv_results),
            "memories": mem_results,
            "conversations": conv_results,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
