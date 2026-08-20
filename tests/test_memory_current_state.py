from __future__ import annotations

from typing import Any

import pytest

from mapi_core.memory.current_state import resolve_current_memory_state


def _direct(server: Any, *, content: str, project_key: str, **extra: Any) -> int:
    payload = {
        "content": content,
        "memory_type": "project_decision",
        "summary_short": content,
        "source": "pytest-current-state",
        "importance_score": 0.9,
        "confidence_score": 0.95,
        "tags": f"{project_key},current-state,decision",
        "state_code": "validated",
        "scope_code": "project",
        "project_key": project_key,
        "truth_kind": "decision",
        "entry_type": "decision",
        "memory_v2_status": "active",
    }
    payload.update(extra)
    result = server._create_memory_direct(**payload)
    assert result["status"] == "created", result
    return int(result["memory"]["id"])


def _row(server: Any, memory_id: int) -> dict[str, Any]:
    conn = server.get_db_connection()
    try:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def test_half_supersession_resolves_current_head_and_preserves_history(server: Any) -> None:
    project = "current-state-half"
    old_id = _direct(server, content="legacy unique routing policy", project_key=project)
    new_id = _direct(server, content="current replacement routing policy", project_key=project)
    conn = server.get_db_connection()
    try:
        conn.execute("UPDATE memories SET supersedes_memory_id = ? WHERE id = ?", (old_id, new_id))
        conn.commit()
    finally:
        conn.close()

    current = server.get_memory_current_state(old_id, include_history=True, include_debug=True)
    assert current["current"]["id"] == new_id
    assert [item["id"] for item in current["history"]] == [old_id]
    assert current["current"]["current_state"]["lineage_ids"] == [old_id, new_id]
    assert current["current"]["current_state"]["matched_history_ids"] == [old_id]
    assert {item["issue_code"] for item in current["issues"]} == {"half_supersession"}

    found = server.find_memories(
        "legacy unique routing policy",
        project_key=project,
        project_key_mode="exact",
        limit=10,
    )
    assert [item["id"] for item in found["items"]] == [new_id]

    historical = server.find_memories(
        "legacy unique routing policy",
        project_key=project,
        project_key_mode="exact",
        limit=10,
        include_history=True,
    )
    assert {item["id"] for item in historical["items"]} == {old_id, new_id}


def test_direct_full_supersession_is_symmetric_and_audited(server: Any) -> None:
    project = "current-state-full"
    old_id = _direct(server, content="full old decision", project_key=project)
    new_id = _direct(
        server,
        content="full new decision",
        project_key=project,
        supersedes_memory_id=old_id,
    )

    old = _row(server, old_id)
    new = _row(server, new_id)
    assert int(new["supersedes_memory_id"]) == old_id
    assert int(old["superseded_by_memory_id"]) == new_id
    assert old["state_code"] == "superseded"
    assert old["memory_v2_status"] == "superseded"
    assert old["activity_state"] == "superseded"
    assert old["valid_to"]

    conn = server.get_db_connection()
    try:
        link = conn.execute(
            "SELECT relation_type, origin FROM memory_links WHERE from_memory_id = ? AND to_memory_id = ?",
            (new_id, old_id),
        ).fetchone()
        events = conn.execute(
            "SELECT memory_id, event_type FROM memory_events WHERE memory_id IN (?, ?) ORDER BY id",
            (old_id, new_id),
        ).fetchall()
    finally:
        conn.close()
    assert dict(link)["relation_type"] == "supersedes"
    event_types = {(int(row["memory_id"]), row["event_type"]) for row in events}
    assert (new_id, "version.supersession_applied") in event_types
    assert (old_id, "version.superseded") in event_types

    current = server.get_memory_current_state(old_id, include_history=True)
    assert current["current"]["id"] == new_id
    assert not current["issues"]


