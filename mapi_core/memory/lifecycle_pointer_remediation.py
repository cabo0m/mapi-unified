from __future__ import annotations

from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from mapi_core.memory.lifecycle_contracts import derive_canonical_memory_state
from mapi_core.memory.lifecycle_pointer_baseline_contract import (
    pointer_memory_immutable_identity_fingerprint,
    validate_pointer_lifecycle_baseline_contract,
)


POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION = "legacy_pointer_only_supersession_2026_07_17_v2"
POINTER_LIFECYCLE_INVENTORY_SCHEMA_VERSION = "memory_v3_pointer_lifecycle_remediation_inventory.v2"
POINTER_LIFECYCLE_PREVIEW_SCHEMA_VERSION = "memory_v3_pointer_lifecycle_remediation_preview.v2"
POINTER_LIFECYCLE_VALIDATION_SOURCE = "memory_v3_pointer_lineage_remediation"
POINTER_LIFECYCLE_LINK_WEIGHT = 0.82
POINTER_LIFECYCLE_TIMESTAMP_POLICY_VERSION = "provenance_aware_timestamp_policy_2026_07_19_v1"
GUARDED_SUPERSESSION_ORIGIN_PREFIX = "memory_v3_supersession:supersession:"
ACCEPTANCE_AUDIT_SUPERSESSION_ORIGIN = "codex_v3_pointer_lifecycle_0027_acceptance_audit"

PUBLIC_MEMORY_FIELDS = (
    "id", "project_key", "scope_code", "workspace_id", "state_code", "memory_v2_status",
    "activity_state", "archived_at", "supersedes_memory_id", "superseded_by_memory_id",
    "created_at", "valid_to", "expired_due_at", "validation_source", "memory_type",
    "entry_type", "truth_kind", "requires_user_confirmation", "visibility_scope", "title",
    "summary_short", "importance_level", "layer_code",
)
BLOCKER_PRIORITY = (
    "missing_parent", "self_reference", "cycle", "branching_multiple_successors",
    "multiple_predecessors", "chronology_violation", "reverse_pointer_conflict",
    "active_link_conflict", "archived_link_conflict", "conflicting_valid_to",
    "conflicting_expired_due_at", "unsupported_old_state", "unsupported_new_state",
    "archived_or_historical_component", "cross_workspace", "cross_scope", "cross_project",
)
LINK_CREATED_EVENT = "version.pointer_only_supersession_link_created"
REVERSE_POINTER_REPAIRED_EVENT = "version.pointer_only_reverse_pointer_repaired"
SUPERSEDED_STATE_REPAIRED_EVENT = "version.pointer_only_superseded_state_repaired"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _parse_iso(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _public_memory(row: dict[str, Any]) -> dict[str, Any]:
    public = {field: row.get(field) for field in PUBLIC_MEMORY_FIELDS}
    public["content_sha256"] = hashlib.sha256(str(row.get("content") or "").encode("utf-8")).hexdigest()
    public["immutable_identity_fingerprint"] = pointer_memory_immutable_identity_fingerprint(row)
    return public


def _safe_state(row: dict[str, Any]) -> str | None:
    try:
        return derive_canonical_memory_state(
            state_code=row.get("state_code"),
            activity_state=row.get("activity_state"),
            contradiction_flag=row.get("contradiction_flag"),
        )
    except ValueError:
        return None


def _unsupported(plan_version: str, schema_version: str) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "status": "unsupported_plan_version",
        "plan_version": plan_version,
        "expected_plan_version": POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
        "safety": {
            "read_only": True, "mutations_performed": 0, "apply_supported": False,
            "apply_block_reason": "phase_1_review_only",
        },
    }


def _load_graph(conn: Any) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    memories = {int(row["id"]): _row_dict(row) for row in conn.execute("SELECT * FROM memories").fetchall()}
    links = [
        _row_dict(row)
        for row in conn.execute(
            "SELECT * FROM memory_links WHERE lower(trim(relation_type)) = 'supersedes' ORDER BY id"
        ).fetchall()
    ]
    return memories, links


