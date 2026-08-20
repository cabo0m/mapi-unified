from __future__ import annotations

from typing import Any, Callable


RELATION_CONTRACT_SCHEMA = "mapi_memory_relation_contracts.v1"
RELATION_PREVIEW_SCHEMA = "mapi_memory_relation_preview.v1"
CANONICAL_MEMORY_RELATIONS = (
    "supports",
    "contradicts",
    "supersedes",
    "refines",
    "derived_from",
    "about_project",
)


_RELATION_CONTRACTS: dict[str, dict[str, Any]] = {
    "supports": {
        "relation": "supports",
        "status": "implemented_guarded",
        "storage_model": "memory_link_plus_audit_events",
        "durable_memory_link_relation": "supports",
        "existing_route": "memory.relation_preview -> memory.relation_apply; capture reconciliation reinforcement remains event-only when there is no second memory node",
        "evidence_required": ["explicit_from_memory_id", "explicit_to_memory_id", "same_domain", "eligible_lifecycle_state", "evidence_kind", "evidence_ref", "reason", "fresh_preview_hash", "explicit_confirmation"],
        "allowed_evidence_kinds": ["same_source_event_ref", "explicit_support_attestation"],
        "semantic_similarity_alone_allowed": False,
        "direct_link_apply_allowed": False,
        "notes": "Durable supports links require guarded evidence-bound apply. Same-source evidence is structurally validated; explicit attestation requires an auditable evidence_ref and confirmation. Capture reinforcement remains valid as event-only evidence when no supporting memory node exists.",
    },
    "contradicts": {
        "relation": "contradicts",
        "status": "implemented_reviewed",
        "storage_model": "memory_link_plus_conflicted_lifecycle",
        "durable_memory_link_relation": "contradicts",
        "existing_route": "memory.capture_reconciliation -> conflict_review -> open_unresolved_conflict_review",
        "evidence_required": ["approved_capture_item", "fresh_preview_hash", "explicit_contradiction_target", "same_project", "same_scope", "eligible_lifecycle_state"],
        "semantic_similarity_alone_allowed": False,
        "direct_link_apply_allowed": False,
        "notes": "Conflict apply creates/reuses a contradicts link and moves both memories to conflicted lifecycle state; it does not auto-resolve the conflict.",
    },
    "supersedes": {
        "relation": "supersedes",
        "status": "implemented_guarded",
        "storage_model": "memory_link_plus_lifecycle_snapshot",
        "durable_memory_link_relation": "supersedes",
        "existing_route": "memory.supersession_preview -> memory.supersession_apply",
        "evidence_required": ["explicit_new_memory_id", "explicit_old_memory_id", "relation_kind=correction|replacement", "reason", "fresh_preview_hash"],
        "additional_structural_mirror": "supersedes_memory_id",
        "semantic_similarity_alone_allowed": False,
        "direct_link_apply_allowed": False,
        "notes": "Memory Linking V2 may mirror an exact supersedes_memory_id pointer as a structural supersedes link; normal lifecycle mutation remains guarded by supersession preview/apply.",
    },
    "refines": {
        "relation": "refines",
        "status": "implemented_as_lifecycle_projection",
        "storage_model": "supersedes_link_with_refinement_relation_kind",
        "durable_memory_link_relation": "supersedes",
        "lifecycle_relation_kind": "refinement",
        "existing_route": "memory.supersession_preview -> memory.supersession_apply relation_kind=refinement",
        "evidence_required": ["explicit_new_memory_id", "explicit_old_memory_id", "relation_kind=refinement", "reason", "fresh_preview_hash"],
        "semantic_similarity_alone_allowed": False,
        "direct_link_apply_allowed": False,
        "notes": "Canonical semantic relation is refines, but current lifecycle storage intentionally uses relation_type=supersedes plus relation_kind=refinement. A second refines link is not created.",
    },
    "derived_from": {
        "relation": "derived_from",
        "status": "implemented_guarded",
        "storage_model": "memory_link_plus_audit_events",
        "durable_memory_link_relation": "derived_from",
        "existing_route": "memory.relation_preview -> memory.relation_apply",
        "evidence_required": ["explicit_derived_memory_id", "explicit_source_memory_id", "same_domain", "eligible_lifecycle_state", "evidence_kind=explicit_source_memory_reference", "evidence_ref=memory:<source_id>", "reason", "fresh_preview_hash", "explicit_confirmation"],
        "allowed_evidence_kinds": ["explicit_source_memory_reference"],
        "semantic_similarity_alone_allowed": False,
        "direct_link_apply_allowed": False,
        "forbidden_inferences": ["semantic_similarity", "tags_overlap", "read_model_source_memory_ids", "source_event_ref_only"],
        "notes": "The durable derivation contract is now the guarded relation assertion itself: explicit derived/source memory IDs, structured source-memory reference, preview hash, confirmation, materialized derived_from link and append-only audit events.",
    },
    "about_project": {
        "relation": "about_project",
        "status": "implemented_virtual",
        "storage_model": "memories.project_key",
        "durable_memory_link_relation": None,
        "existing_route": "project_key canonicalization/write routing",
        "evidence_required": ["canonical_project_key_on_memory"],
        "semantic_similarity_alone_allowed": False,
        "direct_link_apply_allowed": False,
        "virtual_relation": True,
        "notes": "Projects are not nodes in memory_links. Project membership is represented structurally by memories.project_key, so about_project is a virtual relation rather than a synthetic memory link.",
    },
}


