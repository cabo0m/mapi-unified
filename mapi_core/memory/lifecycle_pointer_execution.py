from __future__ import annotations

import copy
from typing import Any

from mapi_core.memory.lifecycle_pointer_remediation import (
    POINTER_LIFECYCLE_INVENTORY_SCHEMA_VERSION,
    POINTER_LIFECYCLE_PREVIEW_SCHEMA_VERSION,
    POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
    _hash,
    _load_graph,
    _public_memory,
    get_memory_pointer_lifecycle_remediation_inventory_payload,
    preview_memory_pointer_lifecycle_remediation_payload,
)
from mapi_core.sandman.contracts import ContractError, strict_json_loads


POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION = "pointer_lifecycle_execution_2026_07_19_v4"
POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION = "memory_v3_pointer_lifecycle_execution_manifest.v2"
LEGACY_POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION = (
    "memory_v3_pointer_lifecycle_execution_manifest.v1"
)
POINTER_LIFECYCLE_EXECUTION_PREVIEW_SCHEMA_VERSION = "memory_v3_pointer_lifecycle_execution_preview.v4"
POINTER_LIFECYCLE_FUTURE_APPLY_SCHEMA_VERSION = "memory_v3_pointer_lifecycle_future_apply_contract.v4"
POINTER_LIFECYCLE_FUTURE_ROLLBACK_SCHEMA_VERSION = "memory_v3_pointer_lifecycle_future_rollback_contract.v1"
POINTER_LIFECYCLE_OPERATION_IDENTITY_SCHEMA_VERSION = (
    "memory_v3_pointer_lifecycle_operation_identity_contract.v1"
)
EXECUTION_SCOPES = ("all_safe", "unprotected_only")
APPLY_BLOCK_REASON = None
MAX_MANIFEST_BYTES = 2_000_000

LIFECYCLE_FIELDS = (
    "project_key", "scope_code", "workspace_id", "state_code", "memory_v2_status",
    "activity_state", "archived_at", "supersedes_memory_id", "superseded_by_memory_id",
    "created_at", "valid_to", "expired_due_at", "validation_source",
)
MANIFEST_FIELDS = frozenset({
    "schema_version", "plan_version", "execution_policy_version", "execution_scope",
    "created_from_inventory_schema", "created_from_preview_schema", "selected_component_ids",
    "protected_component_ids", "selected_edges", "unique_target_memory_ids", "target_event_ledger",
    "active_target_supersedes_link_ledger", "archived_target_supersedes_link_ledger",
    "baseline_contract_fingerprint",
})
LEGACY_MANIFEST_FIELDS = MANIFEST_FIELDS - {"baseline_contract_fingerprint"}
EDGE_FIELDS = frozenset({
    "component_id", "protected_review_required", "new_memory_id", "old_memory_id",
    "new_immutable_identity_fingerprint", "old_immutable_identity_fingerprint",
    "expected_new_lifecycle_fields", "expected_old_lifecycle_fields", "projected_link_create",
    "projected_reverse_pointer_update", "projected_state_update", "changed_fields",
    "projected_events", "per_edge_contract_fingerprint",
})
EVENT_LEDGER_FIELDS = frozenset({"event_id", "memory_id", "event_type", "created_at", "payload_sha256"})
LINK_LEDGER_FIELDS = frozenset({
    "link_id", "from_memory_id", "to_memory_id", "relation_type", "workspace_id", "origin",
    "weight", "created_at", "archived_at", "visibility_scope",
})
LEGACY_LINK_LEDGER_FIELDS = LINK_LEDGER_FIELDS - {"visibility_scope"}
PROJECTED_EVENT_FIELDS = frozenset({
    "required_payload_fields", "new_memory_id", "old_memory_id", "event_type",
    "changed_fields", "before_field_hash", "after_field_hash",
})
PROJECTED_LINK_FIELDS = frozenset({
    "from_memory_id", "to_memory_id", "relation_type", "weight", "origin", "workspace_id",
    "visibility_scope", "archived_at",
})

SAFETY = {
    "read_only": True,
    "mutations_performed": 0,
    "backup_created": False,
    "snapshot_created": False,
    "apply_supported": True,
}

OPERATION_IDENTITY_FIELDS = (
    "operation_key",
    "logical_operation_type",
    "relation_kind",
    "plan_version",
    "execution_policy_version",
    "execution_manifest_fingerprint",
    "execution_preview_hash",
    "execution_scope",
    "selected_component_ids_fingerprint",
    "selected_edge_contract_fingerprint",
    "protected_component_ids_fingerprint",
    "status",
)
IMMUTABLE_OPERATION_IDENTITY_FIELDS = tuple(
    field for field in OPERATION_IDENTITY_FIELDS if field != "status"
)

