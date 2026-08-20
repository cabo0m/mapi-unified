from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

AGENT_GRAVITY_PREVIEW_SCHEMA = "mapi_agent_gravity_preview.v1"
AGENT_GRAVITY_POLICY_SCHEMA = "mapi_agent_gravity_policy.v1"
AGENT_GRAVITY_CONTEXT_SCHEMA = "mapi_agent_gravity_context.v1"
AGENT_GRAVITY_SHADOW_SCHEMA = "mapi_agent_gravity_shadow.v1"
MAX_GRAVITY_CANDIDATES = 50
MAX_GRAVITY_RESULTS = 12
MAX_GRAVITY_CONTEXT_ITEMS = 2
_TERMINAL_STATES = frozenset({"archived", "superseded", "expired", "rejected", "cancelled"})
_UNSAFE_TRUTH_KINDS = frozenset({"dream", "proposal", "interpretation"})


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _norm(value: Any) -> str:
    return _text(value).casefold()


def _tokens(value: Any) -> set[str]:
    return {token for token in re.findall(r"[\w-]+", _norm(value), flags=re.UNICODE) if len(token) >= 2}


def _tags(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = str(value or "").replace(";", ",").split(",")
    return {_norm(item) for item in raw if _text(item)}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [_text(item) for item in parsed if _text(item)]
    return []


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def gravity_policy() -> dict[str, Any]:
    return {
        "schema": AGENT_GRAVITY_POLICY_SCHEMA,
        "read_only": True,
        "max_candidates": MAX_GRAVITY_CANDIDATES,
        "max_results": MAX_GRAVITY_RESULTS,
        "max_context_items": MAX_GRAVITY_CONTEXT_ITEMS,
        "lanes": ["required", "strong", "contextual"],
        "weights": {
            "explicit_trigger": 0.45,
            "query_overlap": 0.20,
            "role": 0.15,
            "scope": 0.10,
            "importance": 0.05,
            "recall": 0.05,
        },
        "safety": {
            "semantic_similarity_alone_is_not_durable_evidence": True,
            "gravity_can_create_durable_relation": False,
            "gravity_can_modify_importance": False,
            "gravity_can_modify_recall": False,
            "dream_proposal_interpretation_excluded": True,
            "foreign_project_candidates_excluded": True,
        },
    }


def _statement(item: Mapping[str, Any]) -> str:
    return _text(item.get("summary_short") or item.get("title") or item.get("content"))


def _state(item: Mapping[str, Any]) -> str:
    return _norm(item.get("state_code") or item.get("memory_v2_status") or item.get("activity_state"))


def _historical_milestone(item: Mapping[str, Any]) -> bool:
    tags = _tags(item.get("tags"))
    return _norm(item.get("layer_code")) == "autobio" or _norm(item.get("area_code")) == "history" or bool(tags & {"milestone", "autobiographical-memory", "self-event"})


def _role_signals(item: Mapping[str, Any], source_kinds: Sequence[str]) -> tuple[float, list[str]]:
    tags = _tags(item.get("tags"))
    memory_type = _norm(item.get("memory_type"))
    area = _norm(item.get("area_code"))
    layer = _norm(item.get("layer_code"))
    signals: list[str] = []
    score = 0.0
    kinds = set(source_kinds)
    if "commitment_ledger" in kinds or tags & {"guardrail", "commitment", "safety", "security", "policy"} or memory_type in {"guardrail", "commitment"}:
        score = max(score, 1.0)
        signals.append("commitment_or_guardrail")
    if "autobiographical_timeline" in kinds or layer == "autobio" or area == "history" or "milestone" in tags:
        score = max(score, 0.85)
        signals.append("milestone_or_autobiography")
    if "self_capsule" in kinds or layer in {"core", "identity"} or area == "identity":
        score = max(score, 0.75)
        signals.append("identity_or_self_model")
    if "retrieval_pool" in kinds:
        score = max(score, 0.55)
        signals.append("retrieval_candidate")
    if _json_list(item.get("should_resurface_when")) or _json_list(item.get("should_resurface_when_json")):
        score = max(score, 0.65)
        signals.append("explicit_resurface_rule")
    return score, signals


def _trigger_score(item: Mapping[str, Any], query_tokens: set[str]) -> tuple[float, list[str]]:
    rules = _json_list(item.get("should_resurface_when")) or _json_list(item.get("should_resurface_when_json"))
    if not query_tokens:
        return 0.0, []
    best = 0.0
    matched: list[str] = []
    for rule in rules:
        rt = _tokens(rule)
        if not rt:
            continue
        overlap = len(query_tokens & rt) / max(1, len(rt))
        coverage = len(query_tokens & rt) / max(1, len(query_tokens))
        score = max(overlap, coverage)
        if score > best:
            best = score
        if score >= 0.5:
            matched.append(rule)
    return min(1.0, best), matched[:4]


def _query_overlap(item: Mapping[str, Any], query_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    blob = _tokens(" ".join([_statement(item), _text(item.get("tags")), _text(item.get("source_context"))]))
    if not blob:
        return 0.0
    return min(1.0, len(query_tokens & blob) / max(1, min(len(query_tokens), 8)))


def _candidate_lane(*, score: float, trigger: float, explicit: bool, role_score: float) -> str | None:
    if explicit or trigger >= 0.75:
        return "required"
    if score >= 0.62 or (role_score >= 0.85 and score >= 0.45):
        return "strong"
    if score >= 0.40:
        return "contextual"
    return None


def build_agent_gravity_preview(
    *,
    query: str,
    project_key: str | None,
    candidates: Sequence[Mapping[str, Any]],
    explicit_memory_ids: Sequence[int] | None = None,
    max_results: int = MAX_GRAVITY_RESULTS,
    include_debug: bool = False,
) -> dict[str, Any]:
    policy = gravity_policy()
    requested_project = _text(project_key)
    query_tokens = _tokens(query)
    explicit_ids = {int(value) for value in (explicit_memory_ids or []) if int(value) > 0}
    safe_results = max(1, min(int(max_results), MAX_GRAVITY_RESULTS))
    seen: set[int] = set()
    duplicate_ids: list[int] = []
    evaluated: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    raw_candidates = [dict(raw) for raw in list(candidates)[:250]]
    if len(raw_candidates) > MAX_GRAVITY_CANDIDATES:
        def _prefilter_key(item: Mapping[str, Any]) -> tuple[float, float, float, float, float, int]:
            memory_id = int(item.get("id") or item.get("memory_id") or 0)
            trigger, _ = _trigger_score(item, query_tokens)
            overlap = _query_overlap(item, query_tokens)
            role_score, _ = _role_signals(item, [str(value) for value in item.get("source_kinds") or []])
            scope = 1.0 if requested_project and _text(item.get("project_key")) == requested_project else 0.0
            importance = max(0.0, min(1.0, float(item.get("importance_score") or 0.0)))
            explicit = 1.0 if memory_id in explicit_ids else 0.0
            return (explicit, trigger, overlap, role_score, scope + importance * 0.1, -memory_id)
        raw_candidates.sort(key=_prefilter_key, reverse=True)
        raw_candidates = raw_candidates[:MAX_GRAVITY_CANDIDATES]

    for raw in raw_candidates:
        item = dict(raw)
        memory_id = int(item.get("id") or item.get("memory_id") or 0)
        if memory_id <= 0:
            continue
        if memory_id in seen:
            duplicate_ids.append(memory_id)
            continue
        seen.add(memory_id)
        item_project = _text(item.get("project_key"))
        source_kinds = [str(value) for value in item.get("source_kinds") or []]
        is_self_source = bool(set(source_kinds) & {"self_capsule", "commitment_ledger", "autobiographical_timeline"})
        if requested_project and item_project and item_project != requested_project and not is_self_source:
            excluded.append({"memory_id": memory_id, "reason": "foreign_project"})
            continue
        truth_kind = _norm(item.get("truth_kind"))
        if truth_kind in _UNSAFE_TRUTH_KINDS:
            excluded.append({"memory_id": memory_id, "reason": f"unsafe_truth_kind:{truth_kind}"})
            continue
        state = _state(item)
        if state in _TERMINAL_STATES and not _historical_milestone(item):
            excluded.append({"memory_id": memory_id, "reason": f"terminal_state:{state}"})
            continue

        trigger, matched_rules = _trigger_score(item, query_tokens)
        overlap = _query_overlap(item, query_tokens)
        role_score, role_signals = _role_signals(item, source_kinds)
        scope_score = 1.0 if requested_project and item_project == requested_project else (0.7 if is_self_source else 0.0)
        importance = max(0.0, min(1.0, float(item.get("importance_score") or 0.0)))
        recall_count = max(0, int(item.get("recall_count") or 0))
        recall = min(1.0, recall_count / 10.0)
        explicit = memory_id in explicit_ids
        total = 0.45 * trigger + 0.20 * overlap + 0.15 * role_score + 0.10 * scope_score + 0.05 * importance + 0.05 * recall
        if explicit:
            total = max(total, 0.80)
        lane = _candidate_lane(score=total, trigger=trigger, explicit=explicit, role_score=role_score)
        if lane is None:
            excluded.append({"memory_id": memory_id, "reason": "below_threshold", "score": round(total, 6)})
            continue
        evaluated.append({
            "memory_id": memory_id,
            "statement": _statement(item),
            "project_key": item_project or None,
            "lane": lane,
            "gravity_score": round(total, 6),
            "source_memory_ids": [memory_id],
            "source_kinds": sorted(set(source_kinds)),
            "matched_resurface_rules": matched_rules,
            "reason_codes": [
                *( ["explicit_memory_id"] if explicit else []),
                *( ["explicit_trigger_match"] if trigger > 0 else []),
                *( ["query_overlap"] if overlap > 0 else []),
                *role_signals,
                *( ["project_scope_match"] if scope_score == 1.0 else []),
                *( ["self_model_source"] if is_self_source else []),
            ],
            "signals": {
                "explicit_trigger": round(trigger, 6),
                "query_overlap": round(overlap, 6),
                "role": round(role_score, 6),
                "scope": round(scope_score, 6),
                "importance_minor": round(importance, 6),
                "recall_minor": round(recall, 6),
            },
        })

    lane_rank = {"required": 0, "strong": 1, "contextual": 2}
    evaluated.sort(key=lambda item: (lane_rank[item["lane"]], -float(item["gravity_score"]), int(item["memory_id"])))
    attractors = evaluated[:safe_results]
    source_ids = [int(item["memory_id"]) for item in attractors]
    result = {
        "status": "ok",
        "schema": AGENT_GRAVITY_PREVIEW_SCHEMA,
        "policy_schema": policy["schema"],
        "query": _text(query),
        "project_key": requested_project or None,
        "attractors": attractors,
        "items": attractors,
        "source_memory_ids": source_ids,
        "candidate_source_memory_ids": sorted(seen),
        "warnings": ["duplicate_candidate_ids_ignored"] if duplicate_ids else [],
        "preview_fingerprint": _fingerprint({"query": _text(query), "project_key": requested_project, "attractors": attractors}),
        "safety": policy["safety"] | {"read_only": True, "model_calls_performed": 0},
    }
    if include_debug:
        result["debug"] = {
            "duplicate_candidate_ids": sorted(set(duplicate_ids)),
            "excluded": excluded,
            "candidate_count": len(seen),
            "accepted_count": len(evaluated),
            "input_candidate_count": min(len(list(candidates)), 250),
            "prefilter_limit": MAX_GRAVITY_CANDIDATES,
        }
    return result


def build_gravity_context_block(*, gravity_payload: Mapping[str, Any], canonical_source_memory_ids: Sequence[int], max_items: int = MAX_GRAVITY_CONTEXT_ITEMS) -> dict[str, Any]:
    canonical = {int(value) for value in canonical_source_memory_ids if int(value) > 0}
    safe_limit = max(0, min(int(max_items), MAX_GRAVITY_CONTEXT_ITEMS))
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if gravity_payload.get("status") != "ok":
        return {"status": "unavailable", "schema": AGENT_GRAVITY_CONTEXT_SCHEMA, "reason": "gravity_preview_unavailable", "items": [], "source_memory_ids": [], "safety": {"read_only": True, "canonical_source_ids_unchanged": True}}
    for raw in gravity_payload.get("attractors") or []:
        memory_id = int(raw.get("memory_id") or 0)
        if memory_id in canonical:
            skipped.append({"memory_id": memory_id, "reason": "already_in_canonical_sources"})
            continue
        if raw.get("lane") not in {"required", "strong"}:
            skipped.append({"memory_id": memory_id, "reason": "contextual_lane_not_injected"})
            continue
        selected.append(dict(raw))
        if len(selected) >= safe_limit:
            break
    return {
        "status": "ok" if selected else "empty",
        "schema": AGENT_GRAVITY_CONTEXT_SCHEMA,
        "reason": None if selected else "no_noncanonical_required_or_strong_items",
        "items": selected,
        "source_memory_ids": [int(item["memory_id"]) for item in selected],
        "skipped": skipped,
        "safety": {"read_only": True, "canonical_source_ids_unchanged": True, "max_items": MAX_GRAVITY_CONTEXT_ITEMS, "durable_writes_performed": 0},
    }


def build_gravity_shadow_comparison(*, baseline_source_memory_ids: Sequence[int], gravity_payload: Mapping[str, Any], max_injections: int = 2) -> dict[str, Any]:
    baseline = [int(value) for value in baseline_source_memory_ids if int(value) > 0]
    baseline_set = set(baseline)
    injection_limit = max(0, min(int(max_injections), 2))
    inject = [int(item["memory_id"]) for item in gravity_payload.get("attractors") or [] if item.get("lane") in {"required", "strong"} and int(item.get("memory_id") or 0) not in baseline_set][:injection_limit]
    augmented = list(baseline)
    if baseline:
        augmented = [baseline[0], *inject, *[value for value in baseline[1:] if value not in inject]]
    else:
        augmented = inject
    return {
        "status": "ok",
        "schema": AGENT_GRAVITY_SHADOW_SCHEMA,
        "canonical": {"source_memory_ids": baseline},
        "shadow": {
            "augmented_preview": {"source_memory_ids": augmented},
            "introduced_ids": inject,
            "canonical_changed": False,
        },
        "safety": {"read_only": True, "canonical_baseline_preserved": True, "writes_performed": 0},
    }


def build_source_bound_gravity_preview(
    *,
    query: str,
    project_key: str | None,
    candidates: Sequence[Mapping[str, Any]],
    limit: int = 8,
    include_debug: bool = False,
    explicit_memory_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Compatibility surface for the public server: bounded, source-bound Gravity preview."""
    return build_agent_gravity_preview(
        query=query,
        project_key=project_key,
        candidates=candidates,
        explicit_memory_ids=explicit_memory_ids,
        max_results=limit,
        include_debug=include_debug,
    )
