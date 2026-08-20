from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Callable

from mapi_core.memory.canonical_truth_review import (
    EXPLICIT_LEGACY_REFINES_ORIGINS,
    matching_refines_event_evidence,
)


LEGACY_GRAPH_AUDIT_SCHEMA = "mapi_legacy_graph_audit.v1"
LEGACY_GRAPH_CLASSIFICATIONS = ("trusted", "legacy_unverified", "invalid", "redundant")
CANONICAL_TRUTH_RELATIONS = frozenset({"supports", "contradicts", "supersedes", "refines", "derived_from"})
CURRENT_EVIDENCE_ORIGIN_PREFIX = "memory_v3_evidence_relation:"
CURRENT_SUPERSESSION_ORIGIN_PREFIX = "memory_v3_supersession:"
CURRENT_CONFLICT_ORIGIN_PREFIX = "memory_v3_capture_conflict:"

_HEURISTIC_ORIGIN_PREFIXES = (
    "sandman_v1_",
    "sandman_mara:",
    "memory_linking_pass_v1",
    "auto_link_memories_v1",
    "consolidation_v1_auto",
    "conflicts_v1_auto",
    "sandman_agent",
)
_MANUAL_ORIGIN_PREFIXES = (
    "memory_write:user_explicit",
    "memory_write:agent_autonomous",
    "manual_",
    "chatgpt_",
    "conversation",
    "assistant_",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical_project(
    conn: Any,
    value: Any,
    *,
    resolve_project_key: Callable[[Any, str], str],
) -> str | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        return _text(resolve_project_key(conn, raw)) or raw
    except Exception:
        return raw


def _same_domain(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left.get("canonical_project_key") == right.get("canonical_project_key")
        and (left.get("scope_code") or None) == (right.get("scope_code") or None)
        and int(left.get("workspace_id") or 1) == int(right.get("workspace_id") or 1)
    )


def _memory_snapshot(
    row: dict[str, Any],
    *,
    canonical_project_key: str | None,
) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "project_key": row.get("project_key"),
        "canonical_project_key": canonical_project_key,
        "scope_code": row.get("scope_code"),
        "workspace_id": int(row.get("workspace_id") or 1),
        "state_code": row.get("state_code"),
        "memory_v2_status": row.get("memory_v2_status"),
        "supersedes_memory_id": row.get("supersedes_memory_id"),
        "superseded_by_memory_id": row.get("superseded_by_memory_id"),
        "source_event_ref": row.get("source_event_ref"),
    }


def _is_current_evidence_link(link: dict[str, Any]) -> bool:
    origin = _text(link.get("origin"))
    return origin.startswith(CURRENT_EVIDENCE_ORIGIN_PREFIX)


def _is_current_conflict_link(link: dict[str, Any]) -> bool:
    return _text(link.get("origin")).startswith(CURRENT_CONFLICT_ORIGIN_PREFIX)


def _is_heuristic_origin(origin: str) -> bool:
    return any(origin.startswith(prefix) for prefix in _HEURISTIC_ORIGIN_PREFIXES)


def _is_manual_origin(origin: str) -> bool:
    return any(origin.startswith(prefix) for prefix in _MANUAL_ORIGIN_PREFIXES)


def _reason_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in items:
        counter.update(str(reason) for reason in item.get("reason_codes") or [])
    return dict(sorted(counter.items(), key=lambda pair: (-pair[1], pair[0])))


