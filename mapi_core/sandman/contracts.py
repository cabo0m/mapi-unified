from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from typing import Any, Mapping


PROVIDER_REQUEST_SCHEMA_VERSION = "sandman_provider_request.v1"
PROVIDER_RESPONSE_SCHEMA_VERSION = "sandman_provider_response.v1"
PROVIDER_VALIDATION_SCHEMA_VERSION = "sandman_provider_validation.v1"
REDACTION_MANIFEST_SCHEMA_VERSION = "sandman_redaction_manifest.v1"
REDACTION_POLICY_VERSION = "sandman_redaction_policy.v1"
EXTERNAL_DATA_POLICY = "redacted_project_only"
HASH_ALGORITHM = "sha256:canonical-json:v1"

MAX_CANDIDATES = 20
MAX_PROPOSALS = 8
MAX_REDACTED_CHARS_PER_CANDIDATE = 800
MAX_TOTAL_REDACTED_CHARS = 8000
MAX_REASON_CHARS = 500
MAX_EVIDENCE_IDS_PER_PROPOSAL = 20
INTERACTIONS_SCHEMA_KEYWORDS = frozenset(
    {
        "type", "title", "description", "properties", "required",
        "additionalProperties", "enum", "format", "minimum", "maximum",
        "items", "prefixItems", "minItems", "maxItems",
    }
)


class ProviderAction(StrEnum):
    DUPLICATE_OF = "duplicate_of"
    REINFORCES = "reinforces"
    SUPERSEDES = "supersedes"
    CONTRADICTS = "contradicts"
    RELATED_TO = "related_to"
    ABSTAIN = "abstain"


PROVIDER_ACTIONS = frozenset(item.value for item in ProviderAction)
PROPOSAL_ACTIONS = PROVIDER_ACTIONS - {ProviderAction.ABSTAIN.value}

REQUEST_FIELDS = frozenset(
    {
        "schema_version", "request_id", "provider_name", "project_key", "scope_code",
        "workspace_id", "candidate_memory_ids", "candidates", "allowed_actions",
        "proposal_budget", "input_fingerprint", "redaction_manifest", "safety",
    }
)
CANDIDATE_FIELDS = frozenset(
    {
        "memory_id", "project_key", "scope_code", "workspace_id", "memory_type",
        "truth_kind", "state_code", "artifact_kind", "created_at", "updated_at",
        "content_redacted", "content_sha256", "redacted_content_sha256",
        "sensitivity_class", "redaction_applied", "supersedes_memory_id",
        "superseded_by_memory_id", "allowlisted_links",
    }
)
LINK_FIELDS = frozenset({"relation_type", "target_memory_id"})
REDACTION_MANIFEST_FIELDS = frozenset(
    {
        "schema_version", "policy_version", "external_data_policy",
        "candidate_count_requested", "candidate_count_included", "candidate_count_excluded",
        "included_memory_ids", "excluded_candidates", "replacement_counts",
        "truncated_memory_ids", "raw_secret_exposed", "full_project_dump",
    }
)
EXCLUDED_CANDIDATE_FIELDS = frozenset({"memory_id", "sensitivity_class", "reason_codes"})
RESPONSE_FIELDS = frozenset(
    {"schema_version", "request_id", "input_fingerprint", "abstain", "proposals", "unsupported_metrics"}
)
PROPOSAL_FIELDS = frozenset(
    {"proposal_id", "action", "source_memory_ids", "target_memory_id", "confidence", "evidence_memory_ids", "reason"}
)
SAFETY_BLOCK = {
    "proposal_only": True,
    "model_auto_apply": False,
    "queue_routing": False,
    "tools_available": False,
    "database_access": False,
    "filesystem_access": False,
    "raw_secret_exposed": False,
    "full_history_included": False,
}

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_METRIC_ID = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_REPLACEMENT_KEYS = frozenset({"credential_uri", "email", "person_id", "phone", "ip", "address"})
_ALLOWED_INCLUDED_SENSITIVITY_CLASSES = frozenset({"public", "internal", "personal"})
_RESTRICTED_SENSITIVITY_CLASSES = frozenset(
    {"health_sensitive", "financial_sensitive", "credential_secret", "private_key", "never_store"}
)
_PERSONAL_PLACEHOLDERS = frozenset(
    {
        "[REDACTED_CREDENTIAL_URI]", "[REDACTED_EMAIL]", "[REDACTED_PERSON_ID]",
        "[REDACTED_PHONE]", "[REDACTED_IP]", "[REDACTED_ADDRESS]",
    }
)


