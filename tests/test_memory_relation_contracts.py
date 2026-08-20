from __future__ import annotations

from typing import Any

import mcp_surface

from mapi_core.memory.relation_contracts import CANONICAL_MEMORY_RELATIONS


def _counts(server: Any) -> dict[str, int]:
    conn = server.get_db_connection()
    try:
        return {
            "links": int(conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]),
            "events": int(conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]),
            "sleep_runs": int(conn.execute("SELECT COUNT(*) FROM sleep_runs").fetchone()[0]),
            "sleep_actions": int(conn.execute("SELECT COUNT(*) FROM sleep_run_actions").fetchone()[0]),
        }
    finally:
        conn.close()


def _memory(server: Any, memory_factory, *, project: str, summary: str, source_event_ref: str | None = None) -> int:
    return int(
        memory_factory(
            content=summary,
            memory_type="project_checkpoint",
            summary_short=summary,
            source="pytest",
            importance_score=0.8,
            confidence_score=1.0,
            tags="R5B,relation-contract",
            layer_code="projects",
            area_code="projects",
            state_code="validated",
            scope_code="project",
            project_key=project,
            source_event_ref=source_event_ref,
        )
    )


def test_contract_catalog_contains_exact_canonical_relation_set(server: Any) -> None:
    payload = server.get_memory_relation_contracts()
    assert payload["status"] == "ok"
    assert tuple(payload["allowed_values"]) == CANONICAL_MEMORY_RELATIONS
    assert [item["relation"] for item in payload["relations"]] == list(CANONICAL_MEMORY_RELATIONS)
    assert payload["invariants"] == {
        "semantic_similarity_alone_can_create_durable_relation": False,
        "legacy_link_memories_is_not_canonical_relation_apply": True,
        "new_truth_queue_created": False,
        "supports_and_derived_from_guarded_apply": True,
    }
    assert all(item["semantic_similarity_alone_allowed"] is False for item in payload["relations"])
    assert all(item["direct_link_apply_allowed"] is False for item in payload["relations"])


def test_contracts_are_honest_about_existing_storage_routes(server: Any) -> None:
    by_relation = {
        item["relation"]: item
        for item in server.get_memory_relation_contracts()["relations"]
    }

    supports = by_relation["supports"]
    assert supports["status"] == "implemented_guarded"
    assert supports["storage_model"] == "memory_link_plus_audit_events"
    assert supports["durable_memory_link_relation"] == "supports"
    assert "relation_apply" in supports["existing_route"]
    assert "reinforcement" in supports["existing_route"]

    contradicts = by_relation["contradicts"]
    assert contradicts["status"] == "implemented_reviewed"
    assert contradicts["durable_memory_link_relation"] == "contradicts"
    assert "conflict_review" in contradicts["existing_route"]

    supersedes = by_relation["supersedes"]
    assert supersedes["status"] == "implemented_guarded"
    assert supersedes["durable_memory_link_relation"] == "supersedes"
    assert supersedes["additional_structural_mirror"] == "supersedes_memory_id"

    refines = by_relation["refines"]
    assert refines["status"] == "implemented_as_lifecycle_projection"
    assert refines["durable_memory_link_relation"] == "supersedes"
    assert refines["lifecycle_relation_kind"] == "refinement"

    derived = by_relation["derived_from"]
    assert derived["status"] == "implemented_guarded"
    assert derived["storage_model"] == "memory_link_plus_audit_events"
    assert derived["durable_memory_link_relation"] == "derived_from"
    assert derived["allowed_evidence_kinds"] == ["explicit_source_memory_reference"]
    assert "read_model_source_memory_ids" in derived["forbidden_inferences"]
    assert "source_event_ref_only" in derived["forbidden_inferences"]

    about = by_relation["about_project"]
    assert about["status"] == "implemented_virtual"
    assert about["storage_model"] == "memories.project_key"
    assert about["durable_memory_link_relation"] is None
    assert about["virtual_relation"] is True


