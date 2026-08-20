from __future__ import annotations

"""Queue and quality escalation helpers."""

from typing import Any, Callable

from app import timeline as _timeline
from mapi_core.core.time_score import (
    compute_days_overdue as _compute_days_overdue,
    utc_now_iso as _utc_now_iso,
)
from mapi_core.schemas import normalize_optional_text as _normalize_optional_text, normalize_scope_code as _normalize_scope_code


def escalation_history_payload(
    conn: Any,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    escalation_level: int | None = None,
    project_key: str | None = None,
    include_resolved: bool = False,
    limit: int = 50,
    offset: int = 0,
    normalize_optional_text,
) -> dict[str, Any]:
    normalized_entity_type = normalize_optional_text(entity_type)
    normalized_entity_id = int(entity_id) if entity_id is not None else None
    normalized_escalation_level = int(escalation_level) if escalation_level is not None else None
    normalized_project_key = normalize_optional_text(project_key)
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": 'limit musi byÄ‡ w zakresie 1..1000'}

    sql = "SELECT * FROM escalation_history WHERE 1=1"
    params: list[Any] = []
    if not include_resolved:
        sql += " AND resolved_at IS NULL"
    if normalized_entity_type is not None:
        sql += " AND entity_type = ?"
        params.append(normalized_entity_type)
    if normalized_entity_id is not None:
        sql += " AND entity_id = ?"
        params.append(normalized_entity_id)
    if normalized_escalation_level is not None:
        sql += " AND escalation_level = ?"
        params.append(normalized_escalation_level)
    if normalized_project_key is not None:
        sql += " AND project_key = ?"
        params.append(normalized_project_key)
    sql += " ORDER BY escalated_at DESC, id DESC"

    count_sql = sql.replace("SELECT *", "SELECT COUNT(*)", 1)
    paged_sql = sql + " LIMIT ? OFFSET ?"
    total_count = conn.execute(count_sql, params).fetchone()[0]
    rows = conn.execute(paged_sql, [*params, int(limit), int(offset)]).fetchall()
    items = [dict(r) for r in rows]

    return {
        "status": "ok",
        "count": len(items),
        "total_count": total_count,
        "items": items,
        "filters": {
            "entity_type": normalized_entity_type,
            "entity_id": normalized_entity_id,
            "escalation_level": normalized_escalation_level,
            "project_key": normalized_project_key,
            "include_resolved": include_resolved,
            "limit": int(limit),
            "offset": int(offset),
        },
    }


def escalation_dashboard_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
    normalize_optional_text,
    normalize_scope_code,
) -> dict[str, Any]:
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)

    sql_base = "FROM escalation_history WHERE 1=1"
    params_base: list[Any] = []
    if normalized_project_key is not None:
        sql_base += " AND project_key = ?"
        params_base.append(normalized_project_key)
    if normalized_scope_code is not None:
        sql_base += " AND scope_code = ?"
        params_base.append(normalized_scope_code)

    pending_by_level: dict[int, int] = {}
    for lvl in (1, 2, 3):
        cnt = conn.execute(
            f"SELECT COUNT(*) {sql_base} AND resolved_at IS NULL AND escalation_level = ?",
            [*params_base, lvl],
        ).fetchone()[0]
        pending_by_level[lvl] = int(cnt)

    reason_rows = conn.execute(
        f"SELECT reason, COUNT(*) as cnt {sql_base} AND resolved_at IS NULL GROUP BY reason ORDER BY cnt DESC LIMIT 5",
        params_base,
    ).fetchall()
    most_escalated_reasons = [{"reason": r["reason"], "count": int(r["cnt"])} for r in reason_rows]

    avg_row = conn.execute(
        f"SELECT AVG(julianday(resolved_at) - julianday(escalated_at)) as avg_days {sql_base} AND resolved_at IS NOT NULL",
        params_base,
    ).fetchone()
    avg_days_to_resolve = round(float(avg_row["avg_days"]), 1) if avg_row and avg_row["avg_days"] is not None else None

    recent_rows = conn.execute(
        f"SELECT * {sql_base} AND resolved_at IS NULL ORDER BY escalation_level DESC, escalated_at ASC LIMIT 20",
        params_base,
    ).fetchall()
    recent_pending = [dict(r) for r in recent_rows]
    total_pending = sum(pending_by_level.values())

    return {
        "status": "attention" if pending_by_level.get(3, 0) > 0 else ("ok" if total_pending == 0 else "warning"),
        "summary": {
            "total_pending": total_pending,
            "pending_level3": pending_by_level.get(3, 0),
        },
        "pending_by_level": pending_by_level,
        "most_escalated_reasons": most_escalated_reasons,
        "avg_days_to_resolve": avg_days_to_resolve,
        "recent_pending": recent_pending,
        "filters": {
            "project_key": normalized_project_key,
            "scope_code": normalized_scope_code,
        },
    }


