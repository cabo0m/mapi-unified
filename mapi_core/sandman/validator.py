from __future__ import annotations

import re
from typing import Any, Mapping

from mapi_core.sandman.contracts import (
    ContractError,
    PROVIDER_VALIDATION_SCHEMA_VERSION,
    PROPOSAL_ACTIONS,
    canonical_fingerprint,
    parse_provider_request,
    parse_provider_response,
)
from mapi_core.sandman.redaction import residual_sensitive_reason_codes


VALIDATION_SAFETY = {
    "proposal_only": True,
    "memory_mutations_performed": 0,
    "queue_mutations_performed": 0,
    "raw_secret_exposed": False,
    "auto_apply": False,
}
_FORBIDDEN_SEMANTICS = {
    "archive", "delete", "merge", "create_memory", "promote", "owner_change",
    "score_change", "queue_routing", "apply", "tool_call", "database_write",
}
_DREAM_FACT_ACTIONS = {"duplicate_of", "reinforces", "supersedes", "contradicts"}


def _contains_forbidden_semantics(reason: str) -> bool:
    normalized = reason.casefold().replace("-", "_")
    if re.search(r"\b(?:archive|delete|merge|create_memory|promote|apply)\b", normalized):
        return True
    return any(phrase in normalized.replace(" ", "_") for phrase in _FORBIDDEN_SEMANTICS - {"archive", "delete", "merge", "create_memory", "promote", "apply"})


def _result(
    *, request: Mapping[str, Any] | None, response: Mapping[str, Any] | None,
    provider_name: str, status: str, reason_codes: list[str], rejected_indexes: list[int],
) -> dict[str, Any]:
    accepted = status == "accepted"
    return {
        "schema_version": PROVIDER_VALIDATION_SCHEMA_VERSION,
        "status": status,
        "request_id": str((request or {}).get("request_id") or ""),
        "input_fingerprint": str((request or {}).get("input_fingerprint") or ""),
        "response_fingerprint": canonical_fingerprint(response) if response is not None else None,
        "provider_name": provider_name,
        "abstain": bool(response.get("abstain")) if accepted and response is not None else True,
        "normalized_proposals": list(response.get("proposals") or []) if accepted and response is not None else [],
        "reason_codes": sorted(set(reason_codes)),
        "rejected_proposal_indexes": sorted(set(rejected_indexes)),
        "safety": dict(VALIDATION_SAFETY),
    }


def validate_provider_response(request_value: Any, response_value: Any, *, provider_name: str) -> dict[str, Any]:
    try:
        request = parse_provider_request(request_value)
    except ContractError as exc:
        return _result(request=None, response=None, provider_name=provider_name, status="rejected", reason_codes=exc.reason_codes, rejected_indexes=[])
    try:
        response = parse_provider_response(response_value)
    except ContractError as exc:
        return _result(request=request, response=None, provider_name=provider_name, status="rejected", reason_codes=exc.reason_codes, rejected_indexes=[])

    reasons: list[str] = []
    rejected: list[int] = []
    if response["request_id"] != request["request_id"]:
        reasons.append("request_id_mismatch")
    if response["input_fingerprint"] != request["input_fingerprint"]:
        reasons.append("input_fingerprint_mismatch")
    if len(response["proposals"]) > request["proposal_budget"]:
        reasons.append("proposal_budget_overflow")
    if response["abstain"] != (len(response["proposals"]) == 0):
        reasons.append("abstain_proposals_mismatch")

    allowlist = set(request["candidate_memory_ids"])
    candidates = {item["memory_id"]: item for item in request["candidates"]}
    signatures: set[tuple[Any, ...]] = set()
    for index, proposal in enumerate(response["proposals"]):
        item_reasons: list[str] = []
        action = proposal["action"]
        ids = set(proposal["source_memory_ids"]) | {proposal["target_memory_id"]} | set(proposal["evidence_memory_ids"])
        if action not in PROPOSAL_ACTIONS or action not in request["allowed_actions"]:
            item_reasons.append("forbidden_action")
        if ids - allowlist:
            item_reasons.append("memory_id_not_allowlisted")
        required_evidence = set(proposal["source_memory_ids"]) | {proposal["target_memory_id"]}
        if not required_evidence.issubset(set(proposal["evidence_memory_ids"])):
            item_reasons.append("invalid_evidence")
        signature = (action, tuple(proposal["source_memory_ids"]), proposal["target_memory_id"])
        if signature in signatures:
            item_reasons.append("duplicate_proposal")
        signatures.add(signature)
        if not ids - allowlist:
            for memory_id in ids:
                candidate = candidates[memory_id]
                if any(candidate[field] != request[field] for field in ("project_key", "scope_code", "workspace_id")):
                    item_reasons.append("cross_boundary_reference")
                    break
            target = candidates[proposal["target_memory_id"]]
            if action in _DREAM_FACT_ACTIONS and any(candidates[source]["artifact_kind"] == "dream" for source in proposal["source_memory_ids"]) and target["artifact_kind"] != "dream":
                item_reasons.append("dream_fact_boundary_violation")
        if residual_sensitive_reason_codes(proposal["reason"]):
            item_reasons.append("response_sensitive_echo")
        if _contains_forbidden_semantics(proposal["reason"]):
            item_reasons.append("forbidden_mutation_semantics")
        if item_reasons:
            rejected.append(index)
            reasons.extend(item_reasons)

    if reasons:
        return _result(request=request, response=response, provider_name=provider_name, status="rejected", reason_codes=reasons, rejected_indexes=rejected)
    return _result(request=request, response=response, provider_name=provider_name, status="accepted", reason_codes=[], rejected_indexes=[])