def _relation_records(memories: dict[int, dict[str, Any]], links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def link_workspace_matches_endpoints(link: dict[str, Any]) -> bool:
        new = memories[int(link["from_memory_id"])]
        old = memories[int(link["to_memory_id"])]
        common_workspace = new.get("workspace_id") if new.get("workspace_id") == old.get("workspace_id") else None
        return common_workspace is not None and link.get("workspace_id") in {None, common_workspace}

    active_pairs = {
        (int(link["from_memory_id"]), int(link["to_memory_id"]))
        for link in links
        if link.get("archived_at") is None
        and int(link["from_memory_id"]) in memories
        and int(link["to_memory_id"]) in memories
        and link_workspace_matches_endpoints(link)
    }
    records = []
    for memory_id in sorted(memories):
        row = memories[memory_id]
        if row.get("supersedes_memory_id") is None:
            continue
        old_id = int(row["supersedes_memory_id"])
        records.append(
            {
                "new_memory_id": memory_id,
                "old_memory_id": old_id,
                "active_supersedes_link_present": (memory_id, old_id) in active_pairs,
                "pointer_only": (memory_id, old_id) not in active_pairs,
            }
        )
    return records


def _components(
    memories: dict[int, dict[str, Any]], links: list[dict[str, Any]], relations: list[dict[str, Any]]
) -> list[set[int]]:
    adjacency: dict[int, set[int]] = defaultdict(set)
    seeds: set[int] = set()

    def connect(left: int, right: int) -> None:
        seeds.update((left, right))
        adjacency[left].add(right)
        adjacency[right].add(left)

    for relation in relations:
        connect(int(relation["new_memory_id"]), int(relation["old_memory_id"]))
    for memory_id, row in memories.items():
        if row.get("superseded_by_memory_id") is not None:
            connect(int(row["superseded_by_memory_id"]), memory_id)
    for link in links:
        connect(int(link["from_memory_id"]), int(link["to_memory_id"]))

    result: list[set[int]] = []
    unseen = set(seeds)
    while unseen:
        root = min(unseen)
        component: set[int] = set()
        queue = deque([root])
        while queue:
            node = queue.popleft()
            if node in component:
                continue
            component.add(node)
            unseen.discard(node)
            queue.extend(sorted(adjacency[node] - component))
        result.append(component)
    return sorted(result, key=lambda item: (min(item), len(item)))


def _protected(rows: list[dict[str, Any]]) -> bool:
    return any(
        bool(row.get("requires_user_confirmation"))
        or str(row.get("importance_level") or "").lower() == "critical"
        or str(row.get("layer_code") or "").lower() == "identity"
        or str(row.get("truth_kind") or "").lower() == "decision"
        or str(row.get("visibility_scope") or "").lower() == "private"
        for row in rows
    )


def _has_cycle(edges: set[tuple[int, int]]) -> bool:
    graph: dict[int, set[int]] = defaultdict(set)
    for new_id, old_id in edges:
        graph[new_id].add(old_id)
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(node: int) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(parent) for parent in graph[node]):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in sorted(graph))


def _boundary_issues(
    edges: set[tuple[int, int]], memories: dict[int, dict[str, Any]]
) -> set[str]:
    issues: set[str] = set()
    for new_id, old_id in edges:
        new = memories.get(new_id)
        old = memories.get(old_id)
        if new is None or old is None:
            continue
        for field, code in (
            ("workspace_id", "cross_workspace"),
            ("scope_code", "cross_scope"),
            ("project_key", "cross_project"),
        ):
            if new.get(field) != old.get(field):
                issues.add(code)
    return issues


def _timestamp_status(value: Any, expected: str) -> str:
    if value is None:
        return "missing_repairable"
    try:
        return "exact" if _format_iso(_parse_iso(value)) == expected else "conflicting"
    except (TypeError, ValueError):
        return "conflicting"