def escalation_stage(*, value: int, level1_threshold: int, level2_threshold: int, level3_threshold: int) -> dict[str, Any]:
    numeric_value = int(value)
    lvl1 = int(level1_threshold)
    lvl2 = max(int(level2_threshold), lvl1)
    lvl3 = max(int(level3_threshold), lvl2)
    if numeric_value > lvl3:
        return {"level": 3, "stage": "level_3", "severity": "critical"}
    if numeric_value > lvl2:
        return {"level": 2, "stage": "level_2", "severity": "high"}
    if numeric_value > lvl1:
        return {"level": 1, "stage": "level_1", "severity": "warning"}
    return {"level": 0, "stage": "none", "severity": "ok"}


def highest_escalation_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"level": 0, "stage": "none"}
    highest = max(items, key=lambda item: int(item.get("level") or 0))
    return {"level": int(highest.get("level") or 0), "stage": highest.get("stage") or "none"}


# ---------------------------------------------------------------------------
# MCP tool payloads
# ---------------------------------------------------------------------------

def run_escalation_check_payload(
    conn: Any,
    *,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    level2_threshold_days: int = 3,
    level3_threshold_days: int = 7,
    dry_run: bool = False,
    list_overdue_review_queue: Callable[..., dict[str, Any]],
    list_overdue_revalidation_queue: Callable[..., dict[str, Any]],
    list_overdue_expired_queue: Callable[..., dict[str, Any]],
    list_overdue_duplicate_queue: Callable[..., dict[str, Any]],
    owner_catalog_audit_project_key: Callable[[str | None], str],
) -> dict[str, Any]:
    normalized_as_of = _normalize_optional_text(as_of) or _utc_now_iso()
    normalized_scope = _normalize_scope_code(scope_code)
    normalized_project_key = _normalize_optional_text(project_key)
    if level2_threshold_days < 1:
        return {"status": "error", "error": "level2_threshold_days musi byc >= 1"}
    if level3_threshold_days <= level2_threshold_days:
        return {"status": "error", "error": "level3_threshold_days musi byc > level2_threshold_days"}

    overdue_review = list_overdue_review_queue(limit=1000, as_of=normalized_as_of, scope_code=normalized_scope, project_key=normalized_project_key)
    overdue_revalidation = list_overdue_revalidation_queue(limit=1000, as_of=normalized_as_of, scope_code=normalized_scope, project_key=normalized_project_key)
    overdue_expired = list_overdue_expired_queue(limit=1000, as_of=normalized_as_of, scope_code=normalized_scope, project_key=normalized_project_key)
    overdue_duplicate = list_overdue_duplicate_queue(limit=1000, as_of=normalized_as_of, scope_code=normalized_scope, project_key=normalized_project_key)

    queue_configs = [
        (overdue_review["items"], "memory", "review_due_at", "review_overdue"),
        (overdue_revalidation["items"], "memory", "revalidation_due_at", "revalidation_overdue"),
        (overdue_expired["items"], "memory", "expired_due_at", "expired_overdue"),
        (overdue_duplicate["items"], "duplicate_review_item", "duplicate_due_at", "duplicate_overdue"),
    ]

    escalations: list[dict[str, Any]] = []
    level1_count = level2_count = level3_count = 0
    now_iso = _utc_now_iso()

    for items, entity_type, due_field, base_reason in queue_configs:
        for item in items:
            entity_id = int(item.get("id", 0))
            due_at = _normalize_optional_text(item.get(due_field))
            if due_at is None:
                continue
            days_overdue = _compute_days_overdue(due_at, normalized_as_of)
            owner_role = _normalize_optional_text(item.get("owner_role"))
            priority = _normalize_optional_text(item.get("priority")) or "normal"
            item_project_key = _normalize_optional_text(item.get("project_key"))
            item_scope_code = _normalize_optional_text(item.get("scope_code"))

            reason = base_reason
            if owner_role is None:
                level = max(2, 3 if days_overdue >= level3_threshold_days else 2)
                reason = "owner_missing"
            elif days_overdue >= level3_threshold_days:
                level = 3
            elif days_overdue >= level2_threshold_days:
                level = 2
            else:
                level = 1

            entry = {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "escalation_level": level,
                "owner_role": owner_role,
                "project_key": item_project_key,
                "scope_code": item_scope_code,
                "reason": reason,
                "days_overdue": days_overdue,
                "priority": priority,
                "escalated_at": now_iso,
            }
            escalations.append(entry)

            if level == 1:
                level1_count += 1
            elif level == 2:
                level2_count += 1
            else:
                level3_count += 1

            if not dry_run:
                conn.execute(
                    """
                    INSERT INTO escalation_history
                        (escalation_level, entity_type, entity_id, owner_role, project_key,
                         scope_code, reason, days_overdue, priority, escalated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(entity_type, entity_id, escalation_level, reason)
                    DO UPDATE SET
                        days_overdue = excluded.days_overdue,
                        priority = excluded.priority,
                        escalated_at = excluded.escalated_at,
                        owner_role = excluded.owner_role
                    """,
                    (
                        level, entity_type, entity_id, owner_role, item_project_key,
                        item_scope_code, reason, days_overdue, priority, now_iso,
                    ),
                )
                _timeline.record_project_event(
                    conn,
                    project_key=owner_catalog_audit_project_key(item_project_key),
                    event_type="project.note_recorded",
                    title=f"Escalation level {level}: {entity_type} {entity_id}",
                    description=(
                        f"entity_type={entity_type}; entity_id={entity_id}; "
                        f"escalation_level={level}; reason={reason}; "
                        f"days_overdue={days_overdue}; priority={priority}"
                    ),
                    origin="system",
                    tags=["escalation", f"escalation.level_{level}"],
                    status="completed",
                    canonical=True,
                    category="escalation",
                    now_fn=_utc_now_iso,
                )

    if not dry_run:
        conn.commit()

    return {
        "status": "ok",
        "summary": {
            "level1_count": level1_count,
            "level2_count": level2_count,
            "level3_count": level3_count,
            "total": len(escalations),
            "dry_run": dry_run,
        },
        "escalations": escalations,
        "filters": {
            "as_of": normalized_as_of,
            "scope_code": normalized_scope,
            "project_key": normalized_project_key,
            "level2_threshold_days": level2_threshold_days,
            "level3_threshold_days": level3_threshold_days,
        },
    }


