from __future__ import annotations

"""Owner queue summary and filtering helpers."""

from typing import Any, Callable


def owner_summary_from_items(
    items: list[dict[str, Any]],
    *,
    memory_field: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
) -> dict[str, Any]:
    owner_role_counts: dict[str, int] = {}
    missing_owner_count = 0
    distinct_owner_ids: set[str] = set()

    for item in items:
        memory = item if memory_field is None else item.get(memory_field)
        if not isinstance(memory, dict):
            continue
        owner_role = normalize_optional_text(memory.get("owner_role"))
        owner_id = normalize_optional_text(memory.get("owner_id"))
        if owner_role is None:
            missing_owner_count += 1
        else:
            owner_role_counts[owner_role] = owner_role_counts.get(owner_role, 0) + 1
        if owner_id:
            distinct_owner_ids.add(owner_id)

    return {
        "owner_role_counts": owner_role_counts,
        "missing_owner_count": missing_owner_count,
        "distinct_owner_ids": sorted(distinct_owner_ids),
    }


def effective_owner_summary_from_items(
    items: list[dict[str, Any]],
    *,
    memory_field: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
) -> dict[str, Any]:
    effective_owner_counts: dict[str, int] = {}
    effective_owner_type_counts: dict[str, int] = {}
    unresolved_count = 0

    for item in items:
        memory = item if memory_field is None else item.get(memory_field)
        if not isinstance(memory, dict):
            continue
        effective_owner_key = normalize_optional_text(memory.get("effective_owner_key"))
        effective_owner_type = normalize_optional_text(memory.get("effective_owner_type"))
        if effective_owner_key is None:
            unresolved_count += 1
        else:
            effective_owner_counts[effective_owner_key] = effective_owner_counts.get(effective_owner_key, 0) + 1
        if effective_owner_type:
            effective_owner_type_counts[effective_owner_type] = effective_owner_type_counts.get(effective_owner_type, 0) + 1

    return {
        "effective_owner_counts": effective_owner_counts,
        "effective_owner_type_counts": effective_owner_type_counts,
        "unresolved_count": unresolved_count,
    }


def filter_items_by_effective_owner(
    items: list[dict[str, Any]],
    *,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
    memory_field: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
) -> list[dict[str, Any]]:
    normalized_owner_key = normalize_optional_text(effective_owner_key)
    normalized_owner_type = normalize_optional_text(effective_owner_type)
    if normalized_owner_key is None and normalized_owner_type is None:
        return items
    filtered: list[dict[str, Any]] = []
    for item in items:
        memory = item if memory_field is None else item.get(memory_field)
        if not isinstance(memory, dict):
            continue
        item_owner_key = normalize_optional_text(memory.get("effective_owner_key"))
        item_owner_type = normalize_optional_text(memory.get("effective_owner_type"))
        if normalized_owner_key is not None and item_owner_key != normalized_owner_key:
            continue
        if normalized_owner_type is not None and item_owner_type != normalized_owner_type:
            continue
        filtered.append(item)
    return filtered


