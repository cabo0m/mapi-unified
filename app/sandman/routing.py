from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from app.sandman import shadow_repository
from mapi_core.sandman.contracts import (
    ContractError,
    PROVIDER_REQUEST_SCHEMA_VERSION,
    PROVIDER_RESPONSE_SCHEMA_VERSION,
    PROVIDER_VALIDATION_SCHEMA_VERSION,
    canonical_fingerprint,
    canonical_json,
    strict_json_loads,
)
from app.sandman.evaluation import (
    CORPUS_VERSION,
    EVALUATION_REPORT_SCHEMA_VERSION,
    EvaluationError,
    ROLLOUT_POLICY,
    evaluate_semantic_provider_bundle,
)
from app.sandman.providers.deterministic import DeterministicProvider
from mapi_core.sandman.providers.gemini import (
    PRIMARY_MODEL,
    PROVIDER_CONFIG_VERSION,
    GeminiConfig,
    GeminiShadowProvider,
    ProviderCallError,
)
from app.sandman.redaction import (
    EXTERNAL_DATA_POLICY,
    REDACTION_POLICY_VERSION,
    residual_sensitive_reason_codes,
)
from mapi_core.sandman.validator import validate_provider_response


MODEL_QUEUE_ROUTING_FLAG_KEY = "sandman_model_queue_routing_enabled"
ROUTING_POLICY_VERSION = "sandman_model_queue_routing_policy.v2"
ROUTE_PREVIEW_SCHEMA_VERSION = "sandman_model_queue_route_preview.v2"
ROUTE_RESULT_SCHEMA_VERSION = "sandman_model_queue_route_result.v2"
ROUTE_OPERATION_SCHEMA_VERSION = "sandman_model_queue_route_operation.v2"
MODEL_QUEUE_ORIGIN_SCHEMA_VERSION = "sandman_model_queue_origin.v2"
LEGACY_MODEL_QUEUE_ORIGIN_SCHEMA_VERSION = "sandman_model_queue_origin.v1"
QUEUE_PROPOSAL_KEY_SCHEMA_VERSION = "sandman_model_queue_proposal_key.v3"
MODEL_QUEUE_AUTO_APPLY = False
SUPPORTED_STAGE = "existing_memory"
QUEUE_TARGET = "consolidation_review"
ALLOWED_PROJECT_KEYS = ("mapi",)
ALLOWED_SCOPE_CODES = ("project",)
ALLOWED_MODEL_ROLES = ("primary",)
ALLOWED_PROVIDER_NAMES = ("gemini",)
ROUTABLE_ACTIONS = ("duplicate_of", "supersedes", "contradicts", "reinforces")
NON_ROUTABLE_ACTIONS = ("related_to",)
MAX_ROUTED_PROPOSALS_PER_RUN = 3
MAX_TOTAL_CANARY_PROPOSALS = 20
CORPUS_FINGERPRINT = "sha256:41698207874862ccaffc85924ed08bfa65447e8d020bceae7e0af540f8c4a072"
PROPOSAL_NOTICE = (
    "To jest zwalidowana propozycja modelu do ręcznego przeglądu. "
    "Nie jest faktem ani automatyczną zmianą pamięci."
)
PROPOSAL_TYPES = {
    "duplicate_of": "model_duplicate_review",
    "supersedes": "model_supersession_review",
    "contradicts": "model_contradiction_review",
    "reinforces": "model_reinforcement_review",
}
ACTION_PRIORITY = {action: index for index, action in enumerate(ROUTABLE_ACTIONS)}
LEGACY_ORIGIN_FIELDS = frozenset(
    {
        "schema_version",
        "routing_policy_version",
        "stage",
        "provider_name",
        "provider_kind",
        "model_name",
        "model_role",
        "provider_config_version",
        "api_mode",
        "shadow_run_id",
        "route_operation_key",
        "route_preview_hash",
        "request_id",
        "input_fingerprint",
        "response_fingerprint",
        "proposal_signature_fingerprint",
        "project_key",
        "scope_code",
        "workspace_id",
        "redaction_policy_version",
        "external_data_policy",
        "validation_schema_version",
        "evaluation_report_fingerprint",
        "deterministic_duplicate",
        "proposal_only",
        "created_by",
        "route_reason",
    }
)
ORIGIN_FIELDS = (
    LEGACY_ORIGIN_FIELDS - frozenset({"evaluation_report_fingerprint"})
) | frozenset(
    {
        "evaluation_evidence_fingerprint",
        "route_analysis_fingerprint",
        "queue_proposal_key",
    }
)
ROUTE_SUMMARY_FIELDS = frozenset(
    {
        "execution_mode",
        "routing_policy_version",
        "route_operation_key",
        "route_preview_hash",
        "evaluation_evidence_fingerprint",
        "route_analysis_fingerprint",
        "routing_status",
        "routed_count",
        "deduped_against_deterministic_count",
        "deduped_against_existing_model_queue_count",
        "not_routed_budget_count",
        "queue_proposal_keys",
        "canary_count_before",
        "canary_count_after",
        "remaining_canary_budget",
        "canary_integrity_status",
        "pre_network_skipped_count",
        "pre_network_skip_reason_codes",
        "queue_target",
        "auto_apply",
    }
)
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class RoutingError(ValueError):
    def __init__(self, reason_codes: str | list[str]):
        self.reason_codes = sorted(
            set([reason_codes] if isinstance(reason_codes, str) else reason_codes)
        )
        super().__init__(",".join(self.reason_codes))


def routing_policy() -> dict[str, Any]:
    return {
        "schema_version": ROUTING_POLICY_VERSION,
        "allowed_project_keys": list(ALLOWED_PROJECT_KEYS),
        "allowed_scope_codes": list(ALLOWED_SCOPE_CODES),
        "allowed_model_roles": list(ALLOWED_MODEL_ROLES),
        "allowed_provider_names": list(ALLOWED_PROVIDER_NAMES),
        "max_routed_proposals_per_run": MAX_ROUTED_PROPOSALS_PER_RUN,
        "max_total_canary_proposals": MAX_TOTAL_CANARY_PROPOSALS,
        "routable_actions": list(ROUTABLE_ACTIONS),
        "non_routable_actions": list(NON_ROUTABLE_ACTIONS),
        "supported_stage": SUPPORTED_STAGE,
        "queue_target": QUEUE_TARGET,
        "auto_apply": MODEL_QUEUE_AUTO_APPLY,
    }


def _safety(*, network_calls: int = 0, queue_writes: int = 0) -> dict[str, Any]:
    return {
        "proposal_only": True,
        "auto_apply": MODEL_QUEUE_AUTO_APPLY,
        "candidate_memory_mutations": 0,
        "candidate_link_mutations": 0,
        "queue_mutations_performed": int(queue_writes),
        "network_calls": int(network_calls),
        "store_requested": False,
        "raw_secret_exposed": False,
    }


def _blocked(
    status: str,
    reason_codes: list[str],
    *,
    stage: str,
    flag_evaluations: Mapping[str, Any] | None = None,
    evaluation_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": ROUTE_PREVIEW_SCHEMA_VERSION,
        "status": status,
        "stage": stage,
        "reason_codes": sorted(set(reason_codes)),
        "routing_policy": routing_policy(),
        "flag_evaluations": dict(flag_evaluations or {}),
        "evaluation_gate": dict(evaluation_gate or {}),
        "network_calls": 0,
        "queue_writes": 0,
        "auto_apply": MODEL_QUEUE_AUTO_APPLY,
        "safety": _safety(),
    }


def _parse_string_list(value: str | list[str], *, code: str) -> list[str]:
    if isinstance(value, str):
        try:
            value = strict_json_loads(value, invalid_code=code)
        except ContractError as exc:
            raise RoutingError(exc.reason_codes) from exc
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise RoutingError(code)
    return sorted(set(item.strip() for item in value))


def _flag_view(
    flag: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    *,
    expected_key: str,
) -> dict[str, Any]:
    missing = bool(flag.get("is_implicit_default"))
    enabled = bool(evaluation.get("enabled")) and not bool(
        evaluation.get("read_only_mode")
    )
    reason = (
        "missing_flag"
        if missing
        else "read_only_mode"
        if evaluation.get("read_only_mode")
        else str(evaluation.get("reason") or "flag_disabled")
    )
    return {
        "flag_key": expected_key,
        "enabled": enabled,
        "reason": reason if not enabled else str(evaluation.get("reason") or "enabled"),
        "is_implicit_default": missing,
        "rollout_mode": evaluation.get("rollout_mode"),
        "read_only_mode": bool(evaluation.get("read_only_mode")),
    }


