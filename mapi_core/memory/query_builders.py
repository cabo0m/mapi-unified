from __future__ import annotations

"""Small SQL query-building helpers for memory reads."""

import math
import re
from typing import Any, Callable

_IMPORTANT_SHORT_TERMS = {"ai", "api", "fb", "llm", "mcp", "ui", "ux"}
_TEXT_QUERY_STOPWORDS = {
    "and",
    "are",
    "dla",
    "jest",
    "oraz",
    "pod",
    "przed",
    "przez",
    "przy",
    "się",
    "sie",
    "the",
}


def memory_order_clause(sort_by: str) -> str:
    order_map = {
        "active": "importance_score DESC, recall_count DESC, id DESC",
        "recent": "id DESC",
        "created_at_desc": "COALESCE(created_at, '') DESC, id DESC",
        "created_at_asc": "COALESCE(created_at, '') ASC, id ASC",
        "recalled": "recall_count DESC, importance_score DESC, id DESC",
        "validated": "COALESCE(last_validated_at, '') DESC, importance_score DESC, id DESC",
    }
    if sort_by not in order_map:
        raise ValueError(f"Nieobsługiwane sort_by: {sort_by}")
    return order_map[sort_by]


def text_search_terms(text_query: str | None) -> list[str]:
    normalized = (text_query or "").strip()
    if not normalized:
        return []

    terms: list[str] = []
    seen: set[str] = set()
    for raw_term in re.findall(r"[\w]+", normalized, flags=re.UNICODE):
        term = raw_term.strip()
        folded = term.lower()
        if not folded:
            continue
        if folded in _TEXT_QUERY_STOPWORDS:
            continue
        if len(folded) < 3 and folded not in _IMPORTANT_SHORT_TERMS:
            continue
        if folded in seen:
            continue
        seen.add(folded)
        terms.append(term)
    return terms


def relaxed_text_term_threshold(term_count: int) -> int:
    if term_count <= 0:
        return 0
    if term_count <= 3:
        return term_count
    if term_count <= 6:
        return term_count - 1
    return max(4, int(math.ceil(term_count * 0.7)))


def normalize_project_key_values(
    project_key: str | None,
    project_key_values: list[str] | tuple[str, ...] | None,
    *,
    normalize_optional_text: Callable[[Any], str | None],
) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw_value in list(project_key_values or []) + [project_key]:
        normalized = normalize_optional_text(raw_value)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        values.append(normalized)
    return values