def _classify_link(
    *,
    link: dict[str, Any],
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    duplicate_rank: int,
) -> tuple[str, list[str], dict[str, Any]]:
    relation = _text(link.get("relation_type")).lower()
    origin = _text(link.get("origin"))
    reasons: list[str] = []
    evidence: dict[str, Any] = {
        "origin": origin or None,
        "origin_class": "unknown",
        "semantic_similarity_used_as_current_evidence": False,
    }

    if int(link.get("from_memory_id") or 0) == int(link.get("to_memory_id") or 0):
        return "invalid", ["self_link"], evidence
    if left is None or right is None:
        return "invalid", ["missing_endpoint"], evidence

    same_domain = _same_domain(left, right)
    evidence["same_domain"] = same_domain
    evidence["from_supersedes_pointer_match"] = int(left.get("supersedes_memory_id") or 0) == int(link["to_memory_id"])
    evidence["reverse_superseded_by_pointer_match"] = int(right.get("superseded_by_memory_id") or 0) == int(link["from_memory_id"])
    evidence["same_source_event_ref"] = bool(
        _text(left.get("source_event_ref"))
        and _text(left.get("source_event_ref")) == _text(right.get("source_event_ref"))
    )

    if relation in CANONICAL_TRUTH_RELATIONS and not same_domain:
        return "invalid", ["canonical_truth_relation_domain_mismatch"], evidence
    if relation == "same_project" and left.get("canonical_project_key") != right.get("canonical_project_key"):
        return "invalid", ["same_project_relation_project_mismatch"], evidence
    if duplicate_rank > 0:
        return "redundant", ["duplicate_active_edge"], evidence

    if relation == "supersedes":
        if evidence["from_supersedes_pointer_match"] and evidence["reverse_superseded_by_pointer_match"]:
            evidence["origin_class"] = "structural_corroboration"
            return "trusted", ["supersession_pointer_pair_corroborated"], evidence
        return "invalid", ["supersedes_pointer_mismatch"], evidence

    if relation in {"supports", "derived_from"}:
        if _is_current_evidence_link(link):
            evidence["origin_class"] = "current_evidence_bound"
            return "trusted", ["current_evidence_bound_relation"], evidence
        evidence["origin_class"] = "legacy_truth_relation"
        reasons.append("legacy_truth_relation_without_current_evidence_contract")
        if relation == "supports" and evidence["same_source_event_ref"]:
            reasons.append("same_source_event_ref_corroboration_available")
        return "legacy_unverified", reasons, evidence

    if relation == "contradicts":
        if _is_current_conflict_link(link):
            evidence["origin_class"] = "current_reviewed_conflict"
            return "trusted", ["current_reviewed_conflict_relation"], evidence
        evidence["origin_class"] = "legacy_conflict_relation"
        return "legacy_unverified", ["legacy_contradiction_without_current_review_contract"], evidence

    if relation == "refines":
        evidence["origin_class"] = "legacy_direct_refines_storage"
        return "legacy_unverified", ["legacy_direct_refines_storage"], evidence

    if _is_heuristic_origin(origin):
        evidence["origin_class"] = "legacy_heuristic"
        return "legacy_unverified", ["legacy_heuristic_origin"], evidence
    if _is_manual_origin(origin):
        evidence["origin_class"] = "explicit_or_operator"
        return "trusted", ["explicit_or_operator_association"], evidence

    evidence["origin_class"] = "unknown_or_legacy"
    return "legacy_unverified", ["origin_not_bound_to_current_contract"], evidence


