from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable


POINTER_LIFECYCLE_BASELINE_CONTRACT_SCHEMA_VERSION = "pointer_lifecycle_baseline_contract.v3"
POINTER_LIFECYCLE_BASELINE_SOURCE = (
    "verified online backup agent_memory-v3-pointer-lifecycle-pre-20260719054128-7ce20e05dc.db "
    "sha256=ffeddb0c6ae15dc4567892157ee38ce0904f2c11689fff4c6842c34f5f91be9d"
)

RelationKey = tuple[int, int]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def normalize_relation_set(relations: Iterable[RelationKey]) -> list[dict[str, Any]]:
    return [
        {"new_memory_id": new_id, "old_memory_id": old_id, "relation_type": "supersedes"}
        for new_id, old_id in sorted({(int(new_id), int(old_id)) for new_id, old_id in relations})
    ]


def relation_set_fingerprint(relations: Iterable[RelationKey]) -> str:
    return _hash(normalize_relation_set(relations))


def pointer_memory_immutable_identity_fingerprint(row: Any) -> str:
    """Hash the immutable pointer-lifecycle identity shared by planning and audit."""
    return _hash({
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "source": row.get("source"),
        "memory_type": row.get("memory_type"),
        "content": row.get("content"),
    })


BASELINE_POINTER_ONLY_RELATIONS: tuple[RelationKey, ...] = (
    (139, 138), (300, 299), (881, 880), (1400, 1399), (1435, 1434),
    (1438, 1437), (1515, 1514), (1547, 1546), (1548, 1545),
    (1699, 1696), (1700, 1697), (1702, 1700), (1709, 1701),
    (1710, 1702), (1741, 1739), (1770, 1767), (1771, 1770),
    (1794, 1771), (1804, 1794), (1805, 1804), (1806, 1805),
    (1807, 1806), (1808, 1807), (1816, 1808), (1817, 1816),
    (1818, 1817), (1819, 1818), (1820, 1819), (1821, 1820),
    (1822, 1821), (1823, 1822), (1824, 1823), (1826, 1824),
    (1828, 1827), (1829, 1826), (1831, 1829), (1834, 1831),
    (1835, 1834), (1837, 1835), (1839, 1837), (1840, 1839),
    (1841, 1840), (1842, 1841), (1843, 1842), (1844, 1843),
    (1845, 1844), (1846, 1845), (1847, 1846), (1851, 878),
    (1852, 1851), (1854, 1853), (1855, 1854), (1856, 1855),
    (1857, 1856), (1858, 1857), (1859, 1858), (1860, 1859),
    (1861, 1860), (1980, 1847),
)
BASELINE_POINTER_ONLY_FINGERPRINT = "0d5fb4de5ebb686efce31361feed96256bd96be4114d9bb01737c9c5a7402d80"
BASELINE_ANCHORS: tuple[RelationKey, ...] = ((139, 138), (881, 880), (1400, 1399))

EXPECTED_EVENT_TYPES = {
    "version.pointer_only_supersession_link_created",
    "version.pointer_only_reverse_pointer_repaired",
    "version.pointer_only_superseded_state_repaired",
}


@dataclass(frozen=True)
class PointerLifecycleBaselineContract:
    schema_version: str
    baseline_relations: tuple[RelationKey, ...]
    baseline_inventory_fingerprint: str
    anchors: tuple[RelationKey, ...]
    evidence_source: str

    @property
    def baseline_inventory_count(self) -> int:
        return len(self.baseline_relations)


DEFAULT_POINTER_LIFECYCLE_BASELINE_CONTRACT = PointerLifecycleBaselineContract(
    schema_version=POINTER_LIFECYCLE_BASELINE_CONTRACT_SCHEMA_VERSION,
    baseline_relations=BASELINE_POINTER_ONLY_RELATIONS,
    baseline_inventory_fingerprint=BASELINE_POINTER_ONLY_FINGERPRINT,
    anchors=BASELINE_ANCHORS,
    evidence_source=POINTER_LIFECYCLE_BASELINE_SOURCE,
)


