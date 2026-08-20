from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from mapi_core.memory.agent_self_model import (
    AGENT_SELF_SNAPSHOT_SCHEMA,
    build_agent_self_snapshot_payload,
    calculate_agent_self_snapshot_fingerprint,
)

AGENT_SELF_DELTA_SCHEMA = "mapi_agent_self_delta.v1"
SEMANTIC_FIELDS = (
    "title", "summary_short", "memory_type", "entry_type", "truth_kind", "project_key",
    "scope_code", "layer_code", "area_code", "state_code", "importance_score", "confidence_score",
    "identity_weight", "tags", "supersedes_memory_id", "superseded_by_memory_id", "requires_user_confirmation",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_snapshot(value: str | dict[str, Any], *, field_name: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if isinstance(value, dict):
        snapshot = dict(value)
    else:
        try:
            loaded = json.loads(str(value or ""))
        except json.JSONDecodeError:
            return None, {"status": "error", "schema": AGENT_SELF_DELTA_SCHEMA, "error": "invalid_snapshot_json", "field": field_name}
        if not isinstance(loaded, dict):
            return None, {"status": "error", "schema": AGENT_SELF_DELTA_SCHEMA, "error": "snapshot_must_be_object", "field": field_name}
        snapshot = loaded
    if snapshot.get("schema") != AGENT_SELF_SNAPSHOT_SCHEMA:
        return None, {"status": "error", "schema": AGENT_SELF_DELTA_SCHEMA, "error": "incompatible_snapshot_schema", "field": field_name, "expected_schema": AGENT_SELF_SNAPSHOT_SCHEMA, "actual_schema": snapshot.get("schema")}
    expected = calculate_agent_self_snapshot_fingerprint(snapshot)
    actual = str(snapshot.get("snapshot_fingerprint") or "")
    if actual != expected:
        return None, {"status": "error", "schema": AGENT_SELF_DELTA_SCHEMA, "error": "snapshot_fingerprint_mismatch", "field": field_name, "expected_fingerprint": expected, "actual_fingerprint": actual or None}
    return snapshot, None


def _index(snapshot: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], dict[int, str]]:
    items: dict[int, dict[str, Any]] = {}
    sections: dict[int, str] = {}
    for section, values in dict(snapshot.get("sections") or {}).items():
        for raw in values or []:
            memory_id = int(raw.get("id") or 0)
            if memory_id <= 0:
                continue
            items[memory_id] = dict(raw)
            sections[memory_id] = str(section)
    return items, sections


def _signature(item: dict[str, Any], section: str | None) -> dict[str, Any]:
    return {"section": section, **{field: item.get(field) for field in SEMANTIC_FIELDS}}


def _change_stub(memory_id: int, item: dict[str, Any], section: str | None) -> dict[str, Any]:
    return {"memory_id": int(memory_id), "section": section, "summary_short": item.get("summary_short"), "memory_type": item.get("memory_type"), "truth_kind": item.get("truth_kind"), "project_key": item.get("project_key")}


def _uncertain(item: dict[str, Any]) -> bool:
    truth = str(item.get("truth_kind") or "").casefold()
    state = str(item.get("state_code") or "").casefold()
    confidence = float(item.get("confidence_score") or 0.0)
    return bool(item.get("requires_user_confirmation")) or truth in {"proposal", "interpretation", "dream"} or state in {"candidate", "conflicted", "review"} or (confidence > 0 and confidence < 0.8)


def compare_agent_self_snapshots(from_snapshot: dict[str, Any], to_snapshot: dict[str, Any], *, include_debug: bool = False) -> dict[str, Any]:
    left, left_sections = _index(from_snapshot)
    right, right_sections = _index(to_snapshot)
    left_ids, right_ids = set(left), set(right)
    added_ids = set(right_ids - left_ids)
    removed_ids = set(left_ids - right_ids)
    superseded: list[dict[str, Any]] = []
    for new_id in sorted(list(added_ids)):
        old_id = int(right[new_id].get("supersedes_memory_id") or 0)
        if old_id in removed_ids:
            superseded.append({"old_memory_id": old_id, "new_memory_id": new_id, "old": _change_stub(old_id, left[old_id], left_sections.get(old_id)), "new": _change_stub(new_id, right[new_id], right_sections.get(new_id))})
            added_ids.discard(new_id)
            removed_ids.discard(old_id)

    reclassified: list[dict[str, Any]] = []
    unchanged: list[int] = []
    for memory_id in sorted(left_ids & right_ids):
        before = _signature(left[memory_id], left_sections.get(memory_id))
        after = _signature(right[memory_id], right_sections.get(memory_id))
        changed_fields = sorted(key for key in before if before.get(key) != after.get(key))
        if changed_fields:
            reclassified.append({"memory_id": memory_id, "changed_fields": changed_fields, "before": before, "after": after})
        else:
            unchanged.append(memory_id)

    added = [_change_stub(i, right[i], right_sections.get(i)) for i in sorted(added_ids)]
    removed = [_change_stub(i, left[i], left_sections.get(i)) for i in sorted(removed_ids)]
    old_uncertain = {i for i, item in left.items() if _uncertain(item)}
    new_uncertain = {i for i, item in right.items() if _uncertain(item)}
    new_uncertainties = [_change_stub(i, right[i], right_sections.get(i)) for i in sorted(new_uncertain - old_uncertain) if i in right]
    resolved_uncertainties = [_change_stub(i, left[i], left_sections.get(i)) for i in sorted(old_uncertain - new_uncertain) if i in left]
    old_commitments = {i for i, section in left_sections.items() if section == "commitments"}
    new_commitments = {i for i, section in right_sections.items() if section == "commitments"}
    commitment_changes = [
        *[{"change_kind": "added", **_change_stub(i, right[i], right_sections.get(i))} for i in sorted(new_commitments - old_commitments)],
        *[{"change_kind": "removed", **_change_stub(i, left[i], left_sections.get(i))} for i in sorted(old_commitments - new_commitments)],
    ]
    identity_ids = {i for i, section in left_sections.items() if section == "identity"} & {i for i, section in right_sections.items() if section == "identity"}
    changed_ids = {int(item["memory_id"]) for item in reclassified}
    unchanged_anchors = [_change_stub(i, right[i], "identity") for i in sorted(identity_ids - changed_ids)]
    source_changes = {
        "added_memory_ids": sorted(added_ids),
        "removed_memory_ids": sorted(removed_ids),
        "superseded_old_memory_ids": [int(item["old_memory_id"]) for item in superseded],
        "superseded_new_memory_ids": [int(item["new_memory_id"]) for item in superseded],
        "reclassified_memory_ids": sorted(changed_ids),
    }
    core = {
        "schema": AGENT_SELF_DELTA_SCHEMA,
        "read_only": True,
        "from_snapshot_fingerprint": from_snapshot.get("snapshot_fingerprint"),
        "to_snapshot_fingerprint": to_snapshot.get("snapshot_fingerprint"),
        "added": added,
        "removed": removed,
        "superseded": superseded,
        "reclassified": reclassified,
        "new_uncertainties": new_uncertainties,
        "resolved_uncertainties": resolved_uncertainties,
        "commitment_changes": commitment_changes,
        "unchanged_anchors": unchanged_anchors,
        "source_changes": source_changes,
    }
    has_changes = any(core[key] for key in ("added", "removed", "superseded", "reclassified", "new_uncertainties", "resolved_uncertainties", "commitment_changes"))
    result = {**core, "status": "ok", "has_changes": has_changes, "delta_fingerprint": _fingerprint(core), "safety": {"read_only": True, "writes_performed": 0, "model_calls_performed": 0}}
    if include_debug:
        result["debug"] = {"shared_evidence_count": len(left_ids & right_ids), "from_count": len(left_ids), "to_count": len(right_ids)}
    return result


def build_agent_self_delta_payload(conn: Any, *, from_snapshot_json: str | dict[str, Any], to_snapshot_json: str | dict[str, Any] | None, subject_key: str | None, display_name: str | None, project_key: str | None, include_global: bool, include_debug: bool, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    from_snapshot, error = _parse_snapshot(from_snapshot_json, field_name="from_snapshot")
    if error: return error
    if to_snapshot_json is None:
        to_snapshot = build_agent_self_snapshot_payload(conn, subject_key=subject_key, display_name=display_name, project_key=project_key, include_global=include_global, limit=500, include_content=False, row_to_dict=row_to_dict)
    else:
        to_snapshot, error = _parse_snapshot(to_snapshot_json, field_name="to_snapshot")
        if error: return error
    assert from_snapshot is not None and to_snapshot is not None
    return compare_agent_self_snapshots(from_snapshot, to_snapshot, include_debug=include_debug)
