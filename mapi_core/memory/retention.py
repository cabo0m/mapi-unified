from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from mapi_core.memory.lifecycle_contracts import (
    MEMORY_V3_HASH_ALGORITHM,
    derive_canonical_memory_state,
    is_transition_allowed,
)
from mapi_core.memory.sensitivity import classify_memory_sensitivity


RETENTION_POLICY_VERSION = "memory_v3_retention_policy.v2"
RETENTION_PREVIEW_SCHEMA_VERSION = "memory_v3_retention_preview.v1"
RETENTION_PROJECT_PREVIEW_SCHEMA_VERSION = "memory_v3_project_retention_preview.v2"
SUPPORTED_RETENTION_ACTIONS = frozenset({"revalidate", "archive_candidate", "expire_candidate"})

PROTECTED_MEMORY_TYPES = frozenset(
    {
        "identity",
        "relation_note",
        "operator_preference",
        "consolidated_user_profile",
        "project_decision",
        "decision",
        "architecture_decision",
        "project_architecture",
        "project_requirement",
        "project_runbook",
        "project_vision",
        "project_direction",
        "project_milestone",
        "project_checkpoint",
    }
)
TRANSIENT_MARKERS = frozenset(
    {
        "test",
        "pytest",
        "smoke",
        "fixture",
        "diagnostic",
        "synthetic",
        "prompt",
        "prompt-ready",
        "dry-run",
        "debug",
    }
)
LOW_IMPORTANCE_MAX = 0.3499
TEMPORARY_ARCHIVE_AGE_DAYS = 30
OPERATIONAL_ARCHIVE_AGE_DAYS = 60
RECENT_RECALL_PROTECTION_DAYS = 30



def _hash(payload: Any) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _optional_text(value: Any) -> str | None:
    normalized = str(value).strip() if value is not None else ""
    return normalized or None


def _is_due(value: Any, *, as_of: str) -> bool:
    normalized = _optional_text(value)
    return normalized is not None and normalized <= as_of