def _json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _rows_by_id(conn: Any, table: str, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT * FROM {table} WHERE id IN ({placeholders})", tuple(ids)).fetchall()
    return {int(row["id"]): {key: row[key] for key in row.keys()} for row in rows}


def _lifecycle_projection(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "activity_state", "archived_at", "created_at", "expired_due_at", "memory_v2_status",
        "project_key", "scope_code", "state_code", "superseded_by_memory_id",
        "supersedes_memory_id", "valid_to", "validation_source", "workspace_id",
    )
    return {field: row.get(field) for field in fields}


def _snapshot_relation_evidence(conn: Any, snapshot: dict[str, Any], relation: RelationKey) -> dict[str, Any]:
    from mapi_core.memory.lifecycle_pointer_apply import (
        pointer_lifecycle_operation_identity_is_exact,
        validate_pointer_lifecycle_stored_apply_integrity,
    )
    from mapi_core.memory.lifecycle_pointer_execution import (
        LEGACY_POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION,
        POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION,
        validate_pointer_lifecycle_execution_manifest,
    )

    new_id, old_id = relation
    reasons: list[str] = []
    snapshot_id = snapshot.get("id")
    if not isinstance(snapshot_id, int):
        return {"valid": False, "reason_codes": ["accepted_run_snapshot_identity_invalid"]}
    if (
        snapshot.get("operation_type") != "pointer_lineage_remediation"
        or snapshot.get("relation_kind") != "pointer_only_chain_repair"
        or snapshot.get("status") != "applied"
        or snapshot.get("rolled_back_at") is not None
        or not snapshot.get("started_at")
        or not snapshot.get("applied_at")
    ):
        reasons.append("accepted_run_not_applied_or_rolled_back")
    before = _json_object(snapshot.get("before_snapshot_json"))
    after = _json_object(snapshot.get("after_snapshot_json"))
    link_snapshot = _json_object(snapshot.get("link_snapshot_json"))
    event_snapshot = _json_object(snapshot.get("event_snapshot_json"))
    if not all((before, after, link_snapshot, event_snapshot)):
        return {"valid": False, "reason_codes": ["accepted_run_snapshot_json_incomplete"]}

    raw_manifest = before.get("approved_execution_manifest")
    identity = before.get("operation_identity")
    if not isinstance(raw_manifest, dict) or not isinstance(identity, dict):
        reasons.append("accepted_run_manifest_or_identity_missing")
        manifest: dict[str, Any] = {}
        selected_edges: list[Any] = []
    else:
        required_manifest_fields = {
            "schema_version", "plan_version", "execution_policy_version", "execution_scope",
            "created_from_inventory_schema", "created_from_preview_schema",
            "selected_component_ids", "protected_component_ids", "selected_edges",
            "unique_target_memory_ids", "target_event_ledger",
            "active_target_supersedes_link_ledger", "archived_target_supersedes_link_ledger",
        }
        if not required_manifest_fields.issubset(raw_manifest):
            reasons.append("accepted_run_manifest_field_missing")
        if raw_manifest.get("schema_version") not in {
            POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION,
            LEGACY_POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION,
        }:
            reasons.append("accepted_run_manifest_schema_unsupported")
        if raw_manifest.get("plan_version") != "legacy_pointer_only_supersession_2026_07_17_v2":
            reasons.append("accepted_run_plan_version_mismatch")
        if raw_manifest.get("execution_policy_version") != "pointer_lifecycle_execution_2026_07_19_v4":
            reasons.append("accepted_run_execution_policy_mismatch")
        try:
            manifest = validate_pointer_lifecycle_execution_manifest(
                raw_manifest, allow_legacy=True
            )
        except Exception as exc:
            manifest = {}
            reasons.append("accepted_run_manifest_invalid")
            if str(exc) == "execution_edge_fingerprint_mismatch":
                reasons.append("accepted_run_edge_contract_mismatch")
        selected_edges = manifest.get("selected_edges") if isinstance(manifest.get("selected_edges"), list) else []
    run = {
        **snapshot,
        "before_snapshot": before,
        "after_snapshot": after,
        "link_snapshot": link_snapshot,
        "event_snapshot": event_snapshot,
    }
    if manifest and not pointer_lifecycle_operation_identity_is_exact(run):
        reasons.append("accepted_run_operation_identity_mismatch")
    if (
        identity.get("operation_key") != snapshot.get("operation_key")
        or identity.get("execution_preview_hash") != snapshot.get("preview_hash")
        or identity.get("execution_manifest_fingerprint") != _hash(manifest)
        or snapshot.get("input_fingerprint") != identity.get("execution_manifest_fingerprint")
        or snapshot.get("candidate_set_fingerprint") != identity.get("selected_component_ids_fingerprint")
    ):
        reasons.append("accepted_run_operation_key_mismatch")
    matching_edges = [
        edge for edge in selected_edges if isinstance(edge, dict)
        and (edge.get("new_memory_id"), edge.get("old_memory_id")) == relation
    ]
    if len(matching_edges) != 1:
        reasons.append("accepted_run_relation_manifest_mismatch")
        matching_edge: dict[str, Any] | None = None
    else:
        matching_edge = matching_edges[0]
    selected_relation_set = {
        (int(edge["new_memory_id"]), int(edge["old_memory_id"]))
        for edge in selected_edges if isinstance(edge, dict)
        and isinstance(edge.get("new_memory_id"), int) and isinstance(edge.get("old_memory_id"), int)
    }
    if (snapshot.get("new_memory_id"), snapshot.get("old_memory_id")) not in selected_relation_set:
        reasons.append("accepted_run_snapshot_anchor_mismatch")
    protected_ids = manifest.get("protected_component_ids") if manifest else None
    approvals = before.get("protected_approvals")
    if not isinstance(protected_ids, list) or approvals != protected_ids:
        reasons.append("accepted_run_protected_approvals_mismatch")

    created_links = link_snapshot.get("created_link_ledger")
    if not isinstance(created_links, list):
        created_links = []
    relation_links = [
        item for item in created_links if isinstance(item, dict)
        and (item.get("from_memory_id"), item.get("to_memory_id")) == relation
        and item.get("relation_type") == "supersedes"
    ]
    created_link_relation_set = {
        (int(item["from_memory_id"]), int(item["to_memory_id"]))
        for item in created_links if isinstance(item, dict)
        and isinstance(item.get("from_memory_id"), int) and isinstance(item.get("to_memory_id"), int)
        and item.get("relation_type") == "supersedes"
    }
    if created_link_relation_set != selected_relation_set:
        reasons.append("accepted_run_exact_relation_set_mismatch")
    if len(created_links) != len(selected_edges):
        reasons.append("accepted_run_link_ledger_mismatch")
    created_link_ids = link_snapshot.get("created_link_ids")
    ledger_link_ids = sorted(
        item.get("link_id") for item in created_links
        if isinstance(item, dict) and isinstance(item.get("link_id"), int)
    )
    if created_link_ids != ledger_link_ids:
        reasons.append("accepted_run_link_ledger_mismatch")
    if len(relation_links) != 1 or not isinstance(relation_links[0].get("link_id"), int):
        reasons.append("accepted_run_link_ledger_mismatch")
    else:
        ledger_link = relation_links[0]
        live_link = _rows_by_id(conn, "memory_links", [ledger_link["link_id"]]).get(ledger_link["link_id"])
        projected_link = matching_edge.get("projected_link_create") if matching_edge else None
        expected_visibility = (
            ledger_link.get("visibility_scope")
            if "visibility_scope" in ledger_link
            else projected_link.get("visibility_scope") if isinstance(projected_link, dict) else None
        )
        required_link_fields = {
            "link_id", "from_memory_id", "to_memory_id", "relation_type", "weight", "origin",
            "created_at", "archived_at", "workspace_id",
        }
        manifest_schema = manifest.get("schema_version") if manifest else None
        if manifest_schema == POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION:
            required_link_fields.add("visibility_scope")
        elif manifest_schema != LEGACY_POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION:
            reasons.append("accepted_run_manifest_invalid")
        if set(ledger_link) != required_link_fields:
            reasons.append("accepted_run_link_ledger_mismatch")
        if live_link is None or any(
            live_link.get(field) != ledger_link.get(field)
            for field in required_link_fields - {"link_id", "visibility_scope"}
        ) or live_link.get("id") != ledger_link.get("link_id") or (
            live_link.get("visibility_scope") != expected_visibility
        ) or live_link.get("archived_at") is not None:
            reasons.append("accepted_run_live_link_mismatch")
        live_relation_links = conn.execute(
            "SELECT id FROM memory_links WHERE from_memory_id=? AND to_memory_id=? "
            "AND relation_type='supersedes' AND archived_at IS NULL ORDER BY id",
            relation,
        ).fetchall()
        if [int(row["id"]) for row in live_relation_links] != [int(ledger_link["link_id"])]:
            reasons.append("accepted_run_live_link_mismatch")

    created_events = event_snapshot.get("created_apply_event_ledger")
    if not isinstance(created_events, list):
        created_events = []
    event_ids = [item.get("event_id") for item in created_events if isinstance(item, dict)]
    if event_snapshot.get("created_apply_event_ids") != event_ids:
        reasons.append("accepted_run_event_ledger_invalid")
    if len(created_events) != len(selected_edges) * len(EXPECTED_EVENT_TYPES):
        reasons.append("accepted_run_event_ledger_invalid")
    if any(not isinstance(event_id, int) for event_id in event_ids):
        reasons.append("accepted_run_event_ledger_invalid")
        live_events: dict[int, dict[str, Any]] = {}
    else:
        live_events = _rows_by_id(conn, "memory_events", event_ids)
    relation_event_types: set[str] = set()
    relation_event_ids: list[int] = []
    event_relation_set: set[RelationKey] = set()
    ledger_fields = {"event_id", "memory_id", "event_type", "created_at", "payload_sha256"}
    for item in created_events:
        if not isinstance(item, dict) or not isinstance(item.get("event_id"), int):
            continue
        if set(item) != ledger_fields:
            reasons.append("accepted_run_event_ledger_invalid")
        live = live_events.get(item["event_id"])
        payload = _json_object(live.get("payload_json")) if live else None
        payload_relation = (
            (payload.get("new_memory_id"), payload.get("old_memory_id")) if payload else None
        )
        if payload and all(isinstance(value, int) for value in payload_relation or ()):
            event_relation_set.add(payload_relation)  # type: ignore[arg-type]
        if payload and payload_relation == relation:
            relation_event_ids.append(item["event_id"])
            relation_event_types.add(str(live.get("event_type")))
            if any(live.get(field) != item.get(field) for field in ("memory_id", "event_type", "created_at")):
                reasons.append("accepted_run_live_event_ledger_mismatch")
            if hashlib.sha256(str(live.get("payload_json") or "").encode("utf-8")).hexdigest() != item.get("payload_sha256"):
                reasons.append("accepted_run_live_event_payload_mismatch")
            expected_memory_id = new_id if live.get("event_type") == "version.pointer_only_supersession_link_created" else old_id
            if live.get("memory_id") != expected_memory_id:
                reasons.append("accepted_run_live_event_ledger_mismatch")
            if any(
                payload.get(field) != identity.get(identity_field)
                for field, identity_field in (
                    ("operation_key", "operation_key"),
                    ("plan_version", "plan_version"),
                    ("execution_policy_version", "execution_policy_version"),
                    ("execution_manifest_fingerprint", "execution_manifest_fingerprint"),
                    ("execution_preview_hash", "execution_preview_hash"),
                )
            ) or payload.get("remediation_run_id") != snapshot_id:
                reasons.append("accepted_run_event_identity_mismatch")
            descriptor = next((
                value for value in (matching_edge or {}).get("projected_events", [])
                if value.get("event_type") == live.get("event_type")
            ), None)
            if descriptor is None or any(
                payload.get(field) != descriptor.get(field)
                for field in ("changed_fields", "before_field_hash", "after_field_hash")
            ):
                reasons.append("accepted_run_event_contract_mismatch")
    if relation_event_types != EXPECTED_EVENT_TYPES or len(relation_event_ids) != len(EXPECTED_EVENT_TYPES):
        reasons.append("accepted_run_relation_event_set_mismatch")
    if event_relation_set != selected_relation_set:
        reasons.append("accepted_run_exact_event_relation_set_mismatch")

    memories = _rows_by_id(conn, "memories", [new_id, old_id])
    new = memories.get(new_id)
    old = memories.get(old_id)
    if new is None or old is None:
        reasons.append("accepted_run_target_memory_missing")
    else:
        if matching_edge:
            if pointer_memory_immutable_identity_fingerprint(new) != matching_edge.get("new_immutable_identity_fingerprint"):
                reasons.append("accepted_run_new_identity_mismatch")
            if pointer_memory_immutable_identity_fingerprint(old) != matching_edge.get("old_immutable_identity_fingerprint"):
                reasons.append("accepted_run_old_identity_mismatch")
            edge_basis = {key: value for key, value in matching_edge.items() if key != "per_edge_contract_fingerprint"}
            if _hash(edge_basis) != matching_edge.get("per_edge_contract_fingerprint"):
                reasons.append("accepted_run_edge_contract_mismatch")
        after_rows = after.get("after_target_lifecycle_state")
        expected_after = {
            int(item["memory_id"]): item.get("lifecycle") for item in after_rows or []
            if isinstance(item, dict) and isinstance(item.get("memory_id"), int)
        }
        if expected_after.get(new_id) != _lifecycle_projection(new) or expected_after.get(old_id) != _lifecycle_projection(old):
            reasons.append("accepted_run_current_lifecycle_mismatch")
        target_ids = manifest.get("unique_target_memory_ids") if manifest else []
        if sorted(expected_after) != target_ids:
            reasons.append("accepted_run_current_lifecycle_mismatch")
        if matching_edge and any(
            new.get(field) != matching_edge["expected_new_lifecycle_fields"].get(field)
            for field in ("project_key", "scope_code", "workspace_id")
        ):
            reasons.append("accepted_run_current_lifecycle_mismatch")
        if matching_edge and any(
            old.get(field) != matching_edge["expected_old_lifecycle_fields"].get(field)
            for field in ("project_key", "scope_code", "workspace_id")
        ):
            reasons.append("accepted_run_current_lifecycle_mismatch")
        if (
            new.get("supersedes_memory_id") != old_id
            or old.get("superseded_by_memory_id") != new_id
            or str(old.get("state_code") or "").lower() != "superseded"
            or str(old.get("memory_v2_status") or "").lower() != "superseded"
            or str(old.get("activity_state") or "active").lower() != "active"
        ):
            reasons.append("accepted_run_current_relation_not_canonical")

    if manifest:
        integrity_reasons = validate_pointer_lifecycle_stored_apply_integrity(run)
        if integrity_reasons:
            reasons.append("accepted_run_integrity_evidence_invalid")
            reasons.extend(f"accepted_run_integrity_{code}" for code in integrity_reasons)

    return {
        "valid": not reasons,
        "run_id": snapshot_id,
        "snapshot_id": snapshot_id,
        "operation_key": snapshot.get("operation_key"),
        "link_ids": sorted(item["link_id"] for item in relation_links if isinstance(item.get("link_id"), int)),
        "event_ids": sorted(relation_event_ids),
        "reason_codes": sorted(set(reasons)),
    }


def validate_pointer_lifecycle_baseline_contract(
    conn: Any,
    *,
    observed_pointer_only_relations: Iterable[RelationKey],
    observed_forward_relations: Iterable[RelationKey],
    candidate_relations: Iterable[RelationKey] = (),
    contract: PointerLifecycleBaselineContract = DEFAULT_POINTER_LIFECYCLE_BASELINE_CONTRACT,
) -> dict[str, Any]:
    baseline = set(contract.baseline_relations)
    observed = {(int(a), int(b)) for a, b in observed_pointer_only_relations}
    forward = {(int(a), int(b)) for a, b in observed_forward_relations}
    candidates = {(int(a), int(b)) for a, b in candidate_relations}
    blocking: list[str] = []

    actual_baseline_fingerprint = relation_set_fingerprint(baseline)
    if not contract.baseline_inventory_fingerprint:
        blocking.append("baseline_inventory_fingerprint_missing")
    elif actual_baseline_fingerprint != contract.baseline_inventory_fingerprint:
        blocking.append("baseline_inventory_fingerprint_mismatch")
    if contract.schema_version != POINTER_LIFECYCLE_BASELINE_CONTRACT_SCHEMA_VERSION:
        blocking.append("unsupported_baseline_contract_schema")

    missing = baseline - observed
    added = observed - baseline
    snapshots = [
        {key: row[key] for key in row.keys()}
        for row in conn.execute(
            "SELECT * FROM memory_lifecycle_snapshots "
            "WHERE operation_type='pointer_lineage_remediation' ORDER BY id"
        ).fetchall()
    ]
    verified: dict[RelationKey, dict[str, Any]] = {}
    invalid_evidence: dict[RelationKey, list[str]] = {}
    for relation in sorted(missing & forward):
        claiming_snapshots = []
        for snapshot in snapshots:
            if snapshot.get("status") != "applied":
                continue
            before = _json_object(snapshot.get("before_snapshot_json")) or {}
            manifest = before.get("approved_execution_manifest") or {}
            edges = manifest.get("selected_edges") if isinstance(manifest, dict) else []
            if any(
                isinstance(edge, dict)
                and (edge.get("new_memory_id"), edge.get("old_memory_id")) == relation
                for edge in edges or []
            ):
                claiming_snapshots.append(snapshot)
        active_claims = [item for item in claiming_snapshots if item.get("rolled_back_at") is None]
        if len(active_claims) > 1:
            invalid_evidence[relation] = ["ambiguous_accepted_transition_evidence"]
            continue
        matches = []
        failures: list[str] = []
        for snapshot in claiming_snapshots:
            evidence = _snapshot_relation_evidence(conn, snapshot, relation)
            if evidence["valid"]:
                matches.append(evidence)
            else:
                failures.extend(evidence.get("reason_codes", []))
        if len(matches) == 1:
            verified[relation] = matches[0]
        elif len(matches) > 1:
            invalid_evidence[relation] = ["ambiguous_accepted_transition_evidence"]
        else:
            invalid_evidence[relation] = sorted(set(failures or ["accepted_transition_evidence_missing"]))

    unexpected_removed = sorted(missing - forward)
    unexpected_reclassified = sorted((missing & forward) - set(verified))
    verified_relations = sorted(verified)
    expected_current_count = contract.baseline_inventory_count - len(verified_relations)
    unaccounted_delta = len(observed) - expected_current_count
    if added:
        blocking.append("unexpected_added_relations")
    if unexpected_removed:
        blocking.append("unexpected_removed_relations")
    if unexpected_reclassified:
        blocking.append("unexpected_reclassified_relations")
    if invalid_evidence:
        blocking.append("accepted_transition_evidence_invalid")
    if unaccounted_delta:
        blocking.append("pointer_only_count_unaccounted_delta")

    anchor_states = []
    for relation in contract.anchors:
        if relation in observed and relation in candidates:
            state = "candidate"
            accepted = True
            evidence = None
        elif relation in verified:
            state = "already_canonical"
            accepted = True
            evidence = verified[relation]
        elif relation in observed:
            state = "not_plannable_candidate"
            accepted = False
            evidence = None
        else:
            state = "missing_or_unverified"
            accepted = False
            evidence = invalid_evidence.get(relation)
        anchor_states.append({
            "new_memory_id": relation[0], "old_memory_id": relation[1],
            "state": state, "accepted": accepted, "evidence": evidence,
        })
        if not accepted:
            blocking.append("baseline_anchor_not_accepted")

    result = {
        "schema_version": contract.schema_version,
        "baseline_inventory_count": contract.baseline_inventory_count,
        "baseline_inventory_fingerprint": contract.baseline_inventory_fingerprint,
        "baseline_evidence_source": contract.evidence_source,
        "observed_pointer_only_count": len(observed),
        "observed_pointer_only_fingerprint": relation_set_fingerprint(observed),
        "verified_resolved_count": len(verified_relations),
        "expected_current_pointer_only_count": expected_current_count,
        "verified_resolved_relations": normalize_relation_set(verified_relations),
        "verified_transition_evidence": [
            {"relation": normalize_relation_set([relation])[0], **verified[relation]}
            for relation in verified_relations
        ],
        "unexpected_removed_relations": normalize_relation_set(unexpected_removed),
        "unexpected_added_relations": normalize_relation_set(added),
        "unexpected_reclassified_relations": normalize_relation_set(unexpected_reclassified),
        "anchor_states": anchor_states,
        "evidence_status": "verified" if not invalid_evidence else "invalid_or_incomplete",
        "invalid_evidence": [
            {"relation": normalize_relation_set([relation])[0], "reason_codes": reasons}
            for relation, reasons in sorted(invalid_evidence.items())
        ],
        "unaccounted_delta": unaccounted_delta,
        "contract_status": "accepted" if not blocking else "blocked",
        "blocking_reasons": sorted(set(blocking)),
        "unsupported_metrics": [],
    }
    result["contract_fingerprint"] = _hash(result)
    return result
