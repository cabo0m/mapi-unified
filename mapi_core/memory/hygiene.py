from __future__ import annotations

"""Deterministic, reversible memory hygiene policy for Sprint 9.

The policy may change only metadata. It never edits content, summaries, links,
embeddings, lifecycle pointers, or archive state.
"""

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

HYGIENE_POLICY_VERSION = "memory_hygiene_policy.v1"
HYGIENE_PREVIEW_SCHEMA_VERSION = "memory_hygiene_preview.v1"
HYGIENE_APPLY_SCHEMA_VERSION = "memory_hygiene_apply.v1"
HYGIENE_ROLLBACK_SCHEMA_VERSION = "memory_hygiene_rollback.v1"

MUTABLE_METADATA_FIELDS = (
    "scope_code",
    "owner_role",
    "importance_score",
    "importance_level",
    "priority",
    "revalidation_due_at",
)

TARGET_IMPORTANCE_SCORE = {
    "critical": 0.95,
    "high": 0.75,
    "medium": 0.50,
    "low": 0.25,
}

CANONICAL_OWNER_ROLES = frozenset(
    {"project_maintainer", "knowledge_curator", "maintainer", "review_team"}
)
LEGACY_OWNER_ROLE_ALIASES = {
    "operator": "project_maintainer",
    "project": "project_maintainer",
    "project_owner": "project_maintainer",
    "user": "knowledge_curator",
}

GLOBAL_SEMANTIC_AREAS = frozenset({"identity", "relation", "preferences"})
GLOBAL_SEMANTIC_TYPES = frozenset(
    {
        "identity",
        "relation_note",
        "user_introspection",
        "consolidated_user_profile",
        "operator_preference",
        "error_correction",
        "career_profile",
        "business_context",
        "content_strategy_preference",
    }
)

TRANSIENT_MEMORY_TYPES = frozenset(
    {
        "working",
        "session_state",
        "project_task",
        "project_update",
        "experience_sample",
    }
)
MEDIUM_PROJECT_TYPES = frozenset(
    {
        "project_artifact",
        "project_artifact_update",
        "project_context",
        "project_note",
        "project_status",
        "project_research",
        "project_audit",
        "continuity",
        "fact",
        "project",
    }
)
HIGH_PROJECT_TYPES = frozenset(
    {
        "project_decision",
        "project_state",
        "project_checkpoint",
        "project_milestone",
        "project_requirement",
        "project_runbook",
        "project_architecture",
        "project_vision",
        "project_direction",
        "project_concept",
        "project_concept_reconstruction",
        "implementation_checkpoint",
        "deployment_state",
        "operator_preference",
        "identity",
        "relation_note",
        "user_introspection",
        "consolidated_user_profile",
    }
)

NO_RECURRING_REVALIDATION_TYPES = frozenset(
    {
        "project_checkpoint",
        "project_artifact",
        "project_artifact_update",
        "project_milestone",
        "project_update",
        "deployment_state",
        "session_state",
        "project_task",
        "project_note",
        "project_context",
        "project_status",
        "continuity",
        "experience_sample",
        "working",
    }
)
ACTIONABLE_REVALIDATION_TYPES = frozenset(
    {
        "identity",
        "operator_preference",
        "consolidated_user_profile",
        "project_decision",
        "project_requirement",
        "project_architecture",
        "project_runbook",
        "project_vision",
    }
)

TRANSIENT_MARKERS = frozenset(
    {
        "test",
        "pytest",
        "smoke",
        "synthetic",
        "debug",
        "diagnostic",
        "fixture",
        "test-only",
        "prompt-ready",
        "prompt",
    }
)
CURRENT_STATE_MARKERS = frozenset(
    {
        "active",
        "current",
        "pending",
        "decision-ready",
        "operator-pending",
        "source-of-truth",
        "production",
        "deployed",
    }
)
HISTORICAL_STATE_MARKERS = frozenset(
    {
        "completed",
        "accepted",
        "preview-ready",
        "blocked",
        "review",
        "tests",
        "fixed",
        "verified",
        "prompt-ready",
    }
)
CRITICAL_SIGNAL_ELIGIBLE_TYPES = frozenset(
    {
        "identity",
        "operator_preference",
        "consolidated_user_profile",
        "project_decision",
        "project_architecture",
        "project_requirement",
        "project_runbook",
        "project_vision",
        "project_direction",
    }
)

