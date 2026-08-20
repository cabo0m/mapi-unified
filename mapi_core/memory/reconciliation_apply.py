from __future__ import annotations

from typing import Any, Callable

from app import conflict_logic
from mapi_core.memory.capture_queue import (
    get_capture_review_item,
    mark_capture_review_item_applied,
)
from mapi_core.memory.reconciliation import CAPTURE_RECONCILIATION_PREVIEW_SCHEMA_VERSION
from mapi_core.memory.supersession import (
    apply_memory_supersession_payload,
    preview_memory_supersession_payload,
)


CAPTURE_RECONCILIATION_APPLY_SCHEMA_VERSION = "memory_v3_capture_reconciliation_apply.v1"
CAPTURE_RECONCILIATION_APPLY_AUDIT_SCHEMA_VERSION = "memory_v3_capture_reconciliation_apply_audit.v1"
SUPPORTED_APPLY_OUTCOMES = frozenset(
    {
        "create_new",
        "create_version",
        "reinforce_existing",
        "duplicate_existing",
        "conflict_review",
        "skip_transient",
    }
)
EXPECTED_FUTURE_ACTIONS = {
    "create_new": "create_new",
    "create_version": "create_version",
    "reinforce_existing": "reinforce_existing",
    "duplicate_existing": "mark_duplicate",
    "conflict_review": "conflict_review",
    "skip_transient": "skip",
}


