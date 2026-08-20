from __future__ import annotations

import copy

import pytest

from mapi_core.sandman.contracts import ContractError, SAFETY_BLOCK, build_provider_request, parse_provider_response, validate_provider_request
from tests.sandman_v3_helpers import candidate, request


def test_valid_request_normalizes_ids_actions_and_fingerprint() -> None:
    req = request([candidate(2), candidate(1)], allowed_actions=["related_to", "duplicate_of", "related_to"])
    assert req["candidate_memory_ids"] == [1, 2]
    assert req["allowed_actions"] == ["duplicate_of", "related_to"]
    assert validate_provider_request(req) == req


def test_unknown_field_and_invalid_safety_are_rejected() -> None:
    bad = request(); bad["extra"] = True
    with pytest.raises(ContractError): validate_provider_request(bad)
    bad = request(); bad["safety"] = {**SAFETY_BLOCK, "database_access": True}
    with pytest.raises(ContractError): validate_provider_request(bad)


def test_limits_and_fingerprint_drift() -> None:
    with pytest.raises(ContractError): request([candidate(i) for i in range(1, 22)])
    with pytest.raises(ContractError): request(budget=9)
    first = request(); second = request(request_id="req-2")
    changed = request([candidate(1, content="changed"), candidate(2)])
    assert first["input_fingerprint"] != second["input_fingerprint"] != changed["input_fingerprint"]


def test_response_parser_is_strict_and_never_repairs_prose() -> None:
    valid = {"schema_version": "sandman_provider_response.v1", "request_id": "r", "input_fingerprint": "f", "abstain": True, "proposals": [], "unsupported_metrics": []}
    assert parse_provider_response(valid)["abstain"] is True
    for raw in ("prose", "```json\n{}\n```", '{"schema_version":"sandman_provider_response.v1"}'):
        with pytest.raises(ContractError): parse_provider_response(raw)
