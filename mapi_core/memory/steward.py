from __future__ import annotations

"""Read-only Memory Steward lifecycle contracts.

Steward never creates or mutates memories itself. It composes existing read-only
surfaces and, for after-action/session-close phases, returns an explicit route to
an existing durable capture-review action.
"""

import hashlib
import json
from typing import Any, Iterable, Mapping


MEMORY_STEWARD_SCHEMA = "memory_steward.v1"
MEMORY_STEWARD_NIGHTLY_SCHEMA = "memory_steward_nightly.v1"


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _source_ids(values: Iterable[Any]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            memory_id = int(value)
        except (TypeError, ValueError):
            continue
        if memory_id <= 0 or memory_id in seen:
            continue
        seen.add(memory_id)
        out.append(memory_id)
    return out


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def before_action_payload(*, context: Mapping[str, Any]) -> dict[str, Any]:
    if context.get("status") != "ok":
        return {
            "status": "blocked",
            "schema": MEMORY_STEWARD_SCHEMA,
            "phase": "before_action",
            "reason": "context_not_ready",
            "context_status": context.get("status"),
        }
    payload = {
        "status": "ready",
        "schema": MEMORY_STEWARD_SCHEMA,
        "phase": "before_action",
        "project_key": context.get("project_key"),
        "requested_project_key": context.get("requested_project_key"),
        "context": dict(context),
        "source_memory_ids": _source_ids(context.get("source_memory_ids") or []),
        "next_route": None,
        "safety": {
            "read_only": True,
            "memory_mutations_performed": 0,
            "capture_items_created": 0,
            "model_calls": False,
        },
    }
    payload["steward_fingerprint"] = _fingerprint(
        {
            "phase": payload["phase"],
            "project_key": payload["project_key"],
            "context_fingerprint": context.get("context_fingerprint"),
            "source_memory_ids": payload["source_memory_ids"],
        }
    )
    return payload


def after_action_content(
    *,
    action_summary: str,
    outcome_summary: str,
    durable_delta: str,
    source_event_ref: str | None,
) -> str:
    lines = [
        f"Action: {_text(action_summary)}",
        f"Outcome: {_text(outcome_summary)}",
        f"Durable delta: {_text(durable_delta)}",
    ]
    if _text(source_event_ref):
        lines.append(f"Evidence event: {_text(source_event_ref)}")
    return "\n".join(lines)


def session_close_content(
    *,
    completed_summary: str,
    open_items_summary: str,
    next_step: str,
    source_event_ref: str | None,
) -> str:
    lines = [
        f"Completed: {_text(completed_summary)}",
        f"Open items: {_text(open_items_summary)}",
        f"Next step: {_text(next_step)}",
    ]
    if _text(source_event_ref):
        lines.append(f"Evidence event: {_text(source_event_ref)}")
    return "\n".join(lines)


def capture_phase_payload(
    *,
    phase: str,
    capture_proposal: Mapping[str, Any],
    requested_project_key: str,
    canonical_project_key: str,
    content: str,
    source_context: str | None,
    conversation_key: str | None,
    source_event_ref: str | None,
    source_memory_ids: Iterable[Any],
    hint: str,
) -> dict[str, Any]:
    evidence_ids = _source_ids(source_memory_ids)
    has_evidence = bool(evidence_ids or _text(source_event_ref) or (phase == "session_close" and _text(conversation_key)))
    if not has_evidence:
        return {
            "status": "blocked",
            "schema": MEMORY_STEWARD_SCHEMA,
            "phase": phase,
            "reason": "source_evidence_required",
            "safety": {"read_only": True, "memory_mutations_performed": 0, "capture_items_created": 0},
        }
    if capture_proposal.get("status") != "proposed":
        return {
            "status": "blocked",
            "schema": MEMORY_STEWARD_SCHEMA,
            "phase": phase,
            "reason": "capture_proposal_not_ready",
            "capture": dict(capture_proposal),
            "source_memory_ids": evidence_ids,
            "source_event_ref": _text(source_event_ref) or None,
            "safety": {"read_only": True, "memory_mutations_performed": 0, "capture_items_created": 0},
        }

    route_payload = {
        "content": content,
        "project_key": canonical_project_key,
        "scope_code": "project",
        "source_context": _text(source_context) or None,
        "conversation_key": _text(conversation_key) or None,
        "source_event_ref": _text(source_event_ref) or None,
        "hint": hint,
    }
    payload = {
        "status": "proposal_ready",
        "schema": MEMORY_STEWARD_SCHEMA,
        "phase": phase,
        "requested_project_key": requested_project_key,
        "project_key": canonical_project_key,
        "capture": dict(capture_proposal),
        "source_memory_ids": evidence_ids,
        "source_event_ref": _text(source_event_ref) or None,
        "review_route": {
            "area": "memory",
            "action": "capture_save",
            "payload": route_payload,
            "note": "Explicit follow-up only. Steward does not execute this route automatically.",
        },
        "safety": {
            "read_only": True,
            "memory_mutations_performed": 0,
            "capture_items_created": 0,
            "review_required_before_memory_creation": True,
            "auto_apply": False,
        },
    }
    payload["steward_fingerprint"] = _fingerprint(
        {
            "phase": phase,
            "project_key": canonical_project_key,
            "capture_input_fingerprint": capture_proposal.get("proposal", {}).get("input_fingerprint")
            or capture_proposal.get("input_fingerprint"),
            "source_memory_ids": evidence_ids,
            "source_event_ref": payload["source_event_ref"],
            "review_route": route_payload,
        }
    )
    return payload


def nightly_payload(
    *,
    project_key: str,
    sandman: Mapping[str, Any],
    retention: Mapping[str, Any],
    revalidation: Mapping[str, Any],
    capture_queue: Mapping[str, Any],
    consolidation_queue: Mapping[str, Any],
    candidate_limit: int,
) -> dict[str, Any]:
    retention_items = list(retention.get("items") or [])[:candidate_limit]
    revalidation_items = list(revalidation.get("items") or [])[:candidate_limit]
    capture_items = list(capture_queue.get("items") or [])[:candidate_limit]
    consolidation_items = list(consolidation_queue.get("items") or consolidation_queue.get("proposals") or [])[:candidate_limit]

    retention_candidates = [
        {
            "memory_id": int(item["memory_id"]),
            "policy_outcome": item.get("policy_outcome"),
            "proposed_action": item.get("proposed_action"),
            "apply_eligible": bool((item.get("guard") or {}).get("apply_eligible")),
            "blockers": list((item.get("guard") or {}).get("blockers") or []),
        }
        for item in retention_items
        if item.get("memory_id") is not None
    ]
    revalidation_candidates = [
        {
            "memory_id": int(item["id"]),
            "revalidation_due_at": item.get("revalidation_due_at"),
            "last_validated_at": item.get("last_validated_at"),
        }
        for item in revalidation_items
        if item.get("id") is not None
    ]
    capture_candidates = [
        {
            "item_id": int(item["id"]),
            "status": item.get("status"),
            "proposal_key": item.get("proposal_key"),
        }
        for item in capture_items
        if item.get("id") is not None
    ]
    consolidation_candidates = [
        {
            "proposal_id": int(item.get("proposal_id") or item.get("id")),
            "status": item.get("status"),
            "proposal_type": item.get("proposal_type"),
        }
        for item in consolidation_items
        if item.get("proposal_id") is not None or item.get("id") is not None
    ]
    sandman_source_ids = _source_ids(
        list(sandman.get("source_memory_ids") or []) + list(sandman.get("candidate_memory_ids") or [])
    )
    source_memory_ids = sorted(
        {
            *sandman_source_ids,
            *[item["memory_id"] for item in retention_candidates],
            *[item["memory_id"] for item in revalidation_candidates],
        }
    )
    payload = {
        "status": "preview_ready",
        "schema": MEMORY_STEWARD_NIGHTLY_SCHEMA,
        "project_key": project_key,
        "candidate_limit": int(candidate_limit),
        "sandman": {
            "status": sandman.get("status"),
            "schema": sandman.get("schema") or sandman.get("schema_version"),
            "proposal_count": len(sandman.get("proposals") or []),
            "reason_codes": list(sandman.get("reason_codes") or []),
            "candidate_memory_ids": _source_ids(sandman.get("candidate_memory_ids") or []),
            "source_memory_ids": _source_ids(
                list(sandman.get("source_memory_ids") or []) + list(sandman.get("candidate_memory_ids") or [])
            ),
        },
        "retention": {
            "status": retention.get("status"),
            "summary": dict(retention.get("summary") or {}),
            "candidates": retention_candidates,
        },
        "revalidation": {
            "count": int(revalidation.get("count") or 0),
            "candidates": revalidation_candidates,
        },
        "capture_review": {
            "returned_count": len(capture_candidates),
            "source_total_returned": int((capture_queue.get("summary") or {}).get("total_returned") or len(capture_candidates)),
            "candidates": capture_candidates,
        },
        "consolidation_review": {
            "returned_count": len(consolidation_candidates),
            "source_total_returned": int((consolidation_queue.get("summary") or {}).get("total_returned") or len(consolidation_candidates)),
            "candidates": consolidation_candidates,
        },
        "source_memory_ids": source_memory_ids,
        "routes": {
            "capture_review": "memory/capture_list -> capture_review_decide -> capture_apply",
            "consolidation_review": "memory/consolidation_queue -> consolidation_approve -> consolidation_apply_preview -> consolidation_apply",
            "retention_review": "memory/retention_project_preview -> retention_review_save -> retention_review_decide -> retention_apply",
            "sandman": "sandman/canonical_preview (proposal-only); no auto-apply",
        },
        "safety": {
            "read_only": True,
            "memory_mutations_performed": 0,
            "capture_items_created": 0,
            "auto_apply": False,
            "backup_required_before_mutating_apply": True,
            "rollback_required_for_mutating_apply": True,
        },
    }
    payload["steward_fingerprint"] = _fingerprint(
        {
            "schema": payload["schema"],
            "project_key": project_key,
            "retention": payload["retention"],
            "revalidation": payload["revalidation"],
            "capture_review": payload["capture_review"],
            "consolidation_review": payload["consolidation_review"],
            "sandman": payload["sandman"],
        }
    )
    return payload