def normalize_relation(value: Any) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized if normalized in _RELATION_CONTRACTS else None


def get_relation_contracts_payload(*, relation: str | None = None) -> dict[str, Any]:
    normalized = normalize_relation(relation) if relation is not None else None
    if relation is not None and normalized is None:
        return {
            "status": "error",
            "schema": RELATION_CONTRACT_SCHEMA,
            "error": "unsupported_canonical_relation",
            "actual": relation,
            "allowed_values": list(CANONICAL_MEMORY_RELATIONS),
        }
    items = [dict(_RELATION_CONTRACTS[name]) for name in CANONICAL_MEMORY_RELATIONS if normalized is None or name == normalized]
    return {
        "status": "ok",
        "schema": RELATION_CONTRACT_SCHEMA,
        "count": len(items),
        "relations": items,
        "allowed_values": list(CANONICAL_MEMORY_RELATIONS),
        "invariants": {
            "semantic_similarity_alone_can_create_durable_relation": False,
            "legacy_link_memories_is_not_canonical_relation_apply": True,
            "new_truth_queue_created": False,
            "supports_and_derived_from_guarded_apply": True,
        },
    }


def _same_domain(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        (left.get("project_key") or None) == (right.get("project_key") or None)
        and (left.get("scope_code") or None) == (right.get("scope_code") or None)
        and int(left.get("workspace_id") or 1) == int(right.get("workspace_id") or 1)
    )


def _memory_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(item["id"]),
        "project_key": item.get("project_key"),
        "scope_code": item.get("scope_code"),
        "state_code": item.get("state_code"),
        "memory_v2_status": item.get("memory_v2_status"),
        "supersedes_memory_id": item.get("supersedes_memory_id"),
        "parent_memory_id": item.get("parent_memory_id"),
        "source_event_ref": item.get("source_event_ref"),
    }


