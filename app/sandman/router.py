from __future__ import annotations

import json
import os
import uuid
from typing import Any, Callable, Mapping

from mapi_core.sandman.contracts import (
    MAX_CANDIDATES,
    MAX_EVIDENCE_IDS_PER_PROPOSAL,
    MAX_PROPOSALS,
    MAX_REASON_CHARS,
    MAX_REDACTED_CHARS_PER_CANDIDATE,
    MAX_TOTAL_REDACTED_CHARS,
    PROVIDER_REQUEST_SCHEMA_VERSION,
    PROVIDER_RESPONSE_SCHEMA_VERSION,
    PROVIDER_VALIDATION_SCHEMA_VERSION,
    PROPOSAL_ACTIONS,
    SAFETY_BLOCK,
    build_provider_request,
)
from app.sandman.providers.deterministic import DeterministicProvider
from app.sandman.redaction import EXTERNAL_DATA_POLICY, REDACTION_POLICY_VERSION, build_redacted_candidates
from mapi_core.sandman.validator import validate_provider_response


PROVIDER_STATUS_SCHEMA_VERSION = "sandman_provider_status.v1"
SANDMAN_PROVIDER_V3_FLAG_KEY = "sandman_provider_v3_enabled"
PROVIDER_REGISTRY = {
    "deterministic": {"availability": "available", "kind": "local_rules"},
    "gemma": {"availability": "unavailable_in_v3_router", "kind": "legacy_separate"},
    "gemini": {"availability": "implemented", "kind": "shadow_only"},
}
PROVIDERS = {"deterministic": DeterministicProvider()}
LIMITS = {
    "max_candidates": MAX_CANDIDATES,
    "max_proposals": MAX_PROPOSALS,
    "max_redacted_chars_per_candidate": MAX_REDACTED_CHARS_PER_CANDIDATE,
    "max_total_redacted_chars": MAX_TOTAL_REDACTED_CHARS,
    "max_reason_chars": MAX_REASON_CHARS,
    "max_evidence_ids_per_proposal": MAX_EVIDENCE_IDS_PER_PROPOSAL,
}
PREVIEW_SAFETY = {
    "read_only": True,
    "proposal_only": True,
    "memory_mutations_performed": 0,
    "queue_mutations_performed": 0,
    "sleep_mutations_performed": 0,
    "timeline_mutations_performed": 0,
    "feature_flag_mutations_performed": 0,
    "raw_secret_exposed": False,
    "auto_apply": False,
}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def provider_status_payload(
    *,
    feature_flag: Mapping[str, Any],
    feature_flag_evaluation: Mapping[str, Any],
    gemini_shadow_flag_evaluation: Mapping[str, Any] | None = None,
    routing_flag: Mapping[str, Any] | None = None,
    routing_flag_evaluation: Mapping[str, Any] | None = None,
    routing_canary: Mapping[str, Any] | None = None,
    gemini_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    missing = bool(feature_flag.get("is_implicit_default"))
    enabled = bool(feature_flag_evaluation.get("enabled"))
    reason = "missing_flag" if missing else str(feature_flag_evaluation.get("reason") or "flag_disabled")
    gemini_config = dict(gemini_config or {})
    from app.sandman import routing

    routing_flag = dict(routing_flag or {})
    routing_evaluation = dict(routing_flag_evaluation or {})
    routing_missing = bool(routing_flag.get("is_implicit_default", True))
    routing_enabled = bool(routing_evaluation.get("enabled")) and not bool(
        routing_evaluation.get("read_only_mode")
    )
    canary = dict(routing_canary or {})
    return {
        "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
        "status": "ready" if enabled else "feature_disabled",
        "feature_flag_evaluation": {
            "flag_key": SANDMAN_PROVIDER_V3_FLAG_KEY,
            "enabled": enabled,
            "reason": reason,
            "is_implicit_default": missing,
            "rollout_mode": feature_flag_evaluation.get("rollout_mode"),
        },
        "registered_providers": [
            {"provider_name": "deterministic", **PROVIDER_REGISTRY["deterministic"]}
        ],
        "unavailable_providers": [{"provider_name": "gemma", **PROVIDER_REGISTRY["gemma"]}],
        "shadow_providers": [{"provider_name": "gemini", **PROVIDER_REGISTRY["gemini"]}],
        "request_schema_version": PROVIDER_REQUEST_SCHEMA_VERSION,
        "response_schema_version": PROVIDER_RESPONSE_SCHEMA_VERSION,
        "validation_schema_version": PROVIDER_VALIDATION_SCHEMA_VERSION,
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "external_data_policy": EXTERNAL_DATA_POLICY,
        "limits": dict(LIMITS),
        "model_auto_apply": False,
        "queue_routing": False,
        "routing_implementation_available": True,
        "routing_enabled": routing_enabled,
        "routing_flag_evaluation": {
            "flag_key": routing.MODEL_QUEUE_ROUTING_FLAG_KEY,
            "enabled": routing_enabled,
            "reason": "missing_flag"
            if routing_missing
            else routing_evaluation.get("reason"),
            "is_implicit_default": routing_missing,
            "rollout_mode": routing_evaluation.get("rollout_mode"),
            "read_only_mode": bool(routing_evaluation.get("read_only_mode")),
        },
        "routing_policy_version": routing.ROUTING_POLICY_VERSION,
        "routing_stage": routing.SUPPORTED_STAGE,
        "routing_queue_target": routing.QUEUE_TARGET,
        "routing_auto_apply": routing.MODEL_QUEUE_AUTO_APPLY,
        "canary_max_per_run": routing.MAX_ROUTED_PROPOSALS_PER_RUN,
        "canary_total_cap": routing.MAX_TOTAL_CANARY_PROPOSALS,
        "canary_current_count": canary.get("current_routed_count", 0),
        "canary_remaining": canary.get(
            "remaining_canary_budget", routing.MAX_TOTAL_CANARY_PROPOSALS
        ),
        "canary_paused": bool(canary.get("paused", False)),
        "real_evaluation_report_required": True,
        "real_external_provider_available": _env_bool("MAPI_GEMINI_ENABLED", False) and bool(
            os.getenv("GEMINI_API_KEY", "").strip()
            if "api_key_configured" not in gemini_config
            else gemini_config["api_key_configured"]
        ),
        "legacy_gemma_status": "legacy_separate",
        "gemini_implementation_available": True,
        "gemini_api_key_configured": _env_bool("MAPI_GEMINI_ENABLED", False)
        and (bool(os.getenv("GEMINI_API_KEY", "").strip())
        if "api_key_configured" not in gemini_config
        else bool(gemini_config["api_key_configured"])),
        "gemini_shadow_flag_evaluation": dict(gemini_shadow_flag_evaluation or {}),
        "primary_model": gemini_config.get("primary_model", "gemini-3.1-flash-lite"),
        "escalation_model": gemini_config.get("escalation_model", "gemini-3.5-flash"),
        "escalation_enabled": bool(gemini_config.get("escalation_enabled", False)),
        "api_mode": "interactions",
        "stateless_required": True,
        "store_required": False,
        "network_health_check_performed": False,
        "safety": {"read_only": True, "network_health_check_performed": False, "model_loaded": False},
    }


def _blocked(status: str, *, provider_name: str, reason_codes: list[str], feature_flag_evaluation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "status": status,
        "provider_name": provider_name,
        "reason_codes": sorted(set(reason_codes)),
        "safety": dict(PREVIEW_SAFETY),
    }
    if feature_flag_evaluation is not None:
        payload["feature_flag_evaluation"] = dict(feature_flag_evaluation)
    return payload


def _parse_json_list(value: str | list[Any], *, field: str) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid_{field}_json") from exc
    if not isinstance(decoded, list):
        raise ValueError(f"invalid_{field}_json")
    return decoded


def _load_candidates(conn: Any, *, memory_ids: list[int]) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    placeholders = ",".join("?" for _ in memory_ids)
    rows = conn.execute(f"SELECT * FROM memories WHERE id IN ({placeholders}) ORDER BY id ASC", tuple(memory_ids)).fetchall()
    memories = [dict(row) for row in rows]
    link_rows = conn.execute(
        f"""
        SELECT from_memory_id, to_memory_id, relation_type
        FROM memory_links
        WHERE archived_at IS NULL
          AND from_memory_id IN ({placeholders})
          AND to_memory_id IN ({placeholders})
          AND relation_type IN ('contradicts','reinforces','related_to')
        ORDER BY relation_type, from_memory_id, to_memory_id
        """,
        tuple(memory_ids) + tuple(memory_ids),
    ).fetchall()
    links: dict[int, list[dict[str, Any]]] = {}
    for row in link_rows:
        links.setdefault(int(row["from_memory_id"]), []).append(
            {"relation_type": str(row["relation_type"]), "target_memory_id": int(row["to_memory_id"])}
        )
    return memories, links


def preview_provider_request_payload(
    conn: Any,
    *,
    project_key: str,
    scope_code: str,
    memory_ids_json: str | list[int],
    allowed_actions_json: str | list[str],
    provider_name: str = "deterministic",
    proposal_budget: int = MAX_PROPOSALS,
    include_debug: bool = False,
    feature_flag: Mapping[str, Any],
    feature_flag_evaluation: Mapping[str, Any],
    request_id_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    if not feature_flag_evaluation.get("enabled"):
        reason = "missing_flag" if feature_flag.get("is_implicit_default") else str(feature_flag_evaluation.get("reason") or "flag_disabled")
        return _blocked("feature_disabled", provider_name=provider_name, reason_codes=[reason], feature_flag_evaluation=feature_flag_evaluation)
    registry = PROVIDER_REGISTRY.get(str(provider_name or "").strip().lower())
    if registry is None:
        return _blocked("provider_unavailable", provider_name=str(provider_name), reason_codes=["unknown_provider"])
    if registry["availability"] not in {"available", "implemented"}:
        return _blocked("provider_unavailable", provider_name=str(provider_name), reason_codes=[registry["kind"], registry["availability"]])
    try:
        memory_ids_raw = _parse_json_list(memory_ids_json, field="memory_ids")
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in memory_ids_raw):
            raise ValueError("invalid_memory_ids")
        memory_ids = sorted(set(memory_ids_raw))
        actions_raw = _parse_json_list(allowed_actions_json, field="allowed_actions")
        if any(not isinstance(item, str) for item in actions_raw):
            raise ValueError("invalid_allowed_actions")
        actions = sorted(set(actions_raw))
    except ValueError as exc:
        return _blocked("request_blocked", provider_name=provider_name, reason_codes=[str(exc)])
    if not memory_ids:
        return _blocked("request_blocked", provider_name=provider_name, reason_codes=["memory_ids_required"])
    if len(memory_ids) > MAX_CANDIDATES:
        return _blocked("request_blocked", provider_name=provider_name, reason_codes=["candidate_budget_overflow"])
    if not actions or any(action not in PROPOSAL_ACTIONS for action in actions):
        return _blocked("request_blocked", provider_name=provider_name, reason_codes=["invalid_allowed_actions"])
    if scope_code in {"global", "public"} or scope_code != "project":
        return _blocked("request_blocked", provider_name=provider_name, reason_codes=["boundary_not_allowed"])

    memories, links = _load_candidates(conn, memory_ids=memory_ids)
    if len(memories) != len(memory_ids):
        return _blocked("not_found", provider_name=provider_name, reason_codes=["candidate_not_found"])
    boundaries = {(item.get("project_key"), item.get("scope_code"), item.get("workspace_id")) for item in memories}
    if len(boundaries) != 1:
        return _blocked("request_blocked", provider_name=provider_name, reason_codes=["cross_boundary_candidates"])
    actual_project, actual_scope, workspace_id = next(iter(boundaries))
    if actual_project != project_key or actual_scope != scope_code:
        return _blocked("request_blocked", provider_name=provider_name, reason_codes=["boundary_mismatch"])

    redaction = build_redacted_candidates(memories, links_by_source=links, requested_ids=memory_ids)
    if redaction["status"] == "blocked":
        return {
            **_blocked("request_blocked", provider_name=provider_name, reason_codes=redaction["reason_codes"]),
            "redaction_manifest": redaction["redaction_manifest"],
        }
    try:
        request = build_provider_request(
            request_id=(request_id_factory or (lambda: str(uuid.uuid4())))(),
            provider_name=provider_name,
            project_key=project_key,
            scope_code=scope_code,
            workspace_id=workspace_id,
            candidate_memory_ids=[item["memory_id"] for item in redaction["candidates"]],
            candidates=redaction["candidates"],
            allowed_actions=actions,
            proposal_budget=proposal_budget,
            redaction_manifest=redaction["redaction_manifest"],
            safety=dict(SAFETY_BLOCK),
        )
    except ValueError as exc:
        reason_codes = getattr(exc, "reason_codes", [str(exc)])
        return _blocked("request_blocked", provider_name=provider_name, reason_codes=list(reason_codes))
    result = {
        "status": redaction["status"],
        "provider_name": provider_name,
        "request": request,
        "redaction_manifest": redaction["redaction_manifest"],
        "safety": dict(PREVIEW_SAFETY),
    }
    if include_debug:
        result["debug"] = {
            "rule_ids": ["explicit_ids_only", "exact_boundary", "local_sensitivity", "residual_scan", "strict_contract"],
            "requested_candidate_count": len(memory_ids),
            "included_candidate_count": len(request["candidate_memory_ids"]),
        }
    return result


def preview_deterministic_provider_payload(conn: Any, **kwargs: Any) -> dict[str, Any]:
    request_preview = preview_provider_request_payload(conn, **kwargs)
    if request_preview["status"] not in {"request_ready", "request_ready_partial"}:
        return request_preview
    request = request_preview["request"]
    provider = PROVIDERS["deterministic"]
    response = provider.analyze(request)
    validation = validate_provider_response(request, response, provider_name=provider.name)
    if validation["status"] != "accepted":
        return {
            "status": "response_rejected",
            "provider_name": provider.name,
            "request_summary": _request_summary(request),
            "redaction_manifest": request["redaction_manifest"],
            "validation": validation,
            "proposals": [],
            "abstain": True,
            "unsupported_metrics": [],
            "safety": dict(PREVIEW_SAFETY),
        }
    return {
        "status": "preview_completed",
        "provider_name": provider.name,
        "request_summary": _request_summary(request),
        "redaction_manifest": request["redaction_manifest"],
        "validation": validation,
        "proposals": validation["normalized_proposals"],
        "abstain": validation["abstain"],
        "unsupported_metrics": response["unsupported_metrics"],
        "safety": dict(PREVIEW_SAFETY),
    }


def _request_summary(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": request["schema_version"],
        "request_id": request["request_id"],
        "input_fingerprint": request["input_fingerprint"],
        "project_key": request["project_key"],
        "scope_code": request["scope_code"],
        "workspace_id": request["workspace_id"],
        "candidate_memory_ids": list(request["candidate_memory_ids"]),
        "allowed_actions": list(request["allowed_actions"]),
        "proposal_budget": request["proposal_budget"],
    }