CRITICAL_SIGNAL_TERMS = (
    "source of truth",
    "source-of-truth",
    "single writer",
    "single-writer",
    "no public admin",
    "no-public-admin",
    "authentication",
    "authorization",
    "oauth",
    "bearer token",
    "security",
    "privacy",
    "never-store",
    "data loss",
    "rollback",
    "backup",
    "identity boundary",
    "identity-boundary",
    "guardrail",
    "safety",
    "runtime freshness",
    "freshness enforcement",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).casefold()


def _tags(value: Any) -> set[str]:
    return {part.strip().casefold() for part in _text(value).split(",") if part.strip()}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _memory_blob(memory: Mapping[str, Any]) -> str:
    return " ".join(
        _lower(memory.get(field))
        for field in ("title", "summary_short", "memory_type", "tags", "source_context")
    )


def global_project_scope_is_semantically_allowed(memory: Mapping[str, Any]) -> tuple[bool, list[str]]:
    if "allow-global-project-scope" in _tags(memory.get("tags")):
        return True, ["explicit_allow_global_project_scope"]
    area = _lower(memory.get("area_code"))
    memory_type = _lower(memory.get("memory_type"))
    if area in GLOBAL_SEMANTIC_AREAS:
        return True, ["global_semantic_area"]
    if memory_type in GLOBAL_SEMANTIC_TYPES:
        return True, ["global_semantic_memory_type"]
    return False, []


def recommend_scope(memory: Mapping[str, Any]) -> dict[str, Any]:
    project_key = _text(memory.get("project_key")) or None
    current = _text(memory.get("scope_code")) or None
    if project_key is None:
        return {"value": current, "reason_codes": ["no_project_key"]}
    if current not in {None, "global"}:
        return {"value": current, "reason_codes": ["scope_already_specific"]}
    allowed, reasons = global_project_scope_is_semantically_allowed(memory)
    if current == "global" and allowed:
        return {"value": "global", "reason_codes": reasons}
    return {
        "value": "project",
        "reason_codes": ["missing_or_invalid_project_scope"],
    }


def recommend_owner_role(memory: Mapping[str, Any]) -> dict[str, Any]:
    current = _lower(memory.get("owner_role")) or None
    if current in LEGACY_OWNER_ROLE_ALIASES:
        return {
            "value": LEGACY_OWNER_ROLE_ALIASES[current],
            "reason_codes": ["legacy_owner_role_alias"],
        }
    if current in CANONICAL_OWNER_ROLES:
        return {"value": current, "reason_codes": ["canonical_owner_role_preserved"]}

    scope = _lower(memory.get("scope_code")) or None
    state = _lower(memory.get("state_code")) or "active"
    project_key = _text(memory.get("project_key")) or None
    allowed_global, _ = global_project_scope_is_semantically_allowed(memory)
    if state == "candidate":
        value = "maintainer" if scope == "global" else "review_team"
        return {"value": value, "reason_codes": ["candidate_owner_default"]}
    if scope == "global" and allowed_global:
        return {"value": "knowledge_curator", "reason_codes": ["global_semantic_owner_default"]}
    if project_key:
        return {"value": "project_maintainer", "reason_codes": ["project_owner_default"]}
    if scope == "global":
        return {"value": "knowledge_curator", "reason_codes": ["global_owner_default"]}
    return {"value": "review_team", "reason_codes": ["review_owner_default"]}


def _has_critical_signal(memory: Mapping[str, Any]) -> bool:
    blob = _memory_blob(memory)
    return any(term in blob for term in CRITICAL_SIGNAL_TERMS)


def _is_transient(memory: Mapping[str, Any]) -> bool:
    memory_type = _lower(memory.get("memory_type"))
    if memory_type in TRANSIENT_MEMORY_TYPES:
        return True
    tags = _tags(memory.get("tags"))
    if tags & TRANSIENT_MARKERS:
        return True
    title = _lower(memory.get("title"))
    if (
        "prompt active" in title
        or title.endswith(" prompt ready")
        or title.startswith("test ")
        or title.endswith(" test")
        or " smoke test" in title
    ):
        return True
    return False