def recommended_bulk_actions(
    *,
    owner_summary: dict[str, Any],
    overdue_review_queue: dict[str, Any],
    overdue_revalidation_queue: dict[str, Any],
    overdue_expired_queue: dict[str, Any],
    overdue_duplicate_queue: dict[str, Any],
    normalize_optional_text: Callable[[Any], str | None],
) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []

    review_overdue_ids = [int(item["id"]) for item in overdue_review_queue.get("items", [])]
    review_missing_ids = [int(item["id"]) for item in overdue_review_queue.get("items", []) if normalize_optional_text(item.get("owner_role")) is None]
    snapshot_missing = int((owner_summary.get("snapshot") or {}).get("missing_owner_count") or 0)
    if not review_missing_ids and snapshot_missing > 0 and review_overdue_ids:
        review_missing_ids = review_overdue_ids
    if review_missing_ids:
        recommendations.append({
            "kind": "assign_missing_review_owners",
            "action": "bulk_set_memory_owner",
            "target_queue": "overdue_review",
            "count": len(review_missing_ids),
            "reason": "review_overdue_with_missing_owner",
            "suggested_payload": {"memory_ids": review_missing_ids, "owner_role": "maintainer"},
        })

    revalidation_missing_ids = [int(item["id"]) for item in overdue_revalidation_queue.get("items", []) if normalize_optional_text(item.get("owner_role")) is None]
    if revalidation_missing_ids:
        recommendations.append({
            "kind": "assign_missing_revalidation_owners",
            "action": "bulk_set_memory_owner",
            "target_queue": "overdue_revalidation",
            "count": len(revalidation_missing_ids),
            "reason": "revalidation_overdue_with_missing_owner",
            "suggested_payload": {"memory_ids": revalidation_missing_ids, "owner_role": "knowledge_curator"},
        })

    expired_missing_ids = [int(item["id"]) for item in overdue_expired_queue.get("items", []) if normalize_optional_text(item.get("owner_role")) is None]
    if expired_missing_ids:
        recommendations.append({
            "kind": "assign_missing_expired_owners",
            "action": "bulk_set_memory_owner",
            "target_queue": "overdue_expired",
            "count": len(expired_missing_ids),
            "reason": "expired_overdue_with_missing_owner",
            "suggested_payload": {"memory_ids": expired_missing_ids, "owner_role": "knowledge_curator"},
        })

    duplicate_missing_pairs = [
        {"canonical_memory_id": int(item["canonical_memory_id"]), "duplicate_memory_id": int(item["duplicate_memory_id"])}
        for item in overdue_duplicate_queue.get("items", [])
        if normalize_optional_text((item.get("duplicate_review") or {}).get("owner_role")) is None
    ]
    if duplicate_missing_pairs:
        recommendations.append({
            "kind": "assign_missing_duplicate_owners",
            "action": "bulk_set_duplicate_candidate_sla",
            "target_queue": "overdue_duplicates",
            "count": len(duplicate_missing_pairs),
            "reason": "duplicate_overdue_with_missing_owner",
            "suggested_payload": {"pairs": duplicate_missing_pairs, "owner_role": "maintainer", "status": "open"},
        })

    overdue_review_ids = review_overdue_ids
    if len(overdue_review_ids) >= 2:
        recommendations.append({
            "kind": "rebatch_overdue_review_sla",
            "action": "bulk_set_memory_sla",
            "target_queue": "overdue_review",
            "count": len(overdue_review_ids),
            "reason": "review_overdue_batch_candidate",
            "suggested_payload": {"memory_ids": overdue_review_ids},
        })

    overdue_revalidation_ids = [int(item["id"]) for item in overdue_revalidation_queue.get("items", [])]
    if len(overdue_revalidation_ids) >= 2:
        recommendations.append({
            "kind": "rebatch_overdue_revalidation_sla",
            "action": "bulk_set_memory_sla",
            "target_queue": "overdue_revalidation",
            "count": len(overdue_revalidation_ids),
            "reason": "revalidation_overdue_batch_candidate",
            "suggested_payload": {"memory_ids": overdue_revalidation_ids},
        })

    overdue_expired_ids = [int(item["id"]) for item in overdue_expired_queue.get("items", [])]
    if len(overdue_expired_ids) >= 2:
        recommendations.append({
            "kind": "rebatch_overdue_expired_sla",
            "action": "bulk_set_memory_sla",
            "target_queue": "overdue_expired",
            "count": len(overdue_expired_ids),
            "reason": "expired_overdue_batch_candidate",
            "suggested_payload": {"memory_ids": overdue_expired_ids},
        })

    overdue_duplicate_pairs = [
        {"canonical_memory_id": int(item["canonical_memory_id"]), "duplicate_memory_id": int(item["duplicate_memory_id"])}
        for item in overdue_duplicate_queue.get("items", [])
    ]
    if len(overdue_duplicate_pairs) >= 2:
        recommendations.append({
            "kind": "rebatch_overdue_duplicate_sla",
            "action": "bulk_set_duplicate_candidate_sla",
            "target_queue": "overdue_duplicates",
            "count": len(overdue_duplicate_pairs),
            "reason": "duplicate_overdue_batch_candidate",
            "suggested_payload": {"pairs": overdue_duplicate_pairs, "status": "open"},
        })

    if snapshot_missing >= 3:
        recommendations.append({
            "kind": "global_owner_cleanup",
            "action": "bulk_set_memory_owner",
            "target_queue": "snapshot",
            "count": snapshot_missing,
            "reason": "snapshot_missing_owner_pressure",
            "suggested_payload": {"owner_role": "review_team"},
        })

    return recommendations