FUTURE_OPERATION_IDENTITY_CONTRACT = {
    "schema_version": POINTER_LIFECYCLE_OPERATION_IDENTITY_SCHEMA_VERSION,
    "supported_now": True,
    "logical_operation_type": "pointer_lineage_remediation",
    "relation_kind": "pointer_only_chain_repair",
    "operation_key_format": (
        "pointer_lineage_execution:<execution_policy_version>:"
        "<execution_manifest_fingerprint>"
    ),
    "required_stored_identity_fields": list(OPERATION_IDENTITY_FIELDS),
    "immutable_identity_fields": list(IMMUTABLE_OPERATION_IDENTITY_FIELDS),
    "status_classified_separately_after_immutable_identity_match": True,
    "full_immutable_identity_match_required": True,
}

IDEMPOTENCY_DESIGN = {
    "pre_backup_lookup_required": True,
    "inside_transaction_lookup_required": True,
    "ordinary_exact_repeat_creates_backup": False,
    "target_prestate_validation_for_exact_repeat": False,
    "concurrent_exact_repeat_after_backup": "already_applied_exact_concurrent",
    "ambiguous_transport_auto_retry": False,
}

FUTURE_APPLY_CONTRACT = {
    "schema_version": POINTER_LIFECYCLE_FUTURE_APPLY_SCHEMA_VERSION,
    "supported_now": True,
    "required_inputs": [
        "plan_version", "execution_policy_version",
        "approved_execution_manifest_json", "expected_execution_manifest_fingerprint",
        "expected_execution_preview_hash", "approved_protected_component_ids_json",
        "applied_by", "reason", "confirm_data_repair", "confirm_protected",
    ],
    "guarded_sequence": [
        "strict_validate_manifest_and_hashes",
        "derive_operation_identity_and_operation_key",
        "lookup_existing_operation_before_backup",
        "classify_existing_operation_before_backup",
        "return_already_applied_exact_without_backup_when_applicable",
        "block_non_retriable_existing_operation_states",
        "create_and_validate_online_sqlite_backup_for_not_applied_only",
        "begin_immediate",
        "repeat_existing_operation_lookup_inside_transaction",
        "classify_existing_operation_inside_transaction",
        "handle_concurrent_exact_apply_without_writes",
        "block_concurrent_identity_or_status_conflicts",
        "revalidate_frozen_targets_inside_transaction",
        "create_applying_snapshot_and_real_run_id",
        "create_only_projected_links",
        "update_only_projected_memory_fields",
        "create_only_projected_events_with_real_run_id",
        "finalize_applied_snapshot_with_exact_after_state",
        "post_write_lifecycle_integrity",
        "atomic_commit",
        "no_automatic_retry_after_ambiguous_transport",
    ],
    "idempotency_states": [
        "not_applied",
        "already_applied_exact",
        "already_applied_exact_concurrent",
        "blocked_operation_identity_mismatch",
        "blocked_previous_run_rolled_back_requires_new_preview",
        "blocked_incomplete_applying_run_requires_operator_review",
        "blocked_previous_failed_run_requires_operator_review",
        "blocked_duplicate_operation_key_rows",
        "blocked_unknown_run_status",
    ],
    "ambiguous_transport_requires_operation_lookup": True,
    "automatic_retry_allowed": False,
    "ambiguous_transport_runbook": [
        "caller_does_not_retry_apply",
        "caller_performs_read_only_lookup_by_operation_key",
        "exact_applied_identity_means_success",
        "applying_requires_operator_review",
        "missing_run_requires_target_and_snapshot_audit_without_retry",
        "identity_mismatch_blocks",
    ],
}

FUTURE_BACKUP_CONTRACT = {
    "supported_now": True,
    "required_fields": [
        "schema_version", "operation_key", "execution_manifest_fingerprint",
        "execution_preview_hash", "source_database_path", "source_database_size",
        "source_database_sha256", "source_quick_check", "backup_path", "backup_size",
        "backup_sha256", "backup_quick_check", "migration_tail", "max_memory_id",
        "max_event_id", "memory_count", "link_count", "event_count", "snapshot_count",
        "target_memory_lifecycle_fingerprint", "target_event_ledger_fingerprint",
        "target_active_supersedes_link_ledger_fingerprint",
        "target_archived_supersedes_link_ledger_fingerprint", "created_at",
    ],
    "verified_before_transaction": True,
    "rebound_to_target_state_before_begin_immediate": True,
    "backup_created_only_after_pre_backup_operation_lookup_returns_not_applied": True,
    "ordinary_already_applied_exact_creates_backup": False,
    "concurrent_exact_apply_backup_disposition": "retain_verified_unused_backup_and_report",
    "concurrent_exact_apply_writes": 0,
    "automatic_restore_allowed": False,
}

FUTURE_SNAPSHOT_CONTRACT = {
    "supported_now": True,
    "logical_operation_type": "pointer_lineage_remediation",
    "relation_kind": "pointer_only_chain_repair",
    "single_logical_snapshot": True,
    "excluded_from_single_edge_supersession_runs": True,
    "required_fields": [
        "operation_key", "execution_policy_version", "approved_manifest", "manifest_fingerprint",
        "execution_preview_hash", "protected_approvals", "backup_manifest",
        "before_target_memory_state", "after_target_memory_state", "before_link_ledger",
        "created_link_ids", "before_target_event_ledger", "created_apply_event_ledger",
        "applied_by", "reason", "status", "applied_at", "rolled_back_at",
    ],
}

