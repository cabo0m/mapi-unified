from __future__ import annotations

import re
from typing import Any, Callable

from mapi_core.memory.capture_queue import (
    CAPTURE_REVIEW_MUTABLE_STATUSES,
    get_capture_review_item,
    update_capture_reconciliation_preview,
)
from mapi_core.memory.lifecycle_integrity import (
    PREVIEW_BLOCKING_LIFECYCLE_ISSUE_CODES,
    evaluate_lifecycle_integrity_graph,
    load_lifecycle_graph,
)
from mapi_core.memory.lifecycle_contracts import (
    MEMORY_V3_HASH_ALGORITHM,
    derive_canonical_memory_state,
    is_supersession_capable_relation_kind,
    is_transition_allowed,
    normalize_relation_kind,
)
from mapi_core.schemas import normalize_optional_text


CAPTURE_RECONCILIATION_PREVIEW_SCHEMA_VERSION = "memory_v3_capture_reconciliation_preview.v2"
CAPTURE_RECONCILIATION_OUTCOMES = (
    "create_new",
    "create_version",
    "reinforce_existing",
    "duplicate_existing",
    "update_metadata_proposal",
    "conflict_review",
    "skip_transient",
    "abstain",
)
SEMANTIC_AMBIGUITY_THRESHOLD = 0.85


_FUTURE_ACTION_BY_OUTCOME = {
    "create_new": "create_new",
    "create_version": "create_version",
    "reinforce_existing": "reinforce_existing",
    "duplicate_existing": "mark_duplicate",
    "update_metadata_proposal": "metadata_review",
    "conflict_review": "conflict_review",
    "skip_transient": "skip",
    "abstain": "none",
}


def _confidence_band(outcome: str) -> str:
    if outcome == "abstain":
        return "insufficient"
    if outcome == "create_new":
        return "deterministic_medium"
    return "deterministic_high"


def _planned_future_action(
    *,
    outcome: str,
    primary_memory_id: int | None,
    target_memory_id: int | None,
    relation_kind: str | None,
    reason: str | None,
) -> dict[str, Any]:
    apply_supported = outcome in {
        "create_new",
        "create_version",
        "reinforce_existing",
        "duplicate_existing",
        "conflict_review",
        "skip_transient",
    }
    future_apply_would_mutate_memory = outcome in {
        "create_new",
        "create_version",
        "reinforce_existing",
        "conflict_review",
    }
    return {
        "action": _FUTURE_ACTION_BY_OUTCOME[outcome],
        "apply_supported": apply_supported,
        "requires_approved_item": True,
        "requires_expected_preview_hash": True,
        "primary_memory_id": primary_memory_id,
        "target_memory_id": target_memory_id,
        "relation_kind": relation_kind,
        "reason": reason,
        "auto_resolve": False,
        "create_candidate_memory": outcome == "conflict_review",
        "memory_mutation_planned": future_apply_would_mutate_memory,
        "preview_memory_mutations_performed": 0,
        "future_apply_would_mutate_memory": future_apply_would_mutate_memory,
    }


def _normalize_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        tokens = [str(token).strip().lower() for token in value]
    else:
        tokens = [token.strip().lower() for token in str(value).split(",")]
    return sorted(token for token in tokens if token)