class ContractError(ValueError):
    def __init__(self, reason_codes: str | list[str]):
        self.reason_codes = sorted(set([reason_codes] if isinstance(reason_codes, str) else reason_codes))
        super().__init__(",".join(self.reason_codes))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_fingerprint(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _strict_fields(value: Mapping[str, Any], expected: frozenset[str], code: str) -> None:
    if set(value) != expected:
        raise ContractError(code)


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate_json_key")
        result[key] = value
    return result


def _reject_invalid_json_constant(_value: str) -> None:
    raise ContractError("invalid_json_constant")


def strict_json_loads(value: str, *, invalid_code: str) -> Any:
    try:
        return json.loads(
            value,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_invalid_json_constant,
        )
    except ContractError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ContractError(invalid_code) from exc


_strict_json_loads = strict_json_loads


def _normalized_int_ids(value: Any, *, non_empty: bool = True, require_canonical: bool = False) -> list[int]:
    if not isinstance(value, list) or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in value):
        raise ContractError("invalid_memory_ids")
    result = sorted(set(value))
    if non_empty and not result:
        raise ContractError("invalid_memory_ids")
    if require_canonical and value != result:
        raise ContractError("invalid_memory_ids")
    return result


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _manifest_ids(value: Any) -> list[int]:
    try:
        return _normalized_int_ids(value, non_empty=False, require_canonical=True)
    except ContractError as exc:
        raise ContractError("redaction_manifest_consistency_error") from exc