def apply_escalation_reactions_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
    min_level: int = 2,
    owner_overload_threshold: int = 3,
    dry_run: bool = True,
    insert_memory_event: Callable[..., Any],
    owner_catalog_audit_project_key: Callable[[str | None], str],
) -> dict[str, Any]:
    _PRIORITY_ORDER = {"low": 0, "normal": 1, "high": 2, "critical": 3}
    _BOOST_MAP = {2: "high", 3: "critical"}

    if min_level not in (1, 2, 3):
        return {"status": "error", "error": "min_level musi byc 1, 2 lub 3"}

    normalized_project_key = _normalize_optional_text(project_key)
    normalized_scope_code = _normalize_scope_code(scope_code)

    sql = (
        "SELECT * FROM escalation_history "
        "WHERE resolved_at IS NULL AND escalation_level >= ? AND entity_type = 'memory'"
    )
    params: list[Any] = [min_level]
    if normalized_project_key is not None:
        sql += " AND project_key = ?"
        params.append(normalized_project_key)
    if normalized_scope_code is not None:
        sql += " AND scope_code = ?"
        params.append(normalized_scope_code)
    sql += " ORDER BY escalation_level DESC, days_overdue DESC"
    escalation_rows = conn.execute(sql, params).fetchall()

    overload_sql = (
        "SELECT owner_role, COUNT(*) as cnt FROM escalation_history "
        "WHERE resolved_at IS NULL AND escalation_level = 3 AND owner_role IS NOT NULL"
    )
    overload_params: list[Any] = []
    if normalized_project_key is not None:
        overload_sql += " AND project_key = ?"
        overload_params.append(normalized_project_key)
    overload_sql += " GROUP BY owner_role"
    overload_rows = conn.execute(overload_sql, overload_params).fetchall()
    overloaded_owners = {
        r["owner_role"]: int(r["cnt"])
        for r in overload_rows
        if int(r["cnt"]) >= owner_overload_threshold
    }

    planned_actions: list[dict[str, Any]] = []
    now_iso = _utc_now_iso()

    for esc in escalation_rows:
        e = dict(esc)
        entity_id = int(e["entity_id"])
        level = int(e["escalation_level"])
        target_priority = _BOOST_MAP.get(level)
        if target_priority is None:
            continue

        mem_row = conn.execute(
            "SELECT id, priority, state_code FROM memories WHERE id = ? AND activity_state = 'active'",
            (entity_id,),
        ).fetchone()
        if mem_row is None:
            continue

        current_priority = str(mem_row["priority"] or "normal")
        current_order = _PRIORITY_ORDER.get(current_priority, 1)
        target_order = _PRIORITY_ORDER.get(target_priority, 2)

        if target_order > current_order:
            action = {
                "action": "boost_priority",
                "entity_type": "memory",
                "entity_id": entity_id,
                "current_priority": current_priority,
                "target_priority": target_priority,
                "escalation_level": level,
                "reason": e.get("reason"),
                "applied": False,
            }
            planned_actions.append(action)

            if not dry_run:
                conn.execute(
                    "UPDATE memories SET priority = ?, last_accessed_at = ? WHERE id = ?",
                    (target_priority, now_iso, entity_id),
                )
                insert_memory_event(
                    conn,
                    memory_id=entity_id,
                    event_type="priority.updated",
                    payload={
                        "priority": target_priority,
                        "reason": "escalation_reaction",
                        "escalation_level": level,
                    },
                )
                action["applied"] = True

    owner_actions: list[dict[str, Any]] = []
    emitted_owners: set[str] = set()
    for owner_role_key, count in overloaded_owners.items():
        if owner_role_key in emitted_owners:
            continue
        emitted_owners.add(owner_role_key)
        owner_action = {
            "action": "flag_owner_overloaded",
            "owner_role": owner_role_key,
            "level3_escalation_count": count,
            "applied": False,
        }
        owner_actions.append(owner_action)

        if not dry_run:
            _timeline.record_project_event(
                conn,
                project_key=owner_catalog_audit_project_key(normalized_project_key),
                event_type="project.note_recorded",
                title=f"Owner overloaded: {owner_role_key}",
                description=(
                    f"owner_role={owner_role_key}; level3_escalation_count={count}; "
                    f"threshold={owner_overload_threshold}"
                ),
                origin="system",
                tags=["owner_overloaded", "escalation_reaction"],
                status="completed",
                canonical=True,
                category="owner_overloaded",
                now_fn=_utc_now_iso,
            )
            owner_action["applied"] = True

    if not dry_run:
        conn.commit()

    all_actions = planned_actions + owner_actions
    applied_count = sum(1 for a in all_actions if a.get("applied"))
    return {
        "status": "ok",
        "summary": {
            "total_actions": len(all_actions),
            "priority_boosts": len(planned_actions),
            "owner_overload_flags": len(owner_actions),
            "applied": applied_count if not dry_run else 0,
            "dry_run": dry_run,
        },
        "actions": all_actions,
        "filters": {
            "project_key": normalized_project_key,
            "scope_code": normalized_scope_code,
            "min_level": min_level,
            "owner_overload_threshold": owner_overload_threshold,
        },
    }