def _is_protected_critical(memory: Mapping[str, Any]) -> bool:
    memory_type = _lower(memory.get("memory_type"))
    layer = _lower(memory.get("layer_code"))
    area = _lower(memory.get("area_code"))
    if memory_type == "identity":
        return True
    if layer == "core" and area in {"identity", "relation", "preferences"}:
        return True
    if layer == "core" and memory_type in {"project_decision", "project_architecture"}:
        return True
    if (
        memory_type in CRITICAL_SIGNAL_ELIGIBLE_TYPES
        and _has_critical_signal(memory)
        and (
            _text(memory.get("source_context"))
            or _text(memory.get("source_event_ref"))
            or layer == "core"
        )
    ):
        return True
    return False


def recommend_importance(memory: Mapping[str, Any]) -> dict[str, Any]:
    memory_type = _lower(memory.get("memory_type"))
    layer = _lower(memory.get("layer_code"))
    area = _lower(memory.get("area_code"))
    reason_codes: list[str]
    if _is_protected_critical(memory):
        level = "critical"
        reason_codes = ["protected_critical_signal"]
    elif memory_type == "project_decision" or layer == "core":
        level = "high"
        reason_codes = ["protected_high_value_memory"]
    elif _is_transient(memory):
        level = "low"
        reason_codes = ["transient_or_test_memory"]
    elif memory_type in HIGH_PROJECT_TYPES or area in GLOBAL_SEMANTIC_AREAS:
        level = "high"
        reason_codes = ["durable_high_value_memory"]
    elif memory_type in MEDIUM_PROJECT_TYPES:
        level = "medium"
        reason_codes = ["normal_project_context"]
    else:
        level = "medium"
        reason_codes = ["default_medium_importance"]
    priority = "critical" if level == "critical" else "high" if level == "high" else "low" if level == "low" else "normal"
    return {
        "level": level,
        "score": TARGET_IMPORTANCE_SCORE[level],
        "priority": priority,
        "reason_codes": reason_codes,
    }


LEVEL_SCORE_BOUNDS = {
    "low": (0.0, 0.3499),
    "medium": (0.35, 0.6499),
    "high": (0.65, 0.8499),
    "critical": (0.85, 1.0),
}


def _level_for_score(score: float) -> str:
    if score >= 0.85:
        return "critical"
    if score >= 0.65:
        return "high"
    if score >= 0.35:
        return "medium"
    return "low"


def _remap_score_to_level(score: float, target_level: str, *, memory_id: int) -> float:
    source_level = _level_for_score(score)
    source_low, source_high = LEVEL_SCORE_BOUNDS[source_level]
    target_low, target_high = LEVEL_SCORE_BOUNDS[target_level]
    source_range = max(source_high - source_low, 0.0001)
    percentile = min(1.0, max(0.0, (score - source_low) / source_range))
    # Keep a narrow headroom for an ID-based recency tiebreak. This preserves
    # relative evidence strength while avoiding hundreds of equal scores.
    usable_high = max(target_low, target_high - 0.005)
    remapped = target_low + percentile * (usable_high - target_low)
    remapped += min(max(int(memory_id), 0), 4000) * 0.000001
    return round(min(target_high, max(target_low, remapped)), 4)


def _score_matches_level(score: float, level: str) -> bool:
    if level == "critical":
        return 0.85 <= score <= 1.0
    if level == "high":
        return 0.65 <= score < 0.85
    if level == "medium":
        return 0.35 <= score < 0.65
    return 0.0 <= score < 0.35


def apply_new_write_importance_policy(
    *,
    memory_type: str,
    requested_score: float,
    project_key: str | None,
    scope_code: str | None,
    tags: str | None,
    title: str | None,
    summary_short: str | None,
    entry_type: str | None,
    truth_kind: str | None,
    source_context: str | None,
    source_event_ref: str | None,
    layer_code: str | None = None,
    area_code: str | None = None,
) -> dict[str, Any]:
    memory = {
        "memory_type": memory_type,
        "project_key": project_key,
        "scope_code": scope_code,
        "tags": tags,
        "title": title,
        "summary_short": summary_short,
        "entry_type": entry_type,
        "truth_kind": truth_kind,
        "source_context": source_context,
        "source_event_ref": source_event_ref,
        "layer_code": layer_code,
        "area_code": area_code,
    }
    recommendation = recommend_importance(memory)
    requested = max(0.0, min(1.0, float(requested_score)))
    ceiling = recommendation["score"]
    effective = min(requested, ceiling) if recommendation["level"] != "critical" else requested
    if recommendation["level"] == "critical":
        effective = max(effective, TARGET_IMPORTANCE_SCORE["critical"])
    effective_level = (
        "critical" if effective >= 0.85 else "high" if effective >= 0.65 else "medium" if effective >= 0.35 else "low"
    )
    return {
        "schema_version": HYGIENE_POLICY_VERSION,
        "requested_score": requested,
        "effective_score": round(effective, 4),
        "effective_level": effective_level,
        "recommended_level": recommendation["level"],
        "capped": effective < requested,
        "reason_codes": recommendation["reason_codes"] + (["requested_score_capped"] if effective < requested else []),
    }


