from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from mapi_core.sandman.contracts import (
    PROVIDER_REQUEST_SCHEMA_VERSION,
    PROVIDER_RESPONSE_SCHEMA_VERSION,
    PROVIDER_VALIDATION_SCHEMA_VERSION,
    canonical_fingerprint,
)
from mapi_core.sandman.providers.gemini import (
    PROVIDER_CONFIG_VERSION,
    STATELESS_AUDIT,
    GeminiConfig,
    GeminiShadowProvider,
    ProviderCallError,
)
from app.sandman.redaction import EXTERNAL_DATA_POLICY, REDACTION_POLICY_VERSION
from app.sandman import shadow_repository


SHADOW_RESULT_SCHEMA_VERSION = "sandman_gemini_shadow_result.v1"
SHADOW_OPERATION_SCHEMA_VERSION = "sandman_gemini_shadow_operation.v1"
SHADOW_FLAG_KEY = "sandman_gemini_shadow_enabled"


def shadow_operation_key(
    *,
    input_fingerprint: str,
    model_name: str,
    model_role: str,
    thinking_level: str,
) -> str:
    return canonical_fingerprint(
        {
            "schema_version": SHADOW_OPERATION_SCHEMA_VERSION,
            "input_fingerprint": input_fingerprint,
            "provider_name": "gemini",
            "model_name": model_name,
            "model_role": model_role,
            "thinking_level": thinking_level,
            "response_schema_version": PROVIDER_RESPONSE_SCHEMA_VERSION,
            "redaction_policy_version": REDACTION_POLICY_VERSION,
            "provider_config_version": PROVIDER_CONFIG_VERSION,
        }
    )


def _flags_enabled(provider_evaluation: Mapping[str, Any], shadow_evaluation: Mapping[str, Any]) -> bool:
    return bool(provider_evaluation.get("enabled") and shadow_evaluation.get("enabled"))


def _manifest(request: Mapping[str, Any]) -> dict[str, Any]:
    redaction = request["redaction_manifest"]
    return {
        "request_schema_version": request["schema_version"],
        "request_id": request["request_id"],
        "input_fingerprint": request["input_fingerprint"],
        "project_key": request["project_key"],
        "scope_code": request["scope_code"],
        "workspace_id": request["workspace_id"],
        "candidate_count": len(request["candidate_memory_ids"]),
        "candidate_memory_ids": list(request["candidate_memory_ids"]),
        "allowed_actions": list(request["allowed_actions"]),
        "proposal_budget": request["proposal_budget"],
        "redaction_policy_version": redaction["policy_version"],
        "external_data_policy": redaction["external_data_policy"],
        "excluded_candidate_count": redaction["candidate_count_excluded"],
        "replacement_counts": dict(redaction["replacement_counts"]),
        "truncated_ids": list(redaction["truncated_memory_ids"]),
        "raw_secret_exposed": False,
        "full_project_dump": False,
    }


def preview_shadow(
    *,
    request_preview: Mapping[str, Any],
    provider_evaluation: Mapping[str, Any],
    shadow_evaluation: Mapping[str, Any],
    config: GeminiConfig,
    model_role: str,
    include_debug: bool = False,
) -> dict[str, Any]:
    if not _flags_enabled(provider_evaluation, shadow_evaluation):
        return {
            "status": "feature_disabled",
            "reason_codes": sorted(
                {
                    str(item.get("reason") or "flag_disabled")
                    for item in (provider_evaluation, shadow_evaluation)
                    if not item.get("enabled")
                }
            ),
            "safety": _safety(),
        }
    if request_preview.get("status") not in {"request_ready", "request_ready_partial"}:
        return {
            "status": "request_blocked",
            "reason_codes": list(request_preview.get("reason_codes") or []),
            "redaction_manifest": request_preview.get("redaction_manifest"),
            "safety": _safety(),
        }
    try:
        model = config.model_for_role(model_role)
    except ValueError as exc:
        return {"status": "provider_unavailable", "reason_codes": [str(exc)], "safety": _safety()}
    if not config.api_key_configured:
        return {"status": "provider_unconfigured", "reason_codes": ["api_key_missing"], "safety": _safety()}
    request = request_preview["request"]
    operation_key = shadow_operation_key(
        input_fingerprint=request["input_fingerprint"],
        model_name=model,
        model_role=model_role,
        thinking_level=config.thinking_level,
    )
    result = {
        "status": "preview_ready",
        "shadow_operation_key": operation_key,
        "request_id": request["request_id"],
        "input_fingerprint": request["input_fingerprint"],
        "provider_name": "gemini",
        "model_name": model,
        "model_role": model_role,
        "api_mode": "interactions",
        "thinking_level": config.thinking_level,
        "proposal_budget": request["proposal_budget"],
        "estimated_maximum_request_chars": len(str(request)),
        "api_key_configured": True,
        "redaction_manifest": request["redaction_manifest"],
        "safety": _safety(),
        "_request": request,
    }
    if include_debug:
        result["debug"] = {
            "rule_ids": ["both_flags", "exact_boundary", "fresh_request", "stateless_interactions"],
            "candidate_count": len(request["candidate_memory_ids"]),
            "request_fingerprint": request["input_fingerprint"],
        }
    return result