def _provenance_aware_timestamp_assessment(
    *, old: dict[str, Any], new_created: datetime, exact_active: list[dict[str, Any]]
) -> dict[str, Any]:
    successor_valid_to = _format_iso(new_created)
    successor_expired = _format_iso(new_created + timedelta(days=2))
    valid_to_status = _timestamp_status(old.get("valid_to"), successor_valid_to)
    expired_status = _timestamp_status(old.get("expired_due_at"), successor_expired)
    result = {
        "valid_to_status": valid_to_status,
        "expired_due_at_status": expired_status,
        "timestamp_policy_version": POINTER_LIFECYCLE_TIMESTAMP_POLICY_VERSION,
        "timestamp_anchor": "successor.created_at",
        "timestamp_provenance_status": "same_or_unknown_provenance",
        "original_timestamps_preserved": True,
    }
    if len(exact_active) != 1:
        return result
    link = exact_active[0]
    origin = str(link.get("origin") or "")
    is_guarded_supersession = origin.startswith(GUARDED_SUPERSESSION_ORIGIN_PREFIX)
    is_acceptance_audit_supersession = origin == ACCEPTANCE_AUDIT_SUPERSESSION_ORIGIN
    if not is_guarded_supersession and not is_acceptance_audit_supersession:
        return result
    try:
        applied_at = _parse_iso(link.get("created_at"))
    except (TypeError, ValueError):
        return result
    applied_valid_to = _format_iso(applied_at)
    applied_expired = _format_iso(applied_at + timedelta(days=2))
    applied_valid_status = _timestamp_status(old.get("valid_to"), applied_valid_to)
    applied_expired_status = _timestamp_status(old.get("expired_due_at"), applied_expired)
    if is_acceptance_audit_supersession and old.get("expired_due_at") is None:
        applied_expired_status = "exact"
    if applied_valid_status == "exact":
        result["valid_to_status"] = "exact"
    if applied_expired_status == "exact":
        result["expired_due_at_status"] = "exact"
    if applied_valid_status == "exact" and applied_expired_status in {"exact", "missing_repairable"}:
        result.update({
            "timestamp_anchor": (
                "acceptance_audit_supersession_link.created_at"
                if is_acceptance_audit_supersession
                else "guarded_supersession_link.created_at"
            ),
            "timestamp_provenance_status": (
                "operator_authorized_acceptance_audit_provenance"
                if is_acceptance_audit_supersession
                else "explained_distinct_processing_provenance"
            ),
            "expired_due_at_semantics": (
                "not_applicable_supersession_termination"
                if is_acceptance_audit_supersession
                else "scheduled_two_days_after_apply"
            ),
        })
    return result