FUTURE_EVENT_CONTRACT = {
    "supported_now": True,
    "event_types": [
        "version.pointer_only_reverse_pointer_repaired",
        "version.pointer_only_superseded_state_repaired",
        "version.pointer_only_supersession_link_created",
    ],
    "required_payload_fields": [
        "remediation_run_id", "operation_key", "plan_version", "execution_policy_version",
        "execution_manifest_fingerprint", "execution_preview_hash", "new_memory_id",
        "old_memory_id", "changed_fields", "before_field_hash", "after_field_hash",
        "applied_by", "reason",
    ],
    "projected_event_count_must_match_frozen_descriptors": True,
    "dynamic_event_ids_required": True,
}

FUTURE_ROLLBACK_CONTRACT = {
    "schema_version": POINTER_LIFECYCLE_FUTURE_ROLLBACK_SCHEMA_VERSION,
    "supported_now": True,
    "requires_exact_snapshot": True,
    "requires_exact_dynamic_event_ids": True,
    "requires_fresh_rollback_preview_hash": True,
    "restores_only_frozen_target_state": True,
    "idempotency_states": ["not_rolled_back", "already_rolled_back_exact", "blocked_contract_mismatch"],
}


def _failure(code: str, *, status: str = "blocked") -> dict[str, Any]:
    return {
        "schema_version": POINTER_LIFECYCLE_EXECUTION_PREVIEW_SCHEMA_VERSION,
        "status": status,
        "reason_codes": [code],
        "safety": dict(SAFETY),
    }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_exact_fields(value: Any, fields: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError(code)
    return value


def _require_sorted_unique_ids(value: Any, code: str, *, allow_empty: bool = True) -> list[int]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ContractError(code)
    if any(not _is_int(item) or item <= 0 for item in value) or value != sorted(set(value)):
        raise ContractError(code)
    return value


def classify_existing_pointer_lifecycle_operation(
    rows: list[dict[str, Any]],
    *,
    expected_identity: dict[str, Any],
) -> dict[str, Any]:
    """Classify a future operation-key lookup without reading or mutating runtime state."""
    if not isinstance(rows, list):
        raise ValueError("rows_must_be_a_list")
    if set(expected_identity) != set(OPERATION_IDENTITY_FIELDS):
        raise ValueError("invalid_expected_operation_identity")
    if not rows:
        return {"decision": "not_applied", "writes": 0, "backup_required": True}
    if len(rows) > 1:
        return {
            "decision": "blocked_duplicate_operation_key_rows",
            "writes": 0,
            "backup_required": False,
        }

    row = rows[0]
    mismatched_fields = [
        field
        for field in IMMUTABLE_OPERATION_IDENTITY_FIELDS
        if row.get(field) != expected_identity[field]
    ]
    if mismatched_fields:
        return {
            "decision": "blocked_operation_identity_mismatch",
            "mismatched_fields": mismatched_fields,
            "writes": 0,
            "backup_required": False,
        }

    status = row.get("status")
    if status == "rolled_back" or row.get("rolled_back_at") is not None:
        decision = "blocked_previous_run_rolled_back_requires_new_preview"
    elif status == "applying":
        decision = "blocked_incomplete_applying_run_requires_operator_review"
    elif status == "failed":
        decision = "blocked_previous_failed_run_requires_operator_review"
    elif status == "applied" and status == expected_identity["status"]:
        decision = "already_applied_exact"
    else:
        decision = "blocked_unknown_run_status"
    return {"decision": decision, "writes": 0, "backup_required": False}


def _validate_manifest(value: Any, *, allow_legacy: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("invalid_execution_manifest_fields")
    schema_version = value.get("schema_version")
    if schema_version == POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION:
        manifest = _require_exact_fields(value, MANIFEST_FIELDS, "invalid_execution_manifest_fields")
        baseline_fingerprint = manifest.get("baseline_contract_fingerprint")
        if not isinstance(baseline_fingerprint, str) or len(baseline_fingerprint) != 64:
            raise ContractError("invalid_baseline_contract_fingerprint")
    elif allow_legacy and schema_version == LEGACY_POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION:
        manifest = _require_exact_fields(
            value, LEGACY_MANIFEST_FIELDS, "invalid_execution_manifest_fields"
        )
    else:
        raise ContractError("unsupported_execution_manifest_version")
    expected_strings = {
        "plan_version": POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
        "execution_policy_version": POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION,
        "created_from_inventory_schema": POINTER_LIFECYCLE_INVENTORY_SCHEMA_VERSION,
        "created_from_preview_schema": POINTER_LIFECYCLE_PREVIEW_SCHEMA_VERSION,
    }
    if any(manifest.get(key) != expected for key, expected in expected_strings.items()):
        raise ContractError("unsupported_execution_manifest_version")
    if manifest.get("execution_scope") not in EXECUTION_SCOPES:
        raise ContractError("invalid_execution_scope")
    component_ids = manifest["selected_component_ids"]
    protected_ids = manifest["protected_component_ids"]
    if not isinstance(component_ids, list) or component_ids != sorted(set(component_ids)) or any(
        not isinstance(item, str) or not item for item in component_ids
    ):
        raise ContractError("invalid_selected_component_ids")
    if not isinstance(protected_ids, list) or protected_ids != sorted(set(protected_ids)) or any(
        not isinstance(item, str) or item not in component_ids for item in protected_ids
    ):
        raise ContractError("invalid_protected_component_ids")
    target_ids = _require_sorted_unique_ids(
        manifest["unique_target_memory_ids"], "invalid_target_memory_ids", allow_empty=False
    )
    edges = manifest["selected_edges"]
    if not isinstance(edges, list) or not edges:
        raise ContractError("invalid_selected_edges")
    edge_keys: list[tuple[str, int, int]] = []
    edge_target_ids: set[int] = set()
    for edge in edges:
        _require_exact_fields(edge, EDGE_FIELDS, "invalid_execution_edge_fields")
        if not isinstance(edge["component_id"], str) or edge["component_id"] not in component_ids:
            raise ContractError("invalid_execution_edge_component")
        if not isinstance(edge["protected_review_required"], bool):
            raise ContractError("invalid_execution_edge_protected_marker")
        if edge["protected_review_required"] != (edge["component_id"] in protected_ids):
            raise ContractError("execution_edge_protected_marker_mismatch")
        for key in ("new_memory_id", "old_memory_id"):
            if not _is_int(edge[key]) or edge[key] <= 0:
                raise ContractError("invalid_execution_edge_memory_id")
            edge_target_ids.add(edge[key])
        for key in ("new_immutable_identity_fingerprint", "old_immutable_identity_fingerprint", "per_edge_contract_fingerprint"):
            if not isinstance(edge[key], str) or len(edge[key]) != 64:
                raise ContractError("invalid_execution_edge_fingerprint")
        for key in ("expected_new_lifecycle_fields", "expected_old_lifecycle_fields"):
            if not isinstance(edge[key], dict) or set(edge[key]) != set(LIFECYCLE_FIELDS):
                raise ContractError("invalid_execution_lifecycle_fields")
        if not isinstance(edge["changed_fields"], list) or edge["changed_fields"] != sorted(set(edge["changed_fields"])):
            raise ContractError("invalid_execution_changed_fields")
        if not isinstance(edge["projected_events"], list):
            raise ContractError("invalid_execution_projected_events")
        projected_link = edge["projected_link_create"]
        if projected_link is not None:
            _require_exact_fields(projected_link, PROJECTED_LINK_FIELDS, "invalid_projected_link_fields")
        reverse_update = edge["projected_reverse_pointer_update"]
        if reverse_update is not None and set(reverse_update) != {"superseded_by_memory_id"}:
            raise ContractError("invalid_projected_reverse_update")
        state_update = edge["projected_state_update"]
        allowed_state_updates = {"state_code", "memory_v2_status", "valid_to", "expired_due_at", "validation_source"}
        if state_update is not None and (
            not isinstance(state_update, dict) or not state_update or not set(state_update) <= allowed_state_updates
        ):
            raise ContractError("invalid_projected_state_update")
        for updates in (reverse_update, state_update):
            if updates is None:
                continue
            for change in updates.values():
                _require_exact_fields(change, frozenset({"before", "after"}), "invalid_projected_field_change")
        for event in edge["projected_events"]:
            _require_exact_fields(event, PROJECTED_EVENT_FIELDS, "invalid_projected_event_fields")
            if event["required_payload_fields"] != FUTURE_EVENT_CONTRACT["required_payload_fields"]:
                raise ContractError("invalid_projected_event_payload_contract")
        edge_without_fingerprint = {key: edge[key] for key in edge if key != "per_edge_contract_fingerprint"}
        if _hash(edge_without_fingerprint) != edge["per_edge_contract_fingerprint"]:
            raise ContractError("execution_edge_fingerprint_mismatch")
        edge_keys.append((edge["component_id"], edge["new_memory_id"], edge["old_memory_id"]))
    if (
        edge_keys != sorted(set(edge_keys))
        or sorted(edge_target_ids) != target_ids
        or sorted({key[0] for key in edge_keys}) != component_ids
    ):
        raise ContractError("invalid_execution_edge_order")
    link_ledger_fields = (
        LEGACY_LINK_LEDGER_FIELDS
        if schema_version == LEGACY_POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION
        else LINK_LEDGER_FIELDS
    )
    for ledger_name, fields in (
        ("target_event_ledger", EVENT_LEDGER_FIELDS),
        ("active_target_supersedes_link_ledger", link_ledger_fields),
        ("archived_target_supersedes_link_ledger", link_ledger_fields),
    ):
        ledger = manifest[ledger_name]
        if not isinstance(ledger, list):
            raise ContractError("invalid_execution_ledger")
        ids = []
        id_field = "event_id" if ledger_name == "target_event_ledger" else "link_id"
        for entry in ledger:
            _require_exact_fields(entry, fields, "invalid_execution_ledger_fields")
            if not _is_int(entry[id_field]) or entry[id_field] <= 0:
                raise ContractError("invalid_execution_ledger_id")
            related_id_fields = (
                ("memory_id",) if ledger_name == "target_event_ledger"
                else ("from_memory_id", "to_memory_id")
            )
            if any(not _is_int(entry[key]) or entry[key] <= 0 for key in related_id_fields):
                raise ContractError("invalid_execution_ledger_memory_id")
            ids.append(entry[id_field])
        if ids != sorted(set(ids)):
            raise ContractError("invalid_execution_ledger_order")
    return manifest


def validate_pointer_lifecycle_execution_manifest(
    value: Any, *, allow_legacy: bool = False,
) -> dict[str, Any]:
    """Strictly validate and return a canonical pointer lifecycle manifest."""
    return _validate_manifest(value, allow_legacy=allow_legacy)


def _strict_manifest(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ContractError("invalid_execution_manifest_json")
    return _validate_manifest(strict_json_loads(raw, invalid_code="invalid_execution_manifest_json"))


def _lifecycle(memory: dict[str, Any]) -> dict[str, Any]:
    return {field: memory.get(field) for field in LIFECYCLE_FIELDS}


def _event_ledger(conn: Any, target_ids: list[int]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in target_ids)
    rows = conn.execute(
        f"SELECT id, memory_id, event_type, created_at, payload_json FROM memory_events "
        f"WHERE memory_id IN ({placeholders}) ORDER BY id", tuple(target_ids),
    ).fetchall()
    import hashlib
    return [{
        "event_id": int(row["id"]), "memory_id": int(row["memory_id"]),
        "event_type": row["event_type"], "created_at": row["created_at"],
        "payload_sha256": hashlib.sha256(str(row["payload_json"] or "").encode("utf-8")).hexdigest(),
    } for row in rows]


def _link_ledgers(conn: Any, target_ids: list[int]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    placeholders = ",".join("?" for _ in target_ids)
    rows = conn.execute(
        f"SELECT id, from_memory_id, to_memory_id, relation_type, workspace_id, origin, weight, "
        f"created_at, archived_at, visibility_scope FROM memory_links "
        f"WHERE lower(trim(relation_type))='supersedes' "
        f"AND (from_memory_id IN ({placeholders}) OR to_memory_id IN ({placeholders})) ORDER BY id",
        tuple(target_ids) + tuple(target_ids),
    ).fetchall()
    entries = [{
        "link_id": int(row["id"]), "from_memory_id": int(row["from_memory_id"]),
        "to_memory_id": int(row["to_memory_id"]), "relation_type": row["relation_type"],
        "workspace_id": row["workspace_id"], "origin": row["origin"], "weight": row["weight"],
        "created_at": row["created_at"], "archived_at": row["archived_at"],
        "visibility_scope": row["visibility_scope"],
    } for row in rows]
    return (
        [entry for entry in entries if entry["archived_at"] is None],
        [entry for entry in entries if entry["archived_at"] is not None],
    )


def _build_manifest(conn: Any, scope: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    inventory = get_memory_pointer_lifecycle_remediation_inventory_payload(conn, include_debug=True)
    preview = preview_memory_pointer_lifecycle_remediation_payload(conn, include_debug=True)
    if inventory["status"] != "inventory_ready" or preview["status"] != "preview_ready":
        raise ContractError("accepted_pointer_preview_not_ready")
    selected_components = [
        component for component in inventory["safe_components"]
        if scope == "all_safe" or not component["protected_review_required"]
    ]
    if not selected_components:
        raise ContractError("execution_scope_has_no_safe_components")
    component_by_id = {item["component_id"]: item for item in selected_components}
    projected_by_id = {item["component_id"]: item for item in preview["projected_changes"]}
    edges: list[dict[str, Any]] = []
    for component_id in sorted(component_by_id):
        component = component_by_id[component_id]
        memory_by_id = {int(item["id"]): item for item in component["memories"]}
        for projected in projected_by_id[component_id]["edges"]:
            new_id = int(projected["new_memory_id"])
            old_id = int(projected["old_memory_id"])
            edge = {
                "component_id": component_id,
                "protected_review_required": bool(component["protected_review_required"]),
                "new_memory_id": new_id,
                "old_memory_id": old_id,
                "new_immutable_identity_fingerprint": memory_by_id[new_id]["immutable_identity_fingerprint"],
                "old_immutable_identity_fingerprint": memory_by_id[old_id]["immutable_identity_fingerprint"],
                "expected_new_lifecycle_fields": _lifecycle(memory_by_id[new_id]),
                "expected_old_lifecycle_fields": _lifecycle(memory_by_id[old_id]),
                "projected_link_create": copy.deepcopy(projected["projected_link_create"]),
                "projected_reverse_pointer_update": copy.deepcopy(projected["projected_reverse_pointer_update"]),
                "projected_state_update": copy.deepcopy(projected["projected_state_update"]),
                "changed_fields": list(projected["changed_fields"]),
                "projected_events": copy.deepcopy(projected["projected_events"]),
            }
            for event in edge["projected_events"]:
                event["required_payload_fields"] = list(FUTURE_EVENT_CONTRACT["required_payload_fields"])
            edge["per_edge_contract_fingerprint"] = _hash(edge)
            edges.append(edge)
    edges.sort(key=lambda item: (item["component_id"], item["new_memory_id"], item["old_memory_id"]))
    target_ids = sorted({edge[key] for edge in edges for key in ("new_memory_id", "old_memory_id")})
    active_links, archived_links = _link_ledgers(conn, target_ids)
    manifest = {
        "schema_version": POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION,
        "plan_version": POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
        "execution_policy_version": POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION,
        "execution_scope": scope,
        "created_from_inventory_schema": POINTER_LIFECYCLE_INVENTORY_SCHEMA_VERSION,
        "created_from_preview_schema": POINTER_LIFECYCLE_PREVIEW_SCHEMA_VERSION,
        "selected_component_ids": sorted(component_by_id),
        "protected_component_ids": sorted(
            item["component_id"] for item in selected_components if item["protected_review_required"]
        ),
        "selected_edges": edges,
        "unique_target_memory_ids": target_ids,
        "target_event_ledger": _event_ledger(conn, target_ids),
        "active_target_supersedes_link_ledger": active_links,
        "archived_target_supersedes_link_ledger": archived_links,
        "baseline_contract_fingerprint": inventory["baseline_contract"]["contract_fingerprint"],
    }
    return manifest, inventory, preview


def _execution_hash(manifest_fingerprint: str, manifest: dict[str, Any]) -> str:
    return _hash({
        "schema_version": POINTER_LIFECYCLE_EXECUTION_PREVIEW_SCHEMA_VERSION,
        "execution_manifest_fingerprint": manifest_fingerprint,
        "execution_scope": manifest["execution_scope"],
        "protected_component_ids": manifest["protected_component_ids"],
        "protected_confirmation_required": bool(manifest["protected_component_ids"]),
        "target_event_ledger_fingerprint": _hash(manifest["target_event_ledger"]),
        "active_target_link_ledger_fingerprint": _hash(manifest["active_target_supersedes_link_ledger"]),
        "archived_target_link_ledger_fingerprint": _hash(manifest["archived_target_supersedes_link_ledger"]),
        "future_operation_identity_contract": FUTURE_OPERATION_IDENTITY_CONTRACT,
        "future_apply_contract": FUTURE_APPLY_CONTRACT,
        "future_backup_contract": FUTURE_BACKUP_CONTRACT,
        "future_snapshot_contract": FUTURE_SNAPSHOT_CONTRACT,
        "future_event_contract": FUTURE_EVENT_CONTRACT,
        "future_rollback_contract": FUTURE_ROLLBACK_CONTRACT,
        "idempotency_design": IDEMPOTENCY_DESIGN,
    })


def _scope_summary(conn: Any, inventory: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for scope in EXECUTION_SCOPES:
        selected = [
            item for item in inventory["safe_components"]
            if scope == "all_safe" or not item["protected_review_required"]
        ]
        result[scope] = {
            "component_count": len(selected),
            "edge_count": sum(len(item["candidate_edges"]) for item in selected),
            "protected_component_count": sum(bool(item["protected_review_required"]) for item in selected),
            "component_set_fingerprint": _hash([
                {"component_id": item["component_id"], "candidate_edges": item["candidate_edges"]}
                for item in selected
            ]),
        }
    return result


def _base_result(manifest: dict[str, Any], fingerprint: str) -> dict[str, Any]:
    operation_key = f"pointer_lineage_execution:{POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION}:{fingerprint}"
    edges = manifest["selected_edges"]
    projected_memory_updates = sum(
        edge["projected_reverse_pointer_update"] is not None or edge["projected_state_update"] is not None
        for edge in edges
    )
    projected_link_creates = sum(edge["projected_link_create"] is not None for edge in edges)
    projected_events = sum(len(edge["projected_events"]) for edge in edges)
    execution_preview_hash = _execution_hash(fingerprint, manifest)
    operation_identity = {
        "operation_key": operation_key,
        "logical_operation_type": "pointer_lineage_remediation",
        "relation_kind": "pointer_only_chain_repair",
        "plan_version": POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
        "execution_policy_version": POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION,
        "execution_manifest_fingerprint": fingerprint,
        "execution_preview_hash": execution_preview_hash,
        "execution_scope": manifest["execution_scope"],
        "selected_component_ids_fingerprint": _hash(manifest["selected_component_ids"]),
        "selected_edge_contract_fingerprint": _hash([
            edge["per_edge_contract_fingerprint"] for edge in manifest["selected_edges"]
        ]),
        "protected_component_ids_fingerprint": _hash(manifest["protected_component_ids"]),
        "status": "applied",
    }
    return {
        "schema_version": POINTER_LIFECYCLE_EXECUTION_PREVIEW_SCHEMA_VERSION,
        "plan_version": POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
        "execution_policy_version": POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION,
        "execution_scope": manifest["execution_scope"],
        "operation_key": operation_key,
        "execution_manifest": manifest,
        "execution_manifest_fingerprint": fingerprint,
        "execution_preview_hash": execution_preview_hash,
        "future_operation_identity_contract": copy.deepcopy(FUTURE_OPERATION_IDENTITY_CONTRACT),
        "future_operation_identity": operation_identity,
        "protected_confirmation_required": bool(manifest["protected_component_ids"]),
        "protected_component_ids": manifest["protected_component_ids"],
        "future_apply_contract": copy.deepcopy(FUTURE_APPLY_CONTRACT),
        "future_backup_contract": copy.deepcopy(FUTURE_BACKUP_CONTRACT),
        "future_snapshot_contract": copy.deepcopy(FUTURE_SNAPSHOT_CONTRACT),
        "future_event_contract": copy.deepcopy(FUTURE_EVENT_CONTRACT),
        "future_rollback_contract": copy.deepcopy(FUTURE_ROLLBACK_CONTRACT),
        "idempotency_design": copy.deepcopy(IDEMPOTENCY_DESIGN),
        "future_confirm_protected_required": bool(manifest["protected_component_ids"]),
        "summary": {
            "selected_components": len(manifest["selected_component_ids"]),
            "selected_edges": len(edges),
            "protected_components": len(manifest["protected_component_ids"]),
            "projected_memory_updates": projected_memory_updates,
            "projected_link_creates": projected_link_creates,
            "projected_events": projected_events,
            "projected_logical_mutations_including_snapshot": (
                projected_memory_updates + projected_link_creates + projected_events + 1
            ),
        },
        "remaining_blocked_components_present": False,
        "v3_b10_ready_after_safe_apply": False,
        "safety": dict(SAFETY),
    }


def _revalidate(conn: Any, manifest: dict[str, Any], expected_fingerprint: str) -> dict[str, Any]:
    fingerprint = _hash(manifest)
    result = _base_result(manifest, fingerprint)
    reasons: list[dict[str, Any]] = []
    if expected_fingerprint != fingerprint:
        reasons.append({"code": "execution_manifest_fingerprint_mismatch"})
    memories, _ = _load_graph(conn)
    for edge in manifest["selected_edges"]:
        for role in ("new", "old"):
            memory_id = edge[f"{role}_memory_id"]
            row = memories.get(memory_id)
            if row is None:
                reasons.append({"code": "target_memory_missing", "memory_id": memory_id})
                continue
            public = _public_memory(row)
            if public["immutable_identity_fingerprint"] != edge[f"{role}_immutable_identity_fingerprint"]:
                reasons.append({"code": "target_memory_identity_drift", "memory_id": memory_id})
            if _lifecycle(public) != edge[f"expected_{role}_lifecycle_fields"]:
                reasons.append({"code": "target_memory_lifecycle_drift", "memory_id": memory_id})
                if any(
                    public.get(field) != edge[f"expected_{role}_lifecycle_fields"].get(field)
                    for field in ("project_key", "scope_code", "workspace_id")
                ):
                    reasons.append({"code": "target_boundary_drift", "memory_id": memory_id})
    live_events = _event_ledger(conn, manifest["unique_target_memory_ids"])
    if live_events != manifest["target_event_ledger"]:
        reasons.append({"code": "target_event_ledger_drift"})
    active_links, archived_links = _link_ledgers(conn, manifest["unique_target_memory_ids"])
    if active_links != manifest["active_target_supersedes_link_ledger"]:
        reasons.append({"code": "target_active_link_ledger_drift"})
    if archived_links != manifest["archived_target_supersedes_link_ledger"]:
        reasons.append({"code": "target_archived_link_ledger_drift"})

    inventory = get_memory_pointer_lifecycle_remediation_inventory_payload(conn, include_debug=True)
    preview = preview_memory_pointer_lifecycle_remediation_payload(conn, include_debug=True)
    live_components = {item["component_id"]: item for item in inventory.get("safe_components", [])}
    live_changes = {item["component_id"]: item for item in preview.get("projected_changes", [])}
    selected_pairs = {(edge["new_memory_id"], edge["old_memory_id"]) for edge in manifest["selected_edges"]}
    for edge in manifest["selected_edges"]:
        component = live_components.get(edge["component_id"])
        change = live_changes.get(edge["component_id"])
        if component is None or change is None:
            reasons.append({"code": "target_component_no_longer_safe", "component_id": edge["component_id"]})
            continue
        if bool(component["protected_review_required"]) != bool(edge["protected_review_required"]):
            reasons.append({"code": "target_protected_marker_drift", "component_id": edge["component_id"]})
        projected = next((item for item in change["edges"] if (
            item["new_memory_id"], item["old_memory_id"]
        ) == (edge["new_memory_id"], edge["old_memory_id"])), None)
        if projected is None:
            reasons.append({"code": "target_edge_projection_missing", "component_id": edge["component_id"]})
            continue
        projection = {
            "projected_link_create": projected["projected_link_create"],
            "projected_reverse_pointer_update": projected["projected_reverse_pointer_update"],
            "projected_state_update": projected["projected_state_update"],
            "changed_fields": projected["changed_fields"],
            "projected_events": copy.deepcopy(projected["projected_events"]),
        }
        for event in projection["projected_events"]:
            event["required_payload_fields"] = list(FUTURE_EVENT_CONTRACT["required_payload_fields"])
        frozen_projection = {key: edge[key] for key in projection}
        if projection != frozen_projection:
            reasons.append({"code": "target_edge_projection_drift", "component_id": edge["component_id"]})
    live_pairs = {
        (item["new_memory_id"], item["old_memory_id"])
        for component in inventory.get("safe_components", []) for item in component["candidate_edges"]
    }
    unrelated_pairs = sorted(live_pairs - selected_pairs)
    unrelated_ids = sorted({memory_id for pair in unrelated_pairs for memory_id in pair} - set(manifest["unique_target_memory_ids"]))
    result.update({
        "status": "execution_revalidation_ready" if not reasons else "execution_revalidation_blocked",
        "manifest_status": "exact_match" if not reasons else "stale",
        "reason_codes": sorted({item["code"] for item in reasons}),
        "blocking_findings": reasons,
        "diagnostics": {
            "unrelated_inventory_drift_detected": bool(unrelated_pairs),
            "unrelated_added_memory_ids": unrelated_ids,
            "unrelated_added_pointer_edges": [
                {"new_memory_id": new_id, "old_memory_id": old_id} for new_id, old_id in unrelated_pairs
            ],
        },
        "scope_comparison": _scope_summary(conn, inventory),
        "mode": "revalidate",
        "blocking_reasons": reasons,
        "remaining_blocked_components": [
            {key: component[key] for key in ("component_id", "memory_ids", "classification", "issue_codes")}
            for component in inventory.get("blocked_components", [])
        ],
        "remaining_blocked_components_present": bool(inventory.get("blocked_components")),
    })
    return result


def preview_memory_pointer_lifecycle_remediation_execution_payload(
    conn: Any, *,
    plan_version: str = POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
    execution_policy_version: str = POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION,
    execution_scope: str = "all_safe",
    approved_execution_manifest_json: str | None = None,
    expected_execution_manifest_fingerprint: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    del include_debug
    if plan_version != POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION:
        return _failure("unsupported_plan_version", status="unsupported_plan_version")
    if execution_policy_version != POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION:
        return _failure("unsupported_execution_policy_version", status="unsupported_execution_policy_version")
    if execution_scope not in EXECUTION_SCOPES:
        return _failure("invalid_execution_scope")
    try:
        if approved_execution_manifest_json is None:
            if expected_execution_manifest_fingerprint is not None:
                return _failure("unexpected_execution_manifest_fingerprint")
            manifest, inventory, _ = _build_manifest(conn, execution_scope)
            fingerprint = _hash(manifest)
            result = _base_result(manifest, fingerprint)
            result.update({
                "status": "execution_preview_ready",
                "mode": "build",
                "manifest_status": "built",
                "scope_comparison": _scope_summary(conn, inventory),
                "reason_codes": [],
                "blocking_findings": [],
                "remaining_blocked_components": [
                    {key: component[key] for key in ("component_id", "memory_ids", "classification", "issue_codes")}
                    for component in inventory.get("blocked_components", [])
                ],
                "remaining_blocked_components_present": bool(inventory.get("blocked_components")),
                "diagnostics": {
                    "unrelated_inventory_drift_detected": False,
                    "unrelated_added_memory_ids": [],
                    "unrelated_added_pointer_edges": [],
                },
            })
            return result
        if expected_execution_manifest_fingerprint is None:
            return _failure("expected_execution_manifest_fingerprint_required")
        manifest = _strict_manifest(approved_execution_manifest_json)
        if manifest["execution_scope"] != execution_scope:
            return _failure("execution_scope_manifest_mismatch")
        return _revalidate(conn, manifest, expected_execution_manifest_fingerprint)
    except ContractError as exc:
        result = _failure(exc.reason_codes[0] if exc.reason_codes else "invalid_execution_manifest")
        result["reason_codes"] = exc.reason_codes
        return result


__all__ = [
    "POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION",
    "POINTER_LIFECYCLE_EXECUTION_MANIFEST_SCHEMA_VERSION",
    "POINTER_LIFECYCLE_EXECUTION_PREVIEW_SCHEMA_VERSION",
    "POINTER_LIFECYCLE_FUTURE_APPLY_SCHEMA_VERSION",
    "FUTURE_OPERATION_IDENTITY_CONTRACT",
    "classify_existing_pointer_lifecycle_operation",
    "preview_memory_pointer_lifecycle_remediation_execution_payload",
]