def _validate_redaction_manifest(
    manifest_value: Any,
    *,
    candidate_ids: list[int],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(manifest_value, Mapping):
        raise ContractError("invalid_redaction_manifest_schema")
    manifest = dict(manifest_value)
    _strict_fields(manifest, REDACTION_MANIFEST_FIELDS, "invalid_redaction_manifest_schema")
    if (
        manifest["schema_version"] != REDACTION_MANIFEST_SCHEMA_VERSION
        or manifest["policy_version"] != REDACTION_POLICY_VERSION
        or manifest["external_data_policy"] != EXTERNAL_DATA_POLICY
        or manifest["raw_secret_exposed"] is not False
        or manifest["full_project_dump"] is not False
    ):
        raise ContractError("unsafe_redaction_manifest")

    count_fields = ("candidate_count_requested", "candidate_count_included", "candidate_count_excluded")
    if any(not _non_negative_int(manifest[field]) for field in count_fields):
        raise ContractError("redaction_manifest_consistency_error")
    included_ids = _manifest_ids(manifest["included_memory_ids"])
    if not isinstance(manifest["excluded_candidates"], list):
        raise ContractError("invalid_redaction_manifest_schema")
    excluded_candidates: list[dict[str, Any]] = []
    excluded_ids: list[int] = []
    from mapi_core.memory.sensitivity import SENSITIVITY_CLASSES

    for excluded_value in manifest["excluded_candidates"]:
        if not isinstance(excluded_value, Mapping):
            raise ContractError("invalid_redaction_manifest_schema")
        excluded = dict(excluded_value)
        _strict_fields(excluded, EXCLUDED_CANDIDATE_FIELDS, "invalid_redaction_manifest_schema")
        memory_id = excluded["memory_id"]
        if isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id < 1:
            raise ContractError("redaction_manifest_consistency_error")
        if excluded["sensitivity_class"] not in SENSITIVITY_CLASSES:
            raise ContractError("redaction_manifest_consistency_error")
        reason_codes = excluded["reason_codes"]
        if (
            not isinstance(reason_codes, list)
            or not reason_codes
            or reason_codes != sorted(set(reason_codes))
            or any(not isinstance(code, str) or not _REASON_CODE.fullmatch(code) for code in reason_codes)
        ):
            raise ContractError("redaction_manifest_consistency_error")
        excluded_ids.append(memory_id)
        excluded_candidates.append(excluded)
    if excluded_ids != sorted(set(excluded_ids)):
        raise ContractError("redaction_manifest_consistency_error")

    replacements = manifest["replacement_counts"]
    if not isinstance(replacements, Mapping) or set(replacements) - _ALLOWED_REPLACEMENT_KEYS:
        raise ContractError("redaction_manifest_consistency_error")
    replacements = dict(replacements)
    if any(not _non_negative_int(count) for count in replacements.values()):
        raise ContractError("redaction_manifest_consistency_error")
    truncated_ids = _manifest_ids(manifest["truncated_memory_ids"])
    candidate_by_id = {candidate["memory_id"]: candidate for candidate in candidates}
    exact_candidate_ids = [candidate["memory_id"] for candidate in candidates]
    if (
        manifest["candidate_count_requested"]
        != manifest["candidate_count_included"] + manifest["candidate_count_excluded"]
        or manifest["candidate_count_included"] != len(included_ids)
        or manifest["candidate_count_excluded"] != len(excluded_candidates)
        or included_ids != candidate_ids
        or included_ids != exact_candidate_ids
        or set(included_ids) & set(excluded_ids)
        or not set(truncated_ids).issubset(included_ids)
    ):
        raise ContractError("redaction_manifest_consistency_error")
    if any(not candidate_by_id[memory_id]["redaction_applied"] for memory_id in truncated_ids):
        raise ContractError("redaction_manifest_consistency_error")
    manifest["included_memory_ids"] = included_ids
    manifest["excluded_candidates"] = excluded_candidates
    manifest["replacement_counts"] = dict(sorted(replacements.items()))
    manifest["truncated_memory_ids"] = truncated_ids
    return manifest


def _validate_candidate_trust_boundary(
    candidate: dict[str, Any],
    *,
    manifest: Mapping[str, Any],
) -> None:
    sensitivity_class = candidate["sensitivity_class"]
    if sensitivity_class in _RESTRICTED_SENSITIVITY_CLASSES:
        raise ContractError("restricted_candidate_in_provider_request")
    if sensitivity_class not in _ALLOWED_INCLUDED_SENSITIVITY_CLASSES:
        raise ContractError("invalid_candidate_schema")
    content_hash = candidate["content_sha256"]
    redacted_hash = candidate["redacted_content_sha256"]
    if (
        not isinstance(content_hash, str)
        or not _SHA256.fullmatch(content_hash)
        or not isinstance(redacted_hash, str)
        or not _SHA256.fullmatch(redacted_hash)
    ):
        raise ContractError("invalid_candidate_hash")
    expected_redacted_hash = hashlib.sha256(candidate["content_redacted"].encode("utf-8")).hexdigest()
    if redacted_hash != expected_redacted_hash:
        raise ContractError("redacted_content_hash_mismatch")

    from mapi_core.memory.sensitivity import classify_memory_sensitivity
    from mapi_core.sandman.redaction import residual_sensitive_reason_codes

    if residual_sensitive_reason_codes(candidate["content_redacted"]):
        raise ContractError("residual_sensitive_material_in_request")
    residual_class = classify_memory_sensitivity(candidate["content_redacted"], metadata={})["sensitivity_class"]
    if residual_class in _RESTRICTED_SENSITIVITY_CLASSES:
        raise ContractError("restricted_residual_classification")
    if sensitivity_class == "personal":
        has_placeholder = any(
            placeholder in candidate["content_redacted"] for placeholder in _PERSONAL_PLACEHOLDERS
        )
        has_positive_replacement = any(
            count > 0 for count in manifest["replacement_counts"].values()
        )
        if not candidate["redaction_applied"] or not has_placeholder or not has_positive_replacement:
            raise ContractError("personal_redaction_not_proven_in_request")


def semantic_request_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    return {key: request[key] for key in sorted(REQUEST_FIELDS - {"input_fingerprint"})}


def compute_request_fingerprint(request: Mapping[str, Any]) -> str:
    return canonical_fingerprint(semantic_request_payload(request))


def validate_provider_request(request: Any, *, require_fingerprint: bool = True) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ContractError("invalid_request_schema")
    request = dict(request)
    _strict_fields(request, REQUEST_FIELDS, "invalid_request_schema")
    if request["schema_version"] != PROVIDER_REQUEST_SCHEMA_VERSION:
        raise ContractError("invalid_request_schema")
    for field in ("request_id", "provider_name", "project_key", "scope_code"):
        if not isinstance(request[field], str) or not request[field].strip():
            raise ContractError("invalid_request_schema")
    if not _OPAQUE_ID.fullmatch(request["request_id"]):
        raise ContractError("invalid_request_id")
    if request["scope_code"] in {"global", "public"}:
        raise ContractError("boundary_not_allowed")
    if request["workspace_id"] is not None and (isinstance(request["workspace_id"], bool) or not isinstance(request["workspace_id"], int)):
        raise ContractError("invalid_request_schema")
    candidate_ids = _normalized_int_ids(request["candidate_memory_ids"])
    if len(candidate_ids) > MAX_CANDIDATES:
        raise ContractError("candidate_budget_overflow")
    if not isinstance(request["allowed_actions"], list):
        raise ContractError("invalid_allowed_actions")
    allowed_actions = sorted(set(request["allowed_actions"]))
    if not allowed_actions or any(action not in PROPOSAL_ACTIONS for action in allowed_actions):
        raise ContractError("invalid_allowed_actions")
    budget = request["proposal_budget"]
    if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= MAX_PROPOSALS:
        raise ContractError("proposal_budget_overflow")
    if request["safety"] != SAFETY_BLOCK:
        raise ContractError("invalid_safety_block")
    candidates = request["candidates"]
    if not isinstance(candidates, list) or len(candidates) != len(candidate_ids):
        raise ContractError("candidate_allowlist_mismatch")
    normalized_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ContractError("invalid_candidate_schema")
        candidate = dict(candidate)
        _strict_fields(candidate, CANDIDATE_FIELDS, "invalid_candidate_schema")
        memory_id = candidate["memory_id"]
        if memory_id not in candidate_ids:
            raise ContractError("candidate_allowlist_mismatch")
        if any(candidate[field] != request[field] for field in ("project_key", "scope_code", "workspace_id")):
            raise ContractError("candidate_boundary_mismatch")
        if candidate["artifact_kind"] not in {"fact", "decision", "preference", "dream", "hypothesis", "operational", "unknown"}:
            raise ContractError("invalid_artifact_kind")
        if not isinstance(candidate["content_redacted"], str) or len(candidate["content_redacted"]) > MAX_REDACTED_CHARS_PER_CANDIDATE:
            raise ContractError("invalid_redacted_content")
        if not isinstance(candidate["redaction_applied"], bool):
            raise ContractError("invalid_candidate_schema")
        links = candidate["allowlisted_links"]
        if not isinstance(links, list):
            raise ContractError("invalid_candidate_links")
        normalized_links: list[dict[str, Any]] = []
        for link in links:
            if not isinstance(link, Mapping):
                raise ContractError("invalid_candidate_links")
            link = dict(link)
            _strict_fields(link, LINK_FIELDS, "invalid_candidate_links")
            if link["relation_type"] not in {"contradicts", "reinforces", "related_to"} or link["target_memory_id"] not in candidate_ids:
                raise ContractError("invalid_candidate_links")
            normalized_links.append(link)
        candidate["allowlisted_links"] = sorted(normalized_links, key=lambda item: (item["relation_type"], item["target_memory_id"]))
        normalized_candidates.append(candidate)
    normalized_candidates.sort(key=lambda item: item["memory_id"])
    if [item["memory_id"] for item in normalized_candidates] != candidate_ids:
        raise ContractError("candidate_allowlist_mismatch")
    manifest = _validate_redaction_manifest(
        request["redaction_manifest"],
        candidate_ids=candidate_ids,
        candidates=normalized_candidates,
    )
    for candidate in normalized_candidates:
        _validate_candidate_trust_boundary(candidate, manifest=manifest)
    if sum(len(item["content_redacted"]) for item in normalized_candidates) > MAX_TOTAL_REDACTED_CHARS:
        raise ContractError("total_redacted_content_overflow")
    request["candidate_memory_ids"] = candidate_ids
    request["allowed_actions"] = allowed_actions
    request["candidates"] = normalized_candidates
    request["redaction_manifest"] = manifest
    expected = compute_request_fingerprint(request)
    if require_fingerprint and request["input_fingerprint"] != expected:
        raise ContractError("input_fingerprint_mismatch")
    request["input_fingerprint"] = expected
    return request


def build_provider_request(**fields: Any) -> dict[str, Any]:
    payload = {"schema_version": PROVIDER_REQUEST_SCHEMA_VERSION, **fields, "input_fingerprint": "pending"}
    normalized = validate_provider_request(payload, require_fingerprint=False)
    normalized["input_fingerprint"] = compute_request_fingerprint(normalized)
    return validate_provider_request(normalized)


def parse_provider_request(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = strict_json_loads(value, invalid_code="invalid_request_json")
    if not isinstance(value, Mapping):
        raise ContractError("invalid_request_schema")
    return validate_provider_request(value)


def parse_provider_response(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = strict_json_loads(value, invalid_code="invalid_response_json")
    if not isinstance(value, Mapping):
        raise ContractError("invalid_response_schema")
    response = dict(value)
    _strict_fields(response, RESPONSE_FIELDS, "invalid_response_schema")
    if response["schema_version"] != PROVIDER_RESPONSE_SCHEMA_VERSION:
        raise ContractError("invalid_response_schema")
    if not isinstance(response["request_id"], str) or not isinstance(response["input_fingerprint"], str):
        raise ContractError("invalid_response_schema")
    if not isinstance(response["abstain"], bool) or not isinstance(response["proposals"], list):
        raise ContractError("invalid_response_schema")
    metrics = response["unsupported_metrics"]
    if not isinstance(metrics, list) or any(not isinstance(item, str) or not _METRIC_ID.fullmatch(item) for item in metrics):
        raise ContractError("invalid_unsupported_metrics")
    if len(metrics) != len(set(metrics)):
        raise ContractError("invalid_unsupported_metrics")
    response["unsupported_metrics"] = sorted(metrics)
    normalized: list[dict[str, Any]] = []
    for proposal in response["proposals"]:
        if not isinstance(proposal, Mapping):
            raise ContractError("invalid_proposal_schema")
        proposal = dict(proposal)
        _strict_fields(proposal, PROPOSAL_FIELDS, "invalid_proposal_schema")
        if not isinstance(proposal["proposal_id"], str) or not _OPAQUE_ID.fullmatch(proposal["proposal_id"]):
            raise ContractError("invalid_proposal_id")
        if not isinstance(proposal["action"], str):
            raise ContractError("invalid_proposal_schema")
        proposal["source_memory_ids"] = _normalized_int_ids(proposal["source_memory_ids"], require_canonical=True)
        target = proposal["target_memory_id"]
        if isinstance(target, bool) or not isinstance(target, int) or target < 1 or target in proposal["source_memory_ids"]:
            raise ContractError("invalid_target_memory_id")
        proposal["evidence_memory_ids"] = _normalized_int_ids(proposal["evidence_memory_ids"], require_canonical=True)
        if len(proposal["evidence_memory_ids"]) > MAX_EVIDENCE_IDS_PER_PROPOSAL:
            raise ContractError("evidence_budget_overflow")
        confidence = proposal["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
            raise ContractError("invalid_confidence")
        proposal["confidence"] = float(confidence)
        if not isinstance(proposal["reason"], str) or not 1 <= len(proposal["reason"].strip()) <= MAX_REASON_CHARS:
            raise ContractError("invalid_reason")
        normalized.append(proposal)
    response["proposals"] = normalized
    return response


def provider_response_json_schema() -> dict[str, Any]:
    proposal_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(PROPOSAL_FIELDS),
        "properties": {
            "proposal_id": {"type": "string"},
            "action": {"type": "string", "enum": sorted(PROPOSAL_ACTIONS)},
            "source_memory_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_CANDIDATES,
                "items": {"type": "integer", "minimum": 1},
            },
            "target_memory_id": {"type": "integer", "minimum": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence_memory_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_EVIDENCE_IDS_PER_PROPOSAL,
                "items": {"type": "integer", "minimum": 1},
            },
            "reason": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(RESPONSE_FIELDS),
        "properties": {
            "schema_version": {"type": "string", "enum": [PROVIDER_RESPONSE_SCHEMA_VERSION]},
            "request_id": {"type": "string"},
            "input_fingerprint": {"type": "string"},
            "abstain": {"type": "boolean"},
            "proposals": {
                "type": "array",
                "maxItems": MAX_PROPOSALS,
                "items": proposal_schema,
            },
            "unsupported_metrics": {
                "type": "array",
                "maxItems": MAX_PROPOSALS,
                "items": {"type": "string"},
            },
        },
    }


def audit_interactions_schema_keywords(schema: Mapping[str, Any]) -> set[str]:
    invalid: set[str] = set()

    def visit(value: Any, *, inside_properties: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if not inside_properties and key not in INTERACTIONS_SCHEMA_KEYWORDS:
                    invalid.add(str(key))
                visit(nested, inside_properties=(key == "properties"))
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(schema)
    return invalid