def should_schedule_revalidation(*, memory_type: str | None) -> bool:
    normalized = _lower(memory_type)
    if normalized in NO_RECURRING_REVALIDATION_TYPES:
        return False
    return normalized in ACTIONABLE_REVALIDATION_TYPES or normalized == "project_state"


def classify_revalidation(memory: Mapping[str, Any], *, as_of: str) -> dict[str, Any]:
    due_at = _text(memory.get("revalidation_due_at")) or None
    state = _lower(memory.get("state_code"))
    memory_type = _lower(memory.get("memory_type"))
    layer = _lower(memory.get("layer_code"))
    area = _lower(memory.get("area_code"))
    tags = _tags(memory.get("tags"))
    blob = _memory_blob(memory)
    overdue = due_at is not None and due_at <= as_of
    if due_at is None:
        return {"category": "not_scheduled", "overdue": False, "clear_due_at": False, "reason_codes": ["no_due_date"]}
    if state in {"archived", "superseded"}:
        return {"category": "historical", "overdue": overdue, "clear_due_at": True, "reason_codes": ["historical_lifecycle_state"]}
    if memory_type in NO_RECURRING_REVALIDATION_TYPES or _is_transient(memory):
        return {"category": "historical", "overdue": overdue, "clear_due_at": True, "reason_codes": ["immutable_or_transient_record"]}
    if memory_type == "project_state":
        if tags & CURRENT_STATE_MARKERS:
            return {"category": "actionable", "overdue": overdue, "clear_due_at": False, "reason_codes": ["active_project_state"]}
        if tags & HISTORICAL_STATE_MARKERS or any(marker in blob for marker in HISTORICAL_STATE_MARKERS):
            return {"category": "historical", "overdue": overdue, "clear_due_at": True, "reason_codes": ["historical_project_state"]}
        return {"category": "actionable", "overdue": overdue, "clear_due_at": False, "reason_codes": ["unclassified_project_state_requires_review"]}
    if layer == "core" or area in GLOBAL_SEMANTIC_AREAS or memory_type in ACTIONABLE_REVALIDATION_TYPES:
        return {"category": "actionable", "overdue": overdue, "clear_due_at": False, "reason_codes": ["durable_state_requires_revalidation"]}
    return {"category": "historical", "overdue": overdue, "clear_due_at": True, "reason_codes": ["non_recurring_context"]}


