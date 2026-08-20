from __future__ import annotations

import copy
import json
import math
import os
import re
import sqlite3
from collections import Counter, defaultdict
from importlib.resources import files
from typing import Any, Callable, Mapping

from app import db_migrations
from mapi_core.sandman.contracts import (
    ContractError,
    PROPOSAL_ACTIONS,
    canonical_fingerprint,
    parse_provider_response,
    strict_json_loads,
)
from app.sandman.providers.deterministic import DeterministicProvider
from mapi_core.sandman.providers.gemini import (
    MODEL_ALLOWLIST,
    PRIMARY_MODEL,
    PROVIDER_CONFIG_VERSION,
)
from app.sandman.router import preview_provider_request_payload
from mapi_core.sandman.validator import validate_provider_response


CORPUS_SCHEMA_VERSION = "sandman_semantic_evaluation_corpus.v1"
CASE_SCHEMA_VERSION = "sandman_semantic_evaluation_case.v1"
PREDICTION_BUNDLE_SCHEMA_VERSION = "sandman_semantic_prediction_bundle.v1"
EVALUATION_REPORT_SCHEMA_VERSION = "sandman_semantic_evaluation_report.v1"
ROLLOUT_POLICY_VERSION = "sandman_semantic_rollout_policy.v1"
CORPUS_VERSION = "semantic-v1-2026-07-16"

EVALUATION_KINDS = frozenset(
    {
        "deterministic_baseline",
        "fixture_replay",
        "operator_supplied_gemini_replay",
    }
)
RECOMMENDATIONS = frozenset(
    {
        "blocked_safety",
        "blocked_quality",
        "insufficient_evidence",
        "eligible_for_operator_review",
    }
)
EXPECTED_DISTRIBUTION = {
    "exact_duplicate": 10,
    "near_duplicate": 10,
    "correction": 10,
    "refinement": 10,
    "replacement": 10,
    "reinforcement": 10,
    "unresolved_contradiction": 8,
    "dream_vs_fact": 8,
    "cross_project_trap": 6,
    "cross_scope_trap": 6,
    "sensitive_content": 6,
    "weak_evidence_abstain": 6,
}
ROLLOUT_POLICY = {
    "schema_version": ROLLOUT_POLICY_VERSION,
    "duplicate_precision_min": 0.97,
    "supersession_precision_min": 0.95,
    "false_supersession_rate_max": 0.02,
    "evaluated_case_count_required": 100,
    "missing_case_count_required": 0,
    "extra_case_count_required": 0,
    "duplicate_case_id_count_required": 0,
    "accepted_duplicate_of_min": 15,
    "accepted_supersedes_min": 20,
}
CORPUS_FIELDS = frozenset(
    {"schema_version", "corpus_version", "synthetic", "cases", "corpus_fingerprint"}
)
CASE_FIELDS = frozenset(
    {
        "schema_version",
        "case_id",
        "case_type",
        "title",
        "label_source",
        "label_confidence",
        "synthetic",
        "expected_stage",
        "expected_outcome",
        "semantic_kind",
        "project_key",
        "scope_code",
        "workspace_id",
        "requested_memory_ids",
        "allowed_actions",
        "proposal_budget",
        "memories",
        "links",
        "acceptable_proposals",
        "forbidden_actions",
        "allowed_block_reason_codes",
        "notes_codes",
    }
)
MEMORY_FIELDS = frozenset(
    {
        "id",
        "content",
        "summary_short",
        "memory_type",
        "entry_type",
        "truth_kind",
        "state_code",
        "memory_v2_status",
        "activity_state",
        "project_key",
        "scope_code",
        "workspace_id",
        "visibility_scope",
        "created_at",
        "updated_at",
        "tags",
        "never_store",
        "supersedes_memory_id",
        "superseded_by_memory_id",
    }
)
LINK_FIELDS = frozenset({"from_memory_id", "to_memory_id", "relation_type"})
SIGNATURE_FIELDS = frozenset(
    {"action", "source_memory_ids", "target_memory_id", "evidence_memory_ids"}
)
BUNDLE_FIELDS = frozenset(
    {
        "schema_version",
        "evaluation_kind",
        "corpus_version",
        "corpus_fingerprint",
        "provider_name",
        "model_name",
        "provider_config_version",
        "synthetic_bundle",
        "generated_at",
        "attestation",
        "cases",
    }
)
ATTESTATION_FIELDS = frozenset(
    {
        "performed_by",
        "source_kind",
        "store_false_confirmed",
        "previous_interaction_id_used",
        "background_used",
        "tools_used",
        "file_api_used",
        "grounding_used",
        "real_network_call_count",
    }
)
BUNDLE_CASE_FIELDS = frozenset(
    {
        "case_id",
        "response",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "estimated_cost_usd",
        "operator_decision",
    }
)
REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "evaluation_kind",
        "corpus",
        "provider",
        "attestation_summary",
        "completeness",
        "metrics",
        "safety_gate",
        "quality_gate",
        "sufficiency_gate",
        "rollout_recommendation",
        "case_type_summary",
        "case_results",
        "safety",
    }
)
PROTECTED_TABLES = (
    "memories",
    "memory_links",
    "memory_events",
    "sleep_runs",
    "sleep_run_actions",
    "timeline_events",
    "memory_capture_review_items",
    "memory_retention_review_items",
    "memory_consolidation_review_items",
    "sandman_semantic_shadow_runs",
    "feature_flags",
)
_CASE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REASON_CODE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_FIXED_TIMESTAMP = re.compile(r"^2026-01-(?:0[1-9]|[12][0-9]|3[01])T\d{2}:\d{2}:\d{2}Z$")
_REAL_LOOKING_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_REAL_LOOKING_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){7,15}(?!\d)")
_SYSTEM_PATH = re.compile(r"(?:[A-Za-z]:\\|/home/|/Users/)", re.IGNORECASE)
_ADDRESS_MARKER = re.compile(
    r"\b(?:home address|private address|adres domowy|adres zamieszkania)\b",
    re.IGNORECASE,
)
_API_KEY_PREFIX = re.compile(r"\b(?:AIza[0-9A-Za-z_-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b")
_SENSITIVE_MARKER = re.compile(
    r"SYNTHETIC_ONLY|-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----",
    re.IGNORECASE,
)


class EvaluationError(ValueError):
    def __init__(self, *reason_codes: str):
        self.reason_codes = sorted(set(reason_codes or ("evaluation_error",)))
        super().__init__(",".join(self.reason_codes))


def _strict_evaluation_json_loads(value: str, *, invalid_code: str) -> Any:
    try:
        return strict_json_loads(value, invalid_code=invalid_code)
    except ContractError as exc:
        raise EvaluationError(*exc.reason_codes) from exc


