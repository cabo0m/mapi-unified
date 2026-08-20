from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from mapi_core.memory.lifecycle_snapshots import (
    create_lifecycle_snapshot_payload,
    find_lifecycle_snapshot_by_operation_key,
    get_lifecycle_snapshot_payload,
    list_lifecycle_snapshots_payload,
    mark_lifecycle_snapshot_rolled_back_payload,
)
from mapi_core.memory.lifecycle_contracts import (
    MEMORY_V3_HASH_ALGORITHM,
    derive_canonical_memory_state,
    is_supersession_capable_relation_kind,
    normalize_relation_kind,
    project_memory_v2_status,
)
from mapi_core.memory.lifecycle_integrity import (
    PREVIEW_BLOCKING_LIFECYCLE_ISSUE_CODES,
    evaluate_lifecycle_integrity_graph,
    load_lifecycle_graph,
)
from mapi_core.schemas import normalize_optional_text


SUPERSESSION_PREVIEW_SCHEMA_VERSION = "memory_v3_supersession_preview.v1"
SUPERSESSION_APPLY_SCHEMA_VERSION = "memory_v3_supersession_apply.v1"
SUPERSESSION_RUN_SCHEMA_VERSION = "memory_v3_supersession_run.v1"
SUPERSESSION_RUNS_SCHEMA_VERSION = "memory_v3_supersession_runs.v1"
SUPERSESSION_ROLLBACK_PREVIEW_SCHEMA_VERSION = "memory_v3_supersession_rollback_preview.v1"
SUPERSESSION_ROLLBACK_SCHEMA_VERSION = "memory_v3_supersession_rollback.v1"


def _minimal_lineage_snapshot(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(memory["id"]),
        "updated_at": normalize_optional_text(memory.get("updated_at")),
        "state_code": normalize_optional_text(memory.get("_raw", {}).get("state_code")) or normalize_optional_text(memory.get("state_code")),
        "memory_v2_status": normalize_optional_text(memory.get("_raw", {}).get("memory_v2_status")) or normalize_optional_text(memory.get("memory_v2_status")),
        "project_key": normalize_optional_text(memory.get("project_key")),
        "scope_code": normalize_optional_text(memory.get("scope_code")),
        "truth_kind": normalize_optional_text(memory.get("truth_kind")),
        "supersedes_memory_id": None if memory.get("supersedes_memory_id") is None else int(memory["supersedes_memory_id"]),
        "superseded_by_memory_id": None if memory.get("superseded_by_memory_id") is None else int(memory["superseded_by_memory_id"]),
    }


def _lineage_ids(memories_by_id: dict[int, dict[str, Any]], *, root_ids: list[int]) -> dict[str, list[int]]:
    children_by_parent: dict[int, list[int]] = {}
    for memory in memories_by_id.values():
        parent_id = memory.get("supersedes_memory_id")
        if parent_id is None:
            continue
        children_by_parent.setdefault(int(parent_id), []).append(int(memory["id"]))

    def _ancestors(memory_id: int) -> list[int]:
        result: list[int] = []
        current = memories_by_id.get(int(memory_id))
        seen: set[int] = set()
        while current is not None and current.get("supersedes_memory_id") is not None:
            parent_id = int(current["supersedes_memory_id"])
            if parent_id in seen:
                break
            seen.add(parent_id)
            result.append(parent_id)
            current = memories_by_id.get(parent_id)
        return result

    def _descendants(memory_id: int) -> list[int]:
        result: list[int] = []
        queue = list(children_by_parent.get(int(memory_id), []))
        seen: set[int] = set()
        while queue:
            current_id = int(queue.pop(0))
            if current_id in seen:
                continue
            seen.add(current_id)
            result.append(current_id)
            queue.extend(children_by_parent.get(current_id, []))
        return result

    payload: dict[str, list[int]] = {}
    for memory_id in root_ids:
        payload[str(memory_id)] = sorted({* _ancestors(memory_id), * _descendants(memory_id)})
    return payload