def _distribution(items: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in items:
        value = _text(item.get(field)) or "<null>"
        counts[value] += 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def _candidate_snapshot(memory: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {field: memory.get(field) for field in fields}


def build_hygiene_preview(conn: Any, *, project_key: str, as_of: str | None = None) -> dict[str, Any]:
    normalized_project = _text(project_key)
    if not normalized_project:
        raise ValueError("project_key is required")
    normalized_as_of = _text(as_of) or _utc_now_iso()
    rows = conn.execute(
        """
        SELECT * FROM memories
        WHERE project_key = ? AND archived_at IS NULL
        ORDER BY id ASC
        """,
        (normalized_project,),
    ).fetchall()
    memories = [dict(row) for row in rows]
    candidates: list[dict[str, Any]] = []
    field_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    revalidation_counts: Counter[str] = Counter()
    simulated: list[dict[str, Any]] = []

    for raw in memories:
        memory = dict(raw)
        changes: dict[str, Any] = {}
        reasons: list[str] = []

        scope = recommend_scope(memory)
        effective_memory = dict(memory)
        effective_memory["scope_code"] = scope["value"]
        if scope["value"] != memory.get("scope_code"):
            changes["scope_code"] = scope["value"]
            reasons.extend(scope["reason_codes"])

        owner = recommend_owner_role(effective_memory)
        if owner["value"] != memory.get("owner_role"):
            changes["owner_role"] = owner["value"]
            reasons.extend(owner["reason_codes"])

        importance = recommend_importance(effective_memory)
        current_score = round(float(memory.get("importance_score") or 0.0), 4)
        current_level = _lower(memory.get("importance_level"))
        level_changes = current_level != importance["level"]
        if level_changes:
            changes["importance_level"] = importance["level"]
        if level_changes:
            changes["importance_score"] = _remap_score_to_level(
                current_score,
                importance["level"],
                memory_id=int(memory.get("id") or 0),
            )
        elif not _score_matches_level(current_score, importance["level"]):
            changes["importance_score"] = float(importance["score"])
        if _lower(memory.get("priority")) != importance["priority"]:
            changes["priority"] = importance["priority"]
        if any(field in changes for field in ("importance_level", "importance_score", "priority")):
            reasons.extend(importance["reason_codes"])

        revalidation = classify_revalidation(effective_memory, as_of=normalized_as_of)
        revalidation_counts[revalidation["category"]] += 1
        if revalidation["clear_due_at"] and memory.get("revalidation_due_at") is not None:
            changes["revalidation_due_at"] = None
            reasons.extend(revalidation["reason_codes"])

        simulated_memory = dict(memory)
        simulated_memory.update(changes)
        simulated.append(simulated_memory)

        if not changes:
            continue
        for field in changes:
            field_counts[field] += 1
        for reason in sorted(set(reasons)):
            reason_counts[reason] += 1
        candidates.append(
            {
                "memory_id": int(memory["id"]),
                "memory_type": memory.get("memory_type"),
                "title": memory.get("title"),
                "old": _candidate_snapshot(memory, tuple(changes)),
                "new": dict(changes),
                "reason_codes": sorted(set(reasons)),
            }
        )

    sentinel_findings: list[dict[str, Any]] = []
    for candidate in candidates:
        memory = next(item for item in memories if int(item["id"]) == int(candidate["memory_id"]))
        new_level = candidate["new"].get("importance_level", memory.get("importance_level"))
        if _lower(memory.get("layer_code")) == "core" and new_level == "low":
            sentinel_findings.append({"memory_id": candidate["memory_id"], "reason": "core_downgraded_to_low"})
        if _lower(memory.get("memory_type")) == "identity" and new_level not in {"critical", "high"}:
            sentinel_findings.append({"memory_id": candidate["memory_id"], "reason": "identity_below_high"})
        if _lower(memory.get("memory_type")) == "project_decision" and new_level not in {"critical", "high"}:
            sentinel_findings.append({"memory_id": candidate["memory_id"], "reason": "project_decision_below_high"})
        if set(candidate["new"]) - set(MUTABLE_METADATA_FIELDS):
            sentinel_findings.append({"memory_id": candidate["memory_id"], "reason": "forbidden_field_change"})

    candidate_core = [
        {"memory_id": item["memory_id"], "old": item["old"], "new": item["new"], "reason_codes": item["reason_codes"]}
        for item in candidates
    ]
    candidate_set_fingerprint = _fingerprint(candidate_core)
    preview_core = {
        "schema_version": HYGIENE_PREVIEW_SCHEMA_VERSION,
        "policy_version": HYGIENE_POLICY_VERSION,
        "project_key": normalized_project,
        "as_of": normalized_as_of,
        "candidate_set_fingerprint": candidate_set_fingerprint,
        "candidates": candidate_core,
    }
    preview_hash = _fingerprint(preview_core)
    actionable_revalidation = sum(
        1 for memory in memories if classify_revalidation(memory, as_of=normalized_as_of)["category"] == "actionable"
    )
    overdue_actionable = sum(
        1
        for memory in memories
        if (
            classify_revalidation(memory, as_of=normalized_as_of)["category"] == "actionable"
            and classify_revalidation(memory, as_of=normalized_as_of)["overdue"]
        )
    )
    return {
        **preview_core,
        "preview_hash": preview_hash,
        "status": "preview_ready" if not sentinel_findings else "blocked",
        "memory_count": len(memories),
        "candidate_count": len(candidates),
        "field_change_counts": dict(sorted(field_counts.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
        "revalidation_classification": dict(sorted(revalidation_counts.items())),
        "actionable_revalidation_count": actionable_revalidation,
        "overdue_actionable_revalidation_count": overdue_actionable,
        "before_distributions": {
            "importance_level": _distribution(memories, "importance_level"),
            "priority": _distribution(memories, "priority"),
            "owner_role": _distribution(memories, "owner_role"),
            "scope_code": _distribution(memories, "scope_code"),
        },
        "after_distributions": {
            "importance_level": _distribution(simulated, "importance_level"),
            "priority": _distribution(simulated, "priority"),
            "owner_role": _distribution(simulated, "owner_role"),
            "scope_code": _distribution(simulated, "scope_code"),
        },
        "sentinel_findings": sentinel_findings,
        "apply_allowed": not sentinel_findings and len(candidates) <= 1000,
        "candidates": candidates,
    }


def hygiene_inventory(conn: Any, *, project_key: str, as_of: str | None = None, include_candidates: bool = False) -> dict[str, Any]:
    preview = build_hygiene_preview(conn, project_key=project_key, as_of=as_of)
    if not include_candidates:
        preview = {key: value for key, value in preview.items() if key != "candidates"}
    return {"status": preview["status"], "schema_version": "memory_hygiene_inventory.v1", "preview": preview}


def _require_backup(backup_path: str) -> str:
    path = Path(_text(backup_path))
    if not path.is_file():
        raise ValueError("verified backup_path is required")
    return str(path.resolve())


def apply_hygiene_preview(
    conn: Any,
    *,
    project_key: str,
    expected_preview_hash: str,
    applied_by: str,
    reason: str,
    backup_path: str,
    confirm_metadata_repair: bool,
    as_of: str | None = None,
) -> dict[str, Any]:
    if not confirm_metadata_repair:
        raise ValueError("confirm_metadata_repair=true is required")
    normalized_backup = _require_backup(backup_path)
    preview = build_hygiene_preview(conn, project_key=project_key, as_of=as_of)
    if preview["status"] != "preview_ready" or not preview["apply_allowed"]:
        raise ValueError("hygiene preview is not applyable")
    if _text(expected_preview_hash) != preview["preview_hash"]:
        raise ValueError("expected_preview_hash mismatch")
    existing = conn.execute(
        "SELECT * FROM memory_hygiene_runs WHERE preview_hash=? AND status IN ('completed','rolled_back') ORDER BY id DESC LIMIT 1",
        (preview["preview_hash"],),
    ).fetchone()
    if existing is not None:
        return {"status": "existing_result", "run_id": int(existing["id"]), "preview_hash": preview["preview_hash"]}

    now = _utc_now_iso()
    cursor = conn.execute(
        """
        INSERT INTO memory_hygiene_runs (
            policy_version, project_key, status, preview_hash,
            candidate_set_fingerprint, candidate_count, changed_count,
            applied_by, reason, backup_path, preview_json,
            started_at, created_at, updated_at
        ) VALUES (?, ?, 'running', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            HYGIENE_POLICY_VERSION,
            _text(project_key),
            preview["preview_hash"],
            preview["candidate_set_fingerprint"],
            int(preview["candidate_count"]),
            _text(applied_by),
            _text(reason),
            normalized_backup,
            _canonical_json(preview),
            now,
            now,
            now,
        ),
    )
    run_id = int(cursor.lastrowid)
    changed = 0
    for candidate in preview["candidates"]:
        memory_id = int(candidate["memory_id"])
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            raise ValueError(f"memory {memory_id} disappeared")
        current = dict(row)
        for field, old_value in candidate["old"].items():
            if current.get(field) != old_value:
                raise ValueError(f"memory {memory_id} drifted on {field}")
        old_snapshot = {**candidate["old"], "updated_at": current.get("updated_at")}
        new_snapshot = {**candidate["new"], "updated_at": now}
        conn.execute(
            """
            INSERT INTO memory_hygiene_run_items (
                run_id, memory_id, old_metadata_json, new_metadata_json,
                reason_codes_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                memory_id,
                _canonical_json(old_snapshot),
                _canonical_json(new_snapshot),
                _canonical_json(candidate["reason_codes"]),
                now,
            ),
        )
        assignments = [f"{field}=?" for field in candidate["new"]]
        values = [candidate["new"][field] for field in candidate["new"]]
        assignments.append("updated_at=?")
        values.append(now)
        values.append(memory_id)
        conn.execute(f"UPDATE memories SET {', '.join(assignments)} WHERE id=?", values)
        conn.execute(
            "INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
            (
                memory_id,
                "memory.hygiene.metadata_applied",
                _canonical_json({"run_id": run_id, "changed_fields": sorted(candidate["new"]), "policy_version": HYGIENE_POLICY_VERSION}),
                now,
            ),
        )
        changed += 1
    conn.execute(
        """
        UPDATE memory_hygiene_runs
        SET status='completed', changed_count=?, completed_at=?, updated_at=?
        WHERE id=?
        """,
        (changed, now, now, run_id),
    )
    return {
        "status": "completed",
        "schema_version": HYGIENE_APPLY_SCHEMA_VERSION,
        "run_id": run_id,
        "preview_hash": preview["preview_hash"],
        "changed_count": changed,
        "backup_path": normalized_backup,
    }


def get_hygiene_run(conn: Any, *, run_id: int) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM memory_hygiene_runs WHERE id=?", (int(run_id),)).fetchone()
    if run is None:
        raise ValueError("hygiene run not found")
    items = conn.execute(
        "SELECT * FROM memory_hygiene_run_items WHERE run_id=? ORDER BY memory_id",
        (int(run_id),),
    ).fetchall()
    return {"status": "ok", "run": dict(run), "items": [dict(item) for item in items]}


def preview_hygiene_rollback(conn: Any, *, run_id: int) -> dict[str, Any]:
    payload = get_hygiene_run(conn, run_id=run_id)
    run = payload["run"]
    if run["status"] != "completed":
        raise ValueError("only completed hygiene runs can be rolled back")
    findings: list[dict[str, Any]] = []
    rollback_items: list[dict[str, Any]] = []
    for item in payload["items"]:
        memory_id = int(item["memory_id"])
        current_row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if current_row is None:
            findings.append({"memory_id": memory_id, "reason": "memory_missing"})
            continue
        current = dict(current_row)
        old = json.loads(item["old_metadata_json"])
        new = json.loads(item["new_metadata_json"])
        drift = [field for field, value in new.items() if current.get(field) != value]
        if drift:
            findings.append({"memory_id": memory_id, "reason": "post_apply_drift", "fields": drift})
        rollback_items.append({"memory_id": memory_id, "old": old, "new": new})
    core = {
        "schema_version": HYGIENE_ROLLBACK_SCHEMA_VERSION,
        "run_id": int(run_id),
        "items": rollback_items,
    }
    return {
        **core,
        "status": "rollback_ready" if not findings else "blocked",
        "rollback_preview_hash": _fingerprint(core),
        "findings": findings,
        "item_count": len(rollback_items),
    }


def rollback_hygiene_run(
    conn: Any,
    *,
    run_id: int,
    expected_rollback_preview_hash: str,
    rolled_back_by: str,
    notes: str | None = None,
) -> dict[str, Any]:
    preview = preview_hygiene_rollback(conn, run_id=run_id)
    if preview["status"] != "rollback_ready":
        raise ValueError("rollback preview is blocked")
    if _text(expected_rollback_preview_hash) != preview["rollback_preview_hash"]:
        raise ValueError("expected_rollback_preview_hash mismatch")
    now = _utc_now_iso()
    for item in preview["items"]:
        memory_id = int(item["memory_id"])
        old = dict(item["old"])
        assignments = [f"{field}=?" for field in old]
        values = [old[field] for field in old]
        values.append(memory_id)
        conn.execute(f"UPDATE memories SET {', '.join(assignments)} WHERE id=?", values)
        conn.execute(
            "INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
            (
                memory_id,
                "memory.hygiene.metadata_rolled_back",
                _canonical_json({"run_id": int(run_id), "rolled_back_by": _text(rolled_back_by)}),
                now,
            ),
        )
    conn.execute(
        """
        UPDATE memory_hygiene_runs
        SET status='rolled_back', rolled_back_at=?, rolled_back_by=?,
            rollback_note=?, rollback_preview_hash=?, updated_at=?
        WHERE id=?
        """,
        (
            now,
            _text(rolled_back_by),
            _text(notes) or None,
            preview["rollback_preview_hash"],
            now,
            int(run_id),
        ),
    )
    return {
        "status": "rolled_back",
        "schema_version": HYGIENE_ROLLBACK_SCHEMA_VERSION,
        "run_id": int(run_id),
        "restored_count": int(preview["item_count"]),
        "rollback_preview_hash": preview["rollback_preview_hash"],
    }