class ReconciliationApplyBlocked(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _base_result(
    *,
    status: str,
    item_id: int,
    expected_preview_hash: str | None,
    proposal_key: str | None = None,
    outcome: str | None = None,
    queue_status_before: str | None = None,
    queue_status_after: str | None = None,
    current_preview_hash: str | None = None,
    created_memory_id: int | None = None,
    primary_memory_id: int | None = None,
    supersession_run_id: int | None = None,
    event_ids: list[int] | None = None,
    link_ids: list[int] | None = None,
    lifecycle_transitions: list[dict[str, Any]] | None = None,
    apply_audit: dict[str, Any] | None = None,
    operator_next_action: str = "inspect_blockers",
    unsupported_metrics: list[str] | None = None,
    blocking_reasons: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload = {
        "status": status,
        "schema_version": CAPTURE_RECONCILIATION_APPLY_SCHEMA_VERSION,
        "item_id": int(item_id),
        "proposal_key": proposal_key,
        "outcome": outcome,
        "queue_status_before": queue_status_before,
        "queue_status_after": queue_status_after,
        "expected_preview_hash": expected_preview_hash,
        "current_preview_hash": current_preview_hash,
        "created_memory_id": created_memory_id,
        "primary_memory_id": primary_memory_id,
        "supersession_run_id": supersession_run_id,
        "event_ids": sorted({int(value) for value in (event_ids or [])}),
        "link_ids": sorted({int(value) for value in (link_ids or [])}),
        "lifecycle_transitions": list(lifecycle_transitions or []),
        "apply_audit": apply_audit,
        "safety": {
            "atomic": True,
            "model_auto_apply": False,
            "operator_required": True,
        },
        "operator_next_action": operator_next_action,
        "unsupported_metrics": sorted(set(unsupported_metrics or [])),
        "blocking_reasons": sorted(set(blocking_reasons or [])),
    }
    if error is not None:
        payload["error"] = error
    return payload


def _insert_proposal_memory(
    conn: Any,
    *,
    proposal: dict[str, Any],
    proposal_key: str,
    applied_at: str,
    insert_memory: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    original_source = str(proposal.get("source") or "memory_v3_capture_apply")
    return insert_memory(
        conn,
        content=str(proposal.get("content") or ""),
        memory_type=str(proposal.get("memory_type") or "project_note"),
        summary_short=proposal.get("summary_short"),
        source=f"{original_source};proposal_key={proposal_key}",
        importance_score=float(proposal.get("importance_score") or 0.5),
        confidence_score=float(proposal.get("confidence_score") or 0.5),
        tags=proposal.get("tags"),
        layer_code=proposal.get("layer_code"),
        area_code=proposal.get("area_code"),
        state_code="validated",
        scope_code=proposal.get("scope_code"),
        parent_memory_id=proposal.get("parent_memory_id"),
        version=int(proposal.get("version") or 1),
        promoted_from_id=proposal.get("promoted_from_id"),
        demoted_from_id=proposal.get("demoted_from_id"),
        supersedes_memory_id=None,
        valid_from=proposal.get("valid_from"),
        valid_to=None,
        decay_score=float(proposal.get("decay_score") or 0.0),
        emotional_weight=float(proposal.get("emotional_weight") or 0.0),
        identity_weight=float(proposal.get("identity_weight") or 0.0),
        project_key=proposal.get("project_key"),
        conversation_key=proposal.get("conversation_key"),
        last_validated_at=applied_at,
        validation_source="memory_v3_capture_apply",
        schema_version=max(int(proposal.get("schema_version") or 2), 2),
        entry_type=proposal.get("entry_type"),
        truth_kind=proposal.get("truth_kind"),
        title=proposal.get("title"),
        source_context=proposal.get("source_context"),
        source_event_ref=proposal.get("source_event_ref"),
        updated_at=applied_at,
        last_confirmed_at=applied_at,
        memory_v2_status="active",
        importance_level=proposal.get("importance_level"),
        superseded_by_memory_id=None,
        requires_user_confirmation=False,
        should_resurface_when=proposal.get("should_resurface_when"),
        owner_role=proposal.get("owner_role"),
        owner_id=proposal.get("owner_id"),
        review_due_at=proposal.get("review_due_at"),
        revalidation_due_at=proposal.get("revalidation_due_at"),
        expired_due_at=proposal.get("expired_due_at"),
        priority=proposal.get("priority"),
        visibility_scope=proposal.get("visibility_scope"),
        workspace_id=proposal.get("workspace_id"),
        owner_user_id=proposal.get("owner_user_id"),
        created_by_user_id=proposal.get("created_by_user_id"),
        last_modified_by_user_id=proposal.get("last_modified_by_user_id"),
        sharing_policy=proposal.get("sharing_policy"),
        ensure_embedding=False,
    )


def _stored_apply_result(
    *,
    item: dict[str, Any],
    expected_preview_hash: str,
) -> dict[str, Any]:
    audit = dict((item.get("reconciliation") or {}).get("apply_audit") or {})
    if not audit or str(audit.get("result_fingerprint") or "").strip() == "":
        return _base_result(
            status="blocked",
            item_id=int(item["id"]),
            proposal_key=item.get("proposal_key"),
            outcome=audit.get("outcome"),
            queue_status_before="applied",
            queue_status_after="applied",
            expected_preview_hash=expected_preview_hash,
            current_preview_hash=item.get("reconciliation_preview_hash"),
            blocking_reasons=["applied_item_missing_apply_audit"],
        )
    if str(audit.get("expected_preview_hash") or "") != expected_preview_hash:
        return _base_result(
            status="blocked",
            item_id=int(item["id"]),
            proposal_key=item.get("proposal_key"),
            outcome=audit.get("outcome"),
            queue_status_before="applied",
            queue_status_after="applied",
            expected_preview_hash=expected_preview_hash,
            current_preview_hash=item.get("reconciliation_preview_hash"),
            apply_audit=audit,
            blocking_reasons=["applied_item_contract_mismatch"],
        )
    return _base_result(
        status="already_applied",
        item_id=int(item["id"]),
        proposal_key=item.get("proposal_key"),
        outcome=audit.get("outcome"),
        queue_status_before="applied",
        queue_status_after="applied",
        expected_preview_hash=expected_preview_hash,
        current_preview_hash=item.get("reconciliation_preview_hash"),
        created_memory_id=audit.get("created_memory_id"),
        primary_memory_id=audit.get("primary_memory_id"),
        supersession_run_id=audit.get("supersession_run_id"),
        event_ids=list(audit.get("event_ids") or []),
        link_ids=list(audit.get("link_ids") or []),
        lifecycle_transitions=list(audit.get("lifecycle_transitions") or []),
        apply_audit=audit,
        operator_next_action="none",
    )


def apply_memory_capture_reconciliation_payload(
    conn: Any,
    *,
    item_id: int,
    expected_preview_hash: str,
    applied_by: str,
    notes: str | None = None,
    confirm_protected: bool = False,
    include_debug: bool = False,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    canonical_json_hash: Callable[[Any], str],
    utc_now_iso: Callable[[], str],
    shift_iso_days: Callable[[str | None, int], str | None],
    memory_v2_enabled: Callable[[Any], bool],
    reconciliation_flag_evaluation: Callable[..., dict[str, Any]],
    capture_proposal_key: Callable[..., str],
    preview_reconciliation: Callable[..., dict[str, Any]],
    search_semantic_func: Callable[..., dict[str, Any]] | None,
    insert_memory: Callable[..., dict[str, Any]],
    insert_memory_event: Callable[..., dict[str, Any]],
    create_link: Callable[..., dict[str, Any]],
    record_timeline_event: Callable[..., int],
    new_operation_id: Callable[[str | None], str],
) -> dict[str, Any]:
    try:
        normalized_hash = normalize_required_text(expected_preview_hash, "expected_preview_hash")
        normalized_applied_by = normalize_required_text(applied_by, "applied_by")
    except ValueError as exc:
        return _base_result(
            status="blocked",
            item_id=int(item_id),
            expected_preview_hash=normalize_optional_text(expected_preview_hash),
            blocking_reasons=[str(exc)],
        )

    queue_status_before: str | None = None
    proposal_key: str | None = None
    outcome: str | None = None
    current_preview_hash: str | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        item = get_capture_review_item(conn, item_id=int(item_id), row_to_dict=row_to_dict)
        queue_status_before = str(item["status"])
        proposal_key = str(item["proposal_key"])
        current_preview_hash = normalize_optional_text(item.get("reconciliation_preview_hash"))
        if queue_status_before == "applied":
            result = _stored_apply_result(item=item, expected_preview_hash=normalized_hash)
            conn.rollback()
            return result

        if not memory_v2_enabled(conn):
            raise ReconciliationApplyBlocked("memory_v2_feature_flag_off")
        flag_evaluation = reconciliation_flag_evaluation(
            conn,
            project_key=item.get("project_key"),
            scope_code=item.get("scope_code"),
        )
        if not flag_evaluation.get("enabled"):
            raise ReconciliationApplyBlocked("memory_v3_capture_reconciliation_feature_flag_off")
        if flag_evaluation.get("read_only_mode"):
            raise ReconciliationApplyBlocked("reconciliation_apply_blocked_read_only_mode")
        if queue_status_before != "approved":
            raise ReconciliationApplyBlocked(f"item_status_not_approved:{queue_status_before}")

        applied_at = utc_now_iso()
        expires_at = normalize_optional_text(item.get("expires_at"))
        if expires_at is not None and expires_at <= applied_at:
            raise ReconciliationApplyBlocked("item_expired")
        proposal = dict(item.get("proposal") or {})
        raw_workspace_id = proposal.get("workspace_id")
        if raw_workspace_id is None:
            default_workspace = conn.execute(
                "SELECT id FROM workspaces WHERE workspace_key = 'default' LIMIT 1"
            ).fetchone()
            proposal_workspace_id = None if default_workspace is None else int(default_workspace["id"])
        else:
            try:
                proposal_workspace_id = int(raw_workspace_id)
            except (TypeError, ValueError) as exc:
                raise ReconciliationApplyBlocked("invalid_workspace_id") from exc
        input_fingerprint = normalize_required_text(item.get("input_fingerprint"), "input_fingerprint")
        if proposal_key != capture_proposal_key(input_fingerprint=input_fingerprint):
            raise ReconciliationApplyBlocked("proposal_key_input_fingerprint_mismatch")
        if normalize_optional_text(item.get("project_key")) != normalize_optional_text(proposal.get("project_key")):
            raise ReconciliationApplyBlocked("immutable_project_key_mismatch")
        if normalize_optional_text(item.get("scope_code")) != normalize_optional_text(proposal.get("scope_code")):
            raise ReconciliationApplyBlocked("immutable_scope_code_mismatch")

        stored_preview = dict(item.get("reconciliation") or {})
        if stored_preview.get("schema_version") != CAPTURE_RECONCILIATION_PREVIEW_SCHEMA_VERSION:
            raise ReconciliationApplyBlocked("preview_schema_v2_required")
        if stored_preview.get("status") != "preview_ready":
            raise ReconciliationApplyBlocked("stored_preview_not_ready")
        if current_preview_hash != normalized_hash:
            conn.rollback()
            return _base_result(
                status="stale_preview",
                item_id=int(item_id),
                proposal_key=proposal_key,
                outcome=stored_preview.get("outcome"),
                queue_status_before=queue_status_before,
                queue_status_after=queue_status_before,
                expected_preview_hash=normalized_hash,
                current_preview_hash=current_preview_hash,
                blocking_reasons=["expected_preview_hash_mismatch"],
            )

        preview_options = dict(stored_preview.get("preview_options") or {})
        fresh_preview = preview_reconciliation(
            conn,
            item_id=int(item_id),
            candidate_limit=int(preview_options.get("candidate_limit") or 20),
            semantic_limit=int(preview_options.get("semantic_limit") or 10),
            include_semantic=bool(preview_options.get("include_semantic", True)),
            include_debug=bool(include_debug),
            persist=False,
            normalize_required_text=normalize_required_text,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            canonical_json_hash=canonical_json_hash,
            utc_now_iso=utc_now_iso,
            search_semantic_func=search_semantic_func,
        )
        current_preview_hash = normalize_optional_text(fresh_preview.get("reconciliation_preview_hash"))
        if fresh_preview.get("schema_version") != CAPTURE_RECONCILIATION_PREVIEW_SCHEMA_VERSION:
            raise ReconciliationApplyBlocked("fresh_preview_schema_v2_required")
        if fresh_preview.get("status") != "preview_ready" or not fresh_preview.get("guard", {}).get("allowed"):
            raise ReconciliationApplyBlocked("fresh_preview_not_allowed")
        if current_preview_hash != normalized_hash:
            conn.rollback()
            return _base_result(
                status="stale_preview",
                item_id=int(item_id),
                proposal_key=proposal_key,
                outcome=fresh_preview.get("outcome"),
                queue_status_before=queue_status_before,
                queue_status_after=queue_status_before,
                expected_preview_hash=normalized_hash,
                current_preview_hash=current_preview_hash,
                unsupported_metrics=list(fresh_preview.get("unsupported_metrics") or []),
                blocking_reasons=["fresh_preview_hash_mismatch"],
            )

        outcome = str(fresh_preview.get("outcome") or "")
        if outcome == "update_metadata_proposal":
            conn.rollback()
            return _base_result(
                status="outcome_not_supported",
                item_id=int(item_id),
                proposal_key=proposal_key,
                outcome=outcome,
                queue_status_before=queue_status_before,
                queue_status_after=queue_status_before,
                expected_preview_hash=normalized_hash,
                current_preview_hash=current_preview_hash,
                operator_next_action="review_metadata_proposal",
                blocking_reasons=["metadata_apply_not_supported"],
            )
        if outcome not in SUPPORTED_APPLY_OUTCOMES:
            raise ReconciliationApplyBlocked(f"outcome_not_supported:{outcome or 'missing'}")
        guard = dict(fresh_preview.get("guard") or {})
        planned = dict(fresh_preview.get("planned_future_action") or {})
        if not guard.get("apply_eligible") or not planned.get("apply_supported"):
            raise ReconciliationApplyBlocked("outcome_not_apply_eligible")
        if planned.get("action") != EXPECTED_FUTURE_ACTIONS[outcome]:
            raise ReconciliationApplyBlocked("planned_future_action_mismatch")

        primary_memory_id = planned.get("primary_memory_id")
        primary_memory_id = None if primary_memory_id is None else int(primary_memory_id)
        created_memory_id: int | None = None
        supersession_run_id: int | None = None
        conflict_operation_id: str | None = None
        event_ids: list[int] = []
        link_ids: list[int] = []
        lifecycle_transitions: list[dict[str, Any]] = []

        if outcome in {"duplicate_existing", "reinforce_existing", "create_version", "conflict_review"}:
            if primary_memory_id is None:
                raise ReconciliationApplyBlocked("primary_memory_id_required")
            primary_row = conn.execute("SELECT * FROM memories WHERE id = ?", (primary_memory_id,)).fetchone()
            if primary_row is None:
                raise ReconciliationApplyBlocked("primary_memory_missing")
            primary = row_to_dict(primary_row)
            if normalize_optional_text(primary.get("project_key")) != normalize_optional_text(proposal.get("project_key")):
                raise ReconciliationApplyBlocked("primary_memory_project_mismatch")
            if normalize_optional_text(primary.get("scope_code")) != normalize_optional_text(proposal.get("scope_code")):
                raise ReconciliationApplyBlocked("primary_memory_scope_mismatch")
            primary_workspace_id = None if primary.get("workspace_id") is None else int(primary["workspace_id"])
            if primary_workspace_id != proposal_workspace_id:
                raise ReconciliationApplyBlocked("primary_memory_workspace_mismatch")

        if outcome in {"create_new", "create_version", "conflict_review"}:
            created = _insert_proposal_memory(
                conn,
                proposal=proposal,
                proposal_key=proposal_key,
                applied_at=applied_at,
                insert_memory=insert_memory,
            )
            created_memory_id = int(created["id"])
            created_event = insert_memory_event(
                conn,
                memory_id=created_memory_id,
                event_type="memory_v2.created",
                payload={
                    "source": "memory_v3_capture_apply",
                    "item_id": int(item_id),
                    "proposal_key": proposal_key,
                    "applied_by": normalized_applied_by,
                    "state_code": "validated",
                    "memory_v2_status": "active",
                },
            )
            event_ids.append(int(created_event["id"]))

        if outcome == "create_new":
            capture_event = insert_memory_event(
                conn,
                memory_id=int(created_memory_id),
                event_type="memory_v3.capture_applied",
                payload={
                    "item_id": int(item_id),
                    "proposal_key": proposal_key,
                    "input_fingerprint": input_fingerprint,
                    "applied_by": normalized_applied_by,
                    "preview_hash": normalized_hash,
                    "outcome": outcome,
                },
            )
            event_ids.append(int(capture_event["id"]))
        elif outcome == "duplicate_existing":
            exact_ids = set(fresh_preview.get("evidence", {}).get("exact", {}).get("matched_memory_ids") or [])
            source_ids = set(fresh_preview.get("evidence", {}).get("source", {}).get("matched_memory_ids") or [])
            if primary_memory_id not in exact_ids | source_ids:
                raise ReconciliationApplyBlocked("duplicate_requires_exact_or_source_evidence")
        elif outcome == "reinforce_existing":
            source_ids = set(fresh_preview.get("evidence", {}).get("source", {}).get("matched_memory_ids") or [])
            if primary_memory_id not in source_ids:
                raise ReconciliationApplyBlocked("reinforce_requires_source_evidence")
            reinforcement_event = insert_memory_event(
                conn,
                memory_id=int(primary_memory_id),
                event_type="memory_v3.capture_reinforced",
                payload={
                    "item_id": int(item_id),
                    "proposal_key": proposal_key,
                    "input_fingerprint": input_fingerprint,
                    "source_event_ref": proposal.get("source_event_ref"),
                    "applied_by": normalized_applied_by,
                    "preview_hash": normalized_hash,
                },
            )
            event_ids.append(int(reinforcement_event["id"]))
        elif outcome == "create_version":
            relation_kind = normalize_required_text(planned.get("relation_kind"), "relation_kind")
            reason = normalize_required_text(planned.get("reason"), "reason")
            supersession_preview = preview_memory_supersession_payload(
                conn,
                new_memory_id=int(created_memory_id),
                old_memory_id=int(primary_memory_id),
                relation_kind=relation_kind,
                reason=reason,
                include_debug=True,
                normalize_required_text=normalize_required_text,
                row_to_dict=row_to_dict,
                enrich_memory_dict=enrich_memory_dict,
                canonical_json_hash=canonical_json_hash,
            )
            if supersession_preview.get("status") != "preview_ready":
                raise ReconciliationApplyBlocked("supersession_preview_blocked")
            supersession = apply_memory_supersession_payload(
                conn,
                new_memory_id=int(created_memory_id),
                old_memory_id=int(primary_memory_id),
                relation_kind=relation_kind,
                reason=reason,
                expected_preview_hash=str(supersession_preview["preview_hash"]),
                applied_by=normalized_applied_by,
                notes=normalize_optional_text(notes),
                confirm_protected=bool(confirm_protected),
                include_debug=True,
                manage_transaction=False,
                normalize_required_text=normalize_required_text,
                normalize_optional_text=normalize_optional_text,
                row_to_dict=row_to_dict,
                enrich_memory_dict=enrich_memory_dict,
                canonical_json_hash=canonical_json_hash,
                utc_now_iso=utc_now_iso,
                shift_iso_days=shift_iso_days,
                insert_memory_event=insert_memory_event,
            )
            if supersession.get("status") not in {"applied", "already_applied"}:
                reasons = ",".join(supersession.get("blocking_reasons") or ["supersession_apply_blocked"])
                raise ReconciliationApplyBlocked(reasons)
            supersession_run_id = int(supersession["run_id"])
            debug = dict(supersession.get("debug") or {})
            created_events = dict(debug.get("event_snapshot", {}).get("created_event_ids") or {})
            event_ids.extend(int(value) for value in created_events.values())
            link_snapshot = dict(debug.get("link_snapshot") or {})
            for value in (link_snapshot.get("created_link_id"), link_snapshot.get("reused_link_id")):
                if value is not None:
                    link_ids.append(int(value))
            capture_event = insert_memory_event(
                conn,
                memory_id=int(created_memory_id),
                event_type="memory_v3.capture_applied",
                payload={
                    "item_id": int(item_id),
                    "proposal_key": proposal_key,
                    "input_fingerprint": input_fingerprint,
                    "applied_by": normalized_applied_by,
                    "preview_hash": normalized_hash,
                    "outcome": outcome,
                    "supersession_run_id": supersession_run_id,
                },
            )
            event_ids.append(int(capture_event["id"]))
        elif outcome == "conflict_review":
            conflict = conflict_logic.open_unresolved_conflict_review(
                conn,
                new_memory_id=int(created_memory_id),
                target_memory_id=int(primary_memory_id),
                item_id=int(item_id),
                proposal_key=proposal_key,
                applied_by=normalized_applied_by,
                preview_hash=normalized_hash,
                applied_at=applied_at,
                create_link=create_link,
                insert_memory_event=insert_memory_event,
                record_timeline_event=record_timeline_event,
                new_operation_id=new_operation_id,
            )
            conflict_operation_id = str(conflict["operation_id"])
            event_ids.append(int(conflict["memory_event_id"]))
            event_ids.extend(int(value) for value in conflict.get("transition_event_ids") or [])
            lifecycle_transitions = list(conflict.get("lifecycle_transitions") or [])
            for value in (conflict.get("created_link_id"), conflict.get("reused_link_id")):
                if value is not None:
                    link_ids.append(int(value))

        effect = {
            "schema_version": CAPTURE_RECONCILIATION_APPLY_AUDIT_SCHEMA_VERSION,
            "item_id": int(item_id),
            "proposal_key": proposal_key,
            "expected_preview_hash": normalized_hash,
            "outcome": outcome,
            "created_memory_id": created_memory_id,
            "primary_memory_id": primary_memory_id,
            "event_ids": sorted(set(event_ids)),
            "link_ids": sorted(set(link_ids)),
            "supersession_run_id": supersession_run_id,
            "conflict_operation_id": conflict_operation_id,
            "lifecycle_transitions": lifecycle_transitions,
        }
        apply_audit = {
            **effect,
            "applied_at": applied_at,
            "applied_by": normalized_applied_by,
            "notes": normalize_optional_text(notes),
            "result_fingerprint": canonical_json_hash(effect),
        }
        queue_result = mark_capture_review_item_applied(
            conn,
            item_id=int(item_id),
            expected_preview_hash=normalized_hash,
            outcome=outcome,
            apply_audit=apply_audit,
            created_memory_id=created_memory_id,
            utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text,
            row_to_dict=row_to_dict,
        )
        if queue_result.get("status") != "applied":
            raise ReconciliationApplyBlocked(str(queue_result.get("error") or "queue_apply_audit_failed"))
        conn.commit()
        result = _base_result(
            status="applied",
            item_id=int(item_id),
            proposal_key=proposal_key,
            outcome=outcome,
            queue_status_before=queue_status_before,
            queue_status_after="applied",
            expected_preview_hash=normalized_hash,
            current_preview_hash=current_preview_hash,
            created_memory_id=created_memory_id,
            primary_memory_id=primary_memory_id,
            supersession_run_id=supersession_run_id,
            event_ids=event_ids,
            link_ids=link_ids,
            lifecycle_transitions=lifecycle_transitions,
            apply_audit=apply_audit,
            operator_next_action="none",
            unsupported_metrics=list(fresh_preview.get("unsupported_metrics") or []),
        )
        if include_debug:
            result["debug"] = {
                "fresh_preview": fresh_preview,
                "conflict_operation_id": conflict_operation_id,
            }
        return result
    except ReconciliationApplyBlocked as exc:
        conn.rollback()
        return _base_result(
            status="blocked",
            item_id=int(item_id),
            proposal_key=proposal_key,
            outcome=outcome,
            queue_status_before=queue_status_before,
            queue_status_after=queue_status_before,
            expected_preview_hash=normalized_hash,
            current_preview_hash=current_preview_hash,
            blocking_reasons=[exc.reason],
        )
    except FileNotFoundError as exc:
        conn.rollback()
        return _base_result(
            status="blocked",
            item_id=int(item_id),
            proposal_key=proposal_key,
            outcome=outcome,
            queue_status_before=queue_status_before,
            queue_status_after=queue_status_before,
            expected_preview_hash=normalized_hash,
            current_preview_hash=current_preview_hash,
            blocking_reasons=["capture_review_item_missing"],
            error=str(exc),
        )
    except Exception as exc:
        conn.rollback()
        return _base_result(
            status="error",
            item_id=int(item_id),
            proposal_key=proposal_key,
            outcome=outcome,
            queue_status_before=queue_status_before,
            queue_status_after=queue_status_before,
            expected_preview_hash=normalized_hash,
            current_preview_hash=current_preview_hash,
            blocking_reasons=["atomic_apply_rolled_back"],
            error=str(exc),
        )