def _evaluate_edge(
    new_id: int,
    old_id: int,
    memories: dict[int, dict[str, Any]],
    active_links: list[dict[str, Any]],
    archived_links: list[dict[str, Any]],
) -> dict[str, Any]:
    new = memories.get(new_id)
    old = memories.get(old_id)
    if new is None or old is None:
        return {
            "new_memory_id": new_id,
            "old_memory_id": old_id,
            "link_status": "unsupported",
            "reverse_pointer_status": "unsupported",
            "old_state_status": "unsupported",
            "valid_to_status": "unsupported",
            "expired_due_at_status": "unsupported",
            "boundary_status": "unsupported",
            "chronology_status": "unsupported",
            "canonical_part_count": 0,
            "repairable_part_count": 0,
            "complete": False,
        }

    matching_active = [
        link for link in active_links
        if int(link["from_memory_id"]) == new_id and int(link["to_memory_id"]) == old_id
    ]
    matching_archived = [
        link for link in archived_links
        if int(link["from_memory_id"]) == new_id and int(link["to_memory_id"]) == old_id
    ]
    exact_active = [
        link for link in matching_active
        if new.get("workspace_id") == old.get("workspace_id")
        and link.get("workspace_id") in {None, new.get("workspace_id")}
    ]
    conflicting_active = [
        link for link in active_links
        if (int(link["from_memory_id"]) == new_id and int(link["to_memory_id"]) != old_id)
        or (int(link["to_memory_id"]) == old_id and int(link["from_memory_id"]) != new_id)
    ]
    if matching_archived or conflicting_active or len(matching_active) > 1 or (
        matching_active and len(exact_active) != 1
    ):
        link_status = "conflicting"
    elif len(exact_active) == 1:
        link_status = "exact"
    else:
        link_status = "missing_repairable"

    reverse = old.get("superseded_by_memory_id")
    if reverse is None:
        reverse_status = "missing_repairable"
    elif int(reverse) == new_id:
        reverse_status = "exact"
    else:
        reverse_status = "conflicting"

    old_state = _safe_state(old)
    old_v2_status = str(old.get("memory_v2_status") or "").strip().lower()
    if old_state == "superseded" and old_v2_status == "superseded":
        old_state_status = "exact"
    elif old_state == "validated" and old_v2_status in {"", "active"}:
        old_state_status = "missing_repairable"
    else:
        old_state_status = "unsupported"

    same_boundary = all(
        new.get(field) == old.get(field) for field in ("workspace_id", "scope_code", "project_key")
    )
    boundary_status = "exact" if same_boundary else "conflicting"
    try:
        new_created = _parse_iso(new.get("created_at"))
        old_created = _parse_iso(old.get("created_at"))
        chronology_status = "exact" if new_created > old_created else "conflicting"
        timestamp_assessment = _provenance_aware_timestamp_assessment(
            old=old, new_created=new_created, exact_active=exact_active
        )
        valid_to_status = timestamp_assessment["valid_to_status"]
        expired_status = timestamp_assessment["expired_due_at_status"]
    except (TypeError, ValueError):
        chronology_status = "conflicting"
        valid_to_status = "unsupported"
        expired_status = "unsupported"
        timestamp_assessment = {
            "timestamp_policy_version": POINTER_LIFECYCLE_TIMESTAMP_POLICY_VERSION,
            "timestamp_anchor": "unsupported",
            "timestamp_provenance_status": "unsupported",
            "original_timestamps_preserved": True,
        }

    statuses = (
        link_status,
        reverse_status,
        old_state_status,
        valid_to_status,
        expired_status,
    )
    return {
        "new_memory_id": new_id,
        "old_memory_id": old_id,
        "link_status": link_status,
        "reverse_pointer_status": reverse_status,
        "old_state_status": old_state_status,
        "valid_to_status": valid_to_status,
        "expired_due_at_status": expired_status,
        **timestamp_assessment,
        "boundary_status": boundary_status,
        "chronology_status": chronology_status,
        "canonical_part_count": sum(status == "exact" for status in statuses),
        "repairable_part_count": sum(status == "missing_repairable" for status in statuses),
        "complete": all(status == "exact" for status in statuses)
        and boundary_status == "exact" and chronology_status == "exact",
    }


