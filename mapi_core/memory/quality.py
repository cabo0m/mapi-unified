from __future__ import annotations

"""Memory quality gate helpers."""

from typing import Any, Callable

from mapi_core.memory.hygiene import global_project_scope_is_semantically_allowed


def tag_count(tags: str | None, *, normalize_optional_text: Callable[[Any], str | None]) -> int:
    normalized_tags = normalize_optional_text(tags)
    if not normalized_tags:
        return 0
    return len([item for item in (part.strip() for part in normalized_tags.split(",")) if item])


def quality_gate_issues_for_memory(
    memory: dict[str, Any],
    *,
    target_scope_code: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
) -> list[str]:
    target_scope = normalize_scope_code(target_scope_code) or normalize_scope_code(str(memory.get("scope_code") or ""))
    if target_scope != "global":
        return []

    issues: list[str] = []
    summary_short = normalize_optional_text(memory.get("summary_short"))
    content = normalize_optional_text(memory.get("content")) or ""
    confidence_score = float(memory.get("confidence_score") or 0.0)
    memory_type = normalize_optional_text(memory.get("memory_type")) or ""
    project_key = normalize_optional_text(memory.get("project_key"))

    if summary_short is None:
        issues.append("summary_short_required_for_global")
    if tag_count(memory.get("tags"), normalize_optional_text=normalize_optional_text) < 2:
        issues.append("at_least_two_tags_required_for_global")
    if len(content) < 25:
        issues.append("content_too_short_for_global")
    if confidence_score < 0.7:
        issues.append("confidence_too_low_for_global")
    if memory_type == "working":
        issues.append("working_memory_type_not_allowed_for_global")
    if project_key and memory_type == "project_note":
        issues.append("project_note_with_project_key_not_allowed_for_global")

    return issues


def _scope_mismatch_filters(
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
) -> tuple[list[str], list[Any]]:
    filters = [
        "archived_at IS NULL",
        "project_key IS NOT NULL",
        "trim(project_key) <> ''",
        "(scope_code IS NULL OR trim(scope_code) = '' OR scope_code = 'global')",
        "COALESCE(tags, '') NOT LIKE '%allow-global-project-scope%'",
    ]
    params: list[Any] = []
    if project_key is not None:
        filters.append("LOWER(project_key) = LOWER(?)")
        params.append(project_key)
    if scope_code is not None:
        filters.append("scope_code = ?")
        params.append(scope_code)
    if layer_code is not None:
        filters.append("layer_code = ?")
        params.append(layer_code)
    if area_code is not None:
        filters.append("area_code = ?")
        params.append(area_code)
    if memory_type is not None:
        filters.append("memory_type = ?")
        params.append(memory_type)
    if tag is not None:
        filters.append("tags LIKE ?")
        params.append(f"%{tag}%")
    if text_query is not None:
        filters.append("(content LIKE ? OR summary_short LIKE ?)")
        params.extend([f"%{text_query}%", f"%{text_query}%"])
    return filters, params


def _is_real_scope_mismatch(memory: dict[str, Any]) -> bool:
    allowed, _ = global_project_scope_is_semantically_allowed(memory)
    return not allowed


def count_project_scope_mismatches(
    conn: Any,
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
) -> int:
    filters, params = _scope_mismatch_filters(
        project_key=project_key,
        scope_code=scope_code,
        layer_code=layer_code,
        area_code=area_code,
        memory_type=memory_type,
        tag=tag,
        text_query=text_query,
    )
    rows = conn.execute(
        f"SELECT * FROM memories WHERE {' AND '.join(filters)}",
        tuple(params),
    ).fetchall()
    return sum(1 for row in rows if _is_real_scope_mismatch(dict(row)))


def project_scope_mismatch_rows(
    conn: Any,
    *,
    project_key: str | None = None,
    limit: int = 50,
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> list[dict[str, Any]]:
    filters, params = _scope_mismatch_filters(project_key=normalize_optional_text(project_key))
    rows = conn.execute(
        f"""
        SELECT id, content, title, summary_short, memory_type, layer_code, area_code,
               scope_code, project_key, owner_role, owner_id, tags, source_context,
               source_event_ref, created_at
        FROM memories
        WHERE {' AND '.join(filters)}
        ORDER BY id DESC
        LIMIT 500
        """,
        tuple(params),
    ).fetchall()
    mismatches = [row_to_dict(row) for row in rows]
    mismatches = [item for item in mismatches if _is_real_scope_mismatch(item)]
    return mismatches[: max(1, min(int(limit or 50), 500))]


def list_project_scope_mismatches_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    limit: int = 50,
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    items = project_scope_mismatch_rows(
        conn,
        project_key=project_key,
        limit=limit,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=row_to_dict,
    )
    return {"status": "ok", "count": len(items), "items": items}
