from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from mapi_core.memory.current_state import resolve_current_memory_state
from mapi_core.onboarding import persisted_agent_name

AGENT_SELF_SNAPSHOT_SCHEMA = "mapi_agent_self_snapshot.v1"
AGENT_COMMITMENT_LEDGER_SCHEMA = "mapi_agent_commitment_ledger.v1"
AGENT_AUTOBIOGRAPHICAL_TIMELINE_SCHEMA = "mapi_agent_autobiographical_timeline.v1"
AGENT_SELF_CAPSULE_SCHEMA = "mapi_agent_self_capsule.v1"

_SELF_LAYERS = frozenset({"core", "identity", "autobio"})
_SELF_AREAS = frozenset({"identity", "preferences", "relation", "history", "meta"})
_SELF_TYPES = frozenset({"identity", "preference", "relation_note", "commitment", "guardrail", "self_model", "autobiographical_memory"})
_SELF_TAGS = frozenset({"agent-self", "self-model", "self-evidence", "identity", "autobiographical-memory"})
_COMMITMENT_TAGS = frozenset({"commitment", "promise", "guardrail", "invariant", "security", "safety", "policy"})
_COMMITMENT_TYPES = frozenset({"commitment", "guardrail", "decision", "policy"})
_TIMELINE_TAGS = frozenset({"milestone", "autobiographical-memory", "history", "checkpoint", "self-event"})
_HISTORICAL_STATES = frozenset({"archived", "superseded", "expired", "rejected", "cancelled"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return _text(value).casefold()


def _tags(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = str(value or "").replace(";", ",").split(",")
    return {_norm(item) for item in raw if _text(item)}


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _active(item: dict[str, Any]) -> bool:
    if item.get("archived_at") is not None:
        return False
    state = _norm(item.get("state_code") or item.get("memory_v2_status") or item.get("activity_state"))
    return state not in _HISTORICAL_STATES


@dataclass(frozen=True)
class AgentIdentity:
    subject_key: str
    display_name: str
    project_key: str


def resolve_agent_identity(*, subject_key: str | None = None, display_name: str | None = None, project_key: str | None = None) -> AgentIdentity:
    subject = _text(subject_key) or _text(os.getenv("MAPI_AGENT_SUBJECT_KEY")) or _text(os.getenv("MAPI_OWNER_KEY")) or "agent"
    name = _text(display_name) or _text(os.getenv("MAPI_AGENT_DISPLAY_NAME")) or subject
    project = _text(project_key) or _text(os.getenv("MAPI_AGENT_PROJECT_KEY")) or "agent-self"
    if len(subject) > 128 or len(project) > 200 or len(name) > 200:
        raise ValueError("agent_identity_value_too_long")
    return AgentIdentity(subject_key=subject, display_name=name, project_key=project)


def _explicit_subject_tags(identity: AgentIdentity) -> set[str]:
    subject = _norm(identity.subject_key)
    return {subject, f"subject:{subject}", f"agent:{subject}"}


def _is_self_evidence(item: dict[str, Any], identity: AgentIdentity, *, include_global: bool) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    project = _text(item.get("project_key"))
    scope = _norm(item.get("scope_code"))
    tags = _tags(item.get("tags"))
    layer = _norm(item.get("layer_code"))
    area = _norm(item.get("area_code"))
    memory_type = _norm(item.get("memory_type"))
    entry_type = _norm(item.get("entry_type"))

    if not _active(item):
        return False, ["historical_or_archived"]

    explicit_subject = bool(tags & _explicit_subject_tags(identity))
    generic_self = bool(tags & _SELF_TAGS)
    if _norm(project) == _norm(identity.project_key):
        reasons.append("self_project")
        if explicit_subject:
            reasons.append("explicit_subject_tag")
        if generic_self:
            reasons.append("generic_self_tag")
        if layer in _SELF_LAYERS:
            reasons.append(f"self_layer:{layer}")
        if area in _SELF_AREAS:
            reasons.append(f"self_area:{area}")
        if memory_type in _SELF_TYPES or entry_type in _SELF_TYPES:
            reasons.append("self_type")
        if reasons[1:] or explicit_subject:
            return True, reasons
        return False, ["self_project_without_self_evidence"]

    if include_global and scope == "global" and explicit_subject:
        if float(item.get("identity_weight") or 0.0) >= 0.25 or layer in _SELF_LAYERS or area in _SELF_AREAS:
            return True, ["global_explicit_subject", "strong_identity_signal"]
        return False, ["global_subject_signal_too_weak"]

    return False, ["outside_self_scope"]


def _category(item: dict[str, Any]) -> str:
    tags = _tags(item.get("tags"))
    area = _norm(item.get("area_code"))
    layer = _norm(item.get("layer_code"))
    memory_type = _norm(item.get("memory_type"))
    entry_type = _norm(item.get("entry_type"))
    truth_kind = _norm(item.get("truth_kind"))
    if tags & _COMMITMENT_TAGS or memory_type in _COMMITMENT_TYPES or entry_type in _COMMITMENT_TYPES or truth_kind in {"policy", "commitment"}:
        return "commitments"
    if area == "preferences" or memory_type == "preference":
        return "preferences"
    if area == "relation" or memory_type == "relation_note":
        return "relationships"
    if area == "history" or layer == "autobio" or tags & _TIMELINE_TAGS:
        return "autobiography"
    if area == "meta":
        return "meta"
    return "identity"


def _compact(item: dict[str, Any], *, include_content: bool, reason_codes: list[str] | None = None) -> dict[str, Any]:
    payload = {
        "id": int(item["id"]),
        "title": item.get("title"),
        "summary_short": item.get("summary_short"),
        "memory_type": item.get("memory_type"),
        "entry_type": item.get("entry_type"),
        "truth_kind": item.get("truth_kind"),
        "project_key": item.get("project_key"),
        "scope_code": item.get("scope_code"),
        "layer_code": item.get("layer_code"),
        "area_code": item.get("area_code"),
        "state_code": item.get("state_code"),
        "importance_score": float(item.get("importance_score") or 0.0),
        "confidence_score": float(item.get("confidence_score") or 0.0),
        "identity_weight": float(item.get("identity_weight") or 0.0),
        "tags": item.get("tags"),
        "source": item.get("source"),
        "source_event_ref": item.get("source_event_ref"),
        "conversation_key": item.get("conversation_key"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "supersedes_memory_id": item.get("supersedes_memory_id"),
        "superseded_by_memory_id": item.get("superseded_by_memory_id"),
        "requires_user_confirmation": bool(item.get("requires_user_confirmation")),
        "evidence_reason_codes": list(reason_codes or []),
    }
    if include_content:
        payload["content"] = item.get("content")
        payload["source_context"] = item.get("source_context")
    return payload


def _load_candidates(conn: Any, identity: AgentIdentity, *, include_global: bool, row_to_dict: Callable[[Any], dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], dict[int, list[str]]]:
    safe_limit = max(1, min(int(limit), 1000))
    if include_global:
        rows = conn.execute(
            """
            SELECT * FROM memories
            WHERE archived_at IS NULL AND (project_key=? OR scope_code='global')
            ORDER BY identity_weight DESC, importance_score DESC, confidence_score DESC, id DESC
            LIMIT ?
            """,
            (identity.project_key, safe_limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM memories
            WHERE archived_at IS NULL AND project_key=?
            ORDER BY identity_weight DESC, importance_score DESC, confidence_score DESC, id DESC
            LIMIT ?
            """,
            (identity.project_key, safe_limit),
        ).fetchall()
    selected: list[dict[str, Any]] = []
    reasons: dict[int, list[str]] = {}
    for row in rows:
        item = row_to_dict(row)
        ok, reason_codes = _is_self_evidence(item, identity, include_global=include_global)
        if ok:
            selected.append(item)
            reasons[int(item["id"])] = reason_codes
    if not selected:
        return [], reasons
    current = resolve_current_memory_state(conn, selected, include_history=False)
    current_items = [dict(item) for item in current.get("items") or []]
    return current_items, reasons


def calculate_agent_self_snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    """Fingerprint only evidence-bearing snapshot state, never debug or volatile metadata."""
    material = {
        "schema": AGENT_SELF_SNAPSHOT_SCHEMA,
        "subject": dict(snapshot.get("subject") or {}),
        "sections": dict(snapshot.get("sections") or {}),
        "source_memory_ids": sorted(int(v) for v in snapshot.get("source_memory_ids") or [] if int(v) > 0),
    }
    return _fingerprint(material)


def build_agent_self_snapshot_payload(conn: Any, *, subject_key: str | None, display_name: str | None, project_key: str | None, include_global: bool, limit: int, include_content: bool, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    identity = resolve_agent_identity(
        subject_key=subject_key,
        display_name=display_name or persisted_agent_name(conn),
        project_key=project_key,
    )
    items, reason_map = _load_candidates(conn, identity, include_global=bool(include_global), row_to_dict=row_to_dict, limit=limit)
    sections: dict[str, list[dict[str, Any]]] = {k: [] for k in ("identity", "preferences", "relationships", "commitments", "autobiography", "meta")}
    for item in items:
        sections[_category(item)].append(_compact(item, include_content=include_content, reason_codes=reason_map.get(int(item["id"]))))
    for values in sections.values():
        values.sort(key=lambda item: (-float(item.get("identity_weight") or 0.0), -float(item.get("importance_score") or 0.0), -int(item["id"])))
    source_ids = sorted(int(item["id"]) for item in items)
    gaps = []
    if not sections["identity"]:
        gaps.append("missing_explicit_identity_evidence")
    if not sections["commitments"]:
        gaps.append("missing_explicit_commitment_evidence")
    snapshot_core = {
        "status": "ok",
        "schema": AGENT_SELF_SNAPSHOT_SCHEMA,
        "subject": identity.__dict__,
        "sections": sections,
        "source_memory_ids": source_ids,
        "source_count": len(source_ids),
        "gap_codes": gaps,
    }
    return {
        **snapshot_core,
        "snapshot_fingerprint": calculate_agent_self_snapshot_fingerprint(snapshot_core),
        "safety": {"read_only": True, "model_calls_performed": 0, "semantic_similarity_used_as_identity_evidence": False, "project_scoped": True, "include_global": bool(include_global), "content_included": bool(include_content)},
    }


def _commitment_item(item: dict[str, Any]) -> bool:
    tags = _tags(item.get("tags"))
    return bool(tags & _COMMITMENT_TAGS) or _norm(item.get("memory_type")) in _COMMITMENT_TYPES or _norm(item.get("entry_type")) in _COMMITMENT_TYPES or _norm(item.get("truth_kind")) in {"policy", "commitment"}


def build_agent_commitment_ledger_payload(conn: Any, *, subject_key: str | None, display_name: str | None, project_key: str | None, include_global: bool, limit: int, include_content: bool, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    identity = resolve_agent_identity(
        subject_key=subject_key,
        display_name=display_name or persisted_agent_name(conn),
        project_key=project_key,
    )
    items, reason_map = _load_candidates(conn, identity, include_global=bool(include_global), row_to_dict=row_to_dict, limit=limit)
    commitments = []
    for item in items:
        if not _commitment_item(item):
            continue
        compact = _compact(item, include_content=include_content, reason_codes=reason_map.get(int(item["id"])))
        tags = _tags(item.get("tags"))
        if tags & {"security", "safety", "guardrail", "invariant"}:
            commitment_kind = "behavioral_guardrail"
            action_key = "agent.behavior"
        elif _norm(item.get("truth_kind")) == "decision" or _norm(item.get("entry_type")) == "decision":
            commitment_kind = "project_workflow_rule"
            action_key = "agent.behavior"
        else:
            commitment_kind = "operator_instruction"
            action_key = "agent.behavior"
        compact.update({
            "statement": _text(item.get("summary_short")) or _text(item.get("title")) or (_text(item.get("content")) if include_content else f"Memory #{int(item['id'])} commitment"),
            "status": "active",
            "source_memory_id": int(item["id"]),
            "commitment_kind": commitment_kind,
            "action_key": action_key,
            "polarity": "must",
            "scope": {
                "scope_code": item.get("scope_code"),
                "project_key": None if _text(item.get("project_key")) == identity.project_key else item.get("project_key"),
            },
        })
        commitments.append(compact)
    commitments.sort(key=lambda item: (-float(item.get("importance_score") or 0.0), -float(item.get("confidence_score") or 0.0), -int(item["id"])))
    source_ids = [int(item["id"]) for item in commitments]
    return {
        "status": "ok",
        "schema": AGENT_COMMITMENT_LEDGER_SCHEMA,
        "subject": identity.__dict__,
        "commitments": commitments,
        "count": len(commitments),
        "source_memory_ids": source_ids,
        "ledger_fingerprint": _fingerprint({"subject": identity.__dict__, "ids": source_ids}),
        "safety": {"read_only": True, "explicit_evidence_only": True, "model_calls_performed": 0, "content_included": bool(include_content)},
    }


def build_agent_autobiographical_timeline_payload(conn: Any, *, subject_key: str | None, display_name: str | None, project_key: str | None, include_global: bool, limit: int, include_content: bool, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    identity = resolve_agent_identity(
        subject_key=subject_key,
        display_name=display_name or persisted_agent_name(conn),
        project_key=project_key,
    )
    items, reason_map = _load_candidates(conn, identity, include_global=bool(include_global), row_to_dict=row_to_dict, limit=max(limit, 200))
    timeline_items = []
    for item in items:
        tags = _tags(item.get("tags"))
        category = _category(item)
        if category == "autobiography" or _norm(item.get("layer_code")) == "autobio" or tags & _TIMELINE_TAGS or _commitment_item(item):
            event = _compact(item, include_content=include_content, reason_codes=reason_map.get(int(item["id"])))
            event["event_kind"] = category
            timeline_items.append(event)
    timeline_items.sort(key=lambda item: (_text(item.get("created_at")), int(item["id"])))
    safe_limit = max(1, min(int(limit), 500))
    if len(timeline_items) > safe_limit:
        timeline_items = timeline_items[-safe_limit:]
    source_ids = [int(item["id"]) for item in timeline_items]
    return {
        "status": "ok",
        "schema": AGENT_AUTOBIOGRAPHICAL_TIMELINE_SCHEMA,
        "subject": identity.__dict__,
        "events": timeline_items,
        "count": len(timeline_items),
        "source_memory_ids": source_ids,
        "timeline_fingerprint": _fingerprint({"subject": identity.__dict__, "ids": source_ids}),
        "safety": {"read_only": True, "curated_from_explicit_self_evidence": True, "model_calls_performed": 0, "content_included": bool(include_content)},
    }


def build_agent_self_capsule_payload(conn: Any, *, subject_key: str | None, display_name: str | None, project_key: str | None, include_global: bool, limit: int, include_content: bool, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    snapshot = build_agent_self_snapshot_payload(conn, subject_key=subject_key, display_name=display_name, project_key=project_key, include_global=include_global, limit=max(limit, 200), include_content=include_content, row_to_dict=row_to_dict)
    ledger = build_agent_commitment_ledger_payload(conn, subject_key=subject_key, display_name=display_name, project_key=project_key, include_global=include_global, limit=max(limit, 200), include_content=include_content, row_to_dict=row_to_dict)
    timeline = build_agent_autobiographical_timeline_payload(conn, subject_key=subject_key, display_name=display_name, project_key=project_key, include_global=include_global, limit=min(max(1, int(limit)), 50), include_content=include_content, row_to_dict=row_to_dict)
    identity_items = list(snapshot["sections"]["identity"])
    preference_items = list(snapshot["sections"]["preferences"])
    relationship_items = list(snapshot["sections"]["relationships"])
    commitments = list(ledger["commitments"])
    recent_events = list(timeline["events"])[-10:]
    source_ids = sorted(set(snapshot["source_memory_ids"]) | set(ledger["source_memory_ids"]) | set(timeline["source_memory_ids"]))
    capsule_core = {
        "subject": snapshot["subject"],
        "identity": [item["id"] for item in identity_items[:8]],
        "preferences": [item["id"] for item in preference_items[:8]],
        "relationships": [item["id"] for item in relationship_items[:8]],
        "commitments": [item["id"] for item in commitments[:12]],
        "recent_events": [item["id"] for item in recent_events],
        "source_memory_ids": source_ids,
    }
    return {
        "status": "ok",
        "schema": AGENT_SELF_CAPSULE_SCHEMA,
        "subject": snapshot["subject"],
        "identity": identity_items[:8],
        "preferences": preference_items[:8],
        "relationships": relationship_items[:8],
        "commitments": commitments[:12],
        "recent_autobiographical_events": recent_events,
        "gap_codes": list(snapshot["gap_codes"]),
        "source_memory_ids": source_ids,
        "capsule_fingerprint": _fingerprint(capsule_core),
        "contracts": {"snapshot_schema": snapshot["schema"], "commitment_schema": ledger["schema"], "timeline_schema": timeline["schema"]},
        "safety": {"read_only": True, "source_linked": True, "model_calls_performed": 0, "semantic_similarity_used_as_identity_evidence": False, "content_included": bool(include_content)},
    }