def _classify_component(
    node_ids: set[int], memories: dict[int, dict[str, Any]], links: list[dict[str, Any]]
) -> dict[str, Any]:
    existing_ids = sorted(node_id for node_id in node_ids if node_id in memories)
    rows = [memories[node_id] for node_id in existing_ids]
    forward_edges = {
        (node_id, int(memories[node_id]["supersedes_memory_id"]))
        for node_id in existing_ids if memories[node_id].get("supersedes_memory_id") is not None
    }
    reverse_edges = {
        (int(memories[node_id]["superseded_by_memory_id"]), node_id)
        for node_id in existing_ids if memories[node_id].get("superseded_by_memory_id") is not None
    }
    component_links = [
        link for link in links
        if int(link["from_memory_id"]) in node_ids or int(link["to_memory_id"]) in node_ids
    ]
    active_links = [link for link in component_links if link.get("archived_at") is None]
    archived_links = [link for link in component_links if link.get("archived_at") is not None]
    active_edges = {(int(link["from_memory_id"]), int(link["to_memory_id"])) for link in active_links}
    archived_edges = {(int(link["from_memory_id"]), int(link["to_memory_id"])) for link in archived_links}
    all_edges = forward_edges | reverse_edges | active_edges | archived_edges
    issue_codes: set[str] = set()

    if any(node_id not in memories for node_id in node_ids):
        issue_codes.add("missing_parent")
    if any(new_id == old_id for new_id, old_id in all_edges):
        issue_codes.add("self_reference")
    if _has_cycle(all_edges):
        issue_codes.add("cycle")

    old_to_new: dict[int, set[int]] = defaultdict(set)
    new_to_old: dict[int, set[int]] = defaultdict(set)
    for new_id, old_id in all_edges:
        old_to_new[old_id].add(new_id)
        new_to_old[new_id].add(old_id)
    if any(len(successors) > 1 for successors in old_to_new.values()):
        issue_codes.add("branching_multiple_successors")
    if any(len(predecessors) > 1 for predecessors in new_to_old.values()):
        issue_codes.add("multiple_predecessors")

    issue_codes.update(_boundary_issues(all_edges, memories))
    edge_assessments = []
    for new_id, old_id in sorted(forward_edges):
        new = memories.get(new_id)
        old = memories.get(old_id)
        if new is None or old is None:
            continue
        if new_id == old_id:
            continue
        assessment = _evaluate_edge(new_id, old_id, memories, active_links, archived_links)
        edge_assessments.append(assessment)
        if assessment["chronology_status"] != "exact":
            issue_codes.add("chronology_violation")
        if assessment["reverse_pointer_status"] == "conflicting":
            issue_codes.add("reverse_pointer_conflict")
        if (new_id, old_id) in archived_edges:
            issue_codes.add("archived_link_conflict")
        elif assessment["link_status"] == "conflicting":
            issue_codes.add("active_link_conflict")
        if assessment["valid_to_status"] == "conflicting":
            issue_codes.add("conflicting_valid_to")
        if assessment["expired_due_at_status"] == "conflicting":
            issue_codes.add("conflicting_expired_due_at")
        if assessment["old_state_status"] == "unsupported":
            issue_codes.add("unsupported_old_state")
        new_state = _safe_state(new)
        if new_state not in {"validated", "superseded"}:
            issue_codes.add("unsupported_new_state")
        if any(
            str(item.get("activity_state") or "active").lower() != "active" or item.get("archived_at") is not None
            for item in (old, new)
        ):
            issue_codes.add("archived_or_historical_component")

    ordered_issues = [code for code in BLOCKER_PRIORITY if code in issue_codes]
    if ordered_issues:
        classification = ordered_issues[0]
    elif edge_assessments and all(edge["complete"] for edge in edge_assessments):
        classification = "already_canonical"
    elif any(edge["canonical_part_count"] for edge in edge_assessments):
        classification = "partially_canonical_linear"
    else:
        classification = "safe_linear_same_boundary"

    public_rows = [_public_memory(memories[node_id]) for node_id in existing_ids]
    pointer_relations = [
        {
            "new_memory_id": new_id,
            "old_memory_id": old_id,
            "active_supersedes_link_present": (new_id, old_id) in active_edges,
            "pointer_only": (new_id, old_id) not in active_edges,
        }
        for new_id, old_id in sorted(forward_edges)
    ]
    return {
        "component_id": _hash(sorted(node_ids))[:16],
        "memory_ids": sorted(node_ids),
        "memories": public_rows,
        "pointer_relations": pointer_relations,
        "edge_assessments": edge_assessments,
        "active_supersedes_link_ids": sorted(int(link["id"]) for link in active_links),
        "archived_supersedes_link_ids": sorted(int(link["id"]) for link in archived_links),
        "classification": classification,
        "issue_codes": ordered_issues,
        "protected_review_required": _protected(rows),
        "candidate_edges": [edge for edge in edge_assessments if not edge["complete"]]
        if classification in {"safe_linear_same_boundary", "partially_canonical_linear"} else [],
    }


def _event_ledger(conn: Any, memory_ids: list[int]) -> dict[str, Any]:
    if not memory_ids:
        rows: list[Any] = []
    else:
        placeholders = ",".join("?" for _ in memory_ids)
        rows = conn.execute(
            f"SELECT id, memory_id, event_type, created_at, payload_json FROM memory_events "
            f"WHERE memory_id IN ({placeholders}) ORDER BY id",
            tuple(memory_ids),
        ).fetchall()
    entries = [
        {
            "event_id": int(row["id"]),
            "memory_id": int(row["memory_id"]),
            "event_type": row["event_type"],
            "created_at": row["created_at"],
            "payload_sha256": hashlib.sha256(str(row["payload_json"] or "").encode("utf-8")).hexdigest(),
        }
        for row in rows
    ]
    return {
        "target_event_count": len(entries),
        "target_event_max_id": max((entry["event_id"] for entry in entries), default=0),
        "target_event_ledger_fingerprint": _hash(entries),
        "entries": entries,
    }


