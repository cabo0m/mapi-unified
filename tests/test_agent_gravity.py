from __future__ import annotations

from mapi_core.memory.agent_gravity import build_agent_gravity_preview, build_gravity_context_block, build_gravity_shadow_comparison, gravity_policy


def _candidate(memory_id, *, project="p", summary="release checklist", tags="", truth_kind="fact", state="active", rules=None, source_kinds=None, importance=0.8):
    return {
        "id": memory_id, "project_key": project, "summary_short": summary, "tags": tags,
        "truth_kind": truth_kind, "state_code": state, "importance_score": importance,
        "recall_count": 0, "source_kinds": source_kinds or [], "should_resurface_when": rules or [],
    }


def test_policy_is_read_only_and_bounded():
    policy = gravity_policy()
    assert policy["read_only"] is True
    assert policy["max_results"] == 12
    assert policy["max_context_items"] == 2
    assert policy["safety"]["gravity_can_create_durable_relation"] is False


def test_explicit_trigger_outranks_unrelated_high_importance():
    triggered = _candidate(1, summary="deployment", rules=["gravity release checklist"], importance=0.2)
    unrelated = _candidate(2, summary="unrelated high importance", importance=1.0)
    result = build_agent_gravity_preview(query="gravity release checklist", project_key="p", candidates=[unrelated, triggered])
    assert result["source_memory_ids"][0] == 1
    assert result["attractors"][0]["lane"] == "required"


def test_foreign_project_excluded_but_self_source_allowed():
    foreign = _candidate(1, project="q", summary="gravity release checklist", rules=["gravity release checklist"])
    self_item = _candidate(2, project="agent-self", summary="review before mutation", tags="guardrail", source_kinds=["self_capsule", "commitment_ledger"])
    result = build_agent_gravity_preview(query="review mutation", project_key="p", candidates=[foreign, self_item])
    assert 1 not in result["candidate_source_memory_ids"] or 1 not in result["source_memory_ids"]
    assert 2 in result["source_memory_ids"]


def test_unsafe_truth_and_terminal_non_milestone_are_excluded():
    dream = _candidate(1, truth_kind="dream", rules=["release checklist"])
    archived = _candidate(2, state="archived", rules=["release checklist"])
    milestone = _candidate(3, state="superseded", tags="milestone", summary="release checklist")
    result = build_agent_gravity_preview(query="release checklist", project_key="p", candidates=[dream, archived, milestone], include_debug=True)
    assert 1 not in result["source_memory_ids"]
    assert 2 not in result["source_memory_ids"]
    assert 3 in result["source_memory_ids"]


def test_context_injects_only_required_or_strong_noncanonical():
    payload = {"status": "ok", "attractors": [
        {"memory_id": 1, "lane": "required", "gravity_score": 0.9, "statement": "one", "source_memory_ids": [1]},
        {"memory_id": 2, "lane": "strong", "gravity_score": 0.8, "statement": "two", "source_memory_ids": [2]},
        {"memory_id": 3, "lane": "contextual", "gravity_score": 0.5, "statement": "three", "source_memory_ids": [3]},
    ]}
    block = build_gravity_context_block(gravity_payload=payload, canonical_source_memory_ids=[1], max_items=2)
    assert block["source_memory_ids"] == [2]
    assert block["safety"]["canonical_source_ids_unchanged"] is True


def test_shadow_preserves_canonical_top_and_reports_injection():
    payload = {"status": "ok", "attractors": [{"memory_id": 4, "lane": "required"}, {"memory_id": 5, "lane": "strong"}]}
    result = build_gravity_shadow_comparison(baseline_source_memory_ids=[1, 2, 3], gravity_payload=payload)
    assert result["canonical"]["source_memory_ids"] == [1, 2, 3]
    assert result["shadow"]["augmented_preview"]["source_memory_ids"] == [1, 4, 5, 2, 3]
    assert result["safety"]["canonical_baseline_preserved"] is True


def test_server_gravity_uses_neutral_self_sources(server, memory_factory, monkeypatch):
    monkeypatch.setenv("MAPI_AGENT_SUBJECT_KEY", "alpha")
    monkeypatch.setenv("MAPI_AGENT_PROJECT_KEY", "alpha-self")
    guardrail = memory_factory(
        content="Review before mutation", summary_short="Review before mutation", memory_type="guardrail", source="pytest",
        importance_score=0.9, confidence_score=1.0, tags="agent-self,subject:alpha,guardrail,safety",
        layer_code="core", area_code="meta", state_code="validated", scope_code="project", identity_weight=0.9,
        project_key="alpha-self", entry_type="decision", truth_kind="decision",
    )
    result = server.get_agent_gravity_preview(query="review mutation", project_key="demo-project", limit=8, include_debug=True)
    item = next(entry for entry in result["attractors"] if int(entry["memory_id"]) == guardrail)
    assert item["lane"] in {"required", "strong"}
    assert "self_model_source" in item["reason_codes"]
    assert "commitment_ledger" in item["source_kinds"]


def test_agent_context_includes_self_identity_and_commitment_without_gravity_duplicate(server, memory_factory, monkeypatch):
    monkeypatch.setenv("MAPI_AGENT_SUBJECT_KEY", "alpha")
    monkeypatch.setenv("MAPI_AGENT_PROJECT_KEY", "alpha-self")
    identity = memory_factory(
        content="Alpha is a careful agent", summary_short="Alpha is a careful agent", memory_type="identity", source="pytest",
        importance_score=0.95, confidence_score=1.0, tags="agent-self,subject:alpha", layer_code="identity", area_code="identity",
        state_code="validated", scope_code="project", identity_weight=1.0, project_key="alpha-self", entry_type="user_profile", truth_kind="fact",
    )
    guardrail = memory_factory(
        content="Review before mutation", summary_short="Review before mutation", memory_type="guardrail", source="pytest",
        importance_score=0.9, confidence_score=1.0, tags="agent-self,subject:alpha,guardrail,safety", layer_code="core", area_code="meta",
        state_code="validated", scope_code="project", identity_weight=0.9, project_key="alpha-self", entry_type="decision", truth_kind="decision",
    )
    memory_factory(
        content="Demo project mutation plan", summary_short="Demo project mutation plan", memory_type="project_note", source="pytest",
        importance_score=0.8, confidence_score=1.0, tags="demo", layer_code="projects", area_code="projects", state_code="validated",
        scope_code="project", project_key="demo-project", entry_type="project", truth_kind="fact",
    )
    result = server.build_agent_context(intent="review mutation plan", project_key="demo-project", token_budget=4000, include_debug=True)
    identity_sources = {sid for item in result["sections"]["identity"] for sid in item["source_memory_ids"]}
    commitment_sources = {sid for item in result["sections"]["commitments_guardrails"] for sid in item["source_memory_ids"]}
    gravity_sources = {sid for item in result["sections"]["gravity"] for sid in item["source_memory_ids"]}
    assert identity in identity_sources
    assert guardrail in commitment_sources
    assert guardrail not in gravity_sources
    assert result["debug"]["deferred_channels"] == []