def preview_memory_supersession_payload(
    conn: Any,
    *,
    new_memory_id: int,
    old_memory_id: int,
    relation_kind: str,
    reason: str,
    include_debug: bool = False,
    normalize_required_text: Callable[[Any, str], str],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    canonical_json_hash: Callable[[Any], str],
) -> dict[str, Any]:
    normalized_reason = normalize_required_text(reason, "reason")
    normalized_relation_kind = normalize_relation_kind(relation_kind)
    if normalized_relation_kind is None or not is_supersession_capable_relation_kind(normalized_relation_kind):
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_PREVIEW_SCHEMA_VERSION,
            "input": {
                "new_memory_id": int(new_memory_id),
                "old_memory_id": int(old_memory_id),
                "relation_kind": normalize_optional_text(relation_kind),
            },
            "guard": {
                "allowed": False,
                "blockers": ["unsupported_relation_kind"],
                "warnings": [],
            },
            "planned_changes": {},
            "safety": {
                "read_only": True,
                "mutations_performed": 0,
                "apply_supported": False,
            },
            "operator_next_action": "inspect",
            "unsupported_metrics": [],
        }
    if int(new_memory_id) == int(old_memory_id):
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_PREVIEW_SCHEMA_VERSION,
            "input": {
                "new_memory_id": int(new_memory_id),
                "old_memory_id": int(old_memory_id),
                "relation_kind": normalized_relation_kind,
            },
            "guard": {
                "allowed": False,
                "blockers": ["same_memory_id"],
                "warnings": [],
            },
            "planned_changes": {},
            "safety": {
                "read_only": True,
                "mutations_performed": 0,
                "apply_supported": False,
            },
            "operator_next_action": "inspect",
            "unsupported_metrics": [],
        }

    graph = load_lifecycle_graph(
        conn,
        memory_id=int(new_memory_id),
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
        include_archived=True,
        limit=10,
    )
    old_graph = load_lifecycle_graph(
        conn,
        memory_id=int(old_memory_id),
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
        include_archived=True,
        limit=10,
    )
    memories_by_id = dict(graph["memories_by_id"])
    memories_by_id.update(old_graph["memories_by_id"])
    merged_graph = {
        "base_rows": [item for item in graph["base_rows"] + old_graph["base_rows"] if int(item["id"]) in {int(new_memory_id), int(old_memory_id)}],
        "base_ids": [int(new_memory_id), int(old_memory_id)],
        "memories_by_id": memories_by_id,
        "links": [*graph["links"], *[link for link in old_graph["links"] if link not in graph["links"]]],
    }

    new_memory = memories_by_id.get(int(new_memory_id))
    old_memory = memories_by_id.get(int(old_memory_id))
    if new_memory is None or old_memory is None:
        blockers = []
        if new_memory is None:
            blockers.append("new_memory_missing")
        if old_memory is None:
            blockers.append("old_memory_missing")
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_PREVIEW_SCHEMA_VERSION,
            "input": {
                "new_memory_id": int(new_memory_id),
                "old_memory_id": int(old_memory_id),
                "relation_kind": normalized_relation_kind,
            },
            "guard": {
                "allowed": False,
                "blockers": blockers,
                "warnings": [],
            },
            "planned_changes": {},
            "safety": {
                "read_only": True,
                "mutations_performed": 0,
                "apply_supported": False,
            },
            "operator_next_action": "inspect",
            "unsupported_metrics": [],
        }

    integrity = evaluate_lifecycle_integrity_graph(
        merged_graph,
        sample_limit=50,
        include_debug=include_debug,
    )
    relevant_integrity_findings = [
        finding
        for finding in integrity["findings"]
        if int(new_memory_id) in finding["memory_ids"] or int(old_memory_id) in finding["memory_ids"]
    ]
    blockers: list[str] = []
    warnings: list[str] = []
    for finding in relevant_integrity_findings:
        if str(finding["issue_code"]) in PREVIEW_BLOCKING_LIFECYCLE_ISSUE_CODES:
            blockers.append(str(finding["issue_code"]))
        else:
            warnings.append(str(finding["issue_code"]))

    try:
        new_state = derive_canonical_memory_state(
            state_code=normalize_optional_text(new_memory.get("_raw", {}).get("state_code")) or normalize_optional_text(new_memory.get("state_code")),
            activity_state=normalize_optional_text(new_memory.get("_raw", {}).get("activity_state")) or normalize_optional_text(new_memory.get("activity_state")),
            contradiction_flag=new_memory.get("contradiction_flag"),
        )
        old_state = derive_canonical_memory_state(
            state_code=normalize_optional_text(old_memory.get("_raw", {}).get("state_code")) or normalize_optional_text(old_memory.get("state_code")),
            activity_state=normalize_optional_text(old_memory.get("_raw", {}).get("activity_state")) or normalize_optional_text(old_memory.get("activity_state")),
            contradiction_flag=old_memory.get("contradiction_flag"),
        )
    except ValueError:
        blockers.append("unknown_lifecycle_state")
        new_state = None
        old_state = None

    new_project_key = normalize_optional_text(new_memory.get("project_key"))
    old_project_key = normalize_optional_text(old_memory.get("project_key"))
    if new_project_key != old_project_key and not (new_project_key is None and old_project_key is None):
        blockers.append("cross_project")

    new_scope_code = normalize_optional_text(new_memory.get("scope_code"))
    old_scope_code = normalize_optional_text(old_memory.get("scope_code"))
    if new_scope_code != old_scope_code and not (new_scope_code is None and old_scope_code is None):
        blockers.append("cross_scope")

    if new_state in {"archived", "superseded"}:
        blockers.append("new_memory_not_active_head_candidate")
    if old_state == "candidate":
        blockers.append("old_memory_candidate_requires_review")

    new_truth_kind = normalize_optional_text(new_memory.get("truth_kind"))
    old_truth_kind = normalize_optional_text(old_memory.get("truth_kind"))
    if new_truth_kind in {"dream", "proposal"} and old_truth_kind in {"fact", "decision"} and not bool(old_memory.get("requires_user_confirmation")):
        blockers.append("proposal_or_dream_cannot_replace_confirmed_fact")

    if old_memory.get("superseded_by_memory_id") not in {None, int(new_memory_id)}:
        blockers.append("old_memory_already_replaced_by_different_head")

    lineage_ids = _lineage_ids(memories_by_id, root_ids=[int(new_memory_id), int(old_memory_id)])
    new_related_ids = set(lineage_ids[str(int(new_memory_id))])
    old_related_ids = set(lineage_ids[str(int(old_memory_id))])
    direct_match = (
        int(new_memory.get("supersedes_memory_id") or 0) == int(old_memory_id)
        and int(old_memory.get("superseded_by_memory_id") or 0) == int(new_memory_id)
        and old_state == "superseded"
        and (int(new_memory_id), int(old_memory_id)) in {
            (int(link["from_memory_id"]), int(link["to_memory_id"]))
            for link in merged_graph["links"]
        }
    )
    if direct_match:
        status = "already_satisfied"
    elif int(old_memory_id) in new_related_ids:
        blockers.append("proposed_supersession_would_create_cycle")
    elif int(new_memory_id) in old_related_ids:
        blockers.append("existing_lineage_branch_or_descendant_conflict")
        status = "blocked"
    else:
        status = "preview_ready"

    if blockers:
        status = "blocked"

    input_payload = {
        "new_memory_id": int(new_memory_id),
        "old_memory_id": int(old_memory_id),
        "relation_kind": normalized_relation_kind,
        "reason": normalized_reason,
        "schema_version": SUPERSESSION_PREVIEW_SCHEMA_VERSION,
    }
    input_fingerprint = canonical_json_hash(input_payload)
    candidate_set_payload = {
        "new_memory": _minimal_lineage_snapshot(new_memory),
        "old_memory": _minimal_lineage_snapshot(old_memory),
        "lineage_ids": {
            "new_memory": sorted(new_related_ids),
            "old_memory": sorted(old_related_ids),
        },
    }
    candidate_set_fingerprint = canonical_json_hash(candidate_set_payload)

    planned_changes = {
        "new_memory": {
            "supersedes_memory_id": int(old_memory_id),
        },
        "old_memory": {
            "superseded_by_memory_id": int(new_memory_id),
            "state_code": "superseded",
            "memory_v2_status": project_memory_v2_status(state_code="superseded"),
        },
        "link": {
            "from_memory_id": int(new_memory_id),
            "to_memory_id": int(old_memory_id),
            "relation_type": "supersedes",
            "relation_kind": normalized_relation_kind,
        },
        "events": [
            {
                "memory_id": int(new_memory_id),
                "event_type": "version.supersession_applied",
            },
            {
                "memory_id": int(old_memory_id),
                "event_type": "version.superseded",
            },
        ],
    }
    preview_hash = canonical_json_hash(
        {
            "input": input_payload,
            "candidate_set_payload": candidate_set_payload,
            "status": status,
            "guard": {
                "allowed": not blockers,
                "blockers": blockers,
                "warnings": warnings,
            },
            "planned_changes": planned_changes,
        }
    )

    operator_next_action = "wait_for_v3_2"
    if blockers:
        operator_next_action = "resolve_integrity_issue"
    elif status == "already_satisfied":
        operator_next_action = "inspect"

    result = {
        "status": status,
        "schema_version": SUPERSESSION_PREVIEW_SCHEMA_VERSION,
        "input": {
            "new_memory_id": int(new_memory_id),
            "old_memory_id": int(old_memory_id),
            "relation_kind": normalized_relation_kind,
        },
        "new_memory": _minimal_lineage_snapshot(new_memory),
        "old_memory": _minimal_lineage_snapshot(old_memory),
        "lineage": {
            "ids": {
                "new_memory": sorted(new_related_ids),
                "old_memory": sorted(old_related_ids),
            },
            "integrity_findings": relevant_integrity_findings,
        },
        "guard": {
            "allowed": not blockers,
            "blockers": sorted(dict.fromkeys(blockers)),
            "warnings": sorted(dict.fromkeys(warnings)),
        },
        "planned_changes": planned_changes,
        "hash_algorithm": MEMORY_V3_HASH_ALGORITHM,
        "input_fingerprint": input_fingerprint,
        "candidate_set_fingerprint": candidate_set_fingerprint,
        "preview_hash": preview_hash,
        "safety": {
            "read_only": True,
            "mutations_performed": 0,
            "apply_supported": False,
        },
        "operator_next_action": operator_next_action,
        "unsupported_metrics": list(dict.fromkeys(integrity.get("unsupported_metrics") or [])),
    }
    if include_debug:
        result["debug"] = {
            "input_payload": input_payload,
            "candidate_set_payload": candidate_set_payload,
        }
    return result