def test_partial_refinement_keeps_both_current_and_requires_scope(server: Any) -> None:
    project = "current-state-partial"
    old_id = _direct(
        server,
        content="Which provider should handle alpha beta?",
        project_key=project,
        memory_type="open_question",
        truth_kind="proposal",
        tags=f"{project},alpha,beta,open-question,blocked",
    )
    new_id = _direct(
        server,
        content="Alpha beta provider decision is complete",
        project_key=project,
        tags=f"{project},alpha,beta,resolved",
        supersedes_memory_id=old_id,
        supersession_relation="refines",
        supersession_scope="Resolves provider choice but preserves the historical question context.",
    )

    old = _row(server, old_id)
    new = _row(server, new_id)
    assert old["state_code"] == "validated"
    assert old["superseded_by_memory_id"] is None
    assert new["supersedes_memory_id"] is None

    conn = server.get_db_connection()
    try:
        link = conn.execute(
            "SELECT relation_type FROM memory_links WHERE from_memory_id = ? AND to_memory_id = ?",
            (new_id, old_id),
        ).fetchone()
        rows = conn.execute("SELECT * FROM memories WHERE id IN (?, ?) ORDER BY id", (old_id, new_id)).fetchall()
        projection = resolve_current_memory_state(conn, [dict(row) for row in rows], include_history=True)
    finally:
        conn.close()
    assert link["relation_type"] == "refines"
    assert projection["resolved_question_ids"] == [old_id]
    assert projection["items"][0]["id"] == new_id
    assert old_id in {item["id"] for item in projection["history"]}

    failed = server._create_memory_direct(
        content="partial without scope",
        memory_type="project_decision",
        project_key=project,
        scope_code="project",
        truth_kind="decision",
        supersedes_memory_id=new_id,
        supersession_relation="partially_supersedes",
    )
    assert failed["status"] == "error"
    assert "supersession_scope" in failed["error"]


def test_branching_is_deterministic_and_cross_project_pointer_is_ignored(server: Any) -> None:
    project = "current-state-branch"
    old_id = _direct(server, content="branch root", project_key=project)
    first_id = _direct(server, content="branch first", project_key=project)
    second_id = _direct(server, content="branch second", project_key=project)
    foreign_id = _direct(server, content="foreign replacement", project_key="other-current-state-project")
    conn = server.get_db_connection()
    try:
        conn.execute("UPDATE memories SET supersedes_memory_id = ? WHERE id IN (?, ?)", (old_id, first_id, second_id))
        conn.execute("UPDATE memories SET supersedes_memory_id = ? WHERE id = ?", (old_id, foreign_id))
        rows = conn.execute("SELECT * FROM memories WHERE id = ?", (old_id,)).fetchall()
        conn.commit()
        projection = resolve_current_memory_state(conn, [dict(rows[0])], include_history=True)
    finally:
        conn.close()

    assert projection["items"][0]["id"] == second_id
    issue_codes = {item["issue_code"] for item in projection["issues"]}
    assert "multiple_replacement_heads" in issue_codes
    assert "cross_domain_pointer_ignored" in issue_codes


def test_future_valid_to_is_current_and_past_valid_to_is_history(server: Any) -> None:
    project = "current-state-validity"
    future_id = _direct(server, content="future-valid decision", project_key=project, valid_to="2099-01-01T00:00:00Z")
    past_id = _direct(server, content="expired decision", project_key=project, valid_to="2020-01-01T00:00:00Z")
    conn = server.get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM memories WHERE id IN (?, ?) ORDER BY id", (future_id, past_id)).fetchall()
        projection = resolve_current_memory_state(conn, [dict(row) for row in rows], include_history=False)
    finally:
        conn.close()
    assert [item["id"] for item in projection["items"]] == [future_id]
    assert [item["id"] for item in projection["history"]] == [past_id]