def _safety() -> dict[str, Any]:
    return {
        "shadow_only": True,
        "proposal_only": True,
        **STATELESS_AUDIT,
        "queue_mutations_performed": 0,
        "memory_mutations_performed": 0,
        "raw_secret_exposed": False,
        "auto_apply": False,
    }


def public_preview(preview: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in preview.items() if key != "_request"}


def run_shadow(
    *,
    connection_factory: Callable[[], Any],
    preview: Mapping[str, Any],
    provider: GeminiShadowProvider,
    requested_by: str,
    notes: str | None = None,
) -> dict[str, Any]:
    del notes
    if not requested_by or not requested_by.strip():
        return {"status": "request_blocked", "reason_codes": ["requested_by_required"], "safety": _safety()}
    if preview.get("status") != "preview_ready":
        return public_preview(preview)
    request = preview["_request"]
    run_key = str(preview["shadow_operation_key"])

    conn = connection_factory()
    try:
        existing = shadow_repository.get_by_run_key(conn, run_key)
        if existing is not None:
            integrity_reasons = _existing_integrity_reasons(existing, preview=preview, request=request)
            if integrity_reasons:
                return {"status": "integrity_error", "reason_codes": integrity_reasons, "safety": _safety()}
            if existing["status"] == "running":
                return _existing_result(existing, "already_running")
            if existing["status"] in {"completed", "rejected", "failed", "skipped"}:
                return _existing_result(existing, "existing_result")
        created = shadow_repository.create_planned(
            conn,
            {
                "run_key": run_key,
                "request_id": request["request_id"],
                "provider_name": "gemini",
                "provider_kind": "external_model",
                "model_name": preview["model_name"],
                "model_role": preview["model_role"],
                "api_mode": "interactions",
                "project_key": request["project_key"],
                "scope_code": request["scope_code"],
                "workspace_id": request["workspace_id"],
                "request_schema_version": PROVIDER_REQUEST_SCHEMA_VERSION,
                "response_schema_version": PROVIDER_RESPONSE_SCHEMA_VERSION,
                "validation_schema_version": PROVIDER_VALIDATION_SCHEMA_VERSION,
                "redaction_policy_version": REDACTION_POLICY_VERSION,
                "external_data_policy": EXTERNAL_DATA_POLICY,
                "input_fingerprint": request["input_fingerprint"],
                "request_manifest": _manifest(request),
                "candidate_memory_ids": request["candidate_memory_ids"],
                "allowed_actions": request["allowed_actions"],
                "proposal_budget": request["proposal_budget"],
                "store_requested": 0,
                "previous_interaction_id_used": 0,
                "background_used": 0,
                "tools_used": 0,
                "file_api_used": 0,
                "grounding_used": 0,
            },
        )
        running = shadow_repository.transition(
            conn,
            created["id"],
            expected_status="planned",
            new_status="running",
            fields={"started_at": created["created_at"]},
        )
        conn.commit()
        run_id = running["id"]
    finally:
        conn.close()

    try:
        outcome = provider.analyze(request, model_role=str(preview["model_role"]))
        validation = outcome["validation"]
        accepted = validation["status"] == "accepted"
        final_status = "completed" if accepted else "rejected"
        proposals = validation["normalized_proposals"] if accepted else []
        reason_codes = validation["reason_codes"]
        proposal_counts = {
            action: sum(1 for item in proposals if item["action"] == action)
            for action in sorted(set(item["action"] for item in proposals))
        }
        fields = {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": outcome["latency_ms"],
            "input_tokens": outcome["usage"]["input_tokens"],
            "output_tokens": outcome["usage"]["output_tokens"],
            "total_tokens": outcome["usage"]["total_tokens"],
            "estimated_cost_usd": outcome["estimated_cost_usd"],
            "retry_count": outcome["retry_count"],
            "validation_status": validation["status"],
            "validation_reason_codes_json": reason_codes,
            "proposal_counts_json": proposal_counts,
            "abstain": int(validation["abstain"]),
            "response_fingerprint": validation["response_fingerprint"] or canonical_fingerprint(
                {
                    "request_id": request["request_id"],
                    "input_fingerprint": request["input_fingerprint"],
                    "validation_status": validation["status"],
                    "reason_codes": reason_codes,
                }
            ),
            "provider_metadata_json": {
                **outcome["provider_metadata"],
                "pricing": outcome["pricing"],
                "pricing_reason": outcome["pricing_reason"],
            },
        }
        conn = connection_factory()
        try:
            final = shadow_repository.transition(
                conn, run_id, expected_status="running", new_status=final_status, fields=fields
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "schema_version": SHADOW_RESULT_SCHEMA_VERSION,
            "status": final_status,
            "shadow_run_id": run_id,
            "shadow_operation_key": run_key,
            "request_id": request["request_id"],
            "input_fingerprint": request["input_fingerprint"],
            "provider_name": "gemini",
            "model_name": preview["model_name"],
            "model_role": preview["model_role"],
            "validation_status": validation["status"],
            "validation_reason_codes": reason_codes,
            "abstain": validation["abstain"],
            "proposal_counts": proposal_counts,
            "proposals": proposals,
            "latency_ms": outcome["latency_ms"],
            "usage": outcome["usage"],
            "estimated_cost_usd": outcome["estimated_cost_usd"],
            "retry_count": outcome["retry_count"],
            "safety": _safety(),
        }
    except ProviderCallError as exc:
        conn = connection_factory()
        try:
            final = shadow_repository.transition(
                conn,
                run_id,
                expected_status="running",
                new_status="failed",
                fields={
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "validation_status": "provider_failed",
                    "validation_reason_codes_json": [exc.category],
                    "error_category": exc.category,
                },
            )
            conn.commit()
        finally:
            conn.close()
        return _existing_result(final, "failed")