def build_flag_evaluations(
    *,
    provider_flag: Mapping[str, Any],
    provider_evaluation: Mapping[str, Any],
    shadow_flag: Mapping[str, Any],
    shadow_evaluation: Mapping[str, Any],
    routing_flag: Mapping[str, Any],
    routing_evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "sandman_provider_v3_enabled": _flag_view(
            provider_flag,
            provider_evaluation,
            expected_key="sandman_provider_v3_enabled",
        ),
        "sandman_gemini_shadow_enabled": _flag_view(
            shadow_flag,
            shadow_evaluation,
            expected_key="sandman_gemini_shadow_enabled",
        ),
        MODEL_QUEUE_ROUTING_FLAG_KEY: _flag_view(
            routing_flag,
            routing_evaluation,
            expected_key=MODEL_QUEUE_ROUTING_FLAG_KEY,
        ),
    }


def flags_are_enabled(flag_evaluations: Mapping[str, Any]) -> bool:
    return all(
        bool(flag_evaluations.get(key, {}).get("enabled"))
        for key in (
            "sandman_provider_v3_enabled",
            "sandman_gemini_shadow_enabled",
            MODEL_QUEUE_ROUTING_FLAG_KEY,
        )
    )


def _mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RoutingError(code)
    return dict(value)