def memory_query_parts(
    *,
    limit: int,
    min_importance: float,
    sort_by: str,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    state_code: str | None = None,
    truth_kind: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    project_key_values: list[str] | tuple[str, ...] | None = None,
    project_key_mode: str = "exact",
    conversation_key: str | None = None,
    parent_memory_id: int | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
    visibility_scope: str | None = None,
    workspace_id: int | None = None,
    actor: Any | None = None,
    text_match_mode: str = "phrase",
    exclude_tags: list[str] | tuple[str, ...] | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_layer_code: Callable[[Any], str | None],
    normalize_area_code: Callable[[Any], str | None],
    normalize_state_code: Callable[[Any], str | None],
    normalize_truth_kind: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    build_memory_visibility_filter: Callable[[Any], tuple[str, list[Any]]],
) -> tuple[str, list[Any], dict[str, Any]]:
    if limit < 1 or limit > 1000:
        raise ValueError("limit musi byÄ‡ w zakresie 1..1000")

    sql = "SELECT * FROM memories WHERE importance_score >= ?"
    params: list[Any] = [float(min_importance)]

    normalized_memory_type = normalize_optional_text(memory_type)
    normalized_tag = normalize_optional_text(tag)
    normalized_text_query = normalize_optional_text(text_query)
    normalize_optional_text(effective_owner_key)
    normalize_optional_text(effective_owner_type)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_project_key_mode = normalize_optional_text(project_key_mode) or "exact"
    normalized_project_key_values = normalize_project_key_values(
        normalized_project_key,
        project_key_values,
        normalize_optional_text=normalize_optional_text,
    )
    normalized_conversation_key = normalize_optional_text(conversation_key)
    normalized_layer_code = normalize_layer_code(layer_code)
    normalized_area_code = normalize_area_code(area_code)
    normalized_state_code = normalize_state_code(state_code)
    normalized_truth_kind = normalize_truth_kind(truth_kind)
    normalized_scope_code = normalize_scope_code(scope_code)
    normalized_visibility_scope = normalize_optional_text(visibility_scope)
    normalized_exclude_tags = [
        normalized
        for normalized in (normalize_optional_text(value) for value in (exclude_tags or ()))
        if normalized
    ]

    if normalized_memory_type:
        sql += " AND memory_type = ?"
        params.append(normalized_memory_type)
    if normalized_tag:
        sql += " AND COALESCE(tags, '') LIKE ?"
        params.append(f"%{normalized_tag}%")
    normalized_text_match_mode = normalize_optional_text(text_match_mode) or "phrase"
    if normalized_text_match_mode not in {"phrase", "relaxed"}:
        raise ValueError("text_match_mode musi być 'phrase' albo 'relaxed'")

    if normalized_text_query:
        haystack_sql = "(content || ' ' || COALESCE(summary_short, '') || ' ' || COALESCE(tags, ''))"
        sql += " AND (content LIKE ? OR COALESCE(summary_short, '') LIKE ? OR COALESCE(tags, '') LIKE ?)"
        like_value = f"%{normalized_text_query}%"
        params.extend([like_value, like_value, like_value])
        if normalized_text_match_mode == "relaxed":
            terms = text_search_terms(normalized_text_query)
            threshold = relaxed_text_term_threshold(len(terms))
            if threshold > 0 and len(terms) >= 2:
                term_score_sql = " + ".join(
                    [f"CASE WHEN {haystack_sql} LIKE ? THEN 1 ELSE 0 END" for _ in terms]
                )
                sql = sql[:-1] + f" OR ({term_score_sql}) >= ?)"
                params.extend([f"%{term}%" for term in terms])
                params.append(threshold)
    if normalized_layer_code:
        sql += " AND layer_code = ?"
        params.append(normalized_layer_code)
    if normalized_area_code:
        sql += " AND area_code = ?"
        params.append(normalized_area_code)
    if normalized_state_code:
        sql += " AND state_code = ?"
        params.append(normalized_state_code)
    if normalized_truth_kind:
        sql += " AND truth_kind = ?"
        params.append(normalized_truth_kind)
    if normalized_scope_code:
        sql += " AND scope_code = ?"
        params.append(normalized_scope_code)
    if normalized_project_key:
        if len(normalized_project_key_values) <= 1:
            sql += " AND project_key = ?"
            params.append(normalized_project_key_values[0] if normalized_project_key_values else normalized_project_key)
        else:
            placeholders = ", ".join(["?" for _ in normalized_project_key_values])
            sql += f" AND project_key IN ({placeholders})"
            params.extend(normalized_project_key_values)
    if normalized_exclude_tags:
        exclude_like_sql = " OR ".join(["COALESCE(tags, '') LIKE ?" for _ in normalized_exclude_tags])
        sql += f" AND NOT ({exclude_like_sql})"
        params.extend([f"%{tag}%" for tag in normalized_exclude_tags])
    if normalized_conversation_key:
        sql += " AND conversation_key = ?"
        params.append(normalized_conversation_key)
    if parent_memory_id is not None:
        if int(parent_memory_id) < 1:
            raise ValueError("parent_memory_id musi byÄ‡ >= 1")
        sql += " AND parent_memory_id = ?"
        params.append(int(parent_memory_id))

    if actor is not None:
        visibility_sql, visibility_params = build_memory_visibility_filter(actor)
        sql += f" AND {visibility_sql}"
        params.extend(visibility_params)
    elif normalized_visibility_scope:
        sql += " AND visibility_scope = ?"
        params.append(normalized_visibility_scope)
    if workspace_id is not None:
        sql += " AND workspace_id = ?"
        params.append(int(workspace_id))

    sql += f" ORDER BY {memory_order_clause(sort_by)} LIMIT ?"
    params.append(int(limit))

    filters = {
        "limit": int(limit),
        "memory_type": normalized_memory_type,
        "tag": normalized_tag,
        "text_query": normalized_text_query,
        "layer_code": normalized_layer_code,
        "area_code": normalized_area_code,
        "state_code": normalized_state_code,
        "truth_kind": normalized_truth_kind,
        "scope_code": normalized_scope_code,
        "project_key": normalized_project_key,
        "project_key_mode": normalized_project_key_mode,
        "project_key_values": normalized_project_key_values if normalized_project_key else None,
        "exclude_tags": normalized_exclude_tags,
        "conversation_key": normalized_conversation_key,
        "parent_memory_id": None if parent_memory_id is None else int(parent_memory_id),
        "min_importance": float(min_importance),
        "sort_by": sort_by,
        "visibility_scope": normalized_visibility_scope,
        "workspace_id": workspace_id,
        "text_match_mode": normalized_text_match_mode,
    }
    return sql, params, filters
