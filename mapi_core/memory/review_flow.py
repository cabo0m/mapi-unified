from __future__ import annotations

"""Memory draft and approval review flow payloads."""

from typing import Any, Callable


def create_memory_draft_payload(
    conn: Any,
    *,
    content: str,
    memory_type: str,
    summary_short: str | None = None,
    source: str | None = None,
    importance_score: float = 0.5,
    confidence_score: float = 0.5,
    tags: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    scope_code: str | None = None,
    parent_memory_id: int | None = None,
    project_key: str | None = None,
    conversation_key: str | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    review_due_at: str | None = None,
    cross_project_flag_key: str,
    normalize_scope_code: Callable[[Any], str | None],
    normalize_layer_code: Callable[[Any], str | None],
    normalize_area_code: Callable[[Any], str | None],
    normalize_optional_text: Callable[[Any], str | None],
    require_feature_flag_write_access: Callable[..., dict[str, Any]],
    insert_memory: Callable[..., dict[str, Any]],
    insert_memory_event: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    normalized_scope_code = normalize_scope_code(scope_code)
    normalized_layer_code = normalize_layer_code(layer_code)
    normalized_area_code = normalize_area_code(area_code)
    normalized_source = normalize_optional_text(source) or "manual_draft"
    try:
        if normalized_scope_code == "global":
            require_feature_flag_write_access(
                conn,
                flag_key=cross_project_flag_key,
                project_key=project_key,
                scope_code=normalized_scope_code,
                operation_name="create_memory_draft",
            )
        memory = insert_memory(
            conn,
            content=content,
            memory_type=memory_type,
            summary_short=summary_short,
            source=normalized_source,
            importance_score=importance_score,
            confidence_score=confidence_score,
            tags=tags,
            layer_code=normalized_layer_code,
            area_code=normalized_area_code,
            state_code="candidate",
            scope_code=normalized_scope_code,
            parent_memory_id=parent_memory_id,
            project_key=project_key,
            conversation_key=conversation_key,
            last_validated_at=None,
            validation_source=None,
            owner_role=owner_role,
            owner_id=owner_id,
            review_due_at=review_due_at,
        )
        draft_event = insert_memory_event(
            conn,
            memory_id=int(memory["id"]),
            event_type="review.draft_created",
            payload={
                "source": normalized_source,
                "scope_code": memory.get("scope_code"),
                "layer_code": memory.get("layer_code"),
                "area_code": memory.get("area_code"),
                "project_key": memory.get("project_key"),
                "conversation_key": memory.get("conversation_key"),
            },
        )
        conn.commit()
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    return {"status": "draft_created", "memory": memory, "event": draft_event}


def preview_memory_quality_gate_payload(
    conn: Any,
    *,
    memory_id: int,
    target_scope_code: str | None = None,
    require_memory_row: Callable[[Any, int], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    normalize_scope_code: Callable[[Any], str | None],
    quality_gate_issues_for_memory: Callable[..., list[str]],
) -> dict[str, Any]:
    memory = require_memory_row(conn, int(memory_id))
    enriched = enrich_memory_dict(row_to_dict(memory))
    normalized_target_scope = normalize_scope_code(target_scope_code) or enriched["scope_code"]
    issues = quality_gate_issues_for_memory(enriched, target_scope_code=normalized_target_scope)
    return {
        "status": "completed",
        "memory_id": int(memory_id),
        "target_scope_code": normalized_target_scope,
        "passed": len(issues) == 0,
        "issues": issues,
        "memory": enriched,
    }


def approve_memory_payload(
    conn: Any,
    *,
    memory_id: int,
    validation_source: str | None = "manual_review",
    scope_code: str | None = None,
    importance_score: float | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    revalidation_due_at: str | None = None,
    require_memory_row: Callable[[Any, int], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    normalize_scope_code: Callable[[Any], str | None],
    normalize_optional_text: Callable[[Any], str | None],
    normalize_score: Callable[[float], float],
    utc_now_iso: Callable[[], str],
    utc_offset_days_iso: Callable[[int], str],
    shift_iso_days: Callable[[str | None, int], str | None],
    compute_sla_days: Callable[..., int],
    default_owner_role: Callable[..., str | None],
    quality_gate_issues_for_memory: Callable[..., list[str]],
    insert_memory_event: Callable[..., dict[str, Any]],
    apply_ownership_defaults: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    memory = require_memory_row(conn, memory_id)
    old_memory = enrich_memory_dict(row_to_dict(memory))
    if str(memory["activity_state"] or "active") == "archived":
        return {"status": "error", "error": 'Nie moĹĽna zatwierdziÄ‡ zarchiwizowanego wspomnienia'}

    normalized_scope = normalize_scope_code(scope_code) or old_memory["scope_code"]
    quality_gate_issues = quality_gate_issues_for_memory(old_memory, target_scope_code=normalized_scope)
    if quality_gate_issues:
        return {"status": "error", "error": f"Quality gate failed: {', '.join(quality_gate_issues)}"}

    validated_at = utc_now_iso()
    new_importance = old_memory["importance_score"] if importance_score is None else normalize_score(float(importance_score))
    normalized_validation_source = normalize_optional_text(validation_source) or "manual_review"
    normalized_owner_role = normalize_optional_text(owner_role) or old_memory.get("owner_role") or default_owner_role(
        state_code="validated",
        scope_code=normalized_scope,
        project_key=old_memory.get("project_key"),
    )
    normalized_revalidation_due_at = normalize_optional_text(revalidation_due_at) or old_memory.get("revalidation_due_at") or utc_offset_days_iso(
        compute_sla_days(conn, "revalidation", old_memory.get("priority") or "normal", old_memory.get("memory_type"), old_memory.get("scope_code"), old_memory.get("project_key"))
    )
    conn.execute(
        """
        UPDATE memories
        SET state_code = ?,
            scope_code = ?,
            importance_score = ?,
            last_validated_at = ?,
            validation_source = ?,
            last_accessed_at = ?,
            owner_role = ?,
            owner_id = ?,
            review_due_at = NULL,
            revalidation_due_at = ?
        WHERE id = ?
        """,
        (
            "validated",
            normalized_scope,
            new_importance,
            validated_at,
            normalized_validation_source,
            validated_at,
            normalized_owner_role,
            normalize_optional_text(owner_id) or old_memory.get("owner_id"),
            normalized_revalidation_due_at,
            int(memory_id),
        ),
    )
    approval_event = insert_memory_event(
        conn,
        memory_id=int(memory_id),
        event_type="review.approved",
        payload={
            "source": normalized_validation_source,
            "old_state_code": old_memory.get("state_code"),
            "new_state_code": "validated",
            "scope_code": normalized_scope,
            "importance_score": new_importance,
        },
    )
    superseded_event = None
    superseded_memory_id = old_memory.get("supersedes_memory_id")
    if superseded_memory_id is not None:
        previous_row = require_memory_row(conn, int(superseded_memory_id))
        previous_memory = enrich_memory_dict(row_to_dict(previous_row))
        if previous_memory.get("state_code") != "superseded":
            conn.execute(
                """
                UPDATE memories
                SET state_code = ?,
                    valid_to = ?,
                    expired_due_at = ?,
                    validation_source = ?,
                    last_accessed_at = ?
                WHERE id = ?
                """,
                ("superseded", validated_at, shift_iso_days(validated_at, 2), normalized_validation_source, validated_at, int(superseded_memory_id)),
            )
            superseded_event = insert_memory_event(
                conn,
                memory_id=int(superseded_memory_id),
                event_type="version.superseded",
                payload={
                    "source": normalized_validation_source,
                    "new_memory_id": int(memory_id),
                    "old_state_code": previous_memory.get("state_code"),
                    "new_state_code": "superseded",
                },
            )
    conn.commit()
    updated_row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    updated_memory = apply_ownership_defaults(enrich_memory_dict(row_to_dict(updated_row)))
    return {
        "status": "approved",
        "memory_id": int(memory_id),
        "old_state_code": old_memory["state_code"],
        "new_state_code": updated_memory["state_code"],
        "event": approval_event,
        "superseded_memory_id": None if superseded_memory_id is None else int(superseded_memory_id),
        "superseded_event": superseded_event,
        "memory": updated_memory,
    }