def get_memory_pointer_lifecycle_remediation_inventory_payload(
    conn: Any, *, plan_version: str = POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
    include_debug: bool = False,
) -> dict[str, Any]:
    if plan_version != POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION:
        return _unsupported(plan_version, POINTER_LIFECYCLE_INVENTORY_SCHEMA_VERSION)
    memories, links = _load_graph(conn)
    relations = _relation_records(memories, links)
    components = [_classify_component(nodes, memories, links) for nodes in _components(memories, links, relations)]
    relevant = [component for component in components if component["pointer_relations"]]
    safe = [
        component for component in relevant
        if component["classification"] in {"safe_linear_same_boundary", "partially_canonical_linear"}
        and component["candidate_edges"]
    ]
    canonical = [component for component in relevant if component["classification"] == "already_canonical"]
    blocked = [component for component in relevant if component not in safe and component not in canonical]
    pointer_only = [relation for relation in relations if relation["pointer_only"]]
    classification_counts = dict(sorted(Counter(item["classification"] for item in relevant).items()))
    component_basis = [
        {key: component[key] for key in (
            "component_id", "memory_ids", "pointer_relations", "active_supersedes_link_ids",
            "archived_supersedes_link_ids", "classification", "issue_codes", "protected_review_required",
            "edge_assessments", "candidate_edges",
        )}
        for component in relevant
    ]
    safe_memory_ids = sorted({
        memory_id for component in safe for memory_id in component["memory_ids"] if memory_id in memories
    })
    safe_event_ledger = _event_ledger(conn, safe_memory_ids)
    current_count = len(pointer_only)
    candidate_relations = {
        (int(edge["new_memory_id"]), int(edge["old_memory_id"]))
        for component in safe for edge in component["candidate_edges"]
    }
    baseline_contract = validate_pointer_lifecycle_baseline_contract(
        conn,
        observed_pointer_only_relations={
            (int(item["new_memory_id"]), int(item["old_memory_id"])) for item in pointer_only
        },
        observed_forward_relations={
            (int(item["new_memory_id"]), int(item["old_memory_id"])) for item in relations
        },
        candidate_relations=candidate_relations,
    )
    input_basis = {
        "plan_version": plan_version,
        "relations": relations,
        "components": component_basis,
        "baseline_contract": baseline_contract,
    }
    fingerprints = {
        "input_fingerprint": _hash(input_basis),
        "all_pointer_relations_fingerprint": _hash(relations),
        "component_set_fingerprint": _hash(component_basis),
        "safe_candidate_set_fingerprint": _hash({
            "components": [
                {
                    "component_id": item["component_id"],
                    "candidate_edges": item["candidate_edges"],
                    "protected_review_required": item["protected_review_required"],
                }
                for item in safe
            ],
            "target_event_ledger_fingerprint": safe_event_ledger["target_event_ledger_fingerprint"],
            "baseline_contract_fingerprint": baseline_contract["contract_fingerprint"],
        }),
        "blocked_component_set_fingerprint": _hash([
            {"component_id": item["component_id"], "classification": item["classification"], "issue_codes": item["issue_codes"]}
            for item in blocked
        ]),
    }
    result = {
        "schema_version": POINTER_LIFECYCLE_INVENTORY_SCHEMA_VERSION,
        "status": "inventory_ready",
        "plan_version": plan_version,
        "summary": {
            "forward_pointer_count": len(relations),
            "pointer_only_relation_count": current_count,
            "component_count": len(relevant),
            "safe_component_count": len(safe),
            "blocked_component_count": len(blocked),
            "already_canonical_count": len(canonical),
            "protected_review_component_count": sum(bool(item["protected_review_required"]) for item in safe),
        },
        "baseline": {
            "baseline_reported_count": baseline_contract["baseline_inventory_count"],
            "current_count": current_count,
            "difference": current_count - baseline_contract["baseline_inventory_count"],
            "difference_explanation": (
                "exact_set_reconciled" if baseline_contract["contract_status"] == "accepted"
                else "exact_set_reconciliation_blocked"
            ),
        },
        "baseline_contract": baseline_contract,
        "warnings": list(baseline_contract["blocking_reasons"]),
        "classification_counts": classification_counts,
        "event_ledger": {
            key: value for key, value in safe_event_ledger.items() if key != "entries"
        },
        "safe_components": safe,
        "blocked_components": blocked,
        "already_canonical_components": canonical,
        "fingerprints": fingerprints,
        "safety": {
            "read_only": True, "mutations_performed": 0, "apply_supported": False,
            "apply_block_reason": "phase_1_review_only",
        },
    }
    if include_debug:
        result["debug"] = {"all_components": relevant, "all_pointer_relations": relations}
    return result


