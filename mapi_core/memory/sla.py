from __future__ import annotations

"""SLA policy helpers for memory queues."""

from collections import defaultdict
from typing import Any
from typing import Callable

SLA_FALLBACK_DAYS: dict[str, int] = {"review": 2, "revalidation": 5, "expired": 7, "duplicate": 3}


def compute_sla_days(
    conn: Any,
    queue_type: str,
    priority: str | None = "normal",
    memory_type: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
) -> int:
    rows = conn.execute(
        "SELECT * FROM sla_policies WHERE queue_type = ? AND is_active = 1",
        (queue_type,),
    ).fetchall()
    best_score, best_days = -1, None
    for r in rows:
        d = dict(r)
        score = 0
        if d.get("priority") is not None:
            if d["priority"] != priority:
                continue
            score += 8
        if d.get("project_key") is not None:
            if d["project_key"] != project_key:
                continue
            score += 4
        if d.get("scope_code") is not None:
            if d["scope_code"] != scope_code:
                continue
            score += 2
        if d.get("memory_type") is not None:
            if d["memory_type"] != memory_type:
                continue
            score += 1
        if score > best_score:
            best_score, best_days = score, int(d["sla_days"])
    return best_days if best_days is not None else SLA_FALLBACK_DAYS.get(queue_type, 2)


def sla_policy_observability_payload(
    conn: Any,
    *,
    queue_type: str | None = None,
    project_key: str | None = None,
    scope_code: str | None = None,
    as_of: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
) -> dict[str, Any]:
    valid_queue_types = ("review", "revalidation", "expired", "duplicate")
    normalized_queue_type = normalize_optional_text(queue_type)
    if normalized_queue_type is not None and normalized_queue_type not in valid_queue_types:
        return {"status": "error", "error": f"queue_type musi byÄ‡ jednym z: {', '.join(valid_queue_types)}"}
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)
    normalized_as_of = normalize_optional_text(as_of) or utc_now_iso()

    queue_configs = {
        "review": ("memories", "review_due_at", "priority", "state_code = 'candidate'"),
        "revalidation": ("memories", "revalidation_due_at", "priority", "state_code = 'validated'"),
        "expired": ("memories", "expired_due_at", "priority", "1=1"),
        "duplicate": ("duplicate_review_items", "duplicate_due_at", "priority", "status = 'open'"),
    }
    queues_to_check = [normalized_queue_type] if normalized_queue_type else list(queue_configs.keys())

    metrics: list[dict[str, Any]] = []
    items_without_policy: list[dict[str, Any]] = []
    for qt in queues_to_check:
        table, due_field, prio_field, state_cond = queue_configs[qt]
        base_sql = (
            f"SELECT {prio_field}, {due_field} FROM {table} "
            f"WHERE {due_field} IS NOT NULL AND {state_cond} AND activity_state = 'active'"
            if table == "memories"
            else f"SELECT {prio_field}, {due_field} FROM {table} "
            f"WHERE {due_field} IS NOT NULL AND {state_cond}"
        )
        rows = conn.execute(base_sql).fetchall()

        groups: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "overdue": 0})
        for r in rows:
            prio = str(r[prio_field] or "normal")
            groups[prio]["total"] += 1
            due_val = normalize_optional_text(r[due_field])
            if due_val and due_val <= normalized_as_of:
                groups[prio]["overdue"] += 1

        for prio, counts in groups.items():
            total = counts["total"]
            overdue = counts["overdue"]
            overdue_rate = round(overdue / total * 100, 1) if total > 0 else 0.0
            policy_days = compute_sla_days(conn, qt, prio, None, normalized_scope_code, normalized_project_key)
            is_fallback = not conn.execute(
                "SELECT 1 FROM sla_policies WHERE queue_type = ? AND is_active = 1 LIMIT 1",
                (qt,),
            ).fetchone()

            if is_fallback:
                items_without_policy.append({"queue_type": qt, "priority": prio, "total": total})

            if overdue_rate > 50:
                assessment = "too_aggressive"
            elif overdue_rate < 5 and policy_days > 14:
                assessment = "too_loose"
            else:
                assessment = "ok"

            metrics.append({
                "queue_type": qt,
                "priority": prio,
                "policy_days": policy_days,
                "policy_source": "fallback_default" if is_fallback else "configured",
                "total_items": total,
                "overdue_count": overdue,
                "overdue_rate_pct": overdue_rate,
                "assessment": assessment,
            })

    attention_count = sum(1 for m in metrics if m["assessment"] != "ok")
    return {
        "status": "attention" if attention_count > 0 else "ok",
        "summary": {
            "queues_checked": len(queues_to_check),
            "combinations_checked": len(metrics),
            "attention_count": attention_count,
            "items_without_configured_policy": len(items_without_policy),
        },
        "metrics": sorted(metrics, key=lambda x: (x["queue_type"], x["priority"])),
        "items_without_configured_policy": items_without_policy,
        "filters": {
            "queue_type": normalized_queue_type,
            "project_key": normalized_project_key,
            "scope_code": normalized_scope_code,
            "as_of": normalized_as_of,
        },
    }


