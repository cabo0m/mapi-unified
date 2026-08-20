from __future__ import annotations

"""Read-only queue observability payloads."""

from typing import Any, Callable


def queue_observability_metrics_payload(
    *,
    validated_before: str | None = None,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
    text_query: str | None = None,
    cross_project_flag_key: str,
    get_db_connection: Callable[[], Any],
    list_review_queue: Callable[..., dict[str, Any]],
    list_revalidation_queue: Callable[..., dict[str, Any]],
    list_expired_memories: Callable[..., dict[str, Any]],
    list_duplicate_candidates_admin: Callable[..., dict[str, Any]],
    list_overdue_review_queue: Callable[..., dict[str, Any]],
    list_overdue_revalidation_queue: Callable[..., dict[str, Any]],
    list_overdue_expired_queue: Callable[..., dict[str, Any]],
    list_overdue_duplicate_queue: Callable[..., dict[str, Any]],
    get_owner_catalog_health_data: Callable[..., dict[str, Any]],
    get_feature_flag_config: Callable[..., dict[str, Any]],
    evaluate_feature_flag_config: Callable[..., dict[str, Any]],
    compatibility_feature_flag: Callable[[dict[str, Any]], dict[str, Any]],
    count_project_scope_mismatches: Callable[..., int],
    safe_event_timestamp: Callable[[str | None], float | None],
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    normalize_layer_code: Callable[[Any], str | None],
    normalize_area_code: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    review_queue = list_review_queue(
        limit=1000,
        memory_type=memory_type,
        scope_code=scope_code,
        project_key=project_key,
        layer_code=layer_code,
        area_code=area_code,
        tag=tag,
        text_query=text_query,
        effective_owner_key=effective_owner_key,
        effective_owner_type=effective_owner_type,
    )
    revalidation_queue = list_revalidation_queue(
        limit=1000,
        validated_before=validated_before,
        scope_code=scope_code,
        project_key=project_key,
        layer_code=layer_code,
        area_code=area_code,
        memory_type=memory_type,
        tag=tag,
        text_query=text_query,
        effective_owner_key=effective_owner_key,
        effective_owner_type=effective_owner_type,
    )
    expired_queue = list_expired_memories(
        limit=1000,
        as_of=as_of,
        scope_code=scope_code,
        project_key=project_key,
        layer_code=layer_code,
        area_code=area_code,
        memory_type=memory_type,
        tag=tag,
        text_query=text_query,
        effective_owner_key=effective_owner_key,
        effective_owner_type=effective_owner_type,
    )
    duplicate_queue = list_duplicate_candidates_admin(
        limit=1000,
        scope_code=scope_code,
        project_key=project_key,
        layer_code=layer_code,
        area_code=area_code,
        memory_type=memory_type,
        tag=tag,
        text_query=text_query,
        effective_owner_key=effective_owner_key,
        effective_owner_type=effective_owner_type,
    )
    overdue_review_queue = list_overdue_review_queue(limit=1000, as_of=as_of, scope_code=scope_code, project_key=project_key)
    overdue_revalidation_queue = list_overdue_revalidation_queue(limit=1000, as_of=as_of, scope_code=scope_code, project_key=project_key)
    overdue_expired_queue = list_overdue_expired_queue(limit=1000, as_of=as_of, scope_code=scope_code, project_key=project_key)
    overdue_duplicate_queue = list_overdue_duplicate_queue(limit=1000, as_of=as_of, scope_code=scope_code, project_key=project_key)

    normalized_scope = normalize_scope_code(scope_code)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_layer = normalize_layer_code(layer_code)
    normalized_area = normalize_area_code(area_code)
    normalized_memory_type = normalize_optional_text(memory_type)
    normalized_tag = normalize_optional_text(tag)
    normalized_text_query = normalize_optional_text(text_query)

    conn = get_db_connection()
    try:
        owner_catalog_health = get_owner_catalog_health_data(conn, project_key=normalized_project_key, scope_code=normalized_scope)
        sql = "SELECT * FROM memories WHERE 1 = 1"
        params: list[Any] = []
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
        memory_rows = conn.execute(sql, params).fetchall()
        event_rows = conn.execute(
            "SELECT * FROM memory_events WHERE event_type IN ('review.draft_created', 'review.approved') ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()

    memory_items = [
        apply_ownership_defaults(enrich_memory_dict(row_to_dict(row)))
        for row in memory_rows
    ]
    total_memories = len(memory_items)
    validated_memories = sum(1 for item in memory_items if item.get("state_code") == "validated")
    superseded_memories = sum(1 for item in memory_items if item.get("state_code") == "superseded")
    archived_memories = sum(1 for item in memory_items if item.get("state_code") == "archived")
    missing_owner_count = sum(1 for item in memory_items if normalize_optional_text(item.get("owner_role")) is None)
    duplicate_review_missing_owner_count = sum(1 for item in duplicate_queue["items"] if normalize_optional_text((item.get("duplicate_review") or {}).get("owner_role")) is None)

    draft_created_at: dict[int, float] = {}
    approval_lead_times: list[float] = []
    for row in event_rows:
        event = row_to_dict(row)
        memory_id = int(event["memory_id"])
        if not any(int(item.get("id") or 0) == memory_id for item in memory_items):
            continue
        event_type = str(event.get("event_type") or "")
        event_ts = safe_event_timestamp(event.get("created_at"))
        if event_ts is None:
            continue
        if event_type == "review.draft_created":
            draft_created_at[memory_id] = event_ts
        elif event_type == "review.approved":
            draft_ts = draft_created_at.get(memory_id)
            if draft_ts is not None and event_ts >= draft_ts:
                approval_lead_times.append(event_ts - draft_ts)

    avg_lead = sum(approval_lead_times) / len(approval_lead_times) if approval_lead_times else 0.0
    max_lead = max(approval_lead_times) if approval_lead_times else 0.0

    conn = get_db_connection()
    try:
        feature_flag = get_feature_flag_config(conn, cross_project_flag_key)
    finally:
        conn.close()
    feature_flag_evaluation = evaluate_feature_flag_config(feature_flag, project_key=normalized_project_key, scope_code=normalized_scope)
    feature_flag_view = compatibility_feature_flag(feature_flag)

    project_scope_conn = get_db_connection()
    try:
        project_scope_mismatch_count = count_project_scope_mismatches(
            project_scope_conn,
            project_key=normalized_project_key,
            scope_code=normalized_scope,
            layer_code=normalized_layer,
            area_code=normalized_area,
            memory_type=normalized_memory_type,
            tag=normalized_tag,
            text_query=normalized_text_query,
        )
    finally:
        project_scope_conn.close()

    return {
        "filters": {
            "validated_before": normalize_optional_text(validated_before),
            "as_of": normalize_optional_text(as_of),
            "scope_code": normalized_scope,
            "project_key": normalized_project_key,
            "layer_code": normalized_layer,
            "area_code": normalized_area,
            "memory_type": normalized_memory_type,
            "tag": normalized_tag,
            "text_query": normalized_text_query,
        },
        "feature_flag": feature_flag_view,
        "feature_flag_evaluation": feature_flag_evaluation,
        "backlogs": {
            "review_queue_count": review_queue["count"],
            "revalidation_queue_count": revalidation_queue["count"],
            "expired_queue_count": expired_queue["count"],
            "duplicate_queue_count": duplicate_queue["count"],
            "overdue_review_count": overdue_review_queue["count"],
            "overdue_revalidation_count": overdue_revalidation_queue["count"],
            "overdue_expired_count": overdue_expired_queue["count"],
            "overdue_duplicate_count": overdue_duplicate_queue["count"],
        },
        "inventory": {
            "total_memories": total_memories,
            "validated_memories": validated_memories,
            "superseded_memories": superseded_memories,
            "archived_memories": archived_memories,
            "missing_owner_count": missing_owner_count,
            "duplicate_review_missing_owner_count": duplicate_review_missing_owner_count,
            "broken_owner_mapping_count": int(owner_catalog_health.get("broken_owner_mapping_count") or 0),
            "inactive_owner_target_count": int(owner_catalog_health.get("inactive_owner_target_count") or 0),
            "owner_catalog_governance_warning_count": int(owner_catalog_health.get("governance_warning_count") or 0),
            "project_scope_mismatch_count": project_scope_mismatch_count,
        },
        "owner_catalog_health": owner_catalog_health,
        "approval_metrics": {
            "approved_from_draft_count": len(approval_lead_times),
            "approval_lead_time_avg_seconds": avg_lead,
            "approval_lead_time_max_seconds": max_lead,
        },
    }