def _finite_number(value: Any, *, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoutingError(code)
    number = float(value)
    if number != number or number in {float("inf"), float("-inf")}:
        raise RoutingError(code)
    return number


def _rate(value: Any, *, code: str) -> float:
    number = _finite_number(value, code=code)
    if not 0.0 <= number <= 1.0:
        raise RoutingError(code)
    return number


def _count(value: Any, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RoutingError(code)
    return value


def _reject_invalid_numeric_metadata(value: Any, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_invalid_numeric_metadata(item, path=(*path, str(key)))
        return
    if isinstance(value, list):
        for item in value:
            _reject_invalid_numeric_metadata(item, path=path)
        return
    key = path[-1] if path else ""
    numeric_field = (
        key.endswith(("_count", "_rate", "_precision", "_latency_ms", "_tokens"))
        or "cost" in key
        or key
        in {
            "precision",
            "recall",
            "f1",
            "case_count",
            "queue_writes_performed",
            "cross_project_accepted_errors",
            "cross_scope_accepted_errors",
            "sensitive_leakage",
            "invalid_accepted_schema",
        }
    )
    if numeric_field and isinstance(value, bool):
        raise RoutingError("invalid_numeric_metadata_type")
    if numeric_field and isinstance(value, float) and (
        value != value or value in {float("inf"), float("-inf")}
    ):
        raise RoutingError("invalid_numeric_metadata_value")


def evaluate_routing_evidence(
    operator_prediction_bundle_json: str,
    *,
    evaluation_report_json: str | None = None,
) -> dict[str, Any]:
    if not isinstance(operator_prediction_bundle_json, str) or not operator_prediction_bundle_json.strip():
        raise RoutingError("evaluation_prediction_bundle_required")
    if "://" in operator_prediction_bundle_json[:256]:
        raise RoutingError("evaluation_prediction_bundle_must_be_inline_json")
    try:
        report = evaluate_semantic_provider_bundle(
            evaluation_kind="operator_supplied_gemini_replay",
            prediction_bundle_json=operator_prediction_bundle_json,
            include_case_results=False,
            include_debug=False,
        )
    except EvaluationError as exc:
        raise RoutingError(exc.reason_codes) from exc
    if evaluation_report_json:
        try:
            supplied = strict_json_loads(
                evaluation_report_json, invalid_code="invalid_evaluation_report_json"
            )
        except ContractError as exc:
            raise RoutingError(exc.reason_codes) from exc
        if canonical_json(supplied) != canonical_json(report):
            raise RoutingError("evaluation_report_recomputation_mismatch")
    return validate_evaluation_report(report)


def validate_evaluation_report(value: Mapping[str, Any]) -> dict[str, Any]:
    report = _mapping(value, "invalid_evaluation_report_schema")
    _reject_invalid_numeric_metadata(report)
    corpus = _mapping(report.get("corpus"), "invalid_evaluation_report_schema")
    provider = _mapping(report.get("provider"), "invalid_evaluation_report_schema")
    safety_gate = _mapping(
        report.get("safety_gate"), "invalid_evaluation_report_schema"
    )
    quality_gate = _mapping(
        report.get("quality_gate"), "invalid_evaluation_report_schema"
    )
    sufficiency_gate = _mapping(
        report.get("sufficiency_gate"), "invalid_evaluation_report_schema"
    )
    rollout = _mapping(
        report.get("rollout_recommendation"), "invalid_evaluation_report_schema"
    )
    metrics = _mapping(report.get("metrics"), "invalid_evaluation_report_schema")
    safety = _mapping(report.get("safety"), "invalid_evaluation_report_schema")
    duplicate = _mapping(metrics.get("duplicate"), "invalid_evaluation_report_schema")
    supersession = _mapping(
        metrics.get("supersession"), "invalid_evaluation_report_schema"
    )
    reasons: list[str] = []
    checks = (
        (report.get("schema_version") == EVALUATION_REPORT_SCHEMA_VERSION, "wrong_report_schema"),
        (report.get("status") == "completed", "evaluation_not_completed"),
        (
            report.get("evaluation_kind") == "operator_supplied_gemini_replay",
            "operator_replay_required",
        ),
        (corpus.get("version") == CORPUS_VERSION, "wrong_corpus_version"),
        (corpus.get("fingerprint") == CORPUS_FINGERPRINT, "wrong_corpus_fingerprint"),
        (
            not isinstance(corpus.get("case_count"), bool)
            and corpus.get("case_count") == 100,
            "wrong_corpus_case_count",
        ),
        (provider.get("provider_name") == "gemini", "wrong_provider"),
        (provider.get("model_name") == PRIMARY_MODEL, "wrong_model"),
        (
            provider.get("provider_config_version") == PROVIDER_CONFIG_VERSION,
            "wrong_provider_config",
        ),
        (safety_gate.get("passed") is True, "safety_gate_failed"),
        (quality_gate.get("passed") is True, "quality_gate_failed"),
        (sufficiency_gate.get("passed") is True, "sufficiency_gate_failed"),
        (
            rollout.get("recommendation") == "eligible_for_operator_review",
            "evaluation_not_eligible",
        ),
        (rollout.get("rollout_eligible") is True, "rollout_not_eligible"),
        (rollout.get("routing_enabled") is False, "evaluation_routing_already_enabled"),
        (
            not isinstance(rollout.get("queue_writes_performed"), bool)
            and rollout.get("queue_writes_performed") == 0,
            "evaluation_queue_writes_detected",
        ),
        (rollout.get("auto_apply") is False, "evaluation_auto_apply_detected"),
        (
            rollout.get("requires_explicit_user_decision_for_b09") is True,
            "explicit_b09_decision_not_attested",
        ),
    )
    for passed, code in checks:
        if not passed:
            reasons.append(code)
    try:
        duplicate_precision = _rate(
            duplicate.get("precision"), code="invalid_duplicate_precision"
        )
        supersession_precision = _rate(
            supersession.get("precision"), code="invalid_supersession_precision"
        )
        false_supersession_rate = _rate(
            metrics.get("false_supersession_rate"),
            code="invalid_false_supersession_rate",
        )
        cross_project_errors = _count(
            safety.get("cross_project_accepted_errors"),
            code="invalid_cross_project_error_count",
        )
        cross_scope_errors = _count(
            safety.get("cross_scope_accepted_errors"),
            code="invalid_cross_scope_error_count",
        )
        sensitive_leakage = _count(
            safety.get("sensitive_leakage"), code="invalid_sensitive_leakage_count"
        )
        invalid_schema = _count(
            safety.get("invalid_accepted_schema"),
            code="invalid_accepted_schema_count",
        )
    except RoutingError as exc:
        reasons.extend(exc.reason_codes)
        duplicate_precision = supersession_precision = false_supersession_rate = -1.0
        cross_project_errors = cross_scope_errors = sensitive_leakage = invalid_schema = -1
    if duplicate_precision < ROLLOUT_POLICY["duplicate_precision_min"]:
        reasons.append("duplicate_precision_below_threshold")
    if supersession_precision < ROLLOUT_POLICY["supersession_precision_min"]:
        reasons.append("supersession_precision_below_threshold")
    if false_supersession_rate > ROLLOUT_POLICY["false_supersession_rate_max"]:
        reasons.append("false_supersession_rate_above_threshold")
    if cross_project_errors:
        reasons.append("cross_project_errors_detected")
    if cross_scope_errors:
        reasons.append("cross_scope_errors_detected")
    if sensitive_leakage:
        reasons.append("sensitive_leakage_detected")
    if invalid_schema:
        reasons.append("invalid_schema_accepted")
    if reasons:
        raise RoutingError(reasons)
    fingerprint_payload = {
        key: item
        for key, item in report.items()
        if key not in {"case_results", "debug"}
    }
    return {
        "status": "passed",
        "reason_codes": [],
        "evaluation_kind": report["evaluation_kind"],
        "corpus_version": corpus["version"],
        "corpus_fingerprint": corpus["fingerprint"],
        "provider_name": provider["provider_name"],
        "model_name": provider["model_name"],
        "provider_config_version": provider["provider_config_version"],
        "evaluation_evidence_fingerprint": canonical_fingerprint(fingerprint_payload),
        "thresholds": {
            "duplicate_precision": duplicate_precision,
            "supersession_precision": supersession_precision,
            "false_supersession_rate": false_supersession_rate,
        },
    }


def proposal_signature(proposal: Mapping[str, Any]) -> dict[str, Any]:
    action = proposal.get("action")
    source_ids = proposal.get("source_memory_ids")
    evidence_ids = proposal.get("evidence_memory_ids")
    target_id = proposal.get("target_memory_id")
    if not isinstance(action, str) or not action.strip():
        raise RoutingError("invalid_proposal_signature")
    if not isinstance(source_ids, list) or not isinstance(evidence_ids, list):
        raise RoutingError("invalid_proposal_signature")
    all_ids = [*source_ids, target_id, *evidence_ids]
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 1
        for item in all_ids
    ):
        raise RoutingError("invalid_proposal_signature")
    return {
        "action": action,
        "source_memory_ids": sorted(set(source_ids)),
        "target_memory_id": target_id,
        "evidence_memory_ids": sorted(set(evidence_ids)),
    }


def proposal_signature_fingerprint(proposal: Mapping[str, Any]) -> str:
    return canonical_fingerprint(proposal_signature(proposal))


def _signature_key(proposal: Mapping[str, Any]) -> tuple[Any, ...]:
    signature = proposal_signature(proposal)
    return (
        signature["action"],
        tuple(signature["source_memory_ids"]),
        signature["target_memory_id"],
        tuple(signature["evidence_memory_ids"]),
    )


def _selection_key(proposal: Mapping[str, Any]) -> tuple[Any, ...]:
    signature = proposal_signature(proposal)
    return (
        ACTION_PRIORITY.get(signature["action"], 99),
        -float(proposal["confidence"]),
        tuple(signature["source_memory_ids"]),
        signature["target_memory_id"],
        str(proposal["proposal_id"]),
    )


def parse_model_origin(value: str) -> dict[str, Any]:
    try:
        parsed = strict_json_loads(value, invalid_code="invalid_model_origin_json")
    except ContractError as exc:
        raise RoutingError(exc.reason_codes) from exc
    if not isinstance(parsed, Mapping):
        raise RoutingError("invalid_model_origin_schema")
    result = dict(parsed)
    is_legacy = (
        result.get("schema_version") == LEGACY_MODEL_QUEUE_ORIGIN_SCHEMA_VERSION
        and set(result) == LEGACY_ORIGIN_FIELDS
    )
    is_current = (
        result.get("schema_version") == MODEL_QUEUE_ORIGIN_SCHEMA_VERSION
        and set(result) == ORIGIN_FIELDS
    )
    if not (is_legacy or is_current):
        raise RoutingError("invalid_model_origin_schema")
    if (
        result["routing_policy_version"]
        not in {"sandman_model_queue_routing_policy.v1", ROUTING_POLICY_VERSION}
        or result["stage"] != SUPPORTED_STAGE
        or result["provider_name"] != "gemini"
        or result["provider_kind"] != "external_model"
        or result["model_name"] != PRIMARY_MODEL
        or result["model_role"] != "primary"
        or result["provider_config_version"] != PROVIDER_CONFIG_VERSION
        or result["api_mode"] != "interactions"
        or result["redaction_policy_version"] != REDACTION_POLICY_VERSION
        or result["external_data_policy"] != EXTERNAL_DATA_POLICY
        or result["validation_schema_version"] != PROVIDER_VALIDATION_SCHEMA_VERSION
        or result["created_by"] != "sandman_v3_route_canary"
        or result["proposal_only"] is not True
        or result["deterministic_duplicate"] is not False
        or result["project_key"] not in ALLOWED_PROJECT_KEYS
        or result["scope_code"] not in ALLOWED_SCOPE_CODES
    ):
        raise RoutingError("invalid_model_origin_contract")
    if (
        isinstance(result["shadow_run_id"], bool)
        or not isinstance(result["shadow_run_id"], int)
        or result["shadow_run_id"] <= 0
        or isinstance(result["workspace_id"], bool)
        or not isinstance(result["workspace_id"], int)
        or result["workspace_id"] <= 0
    ):
        raise RoutingError("invalid_model_origin_identifier")
    legacy_fingerprint_fields = (
        "route_operation_key",
        "route_preview_hash",
        "input_fingerprint",
        "response_fingerprint",
        "proposal_signature_fingerprint",
        "evaluation_report_fingerprint",
    )
    current_fingerprint_fields = (
        "route_operation_key",
        "route_preview_hash",
        "input_fingerprint",
        "response_fingerprint",
        "proposal_signature_fingerprint",
        "evaluation_evidence_fingerprint",
        "route_analysis_fingerprint",
        "queue_proposal_key",
    )
    for field in legacy_fingerprint_fields if is_legacy else current_fingerprint_fields:
        if not isinstance(result[field], str) or not SHA256_PATTERN.fullmatch(
            result[field]
        ):
            raise RoutingError("invalid_model_origin_fingerprint")
    if (
        not isinstance(result["request_id"], str)
        or not result["request_id"].strip()
        or not isinstance(result["route_reason"], str)
        or not result["route_reason"].strip()
        or residual_sensitive_reason_codes(result["route_reason"])
    ):
        raise RoutingError("unsafe_model_origin_text")
    return result


def _model_route_rows(conn: Any, *, project_key: str) -> list[Mapping[str, Any]]:
    rows = conn.execute(
        """
        SELECT m.id, m.source, m.source_context, m.created_at,
               m.scope_code, m.workspace_id,
               COUNT(r.id) AS review_count
          FROM memories m
          LEFT JOIN memory_consolidation_review_items r
            ON r.proposal_memory_id=m.id
         WHERE m.memory_type='consolidation_proposal'
           AND m.project_key=?
           AND m.source LIKE 'sandman_v3:gemini:queue_route:%'
         GROUP BY m.id, m.source, m.source_context, m.created_at,
                  m.scope_code, m.workspace_id
         ORDER BY m.id ASC
        """,
        (project_key,),
    ).fetchall()
    return list(rows)


def model_routed_proposals(conn: Any, *, project_key: str) -> list[dict[str, Any]]:
    rows = _model_route_rows(conn, project_key=project_key)
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            origin = parse_model_origin(str(row["source_context"] or ""))
        except RoutingError:
            continue
        items.append(
            {
                "proposal_memory_id": int(row["id"]),
                "created_at": row["created_at"],
                "origin": origin,
            }
        )
    return items


def canary_state(conn: Any, *, project_key: str) -> dict[str, Any]:
    rows = _model_route_rows(conn, project_key=project_key)
    valid = 0
    invalid = 0
    missing_review = 0
    duplicate_queue_keys = 0
    seen_keys: set[str] = set()
    reasons: set[str] = set()
    prefix = "sandman_v3:gemini:queue_route:"
    for row in rows:
        review_count = int(row["review_count"])
        if review_count != 1:
            missing_review += 1
            reasons.add(
                "missing_model_queue_review"
                if review_count == 0
                else "duplicate_model_queue_review"
            )
        source = str(row["source"] or "")
        queue_key = source.removeprefix(prefix)
        if not SHA256_PATTERN.fullmatch(queue_key):
            invalid += 1
            reasons.add("invalid_model_queue_source_key")
            continue
        if queue_key in seen_keys:
            duplicate_queue_keys += 1
            reasons.add("duplicate_model_queue_key")
        seen_keys.add(queue_key)
        try:
            origin = parse_model_origin(str(row["source_context"] or ""))
            if (
                origin["project_key"] != project_key
                or origin["scope_code"] != str(row["scope_code"] or "")
                or int(origin["workspace_id"]) != int(row["workspace_id"])
                or origin.get("queue_proposal_key", queue_key) != queue_key
            ):
                raise RoutingError("model_origin_boundary_or_key_mismatch")
        except RoutingError as exc:
            invalid += 1
            reasons.update(exc.reason_codes)
        else:
            valid += 1
    physical = len(rows)
    remaining = max(0, MAX_TOTAL_CANARY_PROPOSALS - physical)
    integrity_ok = not reasons
    return {
        "current_routed_count": physical,
        "physical_routed_count": physical,
        "valid_origin_count": valid,
        "invalid_origin_count": invalid,
        "missing_review_count": missing_review,
        "duplicate_queue_key_count": duplicate_queue_keys,
        "integrity_status": "ok" if integrity_ok else "error",
        "integrity_reason_codes": sorted(reasons),
        "remaining_canary_budget": remaining,
        "max_per_run": MAX_ROUTED_PROPOSALS_PER_RUN,
        "max_total": MAX_TOTAL_CANARY_PROPOSALS,
        "paused": remaining <= 0 or not integrity_ok,
        "requires_operator_review": remaining <= 0 or not integrity_ok,
    }


def route_operation_key(
    *,
    route_analysis_fingerprint: str,
    model_name: str,
    model_role: str,
) -> str:
    return canonical_fingerprint(
        {
            "schema_version": ROUTE_OPERATION_SCHEMA_VERSION,
            "route_analysis_fingerprint": route_analysis_fingerprint,
            "routing_policy_version": ROUTING_POLICY_VERSION,
            "provider_name": "gemini",
            "model_name": model_name,
            "model_role": model_role,
            "provider_config_version": PROVIDER_CONFIG_VERSION,
            "response_schema_version": PROVIDER_RESPONSE_SCHEMA_VERSION,
            "redaction_policy_version": REDACTION_POLICY_VERSION,
        }
    )


def preview_model_queue_route(
    conn: Any,
    *,
    stage: str,
    project_key: str,
    scope_code: str,
    memory_ids_json: str,
    allowed_actions_json: str,
    proposal_budget: int,
    model_role: str,
    operator_prediction_bundle_json: str,
    evaluation_report_json: str | None,
    include_debug: bool,
    request_builder: Callable[..., dict[str, Any]],
    provider_flag: Mapping[str, Any],
    provider_evaluation: Mapping[str, Any],
    shadow_flag: Mapping[str, Any],
    shadow_evaluation: Mapping[str, Any],
    routing_flag: Mapping[str, Any],
    routing_evaluation: Mapping[str, Any],
    config: GeminiConfig,
    request_id_factory: Callable[[], str],
) -> dict[str, Any]:
    flag_evaluations = build_flag_evaluations(
        provider_flag=provider_flag,
        provider_evaluation=provider_evaluation,
        shadow_flag=shadow_flag,
        shadow_evaluation=shadow_evaluation,
        routing_flag=routing_flag,
        routing_evaluation=routing_evaluation,
    )
    if stage != SUPPORTED_STAGE:
        return _blocked(
            "stage_unsupported",
            ["capture_stage_not_supported_by_current_provider_contract"],
            stage=stage,
            flag_evaluations=flag_evaluations,
        )
    boundary_reasons: list[str] = []
    if project_key not in ALLOWED_PROJECT_KEYS:
        boundary_reasons.append("project_not_allowlisted")
    if scope_code not in ALLOWED_SCOPE_CODES:
        boundary_reasons.append("scope_not_allowlisted")
    if model_role not in ALLOWED_MODEL_ROLES:
        boundary_reasons.append("model_role_not_allowlisted")
    if isinstance(proposal_budget, bool) or not 1 <= int(proposal_budget) <= 3:
        boundary_reasons.append("route_proposal_budget_out_of_range")
    try:
        actions = _parse_string_list(
            allowed_actions_json, code="invalid_allowed_actions_json"
        )
    except RoutingError as exc:
        boundary_reasons.extend(exc.reason_codes)
        actions = []
    if not actions:
        boundary_reasons.append("allowed_actions_required")
    if any(action in NON_ROUTABLE_ACTIONS for action in actions):
        boundary_reasons.append("non_routable_action_requested")
    if any(action not in ROUTABLE_ACTIONS for action in actions):
        boundary_reasons.append("invalid_routable_action")
    if boundary_reasons:
        return _blocked(
            "request_blocked",
            boundary_reasons,
            stage=stage,
            flag_evaluations=flag_evaluations,
        )
    try:
        evaluation_gate = evaluate_routing_evidence(
            operator_prediction_bundle_json,
            evaluation_report_json=evaluation_report_json,
        )
    except RoutingError as exc:
        return _blocked(
            "evaluation_blocked",
            exc.reason_codes,
            stage=stage,
            flag_evaluations=flag_evaluations,
        )
    try:
        model_name = config.validated().model_for_role(model_role)
    except ValueError as exc:
        return _blocked(
            "provider_unavailable",
            [str(exc)],
            stage=stage,
            flag_evaluations=flag_evaluations,
            evaluation_gate=evaluation_gate,
        )
    provider_unconfigured = not config.api_key_configured
    request_preview = request_builder(
        conn,
        project_key=project_key,
        scope_code=scope_code,
        memory_ids_json=memory_ids_json,
        allowed_actions_json=actions,
        provider_name="gemini",
        proposal_budget=int(proposal_budget),
        include_debug=include_debug,
        feature_flag={**dict(provider_flag), "is_implicit_default": False},
        feature_flag_evaluation={
            **dict(provider_evaluation),
            "enabled": True,
            "read_only_mode": False,
        },
        request_id_factory=request_id_factory,
    )
    if request_preview.get("status") not in {"request_ready", "request_ready_partial"}:
        return _blocked(
            "request_blocked",
            list(request_preview.get("reason_codes") or ["request_blocked"]),
            stage=stage,
            flag_evaluations=flag_evaluations,
            evaluation_gate=evaluation_gate,
        )
    request = request_preview["request"]
    blocked_states = {
        str(item.get("state_code") or "")
        for item in request["candidates"]
        if str(item.get("state_code") or "") in {"archived", "superseded"}
    }
    if blocked_states:
        return _blocked(
            "request_blocked",
            [f"candidate_state_{state}" for state in sorted(blocked_states)],
            stage=stage,
            flag_evaluations=flag_evaluations,
            evaluation_gate=evaluation_gate,
        )
    deterministic_response = DeterministicProvider().analyze(request)
    deterministic_validation = validate_provider_response(
        request, deterministic_response, provider_name="deterministic"
    )
    deterministic_proposals = (
        deterministic_validation["normalized_proposals"]
        if deterministic_validation["status"] == "accepted"
        else []
    )
    deterministic_signatures = sorted(
        proposal_signature_fingerprint(item) for item in deterministic_proposals
    )
    candidate_snapshots = [
        {
            "memory_id": item["memory_id"],
            "content_sha256": item["content_sha256"],
            "project_key": item["project_key"],
            "scope_code": item["scope_code"],
            "workspace_id": item["workspace_id"],
            "state_code": item["state_code"],
            "updated_at": item["updated_at"],
        }
        for item in request["candidates"]
    ]
    analysis_payload = {
        "schema_version": "sandman_model_queue_route_analysis.v2",
        "routing_policy_version": ROUTING_POLICY_VERSION,
        "stage": stage,
        "project_key": project_key,
        "scope_code": scope_code,
        "workspace_id": request["workspace_id"],
        "provider_name": "gemini",
        "model_name": model_name,
        "model_role": model_role,
        "provider_config_version": PROVIDER_CONFIG_VERSION,
        "request_schema_version": PROVIDER_REQUEST_SCHEMA_VERSION,
        "response_schema_version": PROVIDER_RESPONSE_SCHEMA_VERSION,
        "validation_schema_version": PROVIDER_VALIDATION_SCHEMA_VERSION,
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "external_data_policy": EXTERNAL_DATA_POLICY,
        "input_fingerprint": request["input_fingerprint"],
        "candidate_memory_ids": request["candidate_memory_ids"],
        "candidate_snapshots": candidate_snapshots,
        "allowed_actions": actions,
        "proposal_budget": int(proposal_budget),
        "deterministic_proposal_signatures": deterministic_signatures,
        "evaluation_evidence_fingerprint": evaluation_gate[
            "evaluation_evidence_fingerprint"
        ],
        "max_per_run": MAX_ROUTED_PROPOSALS_PER_RUN,
        "max_total": MAX_TOTAL_CANARY_PROPOSALS,
    }
    analysis_fingerprint = canonical_fingerprint(analysis_payload)
    operation_key = route_operation_key(
        route_analysis_fingerprint=analysis_fingerprint,
        model_name=model_name,
        model_role=model_role,
    )
    canary = canary_state(conn, project_key=project_key)
    hash_payload = {
        **analysis_payload,
        "schema_version": ROUTE_PREVIEW_SCHEMA_VERSION,
        "route_analysis_fingerprint": analysis_fingerprint,
        "route_operation_key": operation_key,
        "flag_evaluations": flag_evaluations,
        "physical_routed_count": canary["physical_routed_count"],
        "remaining_canary_budget": canary["remaining_canary_budget"],
        "canary_integrity_status": canary["integrity_status"],
        "canary_integrity_reason_codes": canary["integrity_reason_codes"],
    }
    preview_hash = canonical_fingerprint(hash_payload)
    blocked_status = None
    blocked_reasons: list[str] = []
    if not flags_are_enabled(flag_evaluations):
        blocked_status = "feature_disabled"
        blocked_reasons.extend(
            item["reason"]
            for item in flag_evaluations.values()
            if not item["enabled"]
        )
    if provider_unconfigured:
        blocked_status = "provider_unconfigured"
        blocked_reasons.append("api_key_missing")
    if canary["integrity_status"] != "ok":
        blocked_status = "canary_integrity_error"
        blocked_reasons.extend(canary["integrity_reason_codes"])
    elif canary["remaining_canary_budget"] <= 0:
        blocked_status = "canary_paused_for_review"
        blocked_reasons.append("canary_total_cap_reached")
    result = {
        "schema_version": ROUTE_PREVIEW_SCHEMA_VERSION,
        "status": "preview_ready",
        "stage": stage,
        "queue_target": QUEUE_TARGET,
        "route_preview_hash": preview_hash,
        "route_operation_key": operation_key,
        "route_analysis_fingerprint": analysis_fingerprint,
        "request_summary": {
            "schema_version": request["schema_version"],
            "request_id": request["request_id"],
            "input_fingerprint": request["input_fingerprint"],
            "model_name": model_name,
            "project_key": request["project_key"],
            "scope_code": request["scope_code"],
            "workspace_id": request["workspace_id"],
            "candidate_memory_ids": list(request["candidate_memory_ids"]),
            "allowed_actions": list(request["allowed_actions"]),
            "proposal_budget": request["proposal_budget"],
        },
        "flag_evaluations": flag_evaluations,
        "evaluation_gate": evaluation_gate,
        "deterministic_dedupe_baseline": {
            "proposal_count": len(deterministic_proposals),
            "signature_fingerprints": deterministic_signatures,
        },
        "routing_policy": routing_policy(),
        "canary": canary,
        "network_calls": 0,
        "queue_writes": 0,
        "auto_apply": MODEL_QUEUE_AUTO_APPLY,
        "safety": _safety(),
        "_request": request,
        "_candidate_snapshots": candidate_snapshots,
        "_deterministic_proposals": deterministic_proposals,
        "_operator_prediction_bundle_json": operator_prediction_bundle_json,
        "_preview_args": {
            "stage": stage,
            "project_key": project_key,
            "scope_code": scope_code,
            "memory_ids_json": memory_ids_json,
            "allowed_actions_json": canonical_json(actions),
            "proposal_budget": int(proposal_budget),
            "model_role": model_role,
            "operator_prediction_bundle_json": operator_prediction_bundle_json,
            "evaluation_report_json": evaluation_report_json,
            "include_debug": include_debug,
        },
    }
    if blocked_status:
        result["status"] = blocked_status
        result["reason_codes"] = sorted(set(blocked_reasons))
        result["requires_operator_review"] = bool(canary["requires_operator_review"])
    if include_debug:
        result["debug"] = {
            "rule_ids": [
                "triple_feature_gate",
                "real_evaluation_report_gate",
                "exact_deterministic_dedupe",
                "canary_budget",
                "proposal_only",
            ]
        }
    return result


def public_preview(preview: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in preview.items() if not key.startswith("_")}


def _manifest(request: Mapping[str, Any], preview: Mapping[str, Any]) -> dict[str, Any]:
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
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "external_data_policy": EXTERNAL_DATA_POLICY,
        "raw_secret_exposed": False,
        "full_project_dump": False,
        "execution_mode": "route_canary",
        "route_analysis_fingerprint": preview["route_analysis_fingerprint"],
        "evaluation_evidence_fingerprint": preview["evaluation_gate"][
            "evaluation_evidence_fingerprint"
        ],
    }


def _route_existing_result(row: Mapping[str, Any], status: str) -> dict[str, Any]:
    metadata = row.get("provider_metadata") or {}
    route_summary = (
        dict(metadata)
        if isinstance(metadata, Mapping)
        and metadata.get("execution_mode") == "route_canary"
        else {}
    )
    return {
        "schema_version": ROUTE_RESULT_SCHEMA_VERSION,
        "status": status,
        "stage": SUPPORTED_STAGE,
        "provider_name": row["provider_name"],
        "model_name": row["model_name"],
        "model_role": row["model_role"],
        "shadow_run_id": row["id"],
        "route_operation_key": row["run_key"],
        "route_preview_hash": route_summary.get("route_preview_hash"),
        "input_fingerprint": row["input_fingerprint"],
        "validation_status": row.get("validation_status"),
        "routing_status": route_summary.get("routing_status"),
        "summary": route_summary,
        "routed_items": [],
        "deduped_items": [],
        "not_routed_items": [],
        "fallback": None,
        "canary": {
            "current_routed_count": route_summary.get("canary_count_after"),
            "remaining_canary_budget": route_summary.get(
                "remaining_canary_budget"
            ),
            "max_per_run": MAX_ROUTED_PROPOSALS_PER_RUN,
            "max_total": MAX_TOTAL_CANARY_PROPOSALS,
        },
        "flag_evaluations": {},
        "evaluation_gate": {
            "evaluation_evidence_fingerprint": route_summary.get(
                "evaluation_evidence_fingerprint"
                ) or route_summary.get(
                "evaluation_report_fingerprint"
            )
        },
        "safety": _safety(network_calls=0, queue_writes=0),
    }


def existing_route_result_for_preview(
    conn: Any,
    *,
    preview: Mapping[str, Any],
) -> dict[str, Any] | None:
    operation_key = preview.get("route_operation_key")
    if not isinstance(operation_key, str):
        return None
    row = shadow_repository.get_by_run_key(conn, operation_key)
    if row is None:
        return None
    return _route_existing_result(
        row, "already_running" if row["status"] == "running" else "existing_result"
    )


def _finalize_failed(
    connection_factory: Callable[[], Any],
    run_id: int,
    *,
    category: str,
    provider_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    conn = connection_factory()
    try:
        fields = {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "validation_status": "provider_failed",
            "validation_reason_codes_json": [category],
            "error_category": category,
            "provider_metadata_json": dict(provider_fields or {}),
        }
        row = shadow_repository.transition(
            conn,
            run_id,
            expected_status="running",
            new_status="failed",
            fields=fields,
        )
        conn.commit()
        return row
    finally:
        conn.close()


def _finalize_skipped(
    connection_factory: Callable[[], Any],
    run_id: int,
    *,
    reason_codes: list[str],
) -> dict[str, Any]:
    conn = connection_factory()
    try:
        row = shadow_repository.transition(
            conn,
            run_id,
            expected_status="planned",
            new_status="skipped",
            fields={
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "validation_status": "pre_network_blocked",
                "validation_reason_codes_json": sorted(set(reason_codes)),
                "error_category": "pre_network_blocked",
                "provider_metadata_json": {
                    "execution_mode": "route_canary",
                    "routing_policy_version": ROUTING_POLICY_VERSION,
                    "pre_network_skipped_count": 1,
                    "pre_network_skip_reason_codes": sorted(set(reason_codes)),
                },
            },
        )
        conn.commit()
        return row
    finally:
        conn.close()


def _fallback(preview: Mapping[str, Any]) -> dict[str, Any]:
    proposals = list(preview.get("_deterministic_proposals") or [])
    return {
        "fallback_provider": "deterministic",
        "fallback_status": "preview_completed",
        "fallback_proposal_count": len(proposals),
        "routed": False,
        "queue_writes": 0,
    }


def _queue_key(
    proposal: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    model_name: str,
) -> str:
    return canonical_fingerprint(
        {
            "schema_version": QUEUE_PROPOSAL_KEY_SCHEMA_VERSION,
            "routing_policy_version": ROUTING_POLICY_VERSION,
            "project_key": request["project_key"],
            "scope_code": request["scope_code"],
            "workspace_id": request["workspace_id"],
            "provider_name": "gemini",
            "model_name": model_name,
            "provider_config_version": PROVIDER_CONFIG_VERSION,
            "input_fingerprint": request["input_fingerprint"],
            "proposal_signature": proposal_signature(proposal),
        }
    )


def find_existing_structural_model_queue_matches(
    conn: Any,
    *,
    proposal: Mapping[str, Any],
    request: Mapping[str, Any],
    model_name: str,
) -> list[dict[str, Any]]:
    signature_fingerprint = proposal_signature_fingerprint(proposal)
    matches: list[dict[str, Any]] = []
    prefix = "sandman_v3:gemini:queue_route:"
    for row in _model_route_rows(conn, project_key=str(request["project_key"])):
        try:
            origin = parse_model_origin(str(row["source_context"] or ""))
        except RoutingError as exc:
            raise RoutingError(["canary_integrity_error", *exc.reason_codes]) from exc
        if int(row["review_count"]) != 1:
            raise RoutingError("canary_integrity_error")
        if (
            origin["project_key"] == request["project_key"]
            and origin["scope_code"] == request["scope_code"]
            and int(origin["workspace_id"]) == int(request["workspace_id"])
            and origin["provider_name"] == "gemini"
            and origin["model_name"] == model_name
            and origin["provider_config_version"] == PROVIDER_CONFIG_VERSION
            and origin["input_fingerprint"] == request["input_fingerprint"]
            and origin["proposal_signature_fingerprint"]
            == signature_fingerprint
        ):
            source = str(row["source"] or "")
            queue_key = source.removeprefix(prefix)
            if not SHA256_PATTERN.fullmatch(queue_key):
                raise RoutingError("canary_integrity_error")
            matches.append(
                {
                    "proposal_memory_id": int(row["id"]),
                    "queue_proposal_key": queue_key,
                    "origin": origin,
                }
            )
    if len(matches) > 1:
        raise RoutingError("canary_integrity_error")
    return matches


def _proposal_content(
    proposal: Mapping[str, Any],
    *,
    proposal_type: str,
    model_name: str,
    shadow_run_id: int,
    route_operation_key_value: str,
) -> str:
    return "\n".join(
        [
            PROPOSAL_NOTICE,
            f"Akcja: {proposal_type}",
            f"Relacja modelu: {proposal['action']}",
            "Źródłowe memory IDs: "
            + ",".join(str(item) for item in proposal["source_memory_ids"]),
            f"Docelowe memory ID: {proposal['target_memory_id']}",
            "Evidence memory IDs: "
            + ",".join(str(item) for item in proposal["evidence_memory_ids"]),
            f"Uzasadnienie: {str(proposal['reason']).strip()}",
            f"Confidence: {float(proposal['confidence']):.6f}",
            f"Provider/model: gemini/{model_name}",
            f"Shadow run ID: {shadow_run_id}",
            f"Route operation fingerprint: {route_operation_key_value}",
        ]
    )


def _origin(
    proposal: Mapping[str, Any],
    *,
    preview: Mapping[str, Any],
    outcome: Mapping[str, Any],
    shadow_run_id: int,
    route_reason: str,
    queue_proposal_key: str,
) -> dict[str, Any]:
    request = preview["_request"]
    result = {
        "schema_version": MODEL_QUEUE_ORIGIN_SCHEMA_VERSION,
        "routing_policy_version": ROUTING_POLICY_VERSION,
        "stage": SUPPORTED_STAGE,
        "provider_name": "gemini",
        "provider_kind": "external_model",
        "model_name": preview["request_summary"].get("model_name")
        or outcome["model_name"],
        "model_role": outcome["model_role"],
        "provider_config_version": PROVIDER_CONFIG_VERSION,
        "api_mode": "interactions",
        "shadow_run_id": int(shadow_run_id),
        "route_operation_key": preview["route_operation_key"],
        "route_analysis_fingerprint": preview["route_analysis_fingerprint"],
        "route_preview_hash": preview["route_preview_hash"],
        "request_id": request["request_id"],
        "input_fingerprint": request["input_fingerprint"],
        "response_fingerprint": outcome["validation"]["response_fingerprint"],
        "proposal_signature_fingerprint": proposal_signature_fingerprint(proposal),
        "project_key": request["project_key"],
        "scope_code": request["scope_code"],
        "workspace_id": request["workspace_id"],
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "external_data_policy": EXTERNAL_DATA_POLICY,
        "validation_schema_version": PROVIDER_VALIDATION_SCHEMA_VERSION,
        "evaluation_evidence_fingerprint": preview["evaluation_gate"][
            "evaluation_evidence_fingerprint"
        ],
        "queue_proposal_key": queue_proposal_key,
        "deterministic_duplicate": False,
        "proposal_only": True,
        "created_by": "sandman_v3_route_canary",
        "route_reason": route_reason,
    }
    if set(result) != ORIGIN_FIELDS:
        raise RoutingError("invalid_model_origin_schema")
    return result


def run_model_queue_canary(
    *,
    connection_factory: Callable[[], Any],
    preview: Mapping[str, Any],
    rebuild_preview: Callable[[Any], dict[str, Any]],
    provider: GeminiShadowProvider,
    expected_route_preview_hash: str,
    requested_by: str,
    route_reason: str,
    confirm_queue_write: bool,
    insert_memory: Callable[..., dict[str, Any]],
    create_link: Callable[..., dict[str, Any]],
    utc_now_iso: Callable[[], str],
    notes: str | None = None,
    after_planned_claim: Callable[[], None] | None = None,
) -> dict[str, Any]:
    del notes
    preflight_reasons: list[str] = []
    if not confirm_queue_write:
        preflight_reasons.append("queue_write_confirmation_required")
    if not requested_by or not requested_by.strip():
        preflight_reasons.append("requested_by_required")
    if not route_reason or not route_reason.strip():
        preflight_reasons.append("route_reason_required")
    if route_reason and residual_sensitive_reason_codes(route_reason):
        preflight_reasons.append("unsafe_route_reason")
    if preflight_reasons:
        return _blocked(
            "request_blocked",
            preflight_reasons,
            stage=str(preview.get("stage") or SUPPORTED_STAGE),
            flag_evaluations=preview.get("flag_evaluations"),
            evaluation_gate=preview.get("evaluation_gate"),
        )
    existing_conn = connection_factory()
    try:
        existing = existing_route_result_for_preview(existing_conn, preview=preview)
    finally:
        existing_conn.close()
    if existing is not None:
        return existing
    if preview.get("status") != "preview_ready":
        return public_preview(preview)
    request = preview["_request"]
    if expected_route_preview_hash != preview.get("route_preview_hash"):
        preflight_reasons.append("route_preview_hash_mismatch")
    if preflight_reasons:
        return _blocked(
            "request_blocked",
            preflight_reasons,
            stage=str(preview.get("stage") or SUPPORTED_STAGE),
            flag_evaluations=preview.get("flag_evaluations"),
            evaluation_gate=preview.get("evaluation_gate"),
        )

    run_key = str(preview["route_operation_key"])
    conn = connection_factory()
    try:
        existing = shadow_repository.get_by_run_key(conn, run_key)
        if existing is not None:
            if existing["status"] == "running":
                return _route_existing_result(existing, "already_running")
            return _route_existing_result(existing, "existing_result")
        try:
            created = shadow_repository.create_planned(
                conn,
                {
                    "run_key": run_key,
                    "request_id": request["request_id"],
                    "provider_name": "gemini",
                    "provider_kind": "external_model",
                    "model_name": preview["evaluation_gate"]["model_name"],
                    "model_role": "primary",
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
                    "request_manifest": _manifest(request, preview),
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
        except shadow_repository.ShadowRepositoryError as exc:
            if str(exc) != "duplicate_run_key":
                raise
            conn.rollback()
            existing = shadow_repository.get_by_run_key(conn, run_key)
            if existing is None:
                raise
            return _route_existing_result(existing, "already_running")
        conn.commit()
        run_id = int(created["id"])
    finally:
        conn.close()

    if after_planned_claim is not None:
        after_planned_claim()

    recheck_conn = connection_factory()
    try:
        fresh_pre_network = rebuild_preview(recheck_conn)
        pre_network_reasons: list[str] = []
        if fresh_pre_network.get("status") != "preview_ready":
            pre_network_reasons.extend(
                fresh_pre_network.get("reason_codes") or ["pre_network_preview_blocked"]
            )
        if (
            fresh_pre_network.get("route_analysis_fingerprint")
            != preview.get("route_analysis_fingerprint")
        ):
            pre_network_reasons.append("route_analysis_fingerprint_mismatch")
        if fresh_pre_network.get("route_operation_key") != run_key:
            pre_network_reasons.append("route_operation_key_mismatch")
        if fresh_pre_network.get("route_preview_hash") != expected_route_preview_hash:
            pre_network_reasons.append("route_preview_hash_mismatch")
        if pre_network_reasons:
            _finalize_skipped(
                connection_factory, run_id, reason_codes=pre_network_reasons
            )
            blocked = _blocked(
                "pre_network_blocked",
                pre_network_reasons,
                stage=SUPPORTED_STAGE,
                flag_evaluations=fresh_pre_network.get("flag_evaluations"),
                evaluation_gate=fresh_pre_network.get("evaluation_gate"),
            )
            blocked.update(
                {
                    "schema_version": ROUTE_RESULT_SCHEMA_VERSION,
                    "shadow_run_id": run_id,
                    "route_operation_key": run_key,
                    "route_preview_hash": expected_route_preview_hash,
                    "routing_status": "pre_network_blocked",
                }
            )
            return blocked
        running = shadow_repository.transition(
            recheck_conn,
            run_id,
            expected_status="planned",
            new_status="running",
            fields={"started_at": datetime.now(timezone.utc).isoformat()},
        )
        recheck_conn.commit()
        run_id = int(running["id"])
    finally:
        recheck_conn.close()

    try:
        outcome = provider.analyze(request, model_role="primary")
    except ProviderCallError as exc:
        _finalize_failed(connection_factory, run_id, category=exc.category)
        return {
            "schema_version": ROUTE_RESULT_SCHEMA_VERSION,
            "status": "provider_failed",
            "stage": SUPPORTED_STAGE,
            "provider_name": "gemini",
            "model_name": preview["evaluation_gate"]["model_name"],
            "model_role": "primary",
            "shadow_run_id": run_id,
            "route_operation_key": run_key,
            "route_preview_hash": preview["route_preview_hash"],
            "input_fingerprint": request["input_fingerprint"],
            "validation_status": "provider_failed",
            "routing_status": "fallback_only",
            "summary": {"error_category": exc.category},
            "routed_items": [],
            "deduped_items": [],
            "not_routed_items": [],
            "fallback": _fallback(preview),
            "canary": preview["canary"],
            "flag_evaluations": preview["flag_evaluations"],
            "evaluation_gate": preview["evaluation_gate"],
            "safety": _safety(network_calls=1),
        }

    validation = outcome["validation"]
    if validation["status"] != "accepted":
        category = "validation_rejected"
        _finalize_failed(
            connection_factory,
            run_id,
            category=category,
            provider_fields={
                "execution_mode": "route_canary",
                "validation_reason_codes": list(validation["reason_codes"]),
            },
        )
        return {
            "schema_version": ROUTE_RESULT_SCHEMA_VERSION,
            "status": "response_rejected",
            "stage": SUPPORTED_STAGE,
            "provider_name": "gemini",
            "model_name": outcome["model_name"],
            "model_role": outcome["model_role"],
            "shadow_run_id": run_id,
            "route_operation_key": run_key,
            "route_preview_hash": preview["route_preview_hash"],
            "input_fingerprint": request["input_fingerprint"],
            "validation_status": validation["status"],
            "routing_status": "fallback_only",
            "summary": {"reason_codes": list(validation["reason_codes"])},
            "routed_items": [],
            "deduped_items": [],
            "not_routed_items": [],
            "fallback": _fallback(preview),
            "canary": preview["canary"],
            "flag_evaluations": preview["flag_evaluations"],
            "evaluation_gate": preview["evaluation_gate"],
            "safety": _safety(network_calls=1),
        }

    deterministic_keys = {
        _signature_key(item) for item in preview["_deterministic_proposals"]
    }
    deduped = [
        item
        for item in validation["normalized_proposals"]
        if _signature_key(item) in deterministic_keys
    ]
    route_candidates = [
        item
        for item in validation["normalized_proposals"]
        if item["action"] in ROUTABLE_ACTIONS
        and _signature_key(item) not in deterministic_keys
    ]
    non_routable = [
        item
        for item in validation["normalized_proposals"]
        if item["action"] not in ROUTABLE_ACTIONS
    ]
    route_candidates.sort(key=_selection_key)

    conn = connection_factory()
    routed_items: list[dict[str, Any]] = []
    budget_items: list[dict[str, Any]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh = rebuild_preview(conn)
        if (
            fresh.get("status") != "preview_ready"
            or fresh.get("route_analysis_fingerprint")
            != preview["route_analysis_fingerprint"]
            or fresh.get("route_operation_key") != run_key
        ):
            conn.rollback()
            _finalize_failed(
                connection_factory, run_id, category="stale_route_preview"
            )
            return {
                "schema_version": ROUTE_RESULT_SCHEMA_VERSION,
                "status": "stale_route_preview",
                "stage": SUPPORTED_STAGE,
                "provider_name": "gemini",
                "model_name": outcome["model_name"],
                "model_role": outcome["model_role"],
                "shadow_run_id": run_id,
                "route_operation_key": run_key,
                "route_preview_hash": preview["route_preview_hash"],
                "input_fingerprint": request["input_fingerprint"],
                "validation_status": validation["status"],
                "routing_status": "stale_route_preview",
                "summary": {},
                "routed_items": [],
                "deduped_items": [],
                "not_routed_items": [],
                "fallback": None,
                "canary": fresh.get("canary") or preview["canary"],
                "flag_evaluations": fresh.get("flag_evaluations") or {},
                "evaluation_gate": fresh.get("evaluation_gate") or {},
                "safety": _safety(network_calls=1),
            }
        canary_before = fresh["canary"]["physical_routed_count"]
        remaining = fresh["canary"]["remaining_canary_budget"]
        route_limit = min(MAX_ROUTED_PROPOSALS_PER_RUN, remaining)
        selected = route_candidates[:route_limit]
        budget_items = route_candidates[route_limit:]
        queue_keys: list[str] = []
        for proposal in selected:
            queue_key = _queue_key(
                proposal,
                request=request,
                model_name=outcome["model_name"],
            )
            source = f"sandman_v3:gemini:queue_route:{queue_key}"
            structural_matches = find_existing_structural_model_queue_matches(
                conn,
                proposal=proposal,
                request=request,
                model_name=outcome["model_name"],
            )
            if structural_matches:
                existing = structural_matches[0]
                routed_items.append(
                    {
                        "queue_proposal_key": existing["queue_proposal_key"],
                        "proposal_memory_id": existing["proposal_memory_id"],
                        "decision": "deduped_against_existing_model_queue",
                    }
                )
                continue
            if residual_sensitive_reason_codes(str(proposal["reason"])):
                raise RoutingError("unsafe_proposal_reason")
            proposal_type = PROPOSAL_TYPES[proposal["action"]]
            origin = _origin(
                proposal,
                preview=preview,
                outcome=outcome,
                shadow_run_id=run_id,
                route_reason=route_reason.strip(),
                queue_proposal_key=queue_key,
            )
            content = _proposal_content(
                proposal,
                proposal_type=proposal_type,
                model_name=outcome["model_name"],
                shadow_run_id=run_id,
                route_operation_key_value=run_key,
            )
            memory = insert_memory(
                conn,
                content=content,
                memory_type="consolidation_proposal",
                summary_short=f"Sandman model proposal: {proposal_type}",
                title=f"Sandman model proposal: {proposal_type}",
                source=source,
                importance_score=0.7,
                confidence_score=float(proposal["confidence"]),
                tags=(
                    "sandman-v3,gemini,model-proposal,queue-routing-v2,"
                    "consolidation-proposal,requires-review,proposal-only"
                ),
                state_code="candidate",
                scope_code=request["scope_code"],
                project_key=request["project_key"],
                schema_version=2,
                entry_type="project",
                truth_kind="proposal",
                source_context=canonical_json(origin),
                validation_source="sandman_v3_route_canary",
                memory_v2_status="proposed",
                requires_user_confirmation=True,
                workspace_id=request["workspace_id"],
                ensure_embedding=False,
            )
            proposal_memory_id = int(memory["id"])
            referenced_ids = sorted(
                set(
                    [
                        *proposal["source_memory_ids"],
                        proposal["target_memory_id"],
                        *proposal["evidence_memory_ids"],
                    ]
                )
            )
            for memory_id in referenced_ids:
                create_link(
                    conn,
                    proposal_memory_id,
                    int(memory_id),
                    "related_to",
                    1.0,
                    f"sandman_model_queue:{origin['proposal_signature_fingerprint']}",
                )
            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO memory_consolidation_review_items (
                    proposal_memory_id, status, reviewed_at, reviewed_by,
                    review_note, created_at, updated_at
                ) VALUES (?, 'pending', NULL, NULL, NULL, ?, ?)
                """,
                (proposal_memory_id, now, now),
            )
            queue_keys.append(queue_key)
            routed_items.append(
                {
                    "queue_proposal_key": queue_key,
                    "proposal_memory_id": proposal_memory_id,
                    "review_item_status": "pending",
                    "proposal_type": proposal_type,
                    "action": proposal["action"],
                    "source_memory_ids": list(proposal["source_memory_ids"]),
                    "target_memory_id": proposal["target_memory_id"],
                    "confidence": proposal["confidence"],
                }
            )
        inserted_items = [
            item
            for item in routed_items
            if item.get("decision") != "deduped_against_existing_model_queue"
        ]
        existing_deduped_count = len(routed_items) - len(inserted_items)
        canary_after = canary_before + len(inserted_items)
        routing_status = (
            "abstain"
            if validation["abstain"]
            else "all_deduped"
            if deduped and not routed_items and not budget_items and not non_routable
            else "completed"
        )
        route_summary = {
            "execution_mode": "route_canary",
            "routing_policy_version": ROUTING_POLICY_VERSION,
            "route_operation_key": run_key,
            "route_preview_hash": preview["route_preview_hash"],
            "evaluation_evidence_fingerprint": preview["evaluation_gate"][
                "evaluation_evidence_fingerprint"
            ],
            "route_analysis_fingerprint": preview["route_analysis_fingerprint"],
            "routing_status": routing_status,
            "routed_count": len(inserted_items),
            "deduped_against_deterministic_count": len(deduped),
            "deduped_against_existing_model_queue_count": existing_deduped_count,
            "not_routed_budget_count": len(budget_items),
            "queue_proposal_keys": queue_keys,
            "canary_count_before": canary_before,
            "canary_count_after": canary_after,
            "remaining_canary_budget": max(
                0, MAX_TOTAL_CANARY_PROPOSALS - canary_after
            ),
            "canary_integrity_status": fresh["canary"]["integrity_status"],
            "pre_network_skipped_count": 0,
            "pre_network_skip_reason_codes": [],
            "queue_target": QUEUE_TARGET,
            "auto_apply": MODEL_QUEUE_AUTO_APPLY,
        }
        if set(route_summary) != ROUTE_SUMMARY_FIELDS:
            raise RoutingError("invalid_route_summary_schema")
        proposal_counts = {
            action: sum(
                1
                for item in validation["normalized_proposals"]
                if item["action"] == action
            )
            for action in sorted(
                {item["action"] for item in validation["normalized_proposals"]}
            )
        }
        shadow_repository.transition(
            conn,
            run_id,
            expected_status="running",
            new_status="completed",
            fields={
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "latency_ms": outcome["latency_ms"],
                "input_tokens": outcome["usage"]["input_tokens"],
                "output_tokens": outcome["usage"]["output_tokens"],
                "total_tokens": outcome["usage"]["total_tokens"],
                "estimated_cost_usd": outcome["estimated_cost_usd"],
                "retry_count": outcome["retry_count"],
                "validation_status": validation["status"],
                "validation_reason_codes_json": validation["reason_codes"],
                "proposal_counts_json": proposal_counts,
                "abstain": int(validation["abstain"]),
                "response_fingerprint": validation["response_fingerprint"],
                "provider_metadata_json": route_summary,
            },
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        category = (
            exc.reason_codes[0]
            if isinstance(exc, RoutingError)
            else "queue_transaction_failed"
        )
        _finalize_failed(connection_factory, run_id, category=category)
        return {
            "schema_version": ROUTE_RESULT_SCHEMA_VERSION,
            "status": "queue_write_failed",
            "stage": SUPPORTED_STAGE,
            "provider_name": "gemini",
            "model_name": outcome["model_name"],
            "model_role": outcome["model_role"],
            "shadow_run_id": run_id,
            "route_operation_key": run_key,
            "route_preview_hash": preview["route_preview_hash"],
            "input_fingerprint": request["input_fingerprint"],
            "validation_status": validation["status"],
            "routing_status": "failed",
            "summary": {"error_category": category},
            "routed_items": [],
            "deduped_items": [],
            "not_routed_items": [],
            "fallback": None,
            "canary": preview["canary"],
            "flag_evaluations": preview["flag_evaluations"],
            "evaluation_gate": preview["evaluation_gate"],
            "safety": _safety(network_calls=1),
        }
    finally:
        conn.close()

    return {
        "schema_version": ROUTE_RESULT_SCHEMA_VERSION,
        "status": "completed",
        "stage": SUPPORTED_STAGE,
        "provider_name": "gemini",
        "model_name": outcome["model_name"],
        "model_role": outcome["model_role"],
        "shadow_run_id": run_id,
        "route_operation_key": run_key,
        "route_preview_hash": preview["route_preview_hash"],
        "input_fingerprint": request["input_fingerprint"],
        "validation_status": validation["status"],
        "routing_status": route_summary["routing_status"],
        "summary": route_summary,
        "routed_items": inserted_items,
        "deduped_items": [
            {
                "proposal_id": item["proposal_id"],
                "action": item["action"],
                "decision": "deduped_against_deterministic",
            }
            for item in deduped
        ],
        "not_routed_items": [
            {
                "proposal_id": item["proposal_id"],
                "action": item["action"],
                "decision": "not_routed_budget_limit",
            }
            for item in budget_items
        ]
        + [
            {
                "proposal_memory_id": item["proposal_memory_id"],
                "decision": "deduped_against_existing_model_queue",
            }
            for item in routed_items
            if item.get("decision") == "deduped_against_existing_model_queue"
        ]
        + [
            {
                "proposal_id": item["proposal_id"],
                "action": item["action"],
                "decision": "action_not_routable",
            }
            for item in non_routable
        ],
        "fallback": None,
        "canary": {
            "current_routed_count": route_summary["canary_count_after"],
            "remaining_canary_budget": route_summary[
                "remaining_canary_budget"
            ],
            "max_per_run": MAX_ROUTED_PROPOSALS_PER_RUN,
            "max_total": MAX_TOTAL_CANARY_PROPOSALS,
            "paused": route_summary["remaining_canary_budget"] <= 0,
        },
        "flag_evaluations": preview["flag_evaluations"],
        "evaluation_gate": preview["evaluation_gate"],
        "safety": _safety(network_calls=1, queue_writes=len(inserted_items)),
    }