def list_sla_policies_payload(
    conn: Any,
    *,
    queue_type: str | None = None,
    priority: str | None = None,
    active_only: bool = True,
    normalize_optional_text: Callable[[Any], str | None],
) -> dict[str, Any]:
    normalized_queue_type = normalize_optional_text(queue_type)
    normalized_priority = normalize_optional_text(priority)
    sql = "SELECT * FROM sla_policies WHERE 1=1"
    params: list[Any] = []
    if active_only:
        sql += " AND is_active = 1"
    if normalized_queue_type is not None:
        sql += " AND queue_type = ?"
        params.append(normalized_queue_type)
    if normalized_priority is not None:
        sql += " AND priority = ?"
        params.append(normalized_priority)
    sql += " ORDER BY queue_type, priority, project_key, scope_code, id"
    rows = conn.execute(sql, params).fetchall()
    policies = [dict(r) for r in rows]
    return {
        "status": "ok",
        "count": len(policies),
        "policies": policies,
        "filters": {
            "queue_type": normalized_queue_type,
            "priority": normalized_priority,
            "active_only": active_only,
        },
    }


def upsert_sla_policy_payload(
    conn: Any,
    *,
    queue_type: str,
    sla_days: int,
    priority: str | None = None,
    memory_type: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    is_active: bool = True,
    notes: str | None = None,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    owner_catalog_audit_project_key: Callable[[Any], str],
    record_project_event: Callable[..., int],
) -> dict[str, Any]:
    valid_queue_types = ("review", "revalidation", "expired", "duplicate")
    valid_priorities = ("low", "normal", "high", "critical")
    normalized_queue_type = normalize_required_text(queue_type, "queue_type").lower()
    if normalized_queue_type not in valid_queue_types:
        return {"status": "error", "error": f"queue_type musi byÄ‡ jednym z: {', '.join(valid_queue_types)}"}
    normalized_sla_days = int(sla_days)
    if normalized_sla_days < 1:
        return {"status": "error", "error": "sla_days musi byÄ‡ >= 1"}
    normalized_priority = normalize_optional_text(priority)
    if normalized_priority is not None and normalized_priority not in valid_priorities:
        return {"status": "error", "error": f"priority musi byÄ‡ jednym z: {', '.join(valid_priorities)}"}
    normalized_memory_type = normalize_optional_text(memory_type)
    normalized_scope_code = normalize_scope_code(scope_code)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_notes = normalize_optional_text(notes)
    now_iso = utc_now_iso()
    existing = conn.execute(
        "SELECT id FROM sla_policies WHERE queue_type = ? AND priority IS ? AND memory_type IS ? AND scope_code IS ? AND project_key IS ?",
        (normalized_queue_type, normalized_priority, normalized_memory_type, normalized_scope_code, normalized_project_key),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO sla_policies (queue_type, sla_days, priority, memory_type, scope_code, project_key, is_active, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                normalized_queue_type,
                normalized_sla_days,
                normalized_priority,
                normalized_memory_type,
                normalized_scope_code,
                normalized_project_key,
                int(is_active),
                normalized_notes,
                now_iso,
                now_iso,
            ),
        )
    else:
        conn.execute(
            "UPDATE sla_policies SET sla_days = ?, is_active = ?, notes = ?, updated_at = ? WHERE id = ?",
            (normalized_sla_days, int(is_active), normalized_notes, now_iso, int(existing["id"])),
        )
    row = conn.execute(
        "SELECT * FROM sla_policies WHERE queue_type = ? AND priority IS ? AND memory_type IS ? AND scope_code IS ? AND project_key IS ?",
        (normalized_queue_type, normalized_priority, normalized_memory_type, normalized_scope_code, normalized_project_key),
    ).fetchone()
    audit_event_id = record_project_event(
        conn,
        project_key=owner_catalog_audit_project_key(normalized_project_key),
        event_type="project.note_recorded",
        title=f"SLA policy {'created' if existing is None else 'updated'}: {normalized_queue_type}",
        description=(
            f"queue_type={normalized_queue_type}; sla_days={normalized_sla_days}; "
            f"priority={normalized_priority}; memory_type={normalized_memory_type}; "
            f"scope_code={normalized_scope_code}; project_key={normalized_project_key}; "
            f"is_active={bool(is_active)}"
        ),
        origin="system",
        tags=["sla_policy_change", "created" if existing is None else "updated"],
        status="completed",
        canonical=True,
        category="sla_policy_change",
        now_fn=utc_now_iso,
    )
    conn.commit()
    return {
        "status": "sla_policy_upserted",
        "policy": dict(row),
        "audit_event": {"id": audit_event_id, "event_type": "project.note_recorded"},
    }


