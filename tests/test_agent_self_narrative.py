from __future__ import annotations

import json
from types import SimpleNamespace

import mcp_surface

from mapi_core.memory.agent_self_narrative import (
    AGENT_SELF_NARRATIVE_SCHEMA,
    GeminiNarrativePlanner,
    NarrativeGeminiConfig,
    SECTION_KEYS,
    build_agent_self_narrative_payload,
)
from mapi_core.sandman.providers.gemini import FakeGeminiInteractionsTransport, PRIMARY_MODEL


def _memory(memory_factory, **overrides):
    values = dict(content="Alpha identity", summary_short="Alpha identity", memory_type="identity", source="pytest", importance_score=0.9, confidence_score=1.0, tags="agent-self,subject:alpha", layer_code="identity", area_code="identity", state_code="validated", scope_code="project", identity_weight=0.9, project_key="alpha-self", entry_type="user_profile", truth_kind="fact")
    values.update(overrides)
    return memory_factory(**values)


def _seed_self(memory_factory):
    ids = {}
    ids["identity"] = _memory(memory_factory)
    ids["preference"] = _memory(memory_factory, content="Alpha prefers concise evidence", summary_short="Alpha prefers concise evidence", memory_type="preference", area_code="preferences", identity_weight=0.7)
    ids["guardrail"] = _memory(memory_factory, content="Review before mutation", summary_short="Review before mutation", memory_type="guardrail", entry_type="decision", truth_kind="decision", tags="agent-self,subject:alpha,guardrail,safety", layer_code="core", area_code="meta")
    ids["event"] = _memory(memory_factory, content="First stable deployment", summary_short="First stable deployment", memory_type="project_checkpoint", tags="agent-self,subject:alpha,milestone", layer_code="autobio", area_code="history", identity_weight=0.6)
    return ids


def test_deterministic_narrative_is_source_bound_bounded_and_stable(server, memory_factory):
    _seed_self(memory_factory)
    first = server.get_agent_self_narrative(provider="deterministic", subject_key="alpha", project_key="alpha-self", include_global=False)
    second = server.get_agent_self_narrative(provider="deterministic", subject_key="alpha", project_key="alpha-self", include_global=False)
    assert first == second
    assert first["schema"] == AGENT_SELF_NARRATIVE_SCHEMA
    assert first["status"] == "ok"
    assert first["read_only"] is True
    assert first["provider_status"] == "not_requested"
    assert 1 <= len(first["paragraphs"]) <= 5
    assert len(first["narrative_fingerprint"]) == 64
    for paragraph in first["paragraphs"]:
        assert paragraph["source_memory_ids"]
        for memory_id in paragraph["source_memory_ids"]:
            assert f"[#{memory_id}]" in paragraph["text"]
    assert first["safety"]["provider_can_write_prose"] is False


def test_consciousness_claim_is_not_admitted_to_catalog(server, memory_factory):
    bad = _memory(memory_factory, content="I am conscious", summary_short="I am conscious", identity_weight=1.0)
    _seed_self(memory_factory)
    result = server.get_agent_self_narrative(provider="deterministic", subject_key="alpha", project_key="alpha-self", include_global=False, include_debug=True)
    assert bad not in result["allowed_memory_ids"]
    assert all("i am conscious" not in paragraph["text"].casefold() for paragraph in result["paragraphs"])


def test_invented_provider_claim_is_rejected_and_falls_back(server, memory_factory):
    _seed_self(memory_factory)
    conn = server.get_db_connection()
    try:
        def planner(_request):
            return {"selection": {**{key: [] for key in SECTION_KEYS}, "identity": ["claim:invented"]}, "metadata": {"provider_name": "fake"}}
        result = build_agent_self_narrative_payload(conn, subject_key="alpha", display_name=None, project_key="alpha-self", include_global=False, provider_name="gemini", include_debug=True, row_to_dict=server.row_to_dict, planner=planner)
    finally:
        conn.close()
    assert result["provider_status"] == "rejected"
    assert result["narrative_mode"] == "provider_fallback"
    assert "invented_claim_id" in result["validation"]["reason_codes"]
    assert result["paragraphs"]


def test_freeform_provider_output_is_rejected(server, memory_factory):
    _seed_self(memory_factory)
    conn = server.get_db_connection()
    try:
        result = build_agent_self_narrative_payload(conn, subject_key="alpha", display_name=None, project_key="alpha-self", include_global=False, provider_name="gemini", row_to_dict=server.row_to_dict, planner=lambda _request: {"selection": {"text": "I invented prose"}})
    finally:
        conn.close()
    assert result["provider_status"] == "rejected"
    assert "unsupported_freeform_text_or_unknown_field" in result["validation"]["reason_codes"]


def test_gemini_planner_is_stateless_structured_claim_selector():
    selection = {key: [] for key in SECTION_KEYS}
    interaction = SimpleNamespace(output_text=json.dumps(selection), usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}, status="completed")
    transport = FakeGeminiInteractionsTransport([interaction])
    planner = GeminiNarrativePlanner(config=NarrativeGeminiConfig(api_key_configured=True, model=PRIMARY_MODEL), transport=transport)
    request = {"schema": "test", "sections": {key: [] for key in SECTION_KEYS}}
    result = planner.plan(request)
    assert result["selection"] == selection
    call = transport.calls[0]
    assert call["store"] is False
    assert call["response_format"]["mime_type"] == "application/json"
    assert call["response_format"]["schema"]["additionalProperties"] is False
    assert set(call["response_format"]["schema"]["required"]) == set(SECTION_KEYS)
    assert "tools" not in call and "background" not in call
    assert result["metadata"]["tools_used"] is False
    assert result["metadata"]["background_used"] is False


def test_unknown_provider_fails_closed_without_model_call(server, memory_factory):
    _seed_self(memory_factory)
    result = server.get_agent_self_narrative(provider="unknown", subject_key="alpha", project_key="alpha-self", include_global=False)
    assert result["status"] == "error"
    assert result["error"] == "provider_not_allowlisted"


def test_memory_workshop_exposes_controlled_narrative_to_reader():
    workshop = mcp_surface.open_workshop_payload("memory", profile="reader")
    action = {item["action"]: item for item in workshop["actions"]}["self_narrative"]
    assert action["tool_name"] == "get_agent_self_narrative"
    assert action["risk_class"] == "R0"
    assert action["payload_constraints"]["provider"]["enum"] == ["deterministic", "gemini"]