def _projected_change(component: dict[str, Any], memories: dict[int, dict[str, Any]]) -> dict[str, Any]:
    edge_changes = []
    for edge in component["candidate_edges"]:
        new_id = int(edge["new_memory_id"])
        old_id = int(edge["old_memory_id"])
        new = memories[new_id]
        old = memories[old_id]
        expected_valid_to = _format_iso(_parse_iso(new["created_at"]))
        expected_expired = _format_iso(_parse_iso(new["created_at"]) + timedelta(days=2))
        reverse_update = None
        if edge["reverse_pointer_status"] == "missing_repairable":
            reverse_update = {
                "superseded_by_memory_id": {"before": None, "after": new_id},
            }
        state_update: dict[str, dict[str, Any]] = {}
        if edge["old_state_status"] == "missing_repairable":
            if old.get("state_code") != "superseded":
                state_update["state_code"] = {"before": old.get("state_code"), "after": "superseded"}
            if old.get("memory_v2_status") != "superseded":
                state_update["memory_v2_status"] = {
                    "before": old.get("memory_v2_status"), "after": "superseded",
                }
        if edge["valid_to_status"] == "missing_repairable":
            state_update["valid_to"] = {"before": None, "after": expected_valid_to}
        if edge["expired_due_at_status"] == "missing_repairable":
            state_update["expired_due_at"] = {"before": None, "after": expected_expired}
        if state_update and old.get("validation_source") != POINTER_LIFECYCLE_VALIDATION_SOURCE:
            state_update["validation_source"] = {
                "before": old.get("validation_source"),
                "after": POINTER_LIFECYCLE_VALIDATION_SOURCE,
            }

        projected_link_create = None
        if edge["link_status"] == "missing_repairable":
            projected_link_create = {
                "from_memory_id": new_id,
                "to_memory_id": old_id,
                "relation_type": "supersedes",
                "weight": POINTER_LIFECYCLE_LINK_WEIGHT,
                "origin": POINTER_LIFECYCLE_VALIDATION_SOURCE,
                "workspace_id": new.get("workspace_id"),
                "visibility_scope": "inherited",
                "archived_at": None,
            }

        memory_updates = dict(reverse_update or {})
        memory_updates.update(state_update)
        event_base = {
            "required_payload_fields": [
                "remediation_run_id", "operation_key", "plan_version", "candidate_set_fingerprint",
                "new_memory_id", "old_memory_id", "applied_by", "reason", "before_field_hash",
                "after_field_hash",
            ],
            "new_memory_id": new_id,
            "old_memory_id": old_id,
        }
        projected_events = []

        def add_event(event_type: str, changes: dict[str, Any]) -> None:
            projected_events.append({
                **event_base,
                "event_type": event_type,
                "changed_fields": sorted(changes),
                "before_field_hash": _hash({key: changes[key]["before"] for key in sorted(changes)}),
                "after_field_hash": _hash({key: changes[key]["after"] for key in sorted(changes)}),
            })

        if projected_link_create is not None:
            link_changes = {
                key: {"before": None, "after": value}
                for key, value in projected_link_create.items()
            }
            add_event(LINK_CREATED_EVENT, link_changes)
        if reverse_update is not None:
            add_event(REVERSE_POINTER_REPAIRED_EVENT, reverse_update)
        if state_update:
            add_event(SUPERSEDED_STATE_REPAIRED_EVENT, state_update)
        edge_changes.append(
            {
                "new_memory_id": new_id,
                "old_memory_id": old_id,
                "preserve_new_supersedes_memory_id": old_id,
                "edge_status": edge,
                "projected_link_create": projected_link_create,
                "projected_reverse_pointer_update": reverse_update,
                "projected_state_update": state_update or None,
                "changed_fields": sorted(memory_updates),
                "memory_updates": memory_updates,
                "mutation_count": sum(
                    value is not None for value in (projected_link_create, reverse_update, state_update or None)
                ),
                "projected_events": projected_events,
            }
        )
    return {
        "component_id": component["component_id"],
        "classification": component["classification"],
        "protected_review_required": component["protected_review_required"],
        "edges": edge_changes,
    }