def test_project_surfaces_use_the_same_current_state_projection(server: Any) -> None:
    project = "current-state-surfaces"
    old_id = _direct(
        server,
        content="legacy surface policy unique phrase",
        project_key=project,
        tags=f"{project},surface-policy,decision",
    )
    new_id = _direct(
        server,
        content="current surface policy replacement",
        project_key=project,
        tags=f"{project},surface-policy,decision,resolved",
        supersedes_memory_id=old_id,
    )

    found = server.find_memories("legacy surface policy unique phrase", project_key=project, limit=10)
    brief = server.get_project_brief(project, limit=8, include_debug=True)
    card = server.get_project_card(project, limit=8, include_debug=True)
    restore = server.get_memory_restore_ritual(project, full=True, include_debug=True)
    changes = server.get_recent_project_changes(project, limit=10, include_debug=True)

    assert [item["id"] for item in found["items"]] == [new_id]
    assert new_id in {item["id"] for item in brief["decisions"]}
    assert old_id not in {item["id"] for item in brief["decisions"]}
    assert new_id in {item["id"] for item in card["decision_links"]}
    assert old_id not in {item["id"] for item in card["decision_links"]}
    assert new_id in {item["id"] for item in restore["remembered"]}
    assert old_id not in {item["id"] for item in restore["remembered"]}
    assert {old_id, new_id}.issubset({item["id"] for item in changes["items"]})


def test_later_resolved_decision_closes_open_question_everywhere(server: Any) -> None:
    project = "current-state-question"
    old_id = _direct(
        server,
        content="Pointer alpha beta remains blocked and requires decision",
        project_key=project,
        memory_type="open_question",
        truth_kind="proposal",
        tags=f"{project},pointer-alpha,beta,blocker,open-question",
    )
    new_id = _direct(
        server,
        content="Pointer alpha beta decision completed",
        project_key=project,
        tags=f"{project},pointer-alpha,beta,resolved,completed",
    )

    found = server.find_memories("requires decision", project_key=project, limit=10)
    brief = server.get_project_brief(project, limit=8)
    card = server.get_project_card(project, limit=8)
    restore = server.get_memory_restore_ritual(project, full=True)

    assert [item["id"] for item in found["items"]] == [new_id]
    assert old_id not in {item["id"] for item in brief["open_questions"]}
    assert old_id not in {item.get("id") for item in restore["uncertain"]}
    assert card["project_card"]["open_questions"] == []


def test_inventory_and_workshop_expose_current_state_diagnostics(server: Any, monkeypatch) -> None:
    project = "current-state-inventory"
    old_id = _direct(server, content="inventory old", project_key=project)
    new_id = _direct(server, content="inventory new", project_key=project)
    conn = server.get_db_connection()
    try:
        conn.execute("UPDATE memories SET supersedes_memory_id = ? WHERE id = ?", (old_id, new_id))
        conn.commit()
    finally:
        conn.close()

    inventory = server.get_memory_current_state_inventory(project, limit=50, include_debug=True)
    assert inventory["status"] == "attention"
    assert inventory["summary"]["issue_counts"] == {"half_supersession": 1}
    assert any(lineage["memory_ids"] == [old_id, new_id] for lineage in inventory["lineages"])

    monkeypatch.setenv("MCP_SURFACE_PROFILE", "reader")
    workshop = server.open_workshop("memory")
    actions = {item["action"]: item for item in workshop["actions"]}
    assert actions["current_state"]["risk_class"] == "R0"
    assert actions["current_state_inventory"]["access_requirement"] == "reader"


@pytest.mark.parametrize("relation", ["supersedes", "refines", "partially_supersedes"])
def test_direct_transition_rejects_cross_project_lineage(server: Any, relation: str) -> None:
    old_id = _direct(server, content="cross project old", project_key="current-state-project-a")
    result = server._create_memory_direct(
        content="cross project new",
        memory_type="project_decision",
        project_key="current-state-project-b",
        scope_code="project",
        truth_kind="decision",
        supersedes_memory_id=old_id,
        supersession_relation=relation,
        supersession_scope="narrow scope" if relation != "supersedes" else None,
    )
    assert result["status"] == "error"
    assert "cross project" in result["error"]