def rebalance_candidate_items(items: list[dict[str, Any]], *, memory_field: str | None = None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in items:
        memory = item if memory_field is None else item.get(memory_field)
        if not isinstance(memory, dict):
            continue
        normalized.append({
            "memory_id": int(item.get("id") or 0) if memory_field is None else None,
            "canonical_memory_id": item.get("canonical_memory_id"),
            "duplicate_memory_id": item.get("duplicate_memory_id"),
            "summary_short": item.get("summary_short") if memory_field is None else None,
            "memory_type": item.get("memory_type") if memory_field is None else None,
            "owner_role": memory.get("owner_role"),
            "effective_owner_key": memory.get("effective_owner_key"),
            "effective_owner_type": memory.get("effective_owner_type"),
            "effective_display_name": memory.get("effective_display_name"),
            "review_due_at": item.get("review_due_at") if memory_field is None else None,
            "revalidation_due_at": item.get("revalidation_due_at") if memory_field is None else None,
            "expired_due_at": item.get("expired_due_at") if memory_field is None else None,
            "duplicate_due_at": memory.get("duplicate_due_at") if memory_field is not None else None,
        })
    return normalized


def accumulate_effective_owner_workload(
    buckets: dict[str, dict[str, Any]],
    items: list[dict[str, Any]],
    *,
    bucket_name: str,
    memory_field: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
) -> None:
    for item in items:
        memory = item if memory_field is None else item.get(memory_field)
        if not isinstance(memory, dict):
            continue
        effective_owner_key = normalize_optional_text(memory.get("effective_owner_key")) or "__unresolved__"
        bucket = buckets.setdefault(
            effective_owner_key,
            {
                "effective_owner_key": None if effective_owner_key == "__unresolved__" else effective_owner_key,
                "effective_owner_type": normalize_optional_text(memory.get("effective_owner_type")),
                "effective_display_name": normalize_optional_text(memory.get("effective_display_name")),
                "owner_resolution_reason": normalize_optional_text(memory.get("owner_resolution_reason")),
                "counts": {
                    "review": 0,
                    "revalidation": 0,
                    "expired": 0,
                    "duplicates": 0,
                    "overdue_review": 0,
                    "overdue_revalidation": 0,
                    "overdue_expired": 0,
                    "overdue_duplicates": 0,
                },
                "total_count": 0,
                "overdue_total": 0,
            },
        )
        if bucket.get("effective_owner_type") is None:
            bucket["effective_owner_type"] = normalize_optional_text(memory.get("effective_owner_type"))
        if bucket.get("effective_display_name") is None:
            bucket["effective_display_name"] = normalize_optional_text(memory.get("effective_display_name"))
        if bucket.get("owner_resolution_reason") is None:
            bucket["owner_resolution_reason"] = normalize_optional_text(memory.get("owner_resolution_reason"))
        bucket["counts"][bucket_name] += 1
        bucket["total_count"] += 1
        if bucket_name.startswith("overdue_"):
            bucket["overdue_total"] += 1


def effective_owner_workload_payload(
    *,
    limit: int,
    validated_before: str | None,
    as_of: str | None,
    scope_code: str | None,
    project_key: str | None,
    layer_code: str | None,
    area_code: str | None,
    memory_type: str | None,
    tag: str | None,
    text_query: str | None,
    effective_owner_key: str | None,
    effective_owner_type: str | None,
    review_queue: dict[str, Any],
    revalidation_queue: dict[str, Any],
    expired_queue: dict[str, Any],
    duplicate_queue: dict[str, Any],
    overdue_review_queue: dict[str, Any],
    overdue_revalidation_queue: dict[str, Any],
    overdue_expired_queue: dict[str, Any],
    overdue_duplicate_queue: dict[str, Any],
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    normalize_layer_code: Callable[[Any], str | None],
    normalize_area_code: Callable[[Any], str | None],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}

    buckets: dict[str, dict[str, Any]] = {}
    accumulate_effective_owner_workload(buckets, review_queue["items"], bucket_name="review", normalize_optional_text=normalize_optional_text)
    accumulate_effective_owner_workload(buckets, revalidation_queue["items"], bucket_name="revalidation", normalize_optional_text=normalize_optional_text)
    accumulate_effective_owner_workload(buckets, expired_queue["items"], bucket_name="expired", normalize_optional_text=normalize_optional_text)
    accumulate_effective_owner_workload(buckets, duplicate_queue["items"], bucket_name="duplicates", memory_field="duplicate_review", normalize_optional_text=normalize_optional_text)
    accumulate_effective_owner_workload(buckets, overdue_review_queue["items"], bucket_name="overdue_review", normalize_optional_text=normalize_optional_text)
    accumulate_effective_owner_workload(buckets, overdue_revalidation_queue["items"], bucket_name="overdue_revalidation", normalize_optional_text=normalize_optional_text)
    accumulate_effective_owner_workload(buckets, overdue_expired_queue["items"], bucket_name="overdue_expired", normalize_optional_text=normalize_optional_text)
    accumulate_effective_owner_workload(buckets, overdue_duplicate_queue["items"], bucket_name="overdue_duplicates", memory_field="duplicate_review", normalize_optional_text=normalize_optional_text)

    items = sorted(
        buckets.values(),
        key=lambda item: (-int(item.get("total_count") or 0), -int(item.get("overdue_total") or 0), str(item.get("effective_owner_key") or "")),
    )

    return {
        "count": len(items),
        "items": items[: int(limit)],
        "filters": {
            "limit": int(limit),
            "validated_before": normalize_optional_text(validated_before),
            "as_of": normalize_optional_text(as_of),
            "scope_code": normalize_scope_code(scope_code),
            "project_key": normalize_optional_text(project_key),
            "layer_code": normalize_layer_code(layer_code),
            "area_code": normalize_area_code(area_code),
            "memory_type": normalize_optional_text(memory_type),
            "tag": normalize_optional_text(tag),
            "text_query": normalize_optional_text(text_query),
            "effective_owner_key": normalize_optional_text(effective_owner_key),
            "effective_owner_type": normalize_optional_text(effective_owner_type),
        },
        "summary": {
            "review_queue_count": review_queue["count"],
            "revalidation_queue_count": revalidation_queue["count"],
            "expired_queue_count": expired_queue["count"],
            "duplicate_queue_count": duplicate_queue["count"],
            "overdue_review_count": overdue_review_queue["count"],
            "overdue_revalidation_count": overdue_revalidation_queue["count"],
            "overdue_expired_count": overdue_expired_queue["count"],
            "overdue_duplicate_count": overdue_duplicate_queue["count"],
        },
    }


def operational_queue_dashboard_payload(
    conn: Any,
    *,
    limit_per_queue: int,
    validated_before: str | None,
    as_of: str | None,
    scope_code: str | None,
    project_key: str | None,
    layer_code: str | None,
    area_code: str | None,
    memory_type: str | None,
    tag: str | None,
    text_query: str | None,
    effective_owner_key: str | None,
    effective_owner_type: str | None,
    review_queue: dict[str, Any],
    revalidation_queue: dict[str, Any],
    expired_queue: dict[str, Any],
    duplicate_queue: dict[str, Any],
    overdue_review_queue: dict[str, Any],
    overdue_revalidation_queue: dict[str, Any],
    overdue_expired_queue: dict[str, Any],
    overdue_duplicate_queue: dict[str, Any],
    owner_catalog_repair_summary: dict[str, Any],
    feature_flag_key: str,
    get_feature_flag_config: Callable[[Any, str], dict[str, Any]],
    evaluate_feature_flag_config: Callable[..., dict[str, Any]],
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    normalize_layer_code: Callable[[Any], str | None],
    normalize_area_code: Callable[[Any], str | None],
) -> dict[str, Any]:
    if limit_per_queue < 1 or limit_per_queue > 1000:
        return {"status": "error", "error": "limit_per_queue musi byÄ‡ w zakresie 1..1000"}

    normalized_scope = normalize_scope_code(scope_code)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_layer = normalize_layer_code(layer_code)
    normalized_area = normalize_area_code(area_code)
    normalized_memory_type = normalize_optional_text(memory_type)
    normalized_tag = normalize_optional_text(tag)
    normalized_text_query = normalize_optional_text(text_query)
    normalized_effective_owner_key = normalize_optional_text(effective_owner_key)
    normalized_effective_owner_type = normalize_optional_text(effective_owner_type)

    feature_flag = get_feature_flag_config(conn, feature_flag_key)

    sql = "SELECT owner_role, owner_id FROM memories WHERE 1 = 1"
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
    owner_rows = conn.execute(sql, params).fetchall()

    feature_flag_evaluation = evaluate_feature_flag_config(feature_flag, project_key=normalized_project_key, scope_code=normalized_scope)
    feature_flag_view = dict(feature_flag)
    feature_flag_view["key"] = feature_flag_view.get("flag_key")
    feature_flag_view["enabled"] = bool(int(feature_flag_view.get("is_enabled") or 0))
    feature_flag_view["rollout_scope"] = feature_flag_view.get("allowed_scope_codes")
    feature_flag_view["rollout_project_key"] = feature_flag_view.get("allowed_project_keys")
    rollout_mode_aliases = {"all": "global", "projects": "project", "scopes": "scope", "projects_and_scopes": "scoped_project", "off": "off"}
    feature_flag_view["rollout_mode"] = rollout_mode_aliases.get(str(feature_flag_view.get("rollout_mode") or "off"), feature_flag_view.get("rollout_mode"))

    owner_snapshot_items = [{"owner_role": row["owner_role"], "owner_id": row["owner_id"]} for row in owner_rows]
    owner_snapshot = owner_summary_from_items(owner_snapshot_items, normalize_optional_text=normalize_optional_text)
    owner_summary = {
        "review_queue": owner_summary_from_items(review_queue["items"], normalize_optional_text=normalize_optional_text),
        "revalidation_queue": owner_summary_from_items(revalidation_queue["items"], normalize_optional_text=normalize_optional_text),
        "expired_queue": owner_summary_from_items(expired_queue["items"], normalize_optional_text=normalize_optional_text),
        "overdue_review_queue": owner_summary_from_items(overdue_review_queue["items"], normalize_optional_text=normalize_optional_text),
        "overdue_revalidation_queue": owner_summary_from_items(overdue_revalidation_queue["items"], normalize_optional_text=normalize_optional_text),
        "overdue_expired_queue": owner_summary_from_items(overdue_expired_queue["items"], normalize_optional_text=normalize_optional_text),
        "overdue_duplicate_queue": owner_summary_from_items(overdue_duplicate_queue["items"], memory_field="duplicate_review", normalize_optional_text=normalize_optional_text),
        "duplicate_queue": owner_summary_from_items(duplicate_queue["items"], memory_field="duplicate_review", normalize_optional_text=normalize_optional_text),
        "snapshot": owner_snapshot,
    }
    effective_owner_summary = {
        "review_queue": effective_owner_summary_from_items(review_queue["items"], normalize_optional_text=normalize_optional_text),
        "revalidation_queue": effective_owner_summary_from_items(revalidation_queue["items"], normalize_optional_text=normalize_optional_text),
        "expired_queue": effective_owner_summary_from_items(expired_queue["items"], normalize_optional_text=normalize_optional_text),
        "overdue_review_queue": effective_owner_summary_from_items(overdue_review_queue["items"], normalize_optional_text=normalize_optional_text),
        "overdue_revalidation_queue": effective_owner_summary_from_items(overdue_revalidation_queue["items"], normalize_optional_text=normalize_optional_text),
        "overdue_expired_queue": effective_owner_summary_from_items(overdue_expired_queue["items"], normalize_optional_text=normalize_optional_text),
        "overdue_duplicate_queue": effective_owner_summary_from_items(overdue_duplicate_queue["items"], memory_field="duplicate_review", normalize_optional_text=normalize_optional_text),
        "duplicate_queue": effective_owner_summary_from_items(duplicate_queue["items"], memory_field="duplicate_review", normalize_optional_text=normalize_optional_text),
    }
    bulk_actions = recommended_bulk_actions(
        owner_summary=owner_summary,
        overdue_review_queue=overdue_review_queue,
        overdue_revalidation_queue=overdue_revalidation_queue,
        overdue_expired_queue=overdue_expired_queue,
        overdue_duplicate_queue=overdue_duplicate_queue,
        normalize_optional_text=normalize_optional_text,
    )

    return {
        "filters": {
            "limit_per_queue": int(limit_per_queue),
            "validated_before": normalize_optional_text(validated_before),
            "as_of": normalize_optional_text(as_of),
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
        "feature_flag": feature_flag_view,
        "feature_flag_evaluation": feature_flag_evaluation,
        "owner_summary": owner_summary,
        "effective_owner_summary": effective_owner_summary,
        "recommended_bulk_actions": bulk_actions,
        "owner_catalog_repair_summary": owner_catalog_repair_summary,
        "summary": {
            "review_queue_count": review_queue["count"],
            "revalidation_queue_count": revalidation_queue["count"],
            "expired_queue_count": expired_queue["count"],
            "duplicate_queue_count": duplicate_queue["count"],
            "overdue_review_queue_count": overdue_review_queue["count"],
            "overdue_revalidation_queue_count": overdue_revalidation_queue["count"],
            "overdue_expired_queue_count": overdue_expired_queue["count"],
            "overdue_duplicate_queue_count": overdue_duplicate_queue["count"],
            "missing_owner_count": owner_snapshot["missing_owner_count"],
            "owner_catalog_problem_count": int((owner_catalog_repair_summary.get("health") or {}).get("summary", {}).get("problem_count") or 0),
            "owner_catalog_batch_candidate_count": int((owner_catalog_repair_summary.get("batch_candidates_summary") or {}).get("count") or 0),
            "owner_catalog_bulk_repair_count": int((owner_catalog_repair_summary.get("repair_audit_summary") or {}).get("bulk_repair_count") or 0),
            "owner_catalog_governance_warning_count": int((owner_catalog_repair_summary.get("health") or {}).get("summary", {}).get("governance_warning_count") or 0),
            "owner_catalog_governance_event_count": int((owner_catalog_repair_summary.get("governance_history_summary") or {}).get("governance_event_count") or 0),
        },
        "queues": {
            "review": review_queue,
            "revalidation": revalidation_queue,
            "expired": expired_queue,
            "duplicates": duplicate_queue,
            "overdue_review": overdue_review_queue,
            "overdue_revalidation": overdue_revalidation_queue,
            "overdue_expired": overdue_expired_queue,
            "overdue_duplicates": overdue_duplicate_queue,
        },
    }


def owner_rebalance_candidates_payload(
    *,
    limit: int,
    candidate_limit_per_queue: int,
    overloaded_owner_key: str | None,
    validated_before: str | None,
    as_of: str | None,
    scope_code: str | None,
    project_key: str | None,
    layer_code: str | None,
    area_code: str | None,
    memory_type: str | None,
    tag: str | None,
    text_query: str | None,
    get_effective_owner_workload: Callable[..., dict[str, Any]],
    list_overdue_review_queue: Callable[..., dict[str, Any]],
    list_overdue_revalidation_queue: Callable[..., dict[str, Any]],
    list_overdue_expired_queue: Callable[..., dict[str, Any]],
    list_overdue_duplicate_queue: Callable[..., dict[str, Any]],
    list_owner_role_mappings: Callable[..., dict[str, Any]],
    list_owner_directory_items: Callable[..., dict[str, Any]],
    normalize_optional_text: Callable[[Any], str | None],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}
    if candidate_limit_per_queue < 1 or candidate_limit_per_queue > 1000:
        return {"status": "error", "error": "candidate_limit_per_queue musi byÄ‡ w zakresie 1..1000"}

    owner_workload = get_effective_owner_workload(
        limit=200,
        validated_before=validated_before,
        as_of=as_of,
        scope_code=scope_code,
        project_key=project_key,
        layer_code=layer_code,
        area_code=area_code,
        memory_type=memory_type,
        tag=tag,
        text_query=text_query,
    )
    workload_items = owner_workload.get("items") or []
    normalized_overloaded_owner_key = normalize_optional_text(overloaded_owner_key)
    if normalized_overloaded_owner_key is not None:
        source_owner = next((item for item in workload_items if item.get("effective_owner_key") == normalized_overloaded_owner_key), None)
    else:
        ranked_sources = sorted(
            [item for item in workload_items if normalize_optional_text(item.get("effective_owner_key")) is not None],
            key=lambda item: (-int(item.get("overdue_total") or 0), -int(item.get("total_count") or 0), str(item.get("effective_owner_key") or "")),
        )
        source_owner = ranked_sources[0] if ranked_sources else None

    if source_owner is None:
        return {
            "status": "no_source_owner",
            "count": 0,
            "source_owner": None,
            "target_candidates": [],
            "candidate_groups": {},
            "recommended_actions": [],
            "filters": {
                "limit": int(limit),
                "candidate_limit_per_queue": int(candidate_limit_per_queue),
                "overloaded_owner_key": normalized_overloaded_owner_key,
            },
        }

    source_owner_key = normalize_optional_text(source_owner.get("effective_owner_key"))
    source_owner_type = normalize_optional_text(source_owner.get("effective_owner_type"))

    review_items = list_overdue_review_queue(limit=1000, as_of=as_of, scope_code=scope_code, project_key=project_key, effective_owner_key=source_owner_key, effective_owner_type=source_owner_type)["items"]
    revalidation_items = list_overdue_revalidation_queue(limit=1000, as_of=as_of, scope_code=scope_code, project_key=project_key, effective_owner_key=source_owner_key, effective_owner_type=source_owner_type)["items"]
    expired_items = list_overdue_expired_queue(limit=1000, as_of=as_of, scope_code=scope_code, project_key=project_key, effective_owner_key=source_owner_key, effective_owner_type=source_owner_type)["items"]
    duplicate_items = list_overdue_duplicate_queue(limit=1000, as_of=as_of, scope_code=scope_code, project_key=project_key, effective_owner_key=source_owner_key, effective_owner_type=source_owner_type)["items"]

    mappings = list_owner_role_mappings(project_key=project_key, scope_code=scope_code, active_only=True)["items"]
    roles_by_owner_key: dict[str, list[str]] = {}
    for mapping in mappings:
        owner_key = normalize_optional_text(mapping.get("owner_key"))
        owner_role = normalize_optional_text(mapping.get("owner_role"))
        if owner_key is None or owner_role is None:
            continue
        roles = roles_by_owner_key.setdefault(owner_key, [])
        if owner_role not in roles:
            roles.append(owner_role)

    active_targets = list_owner_directory_items(owner_type=source_owner_type, active_only=True)["items"] if source_owner_type else []
    workload_by_owner_key = {item.get("effective_owner_key"): item for item in workload_items if normalize_optional_text(item.get("effective_owner_key")) is not None}
    target_candidates: list[dict[str, Any]] = []
    for target in active_targets:
        owner_key = normalize_optional_text(target.get("owner_key"))
        if owner_key is None or owner_key == source_owner_key:
            continue
        target_workload = workload_by_owner_key.get(owner_key, {})
        target_roles = roles_by_owner_key.get(owner_key, [])
        target_candidates.append({
            "effective_owner_key": owner_key,
            "effective_owner_type": normalize_optional_text(target.get("owner_type")),
            "effective_display_name": normalize_optional_text(target.get("display_name")),
            "total_count": int(target_workload.get("total_count") or 0),
            "overdue_total": int(target_workload.get("overdue_total") or 0),
            "available_owner_roles": target_roles,
            "recommended_owner_role": target_roles[0] if target_roles else None,
        })
    target_candidates.sort(
        key=lambda item: (
            0 if item.get("recommended_owner_role") else 1,
            int(item.get("overdue_total") or 0),
            int(item.get("total_count") or 0),
            str(item.get("effective_owner_key") or ""),
        )
    )
    target_candidates = target_candidates[: int(limit)]

    candidate_groups = {
        "overdue_review": {
            "count": len(review_items),
            "items": rebalance_candidate_items(review_items)[: int(candidate_limit_per_queue)],
            "memory_ids": [int(item.get("id") or 0) for item in review_items[: int(candidate_limit_per_queue)]],
        },
        "overdue_revalidation": {
            "count": len(revalidation_items),
            "items": rebalance_candidate_items(revalidation_items)[: int(candidate_limit_per_queue)],
            "memory_ids": [int(item.get("id") or 0) for item in revalidation_items[: int(candidate_limit_per_queue)]],
        },
        "overdue_expired": {
            "count": len(expired_items),
            "items": rebalance_candidate_items(expired_items)[: int(candidate_limit_per_queue)],
            "memory_ids": [int(item.get("id") or 0) for item in expired_items[: int(candidate_limit_per_queue)]],
        },
        "overdue_duplicates": {
            "count": len(duplicate_items),
            "items": rebalance_candidate_items(duplicate_items, memory_field="duplicate_review")[: int(candidate_limit_per_queue)],
            "pairs": [
                {"canonical_memory_id": int(item.get("canonical_memory_id") or 0), "duplicate_memory_id": int(item.get("duplicate_memory_id") or 0)}
                for item in duplicate_items[: int(candidate_limit_per_queue)]
            ],
        },
    }

    primary_target = next((item for item in target_candidates if item.get("recommended_owner_role")), None)
    recommended_actions: list[dict[str, Any]] = []
    if primary_target is not None and primary_target.get("recommended_owner_role"):
        for queue_name in ["overdue_review", "overdue_revalidation", "overdue_expired"]:
            memory_ids = candidate_groups[queue_name].get("memory_ids") or []
            if memory_ids:
                recommended_actions.append({
                    "kind": f"rebalance_{queue_name}",
                    "action": "bulk_set_memory_owner",
                    "target_owner": primary_target,
                    "payload": {"memory_ids": memory_ids, "owner_role": primary_target.get("recommended_owner_role")},
                })
        duplicate_pairs = candidate_groups["overdue_duplicates"].get("pairs") or []
        if duplicate_pairs:
            recommended_actions.append({
                "kind": "rebalance_overdue_duplicates",
                "action": "bulk_set_duplicate_candidate_sla",
                "target_owner": primary_target,
                "payload": {"pairs": duplicate_pairs, "owner_role": primary_target.get("recommended_owner_role"), "status": "open"},
            })

    return {
        "status": "ok",
        "count": len(target_candidates),
        "source_owner": source_owner,
        "target_candidates": target_candidates,
        "candidate_groups": candidate_groups,
        "recommended_actions": recommended_actions,
        "filters": {
            "limit": int(limit),
            "candidate_limit_per_queue": int(candidate_limit_per_queue),
            "overloaded_owner_key": source_owner_key,
        },
    }