def preview_memory_pointer_lifecycle_remediation_payload(
    conn: Any, *, plan_version: str = POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
    include_debug: bool = False,
) -> dict[str, Any]:
    if plan_version != POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION:
        return _unsupported(plan_version, POINTER_LIFECYCLE_PREVIEW_SCHEMA_VERSION)
    inventory = get_memory_pointer_lifecycle_remediation_inventory_payload(
        conn, plan_version=plan_version, include_debug=include_debug
    )
    memories, _ = _load_graph(conn)
    projected_changes = [_projected_change(component, memories) for component in inventory["safe_components"]]
    safe_memory_ids = sorted({
        memory_id for component in inventory["safe_components"] for memory_id in component["memory_ids"]
        if memory_id in memories
    })
    ledger = _event_ledger(conn, safe_memory_ids)
    safe_basis = {
        "inventory_safe_candidate_set_fingerprint": inventory["fingerprints"]["safe_candidate_set_fingerprint"],
        "projected_changes": projected_changes,
        "target_event_ledger_fingerprint": ledger["target_event_ledger_fingerprint"],
    }
    safe_candidate_fingerprint = _hash(safe_basis)
    input_fingerprint = inventory["fingerprints"]["input_fingerprint"]
    operation_key = f"pointer_lineage_remediation:{plan_version}:{input_fingerprint}"
    baseline_contract = inventory["baseline_contract"]
    baseline_results = [
        {
            "new_memory_id": item["new_memory_id"],
            "old_memory_id": item["old_memory_id"],
            "matched": item["accepted"],
            "classification": item["state"],
            "issue_codes": [] if item["accepted"] else ["baseline_anchor_not_accepted"],
            "evidence": item["evidence"],
        }
        for item in baseline_contract["anchor_states"]
    ]
    status = (
        "preview_ready" if baseline_contract["contract_status"] == "accepted"
        else "preview_blocked_expected_baseline_mismatch"
    )
    safe_edges = sum(len(change["edges"]) for change in projected_changes)
    projected_edges = [edge for change in projected_changes for edge in change["edges"]]
    projected_memory_updates = sum(bool(edge["memory_updates"]) for edge in projected_edges)
    preview_basis = {
        "schema_version": POINTER_LIFECYCLE_PREVIEW_SCHEMA_VERSION,
        "status": status,
        "plan_version": plan_version,
        "operation_key": operation_key,
        "input_fingerprint": input_fingerprint,
        "safe_candidate_set_fingerprint": safe_candidate_fingerprint,
        "blocked_component_set_fingerprint": inventory["fingerprints"]["blocked_component_set_fingerprint"],
        "projected_changes": projected_changes,
        "event_ledger": {key: value for key, value in ledger.items() if key != "entries"},
        "baseline_results": baseline_results,
        "baseline_contract": baseline_contract,
    }
    result = dict(preview_basis)
    result["preview_hash"] = _hash(preview_basis)
    result["summary"] = {
        "safe_components": len(inventory["safe_components"]),
        "safe_edges": safe_edges,
        "blocked_components": len(inventory["blocked_components"]),
        "projected_memory_updates": projected_memory_updates,
        "projected_link_creates": sum(edge["projected_link_create"] is not None for edge in projected_edges),
        "projected_events": sum(len(edge["projected_events"]) for edge in projected_edges),
    }
    result["blocked_components"] = inventory["blocked_components"]
    result["event_ledger"] = ledger
    result["safety"] = {
        "read_only": True, "mutations_performed": 0, "backup_created": False,
        "snapshot_created": False, "apply_supported": False, "apply_block_reason": "phase_1_review_only",
    }
    if include_debug:
        result["debug"] = {"inventory_fingerprints": inventory["fingerprints"]}
    return result