def _parse_iso(value: Any) -> datetime | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    if len(normalized) == 10:
        normalized = f"{normalized}T00:00:00+00:00"
    elif normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _memory_age_days(memory: dict[str, Any], *, as_of: str) -> int | None:
    created = _parse_iso(memory.get("created_at"))
    reference = _parse_iso(as_of)
    if created is None or reference is None:
        return None
    return max(0, int((reference - created).total_seconds() // 86400))


def _recently_recalled(memory: dict[str, Any], *, as_of: str) -> bool:
    recalled = _parse_iso(memory.get("last_recalled_at"))
    reference = _parse_iso(as_of)
    if recalled is None or reference is None:
        return False
    return (reference - recalled).total_seconds() < RECENT_RECALL_PROTECTION_DAYS * 86400


def _memory_text_tokens(memory: dict[str, Any]) -> set[str]:
    values = (memory.get("title"), memory.get("summary_short"), memory.get("tags"), memory.get("memory_type"), memory.get("entry_type"))
    normalized = " ".join(str(value or "").casefold().replace("_", "-") for value in values)
    tokens = {token.strip(" ,.;:()[]{}") for token in normalized.replace("/", " ").split()}
    return {token for token in tokens if token}


def _has_transient_marker(memory: dict[str, Any]) -> bool:
    tokens = _memory_text_tokens(memory)
    if tokens & TRANSIENT_MARKERS:
        return True
    blob = " ".join(str(memory.get(field) or "").casefold() for field in ("title", "summary_short", "tags"))
    return any(marker in blob for marker in ("smoke test", "prompt active", "prompt ready", "test-only"))


def _is_low_importance(memory: dict[str, Any]) -> bool:
    level = str(memory.get("importance_level") or "").casefold()
    score = float(memory.get("importance_score") or 0.0)
    return level == "low" or score <= LOW_IMPORTANCE_MAX


def _age_archive_reason(memory: dict[str, Any], *, retention_class: str, canonical_state: str, as_of: str) -> str | None:
    if canonical_state not in {"candidate", "validated", "stale"}:
        return None
    if bool(memory.get("requires_user_confirmation")) or _recently_recalled(memory, as_of=as_of):
        return None
    if not _is_low_importance(memory):
        return None
    age_days = _memory_age_days(memory, as_of=as_of)
    if age_days is None:
        return None
    if retention_class in {"temporary", "dream"} and age_days >= TEMPORARY_ARCHIVE_AGE_DAYS:
        return "aged_low_value_transient"
    if retention_class == "operational" and _has_transient_marker(memory) and age_days >= OPERATIONAL_ARCHIVE_AGE_DAYS:
        return "aged_low_value_operational"
    return None


def _safe_memory_snapshot(memory: dict[str, Any]) -> dict[str, Any]:
    content = str(memory.get("content") or "")
    tags = str(memory.get("tags") or "")
    fields = (
        "id",
        "entry_type",
        "memory_type",
        "truth_kind",
        "state_code",
        "memory_v2_status",
        "activity_state",
        "requires_user_confirmation",
        "importance_score",
        "importance_level",
        "identity_weight",
        "emotional_weight",
        "visibility_scope",
        "project_key",
        "scope_code",
        "workspace_id",
        "created_at",
        "updated_at",
        "last_recalled_at",
        "review_due_at",
        "revalidation_due_at",
        "expired_due_at",
        "valid_from",
        "valid_to",
        "supersedes_memory_id",
        "superseded_by_memory_id",
        "contradiction_flag",
        "layer_code",
        "priority",
    )
    snapshot = {field: memory.get(field) for field in fields}
    snapshot.update(
        {
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content_length": len(content),
            "tags_sha256": hashlib.sha256(tags.encode("utf-8")).hexdigest(),
        }
    )
    return snapshot


def _protected_reasons(memory: dict[str, Any], sensitivity_class: str, canonical_state: str) -> list[str]:
    entry_type = str(memory.get("entry_type") or "").casefold()
    memory_type = str(memory.get("memory_type") or "").casefold()
    truth_kind = str(memory.get("truth_kind") or "").casefold()
    importance_level = str(memory.get("importance_level") or "").casefold()
    reasons: list[str] = []
    if entry_type == "core" or str(memory.get("layer_code") or "").casefold() == "core":
        reasons.append("entry_type_core")
    if entry_type == "identity" or memory_type == "identity":
        reasons.append("identity_memory")
    if memory_type in {"relation_note", "relationship", "relation"}:
        reasons.append("relation_memory")
    if memory_type in PROTECTED_MEMORY_TYPES:
        reasons.append(f"protected_memory_type:{memory_type}")
    if float(memory.get("identity_weight") or 0.0) > 0:
        reasons.append("identity_weight_positive")
    if entry_type == "decision" or memory_type in {"decision", "project_decision"} or truth_kind == "decision":
        reasons.append("validated_decision")
    if entry_type in {"preference", "confirmed_preference"} or memory_type in {"preference", "operator_preference"}:
        reasons.append("confirmed_preference")
    if importance_level == "critical" or float(memory.get("importance_score") or 0.0) >= 1.0:
        reasons.append("critical_importance")
    if canonical_state == "conflicted":
        reasons.append("unresolved_conflict")
    if canonical_state == "superseded":
        reasons.append("superseded_provenance_preserved")
    if sensitivity_class in {"personal", "health_sensitive", "financial_sensitive"}:
        reasons.append("sensitive_restricted")
    if sensitivity_class in {"credential_secret", "private_key", "never_store"}:
        reasons.append("never_store_manual_remediation")
    return sorted(set(reasons))


def _retention_class(memory: dict[str, Any], sensitivity_class: str, protected_reasons: list[str]) -> str:
    if sensitivity_class in {"credential_secret", "private_key", "never_store"}:
        return "never_store"
    if sensitivity_class in {"personal", "health_sensitive", "financial_sensitive"}:
        return "sensitive_restricted"
    if protected_reasons:
        return "core_protected"
    entry_type = str(memory.get("entry_type") or "").casefold()
    memory_type = str(memory.get("memory_type") or "").casefold()
    tags = str(memory.get("tags") or "").casefold()
    if "dream" in {entry_type, memory_type} or "dream" in tags:
        return "dream"
    if _has_transient_marker(memory):
        return "temporary"
    if entry_type in {"temporary", "transient"} or memory_type in {"temporary", "transient"}:
        return "temporary"
    if entry_type in {"operational", "task"} or memory_type in {"operational", "task", "project_note"}:
        return "operational"
    return "durable"


def evaluate_retention_policy(memory: dict[str, Any], *, as_of: str) -> dict[str, Any]:
    sensitivity = classify_memory_sensitivity(
        memory.get("content"),
        metadata={
            "tags": memory.get("tags"),
            "visibility_scope": memory.get("visibility_scope"),
            "never_store": memory.get("never_store"),
        },
    )
    try:
        canonical_state = derive_canonical_memory_state(
            state_code=memory.get("state_code"),
            activity_state=memory.get("activity_state"),
            contradiction_flag=memory.get("contradiction_flag"),
        )
    except ValueError:
        canonical_state = "unknown"
    protected_reasons = _protected_reasons(memory, sensitivity["sensitivity_class"], canonical_state)
    retention_class = _retention_class(memory, sensitivity["sensitivity_class"], protected_reasons)
    reason_codes: list[str] = []
    proposed_action: str | None = None
    age_days = _memory_age_days(memory, as_of=as_of)

    if retention_class == "never_store":
        policy_outcome = "blocked_never_store"
        reason_codes.append("restricted_material_requires_manual_remediation")
    elif canonical_state == "validated" and _is_due(memory.get("revalidation_due_at"), as_of=as_of):
        policy_outcome = "revalidate"
        proposed_action = "revalidate"
        reason_codes.append("revalidation_due")
    elif protected_reasons:
        policy_outcome = "protected"
        reason_codes.extend(protected_reasons)
    elif canonical_state == "stale" and _is_due(memory.get("review_due_at"), as_of=as_of):
        policy_outcome = "archive_candidate"
        proposed_action = "archive_candidate"
        reason_codes.append("stale_review_due")
    elif (
        retention_class in {"temporary", "dream", "operational"}
        and canonical_state == "validated"
        and (
            _is_due(memory.get("valid_to"), as_of=as_of)
            or (retention_class in {"temporary", "dream"} and _is_due(memory.get("expired_due_at"), as_of=as_of))
        )
        and is_transition_allowed("validated", "stale")
    ):
        policy_outcome = "expire_candidate"
        proposed_action = "expire_candidate"
        reason_codes.append("explicit_validity_due")
    else:
        age_reason = _age_archive_reason(
            memory,
            retention_class=retention_class,
            canonical_state=canonical_state,
            as_of=as_of,
        )
        if age_reason is not None and is_transition_allowed(canonical_state, "archived"):
            policy_outcome = "archive_candidate"
            proposed_action = "archive_candidate"
            reason_codes.append(age_reason)
        else:
            policy_outcome = "retain"
            reason_codes.append("no_explicit_due_action")

    return {
        "canonical_state": canonical_state,
        "sensitivity": sensitivity,
        "retention_class": retention_class,
        "policy_outcome": policy_outcome,
        "proposed_action": proposed_action,
        "protected_reasons": protected_reasons,
        "reason_codes": sorted(set(reason_codes)),
        "age_days": age_days,
        "recently_recalled": _recently_recalled(memory, as_of=as_of),
    }


def preview_memory_retention_policy_payload(
    conn: Any,
    *,
    memory_id: int,
    as_of: str | None = None,
    include_debug: bool = False,
    row_to_dict: Callable[[Any], dict[str, Any]],
    canonical_json_hash: Callable[[Any], str] | None,
    utc_now_iso: Callable[[], str],
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    if row is None:
        return {
            "status": "not_found",
            "schema_version": RETENTION_PREVIEW_SCHEMA_VERSION,
            "memory_id": int(memory_id),
        }
    memory = row_to_dict(row)
    normalized_as_of = _optional_text(as_of) or utc_now_iso()
    evaluation = evaluate_retention_policy(memory, as_of=normalized_as_of)
    safe_snapshot = _safe_memory_snapshot(memory)
    input_contract = {
        "schema_version": RETENTION_PREVIEW_SCHEMA_VERSION,
        "policy_version": RETENTION_POLICY_VERSION,
        "memory_id": int(memory_id),
        "project_key": memory.get("project_key"),
        "scope_code": memory.get("scope_code"),
        "workspace_id": memory.get("workspace_id"),
        "as_of": normalized_as_of,
        "safe_memory_snapshot": safe_snapshot,
        "canonical_lifecycle": evaluation["canonical_state"],
        "sensitivity_class": evaluation["sensitivity"]["sensitivity_class"],
        "sensitivity_reason_codes": evaluation["sensitivity"]["reason_codes"],
        "retention_class": evaluation["retention_class"],
        "policy_outcome": evaluation["policy_outcome"],
        "proposed_action": evaluation["proposed_action"],
        "protected_reasons": evaluation["protected_reasons"],
        "reason_codes": evaluation["reason_codes"],
        "age_days": evaluation["age_days"],
        "recently_recalled": evaluation["recently_recalled"],
    }
    hash_func = canonical_json_hash or _hash
    input_fingerprint = hash_func({"policy_version": RETENTION_POLICY_VERSION, "safe_memory_snapshot": safe_snapshot})
    preview_hash = hash_func(input_contract)
    apply_eligible = evaluation["proposed_action"] in SUPPORTED_RETENTION_ACTIONS
    payload = {
        "status": "preview_ready",
        "schema_version": RETENTION_PREVIEW_SCHEMA_VERSION,
        "policy_version": RETENTION_POLICY_VERSION,
        "memory_id": int(memory_id),
        "project_key": memory.get("project_key"),
        "scope_code": memory.get("scope_code"),
        "workspace_id": memory.get("workspace_id"),
        "as_of": normalized_as_of,
        "input_fingerprint": input_fingerprint,
        "preview_hash": preview_hash,
        "hash_algorithm": MEMORY_V3_HASH_ALGORITHM,
        "sensitivity": evaluation["sensitivity"],
        "retention_class": evaluation["retention_class"],
        "policy_outcome": evaluation["policy_outcome"],
        "proposed_action": evaluation["proposed_action"],
        "protected_reasons": evaluation["protected_reasons"],
        "reason_codes": evaluation["reason_codes"],
        "age_days": evaluation["age_days"],
        "recently_recalled": evaluation["recently_recalled"],
        "guard": {
            "allowed": True,
            "apply_eligible": apply_eligible,
            "blockers": [] if apply_eligible else [f"policy_outcome_not_actionable:{evaluation['policy_outcome']}"],
            "warnings": [],
        },
        "operator_next_action": "save_retention_review" if apply_eligible else "none",
        "safe_memory_snapshot": safe_snapshot,
        "safety": {
            "read_only": True,
            "raw_secret_exposed": False,
            "physical_purge_supported": False,
        },
    }
    if include_debug:
        payload["debug"] = {
            "policy_version": RETENTION_POLICY_VERSION,
            "rule_trace": list(evaluation["reason_codes"]),
        }
    return payload


def preview_project_memory_retention_payload(
    conn: Any,
    *,
    project_key: str,
    as_of: str | None = None,
    limit: int = 50,
    include_retain: bool = False,
    include_debug: bool = False,
    row_to_dict: Callable[[Any], dict[str, Any]],
    canonical_json_hash: Callable[[Any], str] | None,
    utc_now_iso: Callable[[], str],
) -> dict[str, Any]:
    normalized_project_key = str(project_key or "").strip()
    if not normalized_project_key:
        return {"status": "error", "error": "project_key is required"}
    if limit < 1 or limit > 200:
        return {"status": "error", "error": "limit must be in range 1..200"}
    normalized_as_of = _optional_text(as_of) or utc_now_iso()
    rows = conn.execute(
        "SELECT id FROM memories WHERE project_key = ? ORDER BY id ASC",
        (normalized_project_key,),
    ).fetchall()
    previews = [
        preview_memory_retention_policy_payload(
            conn,
            memory_id=int(row["id"]),
            as_of=normalized_as_of,
            include_debug=include_debug,
            row_to_dict=row_to_dict,
            canonical_json_hash=canonical_json_hash,
            utc_now_iso=utc_now_iso,
        )
        for row in rows
    ]
    enriched: list[dict[str, Any]] = []
    for preview in previews:
        item = dict(preview)
        blockers: list[str] = []
        link_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_links WHERE from_memory_id=? OR to_memory_id=?",
                (int(item["memory_id"]), int(item["memory_id"])),
            ).fetchone()[0]
        )
        if item.get("proposed_action") != "archive_candidate":
            blockers.append("not_archive_candidate")
        if item.get("scope_code") != "project":
            blockers.append("scope_not_project")
        if item.get("protected_reasons"):
            blockers.append("protected_memory")
        if link_count:
            blockers.append("linked_memory")
        snapshot = item.get("safe_memory_snapshot") or {}
        if snapshot.get("supersedes_memory_id") is not None or snapshot.get("superseded_by_memory_id") is not None:
            blockers.append("lifecycle_pointer_present")
        item["canary"] = {
            "eligible": not blockers,
            "blockers": blockers,
            "link_count": link_count,
        }
        enriched.append(item)

    outcome_order = {
        "archive_candidate": 0,
        "expire_candidate": 1,
        "revalidate": 2,
        "protected": 3,
        "blocked_never_store": 4,
        "retain": 5,
    }
    enriched.sort(
        key=lambda item: (
            0 if (item.get("canary") or {}).get("eligible") else 1,
            outcome_order.get(str(item.get("policy_outcome")), 99),
            -int(item.get("age_days") or 0),
            int(item["memory_id"]),
        )
    )
    visible = [item for item in enriched if include_retain or item.get("policy_outcome") != "retain"]
    returned = visible[: int(limit)]
    counts: dict[str, dict[str, int]] = {
        "sensitivity": {},
        "retention_class": {},
        "policy_outcome": {},
    }
    for preview in previews:
        values = {
            "sensitivity": preview["sensitivity"]["sensitivity_class"],
            "retention_class": preview["retention_class"],
            "policy_outcome": preview["policy_outcome"],
        }
        for group, value in values.items():
            counts[group][value] = counts[group].get(value, 0) + 1
    canary_candidates = [
        {
            "memory_id": int(item["memory_id"]),
            "preview_hash": item["preview_hash"],
            "policy_outcome": item["policy_outcome"],
            "proposed_action": item["proposed_action"],
            "retention_class": item["retention_class"],
            "reason_codes": item["reason_codes"],
            "age_days": item.get("age_days"),
            "link_count": (item.get("canary") or {}).get("link_count", 0),
        }
        for item in enriched
        if (item.get("canary") or {}).get("eligible")
    ][:5]
    return {
        "status": "preview_ready",
        "schema_version": RETENTION_PROJECT_PREVIEW_SCHEMA_VERSION,
        "policy_version": RETENTION_POLICY_VERSION,
        "project_key": normalized_project_key,
        "as_of": normalized_as_of,
        "limit": int(limit),
        "include_retain": bool(include_retain),
        "summary": {
            "scanned_count": len(previews),
            "matched_count": len(visible),
            "returned_count": len(returned),
            "truncated": len(visible) > len(returned),
            "protected_count": sum(item["policy_outcome"] == "protected" for item in previews),
            "blocked_never_store_count": sum(item["policy_outcome"] == "blocked_never_store" for item in previews),
            "canary_eligible_count": sum(bool((item.get("canary") or {}).get("eligible")) for item in enriched),
            "counts": counts,
        },
        "source_memory_ids": [int(item["memory_id"]) for item in previews],
        "canary_candidates": canary_candidates,
        "items": returned,
        "safety": {
            "read_only": True,
            "raw_secret_exposed": False,
            "physical_purge_supported": False,
            "full_project_scan": True,
        },
    }