def preview_relation_payload(
    conn: Any,
    *,
    relation: str,
    from_memory_id: int | None = None,
    to_memory_id: int | None = None,
    project_key: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    normalized = normalize_relation(relation)
    if normalized is None:
        return {
            "status": "error",
            "schema": RELATION_PREVIEW_SCHEMA,
            "error": "unsupported_canonical_relation",
            "actual": relation,
            "allowed_values": list(CANONICAL_MEMORY_RELATIONS),
            "safety": {"read_only": True, "mutations_performed": 0},
        }
    contract = dict(_RELATION_CONTRACTS[normalized])
    result: dict[str, Any] = {
        "status": "preview_ready",
        "schema": RELATION_PREVIEW_SCHEMA,
        "relation": normalized,
        "contract": contract,
        "apply": {
            "supported_directly_here": False,
            "route": contract.get("existing_route"),
            "eligible": False,
            "blocking_reasons": [],
        },
        "evidence": {},
        "safety": {
            "read_only": True,
            "mutations_performed": 0,
            "semantic_similarity_used_as_evidence": False,
        },
    }

    if normalized == "about_project":
        if from_memory_id is None:
            result["apply"]["blocking_reasons"].append("memory_id_required")
            return result
        row = conn.execute("SELECT * FROM memories WHERE id=?", (int(from_memory_id),)).fetchone()
        if row is None:
            result["apply"]["blocking_reasons"].append("memory_not_found")
            return result
        memory = row_to_dict(row)
        canonical_project = normalize_optional_text(memory.get("project_key"))
        requested_project = normalize_optional_text(project_key)
        result["evidence"] = {
            "memory": _memory_snapshot(memory),
            "canonical_project_key": canonical_project,
            "requested_project_key": requested_project,
            "virtual_relation_present": bool(canonical_project and (requested_project is None or requested_project == canonical_project)),
        }
        result["apply"]["eligible"] = False
        if not canonical_project:
            result["apply"]["blocking_reasons"].append("memory_project_key_missing")
        elif requested_project is not None and requested_project != canonical_project:
            result["apply"]["blocking_reasons"].append("project_key_mismatch")
        else:
            result["status"] = "virtual_relation_present"
        return result

    if from_memory_id is None or to_memory_id is None:
        result["apply"]["blocking_reasons"].append("from_and_to_memory_id_required")
        return result
    rows = conn.execute(
        "SELECT * FROM memories WHERE id IN (?, ?) ORDER BY id",
        (int(from_memory_id), int(to_memory_id)),
    ).fetchall()
    by_id = {int(row["id"]): row_to_dict(row) for row in rows}
    left = by_id.get(int(from_memory_id))
    right = by_id.get(int(to_memory_id))
    if left is None or right is None:
        result["apply"]["blocking_reasons"].append("memory_not_found")
        return result
    same_domain = _same_domain(left, right)
    result["evidence"] = {
        "from_memory": _memory_snapshot(left),
        "to_memory": _memory_snapshot(right),
        "same_domain": same_domain,
    }

    if normalized == "supersedes":
        pointer_match = int(left.get("supersedes_memory_id") or 0) == int(to_memory_id)
        result["evidence"]["supersedes_pointer_match"] = pointer_match
        result["apply"]["eligible"] = bool(same_domain and pointer_match)
        if not same_domain:
            result["apply"]["blocking_reasons"].append("domain_mismatch")
        if not pointer_match:
            result["apply"]["blocking_reasons"].append("structural_pointer_missing_for_direct_mirror")
        result["apply"]["route"] = "memory.supersession_preview/apply for lifecycle change; linking pass may mirror existing pointer"
    elif normalized == "refines":
        result["evidence"]["storage_projection"] = {"relation_type": "supersedes", "relation_kind": "refinement"}
        result["apply"]["eligible"] = False
        result["apply"]["blocking_reasons"].append("use_guarded_supersession_route_with_relation_kind_refinement")
    elif normalized == "contradicts":
        result["evidence"]["explicit_target_compatible"] = same_domain
        result["apply"]["eligible"] = False
        if not same_domain:
            result["apply"]["blocking_reasons"].append("domain_mismatch")
        result["apply"]["blocking_reasons"].append("requires_approved_capture_conflict_review")
    elif normalized == "supports":
        same_source = bool(left.get("source_event_ref") and left.get("source_event_ref") == right.get("source_event_ref"))
        result["evidence"]["same_source_event_ref"] = same_source
        result["apply"]["eligible"] = False
        result["apply"]["blocking_reasons"].append("supports_link_apply_not_implemented_use_reinforcement_event_route")
    elif normalized == "derived_from":
        result["apply"]["eligible"] = False
        result["apply"]["blocking_reasons"].append("missing_explicit_durable_derivation_contract")

    return result
