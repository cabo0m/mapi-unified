from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any, Callable


SCHEMA = "mapi_canonical_truth_review.v1"
RECOMMENDATIONS = (
    "archive_from_active_truth",
    "preserve_legacy_lineage",
    "requires_operator_review",
)
_REVIEW_RELATIONS = frozenset({"supports", "contradicts", "refines"})
_AUTO_SUPPORT_ORIGINS = frozenset({"consolidation_v1_auto"})
_MANUAL_SUPPORT_ORIGINS = frozenset({"manual_forced_linking_pass"})
_LEGACY_CONTRADICT_ORIGINS = frozenset({"conflicts_v1_auto", "manual_fallback_after_run_conflicts_error"})
_EXPLICIT_REFINES_ORIGINS = frozenset({"memory_write:user_explicit", "memory_write:agent_autonomous"})
EXPLICIT_LEGACY_REFINES_ORIGINS = _EXPLICIT_REFINES_ORIGINS


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _active_state(row: dict[str, Any]) -> bool:
    state = _text(row.get("state_code") or row.get("memory_v2_status") or row.get("activity_state")).lower()
    return state not in {"archived", "superseded", "expired", "rejected", "cancelled"} and row.get("archived_at") is None


def _same_domain(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        (left.get("project_key") or None) == (right.get("project_key") or None)
        and (left.get("scope_code") or None) == (right.get("scope_code") or None)
        and int(left.get("workspace_id") or 1) == int(right.get("workspace_id") or 1)
    )


def _matching_refines_events(
    conn: Any,
    *,
    from_memory_id: int,
    to_memory_id: int,
    origin: str,
) -> dict[str, Any]:
    outgoing = []
    for row in conn.execute(
        "SELECT id,event_type,payload_json,created_at FROM memory_events WHERE memory_id=? AND event_type='version.refines' ORDER BY id",
        (int(from_memory_id),),
    ).fetchall():
        payload = _json(row["payload_json"])
        if int(payload.get("old_memory_id") or 0) == int(to_memory_id) and _text(payload.get("relation")) == "refines":
            outgoing.append({"id": int(row["id"]), "payload": payload, "created_at": row["created_at"]})
    incoming = []
    for row in conn.execute(
        "SELECT id,event_type,payload_json,created_at FROM memory_events WHERE memory_id=? AND event_type='version.refines_by' ORDER BY id",
        (int(to_memory_id),),
    ).fetchall():
        payload = _json(row["payload_json"])
        if int(payload.get("new_memory_id") or 0) == int(from_memory_id) and _text(payload.get("relation")) == "refines":
            incoming.append({"id": int(row["id"]), "payload": payload, "created_at": row["created_at"]})
    source_match = bool(
        outgoing
        and incoming
        and _text(outgoing[-1]["payload"].get("source")) == origin
        and _text(incoming[-1]["payload"].get("source")) == origin
    )
    return {
        "outgoing_event_ids": [item["id"] for item in outgoing],
        "incoming_event_ids": [item["id"] for item in incoming],
        "event_pair_present": bool(outgoing and incoming),
        "event_source_matches_link_origin": source_match,
    }


def matching_refines_event_evidence(
    conn: Any,
    *,
    from_memory_id: int,
    to_memory_id: int,
    origin: str,
) -> dict[str, Any]:
    """Public structural helper for R6 audits; no content or semantic evidence."""
    return _matching_refines_events(
        conn,
        from_memory_id=int(from_memory_id),
        to_memory_id=int(to_memory_id),
        origin=str(origin or ""),
    )


def _conflict_review_evidence(conn: Any, left_id: int, right_id: int) -> dict[str, Any]:
    endpoint_ids = (int(left_id), int(right_id))
    event_rows = conn.execute(
        """
        SELECT id,memory_id,event_type,payload_json,created_at
        FROM memory_events
        WHERE memory_id IN (?,?) AND (
            event_type LIKE 'conflict.%'
            OR event_type LIKE 'memory_v3.conflict%'
            OR event_type='memory_v3.capture_conflict_opened'
        )
        ORDER BY id
        """,
        endpoint_ids,
    ).fetchall()
    reviewed = []
    for row in event_rows:
        payload = _json(row["payload_json"])
        ids = {
            int(value)
            for key in ("memory_a_id", "memory_b_id", "other_memory_id", "related_memory_id", "new_memory_id", "old_memory_id")
            for value in [payload.get(key)]
            if str(value or "").isdigit()
        }
        base = payload.get("base_memory_ids")
        if isinstance(base, list):
            ids.update(int(value) for value in base if str(value).isdigit())
        if not ids or set(endpoint_ids).issubset(ids) or row["event_type"].startswith("memory_v3.conflict"):
            reviewed.append(int(row["id"]))
    return {
        "review_event_ids": reviewed,
        "review_event_count": len(reviewed),
        "review_evidence_present": bool(reviewed),
    }