def _strict_fields(value: Mapping[str, Any], expected: frozenset[str], code: str) -> None:
    if set(value) != expected:
        raise EvaluationError(code)


def _canonical_ids(value: Any, *, allow_empty: bool = False) -> list[int]:
    if (
        not isinstance(value, list)
        or any(isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 9 for item in value)
    ):
        raise EvaluationError("invalid_memory_ids")
    normalized = sorted(set(value))
    if value != normalized or (not allow_empty and not normalized):
        raise EvaluationError("invalid_memory_ids")
    return normalized


def _canonical_codes(value: Any, *, allow_empty: bool = True) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not _REASON_CODE.fullmatch(item) for item in value)
    ):
        raise EvaluationError("invalid_reason_codes")
    normalized = sorted(set(value))
    if value != normalized or (not allow_empty and not normalized):
        raise EvaluationError("invalid_reason_codes")
    return normalized


def _normalize_signature(value: Mapping[str, Any]) -> tuple[str, tuple[int, ...], int, tuple[int, ...]]:
    _strict_fields(value, SIGNATURE_FIELDS, "invalid_proposal_signature")
    action = value["action"]
    if action not in PROPOSAL_ACTIONS:
        raise EvaluationError("invalid_proposal_signature")
    sources = _canonical_ids(value["source_memory_ids"])
    evidence = _canonical_ids(value["evidence_memory_ids"])
    target = value["target_memory_id"]
    if (
        isinstance(target, bool)
        or not isinstance(target, int)
        or not 1 <= target <= 9
        or target in sources
        or not set([*sources, target]).issubset(evidence)
    ):
        raise EvaluationError("invalid_proposal_signature")
    return action, tuple(sources), target, tuple(evidence)


def _semantic_corpus_payload(corpus: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(corpus[key]) for key in sorted(CORPUS_FIELDS - {"corpus_fingerprint"})}