def _load_pair_graph(
    conn: Any,
    *,
    new_memory_id: int,
    old_memory_id: int,
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    graph = load_lifecycle_graph(
        conn,
        memory_id=int(new_memory_id),
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
        include_archived=True,
        limit=10,
    )
    old_graph = load_lifecycle_graph(
        conn,
        memory_id=int(old_memory_id),
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
        include_archived=True,
        limit=10,
    )
    memories_by_id = dict(graph["memories_by_id"])
    memories_by_id.update(old_graph["memories_by_id"])
    return {
        "base_rows": [
            item
            for item in graph["base_rows"] + old_graph["base_rows"]
            if int(item["id"]) in {int(new_memory_id), int(old_memory_id)}
        ],
        "base_ids": [int(new_memory_id), int(old_memory_id)],
        "memories_by_id": memories_by_id,
        "links": [*graph["links"], *[link for link in old_graph["links"] if link not in graph["links"]]],
    }


def _semantic_memory_snapshot(memory: dict[str, Any]) -> dict[str, Any]:
    raw = memory.get("_raw", {})
    return {
        "id": int(memory["id"]),
        "updated_at": normalize_optional_text(memory.get("updated_at")) or normalize_optional_text(raw.get("updated_at")),
        "state_code": normalize_optional_text(raw.get("state_code")) or normalize_optional_text(memory.get("state_code")),
        "memory_v2_status": normalize_optional_text(raw.get("memory_v2_status")) or normalize_optional_text(memory.get("memory_v2_status")),
        "activity_state": normalize_optional_text(raw.get("activity_state")) or normalize_optional_text(memory.get("activity_state")),
        "valid_to": normalize_optional_text(raw.get("valid_to")) or normalize_optional_text(memory.get("valid_to")),
        "expired_due_at": normalize_optional_text(raw.get("expired_due_at")) or normalize_optional_text(memory.get("expired_due_at")),
        "project_key": normalize_optional_text(memory.get("project_key")),
        "scope_code": normalize_optional_text(memory.get("scope_code")),
        "truth_kind": normalize_optional_text(memory.get("truth_kind")),
        "layer_code": normalize_optional_text(memory.get("layer_code")),
        "importance_level": normalize_optional_text(memory.get("importance_level")),
        "requires_user_confirmation": bool(memory.get("requires_user_confirmation")),
        "supersedes_memory_id": None if memory.get("supersedes_memory_id") is None else int(memory["supersedes_memory_id"]),
        "superseded_by_memory_id": None if memory.get("superseded_by_memory_id") is None else int(memory["superseded_by_memory_id"]),
    }


def _pair_links_payload(conn: Any, *, new_memory_id: int, old_memory_id: int, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT *
        FROM memory_links
        WHERE relation_type = 'supersedes'
          AND (
            (from_memory_id = ? AND to_memory_id = ?)
            OR
            (from_memory_id = ? AND to_memory_id = ?)
          )
        ORDER BY id ASC
        """,
        (int(new_memory_id), int(old_memory_id), int(old_memory_id), int(new_memory_id)),
    ).fetchall()
    links = []
    for row in rows:
        link = row_to_dict(row)
        links.append(
            {
                "id": int(link["id"]),
                "from_memory_id": int(link["from_memory_id"]),
                "to_memory_id": int(link["to_memory_id"]),
                "relation_type": str(link["relation_type"]),
                "weight": float(link.get("weight") or 0.0),
                "origin": normalize_optional_text(link.get("origin")),
                "archived_at": normalize_optional_text(link.get("archived_at")),
                "created_at": normalize_optional_text(link.get("created_at")),
            }
        )
    active_direct_links = [
        link for link in links
        if link["from_memory_id"] == int(new_memory_id)
        and link["to_memory_id"] == int(old_memory_id)
        and link["archived_at"] is None
    ]
    active_reverse_links = [
        link for link in links
        if link["from_memory_id"] == int(old_memory_id)
        and link["to_memory_id"] == int(new_memory_id)
        and link["archived_at"] is None
    ]
    return {
        "pair_links": links,
        "active_direct_links": active_direct_links,
        "active_reverse_links": active_reverse_links,
        "active_direct_link_id": active_direct_links[0]["id"] if len(active_direct_links) == 1 else None,
        "active_reverse_link_id": active_reverse_links[0]["id"] if len(active_reverse_links) == 1 else None,
    }


def _event_count_snapshot(conn: Any, *, new_memory_id: int, old_memory_id: int) -> dict[str, Any]:
    return {
        "new_memory": int(conn.execute("SELECT COUNT(*) FROM memory_events WHERE memory_id = ?", (int(new_memory_id),)).fetchone()[0]),
        "old_memory": int(conn.execute("SELECT COUNT(*) FROM memory_events WHERE memory_id = ?", (int(old_memory_id),)).fetchone()[0]),
    }


def _operation_key(
    *,
    new_memory_id: int,
    old_memory_id: int,
    relation_kind: str,
    reason: str,
    canonical_json_hash: Callable[[Any], str],
) -> str:
    payload = {
        "schema_version": "memory_v3_supersession_operation.v1",
        "new_memory_id": int(new_memory_id),
        "old_memory_id": int(old_memory_id),
        "relation_kind": str(relation_kind),
        "reason": str(reason),
    }
    return f"supersession:{int(new_memory_id)}:{int(old_memory_id)}:{canonical_json_hash(payload)}"


def _protected_memory_reasons(memory: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if normalize_optional_text(memory.get("layer_code")) in {"core", "identity"}:
        reasons.append("protected_layer")
    if normalize_optional_text(memory.get("truth_kind")) == "decision":
        reasons.append("decision_truth_kind")
    if normalize_optional_text(memory.get("importance_level")) == "critical":
        reasons.append("critical_importance")
    return reasons


def _compare_semantic_memory(expected: dict[str, Any], current: dict[str, Any], *, label: str) -> list[str]:
    reasons: list[str] = []
    for key, expected_value in expected.items():
        current_value = current.get(key)
        if current_value != expected_value:
            reasons.append(f"{label}.{key}: expected {expected_value!r}, got {current_value!r}")
    return reasons


def _compare_link_state(expected: dict[str, Any], current: dict[str, Any]) -> list[str]:
    if current.get("pair_links") != expected.get("pair_links"):
        return ["pair_links differ from expected snapshot"]
    return []


def _current_pair_state(
    conn: Any,
    *,
    new_memory_id: int,
    old_memory_id: int,
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    graph = _load_pair_graph(
        conn,
        new_memory_id=int(new_memory_id),
        old_memory_id=int(old_memory_id),
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
    )
    memories_by_id = graph["memories_by_id"]
    new_memory = memories_by_id.get(int(new_memory_id))
    old_memory = memories_by_id.get(int(old_memory_id))
    if new_memory is None or old_memory is None:
        raise FileNotFoundError("new_memory or old_memory is missing")
    return {
        "graph": graph,
        "new_memory": new_memory,
        "old_memory": old_memory,
        "memory_snapshot": {
            "new_memory": _semantic_memory_snapshot(new_memory),
            "old_memory": _semantic_memory_snapshot(old_memory),
        },
        "link_snapshot": _pair_links_payload(
            conn,
            new_memory_id=int(new_memory_id),
            old_memory_id=int(old_memory_id),
            row_to_dict=row_to_dict,
        ),
        "event_counts": _event_count_snapshot(
            conn,
            new_memory_id=int(new_memory_id),
            old_memory_id=int(old_memory_id),
        ),
    }


def _apply_snapshot_integrity_summary(
    conn: Any,
    *,
    snapshot: dict[str, Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    current = _current_pair_state(
        conn,
        new_memory_id=int(snapshot["new_memory_id"]),
        old_memory_id=int(snapshot["old_memory_id"]),
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
    )
    if snapshot["status"] == "rolled_back" and isinstance(snapshot.get("rollback_snapshot"), dict):
        rollback_snapshot = snapshot["rollback_snapshot"]
        target = rollback_snapshot.get("after_rollback_memory_snapshot") or snapshot["before_snapshot"]
        target_link_snapshot = rollback_snapshot.get("after_rollback_link_snapshot") or snapshot["link_snapshot"]["before"]
    else:
        target = snapshot["after_snapshot"] if snapshot["status"] == "applied" else snapshot["before_snapshot"]
        target_link_snapshot = snapshot["link_snapshot"]["after"] if snapshot["status"] == "applied" else snapshot["link_snapshot"]["before"]
    reasons = [
        *_compare_semantic_memory(target["new_memory"], current["memory_snapshot"]["new_memory"], label="new_memory"),
        *_compare_semantic_memory(target["old_memory"], current["memory_snapshot"]["old_memory"], label="old_memory"),
        *_compare_link_state(target_link_snapshot, current["link_snapshot"]),
    ]
    return {
        "matches_current_state": not reasons,
        "mismatch_reasons": reasons,
        "rollback_available": snapshot["status"] == "applied" and not reasons,
    }


def _run_summary(snapshot: dict[str, Any], *, integrity_summary: dict[str, Any], include_debug: bool = False) -> dict[str, Any]:
    payload = {
        "run_id": int(snapshot["id"]),
        "operation_key": str(snapshot["operation_key"]),
        "status": str(snapshot["status"]),
        "operation_type": str(snapshot["operation_type"]),
        "new_memory_id": int(snapshot["new_memory_id"]),
        "old_memory_id": int(snapshot["old_memory_id"]),
        "relation_kind": str(snapshot["relation_kind"]),
        "preview_hash": str(snapshot["preview_hash"]),
        "applied_at": normalize_optional_text(snapshot.get("applied_at")),
        "applied_by": normalize_optional_text(snapshot.get("applied_by")),
        "rolled_back_at": normalize_optional_text(snapshot.get("rolled_back_at")),
        "rolled_back_by": normalize_optional_text(snapshot.get("rolled_back_by")),
        "snapshot_integrity_summary": integrity_summary,
        "rollback_availability": integrity_summary["rollback_available"],
        "unsupported_metrics": [],
    }
    if include_debug:
        payload["debug"] = {
            "before_snapshot": snapshot["before_snapshot"],
            "after_snapshot": snapshot["after_snapshot"],
            "link_snapshot": snapshot["link_snapshot"],
            "event_snapshot": snapshot["event_snapshot"],
            "rollback_snapshot": snapshot.get("rollback_snapshot"),
        }
    return payload


def apply_memory_supersession_payload(
    conn: Any,
    *,
    new_memory_id: int,
    old_memory_id: int,
    relation_kind: str,
    reason: str,
    expected_preview_hash: str,
    applied_by: str | None = None,
    notes: str | None = None,
    confirm_protected: bool = False,
    include_debug: bool = False,
    manage_transaction: bool = True,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    canonical_json_hash: Callable[[Any], str],
    utc_now_iso: Callable[[], str],
    shift_iso_days: Callable[[str | None, int], str | None],
    insert_memory_event: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    normalized_reason = normalize_required_text(reason, "reason")
    normalized_expected_preview_hash = normalize_required_text(expected_preview_hash, "expected_preview_hash")
    normalized_relation_kind = normalize_relation_kind(relation_kind)
    if normalized_relation_kind is None or not is_supersession_capable_relation_kind(normalized_relation_kind):
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_APPLY_SCHEMA_VERSION,
            "blocking_reasons": ["unsupported_relation_kind"],
            "apply_run_created": False,
        }

    op_key = _operation_key(
        new_memory_id=int(new_memory_id),
        old_memory_id=int(old_memory_id),
        relation_kind=normalized_relation_kind,
        reason=normalized_reason,
        canonical_json_hash=canonical_json_hash,
    )
    existing = find_lifecycle_snapshot_by_operation_key(
        conn,
        operation_key=op_key,
        normalize_required_text=normalize_required_text,
        row_to_dict=row_to_dict,
    )
    if existing is not None:
        integrity_summary = _apply_snapshot_integrity_summary(
            conn,
            snapshot=existing,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
        if existing["status"] == "applied" and integrity_summary["matches_current_state"]:
            return {
                "status": "already_applied",
                "schema_version": SUPERSESSION_APPLY_SCHEMA_VERSION,
                "run_id": int(existing["id"]),
                "operation_key": op_key,
                "preview_hash": str(existing["preview_hash"]),
                "apply_run_created": False,
                "blocking_reasons": [],
                "safety": {
                    "read_only": True,
                    "mutations_performed": 0,
                },
                "unsupported_metrics": [],
            }
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_APPLY_SCHEMA_VERSION,
            "run_id": int(existing["id"]),
            "operation_key": op_key,
            "blocking_reasons": ["operation_key_already_exists"],
            "apply_run_created": False,
            "integrity_summary": integrity_summary,
            "unsupported_metrics": [],
        }

    fresh_preview = preview_memory_supersession_payload(
        conn,
        new_memory_id=int(new_memory_id),
        old_memory_id=int(old_memory_id),
        relation_kind=normalized_relation_kind,
        reason=normalized_reason,
        include_debug=include_debug,
        normalize_required_text=normalize_required_text,
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
        canonical_json_hash=canonical_json_hash,
    )
    if fresh_preview["status"] != "preview_ready":
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_APPLY_SCHEMA_VERSION,
            "operation_key": op_key,
            "blocking_reasons": list(fresh_preview.get("guard", {}).get("blockers") or []),
            "preview_status": fresh_preview["status"],
            "apply_run_created": False,
            "unsupported_metrics": list(fresh_preview.get("unsupported_metrics") or []),
        }
    if normalized_expected_preview_hash != str(fresh_preview["preview_hash"]):
        return {
            "status": "stale_preview",
            "schema_version": SUPERSESSION_APPLY_SCHEMA_VERSION,
            "operation_key": op_key,
            "apply_run_created": False,
            "blocking_reasons": ["expected_preview_hash_mismatch"],
            "expected_preview_hash": normalized_expected_preview_hash,
            "current_preview_hash": str(fresh_preview["preview_hash"]),
            "input_fingerprint": str(fresh_preview["input_fingerprint"]),
            "candidate_set_fingerprint": str(fresh_preview["candidate_set_fingerprint"]),
            "unsupported_metrics": list(fresh_preview.get("unsupported_metrics") or []),
        }

    current = _current_pair_state(
        conn,
        new_memory_id=int(new_memory_id),
        old_memory_id=int(old_memory_id),
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
    )
    new_memory = current["new_memory"]
    old_memory = current["old_memory"]

    protected_reasons = {
        "new_memory": _protected_memory_reasons(new_memory),
        "old_memory": _protected_memory_reasons(old_memory),
    }
    if not confirm_protected and (protected_reasons["new_memory"] or protected_reasons["old_memory"]):
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_APPLY_SCHEMA_VERSION,
            "operation_key": op_key,
            "apply_run_created": False,
            "blocking_reasons": ["protected_memory_requires_confirmation"],
            "protected_reasons": protected_reasons,
            "unsupported_metrics": [],
        }

    link_state = current["link_snapshot"]
    if len(link_state["active_direct_links"]) > 1:
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_APPLY_SCHEMA_VERSION,
            "operation_key": op_key,
            "apply_run_created": False,
            "blocking_reasons": ["duplicate_active_supersedes_links"],
            "unsupported_metrics": [],
        }
    if link_state["active_reverse_links"]:
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_APPLY_SCHEMA_VERSION,
            "operation_key": op_key,
            "apply_run_created": False,
            "blocking_reasons": ["conflicting_reverse_supersedes_link"],
            "unsupported_metrics": [],
        }

    applied_at = utc_now_iso()
    applied_source = "memory_v3_supersession_apply"
    relation_weight = 1.0
    before_snapshot = current["memory_snapshot"]
    before_link_snapshot = current["link_snapshot"]
    before_event_counts = current["event_counts"]

    try:
        if manage_transaction:
            conn.execute("BEGIN")
        current_in_tx = _current_pair_state(
            conn,
            new_memory_id=int(new_memory_id),
            old_memory_id=int(old_memory_id),
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
        if current_in_tx["memory_snapshot"] != before_snapshot or current_in_tx["link_snapshot"] != before_link_snapshot:
            if manage_transaction:
                conn.rollback()
            return {
                "status": "stale_preview",
                "schema_version": SUPERSESSION_APPLY_SCHEMA_VERSION,
                "operation_key": op_key,
                "apply_run_created": False,
                "blocking_reasons": ["candidate_snapshot_changed_before_apply"],
                "unsupported_metrics": [],
            }

        active_direct_links = list(current_in_tx["link_snapshot"]["active_direct_links"])
        created_link_id = None
        reused_link_id = None
        if active_direct_links:
            reused_link_id = int(active_direct_links[0]["id"])
        else:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO memory_links (
                    from_memory_id,
                    to_memory_id,
                    relation_type,
                    weight,
                    origin,
                    created_at
                )
                VALUES (?, ?, 'supersedes', ?, ?, ?)
                """,
                (
                    int(new_memory_id),
                    int(old_memory_id),
                    relation_weight,
                    f"memory_v3_supersession:{op_key}",
                    applied_at,
                ),
            )
            created_link_id = int(cursor.lastrowid)

        conn.execute(
            """
            UPDATE memories
            SET supersedes_memory_id = ?,
                updated_at = ?,
                last_accessed_at = ?,
                validation_source = ?
            WHERE id = ?
            """,
            (int(old_memory_id), applied_at, applied_at, applied_source, int(new_memory_id)),
        )
        conn.execute(
            """
            UPDATE memories
            SET superseded_by_memory_id = ?,
                state_code = ?,
                memory_v2_status = ?,
                valid_to = ?,
                expired_due_at = ?,
                updated_at = ?,
                last_accessed_at = ?,
                validation_source = ?
            WHERE id = ?
            """,
            (
                int(new_memory_id),
                "superseded",
                project_memory_v2_status(state_code="superseded"),
                applied_at,
                shift_iso_days(applied_at, 2),
                applied_at,
                applied_at,
                applied_source,
                int(old_memory_id),
            ),
        )

        apply_event = insert_memory_event(
            conn,
            memory_id=int(new_memory_id),
            event_type="version.supersession_applied",
            payload={
                "operation_key": op_key,
                "relation_kind": normalized_relation_kind,
                "reason": normalized_reason,
                "applied_at": applied_at,
                "applied_by": normalize_optional_text(applied_by),
                "old_memory_id": int(old_memory_id),
            },
        )
        old_event = insert_memory_event(
            conn,
            memory_id=int(old_memory_id),
            event_type="version.superseded",
            payload={
                "operation_key": op_key,
                "relation_kind": normalized_relation_kind,
                "reason": normalized_reason,
                "applied_at": applied_at,
                "applied_by": normalize_optional_text(applied_by),
                "new_memory_id": int(new_memory_id),
            },
        )

        after_state = _current_pair_state(
            conn,
            new_memory_id=int(new_memory_id),
            old_memory_id=int(old_memory_id),
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
        link_action = "reused" if reused_link_id is not None else "created"
        snapshot = create_lifecycle_snapshot_payload(
            conn,
            operation_key=op_key,
            new_memory_id=int(new_memory_id),
            old_memory_id=int(old_memory_id),
            relation_kind=normalized_relation_kind,
            reason=normalized_reason,
            input_fingerprint=str(fresh_preview["input_fingerprint"]),
            candidate_set_fingerprint=str(fresh_preview["candidate_set_fingerprint"]),
            preview_hash=str(fresh_preview["preview_hash"]),
            before_snapshot=before_snapshot,
            after_snapshot=after_state["memory_snapshot"],
            link_snapshot={
                "before": before_link_snapshot,
                "after": {
                    **after_state["link_snapshot"],
                    "link_action": link_action,
                    "created_link_id": created_link_id,
                    "reused_link_id": reused_link_id,
                },
            },
            event_snapshot={
                "before_counts": before_event_counts,
                "after_counts": after_state["event_counts"],
                "created_event_ids": {
                    "new_memory": int(apply_event["id"]),
                    "old_memory": int(old_event["id"]),
                },
                "created_event_types": Counter(
                    [str(apply_event["event_type"]), str(old_event["event_type"])]
                ),
            },
            applied_at=applied_at,
            applied_by=normalize_optional_text(applied_by),
            apply_note=normalize_optional_text(notes),
            utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
        )
        if manage_transaction:
            conn.commit()
    except Exception as exc:
        if not manage_transaction:
            raise
        conn.rollback()
        return {
            "status": "error",
            "schema_version": SUPERSESSION_APPLY_SCHEMA_VERSION,
            "operation_key": op_key,
            "apply_run_created": False,
            "error": str(exc),
            "unsupported_metrics": [],
        }

    result = {
        "status": "applied",
        "schema_version": SUPERSESSION_APPLY_SCHEMA_VERSION,
        "run_id": int(snapshot["id"]),
        "operation_key": op_key,
        "preview_hash": str(fresh_preview["preview_hash"]),
        "apply_run_created": True,
        "new_memory_id": int(new_memory_id),
        "old_memory_id": int(old_memory_id),
        "relation_kind": normalized_relation_kind,
        "applied_at": applied_at,
        "applied_by": normalize_optional_text(applied_by),
        "snapshot_integrity_summary": _apply_snapshot_integrity_summary(
            conn,
            snapshot=snapshot,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        ),
        "unsupported_metrics": list(fresh_preview.get("unsupported_metrics") or []),
    }
    if include_debug:
        result["debug"] = {
            "before_snapshot": before_snapshot,
            "after_snapshot": snapshot["after_snapshot"],
            "link_snapshot": snapshot["link_snapshot"],
            "event_snapshot": snapshot["event_snapshot"],
        }
    return result


def list_memory_supersession_runs_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    new_memory_id: int | None = None,
    old_memory_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    base = list_lifecycle_snapshots_payload(
        conn,
        project_key=project_key,
        new_memory_id=new_memory_id,
        old_memory_id=old_memory_id,
        status=status,
        limit=limit,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=row_to_dict,
    )
    if base.get("status") != "ok":
        return {
            "status": base.get("status"),
            "schema_version": SUPERSESSION_RUNS_SCHEMA_VERSION,
            "error": base.get("error"),
        }
    runs = []
    for snapshot in base["runs"]:
        integrity_summary = _apply_snapshot_integrity_summary(
            conn,
            snapshot=snapshot,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
        runs.append(_run_summary(snapshot, integrity_summary=integrity_summary, include_debug=False))
    return {
        "status": "ok",
        "schema_version": SUPERSESSION_RUNS_SCHEMA_VERSION,
        "filters": base["filters"],
        "summary": {"total_returned": len(runs)},
        "runs": runs,
        "safety": {
            "read_only": True,
            "mutations_performed": 0,
        },
        "unsupported_metrics": [],
    }


def get_memory_supersession_run_payload(
    conn: Any,
    *,
    run_id: int,
    include_debug: bool = False,
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    snapshot = get_lifecycle_snapshot_payload(
        conn,
        snapshot_id=int(run_id),
        row_to_dict=row_to_dict,
    )
    integrity_summary = _apply_snapshot_integrity_summary(
        conn,
        snapshot=snapshot,
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
    )
    run_summary = _run_summary(snapshot, integrity_summary=integrity_summary, include_debug=include_debug)
    run_summary["run_status"] = run_summary.pop("status")
    return {
        "status": "ok",
        "schema_version": SUPERSESSION_RUN_SCHEMA_VERSION,
        **run_summary,
        "safety": {
            "read_only": True,
            "mutations_performed": 0,
        },
    }


def preview_memory_supersession_rollback_payload(
    conn: Any,
    *,
    run_id: int,
    include_debug: bool = False,
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    canonical_json_hash: Callable[[Any], str],
) -> dict[str, Any]:
    snapshot = get_lifecycle_snapshot_payload(
        conn,
        snapshot_id=int(run_id),
        row_to_dict=row_to_dict,
    )
    if snapshot["status"] == "rolled_back":
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_ROLLBACK_PREVIEW_SCHEMA_VERSION,
            "run_id": int(run_id),
            "blocking_reasons": ["run_already_rolled_back"],
            "safety": {"read_only": True, "mutations_performed": 0},
            "unsupported_metrics": [],
        }
    if snapshot["status"] != "applied":
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_ROLLBACK_PREVIEW_SCHEMA_VERSION,
            "run_id": int(run_id),
            "blocking_reasons": [f"run_status_{snapshot['status']}"],
            "safety": {"read_only": True, "mutations_performed": 0},
            "unsupported_metrics": [],
        }

    current = _current_pair_state(
        conn,
        new_memory_id=int(snapshot["new_memory_id"]),
        old_memory_id=int(snapshot["old_memory_id"]),
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
    )
    reasons = [
        *_compare_semantic_memory(snapshot["after_snapshot"]["new_memory"], current["memory_snapshot"]["new_memory"], label="new_memory"),
        *_compare_semantic_memory(snapshot["after_snapshot"]["old_memory"], current["memory_snapshot"]["old_memory"], label="old_memory"),
        *_compare_link_state(snapshot["link_snapshot"]["after"], current["link_snapshot"]),
    ]
    if reasons:
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_ROLLBACK_PREVIEW_SCHEMA_VERSION,
            "run_id": int(run_id),
            "blocking_reasons": ["after_snapshot_mismatch"],
            "mismatch_reasons": reasons,
            "safety": {"read_only": True, "mutations_performed": 0},
            "unsupported_metrics": [],
        }

    rollback_candidate_set_payload = {
        "run_id": int(run_id),
        "status": str(snapshot["status"]),
        "current_memory_snapshot": current["memory_snapshot"],
        "current_link_snapshot": current["link_snapshot"],
        "target_before_snapshot": snapshot["before_snapshot"],
    }
    rollback_candidate_set_fingerprint = canonical_json_hash(rollback_candidate_set_payload)
    planned_restoration = {
        "new_memory": snapshot["before_snapshot"]["new_memory"],
        "old_memory": snapshot["before_snapshot"]["old_memory"],
        "link_state": snapshot["link_snapshot"]["before"],
    }
    rollback_preview_hash = canonical_json_hash(
        {
            "run_id": int(run_id),
            "rollback_candidate_set_fingerprint": rollback_candidate_set_fingerprint,
            "planned_restoration": planned_restoration,
            "status": "preview_ready",
        }
    )
    result = {
        "status": "preview_ready",
        "schema_version": SUPERSESSION_ROLLBACK_PREVIEW_SCHEMA_VERSION,
        "run_id": int(run_id),
        "operation_key": str(snapshot["operation_key"]),
        "planned_restoration": planned_restoration,
        "rollback_candidate_set_fingerprint": rollback_candidate_set_fingerprint,
        "rollback_preview_hash": rollback_preview_hash,
        "hash_algorithm": MEMORY_V3_HASH_ALGORITHM,
        "safety": {"read_only": True, "mutations_performed": 0},
        "unsupported_metrics": [],
    }
    if include_debug:
        result["debug"] = {
            "current_memory_snapshot": current["memory_snapshot"],
            "current_link_snapshot": current["link_snapshot"],
            "after_snapshot": snapshot["after_snapshot"],
        }
    return result


def rollback_memory_supersession_run_payload(
    conn: Any,
    *,
    run_id: int,
    expected_rollback_preview_hash: str,
    rolled_back_by: str | None = None,
    notes: str | None = None,
    include_debug: bool = False,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    canonical_json_hash: Callable[[Any], str],
    utc_now_iso: Callable[[], str],
    insert_memory_event: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    normalized_expected_hash = normalize_required_text(
        expected_rollback_preview_hash,
        "expected_rollback_preview_hash",
    )
    snapshot = get_lifecycle_snapshot_payload(
        conn,
        snapshot_id=int(run_id),
        row_to_dict=row_to_dict,
    )
    if snapshot["status"] == "rolled_back":
        return {
            "status": "already_rolled_back",
            "schema_version": SUPERSESSION_ROLLBACK_SCHEMA_VERSION,
            "run_id": int(run_id),
            "blocking_reasons": [],
            "safety": {"read_only": True, "mutations_performed": 0},
            "unsupported_metrics": [],
        }

    preview = preview_memory_supersession_rollback_payload(
        conn,
        run_id=int(run_id),
        include_debug=include_debug,
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
        canonical_json_hash=canonical_json_hash,
    )
    if preview["status"] != "preview_ready":
        return {
            "status": "blocked",
            "schema_version": SUPERSESSION_ROLLBACK_SCHEMA_VERSION,
            "run_id": int(run_id),
            "blocking_reasons": list(preview.get("blocking_reasons") or []),
            "mismatch_reasons": list(preview.get("mismatch_reasons") or []),
            "safety": {"read_only": True, "mutations_performed": 0},
            "unsupported_metrics": list(preview.get("unsupported_metrics") or []),
        }
    if normalized_expected_hash != str(preview["rollback_preview_hash"]):
        return {
            "status": "stale_rollback_preview",
            "schema_version": SUPERSESSION_ROLLBACK_SCHEMA_VERSION,
            "run_id": int(run_id),
            "expected_rollback_preview_hash": normalized_expected_hash,
            "current_rollback_preview_hash": str(preview["rollback_preview_hash"]),
            "blocking_reasons": ["expected_rollback_preview_hash_mismatch"],
            "safety": {"read_only": True, "mutations_performed": 0},
            "unsupported_metrics": [],
        }

    rolled_back_at = utc_now_iso()
    try:
        conn.execute("BEGIN")
        current = _current_pair_state(
            conn,
            new_memory_id=int(snapshot["new_memory_id"]),
            old_memory_id=int(snapshot["old_memory_id"]),
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
        rollback_snapshot = {
            "before_rollback_memory_snapshot": current["memory_snapshot"],
            "before_rollback_link_snapshot": current["link_snapshot"],
            "before_rollback_event_counts": current["event_counts"],
        }

        conn.execute(
            """
            UPDATE memories
            SET supersedes_memory_id = ?,
                state_code = ?,
                memory_v2_status = ?,
                valid_to = ?,
                expired_due_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                snapshot["before_snapshot"]["new_memory"]["supersedes_memory_id"],
                snapshot["before_snapshot"]["new_memory"]["state_code"],
                snapshot["before_snapshot"]["new_memory"]["memory_v2_status"],
                snapshot["before_snapshot"]["new_memory"]["valid_to"],
                snapshot["before_snapshot"]["new_memory"]["expired_due_at"],
                snapshot["before_snapshot"]["new_memory"]["updated_at"],
                int(snapshot["new_memory_id"]),
            ),
        )
        conn.execute(
            """
            UPDATE memories
            SET superseded_by_memory_id = ?,
                state_code = ?,
                memory_v2_status = ?,
                valid_to = ?,
                expired_due_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                snapshot["before_snapshot"]["old_memory"]["superseded_by_memory_id"],
                snapshot["before_snapshot"]["old_memory"]["state_code"],
                snapshot["before_snapshot"]["old_memory"]["memory_v2_status"],
                snapshot["before_snapshot"]["old_memory"]["valid_to"],
                snapshot["before_snapshot"]["old_memory"]["expired_due_at"],
                snapshot["before_snapshot"]["old_memory"]["updated_at"],
                int(snapshot["old_memory_id"]),
            ),
        )

        before_link_state = snapshot["link_snapshot"]["before"]
        after_link_state = snapshot["link_snapshot"]["after"]
        created_link_id = after_link_state.get("created_link_id")
        if created_link_id is not None and before_link_state.get("active_direct_link_id") is None:
            conn.execute(
                "UPDATE memory_links SET archived_at = ? WHERE id = ?",
                (rolled_back_at, int(created_link_id)),
            )

        new_event = insert_memory_event(
            conn,
            memory_id=int(snapshot["new_memory_id"]),
            event_type="version.supersession_rolled_back",
            payload={
                "operation_key": str(snapshot["operation_key"]),
                "run_id": int(run_id),
                "rolled_back_at": rolled_back_at,
                "rolled_back_by": normalize_optional_text(rolled_back_by),
            },
        )
        old_event = insert_memory_event(
            conn,
            memory_id=int(snapshot["old_memory_id"]),
            event_type="version.supersession_rollback_restored",
            payload={
                "operation_key": str(snapshot["operation_key"]),
                "run_id": int(run_id),
                "rolled_back_at": rolled_back_at,
                "rolled_back_by": normalize_optional_text(rolled_back_by),
            },
        )

        rollback_snapshot["created_event_ids"] = {
            "new_memory": int(new_event["id"]),
            "old_memory": int(old_event["id"]),
        }
        rollback_snapshot["created_event_types"] = Counter(
            [str(new_event["event_type"]), str(old_event["event_type"])]
        )
        rollback_snapshot["after_rollback_memory_snapshot"] = _current_pair_state(
            conn,
            new_memory_id=int(snapshot["new_memory_id"]),
            old_memory_id=int(snapshot["old_memory_id"]),
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )["memory_snapshot"]
        rollback_snapshot["after_rollback_link_snapshot"] = _current_pair_state(
            conn,
            new_memory_id=int(snapshot["new_memory_id"]),
            old_memory_id=int(snapshot["old_memory_id"]),
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )["link_snapshot"]

        updated_snapshot = mark_lifecycle_snapshot_rolled_back_payload(
            conn,
            snapshot_id=int(run_id),
            rollback_preview_hash=normalized_expected_hash,
            rollback_snapshot=rollback_snapshot,
            rolled_back_at=rolled_back_at,
            rolled_back_by=normalize_optional_text(rolled_back_by),
            rollback_note=normalize_optional_text(notes),
            utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return {
            "status": "error",
            "schema_version": SUPERSESSION_ROLLBACK_SCHEMA_VERSION,
            "run_id": int(run_id),
            "error": str(exc),
            "unsupported_metrics": [],
        }

    result = {
        "status": "rolled_back",
        "schema_version": SUPERSESSION_ROLLBACK_SCHEMA_VERSION,
        "run_id": int(run_id),
        "operation_key": str(snapshot["operation_key"]),
        "rolled_back_at": rolled_back_at,
        "rolled_back_by": normalize_optional_text(rolled_back_by),
        "rollback_preview_hash": normalized_expected_hash,
        "snapshot_status": updated_snapshot["status"],
        "unsupported_metrics": [],
    }
    if include_debug:
        result["debug"] = {
            "rollback_snapshot": updated_snapshot["rollback_snapshot"],
        }
    return result