def _support_evidence(conn: Any, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    same_source = bool(
        _text(left.get("source_event_ref"))
        and _text(left.get("source_event_ref")) == _text(right.get("source_event_ref"))
    )
    reinforce_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM memory_events WHERE memory_id IN (?,?) AND event_type='memory_v3.capture_reinforced'",
            (int(left["id"]), int(right["id"])),
        ).fetchone()[0]
    )
    return {
        "same_source_event_ref": same_source,
        "source_event_ref": left.get("source_event_ref") if same_source else None,
        "reinforcement_event_count": reinforce_count,
        "current_contract_evidence_present": bool(same_source or reinforce_count > 0),
    }


def _consumer_impact(
    conn: Any,
    *,
    relation: str,
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    if relation == "supports":
        target_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_links WHERE archived_at IS NULL AND to_memory_id=? AND relation_type='supports'",
                (int(right["id"]),),
            ).fetchone()[0]
        )
        return {
            "active_consumers": ["source_quality", "consolidation"],
            "target_active_support_count": target_count,
            "source_quality_weight_per_active_support": 0.05,
            "archival_changes_active_behavior": True,
        }
    if relation == "contradicts":
        degree_left = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_links WHERE archived_at IS NULL AND relation_type='contradicts' AND (from_memory_id=? OR to_memory_id=?)",
                (int(left["id"]), int(left["id"])),
            ).fetchone()[0]
        )
        degree_right = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_links WHERE archived_at IS NULL AND relation_type='contradicts' AND (from_memory_id=? OR to_memory_id=?)",
                (int(right["id"]), int(right["id"])),
            ).fetchone()[0]
        )
        return {
            "active_consumers": ["conflict_pairs", "conflict_clusters", "conflict_registry", "source_quality"],
            "left_active_contradiction_degree": degree_left,
            "right_active_contradiction_degree": degree_right,
            "archival_changes_active_behavior": True,
        }
    return {
        "active_consumers": ["current_state_lineage"],
        "archival_changes_active_behavior": True,
        "legacy_non_superseding_semantics": True,
    }