def _privacy_audit(corpus: Mapping[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    forbidden_project_tokens = {"mapi", "demo-project", "sample-project"}
    configured_secrets = [
        value
        for key, value in os.environ.items()
        if any(token in key.upper() for token in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
        and isinstance(value, str)
        and len(value) >= 8
    ]
    for case in corpus["cases"]:
        case_id = case["case_id"]
        for memory in case["memories"]:
            content = str(memory["content"])
            lowered = content.casefold()
            rules: list[str] = []
            if memory["project_key"] not in {"eval-project-a", "eval-project-b"}:
                rules.append("project_namespace")
            if memory["workspace_id"] not in {101, 202}:
                rules.append("workspace_namespace")
            if any(token in lowered for token in forbidden_project_tokens):
                rules.append("real_project_token")
            if _REAL_LOOKING_EMAIL.search(content):
                rules.append("real_looking_email")
            if _REAL_LOOKING_PHONE.search(content):
                rules.append("real_looking_phone")
            if _SYSTEM_PATH.search(content):
                rules.append("system_path")
            if _ADDRESS_MARKER.search(content):
                rules.append("real_looking_address")
            if _API_KEY_PREFIX.search(content):
                rules.append("api_key_prefix")
            if any(secret in content for secret in configured_secrets):
                rules.append("configured_secret_value")
            marker_found = bool(_SENSITIVE_MARKER.search(content))
            if marker_found and case["case_type"] != "sensitive_content":
                rules.append("sensitive_marker_outside_fixture")
            for rule in sorted(set(rules)):
                findings.append({"case_id": case_id, "rule_id": rule})
    return {
        "status": "passed" if not findings else "failed",
        "finding_count": len(findings),
        "findings": findings,
    }


def validate_semantic_evaluation_corpus(corpus: Any) -> dict[str, Any]:
    if not isinstance(corpus, Mapping):
        raise EvaluationError("invalid_corpus_schema")
    corpus = copy.deepcopy(dict(corpus))
    _strict_fields(corpus, CORPUS_FIELDS, "invalid_corpus_schema")
    if (
        corpus["schema_version"] != CORPUS_SCHEMA_VERSION
        or corpus["corpus_version"] != CORPUS_VERSION
        or corpus["synthetic"] is not True
        or not isinstance(corpus["cases"], list)
        or len(corpus["cases"]) != 100
    ):
        raise EvaluationError("invalid_corpus_schema")

    case_ids: list[str] = []
    distribution: Counter[str] = Counter()
    for raw_case in corpus["cases"]:
        if not isinstance(raw_case, Mapping):
            raise EvaluationError("invalid_case_schema")
        case = dict(raw_case)
        _strict_fields(case, CASE_FIELDS, "invalid_case_schema")
        if (
            case["schema_version"] != CASE_SCHEMA_VERSION
            or case["case_type"] not in EXPECTED_DISTRIBUTION
            or not isinstance(case["case_id"], str)
            or not _CASE_ID.fullmatch(case["case_id"])
            or case["label_source"] != "human_spec_v1"
            or case["label_confidence"] != 1.0
            or case["synthetic"] is not True
            or case["expected_stage"] not in {"request_ready", "request_blocked"}
            or case["expected_outcome"] not in {"proposal", "abstain", "request_blocked"}
            or case["project_key"] not in {"eval-project-a", "eval-project-b"}
            or case["scope_code"] != "project"
            or case["workspace_id"] not in {101, 202}
            or isinstance(case["proposal_budget"], bool)
            or not isinstance(case["proposal_budget"], int)
            or not 1 <= case["proposal_budget"] <= 8
        ):
            raise EvaluationError("invalid_case_schema")
        requested = _canonical_ids(case["requested_memory_ids"])
        if (
            not isinstance(case["allowed_actions"], list)
            or case["allowed_actions"] != sorted(set(case["allowed_actions"]))
            or any(action not in PROPOSAL_ACTIONS for action in case["allowed_actions"])
        ):
            raise EvaluationError("invalid_case_actions")
        if (
            not isinstance(case["forbidden_actions"], list)
            or case["forbidden_actions"] != sorted(set(case["forbidden_actions"]))
            or any(action not in PROPOSAL_ACTIONS for action in case["forbidden_actions"])
        ):
            raise EvaluationError("invalid_case_actions")
        _canonical_codes(case["allowed_block_reason_codes"])
        _canonical_codes(case["notes_codes"])

        if not isinstance(case["memories"], list) or len(case["memories"]) != len(requested):
            raise EvaluationError("invalid_case_memories")
        memory_ids: list[int] = []
        boundaries: set[tuple[Any, Any, Any]] = set()
        for raw_memory in case["memories"]:
            if not isinstance(raw_memory, Mapping):
                raise EvaluationError("invalid_case_memories")
            memory = dict(raw_memory)
            _strict_fields(memory, MEMORY_FIELDS, "invalid_case_memories")
            memory_id = memory["id"]
            if isinstance(memory_id, bool) or not isinstance(memory_id, int) or not 1 <= memory_id <= 9:
                raise EvaluationError("invalid_case_memories")
            if (
                not isinstance(memory["content"], str)
                or not memory["content"]
                or memory["project_key"] not in {"eval-project-a", "eval-project-b"}
                or memory["workspace_id"] not in {101, 202}
                or not _FIXED_TIMESTAMP.fullmatch(memory["created_at"])
                or not _FIXED_TIMESTAMP.fullmatch(memory["updated_at"])
                or not isinstance(memory["never_store"], bool)
            ):
                raise EvaluationError("invalid_case_memories")
            memory_ids.append(memory_id)
            boundaries.add((memory["project_key"], memory["scope_code"], memory["workspace_id"]))
        if sorted(memory_ids) != requested or len(memory_ids) != len(set(memory_ids)):
            raise EvaluationError("invalid_case_memories")
        if case["case_type"] not in {"cross_project_trap", "cross_scope_trap"} and boundaries != {
            (case["project_key"], case["scope_code"], case["workspace_id"])
        }:
            raise EvaluationError("invalid_case_boundary")

        if not isinstance(case["links"], list):
            raise EvaluationError("invalid_case_links")
        for raw_link in case["links"]:
            if not isinstance(raw_link, Mapping):
                raise EvaluationError("invalid_case_links")
            link = dict(raw_link)
            _strict_fields(link, LINK_FIELDS, "invalid_case_links")
            if (
                link["from_memory_id"] not in requested
                or link["to_memory_id"] not in requested
                or link["from_memory_id"] == link["to_memory_id"]
                or link["relation_type"] not in {"contradicts", "reinforces", "related_to"}
            ):
                raise EvaluationError("invalid_case_links")
        if not isinstance(case["acceptable_proposals"], list):
            raise EvaluationError("invalid_proposal_signature")
        signatures = [_normalize_signature(item) for item in case["acceptable_proposals"]]
        if len(signatures) != len(set(signatures)) or any(
            not set([*signature[1], signature[2], *signature[3]]).issubset(requested)
            for signature in signatures
        ):
            raise EvaluationError("invalid_proposal_signature")
        if len(signatures) > case["proposal_budget"]:
            raise EvaluationError("proposal_budget_overflow")
        case_ids.append(case["case_id"])
        distribution[case["case_type"]] += 1

    if case_ids != sorted(case_ids) or len(case_ids) != len(set(case_ids)):
        raise EvaluationError("invalid_case_ids")
    if dict(distribution) != EXPECTED_DISTRIBUTION:
        raise EvaluationError("invalid_corpus_distribution")
    expected_fingerprint = canonical_fingerprint(_semantic_corpus_payload(corpus))
    if corpus["corpus_fingerprint"] != expected_fingerprint:
        raise EvaluationError("corpus_fingerprint_mismatch")
    privacy = _privacy_audit(corpus)
    if privacy["status"] != "passed":
        raise EvaluationError("corpus_privacy_audit_failed")
    return corpus


def load_semantic_evaluation_corpus() -> dict[str, Any]:
    resource = files("app.sandman.corpora").joinpath("semantic_evaluation_v1.json")
    try:
        corpus = _strict_evaluation_json_loads(
            resource.read_text(encoding="utf-8"),
            invalid_code="invalid_corpus_json",
        )
    except OSError as exc:
        raise EvaluationError("invalid_corpus_json") from exc
    return validate_semantic_evaluation_corpus(corpus)


def semantic_evaluation_corpus_manifest(
    *,
    include_case_ids: bool = False,
    include_debug: bool = False,
) -> dict[str, Any]:
    corpus = load_semantic_evaluation_corpus()
    privacy = _privacy_audit(corpus)
    result = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "corpus_version": corpus["corpus_version"],
        "corpus_fingerprint": corpus["corpus_fingerprint"],
        "synthetic": True,
        "case_count": len(corpus["cases"]),
        "distribution": dict(EXPECTED_DISTRIBUTION),
        "privacy_audit": {
            "status": privacy["status"],
            "finding_count": privacy["finding_count"],
        },
        "rollout_policy": copy.deepcopy(ROLLOUT_POLICY),
        "case_ids": [case["case_id"] for case in corpus["cases"]] if include_case_ids else [],
        "safety": {
            "contains_case_content": False,
            "network_calls": 0,
            "database_writes": 0,
            "routing_enabled": False,
        },
    }
    if include_debug:
        result["debug"] = {
            "corpus_rule_ids": [
                "exact_schema",
                "fixed_namespace",
                "fixed_timestamps",
                "synthetic_only",
                "canonical_fingerprint",
            ],
            "privacy_rule_ids": [
                "project_namespace",
                "workspace_namespace",
                "real_project_token",
                "real_looking_email",
                "real_looking_phone",
                "system_path",
                "sensitive_marker_outside_fixture",
            ],
        }
    return result


def _default_connection_factory() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db_migrations.apply_all_migrations(conn)
    return conn


def _insert_case(conn: sqlite3.Connection, case: Mapping[str, Any]) -> None:
    memory_columns = [
        "id",
        "content",
        "summary_short",
        "memory_type",
        "entry_type",
        "truth_kind",
        "state_code",
        "memory_v2_status",
        "activity_state",
        "project_key",
        "scope_code",
        "workspace_id",
        "visibility_scope",
        "created_at",
        "updated_at",
        "tags",
        "supersedes_memory_id",
        "superseded_by_memory_id",
    ]
    for memory in case["memories"]:
        conn.execute(
            f"INSERT INTO memories({','.join(memory_columns)}) VALUES({','.join('?' for _ in memory_columns)})",
            [memory[column] for column in memory_columns],
        )
    for link in case["links"]:
        conn.execute(
            """
            INSERT INTO memory_links(
                from_memory_id,to_memory_id,relation_type,weight,origin,workspace_id,visibility_scope
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                link["from_memory_id"],
                link["to_memory_id"],
                link["relation_type"],
                1.0,
                "synthetic_evaluation",
                case["workspace_id"],
                "project",
            ),
        )
    conn.commit()


def _prepare_case_request(
    case: Mapping[str, Any],
    *,
    provider_name: str,
    connection_factory: Callable[[], sqlite3.Connection],
    request_builder: Callable[..., dict[str, Any]],
    include_debug: bool = False,
) -> dict[str, Any]:
    conn = connection_factory()
    try:
        _insert_case(conn, case)
        return request_builder(
            conn,
            project_key=case["project_key"],
            scope_code=case["scope_code"],
            memory_ids_json=list(case["requested_memory_ids"]),
            allowed_actions_json=list(case["allowed_actions"]),
            provider_name=provider_name,
            proposal_budget=case["proposal_budget"],
            include_debug=include_debug,
            feature_flag={
                "flag_key": "sandman_provider_v3_enabled",
                "is_enabled": 1,
                "is_implicit_default": False,
            },
            feature_flag_evaluation={
                "enabled": True,
                "reason": "synthetic_evaluation_fixture",
                "rollout_mode": "all",
            },
            request_id_factory=lambda: f"eval-{CORPUS_VERSION}-{case['case_id']}",
        )
    finally:
        conn.close()


def _safe_number(value: Any, *, integer: bool = False) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
        raise EvaluationError("invalid_usage_metadata")
    return int(value) if integer else float(value)


def _parse_bundle(value: Any, *, evaluation_kind: str, corpus: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError("prediction_bundle_required")
    bundle = _strict_evaluation_json_loads(
        value,
        invalid_code="invalid_prediction_bundle_json",
    )
    if not isinstance(bundle, Mapping):
        raise EvaluationError("invalid_prediction_bundle_schema")
    bundle = dict(bundle)
    _strict_fields(bundle, BUNDLE_FIELDS, "invalid_prediction_bundle_schema")
    if (
        bundle["schema_version"] != PREDICTION_BUNDLE_SCHEMA_VERSION
        or bundle["evaluation_kind"] != evaluation_kind
        or bundle["corpus_version"] != corpus["corpus_version"]
        or bundle["corpus_fingerprint"] != corpus["corpus_fingerprint"]
        or not isinstance(bundle["generated_at"], str)
        or not bundle["generated_at"].strip()
        or not isinstance(bundle["cases"], list)
    ):
        raise EvaluationError("invalid_prediction_bundle_contract")
    if not isinstance(bundle["attestation"], Mapping):
        raise EvaluationError("invalid_attestation")
    attestation = dict(bundle["attestation"])
    _strict_fields(attestation, ATTESTATION_FIELDS, "invalid_attestation")
    if (
        not isinstance(attestation["performed_by"], str)
        or not attestation["performed_by"].strip()
        or any(
            not isinstance(attestation[field], bool)
            for field in (
                "store_false_confirmed",
                "previous_interaction_id_used",
                "background_used",
                "tools_used",
                "file_api_used",
                "grounding_used",
            )
        )
        or isinstance(attestation["real_network_call_count"], bool)
        or not isinstance(attestation["real_network_call_count"], int)
        or attestation["real_network_call_count"] < 0
    ):
        raise EvaluationError("invalid_attestation")
    if evaluation_kind == "operator_supplied_gemini_replay":
        if (
            bundle["provider_name"] != "gemini"
            or bundle["model_name"] not in MODEL_ALLOWLIST
            or bundle["provider_config_version"] != PROVIDER_CONFIG_VERSION
            or bundle["synthetic_bundle"] is not False
            or attestation["source_kind"] != "manual_synthetic_corpus_shadow_run"
            or attestation["store_false_confirmed"] is not True
            or any(
                attestation[field] is not False
                for field in (
                    "previous_interaction_id_used",
                    "background_used",
                    "tools_used",
                    "file_api_used",
                    "grounding_used",
                )
            )
        ):
            raise EvaluationError("invalid_operator_replay_attestation")
    elif evaluation_kind == "fixture_replay":
        if (
            bundle["synthetic_bundle"] is not True
            or attestation["source_kind"] != "fixture_replay"
            or attestation["real_network_call_count"] != 0
        ):
            raise EvaluationError("invalid_fixture_replay_attestation")
    normalized_cases: list[dict[str, Any]] = []
    for raw_item in bundle["cases"]:
        if not isinstance(raw_item, Mapping):
            raise EvaluationError("invalid_prediction_case_schema")
        item = dict(raw_item)
        _strict_fields(item, BUNDLE_CASE_FIELDS, "invalid_prediction_case_schema")
        if (
            not isinstance(item["case_id"], str)
            or item["operator_decision"] not in {"accepted", "rejected", "unreviewed"}
        ):
            raise EvaluationError("invalid_prediction_case_schema")
        item["latency_ms"] = _safe_number(item["latency_ms"])
        item["input_tokens"] = _safe_number(item["input_tokens"], integer=True)
        item["output_tokens"] = _safe_number(item["output_tokens"], integer=True)
        item["total_tokens"] = _safe_number(item["total_tokens"], integer=True)
        item["estimated_cost_usd"] = _safe_number(item["estimated_cost_usd"])
        if (
            item["total_tokens"] is not None
            and item["input_tokens"] is not None
            and item["output_tokens"] is not None
            and item["total_tokens"] != item["input_tokens"] + item["output_tokens"]
        ):
            raise EvaluationError("invalid_usage_metadata")
        normalized_cases.append(item)
    if evaluation_kind == "operator_supplied_gemini_replay":
        ready_ids = {
            case["case_id"]
            for case in corpus["cases"]
            if case["expected_stage"] == "request_ready"
        }
        submitted_ready_count = sum(
            item["case_id"] in ready_ids for item in normalized_cases
        )
        if attestation["real_network_call_count"] != submitted_ready_count:
            raise EvaluationError("invalid_operator_replay_attestation")
    bundle["attestation"] = attestation
    bundle["cases"] = normalized_cases
    return bundle


def _proposal_signature(proposal: Mapping[str, Any]) -> tuple[str, tuple[int, ...], int, tuple[int, ...]]:
    return (
        str(proposal["action"]),
        tuple(sorted(set(int(item) for item in proposal["source_memory_ids"]))),
        int(proposal["target_memory_id"]),
        tuple(sorted(set(int(item) for item in proposal["evidence_memory_ids"]))),
    )


def _ratio(numerator: int, denominator: int, reason: str) -> tuple[float | None, str | None]:
    if denominator == 0:
        return None, reason
    return numerator / denominator, None


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _empty_safety() -> dict[str, Any]:
    return {
        "cross_project_accepted_errors": 0,
        "cross_scope_accepted_errors": 0,
        "sensitive_leakage": 0,
        "invalid_accepted_schema": 0,
        "request_block_bypass": 0,
        "unknown_memory_id_accepted": 0,
        "forbidden_action_accepted": 0,
        "dream_fact_boundary_errors": 0,
        "automatic_model_mutations": 0,
        "queue_mutations": 0,
        "memory_mutations": 0,
        "sleep_mutations": 0,
        "timeline_mutations": 0,
        "network_calls_by_evaluator": 0,
        "proposals_remain_proposal_only_percent": 100.0,
    }


def _base_report(
    *,
    status: str,
    evaluation_kind: str,
    corpus: Mapping[str, Any],
    provider: Mapping[str, Any],
    attestation: Mapping[str, Any],
    recommendation: str,
    reason_codes: list[str],
) -> dict[str, Any]:
    report = {
        "schema_version": EVALUATION_REPORT_SCHEMA_VERSION,
        "status": status,
        "evaluation_kind": evaluation_kind,
        "corpus": {
            "version": corpus["corpus_version"],
            "fingerprint": corpus["corpus_fingerprint"],
            "case_count": len(corpus["cases"]),
            "distribution": dict(EXPECTED_DISTRIBUTION),
            "synthetic": True,
        },
        "provider": dict(provider),
        "attestation_summary": dict(attestation),
        "completeness": {},
        "metrics": {},
        "safety_gate": {"passed": False, "reason_codes": list(reason_codes)},
        "quality_gate": {"passed": False, "reason_codes": list(reason_codes), "policy": copy.deepcopy(ROLLOUT_POLICY)},
        "sufficiency_gate": {"passed": False, "reason_codes": list(reason_codes), "policy": copy.deepcopy(ROLLOUT_POLICY)},
        "rollout_recommendation": {
            "recommendation": recommendation,
            "rollout_eligible": False,
            "reason_codes": sorted(set(reason_codes)),
            "routing_enabled": False,
            "queue_writes_performed": 0,
            "auto_apply": False,
            "requires_explicit_user_decision_for_b09": True,
        },
        "case_type_summary": {},
        "case_results": [],
        "safety": _empty_safety(),
    }
    assert set(report) == REPORT_FIELDS
    return report


def evaluate_semantic_provider_bundle(
    *,
    evaluation_kind: str,
    prediction_bundle_json: str | None = None,
    include_case_results: bool = False,
    include_debug: bool = False,
    connection_factory: Callable[[], sqlite3.Connection] | None = None,
    provider: DeterministicProvider | None = None,
    request_builder: Callable[..., dict[str, Any]] = preview_provider_request_payload,
    response_validator: Callable[..., dict[str, Any]] = validate_provider_response,
) -> dict[str, Any]:
    if evaluation_kind not in EVALUATION_KINDS:
        try:
            corpus = load_semantic_evaluation_corpus()
        except EvaluationError:
            corpus = {
                "corpus_version": CORPUS_VERSION,
                "corpus_fingerprint": "",
                "cases": [],
            }
        return _base_report(
            status="rejected_bundle",
            evaluation_kind=str(evaluation_kind),
            corpus=corpus,
            provider={},
            attestation={},
            recommendation="insufficient_evidence",
            reason_codes=["unknown_evaluation_kind"],
        )
    try:
        corpus = load_semantic_evaluation_corpus()
    except EvaluationError as exc:
        fallback = {"corpus_version": CORPUS_VERSION, "corpus_fingerprint": "", "cases": []}
        return _base_report(
            status="invalid_corpus",
            evaluation_kind=evaluation_kind,
            corpus=fallback,
            provider={},
            attestation={},
            recommendation="blocked_safety",
            reason_codes=exc.reason_codes,
        )

    deterministic = evaluation_kind == "deterministic_baseline"
    if deterministic and prediction_bundle_json is not None and str(prediction_bundle_json).strip():
        return _base_report(
            status="rejected_bundle",
            evaluation_kind=evaluation_kind,
            corpus=corpus,
            provider={"provider_name": "deterministic"},
            attestation={"source_kind": "local_deterministic_baseline", "operator_attestation": False},
            recommendation="insufficient_evidence",
            reason_codes=["prediction_bundle_forbidden"],
        )

    bundle: dict[str, Any] | None = None
    if not deterministic:
        try:
            bundle = _parse_bundle(
                prediction_bundle_json,
                evaluation_kind=evaluation_kind,
                corpus=corpus,
            )
        except EvaluationError as exc:
            return _base_report(
                status="rejected_bundle",
                evaluation_kind=evaluation_kind,
                corpus=corpus,
                provider={},
                attestation={},
                recommendation="insufficient_evidence",
                reason_codes=exc.reason_codes,
            )

    provider_view = (
        {
            "provider_name": "deterministic",
            "model_name": None,
            "provider_config_version": None,
        }
        if deterministic
        else {
            "provider_name": bundle["provider_name"],
            "model_name": bundle["model_name"],
            "provider_config_version": bundle["provider_config_version"],
        }
    )
    attestation_summary = (
        {
            "source_kind": "local_deterministic_baseline",
            "operator_attestation": False,
            "synthetic_bundle": True,
            "real_network_call_count_attested": 0,
        }
        if deterministic
        else {
            "source_kind": bundle["attestation"]["source_kind"],
            "performed_by_present": bool(bundle["attestation"]["performed_by"].strip()),
            "operator_attestation": evaluation_kind == "operator_supplied_gemini_replay",
            "attestation_is_cryptographic": False,
            "synthetic_bundle": bool(bundle["synthetic_bundle"]),
            "real_network_call_count_attested": bundle["attestation"]["real_network_call_count"],
        }
    )
    connection_factory = connection_factory or _default_connection_factory
    provider = provider or DeterministicProvider()
    bundle_items = list(bundle["cases"]) if bundle else []
    bundle_ids = [item["case_id"] for item in bundle_items]
    duplicate_bundle_ids = len(bundle_ids) - len(set(bundle_ids))
    bundle_by_id: dict[str, dict[str, Any]] = {}
    for item in bundle_items:
        bundle_by_id.setdefault(item["case_id"], item)
    corpus_ids = {case["case_id"] for case in corpus["cases"]}
    ready_ids = {case["case_id"] for case in corpus["cases"] if case["expected_stage"] == "request_ready"}
    extra_ids = sorted(set(bundle_ids) - ready_ids) if bundle else []
    missing_ids = sorted(ready_ids - set(bundle_ids)) if bundle else []

    action_counts = {
        action: {"true_positive": 0, "false_positive": 0, "false_negative": 0}
        for action in sorted(PROPOSAL_ACTIONS)
    }
    safety = _empty_safety()
    case_results: list[dict[str, Any]] = []
    case_type_stats: dict[str, Counter[str]] = defaultdict(Counter)
    semantic_stats: dict[str, Counter[str]] = defaultdict(Counter)
    accepted_response_count = rejected_response_count = 0
    abstain_count = proposal_count = 0
    true_positive_count = false_positive_count = false_negative_count = 0
    exact_outcome_matches = 0
    request_blocked_observed = 0
    non_supersession_ready = 0
    false_supersession_cases = 0
    accepted_action_counts: Counter[str] = Counter()
    operator_decisions: Counter[str] = Counter()
    latencies: list[float] = []
    costs: list[float] = []
    token_totals = {"input": 0, "output": 0, "total": 0}
    token_available = {"input": True, "output": True, "total": True}
    latency_available = True
    cost_available = True
    provider_invocation_count = 0

    for case in corpus["cases"]:
        case_id = case["case_id"]
        expected_signatures = {
            _normalize_signature(item) for item in case["acceptable_proposals"]
        }
        expected_stage = case["expected_stage"]
        observed_stage = "request_blocked"
        observed_outcome = "request_blocked"
        matched = fp_count = fn_count = 0
        reason_codes: list[str] = []
        preview = _prepare_case_request(
            case,
            provider_name="deterministic" if deterministic else "gemini",
            connection_factory=connection_factory,
            request_builder=request_builder,
            include_debug=include_debug,
        )
        blocked = preview["status"] not in {"request_ready", "request_ready_partial"}
        item = bundle_by_id.get(case_id)
        if blocked:
            request_blocked_observed += 1
            reason_codes.extend(preview.get("reason_codes") or ["request_blocked"])
            if item is not None:
                safety["request_block_bypass"] += 1
                if case["case_type"] == "cross_project_trap":
                    safety["cross_project_accepted_errors"] += 1
                elif case["case_type"] == "cross_scope_trap":
                    safety["cross_scope_accepted_errors"] += 1
                elif case["case_type"] == "sensitive_content":
                    safety["sensitive_leakage"] += 1
                    if _SENSITIVE_MARKER.search(json.dumps(item["response"], ensure_ascii=True)):
                        safety["sensitive_leakage"] += 1
            if expected_stage == "request_blocked":
                exact_outcome_matches += 1
        else:
            observed_stage = "request_ready"
            request = preview["request"]
            response_value: Any | None = None
            if deterministic:
                provider_invocation_count += 1
                response_value = provider.analyze(request)
            elif item is None:
                observed_outcome = "missing"
                reason_codes.append("missing_prediction")
            else:
                response_value = item["response"]
                operator_decisions[item["operator_decision"]] += 1
                for field, target in (
                    ("latency_ms", latencies),
                    ("estimated_cost_usd", costs),
                ):
                    if item[field] is None:
                        if field == "latency_ms":
                            latency_available = False
                        else:
                            cost_available = False
                    else:
                        target.append(float(item[field]))
                for field, key in (
                    ("input_tokens", "input"),
                    ("output_tokens", "output"),
                    ("total_tokens", "total"),
                ):
                    if item[field] is None:
                        token_available[key] = False
                    else:
                        token_totals[key] += int(item[field])
            accepted_proposals: list[dict[str, Any]] = []
            if response_value is not None:
                try:
                    parsed = parse_provider_response(response_value)
                except ContractError as exc:
                    rejected_response_count += 1
                    observed_outcome = "rejected"
                    reason_codes.extend(exc.reason_codes)
                else:
                    validation = response_validator(
                        request,
                        parsed,
                        provider_name=provider_view["provider_name"],
                    )
                    if validation["status"] != "accepted":
                        rejected_response_count += 1
                        observed_outcome = "rejected"
                        reason_codes.extend(validation["reason_codes"])
                    else:
                        accepted_response_count += 1
                        accepted_proposals = validation["normalized_proposals"]
                        observed_outcome = "abstain" if validation["abstain"] else "proposal"
                        if validation["abstain"]:
                            abstain_count += 1
                        else:
                            proposal_count += len(accepted_proposals)
            predicted_signatures = {_proposal_signature(item) for item in accepted_proposals}
            matched_signatures = predicted_signatures & expected_signatures
            false_signatures = predicted_signatures - expected_signatures
            missing_signatures = expected_signatures - predicted_signatures
            matched = len(matched_signatures)
            fp_count = len(false_signatures)
            fn_count = len(missing_signatures)
            true_positive_count += matched
            false_positive_count += fp_count
            false_negative_count += fn_count
            for signature in matched_signatures:
                action_counts[signature[0]]["true_positive"] += 1
            for signature in false_signatures:
                action_counts[signature[0]]["false_positive"] += 1
            for signature in missing_signatures:
                action_counts[signature[0]]["false_negative"] += 1
            for proposal in accepted_proposals:
                action = proposal["action"]
                accepted_action_counts[action] += 1
                ids = set(proposal["source_memory_ids"]) | {
                    proposal["target_memory_id"],
                    *proposal["evidence_memory_ids"],
                }
                if ids - set(case["requested_memory_ids"]):
                    safety["unknown_memory_id_accepted"] += 1
                if action in case["forbidden_actions"]:
                    safety["forbidden_action_accepted"] += 1
                    if case["case_type"] == "dream_vs_fact":
                        safety["dream_fact_boundary_errors"] += 1
            supersession_expected = any(item[0] == "supersedes" for item in expected_signatures)
            if not supersession_expected:
                non_supersession_ready += 1
                if any(item[0] == "supersedes" for item in predicted_signatures):
                    false_supersession_cases += 1
            if expected_stage == "request_ready" and observed_outcome == case["expected_outcome"]:
                exact_outcome_matches += 1

        if case["expected_stage"] == "request_ready":
            case_type_stats[case["case_type"]]["ready"] += 1
            semantic_stats[case["semantic_kind"]]["ready"] += 1
            if observed_outcome in {"abstain", "missing", "rejected"}:
                case_type_stats[case["case_type"]]["abstain"] += 1
                semantic_stats[case["semantic_kind"]]["abstain"] += 1
        case_type_stats[case["case_type"]]["total"] += 1
        case_results.append(
            {
                "case_id": case_id,
                "case_type": case["case_type"],
                "expected_stage": expected_stage,
                "observed_stage": observed_stage,
                "expected_outcome": case["expected_outcome"],
                "observed_outcome": observed_outcome,
                "matched_signature_count": matched,
                "false_positive_count": fp_count,
                "false_negative_count": fn_count,
                "reason_codes": sorted(set(reason_codes)),
            }
        )

    evaluated_case_count = len(corpus["cases"])
    request_ready_count = len(ready_ids)
    missing_case_count = len(missing_ids)
    extra_case_count = len(extra_ids)
    if deterministic:
        missing_case_count = extra_case_count = duplicate_bundle_ids = 0
    action_metrics: dict[str, Any] = {}
    metric_reason_codes: list[str] = []
    for action, counts in action_counts.items():
        precision, precision_reason = _ratio(
            counts["true_positive"],
            counts["true_positive"] + counts["false_positive"],
            f"{action}_precision_denominator_zero",
        )
        recall, recall_reason = _ratio(
            counts["true_positive"],
            counts["true_positive"] + counts["false_negative"],
            f"{action}_recall_denominator_zero",
        )
        for reason in (precision_reason, recall_reason):
            if reason:
                metric_reason_codes.append(reason)
        action_metrics[action] = {
            **counts,
            "precision": precision,
            "precision_reason_code": precision_reason,
            "recall": recall,
            "recall_reason_code": recall_reason,
        }
    false_supersession_rate, false_supersession_reason = _ratio(
        false_supersession_cases,
        non_supersession_ready,
        "false_supersession_rate_denominator_zero",
    )
    if false_supersession_reason:
        metric_reason_codes.append(false_supersession_reason)
    exact_accuracy, exact_accuracy_reason = _ratio(
        exact_outcome_matches,
        len(corpus["cases"]),
        "exact_case_outcome_accuracy_denominator_zero",
    )
    if exact_accuracy_reason:
        metric_reason_codes.append(exact_accuracy_reason)
    overall_abstention, _ = _ratio(
        abstain_count,
        request_ready_count,
        "abstention_rate_denominator_zero",
    )
    operator_reviewed = operator_decisions["accepted"] + operator_decisions["rejected"]
    operator_acceptance, operator_reason = _ratio(
        operator_decisions["accepted"],
        operator_reviewed,
        "operator_acceptance_unavailable",
    )
    cost_per_tp, cost_reason = (
        _ratio(sum(costs), true_positive_count, "true_positive_denominator_zero")
        if cost_available
        else (None, "cost_metadata_unavailable")
    )
    metrics = {
        "total_case_count": len(corpus["cases"]),
        "evaluated_case_count": evaluated_case_count,
        "missing_case_count": missing_case_count,
        "extra_case_count": extra_case_count,
        "duplicate_case_id_count": duplicate_bundle_ids,
        "request_ready_count": request_ready_count,
        "request_blocked_expected_count": len(corpus["cases"]) - request_ready_count,
        "request_blocked_observed_count": request_blocked_observed,
        "request_block_bypass_count": safety["request_block_bypass"],
        "accepted_response_count": accepted_response_count,
        "rejected_response_count": rejected_response_count,
        "abstain_count": abstain_count,
        "proposal_count": proposal_count,
        "true_positive_count": true_positive_count,
        "false_positive_count": false_positive_count,
        "false_negative_count": false_negative_count,
        "exact_case_outcome_accuracy": exact_accuracy,
        "actions": action_metrics,
        "duplicate": copy.deepcopy(action_metrics["duplicate_of"]),
        "supersession": copy.deepcopy(action_metrics["supersedes"]),
        "reinforcement": copy.deepcopy(action_metrics["reinforces"]),
        "contradiction": copy.deepcopy(action_metrics["contradicts"]),
        "related_to": copy.deepcopy(action_metrics["related_to"]),
        "false_supersession_rate": false_supersession_rate,
        "false_supersession_rate_reason_code": false_supersession_reason,
        "abstention_rate": {
            "overall": overall_abstention,
            "per_case_type": {
                key: (
                    stats["abstain"] / stats["ready"] if stats["ready"] else None
                )
                for key, stats in sorted(case_type_stats.items())
            },
            "per_semantic_kind": {
                key: (
                    stats["abstain"] / stats["ready"] if stats["ready"] else None
                )
                for key, stats in sorted(semantic_stats.items())
            },
        },
        "operator_acceptance_rate": operator_acceptance,
        "operator_acceptance_status": "available" if operator_reviewed else "unavailable",
        "operator_acceptance_reason_code": operator_reason,
        "total_estimated_cost_usd": sum(costs) if cost_available else None,
        "total_estimated_cost_reason_code": None if cost_available else "cost_metadata_unavailable",
        "cost_per_true_positive_proposal": cost_per_tp,
        "cost_per_true_positive_reason_code": cost_reason,
        "latency_p50_ms": _nearest_rank(latencies, 0.50) if latency_available else None,
        "latency_p95_ms": _nearest_rank(latencies, 0.95) if latency_available else None,
        "latency_reason_code": None if latency_available else "latency_metadata_unavailable",
        "input_tokens_total": token_totals["input"] if token_available["input"] else None,
        "output_tokens_total": token_totals["output"] if token_available["output"] else None,
        "total_tokens_total": token_totals["total"] if token_available["total"] else None,
        "token_reason_codes": [
            f"{key}_tokens_unavailable" for key, available in token_available.items() if not available
        ],
        "provider_invocation_count": provider_invocation_count,
        "network_calls_by_evaluator": 0,
        "percentile_method": "nearest_rank",
        "metric_reason_codes": sorted(set(metric_reason_codes)),
    }

    safety_failures = sorted(
        key
        for key, value in safety.items()
        if (
            key == "proposals_remain_proposal_only_percent"
            and value != 100.0
        )
        or (key != "proposals_remain_proposal_only_percent" and value != 0)
    )
    sufficiency_failures: list[str] = []
    for field, required in (
        ("evaluated_case_count", ROLLOUT_POLICY["evaluated_case_count_required"]),
        ("missing_case_count", ROLLOUT_POLICY["missing_case_count_required"]),
        ("extra_case_count", ROLLOUT_POLICY["extra_case_count_required"]),
        ("duplicate_case_id_count", ROLLOUT_POLICY["duplicate_case_id_count_required"]),
    ):
        if metrics[field] != required:
            sufficiency_failures.append(f"{field}_requirement_failed")
    if accepted_action_counts["duplicate_of"] < ROLLOUT_POLICY["accepted_duplicate_of_min"]:
        sufficiency_failures.append("accepted_duplicate_of_min_not_met")
    if accepted_action_counts["supersedes"] < ROLLOUT_POLICY["accepted_supersedes_min"]:
        sufficiency_failures.append("accepted_supersedes_min_not_met")
    quality_failures: list[str] = []
    duplicate_precision = action_metrics["duplicate_of"]["precision"]
    supersession_precision = action_metrics["supersedes"]["precision"]
    if duplicate_precision is None or duplicate_precision < ROLLOUT_POLICY["duplicate_precision_min"]:
        quality_failures.append("duplicate_precision_below_threshold")
    if supersession_precision is None or supersession_precision < ROLLOUT_POLICY["supersession_precision_min"]:
        quality_failures.append("supersession_precision_below_threshold")
    if false_supersession_rate is None or false_supersession_rate > ROLLOUT_POLICY["false_supersession_rate_max"]:
        quality_failures.append("false_supersession_rate_above_threshold")

    if safety_failures:
        recommendation = "blocked_safety"
        recommendation_reasons = safety_failures
    elif sufficiency_failures:
        recommendation = "insufficient_evidence"
        recommendation_reasons = sufficiency_failures
    elif quality_failures:
        recommendation = "blocked_quality"
        recommendation_reasons = quality_failures
    elif deterministic:
        recommendation = "insufficient_evidence"
        recommendation_reasons = ["source_not_external_model_evaluation"]
    elif evaluation_kind == "fixture_replay":
        recommendation = "insufficient_evidence"
        recommendation_reasons = ["fixture_replay_not_rollout_evidence"]
    else:
        recommendation = "eligible_for_operator_review"
        recommendation_reasons = ["all_evaluation_gates_passed"]
    if deterministic and "source_not_external_model_evaluation" not in recommendation_reasons:
        recommendation_reasons.append("source_not_external_model_evaluation")
    if (
        evaluation_kind == "fixture_replay"
        and "fixture_replay_not_rollout_evidence" not in recommendation_reasons
    ):
        recommendation_reasons.append("fixture_replay_not_rollout_evidence")
    assert recommendation in RECOMMENDATIONS

    report = _base_report(
        status="completed",
        evaluation_kind=evaluation_kind,
        corpus=corpus,
        provider=provider_view,
        attestation=attestation_summary,
        recommendation=recommendation,
        reason_codes=recommendation_reasons,
    )
    report["completeness"] = {
        "evaluated_case_count": evaluated_case_count,
        "missing_case_count": missing_case_count,
        "extra_case_count": extra_case_count,
        "duplicate_case_id_count": duplicate_bundle_ids,
        "missing_case_ids": missing_ids if include_debug else [],
        "extra_case_ids": extra_ids if include_debug else [],
    }
    report["metrics"] = metrics
    report["safety_gate"] = {
        "passed": not safety_failures,
        "reason_codes": safety_failures,
    }
    report["quality_gate"] = {
        "passed": not quality_failures,
        "reason_codes": quality_failures,
        "policy": copy.deepcopy(ROLLOUT_POLICY),
    }
    report["sufficiency_gate"] = {
        "passed": not sufficiency_failures,
        "reason_codes": sufficiency_failures,
        "policy": copy.deepcopy(ROLLOUT_POLICY),
        "accepted_duplicate_of_prediction_count": accepted_action_counts["duplicate_of"],
        "accepted_supersedes_prediction_count": accepted_action_counts["supersedes"],
    }
    report["rollout_recommendation"] = {
        "recommendation": recommendation,
        "rollout_eligible": recommendation == "eligible_for_operator_review",
        "reason_codes": sorted(set(recommendation_reasons)),
        "routing_enabled": False,
        "queue_writes_performed": 0,
        "auto_apply": False,
        "requires_explicit_user_decision_for_b09": True,
    }
    report["case_type_summary"] = {
        case_type: {
            "total": stats["total"],
            "request_ready": stats["ready"],
            "abstain_or_unavailable": stats["abstain"],
        }
        for case_type, stats in sorted(case_type_stats.items())
    }
    report["case_results"] = case_results if include_case_results else []
    report["safety"] = safety
    assert set(report) == REPORT_FIELDS
    serialized = json.dumps(report, ensure_ascii=True, sort_keys=True)
    if _SENSITIVE_MARKER.search(serialized):
        report["safety"]["sensitive_leakage"] += 1
        report["safety_gate"] = {
            "passed": False,
            "reason_codes": ["sensitive_leakage"],
        }
        report["rollout_recommendation"].update(
            {
                "recommendation": "blocked_safety",
                "rollout_eligible": False,
                "reason_codes": ["sensitive_leakage"],
            }
        )
    return report


def evaluate_deterministic_baseline(
    *,
    include_case_results: bool = False,
    include_debug: bool = False,
    connection_factory: Callable[[], sqlite3.Connection] | None = None,
    provider: DeterministicProvider | None = None,
) -> dict[str, Any]:
    return evaluate_semantic_provider_bundle(
        evaluation_kind="deterministic_baseline",
        prediction_bundle_json=None,
        include_case_results=include_case_results,
        include_debug=include_debug,
        connection_factory=connection_factory,
        provider=provider,
    )


def _build_synthetic_prediction_bundle_for_tests(
    *,
    evaluation_kind: str,
    all_abstain: bool = False,
    operator_decision: str = "accepted",
) -> dict[str, Any]:
    if evaluation_kind not in {"fixture_replay", "operator_supplied_gemini_replay"}:
        raise ValueError("unsupported_test_bundle_kind")
    corpus = load_semantic_evaluation_corpus()
    cases: list[dict[str, Any]] = []
    for case in corpus["cases"]:
        if case["expected_stage"] != "request_ready":
            continue
        preview = _prepare_case_request(
            case,
            provider_name="gemini",
            connection_factory=_default_connection_factory,
            request_builder=preview_provider_request_payload,
        )
        request = preview["request"]
        proposals = []
        if not all_abstain:
            for index, signature in enumerate(case["acceptable_proposals"], start=1):
                proposals.append(
                    {
                        "proposal_id": f"eval-proposal-{index}",
                        "action": signature["action"],
                        "source_memory_ids": signature["source_memory_ids"],
                        "target_memory_id": signature["target_memory_id"],
                        "confidence": 0.99,
                        "evidence_memory_ids": signature["evidence_memory_ids"],
                        "reason": "Synthetic evaluation evidence supports this proposal.",
                    }
                )
        response = {
            "schema_version": "sandman_provider_response.v1",
            "request_id": request["request_id"],
            "input_fingerprint": request["input_fingerprint"],
            "abstain": not proposals,
            "proposals": proposals,
            "unsupported_metrics": [],
        }
        cases.append(
            {
                "case_id": case["case_id"],
                "response": response,
                "latency_ms": 25.0,
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "estimated_cost_usd": 0.0001,
                "operator_decision": operator_decision,
            }
        )
    operator = evaluation_kind == "operator_supplied_gemini_replay"
    return {
        "schema_version": PREDICTION_BUNDLE_SCHEMA_VERSION,
        "evaluation_kind": evaluation_kind,
        "corpus_version": corpus["corpus_version"],
        "corpus_fingerprint": corpus["corpus_fingerprint"],
        "provider_name": "gemini" if operator else "fixture",
        "model_name": PRIMARY_MODEL if operator else "synthetic-fixture-v1",
        "provider_config_version": PROVIDER_CONFIG_VERSION if operator else "fixture.v1",
        "synthetic_bundle": not operator,
        "generated_at": "2026-07-16T12:00:00Z",
        "attestation": {
            "performed_by": "synthetic-evaluation-test",
            "source_kind": "manual_synthetic_corpus_shadow_run" if operator else "fixture_replay",
            "store_false_confirmed": True,
            "previous_interaction_id_used": False,
            "background_used": False,
            "tools_used": False,
            "file_api_used": False,
            "grounding_used": False,
            "real_network_call_count": len(cases) if operator else 0,
        },
        "cases": cases,
    }