def build_legacy_graph_audit_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    include_trusted: bool = False,
    include_candidates: bool = True,
    sample_limit: int = 100,
    row_to_dict: Callable[[Any], dict[str, Any]],
    resolve_project_key: Callable[[Any, str], str],
) -> dict[str, Any]:
    safe_limit = max(1, min(int(sample_limit), 1000))
    requested_project = _text(project_key) or None
    canonical_requested = _canonical_project(conn, requested_project, resolve_project_key=resolve_project_key) if requested_project else None

    link_rows = conn.execute("SELECT * FROM memory_links WHERE archived_at IS NULL ORDER BY id ASC").fetchall()
    links = [row_to_dict(row) for row in link_rows]
    endpoint_ids = sorted({int(link["from_memory_id"]) for link in links} | {int(link["to_memory_id"]) for link in links})
    memories: dict[int, dict[str, Any]] = {}
    if endpoint_ids:
        placeholders = ",".join("?" for _ in endpoint_ids)
        for row in conn.execute(f"SELECT * FROM memories WHERE id IN ({placeholders})", endpoint_ids).fetchall():
            item = row_to_dict(row)
            canonical = _canonical_project(conn, item.get("project_key"), resolve_project_key=resolve_project_key)
            item["canonical_project_key"] = canonical
            memories[int(item["id"])] = item

    duplicate_seen: dict[tuple[int, int, str], int] = defaultdict(int)
    classified: list[dict[str, Any]] = []
    for link in links:
        key = (int(link["from_memory_id"]), int(link["to_memory_id"]), _text(link.get("relation_type")))
        duplicate_rank = duplicate_seen[key]
        duplicate_seen[key] += 1
        left_raw = memories.get(int(link["from_memory_id"]))
        right_raw = memories.get(int(link["to_memory_id"]))
        left = None if left_raw is None else _memory_snapshot(left_raw, canonical_project_key=left_raw.get("canonical_project_key"))
        right = None if right_raw is None else _memory_snapshot(right_raw, canonical_project_key=right_raw.get("canonical_project_key"))
        if canonical_requested is not None:
            if left is None or right is None:
                continue
            if canonical_requested not in {left.get("canonical_project_key"), right.get("canonical_project_key")}:
                continue
        classification, reason_codes, evidence = _classify_link(
            link=link,
            left=left,
            right=right,
            duplicate_rank=duplicate_rank,
        )
        if (
            classification == "legacy_unverified"
            and _text(link.get("relation_type")).lower() == "refines"
            and _text(link.get("origin")) in EXPLICIT_LEGACY_REFINES_ORIGINS
            and left is not None
            and right is not None
            and _same_domain(left, right)
        ):
            refines_evidence = matching_refines_event_evidence(
                conn,
                from_memory_id=int(link["from_memory_id"]),
                to_memory_id=int(link["to_memory_id"]),
                origin=_text(link.get("origin")),
            )
            evidence["explicit_refines_event_evidence"] = refines_evidence
            if refines_evidence["event_pair_present"] and refines_evidence["event_source_matches_link_origin"]:
                classification = "trusted"
                reason_codes = ["explicit_refines_event_pair_corroborated"]
                evidence["origin_class"] = "explicit_legacy_lineage_corroborated"
        classified.append({
            "link_id": int(link["id"]),
            "from_memory_id": int(link["from_memory_id"]),
            "to_memory_id": int(link["to_memory_id"]),
            "relation_type": link.get("relation_type"),
            "origin": link.get("origin"),
            "created_at": link.get("created_at"),
            "classification": classification,
            "reason_codes": reason_codes,
            "evidence": evidence,
            "from_memory": left,
            "to_memory": right,
        })

    counts = Counter(item["classification"] for item in classified)
    by_relation: dict[str, Counter[str]] = defaultdict(Counter)
    by_origin: dict[str, Counter[str]] = defaultdict(Counter)
    for item in classified:
        by_relation[_text(item.get("relation_type"))][item["classification"]] += 1
        by_origin[_text(item.get("origin")) or "<null>"][item["classification"]] += 1

    actionable = [item for item in classified if item["classification"] in {"invalid", "redundant"}]
    review = [item for item in classified if item["classification"] == "legacy_unverified"]
    candidates = [item for item in classified if include_trusted or item["classification"] != "trusted"]

    canonical_truth_review = [
        item for item in review if _text(item.get("relation_type")).lower() in CANONICAL_TRUTH_RELATIONS
    ]
    heuristic_association_review = [
        item for item in review if _text(item.get("relation_type")).lower() not in CANONICAL_TRUTH_RELATIONS
    ]
    priority_debt_count = len(actionable) + len(canonical_truth_review)

    hard_invalid_endpoint_memory_ids = sorted({
        int(memory_id)
        for item in classified
        if item["classification"] == "invalid"
        and "canonical_truth_relation_domain_mismatch" in item["reason_codes"]
        for memory_id in (item["from_memory_id"], item["to_memory_id"])
    })

    return {
        "status": "ok" if not actionable else "attention",
        "schema": LEGACY_GRAPH_AUDIT_SCHEMA,
        "project_key": canonical_requested,
        "requested_project_key": requested_project,
        "summary": {
            "active_links_scanned": len(classified),
            "trusted_count": counts.get("trusted", 0),
            "legacy_unverified_count": counts.get("legacy_unverified", 0),
            "invalid_count": counts.get("invalid", 0),
            "redundant_count": counts.get("redundant", 0),
            "actionable_debt_count": len(actionable),
            "review_debt_count": len(review),
            "legacy_graph_debt_count": len(actionable) + len(review),
            "priority_debt_count": priority_debt_count,
            "canonical_truth_review_count": len(canonical_truth_review),
            "heuristic_association_review_count": len(heuristic_association_review),
            "hard_invalid_endpoint_memory_ids": hard_invalid_endpoint_memory_ids,
        },
        "by_relation": {
            relation: dict(sorted(counter.items()))
            for relation, counter in sorted(by_relation.items())
        },
        "by_origin": {
            origin: dict(sorted(counter.items()))
            for origin, counter in sorted(by_origin.items(), key=lambda pair: (-sum(pair[1].values()), pair[0]))
        },
        "reason_counts": _reason_counts(classified),
        "candidates": candidates[:safe_limit] if include_candidates else [],
        "candidate_count": len(candidates),
        "candidate_limit": safe_limit,
        "priority_buckets": {
            "P0_invalid": [int(item["link_id"]) for item in actionable if item["classification"] == "invalid"],
            "P1_redundant": [int(item["link_id"]) for item in actionable if item["classification"] == "redundant"],
            "P2_canonical_truth_review": [int(item["link_id"]) for item in canonical_truth_review],
            "background_legacy_associations_count": len(heuristic_association_review),
        },
        "remediation": {
            "auto_apply_allowed": False,
            "invalid_requires_exact_review": True,
            "redundant_requires_exact_review": True,
            "legacy_unverified_requires_evidence_revalidation": True,
            "history_should_not_be_rewritten": True,
            "debt_sources": [
                {
                    "code": "legacy_direct_refines_write_path_retired",
                    "status": "closed",
                    "note": "R6B blocks direct refines/partially_supersedes writes before INSERT. R6C treats historical direct refines as trusted only when same-domain explicit write origin is corroborated by matching version.refines/version.refines_by events; otherwise they remain review debt. Explicit low-level unsafe opt-in exists only for compatibility tests/forensics.",
                },
                {
                    "code": "legacy_conflicts_v1_truth_writer_retired",
                    "status": "closed",
                    "note": "R6C retires public run_conflicts_v1. Historical heuristic contradicts generation exists only in a non-MCP compatibility/forensics helper.",
                },
                {
                    "code": "conflict_explainer_canonical_truth_writer_guarded",
                    "status": "closed",
                    "note": "R6C blocks canonical conflict-explainer link creation on public paths and redirects temporal supersession to guarded supersession preview/apply. Low-level unsafe compatibility is non-MCP only.",
                },
            ],
        },
        "safety": {
            "read_only": True,
            "mutations_performed": 0,
            "semantic_similarity_used_for_classification": False,
            "content_used_for_classification": False,
        },
    }
