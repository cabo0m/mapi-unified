from __future__ import annotations

import copy

from mapi_core.sandman.validator import validate_provider_response
from tests.sandman_v3_helpers import candidate, request


def proposal(action="related_to", source=1, target=2, **values):
    item = {"proposal_id": "p-1", "action": action, "source_memory_ids": [source], "target_memory_id": target, "confidence": 1.0, "evidence_memory_ids": sorted([source, target]), "reason": "Explicit local relation evidence."}
    item.update(values); return item


def response(req, proposals=None, *, abstain=False, **values):
    item = {"schema_version": "sandman_provider_response.v1", "request_id": req["request_id"], "input_fingerprint": req["input_fingerprint"], "abstain": abstain, "proposals": proposals or [], "unsupported_metrics": []}
    item.update(values); return item


def rejected_code(req, resp, code):
    result = validate_provider_response(req, resp, provider_name="test")
    assert result["status"] == "rejected" and code in result["reason_codes"]
    assert result["normalized_proposals"] == []


def test_identity_allowlist_action_and_evidence_rejections() -> None:
    req = request()
    rejected_code(req, response(req, [proposal()], request_id="other"), "request_id_mismatch")
    rejected_code(req, response(req, [proposal()], input_fingerprint="sha256:bad"), "input_fingerprint_mismatch")
    rejected_code(req, response(req, [proposal(target=999, evidence_memory_ids=[1, 999])]), "memory_id_not_allowlisted")
    rejected_code(req, response(req, [proposal(action="archive")]), "forbidden_action")
    rejected_code(req, response(req, [proposal(evidence_memory_ids=[1])]), "invalid_evidence")


def test_confidence_duplicates_budget_and_abstain_reject_whole_response() -> None:
    req = request(budget=1)
    rejected_code(req, response(req, [proposal(confidence=2.0)]), "invalid_confidence")
    duplicate = proposal(); duplicate2 = {**proposal(), "proposal_id": "p-2"}
    rejected_code(req, response(req, [duplicate, duplicate2]), "proposal_budget_overflow")
    rejected_code(req, response(req, [proposal()], abstain=True), "abstain_proposals_mismatch")


def test_dream_fact_boundary_and_related_exception() -> None:
    req = request([candidate(1, artifact_kind="dream"), candidate(2, artifact_kind="fact")])
    rejected_code(req, response(req, [proposal(action="supersedes")]), "dream_fact_boundary_violation")
    accepted = validate_provider_response(req, response(req, [proposal(action="related_to")]), provider_name="test")
    assert accepted["status"] == "accepted"


def test_sensitive_reason_rejects_without_echo_and_one_bad_rejects_all() -> None:
    req = request()
    secret = "api_key=abcdefghijklmnop"
    result = validate_provider_response(req, response(req, [proposal(reason=secret)]), provider_name="test")
    assert result["status"] == "rejected" and "response_sensitive_echo" in result["reason_codes"]
    assert secret not in repr(result)
    mixed = response(req, [proposal(), {**proposal(target=999, evidence_memory_ids=[1, 999]), "proposal_id": "p-2"}])
    assert validate_provider_response(req, mixed, provider_name="test")["normalized_proposals"] == []
