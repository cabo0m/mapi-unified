from __future__ import annotations

"""Memory queue payload helpers."""

from typing import Any, Callable


def list_revalidation_queue_payload(
    conn: Any,
    *,
    limit: int = 20,
    validated_before: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    normalize_layer_code: Callable[[Any], str | None],
    normalize_area_code: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
    apply_effective_owner: Callable[[Any, dict[str, Any]], dict[str, Any]],
    filter_items_by_effective_owner: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}

    normalized_scope = normalize_scope_code(scope_code)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_layer = normalize_layer_code(layer_code)
    normalized_area = normalize_area_code(area_code)
    normalized_memory_type = normalize_optional_text(memory_type)
    normalized_tag = normalize_optional_text(tag)
    normalized_text_query = normalize_optional_text(text_query)
    normalized_validated_before = normalize_optional_text(validated_before)

    sql = "SELECT * FROM memories WHERE activity_state != 'archived' AND state_code = 'validated'"
    params: list[Any] = []

    # revalidation_due_at is the canonical scheduling signal. Historical or
    # immutable records intentionally have it cleared by the hygiene policy
    # and must not re-enter the queue merely because last_validated_at is NULL.
    if normalized_validated_before:
        sql += (
            " AND ((revalidation_due_at IS NOT NULL AND revalidation_due_at <= ?) "
            "OR (revalidation_due_at IS NULL AND last_validated_at IS NOT NULL AND last_validated_at < ?))"
        )
        params.extend([normalized_validated_before, normalized_validated_before])
    else:
        sql += " AND revalidation_due_at IS NOT NULL"
    if normalized_scope:
        sql += " AND scope_code = ?"
        params.append(normalized_scope)
    if normalized_project_key:
        sql += " AND project_key = ?"
        params.append(normalized_project_key)
    if normalized_layer:
        sql += " AND layer_code = ?"
        params.append(normalized_layer)
    if normalized_area:
        sql += " AND area_code = ?"
        params.append(normalized_area)
    if normalized_memory_type:
        sql += " AND memory_type = ?"
        params.append(normalized_memory_type)
    if normalized_tag:
        sql += " AND COALESCE(tags, '') LIKE ?"
        params.append(f"%{normalized_tag}%")
    if normalized_text_query:
        sql += " AND (content LIKE ? OR COALESCE(summary_short, '') LIKE ? OR COALESCE(tags, '') LIKE ?)"
        like_value = f"%{normalized_text_query}%"
        params.extend([like_value, like_value, like_value])

    sql += " ORDER BY COALESCE(revalidation_due_at, last_validated_at, '') ASC, importance_score DESC, id DESC LIMIT ?"
    params.append(int(limit))

    normalized_effective_owner_key = normalize_optional_text(effective_owner_key)
    normalized_effective_owner_type = normalize_optional_text(effective_owner_type)
    rows = conn.execute(sql, params).fetchall()
    items = [
        apply_effective_owner(conn, apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        for row in rows
    ]
    items = filter_items_by_effective_owner(
        items,
        effective_owner_key=normalized_effective_owner_key,
        effective_owner_type=normalized_effective_owner_type,
    )

    return {
        "count": len(items),
        "items": items,
        "queue_state": "revalidation",
        "filters": {
            "limit": int(limit),
            "validated_before": normalized_validated_before,
            "scope_code": normalized_scope,
            "project_key": normalized_project_key,
            "layer_code": normalized_layer,
            "area_code": normalized_area,
            "memory_type": normalized_memory_type,
            "tag": normalized_tag,
            "text_query": normalized_text_query,
            "effective_owner_key": normalized_effective_owner_key,
            "effective_owner_type": normalized_effective_owner_type,
        },
    }


def list_expired_memories_payload(
    conn: Any,
    *,
    limit: int = 20,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    normalize_layer_code: Callable[[Any], str | None],
    normalize_area_code: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
    apply_effective_owner: Callable[[Any, dict[str, Any]], dict[str, Any]],
    filter_items_by_effective_owner: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}
    normalized_as_of = normalize_optional_text(as_of) or utc_now_iso()
    normalized_scope = normalize_scope_code(scope_code)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_layer = normalize_layer_code(layer_code)
    normalized_area = normalize_area_code(area_code)
    normalized_memory_type = normalize_optional_text(memory_type)
    normalized_tag = normalize_optional_text(tag)
    normalized_text_query = normalize_optional_text(text_query)

    sql = "SELECT * FROM memories WHERE valid_to IS NOT NULL AND valid_to <= ?"
    params: list[Any] = [normalized_as_of]
    if normalized_scope:
        sql += " AND scope_code = ?"
        params.append(normalized_scope)
    if normalized_project_key:
        sql += " AND project_key = ?"
        params.append(normalized_project_key)
    if normalized_layer:
        sql += " AND layer_code = ?"
        params.append(normalized_layer)
    if normalized_area:
        sql += " AND area_code = ?"
        params.append(normalized_area)
    if normalized_memory_type:
        sql += " AND memory_type = ?"
        params.append(normalized_memory_type)
    if normalized_tag:
        sql += " AND COALESCE(tags, '') LIKE ?"
        params.append(f"%{normalized_tag}%")
    if normalized_text_query:
        sql += " AND (content LIKE ? OR COALESCE(summary_short, '') LIKE ? OR COALESCE(tags, '') LIKE ?)"
        like_value = f"%{normalized_text_query}%"
        params.extend([like_value, like_value, like_value])
    sql += " ORDER BY valid_to ASC, id DESC LIMIT ?"
    params.append(int(limit))

    normalized_effective_owner_key = normalize_optional_text(effective_owner_key)
    normalized_effective_owner_type = normalize_optional_text(effective_owner_type)
    rows = conn.execute(sql, params).fetchall()
    items = [
        apply_effective_owner(conn, apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        for row in rows
    ]
    items = filter_items_by_effective_owner(
        items,
        effective_owner_key=normalized_effective_owner_key,
        effective_owner_type=normalized_effective_owner_type,
    )
    return {
        "count": len(items),
        "items": items,
        "queue_state": "expired",
        "filters": {
            "limit": int(limit),
            "as_of": normalized_as_of,
            "scope_code": normalized_scope,
            "project_key": normalized_project_key,
            "layer_code": normalized_layer,
            "area_code": normalized_area,
            "memory_type": normalized_memory_type,
            "tag": normalized_tag,
            "text_query": normalized_text_query,
            "effective_owner_key": normalized_effective_owner_key,
            "effective_owner_type": normalized_effective_owner_type,
        },
    }


def list_review_queue_payload(
    conn: Any,
    *,
    limit: int = 20,
    memory_type: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    parent_memory_id: int | None = None,
    sort_by: str = "recent",
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
    memory_query_parts: Callable[..., tuple[str, list[Any], dict[str, Any]]],
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
    apply_effective_owner: Callable[[Any, dict[str, Any]], dict[str, Any]],
    filter_items_by_effective_owner: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    sql, params, filters = memory_query_parts(
        limit=limit,
        memory_type=memory_type,
        tag=tag,
        min_importance=0.0,
        sort_by=sort_by,
        text_query=text_query,
        layer_code=layer_code,
        area_code=area_code,
        state_code="candidate",
        scope_code=scope_code,
        project_key=project_key,
        parent_memory_id=parent_memory_id,
    )
    normalized_effective_owner_key = normalize_optional_text(effective_owner_key)
    normalized_effective_owner_type = normalize_optional_text(effective_owner_type)
    rows = conn.execute(sql, params).fetchall()
    items = [
        apply_effective_owner(conn, apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        for row in rows
    ]
    items = filter_items_by_effective_owner(
        items,
        effective_owner_key=normalized_effective_owner_key,
        effective_owner_type=normalized_effective_owner_type,
    )
    filters["effective_owner_key"] = normalized_effective_owner_key
    filters["effective_owner_type"] = normalized_effective_owner_type
    return {
        "count": len(items),
        "items": items,
        "filters": filters,
        "queue_state": "candidate",
    }


def list_overdue_memory_queue_payload(
    conn: Any,
    *,
    limit: int = 20,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
    state_code: str | None,
    due_column: str,
    queue_state: str,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
    apply_effective_owner: Callable[[Any, dict[str, Any]], dict[str, Any]],
    filter_items_by_effective_owner: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}
    normalized_as_of = normalize_optional_text(as_of) or utc_now_iso()
    normalized_scope = normalize_scope_code(scope_code)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_owner_role = normalize_optional_text(owner_role)
    normalized_owner_id = normalize_optional_text(owner_id)
    normalized_effective_owner_key = normalize_optional_text(effective_owner_key)
    normalized_effective_owner_type = normalize_optional_text(effective_owner_type)

    if due_column not in {"review_due_at", "revalidation_due_at", "expired_due_at"}:
        raise ValueError(f"Unsupported overdue due column: {due_column}")

    if due_column == "expired_due_at":
        sql = "SELECT * FROM memories WHERE valid_to IS NOT NULL AND valid_to <= ?"
    else:
        sql = f"SELECT * FROM memories WHERE state_code = ? AND {due_column} IS NOT NULL AND {due_column} <= ?"
    params: list[Any] = [normalized_as_of] if due_column == "expired_due_at" else [state_code, normalized_as_of]

    if normalized_scope:
        sql += " AND scope_code = ?"
        params.append(normalized_scope)
    if normalized_project_key:
        sql += " AND project_key = ?"
        params.append(normalized_project_key)
    if due_column != "expired_due_at":
        if normalized_owner_role:
            sql += " AND owner_role = ?"
            params.append(normalized_owner_role)
        if normalized_owner_id:
            sql += " AND owner_id = ?"
            params.append(normalized_owner_id)
    sql += " ORDER BY valid_to ASC, id DESC" if due_column == "expired_due_at" else f" ORDER BY {due_column} ASC, id DESC LIMIT ?"
    if due_column != "expired_due_at":
        params.append(int(limit))

    rows = conn.execute(sql, params).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = apply_effective_owner(conn, apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        if due_column == "expired_due_at":
            due_at = normalize_optional_text(item.get("expired_due_at"))
            if due_at is None or due_at > normalized_as_of:
                continue
            if normalized_owner_role and normalize_optional_text(item.get("owner_role")) != normalized_owner_role:
                continue
            if normalized_owner_id and normalize_optional_text(item.get("owner_id")) != normalized_owner_id:
                continue
            if normalized_effective_owner_key is not None or normalized_effective_owner_type is not None:
                filtered = filter_items_by_effective_owner(
                    [item],
                    effective_owner_key=normalized_effective_owner_key,
                    effective_owner_type=normalized_effective_owner_type,
                )
                if not filtered:
                    continue
        items.append(item)

    if due_column != "expired_due_at":
        items = filter_items_by_effective_owner(
            items,
            effective_owner_key=normalized_effective_owner_key,
            effective_owner_type=normalized_effective_owner_type,
        )

    return {
        "count": len(items),
        "items": items if due_column != "expired_due_at" else items[: int(limit)],
        "queue_state": queue_state,
        "filters": {
            "limit": int(limit),
            "as_of": normalized_as_of,
            "scope_code": normalized_scope,
            "project_key": normalized_project_key,
            "owner_role": normalized_owner_role,
            "owner_id": normalized_owner_id,
            "effective_owner_key": normalized_effective_owner_key,
            "effective_owner_type": normalized_effective_owner_type,
        },
    }


def list_duplicate_candidates_admin_payload(
    conn: Any,
    *,
    limit: int = 20,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
    get_duplicate_candidates: Callable[[Any], list[dict[str, Any]]],
    require_memory_row: Callable[[Any, int], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
    apply_effective_owner: Callable[..., dict[str, Any]],
    memory_matches_operational_filters: Callable[..., bool],
    get_or_create_duplicate_review_item: Callable[[Any, int, int], dict[str, Any]],
    filter_items_by_effective_owner: Callable[..., list[dict[str, Any]]],
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    normalize_layer_code: Callable[[Any], str | None],
    normalize_area_code: Callable[[Any], str | None],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}
    normalized_effective_owner_key = normalize_optional_text(effective_owner_key)
    normalized_effective_owner_type = normalize_optional_text(effective_owner_type)
    candidates = get_duplicate_candidates(conn)
    items: list[dict[str, Any]] = []
    for candidate in candidates:
        canonical_memory = apply_effective_owner(
            conn,
            apply_ownership_defaults(enrich_memory_dict(row_to_dict(require_memory_row(conn, int(candidate["canonical_memory_id"]))))),
        )
        duplicate_memory = apply_effective_owner(
            conn,
            apply_ownership_defaults(enrich_memory_dict(row_to_dict(require_memory_row(conn, int(candidate["duplicate_memory_id"]))))),
        )
        if not memory_matches_operational_filters(
            canonical_memory,
            scope_code=scope_code,
            project_key=project_key,
            layer_code=layer_code,
            area_code=area_code,
            memory_type=memory_type,
            tag=tag,
            text_query=text_query,
        ):
            continue
        raw_review = get_or_create_duplicate_review_item(
            conn,
            int(candidate["canonical_memory_id"]),
            int(candidate["duplicate_memory_id"]),
        )
        raw_review.setdefault("project_key", canonical_memory.get("project_key"))
        raw_review.setdefault("scope_code", canonical_memory.get("scope_code"))
        duplicate_review = apply_effective_owner(conn, raw_review, owner_field=None)
        if normalized_effective_owner_key is not None or normalized_effective_owner_type is not None:
            filtered_duplicate_review = filter_items_by_effective_owner(
                [{"duplicate_review": duplicate_review}],
                effective_owner_key=normalized_effective_owner_key,
                effective_owner_type=normalized_effective_owner_type,
                memory_field="duplicate_review",
            )
            if not filtered_duplicate_review:
                continue
        items.append(
            {
                **candidate,
                "canonical_memory": canonical_memory,
                "duplicate_memory": duplicate_memory,
                "duplicate_review": duplicate_review,
            }
        )
        if len(items) >= int(limit):
            break
    conn.commit()
    return {
        "count": len(items),
        "items": items,
        "queue_state": "duplicates",
        "filters": {
            "limit": int(limit),
            "scope_code": normalize_scope_code(scope_code),
            "project_key": normalize_optional_text(project_key),
            "layer_code": normalize_layer_code(layer_code),
            "area_code": normalize_area_code(area_code),
            "memory_type": normalize_optional_text(memory_type),
            "tag": normalize_optional_text(tag),
            "text_query": normalize_optional_text(text_query),
            "effective_owner_key": normalized_effective_owner_key,
            "effective_owner_type": normalized_effective_owner_type,
        },
    }


def list_overdue_duplicate_queue_payload(
    *,
    limit: int = 20,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
    list_duplicate_candidates_admin: Callable[..., dict[str, Any]],
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}
    normalized_as_of = normalize_optional_text(as_of) or utc_now_iso()
    normalized_owner_role = normalize_optional_text(owner_role)
    normalized_owner_id = normalize_optional_text(owner_id)
    normalized_effective_owner_key = normalize_optional_text(effective_owner_key)
    normalized_effective_owner_type = normalize_optional_text(effective_owner_type)
    queue = list_duplicate_candidates_admin(
        limit=1000,
        scope_code=scope_code,
        project_key=project_key,
        effective_owner_key=normalized_effective_owner_key,
        effective_owner_type=normalized_effective_owner_type,
    )
    items: list[dict[str, Any]] = []
    for item in queue["items"]:
        review_item = item.get("duplicate_review") or {}
        due_at = normalize_optional_text(review_item.get("duplicate_due_at"))
        status_value = normalize_optional_text(review_item.get("status")) or "open"
        if status_value != "open":
            continue
        if due_at is None or due_at > normalized_as_of:
            continue
        if normalized_owner_role and normalize_optional_text(review_item.get("owner_role")) != normalized_owner_role:
            continue
        if normalized_owner_id and normalize_optional_text(review_item.get("owner_id")) != normalized_owner_id:
            continue
        items.append(item)
    return {
        "count": len(items),
        "items": items[: int(limit)],
        "queue_state": "duplicate_overdue",
        "filters": {
            "limit": int(limit),
            "as_of": normalized_as_of,
            "scope_code": normalize_scope_code(scope_code),
            "project_key": normalize_optional_text(project_key),
            "owner_role": normalized_owner_role,
            "owner_id": normalized_owner_id,
            "effective_owner_key": normalized_effective_owner_key,
            "effective_owner_type": normalized_effective_owner_type,
        },
    }