def set_memory_sla_payload(
    conn: Any,
    *,
    memory_id: int,
    review_due_at: str | None = None,
    revalidation_due_at: str | None = None,
    expired_due_at: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    require_memory_row: Callable[[Any, int], Any],
    insert_memory_event: Callable[..., dict[str, Any]],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    normalized_review_due_at = normalize_optional_text(review_due_at)
    normalized_revalidation_due_at = normalize_optional_text(revalidation_due_at)
    normalized_expired_due_at = normalize_optional_text(expired_due_at)
    if normalized_review_due_at is None and normalized_revalidation_due_at is None and normalized_expired_due_at is None:
        return {"status": "error", "error": "Musisz podaÄ‡ review_due_at, revalidation_due_at albo expired_due_at"}
    require_memory_row(conn, int(memory_id))
    updated_at = utc_now_iso()
    conn.execute(
        "UPDATE memories SET review_due_at = COALESCE(?, review_due_at), revalidation_due_at = COALESCE(?, revalidation_due_at), expired_due_at = COALESCE(?, expired_due_at), last_accessed_at = ? WHERE id = ?",
        (normalized_review_due_at, normalized_revalidation_due_at, normalized_expired_due_at, updated_at, int(memory_id)),
    )
    event = insert_memory_event(
        conn,
        memory_id=int(memory_id),
        event_type="sla.updated",
        payload={
            "review_due_at": normalized_review_due_at,
            "revalidation_due_at": normalized_revalidation_due_at,
            "expired_due_at": normalized_expired_due_at,
        },
    )
    conn.commit()
    updated_row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    return {
        "status": "sla_updated",
        "event": event,
        "memory": apply_ownership_defaults(enrich_memory_dict(row_to_dict(updated_row))),
    }


def bulk_set_memory_sla_payload(
    conn: Any,
    *,
    memory_ids: list[int],
    review_due_at: str | None = None,
    revalidation_due_at: str | None = None,
    expired_due_at: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    require_memory_row: Callable[[Any, int], Any],
    insert_memory_event: Callable[..., dict[str, Any]],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    if not memory_ids:
        return {"status": "error", "error": "memory_ids nie mogÄ… byÄ‡ puste"}
    normalized_review_due_at = normalize_optional_text(review_due_at)
    normalized_revalidation_due_at = normalize_optional_text(revalidation_due_at)
    normalized_expired_due_at = normalize_optional_text(expired_due_at)
    if normalized_review_due_at is None and normalized_revalidation_due_at is None and normalized_expired_due_at is None:
        return {"status": "error", "error": "Musisz podaÄ‡ review_due_at, revalidation_due_at albo expired_due_at"}
    unique_ids = [int(memory_id) for memory_id in dict.fromkeys(memory_ids)]
    updated_at = utc_now_iso()
    items: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for memory_id in unique_ids:
        require_memory_row(conn, memory_id)
        conn.execute(
            "UPDATE memories SET review_due_at = COALESCE(?, review_due_at), revalidation_due_at = COALESCE(?, revalidation_due_at), expired_due_at = COALESCE(?, expired_due_at), last_accessed_at = ? WHERE id = ?",
            (normalized_review_due_at, normalized_revalidation_due_at, normalized_expired_due_at, updated_at, memory_id),
        )
        event = insert_memory_event(
            conn,
            memory_id=memory_id,
            event_type="sla.bulk_updated",
            payload={
                "review_due_at": normalized_review_due_at,
                "revalidation_due_at": normalized_revalidation_due_at,
                "expired_due_at": normalized_expired_due_at,
            },
        )
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        items.append(apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        events.append(event)
    conn.commit()
    return {
        "status": "bulk_sla_updated",
        "count": len(items),
        "memory_ids": unique_ids,
        "events": events,
        "items": items,
    }


def set_memory_priority_payload(
    conn: Any,
    *,
    memory_id: int,
    priority: str,
    normalize_required_text: Callable[[Any, str], str],
    utc_now_iso: Callable[[], str],
    require_memory_row: Callable[[Any, int], Any],
    insert_memory_event: Callable[..., dict[str, Any]],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    valid_priorities = ("low", "normal", "high", "critical")
    normalized_priority = normalize_required_text(priority, "priority").lower()
    if normalized_priority not in valid_priorities:
        return {"status": "error", "error": f"priority musi byÄ‡ jednym z: {', '.join(valid_priorities)}"}
    require_memory_row(conn, int(memory_id))
    updated_at = utc_now_iso()
    conn.execute(
        "UPDATE memories SET priority = ?, last_accessed_at = ? WHERE id = ?",
        (normalized_priority, updated_at, int(memory_id)),
    )
    event = insert_memory_event(
        conn,
        memory_id=int(memory_id),
        event_type="priority.updated",
        payload={"priority": normalized_priority},
    )
    conn.commit()
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    return {
        "status": "priority_updated",
        "event": event,
        "memory": apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))),
    }