def _tokenize(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        normalized = _normalize_text(value).lower()
        tokens.update(token for token in re.findall(r"[a-z0-9_]+", normalized) if len(token) >= 3)
    return tokens


def _lexical_score(proposal: dict[str, Any], candidate: dict[str, Any]) -> float:
    left = _tokenize(
        proposal.get("content"),
        proposal.get("summary_short"),
        proposal.get("title"),
    )
    right = _tokenize(
        candidate.get("content"),
        candidate.get("summary_short"),
        candidate.get("title"),
    )
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    return overlap / max(len(left), len(right))


def _memory_snapshot(memory: dict[str, Any]) -> dict[str, Any]:
    raw = memory.get("_raw", {}) if isinstance(memory.get("_raw"), dict) else {}
    return {
        "id": int(memory["id"]),
        "project_key": normalize_optional_text(memory.get("project_key")),
        "scope_code": normalize_optional_text(memory.get("scope_code")),
        "workspace_id": None if memory.get("workspace_id") is None else int(memory["workspace_id"]),
        "memory_type": normalize_optional_text(memory.get("memory_type")),
        "entry_type": normalize_optional_text(memory.get("entry_type")),
        "truth_kind": normalize_optional_text(memory.get("truth_kind")),
        "state_code": normalize_optional_text(raw.get("state_code")) or normalize_optional_text(memory.get("state_code")),
        "memory_v2_status": normalize_optional_text(raw.get("memory_v2_status")) or normalize_optional_text(memory.get("memory_v2_status")),
        "activity_state": normalize_optional_text(raw.get("activity_state")) or normalize_optional_text(memory.get("activity_state")),
        "contradiction_flag": int(raw.get("contradiction_flag") or memory.get("contradiction_flag") or 0),
        "source_event_ref": normalize_optional_text(memory.get("source_event_ref")),
        "summary_short": normalize_optional_text(memory.get("summary_short")),
        "title": normalize_optional_text(memory.get("title")),
        "tags": _normalize_tags(memory.get("tags")),
        "content_normalized": _normalize_text(memory.get("content")),
        "updated_at": normalize_optional_text(memory.get("updated_at")),
        "supersedes_memory_id": None if memory.get("supersedes_memory_id") is None else int(memory["supersedes_memory_id"]),
        "superseded_by_memory_id": None if memory.get("superseded_by_memory_id") is None else int(memory["superseded_by_memory_id"]),
    }


def _metadata_signature(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": normalize_optional_text(payload.get("title")),
        "summary_short": normalize_optional_text(payload.get("summary_short")),
        "tags": _normalize_tags(payload.get("tags")),
    }


def _metadata_only_difference(proposal: dict[str, Any], candidate_snapshot: dict[str, Any]) -> bool:
    proposal_tags = _normalize_tags(proposal.get("tags"))
    candidate_tags = _normalize_tags(candidate_snapshot.get("tags"))
    if not proposal_tags or not candidate_tags:
        return False
    return proposal_tags != candidate_tags


def _fetch_candidate_memories(
    conn: Any,
    *,
    project_key: str,
    scope_code: str,
    workspace_id: int | None,
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM memories
        WHERE project_key = ? AND scope_code = ? AND workspace_id IS ?
        ORDER BY id ASC
        """,
        (project_key, scope_code, workspace_id),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        raw = row_to_dict(row)
        try:
            enriched = enrich_memory_dict(raw)
        except ValueError:
            # Lifecycle preview must turn unsupported stored states into a
            # controlled abstain instead of failing during legacy enrichment.
            enriched = dict(raw)
        enriched["_raw"] = raw
        items.append(enriched)
    return items


def _semantic_shortlist(
    *,
    query: str,
    project_key: str,
    scope_code: str,
    semantic_limit: int,
    candidate_lookup: dict[int, dict[str, Any]],
    search_semantic_func: Callable[..., dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not search_semantic_func:
        return ([], ["semantic_search_unavailable"])
    result = search_semantic_func(query=query, top_k=int(semantic_limit), project_key=project_key)
    if result.get("status") != "ok":
        return ([], ["semantic_search_unavailable"])
    items: list[dict[str, Any]] = []
    for row in result.get("results") or []:
        memory_id = row.get("memory_id")
        if memory_id is None:
            continue
        candidate = candidate_lookup.get(int(memory_id))
        if candidate is None:
            continue
        if normalize_optional_text(candidate.get("scope_code")) != scope_code:
            continue
        items.append(
            {
                "memory_id": int(memory_id),
                "similarity": float(row.get("similarity") or 0.0),
            }
        )
    items.sort(key=lambda item: (-item["similarity"], item["memory_id"]))
    return (items, [])


def preview_memory_capture_reconciliation_payload(
    conn: Any,
    *,
    item_id: int,
    candidate_limit: int = 20,
    semantic_limit: int = 10,
    include_semantic: bool = True,
    include_debug: bool = False,
    persist: bool = True,
    normalize_required_text: Callable[[Any, str], str],
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    canonical_json_hash: Callable[[Any], str],
    utc_now_iso: Callable[[], str],
    search_semantic_func: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    item = get_capture_review_item(conn, item_id=int(item_id), row_to_dict=row_to_dict)
    current_status = str(item["status"])
    if current_status not in CAPTURE_REVIEW_MUTABLE_STATUSES:
        return {
            "status": "blocked",
            "schema_version": CAPTURE_RECONCILIATION_PREVIEW_SCHEMA_VERSION,
            "item": item,
            "guard": {
                "allowed": False,
                "apply_eligible": False,
                "blockers": [f"item_status_not_reconcilable:{current_status}"],
                "warnings": [],
            },
            "hash_algorithm": MEMORY_V3_HASH_ALGORITHM,
            "safety": {
                "memory_mutations_performed": 0,
                "protected_table_mutations_performed": 0,
                "apply_supported": False,
            },
        }

    proposal = dict(item.get("proposal") or {})
    normalized_content = _normalize_text(proposal.get("content"))
    project_key = normalize_optional_text(proposal.get("project_key"))
    scope_code = normalize_optional_text(proposal.get("scope_code"))
    unsupported_metrics: list[str] = []
    blockers: list[str] = []
    raw_workspace_id = proposal.get("workspace_id")
    if raw_workspace_id is None:
        default_workspace = conn.execute(
            "SELECT id FROM workspaces WHERE workspace_key = 'default' LIMIT 1"
        ).fetchone()
        workspace_id = None if default_workspace is None else int(default_workspace["id"])
    else:
        try:
            workspace_id = int(raw_workspace_id)
        except (TypeError, ValueError):
            workspace_id = None
            blockers.append("invalid_workspace_id")
    input_fingerprint = normalize_required_text(item.get("input_fingerprint"), "input_fingerprint")

    evidence_exact: list[dict[str, Any]] = []
    evidence_source: list[dict[str, Any]] = []
    evidence_lexical: list[dict[str, Any]] = []
    evidence_semantic: list[dict[str, Any]] = []
    explicit_target_evidence: dict[str, Any] | None = None

    recommendation = "manual_review"
    outcome = "abstain"

    if not normalized_content:
        blockers.append("empty_proposal_content")
    if project_key is None or scope_code is None:
        blockers.append("missing_project_or_scope")

    candidate_lookup: dict[int, dict[str, Any]] = {}
    candidate_items: list[dict[str, Any]] = []
    if not blockers:
        candidate_items = _fetch_candidate_memories(
            conn,
            project_key=project_key,
            scope_code=scope_code,
            workspace_id=workspace_id,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
        candidate_lookup = {int(candidate["id"]): candidate for candidate in candidate_items}

    normalized_source_event_ref = normalize_optional_text(proposal.get("source_event_ref"))
    for candidate in candidate_items:
        snapshot = _memory_snapshot(candidate)
        if snapshot["content_normalized"] == normalized_content:
            evidence_exact.append(snapshot)
        if (
            normalized_source_event_ref is not None
            and snapshot["source_event_ref"] == normalized_source_event_ref
        ):
            evidence_source.append(snapshot)
        lexical_score = _lexical_score(proposal, candidate)
        if lexical_score > 0:
            evidence_lexical.append(
                {
                    "memory_id": int(candidate["id"]),
                    "score": round(float(lexical_score), 6),
                    "snapshot": snapshot,
                }
            )

    evidence_lexical.sort(key=lambda item: (-float(item["score"]), int(item["memory_id"])))
    evidence_lexical = evidence_lexical[: max(1, min(int(candidate_limit), 50))]

    if not blockers and include_semantic:
        evidence_semantic, semantic_unsupported = _semantic_shortlist(
            query=normalized_content,
            project_key=project_key,
            scope_code=scope_code,
            semantic_limit=max(1, min(int(semantic_limit), 25)),
            candidate_lookup=candidate_lookup,
            search_semantic_func=search_semantic_func,
        )
        unsupported_metrics.extend(semantic_unsupported)

    explicit_target_id = proposal.get("supersedes_memory_id") or proposal.get("target_memory_id")
    if proposal.get("contradiction_target_memory_id") is not None:
        explicit_target_id = proposal.get("contradiction_target_memory_id")
    elif proposal.get("conflict_target_memory_id") is not None:
        explicit_target_id = proposal.get("conflict_target_memory_id")
    if explicit_target_id is not None:
        explicit_target = candidate_lookup.get(int(explicit_target_id))
        if explicit_target is None:
            blockers.append("explicit_target_out_of_scope_or_missing")
        else:
            target_snapshot = _memory_snapshot(explicit_target)
            try:
                graph = load_lifecycle_graph(
                    conn,
                    memory_id=int(explicit_target_id),
                    row_to_dict=row_to_dict,
                    enrich_memory_dict=enrich_memory_dict,
                    include_archived=True,
                    limit=25,
                )
                integrity = evaluate_lifecycle_integrity_graph(graph, sample_limit=25, include_debug=False)
                target_findings = [
                    finding
                    for finding in integrity["findings"]
                    if int(explicit_target_id) in finding["memory_ids"]
                ]
                blocking_findings = [
                    finding["issue_code"]
                    for finding in target_findings
                    if str(finding["issue_code"]) in PREVIEW_BLOCKING_LIFECYCLE_ISSUE_CODES
                ]
            except ValueError:
                blocking_findings = ["unknown_state_code"]
            explicit_target_evidence = {
                "memory_id": int(explicit_target_id),
                "snapshot": target_snapshot,
                "blocking_findings": blocking_findings,
            }
            try:
                target_canonical_state = derive_canonical_memory_state(
                    state_code=target_snapshot.get("state_code"),
                    activity_state=target_snapshot.get("activity_state"),
                    contradiction_flag=target_snapshot.get("contradiction_flag"),
                )
            except ValueError:
                target_canonical_state = None
            transition_to_conflicted_allowed = bool(
                target_canonical_state == "conflicted"
                or is_transition_allowed(target_canonical_state, "conflicted")
            )
            explicit_target_evidence["lifecycle"] = {
                "target_memory_id": int(explicit_target_id),
                "raw_state_code": target_snapshot.get("state_code"),
                "canonical_state": target_canonical_state,
                "transition_to_conflicted_allowed": transition_to_conflicted_allowed,
                "conflict_review_eligible": bool(
                    target_canonical_state in {"validated", "conflicted"}
                    and transition_to_conflicted_allowed
                ),
            }
            if blocking_findings:
                blockers.extend(f"explicit_target_integrity:{code}" for code in blocking_findings)

    top_lexical = evidence_lexical[0] if evidence_lexical else None
    semantic_only_candidate_ids = {
        int(item["memory_id"])
        for item in evidence_semantic
        if int(item["memory_id"]) not in {int(exact["id"]) for exact in evidence_exact}
        and int(item["memory_id"]) not in {int(source["id"]) for source in evidence_source}
        and int(item["memory_id"]) not in {int(lex["memory_id"]) for lex in evidence_lexical if float(lex["score"]) >= 0.55}
    }
    strongest_semantic_only_similarity = max(
        (
            float(item["similarity"])
            for item in evidence_semantic
            if int(item["memory_id"]) in semantic_only_candidate_ids
        ),
        default=0.0,
    )
    strong_semantic_ambiguity = strongest_semantic_only_similarity >= SEMANTIC_AMBIGUITY_THRESHOLD

    conflict_signal = bool(
        proposal.get("is_contradiction")
        or proposal.get("contradiction_target_memory_id")
        or proposal.get("conflict_target_memory_id")
    )
    transient_signal = proposal.get("skip_transient") is True
    raw_relation_kind = normalize_optional_text(proposal.get("relation_kind"))
    normalized_relation_kind = normalize_relation_kind(raw_relation_kind)
    supersession_reason = (
        normalize_optional_text(proposal.get("supersession_reason"))
        or normalize_optional_text(proposal.get("reason"))
    )
    create_version_contract_reasons: list[str] = []

    if blockers:
        outcome = "abstain"
        recommendation = "manual_review"
    elif transient_signal:
        outcome = "skip_transient"
        recommendation = "skip_transient_capture"
    elif conflict_signal and explicit_target_evidence is None:
        outcome = "abstain"
        recommendation = "manual_review"
    elif conflict_signal and not bool(explicit_target_evidence.get("lifecycle", {}).get("conflict_review_eligible")):
        outcome = "abstain"
        recommendation = "manual_review"
    elif conflict_signal:
        outcome = "conflict_review"
        recommendation = "manual_conflict_review"
    elif explicit_target_evidence is not None:
        target_snapshot = explicit_target_evidence["snapshot"]
        metadata_differs = _metadata_only_difference(proposal, target_snapshot)
        if target_snapshot["content_normalized"] == normalized_content:
            outcome = "update_metadata_proposal" if metadata_differs else "duplicate_existing"
            recommendation = "review_metadata_only" if metadata_differs else "mark_duplicate_existing"
        else:
            if raw_relation_kind is None:
                create_version_contract_reasons.append("missing_supersession_relation_kind")
            elif normalized_relation_kind is None or not is_supersession_capable_relation_kind(normalized_relation_kind):
                create_version_contract_reasons.append("unsupported_supersession_relation_kind")
            if supersession_reason is None:
                create_version_contract_reasons.append("missing_supersession_reason")
            if create_version_contract_reasons:
                outcome = "abstain"
                recommendation = "manual_review"
            else:
                outcome = "create_version"
                recommendation = "create_version_candidate"
    elif normalized_source_event_ref is not None and evidence_source:
        if any(item["content_normalized"] == normalized_content for item in evidence_source):
            outcome = "duplicate_existing"
            recommendation = "mark_duplicate_existing"
        else:
            outcome = "reinforce_existing"
            recommendation = "reinforce_existing_memory"
    elif evidence_exact:
        metadata_differs = _metadata_only_difference(proposal, evidence_exact[0])
        outcome = "update_metadata_proposal" if metadata_differs else "duplicate_existing"
        recommendation = "review_metadata_only" if metadata_differs else "mark_duplicate_existing"
    elif top_lexical is not None and float(top_lexical["score"]) >= 0.55:
        outcome = "abstain"
        recommendation = "manual_review"
    elif strong_semantic_ambiguity:
        outcome = "abstain"
        recommendation = "manual_review"
    else:
        outcome = "create_new"
        recommendation = "create_new_memory"

    if semantic_only_candidate_ids and outcome in {"duplicate_existing", "reinforce_existing", "create_version"}:
        unsupported_metrics.append("semantic_only_shortlist_not_decisive")
    elif semantic_only_candidate_ids:
        unsupported_metrics.append("semantic_shortlist_only")

    reason_codes: list[str] = []
    if not normalized_content:
        reason_codes.append("empty_proposal_content")
    if project_key is None or scope_code is None:
        reason_codes.append("missing_project_or_scope")
    if any(blocker == "explicit_target_out_of_scope_or_missing" for blocker in blockers):
        reason_codes.append("explicit_target_out_of_scope_or_missing")
    if any(blocker.startswith("explicit_target_integrity:") for blocker in blockers):
        reason_codes.append("explicit_target_integrity_blocked")
    if conflict_signal and outcome == "conflict_review":
        reason_codes.append("explicit_contradiction")
    elif conflict_signal and explicit_target_evidence is None:
        reason_codes.append("missing_conflict_target")
    if conflict_signal and explicit_target_evidence is not None:
        target_lifecycle = explicit_target_evidence.get("lifecycle", {})
        if target_lifecycle.get("canonical_state") is None:
            reason_codes.append("conflict_target_state_unknown")
        elif not target_lifecycle.get("conflict_review_eligible"):
            reason_codes.append("conflict_target_state_not_eligible")
    if outcome == "skip_transient":
        reason_codes.append("explicit_transient_signal")
    reason_codes.extend(create_version_contract_reasons)
    if explicit_target_evidence is not None and outcome in {
        "create_version",
        "duplicate_existing",
        "update_metadata_proposal",
    }:
        reason_codes.append("explicit_valid_target")
    if evidence_exact and outcome in {"duplicate_existing", "update_metadata_proposal"}:
        reason_codes.append("exact_content_match")
    if normalized_source_event_ref is not None and evidence_source and outcome in {
        "duplicate_existing",
        "reinforce_existing",
    }:
        reason_codes.append("same_source_event_ref")
    if outcome == "update_metadata_proposal":
        reason_codes.append("metadata_only_difference")
    if outcome == "abstain" and top_lexical is not None and float(top_lexical["score"]) >= 0.55:
        reason_codes.append("strong_lexical_ambiguity")
    if outcome == "abstain" and strong_semantic_ambiguity:
        reason_codes.extend(["semantic_shortlist_only", "strong_semantic_ambiguity"])
    if outcome == "create_new":
        reason_codes.append("no_hard_match")
        if semantic_only_candidate_ids:
            reason_codes.append("semantic_shortlist_only")
    if "semantic_search_unavailable" in unsupported_metrics:
        reason_codes.append("semantic_search_unavailable")
    reason_codes = sorted(set(reason_codes))

    confidence_band = _confidence_band(outcome)

    matched_memory_ids = sorted(
        {
            *(int(item["id"]) for item in evidence_exact),
            *(int(item["id"]) for item in evidence_source),
            *(int(item["memory_id"]) for item in evidence_lexical if float(item["score"]) >= 0.25),
            *(int(item["memory_id"]) for item in evidence_semantic),
            *(() if explicit_target_evidence is None else [int(explicit_target_evidence["memory_id"])]),
        }
    )
    shortlisted_ids = sorted(
        {
            *(int(memory_id) for memory_id in matched_memory_ids),
        }
    )
    shortlisted_snapshots = [
        _memory_snapshot(candidate_lookup[memory_id])
        for memory_id in shortlisted_ids
        if memory_id in candidate_lookup
    ]
    primary_memory_id: int | None = None
    if outcome in {"create_version", "conflict_review"} and explicit_target_evidence is not None:
        primary_memory_id = int(explicit_target_evidence["memory_id"])
    elif outcome == "reinforce_existing" and evidence_source:
        primary_memory_id = int(evidence_source[0]["id"])
    elif outcome in {"duplicate_existing", "update_metadata_proposal"}:
        if explicit_target_evidence is not None:
            primary_memory_id = int(explicit_target_evidence["memory_id"])
        elif evidence_source:
            primary_memory_id = int(evidence_source[0]["id"])
        elif evidence_exact:
            primary_memory_id = int(evidence_exact[0]["id"])
    planned_future_action = _planned_future_action(
        outcome=outcome,
        primary_memory_id=primary_memory_id,
        target_memory_id=(
            int(explicit_target_evidence["memory_id"])
            if explicit_target_evidence is not None and outcome in {"create_version", "conflict_review"}
            else None
        ),
        relation_kind=normalized_relation_kind if outcome == "create_version" else None,
        reason=(
            supersession_reason
            if outcome == "create_version"
            else normalize_optional_text(proposal.get("conflict_reason")) or normalize_optional_text(proposal.get("reason"))
            if outcome == "conflict_review"
            else None
        ),
    )
    guard_warnings = sorted(set(blockers))
    guard_blockers: list[str] = []
    apply_eligible = bool(planned_future_action["apply_supported"]) and not guard_blockers
    candidate_set_fingerprint = canonical_json_hash(
        {
            "schema_version": CAPTURE_RECONCILIATION_PREVIEW_SCHEMA_VERSION,
            "project_key": project_key,
            "scope_code": scope_code,
            "workspace_id": workspace_id,
            "shortlisted_candidates": shortlisted_snapshots,
            "evidence": {
                "exact_ids": sorted(int(item["id"]) for item in evidence_exact),
                "source_ids": sorted(int(item["id"]) for item in evidence_source),
                "lexical": [
                    {"memory_id": int(item["memory_id"]), "score": float(item["score"])}
                    for item in evidence_lexical
                ],
                "semantic": [
                    {"memory_id": int(item["memory_id"]), "similarity": round(float(item["similarity"]), 6)}
                    for item in evidence_semantic
                ],
                "explicit_target_id": None if explicit_target_evidence is None else int(explicit_target_evidence["memory_id"]),
                "explicit_target_lifecycle": (
                    None if explicit_target_evidence is None else explicit_target_evidence.get("lifecycle")
                ),
            },
        }
    )
    reconciliation_preview_hash = canonical_json_hash(
        {
            "schema_version": CAPTURE_RECONCILIATION_PREVIEW_SCHEMA_VERSION,
            "item_id": int(item_id),
            "input_fingerprint": input_fingerprint,
            "candidate_set_fingerprint": candidate_set_fingerprint,
            "outcome": outcome,
            "confidence_band": confidence_band,
            "reason_codes": reason_codes,
            "recommended_action": recommendation,
            "planned_future_action": planned_future_action,
            "guard": {
                "allowed": True,
                "apply_eligible": apply_eligible,
                "blockers": guard_blockers,
                "warnings": guard_warnings,
            },
            "preview_options": {
                "candidate_limit": max(1, min(int(candidate_limit), 50)),
                "semantic_limit": max(1, min(int(semantic_limit), 25)),
                "include_semantic": bool(include_semantic),
            },
        }
    )

    outcome_matrix = {name: name == outcome for name in CAPTURE_RECONCILIATION_OUTCOMES}
    payload = {
        "status": "preview_ready",
        "schema_version": CAPTURE_RECONCILIATION_PREVIEW_SCHEMA_VERSION,
        "item_id": int(item_id),
        "queue_status": current_status,
        "proposal_key": item["proposal_key"],
        "input_fingerprint": input_fingerprint,
        "candidate_set_fingerprint": candidate_set_fingerprint,
        "reconciliation_preview_hash": reconciliation_preview_hash,
        "hash_algorithm": MEMORY_V3_HASH_ALGORITHM,
        "outcome": outcome,
        "confidence_band": confidence_band,
        "reason_codes": reason_codes,
        "recommended_action": recommendation,
        "operator_next_action": recommendation,
        "planned_future_action": planned_future_action,
        "outcome_matrix": outcome_matrix,
        "proposal": {
            "project_key": project_key,
            "scope_code": scope_code,
            "workspace_id": workspace_id,
            "source_event_ref": normalized_source_event_ref,
            "summary_short": normalize_optional_text(proposal.get("summary_short")),
            "title": normalize_optional_text(proposal.get("title")),
            "tags": _normalize_tags(proposal.get("tags")),
            "content_normalized": normalized_content,
            "supersedes_memory_id": None if explicit_target_id is None else int(explicit_target_id),
        },
        "evidence": {
            "exact": {
                "count": len(evidence_exact),
                "matched_memory_ids": [int(item["id"]) for item in evidence_exact],
                "items": evidence_exact,
            },
            "source": {
                "count": len(evidence_source),
                "matched_memory_ids": [int(item["id"]) for item in evidence_source],
                "items": evidence_source,
            },
            "lexical": {
                "count": len(evidence_lexical),
                "matched_memory_ids": [int(item["memory_id"]) for item in evidence_lexical if float(item["score"]) >= 0.25],
                "items": evidence_lexical,
            },
            "semantic": {
                "count": len(evidence_semantic),
                "matched_memory_ids": [int(item["memory_id"]) for item in evidence_semantic],
                "items": evidence_semantic,
                "used_as_shortlist_only": True,
            },
            "explicit_target": explicit_target_evidence,
        },
        "matched_memory_ids": matched_memory_ids,
        "guard": {
            "allowed": True,
            "apply_eligible": apply_eligible,
            "blockers": guard_blockers,
            "warnings": guard_warnings,
        },
        "unsupported_metrics": sorted(set(unsupported_metrics)),
        "preview_options": {
            "candidate_limit": max(1, min(int(candidate_limit), 50)),
            "semantic_limit": max(1, min(int(semantic_limit), 25)),
            "include_semantic": bool(include_semantic),
        },
        "candidate_pool_summary": {
            "same_scope_candidates": len(candidate_items),
            "shortlisted_candidates": len(shortlisted_snapshots),
            "generated_at": utc_now_iso(),
        },
        "safety": {
            "memory_mutations_performed": 0,
            "protected_table_mutations_performed": 0,
            "queue_row_updates_performed": 1,
            "apply_supported": False,
        },
    }
    if include_debug:
        payload["debug"] = {
            "semantic_only_candidate_ids": sorted(semantic_only_candidate_ids),
            "strongest_semantic_only_similarity": strongest_semantic_only_similarity,
            "semantic_ambiguity_threshold": SEMANTIC_AMBIGUITY_THRESHOLD,
            "top_lexical_score": None if top_lexical is None else float(top_lexical["score"]),
        }

    if not persist:
        payload["safety"]["queue_row_updates_performed"] = 0
        payload["item"] = item
        return payload

    persisted = update_capture_reconciliation_preview(
        conn,
        item_id=int(item_id),
        recommended_action=recommendation,
        matched_memory_ids=matched_memory_ids,
        reconciliation=payload,
        candidate_set_fingerprint=candidate_set_fingerprint,
        reconciliation_preview_hash=reconciliation_preview_hash,
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        row_to_dict=row_to_dict,
    )
    if persisted.get("status") != "updated":
        return {
            "status": "blocked",
            "schema_version": CAPTURE_RECONCILIATION_PREVIEW_SCHEMA_VERSION,
            "item": item,
            "guard": {
                "allowed": False,
                "apply_eligible": False,
                "blockers": [str(persisted.get("error") or "preview_persist_failed")],
                "warnings": [],
            },
            "hash_algorithm": MEMORY_V3_HASH_ALGORITHM,
            "safety": {
                "memory_mutations_performed": 0,
                "protected_table_mutations_performed": 0,
                "apply_supported": False,
            },
        }
    payload["item"] = persisted["item"]
    return payload
