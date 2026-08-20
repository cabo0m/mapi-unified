from __future__ import annotations

from typing import Any, Callable


EVIDENCE_RELATION_PREVIEW_SCHEMA = "memory_v3_evidence_relation_preview.v1"
EVIDENCE_RELATION_APPLY_SCHEMA = "memory_v3_evidence_relation_apply.v1"
EVIDENCE_RELATION_ROLLBACK_PREVIEW_SCHEMA = "memory_v3_evidence_relation_rollback_preview.v1"
EVIDENCE_RELATION_ROLLBACK_SCHEMA = "memory_v3_evidence_relation_rollback.v1"
EVIDENCE_RELATION_ORIGIN_PREFIX = "memory_v3_evidence_relation:"
EVIDENCE_BOUND_RELATIONS = frozenset({"supports", "derived_from"})
EVIDENCE_KINDS = {
    "supports": frozenset({"same_source_event_ref", "explicit_support_attestation"}),
    "derived_from": frozenset({"explicit_source_memory_reference"}),
}
_INACTIVE_STATES = frozenset({"archived", "superseded", "expired", "rejected", "cancelled", "revoked"})
_ATTESTATION_REF_PREFIXES = ("source:", "memory_event:", "chat:", "git:", "operator:", "memory:")


def _text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _relation(value: Any) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized if normalized in EVIDENCE_BOUND_RELATIONS else None


def _memory_snapshot(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(memory["id"]),
        "updated_at": memory.get("updated_at"),
        "project_key": memory.get("project_key"),
        "scope_code": memory.get("scope_code"),
        "workspace_id": memory.get("workspace_id"),
        "activity_state": memory.get("activity_state"),
        "state_code": memory.get("state_code"),
        "memory_v2_status": memory.get("memory_v2_status"),
        "archived_at": memory.get("archived_at"),
        "source_event_ref": memory.get("source_event_ref"),
    }


def _link_snapshot(link: dict[str, Any] | None) -> dict[str, Any] | None:
    if link is None:
        return None
    return {
        "id": int(link["id"]),
        "from_memory_id": int(link["from_memory_id"]),
        "to_memory_id": int(link["to_memory_id"]),
        "relation_type": link.get("relation_type"),
        "origin": link.get("origin"),
        "created_at": link.get("created_at"),
        "archived_at": link.get("archived_at"),
        "workspace_id": link.get("workspace_id"),
        "visibility_scope": link.get("visibility_scope"),
    }


def _same_domain(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        (left.get("project_key") or None) == (right.get("project_key") or None)
        and (left.get("scope_code") or None) == (right.get("scope_code") or None)
        and int(left.get("workspace_id") or 1) == int(right.get("workspace_id") or 1)
    )


def _eligible_memory(memory: dict[str, Any]) -> bool:
    if memory.get("archived_at") is not None:
        return False
    values = {
        str(memory.get("activity_state") or "active").strip().lower(),
        str(memory.get("state_code") or "active").strip().lower(),
        str(memory.get("memory_v2_status") or "active").strip().lower(),
    }
    return not bool(values & _INACTIVE_STATES)