def test_unsupported_relation_returns_allowed_values(server: Any) -> None:
    result = server.get_memory_relation_contracts("causes")
    assert result["status"] == "error"
    assert result["error"] == "unsupported_canonical_relation"
    assert result["allowed_values"] == list(CANONICAL_MEMORY_RELATIONS)


def test_about_project_is_virtual_without_link_write(server: Any, memory_factory) -> None:
    memory_id = _memory(server, memory_factory, project="demo-project", summary="R5B about-project target")
    before = _counts(server)
    preview = server.preview_memory_relation(
        relation="about_project",
        from_memory_id=memory_id,
        project_key="demo-project",
    )
    after = _counts(server)

    assert preview["status"] == "virtual_relation_present"
    assert preview["requested_project_key"] == "demo-project"
    assert preview["canonical_project_key"] == "demo-project"
    assert preview["evidence"]["canonical_project_key"] == "demo-project"
    assert preview["evidence"]["virtual_relation_present"] is True
    assert preview["contract"]["storage_model"] == "memories.project_key"
    assert preview["apply"]["eligible"] is False
    assert before == after


def test_derived_from_requires_explicit_source_memory_reference(server: Any, memory_factory) -> None:
    left = _memory(server, memory_factory, project="demo-project", summary="Derived candidate alpha")
    right = _memory(server, memory_factory, project="demo-project", summary="Derived candidate beta")
    before = _counts(server)
    preview = server.preview_memory_relation(
        "derived_from",
        left,
        right,
        evidence_kind="explicit_source_memory_reference",
        evidence_ref=f"memory:{right}",
        reason="Explicit source-memory provenance for the derived memory.",
    )
    after = _counts(server)

    assert preview["status"] == "preview_ready"
    assert preview["contract"]["status"] == "implemented_guarded"
    assert preview["evidence"]["explicit_source_memory_reference"] is True
    assert preview["apply"]["eligible"] is True
    assert preview["apply"]["blocking_reasons"] == []
    assert preview["safety"]["semantic_similarity_used_as_evidence"] is False
    assert before == after


def test_supports_same_source_event_ref_is_guarded_apply_candidate(server: Any, memory_factory) -> None:
    source_ref = "pytest:r5d:reinforcement-source"
    left = _memory(server, memory_factory, project="demo-project", summary="Support candidate alpha", source_event_ref=source_ref)
    right = _memory(server, memory_factory, project="demo-project", summary="Support candidate beta", source_event_ref=source_ref)

    preview = server.preview_memory_relation(
        "supports",
        left,
        right,
        evidence_kind="same_source_event_ref",
        evidence_ref=source_ref,
        reason="Both memories are independently grounded in the same durable source event.",
    )

    assert preview["evidence"]["same_source_event_ref"] is True
    assert preview["apply"]["eligible"] is True
    assert preview["apply"]["blocking_reasons"] == []
    assert "relation_apply" in preview["contract"]["existing_route"]
    assert preview["contract"]["durable_memory_link_relation"] == "supports"


def test_contradicts_requires_review_even_for_compatible_domain(server: Any, memory_factory) -> None:
    left = _memory(server, memory_factory, project="demo-project", summary="Conflict candidate alpha")
    right = _memory(server, memory_factory, project="demo-project", summary="Conflict candidate beta")

    preview = server.preview_memory_relation("contradicts", left, right)

    assert preview["evidence"]["same_domain"] is True
    assert preview["evidence"]["explicit_target_compatible"] is True
    assert preview["apply"]["eligible"] is False
    assert preview["apply"]["blocking_reasons"] == ["requires_approved_capture_conflict_review"]
    assert preview["contract"]["durable_memory_link_relation"] == "contradicts"