def get_quality_alerts_payload(
    *,
    validated_before: str | None = None,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    max_review_queue: int = 10,
    max_revalidation_queue: int = 10,
    max_expired_queue: int = 5,
    max_duplicate_queue: int = 5,
    max_avg_approval_lead_seconds: float = 86400.0,
    max_overdue_review_count: int = 0,
    max_overdue_revalidation_count: int = 0,
    max_missing_owner_count: int = 0,
    max_overdue_review_count_level2: int = 3,
    max_overdue_review_count_level3: int = 7,
    max_overdue_revalidation_count_level2: int = 3,
    max_overdue_revalidation_count_level3: int = 7,
    max_missing_owner_count_level2: int = 2,
    max_missing_owner_count_level3: int = 5,
    max_overdue_expired_count: int = 0,
    max_overdue_expired_count_level2: int = 2,
    max_overdue_expired_count_level3: int = 5,
    max_overdue_duplicate_count: int = 0,
    max_overdue_duplicate_count_level2: int = 2,
    max_overdue_duplicate_count_level3: int = 5,
    max_owner_overdue_total: int = 2,
    max_owner_overdue_total_level2: int = 4,
    max_owner_overdue_total_level3: int = 7,
    max_broken_owner_mapping_count: int = 0,
    max_broken_owner_mapping_count_level2: int = 1,
    max_broken_owner_mapping_count_level3: int = 3,
    max_owner_catalog_governance_warning_count: int = 0,
    max_owner_catalog_governance_warning_count_level2: int = 3,
    max_owner_catalog_governance_warning_count_level3: int = 7,
    max_project_scope_mismatch_count: int = 0,
    max_project_scope_mismatch_count_level2: int = 2,
    max_project_scope_mismatch_count_level3: int = 5,
    get_queue_observability_metrics: Callable[..., dict[str, Any]],
    get_effective_owner_workload: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    metrics = get_queue_observability_metrics(
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

    alerts: list[dict[str, Any]] = []
    backlogs = metrics["backlogs"]
    approval_metrics = metrics["approval_metrics"]

    if backlogs["review_queue_count"] > int(max_review_queue):
        alerts.append({"severity": "warning", "kind": "review_backlog", "value": backlogs["review_queue_count"], "threshold": int(max_review_queue)})
    if backlogs["revalidation_queue_count"] > int(max_revalidation_queue):
        alerts.append({"severity": "warning", "kind": "revalidation_backlog", "value": backlogs["revalidation_queue_count"], "threshold": int(max_revalidation_queue)})
    if backlogs["expired_queue_count"] > int(max_expired_queue):
        alerts.append({"severity": "warning", "kind": "expired_backlog", "value": backlogs["expired_queue_count"], "threshold": int(max_expired_queue)})
    if backlogs["duplicate_queue_count"] > int(max_duplicate_queue):
        alerts.append({"severity": "warning", "kind": "duplicate_backlog", "value": backlogs["duplicate_queue_count"], "threshold": int(max_duplicate_queue)})
    if approval_metrics["approval_lead_time_avg_seconds"] > float(max_avg_approval_lead_seconds):
        alerts.append({"severity": "warning", "kind": "approval_lead_time", "value": approval_metrics["approval_lead_time_avg_seconds"], "threshold": float(max_avg_approval_lead_seconds)})

    es = escalation_stage
    review_esc = es(value=backlogs.get("overdue_review_count", 0), level1_threshold=max_overdue_review_count, level2_threshold=max_overdue_review_count_level2, level3_threshold=max_overdue_review_count_level3)
    reval_esc = es(value=backlogs.get("overdue_revalidation_count", 0), level1_threshold=max_overdue_revalidation_count, level2_threshold=max_overdue_revalidation_count_level2, level3_threshold=max_overdue_revalidation_count_level3)
    owner_miss_esc = es(value=metrics.get("inventory", {}).get("missing_owner_count", 0), level1_threshold=max_missing_owner_count, level2_threshold=max_missing_owner_count_level2, level3_threshold=max_missing_owner_count_level3)
    expired_esc = es(value=backlogs.get("overdue_expired_count", 0), level1_threshold=max_overdue_expired_count, level2_threshold=max_overdue_expired_count_level2, level3_threshold=max_overdue_expired_count_level3)
    dup_esc = es(value=backlogs.get("overdue_duplicate_count", 0), level1_threshold=max_overdue_duplicate_count, level2_threshold=max_overdue_duplicate_count_level2, level3_threshold=max_overdue_duplicate_count_level3)
    top_owner = owner_workload["items"][0] if owner_workload.get("items") else None
    owner_overdue_total = int((top_owner or {}).get("overdue_total") or 0)
    owner_overload_esc = es(value=owner_overdue_total, level1_threshold=max_owner_overdue_total, level2_threshold=max_owner_overdue_total_level2, level3_threshold=max_owner_overdue_total_level3)
    broken_map_esc = es(value=metrics.get("inventory", {}).get("broken_owner_mapping_count", 0), level1_threshold=max_broken_owner_mapping_count, level2_threshold=max_broken_owner_mapping_count_level2, level3_threshold=max_broken_owner_mapping_count_level3)
    gov_esc = es(value=metrics.get("inventory", {}).get("owner_catalog_governance_warning_count", 0), level1_threshold=max_owner_catalog_governance_warning_count, level2_threshold=max_owner_catalog_governance_warning_count_level2, level3_threshold=max_owner_catalog_governance_warning_count_level3)
    scope_mismatch_esc = es(value=metrics.get("inventory", {}).get("project_scope_mismatch_count", 0), level1_threshold=max_project_scope_mismatch_count, level2_threshold=max_project_scope_mismatch_count_level2, level3_threshold=max_project_scope_mismatch_count_level3)

    _RUNBOOK_OVERDUE = "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OVERDUE_ESCALATION_RUNBOOK.md"
    _RUNBOOK_REBALANCE = "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OWNER_REBALANCE_RUNBOOK.md"
    _RUNBOOK_GOV = "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OWNER_CATALOG_GOVERNANCE.md"

    if review_esc["level"] > 0:
        alerts.append({"severity": review_esc["severity"], "kind": "review_overdue", "value": backlogs.get("overdue_review_count", 0), "threshold": int(max_overdue_review_count), "escalation_level": review_esc["level"], "escalation_stage": review_esc["stage"], "runbook": _RUNBOOK_OVERDUE})
    if reval_esc["level"] > 0:
        alerts.append({"severity": reval_esc["severity"], "kind": "revalidation_overdue", "value": backlogs.get("overdue_revalidation_count", 0), "threshold": int(max_overdue_revalidation_count), "escalation_level": reval_esc["level"], "escalation_stage": reval_esc["stage"], "runbook": _RUNBOOK_OVERDUE})
    if owner_miss_esc["level"] > 0:
        alerts.append({"severity": owner_miss_esc["severity"], "kind": "owner_missing", "value": metrics.get("inventory", {}).get("missing_owner_count", 0), "threshold": int(max_missing_owner_count), "escalation_level": owner_miss_esc["level"], "escalation_stage": owner_miss_esc["stage"], "runbook": _RUNBOOK_OVERDUE})
    if expired_esc["level"] > 0:
        alerts.append({"severity": expired_esc["severity"], "kind": "expired_overdue", "value": backlogs.get("overdue_expired_count", 0), "threshold": int(max_overdue_expired_count), "escalation_level": expired_esc["level"], "escalation_stage": expired_esc["stage"], "runbook": _RUNBOOK_OVERDUE})
    if dup_esc["level"] > 0:
        alerts.append({"severity": dup_esc["severity"], "kind": "duplicate_overdue", "value": backlogs.get("overdue_duplicate_count", 0), "threshold": int(max_overdue_duplicate_count), "escalation_level": dup_esc["level"], "escalation_stage": dup_esc["stage"], "runbook": _RUNBOOK_OVERDUE})
    if owner_overload_esc["level"] > 0 and top_owner is not None:
        alerts.append({"severity": owner_overload_esc["severity"], "kind": "owner_overloaded", "value": owner_overdue_total, "threshold": int(max_owner_overdue_total), "escalation_level": owner_overload_esc["level"], "escalation_stage": owner_overload_esc["stage"], "effective_owner_key": top_owner.get("effective_owner_key"), "effective_owner_type": top_owner.get("effective_owner_type"), "effective_display_name": top_owner.get("effective_display_name"), "total_count": int(top_owner.get("total_count") or 0), "overdue_total": owner_overdue_total, "runbook": _RUNBOOK_OVERDUE})
    if broken_map_esc["level"] > 0:
        alerts.append({"severity": broken_map_esc["severity"], "kind": "broken_owner_mapping", "value": metrics.get("inventory", {}).get("broken_owner_mapping_count", 0), "threshold": int(max_broken_owner_mapping_count), "escalation_level": broken_map_esc["level"], "escalation_stage": broken_map_esc["stage"], "runbook": _RUNBOOK_REBALANCE})
    if gov_esc["level"] > 0:
        alerts.append({"severity": gov_esc["severity"], "kind": "owner_catalog_governance_warning", "value": metrics.get("inventory", {}).get("owner_catalog_governance_warning_count", 0), "threshold": int(max_owner_catalog_governance_warning_count), "escalation_level": gov_esc["level"], "escalation_stage": gov_esc["stage"], "runbook": _RUNBOOK_GOV})
    if scope_mismatch_esc["level"] > 0:
        alerts.append({"severity": scope_mismatch_esc["severity"], "kind": "project_scope_mismatch", "value": metrics.get("inventory", {}).get("project_scope_mismatch_count", 0), "threshold": int(max_project_scope_mismatch_count), "escalation_level": scope_mismatch_esc["level"], "escalation_stage": scope_mismatch_esc["stage"], "runbook": _RUNBOOK_REBALANCE})

    escalation_areas = {
        "review_overdue": {**review_esc, "value": backlogs.get("overdue_review_count", 0), "thresholds": {"level1": int(max_overdue_review_count), "level2": int(max_overdue_review_count_level2), "level3": int(max_overdue_review_count_level3)}},
        "revalidation_overdue": {**reval_esc, "value": backlogs.get("overdue_revalidation_count", 0), "thresholds": {"level1": int(max_overdue_revalidation_count), "level2": int(max_overdue_revalidation_count_level2), "level3": int(max_overdue_revalidation_count_level3)}},
        "owner_missing": {**owner_miss_esc, "value": metrics.get("inventory", {}).get("missing_owner_count", 0), "thresholds": {"level1": int(max_missing_owner_count), "level2": int(max_missing_owner_count_level2), "level3": int(max_missing_owner_count_level3)}},
        "expired_overdue": {**expired_esc, "value": backlogs.get("overdue_expired_count", 0), "thresholds": {"level1": int(max_overdue_expired_count), "level2": int(max_overdue_expired_count_level2), "level3": int(max_overdue_expired_count_level3)}},
        "duplicate_overdue": {**dup_esc, "value": backlogs.get("overdue_duplicate_count", 0), "thresholds": {"level1": int(max_overdue_duplicate_count), "level2": int(max_overdue_duplicate_count_level2), "level3": int(max_overdue_duplicate_count_level3)}},
        "owner_overloaded": {**owner_overload_esc, "value": owner_overdue_total, "effective_owner_key": None if top_owner is None else top_owner.get("effective_owner_key"), "thresholds": {"level1": int(max_owner_overdue_total), "level2": int(max_owner_overdue_total_level2), "level3": int(max_owner_overdue_total_level3)}},
        "broken_owner_mapping": {**broken_map_esc, "value": metrics.get("inventory", {}).get("broken_owner_mapping_count", 0), "thresholds": {"level1": int(max_broken_owner_mapping_count), "level2": int(max_broken_owner_mapping_count_level2), "level3": int(max_broken_owner_mapping_count_level3)}},
        "owner_catalog_governance_warning": {**gov_esc, "value": metrics.get("inventory", {}).get("owner_catalog_governance_warning_count", 0), "thresholds": {"level1": int(max_owner_catalog_governance_warning_count), "level2": int(max_owner_catalog_governance_warning_count_level2), "level3": int(max_owner_catalog_governance_warning_count_level3)}},
        "project_scope_mismatch": {**scope_mismatch_esc, "value": metrics.get("inventory", {}).get("project_scope_mismatch_count", 0), "thresholds": {"level1": int(max_project_scope_mismatch_count), "level2": int(max_project_scope_mismatch_count_level2), "level3": int(max_project_scope_mismatch_count_level3)}},
    }

    feature_flag_evaluation = metrics.get("feature_flag_evaluation") or {}
    feature_flag = metrics.get("feature_flag") or {}
    if not bool(feature_flag_evaluation.get("enabled", False)):
        alerts.append({"severity": "info", "kind": "feature_flag_disabled", "value": feature_flag_evaluation.get("reason"), "threshold": None})
    if bool(feature_flag_evaluation.get("read_only_mode", False)):
        alerts.append({"severity": "info", "kind": "feature_flag_read_only", "value": True, "threshold": None})

    escalation_summary = {
        "highest": highest_escalation_summary(list(escalation_areas.values())),
        "areas": escalation_areas,
        "runbook": _RUNBOOK_OVERDUE,
    }

    return {
        "status": "ok" if not alerts else "attention",
        "alert_count": len(alerts),
        "alerts": alerts,
        "feature_flag": feature_flag,
        "feature_flag_evaluation": feature_flag_evaluation,
        "metrics": metrics,
        "owner_workload": owner_workload,
        "escalation_summary": escalation_summary,
        "thresholds": {
            "max_review_queue": int(max_review_queue),
            "max_revalidation_queue": int(max_revalidation_queue),
            "max_expired_queue": int(max_expired_queue),
            "max_duplicate_queue": int(max_duplicate_queue),
            "max_avg_approval_lead_seconds": float(max_avg_approval_lead_seconds),
            "max_overdue_review_count": int(max_overdue_review_count),
            "max_overdue_review_count_level2": int(max_overdue_review_count_level2),
            "max_overdue_review_count_level3": int(max_overdue_review_count_level3),
            "max_overdue_revalidation_count": int(max_overdue_revalidation_count),
            "max_overdue_revalidation_count_level2": int(max_overdue_revalidation_count_level2),
            "max_overdue_revalidation_count_level3": int(max_overdue_revalidation_count_level3),
            "max_missing_owner_count": int(max_missing_owner_count),
            "max_missing_owner_count_level2": int(max_missing_owner_count_level2),
            "max_missing_owner_count_level3": int(max_missing_owner_count_level3),
            "max_overdue_expired_count": int(max_overdue_expired_count),
            "max_overdue_expired_count_level2": int(max_overdue_expired_count_level2),
            "max_overdue_expired_count_level3": int(max_overdue_expired_count_level3),
            "max_overdue_duplicate_count": int(max_overdue_duplicate_count),
            "max_overdue_duplicate_count_level2": int(max_overdue_duplicate_count_level2),
            "max_overdue_duplicate_count_level3": int(max_overdue_duplicate_count_level3),
            "max_owner_overdue_total": int(max_owner_overdue_total),
            "max_owner_overdue_total_level2": int(max_owner_overdue_total_level2),
            "max_owner_overdue_total_level3": int(max_owner_overdue_total_level3),
            "max_broken_owner_mapping_count": int(max_broken_owner_mapping_count),
            "max_broken_owner_mapping_count_level2": int(max_broken_owner_mapping_count_level2),
            "max_broken_owner_mapping_count_level3": int(max_broken_owner_mapping_count_level3),
            "max_owner_catalog_governance_warning_count": int(max_owner_catalog_governance_warning_count),
            "max_owner_catalog_governance_warning_count_level2": int(max_owner_catalog_governance_warning_count_level2),
            "max_owner_catalog_governance_warning_count_level3": int(max_owner_catalog_governance_warning_count_level3),
            "max_project_scope_mismatch_count": int(max_project_scope_mismatch_count),
            "max_project_scope_mismatch_count_level2": int(max_project_scope_mismatch_count_level2),
            "max_project_scope_mismatch_count_level3": int(max_project_scope_mismatch_count_level3),
        },
    }