def _load_memory(conn: Any, memory_id: int, *, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM memories WHERE id=?", (int(memory_id),)).fetchone()
    return None if row is None else row_to_dict(row)


def _active_relation_link(
    conn: Any,
    *,
    relation: str,
    from_memory_id: int,
    to_memory_id: int,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM memory_links
        WHERE from_memory_id=? AND to_memory_id=? AND relation_type=? AND archived_at IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (int(from_memory_id), int(to_memory_id), relation),
    ).fetchone()
    return None if row is None else row_to_dict(row)


def _operation_key(
    *,
    relation: str,
    from_memory_id: int,
    to_memory_id: int,
    evidence_kind: str,
    evidence_ref: str,
    reason: str,
    canonical_json_hash: Callable[[Any], str],
) -> str:
    digest = canonical_json_hash(
        {
            "relation": relation,
            "from_memory_id": int(from_memory_id),
            "to_memory_id": int(to_memory_id),
            "evidence_kind": evidence_kind,
            "evidence_ref": evidence_ref,
            "reason": reason,
        }
    )
    return f"evidence_relation:{relation}:{from_memory_id}:{to_memory_id}:{digest}"


def preview_evidence_relation_payload(
    conn: Any,
    *,
    relation: str,
    from_memory_id: int,
    to_memory_id: int,
    evidence_kind: str | None,
    evidence_ref: str | None,
    reason: str | None,
    project_key: str | None,
    include_debug: bool,
    row_to_dict: Callable[[Any], dict[str, Any]],
    canonical_json_hash: Callable[[Any], str],
) -> dict[str, Any]:
    normalized_relation = _relation(relation)
    normalized_evidence_kind = str(evidence_kind or "").strip().lower().replace("-", "_").replace(" ", "_")
    normalized_evidence_ref = _text(evidence_ref)
    normalized_reason = _text(reason)
    blockers: list[str] = []
    warnings: list[str] = []

    if normalized_relation is None:
        return {
            "status": "blocked",
            "schema": EVIDENCE_RELATION_PREVIEW_SCHEMA,
            "relation": relation,
            "blocking_reasons": ["relation_not_supported_by_evidence_apply"],
            "allowed_relations": sorted(EVIDENCE_BOUND_RELATIONS),
            "safety": {"read_only": True, "mutations_performed": 0, "apply_supported": False},
        }
    if int(from_memory_id) <= 0 or int(to_memory_id) <= 0:
        blockers.append("memory_ids_must_be_positive")
    if int(from_memory_id) == int(to_memory_id):
        blockers.append("same_memory_id")

    left = _load_memory(conn, int(from_memory_id), row_to_dict=row_to_dict) if int(from_memory_id) > 0 else None
    right = _load_memory(conn, int(to_memory_id), row_to_dict=row_to_dict) if int(to_memory_id) > 0 else None
    if left is None:
        blockers.append("from_memory_not_found")
    if right is None:
        blockers.append("to_memory_not_found")

    same_domain = bool(left and right and _same_domain(left, right))
    if left is not None and right is not None and not same_domain:
        blockers.append("domain_mismatch")
    if left is not None and not _eligible_memory(left):
        blockers.append("from_memory_ineligible_lifecycle")
    if right is not None and not _eligible_memory(right):
        blockers.append("to_memory_ineligible_lifecycle")
    if project_key is not None and left is not None and (left.get("project_key") or None) != project_key:
        blockers.append("project_key_mismatch")

    allowed_kinds = EVIDENCE_KINDS[normalized_relation]
    if not normalized_evidence_kind:
        blockers.append("evidence_kind_required")
    elif normalized_evidence_kind not in allowed_kinds:
        blockers.append("unsupported_evidence_kind")
    if normalized_evidence_ref is None:
        blockers.append("evidence_ref_required")
    if normalized_reason is None:
        blockers.append("reason_required")

    evidence: dict[str, Any] = {
        "kind": normalized_evidence_kind or None,
        "ref": normalized_evidence_ref,
        "reason": normalized_reason,
        "same_domain": same_domain,
        "semantic_similarity_used": False,
        "tag_overlap_used": False,
    }
    if left is not None:
        evidence["from_memory"] = _memory_snapshot(left)
    if right is not None:
        evidence["to_memory"] = _memory_snapshot(right)

    if normalized_relation == "supports" and normalized_evidence_kind == "same_source_event_ref" and left and right:
        left_ref = _text(left.get("source_event_ref"))
        right_ref = _text(right.get("source_event_ref"))
        same_source = bool(left_ref and right_ref and left_ref == right_ref)
        evidence["same_source_event_ref"] = same_source
        evidence["source_event_ref"] = left_ref if same_source else None
        if not same_source:
            blockers.append("same_source_event_ref_required")
        elif normalized_evidence_ref != left_ref:
            blockers.append("evidence_ref_must_match_source_event_ref")
    elif normalized_relation == "supports" and normalized_evidence_kind == "explicit_support_attestation":
        if normalized_evidence_ref is not None and not normalized_evidence_ref.lower().startswith(_ATTESTATION_REF_PREFIXES):
            blockers.append("unsupported_attestation_ref_format")
        evidence["explicit_attestation"] = True
    elif normalized_relation == "derived_from" and normalized_evidence_kind == "explicit_source_memory_reference":
        expected_ref = f"memory:{int(to_memory_id)}"
        evidence["expected_source_memory_ref"] = expected_ref
        evidence["explicit_source_memory_reference"] = normalized_evidence_ref == expected_ref
        if normalized_evidence_ref != expected_ref:
            blockers.append("evidence_ref_must_equal_source_memory_reference")

    active_link = None
    if left is not None and right is not None:
        active_link = _active_relation_link(
            conn,
            relation=normalized_relation,
            from_memory_id=int(from_memory_id),
            to_memory_id=int(to_memory_id),
            row_to_dict=row_to_dict,
        )
    if active_link is not None:
        warnings.append("relation_already_materialized")

    input_payload = {
        "relation": normalized_relation,
        "from_memory_id": int(from_memory_id),
        "to_memory_id": int(to_memory_id),
        "evidence_kind": normalized_evidence_kind or None,
        "evidence_ref": normalized_evidence_ref,
        "reason": normalized_reason,
        "project_key": project_key,
    }
    candidate_payload = {
        "from_memory": _memory_snapshot(left) if left is not None else None,
        "to_memory": _memory_snapshot(right) if right is not None else None,
        "active_link": _link_snapshot(active_link),
    }
    operation_key = None
    if normalized_evidence_kind and normalized_evidence_ref and normalized_reason:
        operation_key = _operation_key(
            relation=normalized_relation,
            from_memory_id=int(from_memory_id),
            to_memory_id=int(to_memory_id),
            evidence_kind=normalized_evidence_kind,
            evidence_ref=normalized_evidence_ref,
            reason=normalized_reason,
            canonical_json_hash=canonical_json_hash,
        )
    status = "already_satisfied" if active_link is not None and not blockers else ("preview_ready" if not blockers else "blocked")
    preview_hash = canonical_json_hash(
        {
            "input": input_payload,
            "candidate": candidate_payload,
            "status": status,
            "blocking_reasons": sorted(set(blockers)),
        }
    )
    apply_supported = status == "preview_ready"
    result: dict[str, Any] = {
        "status": status,
        "schema": EVIDENCE_RELATION_PREVIEW_SCHEMA,
        "relation": normalized_relation,
        "input": input_payload,
        "evidence": evidence,
        "active_link": _link_snapshot(active_link),
        "blocking_reasons": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "allowed_evidence_kinds": sorted(allowed_kinds),
        "operation_key": operation_key,
        "preview_hash": preview_hash,
        "hash_algorithm": "sha256:canonical-json:v1",
        "planned_change": None if not apply_supported else {
            "create_link": {
                "from_memory_id": int(from_memory_id),
                "to_memory_id": int(to_memory_id),
                "relation_type": normalized_relation,
                "weight": 1.0,
                "origin": f"{EVIDENCE_RELATION_ORIGIN_PREFIX}{operation_key}",
            },
            "events": ["memory_v3.relation_applied", "memory_v3.relation_received"],
        },
        "safety": {
            "read_only": True,
            "mutations_performed": 0,
            "apply_supported": apply_supported,
            "apply_requires_expected_preview_hash": apply_supported,
            "apply_requires_explicit_confirmation": apply_supported,
            "semantic_similarity_used_as_evidence": False,
        },
        "operator_next_action": "apply_with_expected_preview_hash_and_confirmation" if apply_supported else "inspect",
    }
    if include_debug:
        result["debug"] = {"candidate_payload": candidate_payload}
    return result


def apply_evidence_relation_payload(
    conn: Any,
    *,
    relation: str,
    from_memory_id: int,
    to_memory_id: int,
    evidence_kind: str,
    evidence_ref: str,
    reason: str,
    expected_preview_hash: str,
    applied_by: str,
    confirm_evidence_bound_relation: bool,
    project_key: str | None,
    include_debug: bool,
    row_to_dict: Callable[[Any], dict[str, Any]],
    canonical_json_hash: Callable[[Any], str],
    utc_now_iso: Callable[[], str],
    insert_memory_event: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    normalized_expected_hash = _text(expected_preview_hash)
    normalized_applied_by = _text(applied_by)
    if normalized_expected_hash is None:
        return {"status": "blocked", "schema": EVIDENCE_RELATION_APPLY_SCHEMA, "blocking_reasons": ["expected_preview_hash_required"]}
    if normalized_applied_by is None:
        return {"status": "blocked", "schema": EVIDENCE_RELATION_APPLY_SCHEMA, "blocking_reasons": ["applied_by_required"]}

    preview = preview_evidence_relation_payload(
        conn,
        relation=relation,
        from_memory_id=from_memory_id,
        to_memory_id=to_memory_id,
        evidence_kind=evidence_kind,
        evidence_ref=evidence_ref,
        reason=reason,
        project_key=project_key,
        include_debug=include_debug,
        row_to_dict=row_to_dict,
        canonical_json_hash=canonical_json_hash,
    )
    if preview["status"] == "already_satisfied":
        return {
            "status": "already_applied",
            "schema": EVIDENCE_RELATION_APPLY_SCHEMA,
            "link": preview.get("active_link"),
            "operation_key": preview.get("operation_key"),
            "apply_run_created": False,
            "blocking_reasons": [],
        }
    if preview["status"] != "preview_ready":
        return {
            "status": "blocked",
            "schema": EVIDENCE_RELATION_APPLY_SCHEMA,
            "blocking_reasons": list(preview.get("blocking_reasons") or []),
            "preview_status": preview["status"],
            "apply_run_created": False,
        }
    if normalized_expected_hash != str(preview["preview_hash"]):
        return {
            "status": "stale_preview",
            "schema": EVIDENCE_RELATION_APPLY_SCHEMA,
            "blocking_reasons": ["expected_preview_hash_mismatch"],
            "expected_preview_hash": normalized_expected_hash,
            "current_preview_hash": preview["preview_hash"],
            "apply_run_created": False,
        }
    if not bool(confirm_evidence_bound_relation):
        return {
            "status": "blocked",
            "schema": EVIDENCE_RELATION_APPLY_SCHEMA,
            "blocking_reasons": ["explicit_relation_confirmation_required"],
            "apply_run_created": False,
        }

    operation_key = str(preview["operation_key"])
    origin = f"{EVIDENCE_RELATION_ORIGIN_PREFIX}{operation_key}"
    existing_origin_row = conn.execute(
        "SELECT * FROM memory_links WHERE origin=? ORDER BY id DESC LIMIT 1",
        (origin,),
    ).fetchone()
    if existing_origin_row is not None:
        existing = row_to_dict(existing_origin_row)
        if existing.get("archived_at") is None:
            return {
                "status": "already_applied",
                "schema": EVIDENCE_RELATION_APPLY_SCHEMA,
                "link": _link_snapshot(existing),
                "operation_key": operation_key,
                "apply_run_created": False,
                "blocking_reasons": [],
            }
        return {
            "status": "blocked",
            "schema": EVIDENCE_RELATION_APPLY_SCHEMA,
            "blocking_reasons": ["operation_previously_rolled_back"],
            "operation_key": operation_key,
            "apply_run_created": False,
        }

    applied_at = utc_now_iso()
    try:
        conn.execute("BEGIN")
        left = _load_memory(conn, int(from_memory_id), row_to_dict=row_to_dict)
        workspace_id = None if left is None else left.get("workspace_id")
        cursor = conn.execute(
            """
            INSERT INTO memory_links(
                from_memory_id,to_memory_id,relation_type,weight,origin,created_at,archived_at,workspace_id,visibility_scope
            ) VALUES(?,?,?,?,?,?,NULL,?,'inherited')
            """,
            (int(from_memory_id), int(to_memory_id), preview["relation"], 1.0, origin, applied_at, workspace_id),
        )
        link_id = int(cursor.lastrowid)
        event_payload = {
            "relation": preview["relation"],
            "from_memory_id": int(from_memory_id),
            "to_memory_id": int(to_memory_id),
            "link_id": link_id,
            "operation_key": operation_key,
            "evidence_kind": preview["input"]["evidence_kind"],
            "evidence_ref": preview["input"]["evidence_ref"],
            "reason": preview["input"]["reason"],
            "preview_hash": preview["preview_hash"],
            "applied_by": normalized_applied_by,
        }
        from_event = insert_memory_event(
            conn,
            memory_id=int(from_memory_id),
            event_type="memory_v3.relation_applied",
            payload=event_payload,
        )
        to_event = insert_memory_event(
            conn,
            memory_id=int(to_memory_id),
            event_type="memory_v3.relation_received",
            payload=event_payload,
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return {
            "status": "error",
            "schema": EVIDENCE_RELATION_APPLY_SCHEMA,
            "operation_key": operation_key,
            "apply_run_created": False,
            "error": str(exc),
        }

    link_row = conn.execute("SELECT * FROM memory_links WHERE id=?", (link_id,)).fetchone()
    result: dict[str, Any] = {
        "status": "applied",
        "schema": EVIDENCE_RELATION_APPLY_SCHEMA,
        "relation": preview["relation"],
        "operation_key": operation_key,
        "preview_hash": preview["preview_hash"],
        "link": _link_snapshot(row_to_dict(link_row)),
        "event_ids": [int(from_event["id"]), int(to_event["id"])],
        "applied_at": applied_at,
        "applied_by": normalized_applied_by,
        "apply_run_created": True,
        "rollback_available": True,
        "safety": {"evidence_bound": True, "semantic_similarity_used_as_evidence": False},
    }
    if include_debug:
        result["debug"] = {"evidence": preview["evidence"]}
    return result


def preview_evidence_relation_rollback_payload(
    conn: Any,
    *,
    link_id: int,
    include_debug: bool,
    row_to_dict: Callable[[Any], dict[str, Any]],
    canonical_json_hash: Callable[[Any], str],
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM memory_links WHERE id=?", (int(link_id),)).fetchone()
    if row is None:
        return {
            "status": "blocked",
            "schema": EVIDENCE_RELATION_ROLLBACK_PREVIEW_SCHEMA,
            "link_id": int(link_id),
            "blocking_reasons": ["link_not_found"],
            "safety": {"read_only": True, "mutations_performed": 0},
        }
    link = row_to_dict(row)
    origin = str(link.get("origin") or "")
    blockers: list[str] = []
    if not origin.startswith(EVIDENCE_RELATION_ORIGIN_PREFIX):
        blockers.append("link_not_created_by_evidence_relation_apply")
    if str(link.get("relation_type") or "") not in EVIDENCE_BOUND_RELATIONS:
        blockers.append("relation_not_rollback_managed_here")
    if link.get("archived_at") is not None:
        status = "already_rolled_back"
    elif blockers:
        status = "blocked"
    else:
        status = "preview_ready"
    snapshot = _link_snapshot(link)
    rollback_hash = canonical_json_hash({"link": snapshot, "status": status, "blocking_reasons": sorted(set(blockers))})
    result: dict[str, Any] = {
        "status": status,
        "schema": EVIDENCE_RELATION_ROLLBACK_PREVIEW_SCHEMA,
        "link_id": int(link_id),
        "link": snapshot,
        "blocking_reasons": sorted(set(blockers)),
        "rollback_preview_hash": rollback_hash,
        "hash_algorithm": "sha256:canonical-json:v1",
        "planned_change": None if status != "preview_ready" else {"archive_link_id": int(link_id)},
        "safety": {"read_only": True, "mutations_performed": 0},
    }
    if include_debug:
        result["debug"] = {"origin": origin}
    return result


def rollback_evidence_relation_payload(
    conn: Any,
    *,
    link_id: int,
    expected_rollback_preview_hash: str,
    rolled_back_by: str,
    notes: str | None,
    include_debug: bool,
    row_to_dict: Callable[[Any], dict[str, Any]],
    canonical_json_hash: Callable[[Any], str],
    utc_now_iso: Callable[[], str],
    insert_memory_event: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    expected_hash = _text(expected_rollback_preview_hash)
    actor = _text(rolled_back_by)
    if expected_hash is None:
        return {"status": "blocked", "schema": EVIDENCE_RELATION_ROLLBACK_SCHEMA, "blocking_reasons": ["expected_rollback_preview_hash_required"]}
    if actor is None:
        return {"status": "blocked", "schema": EVIDENCE_RELATION_ROLLBACK_SCHEMA, "blocking_reasons": ["rolled_back_by_required"]}
    preview = preview_evidence_relation_rollback_payload(
        conn,
        link_id=int(link_id),
        include_debug=include_debug,
        row_to_dict=row_to_dict,
        canonical_json_hash=canonical_json_hash,
    )
    if preview["status"] == "already_rolled_back":
        return {"status": "already_rolled_back", "schema": EVIDENCE_RELATION_ROLLBACK_SCHEMA, "link_id": int(link_id), "blocking_reasons": []}
    if preview["status"] != "preview_ready":
        return {"status": "blocked", "schema": EVIDENCE_RELATION_ROLLBACK_SCHEMA, "link_id": int(link_id), "blocking_reasons": list(preview.get("blocking_reasons") or [])}
    if expected_hash != str(preview["rollback_preview_hash"]):
        return {
            "status": "stale_rollback_preview",
            "schema": EVIDENCE_RELATION_ROLLBACK_SCHEMA,
            "link_id": int(link_id),
            "blocking_reasons": ["expected_rollback_preview_hash_mismatch"],
            "expected_rollback_preview_hash": expected_hash,
            "current_rollback_preview_hash": preview["rollback_preview_hash"],
        }
    link = dict(preview["link"])
    rolled_back_at = utc_now_iso()
    payload = {
        "relation": link["relation_type"],
        "from_memory_id": int(link["from_memory_id"]),
        "to_memory_id": int(link["to_memory_id"]),
        "link_id": int(link_id),
        "origin": link.get("origin"),
        "rollback_preview_hash": expected_hash,
        "rolled_back_by": actor,
        "notes": _text(notes),
    }
    try:
        conn.execute("BEGIN")
        cursor = conn.execute(
            "UPDATE memory_links SET archived_at=? WHERE id=? AND archived_at IS NULL",
            (rolled_back_at, int(link_id)),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("link_state_changed_before_rollback")
        from_event = insert_memory_event(
            conn,
            memory_id=int(link["from_memory_id"]),
            event_type="memory_v3.relation_rolled_back",
            payload=payload,
        )
        to_event = insert_memory_event(
            conn,
            memory_id=int(link["to_memory_id"]),
            event_type="memory_v3.relation_rollback_received",
            payload=payload,
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return {"status": "error", "schema": EVIDENCE_RELATION_ROLLBACK_SCHEMA, "link_id": int(link_id), "error": str(exc)}
    result: dict[str, Any] = {
        "status": "rolled_back",
        "schema": EVIDENCE_RELATION_ROLLBACK_SCHEMA,
        "link_id": int(link_id),
        "rolled_back_at": rolled_back_at,
        "rolled_back_by": actor,
        "event_ids": [int(from_event["id"]), int(to_event["id"])],
    }
    if include_debug:
        result["debug"] = {"archived_link": {**link, "archived_at": rolled_back_at}}
    return result