def test_supersedes_preview_requires_structural_pointer_for_direct_mirror(server: Any, memory_factory) -> None:
    old_id = _memory(server, memory_factory, project="demo-project", summary="Superseded old memory")
    new_id = _memory(server, memory_factory, project="demo-project", summary="Superseding new memory")

    missing = server.preview_memory_relation("supersedes", new_id, old_id)
    assert missing["apply"]["eligible"] is False
    assert "structural_pointer_missing_for_direct_mirror" in missing["apply"]["blocking_reasons"]

    conn = server.get_db_connection()
    try:
        conn.execute("UPDATE memories SET supersedes_memory_id=? WHERE id=?", (old_id, new_id))
        conn.commit()
    finally:
        conn.close()

    present = server.preview_memory_relation("supersedes", new_id, old_id)
    assert present["evidence"]["supersedes_pointer_match"] is True
    assert present["apply"]["eligible"] is True
    assert present["apply"]["blocking_reasons"] == []
    assert "supersession_preview/apply" in present["apply"]["route"]


def test_refines_preview_exposes_real_storage_projection_not_second_refines_link(server: Any, memory_factory) -> None:
    old_id = _memory(server, memory_factory, project="demo-project", summary="Refinement old memory")
    new_id = _memory(server, memory_factory, project="demo-project", summary="Refinement new memory")

    preview = server.preview_memory_relation("refines", new_id, old_id)

    assert preview["evidence"]["storage_projection"] == {
        "relation_type": "supersedes",
        "relation_kind": "refinement",
    }
    assert preview["contract"]["durable_memory_link_relation"] == "supersedes"
    assert preview["contract"]["lifecycle_relation_kind"] == "refinement"
    assert preview["apply"]["eligible"] is False
    assert preview["apply"]["blocking_reasons"] == ["use_guarded_supersession_route_with_relation_kind_refinement"]


def test_all_relation_previews_are_read_only(server: Any, memory_factory) -> None:
    left = _memory(server, memory_factory, project="demo-project", summary="Read-only relation left", source_event_ref="pytest:r5b:readonly")
    right = _memory(server, memory_factory, project="demo-project", summary="Read-only relation right", source_event_ref="pytest:r5b:readonly")
    before = _counts(server)

    supports = server.preview_memory_relation(
        "supports", left, right,
        evidence_kind="same_source_event_ref", evidence_ref="pytest:r5b:readonly", reason="same source",
    )
    derived = server.preview_memory_relation(
        "derived_from", left, right,
        evidence_kind="explicit_source_memory_reference", evidence_ref=f"memory:{right}", reason="explicit source",
    )
    for preview in (supports, server.preview_memory_relation("contradicts", left, right), server.preview_memory_relation("refines", left, right), derived):
        assert preview["safety"]["read_only"] is True
        assert preview["safety"]["mutations_performed"] == 0
        assert preview["safety"]["semantic_similarity_used_as_evidence"] is False
    server.preview_memory_relation("about_project", from_memory_id=left, project_key="demo-project")
    after = _counts(server)
    assert before == after


def test_memory_workshop_exposes_relation_contract_and_preview() -> None:
    workshop = mcp_surface.open_workshop_payload("memory", profile="reader")
    actions = {item["action"]: item for item in workshop["actions"]}

    contracts = actions["relation_contracts"]
    assert contracts["risk_class"] == "R0"
    assert contracts["access_requirement"] == "reader"
    assert contracts["payload_constraints"]["relation"]["enum"] == list(CANONICAL_MEMORY_RELATIONS)

    preview = actions["relation_preview"]
    assert preview["risk_class"] == "R0"
    assert preview["access_requirement"] == "reader"
    assert preview["payload_constraints"]["relation"]["enum"] == list(CANONICAL_MEMORY_RELATIONS)
    assert "relation_apply" not in actions
    assert actions["relation_rollback_preview"]["risk_class"] == "R0"

    maintainer = mcp_surface.open_workshop_payload("memory", profile="maintainer")
    maintainer_actions = {item["action"]: item for item in maintainer["actions"]}
    assert set(maintainer_actions["relation_apply"]["payload_constraints"]["relation"]["enum"]) == {"supports", "derived_from"}
    assert maintainer_actions["relation_apply"]["risk_class"] == "R2"
    assert maintainer_actions["relation_apply"]["access_requirement"] == "maintainer"
    assert maintainer_actions["relation_rollback"]["risk_class"] == "R2"
    assert maintainer_actions["relation_rollback"]["access_requirement"] == "maintainer"