def _existing_integrity_reasons(
    row: Mapping[str, Any],
    *,
    preview: Mapping[str, Any],
    request: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    expected = {
        "run_key": preview["shadow_operation_key"],
        "request_id": request["request_id"],
        "input_fingerprint": request["input_fingerprint"],
        "provider_name": "gemini",
        "provider_kind": "external_model",
        "model_name": preview["model_name"],
        "model_role": preview["model_role"],
        "api_mode": "interactions",
        "project_key": request["project_key"],
        "scope_code": request["scope_code"],
        "workspace_id": request["workspace_id"],
        "request_schema_version": PROVIDER_REQUEST_SCHEMA_VERSION,
        "response_schema_version": PROVIDER_RESPONSE_SCHEMA_VERSION,
        "validation_schema_version": PROVIDER_VALIDATION_SCHEMA_VERSION,
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "external_data_policy": EXTERNAL_DATA_POLICY,
        "candidate_memory_ids": request["candidate_memory_ids"],
        "allowed_actions": request["allowed_actions"],
        "proposal_budget": request["proposal_budget"],
    }
    for key, value in expected.items():
        if row.get(key) != value:
            reasons.append(f"{key}_mismatch")
    if any(
        row.get(key) is not False
        for key in (
            "store_requested",
            "previous_interaction_id_used",
            "background_used",
            "tools_used",
            "file_api_used",
            "grounding_used",
        )
    ):
        reasons.append("stateless_audit_mismatch")
    if row["status"] in {"completed", "rejected"} and (
        not row.get("validation_status") or not row.get("response_fingerprint")
    ):
        reasons.append("terminal_audit_incomplete")
    if row["status"] == "failed" and not row.get("error_category"):
        reasons.append("terminal_audit_incomplete")
    return sorted(set(reasons))


def _existing_result(row: Mapping[str, Any], status: str) -> dict[str, Any]:
    return {
        "schema_version": SHADOW_RESULT_SCHEMA_VERSION,
        "status": status,
        "shadow_run_id": row["id"],
        "shadow_operation_key": row["run_key"],
        "request_id": row["request_id"],
        "input_fingerprint": row["input_fingerprint"],
        "provider_name": row["provider_name"],
        "model_name": row["model_name"],
        "model_role": row["model_role"],
        "validation_status": row["validation_status"],
        "validation_reason_codes": row["validation_reason_codes"],
        "abstain": row["abstain"],
        "proposal_counts": row["proposal_counts"],
        "proposals": [],
        "latency_ms": row["latency_ms"],
        "usage": {
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "total_tokens": row["total_tokens"],
        },
        "estimated_cost_usd": row["estimated_cost_usd"],
        "retry_count": row["retry_count"],
        "error_category": row["error_category"],
        "safety": _safety(),
    }
