from __future__ import annotations

from app.sandman.providers.deterministic import DeterministicProvider
from mapi_core.sandman.validator import validate_provider_response
from tests.sandman_v3_helpers import candidate, request


def actions(req):
    return [item["action"] for item in DeterministicProvider().analyze(req)["proposals"]]


def test_hard_evidence_rules_and_abstain() -> None:
    assert actions(request([candidate(1, content_hash="same"), candidate(2, content_hash="same")])) == ["duplicate_of"]
    assert actions(request([candidate(1), candidate(2, supersedes_memory_id=1)])) == ["supersedes"]
    for relation in ("contradicts", "reinforces", "related_to"):
        req = request([candidate(1, allowlisted_links=[{"relation_type": relation, "target_memory_id": 2}]), candidate(2)])
        assert actions(req) == [relation]
    response = DeterministicProvider().analyze(request())
    assert response["abstain"] is True and response["proposals"] == []


def test_unavailable_action_is_skipped_and_budget_order_is_stable() -> None:
    candidates = [
        candidate(1, content_hash="same", allowlisted_links=[{"relation_type": "related_to", "target_memory_id": 2}]),
        candidate(2, content_hash="same"),
    ]
    req = request(candidates, allowed_actions=["related_to"], budget=1)
    first = DeterministicProvider().analyze(req)
    second = DeterministicProvider().analyze(req)
    assert first == second and actions(req) == ["related_to"]


def test_deterministic_output_uses_shared_validator() -> None:
    req = request([candidate(1, content_hash="same"), candidate(2, content_hash="same")])
    response = DeterministicProvider().analyze(req)
    assert validate_provider_response(req, response, provider_name="deterministic")["status"] == "accepted"