def build_canonical_truth_review_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    include_items: bool = True,
    sample_limit: int = 200,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    safe_limit = max(1, min(int(sample_limit), 1000))
    requested_project = _text(project_key) or None
    rows = conn.execute(
        """
        SELECT l.*
        FROM memory_links l
        WHERE l.archived_at IS NULL AND l.relation_type IN ('supports','contradicts','refines')
        ORDER BY l.id
        """
    ).fetchall()
    links = [row_to_dict(row) for row in rows]
    ids = sorted({int(link["from_memory_id"]) for link in links} | {int(link["to_memory_id"]) for link in links})
    memories: dict[int, dict[str, Any]] = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        for row in conn.execute(f"SELECT * FROM memories WHERE id IN ({placeholders})", ids).fetchall():
            memories[int(row["id"])] = row_to_dict(row)

    items: list[dict[str, Any]] = []
    for link in links:
        left = memories.get(int(link["from_memory_id"]))
        right = memories.get(int(link["to_memory_id"]))
        if left is None or right is None:
            continue
        if requested_project is not None and requested_project not in {left.get("project_key"), right.get("project_key")}:
            continue
        relation = _text(link.get("relation_type"))
        origin = _text(link.get("origin"))
        same_domain = _same_domain(left, right)
        evidence: dict[str, Any] = {"same_domain": same_domain}
        recommendation = "requires_operator_review"
        reason_codes: list[str] = []
        confidence = "medium"

        if relation == "refines":
            refines = _matching_refines_events(
                conn,
                from_memory_id=int(left["id"]),
                to_memory_id=int(right["id"]),
                origin=origin,
            )
            evidence.update(refines)
            if same_domain and origin in _EXPLICIT_REFINES_ORIGINS and refines["event_pair_present"] and refines["event_source_matches_link_origin"]:
                recommendation = "preserve_legacy_lineage"
                reason_codes = ["explicit_write_origin", "matching_refines_event_pair", "same_domain"]
                confidence = "high"
            else:
                reason_codes = ["refines_missing_full_explicit_corroboration"]
        elif relation == "supports":
            support = _support_evidence(conn, left, right)
            evidence.update(support)
            if support["current_contract_evidence_present"]:
                recommendation = "requires_operator_review"
                reason_codes = ["legacy_support_has_partial_current_evidence"]
            elif origin in _AUTO_SUPPORT_ORIGINS:
                recommendation = "archive_from_active_truth"
                reason_codes = ["auto_consolidation_support_without_current_evidence"]
                confidence = "high"
            elif origin in _MANUAL_SUPPORT_ORIGINS:
                recommendation = "requires_operator_review"
                reason_codes = ["manual_legacy_support_without_current_evidence"]
                confidence = "medium"
            else:
                reason_codes = ["unknown_legacy_support_origin"]
        elif relation == "contradicts":
            conflict = _conflict_review_evidence(conn, int(left["id"]), int(right["id"]))
            evidence.update(conflict)
            evidence["both_endpoints_currently_active"] = bool(_active_state(left) and _active_state(right))
            if conflict["review_evidence_present"]:
                recommendation = "requires_operator_review"
                reason_codes = ["legacy_contradiction_has_review_evidence"]
            elif origin in _LEGACY_CONTRADICT_ORIGINS:
                recommendation = "archive_from_active_truth"
                reason_codes = ["legacy_conflict_writer_without_review_evidence"]
                if not evidence["both_endpoints_currently_active"]:
                    reason_codes.append("endpoint_not_currently_active")
                confidence = "high"
            else:
                reason_codes = ["unknown_legacy_contradiction_origin"]

        impact = _consumer_impact(conn, relation=relation, left=left, right=right)
        items.append({
            "link_id": int(link["id"]),
            "from_memory_id": int(link["from_memory_id"]),
            "to_memory_id": int(link["to_memory_id"]),
            "relation_type": relation,
            "origin": origin or None,
            "created_at": link.get("created_at"),
            "recommendation": recommendation,
            "confidence": confidence,
            "reason_codes": reason_codes,
            "evidence": evidence,
            "consumer_impact": impact,
            "from_memory": {
                "id": int(left["id"]),
                "project_key": left.get("project_key"),
                "scope_code": left.get("scope_code"),
                "state_code": left.get("state_code"),
                "memory_v2_status": left.get("memory_v2_status"),
                "archived_at": left.get("archived_at"),
            },
            "to_memory": {
                "id": int(right["id"]),
                "project_key": right.get("project_key"),
                "scope_code": right.get("scope_code"),
                "state_code": right.get("state_code"),
                "memory_v2_status": right.get("memory_v2_status"),
                "archived_at": right.get("archived_at"),
            },
        })

    rec_counts = Counter(item["recommendation"] for item in items)
    relation_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for item in items:
        relation_counts[item["relation_type"]][item["recommendation"]] += 1
    archive_ids = [item["link_id"] for item in items if item["recommendation"] == "archive_from_active_truth"]
    preserve_ids = [item["link_id"] for item in items if item["recommendation"] == "preserve_legacy_lineage"]
    review_ids = [item["link_id"] for item in items if item["recommendation"] == "requires_operator_review"]

    return {
        "status": "review_ready",
        "schema": SCHEMA,
        "project_key": requested_project,
        "summary": {
            "active_canonical_truth_links_reviewed": len(items),
            "archive_from_active_truth_count": rec_counts.get("archive_from_active_truth", 0),
            "preserve_legacy_lineage_count": rec_counts.get("preserve_legacy_lineage", 0),
            "requires_operator_review_count": rec_counts.get("requires_operator_review", 0),
            "high_confidence_archive_count": sum(
                1 for item in items
                if item["recommendation"] == "archive_from_active_truth" and item["confidence"] == "high"
            ),
            "high_confidence_preserve_count": sum(
                1 for item in items
                if item["recommendation"] == "preserve_legacy_lineage" and item["confidence"] == "high"
            ),
        },
        "by_relation": {key: dict(sorted(counter.items())) for key, counter in sorted(relation_counts.items())},
        "recommendation_ids": {
            "archive_from_active_truth": archive_ids,
            "preserve_legacy_lineage": preserve_ids,
            "requires_operator_review": review_ids,
        },
        "items": items[:safe_limit] if include_items else [],
        "item_count": len(items),
        "item_limit": safe_limit,
        "policy": {
            "auto_archive_allowed": False,
            "semantic_similarity_used": False,
            "content_used_for_classification": False,
            "heuristic_auto_truth_requires_current_evidence_to_remain_active": True,
            "explicit_legacy_lineage_can_be_preserved_with_matching_event_pair": True,
            "manual_legacy_support_requires_review": True,
        },
        "next_step": {
            "archive_preview_eligible_ids": archive_ids,
            "preserve_without_mutation_ids": preserve_ids,
            "operator_review_ids": review_ids,
        },
        "safety": {"read_only": True, "mutations_performed": 0},
    }
