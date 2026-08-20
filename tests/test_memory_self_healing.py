from __future__ import annotations

from typing import Any

from mapi_core.memory import self_healing


def _direct(server: Any, *, content: str, project_key: str) -> int:
    result = server._create_memory_direct(
        content=content,
        memory_type="project_decision",
        summary_short=content,
        source="pytest-self-healing",
        importance_score=0.75,
        confidence_score=0.95,
        tags=f"{project_key},self-healing,decision",
        state_code="validated",
        scope_code="project",
        project_key=project_key,
        truth_kind="decision",
        entry_type="decision",
        memory_v2_status="active",
    )
    assert result["status"] == "created", result
    return int(result["memory"]["id"])


def _scan(server: Any) -> dict[str, Any]:
    conn = server.get_db_connection()
    try:
        result = self_healing.scan_self_healing_issues(conn)
        conn.commit()
        return result
    finally:
        conn.close()


def test_half_supersession_is_repaired_silently_and_content_is_preserved(server: Any) -> None:
    project = "self-healing-half"
    old_id = _direct(server, content="old routing decision", project_key=project)
    new_id = _direct(server, content="new routing decision", project_key=project)
    conn = server.get_db_connection()
    try:
        conn.execute("UPDATE memories SET supersedes_memory_id=? WHERE id=?", (old_id, new_id))
        conn.commit()
    finally:
        conn.close()

    scan = _scan(server)
    assert scan["issue_count"] == 1

    conn = server.get_db_connection()
    try:
        result = self_healing.repair_deterministic_issues(conn, insert_event=server.insert_memory_event)
        conn.commit()
    finally:
        conn.close()
    assert result["repaired_count"] == 1
    assert result["blocked_count"] == 0

    inventory = server.get_memory_current_state_inventory(project, limit=50)
    assert inventory["summary"]["issue_count"] == 0
    conn = server.get_db_connection()
    try:
        old = conn.execute("SELECT * FROM memories WHERE id=?", (old_id,)).fetchone()
        new = conn.execute("SELECT * FROM memories WHERE id=?", (new_id,)).fetchone()
        issue = conn.execute(
            "SELECT status FROM memory_self_healing_issues WHERE issue_kind='half_supersession'"
        ).fetchone()
    finally:
        conn.close()
    assert old["content"] == "old routing decision"
    assert new["content"] == "new routing decision"
    assert int(old["superseded_by_memory_id"]) == new_id
    assert old["state_code"] == "superseded"
    assert issue["status"] == "repaired"


def test_ambiguous_branch_is_model_proposed_then_user_confirmed(server: Any) -> None:
    project = "self-healing-branch"
    old_id = _direct(server, content="root fact", project_key=project)
    first_id = _direct(server, content="first possible current fact", project_key=project)
    second_id = _direct(server, content="second intended current fact", project_key=project)
    conn = server.get_db_connection()
    try:
        conn.execute(
            "UPDATE memories SET supersedes_memory_id=? WHERE id IN (?,?)",
            (old_id, first_id, second_id),
        )
        conn.commit()
    finally:
        conn.close()

    _scan(server)
    conn = server.get_db_connection()
    try:
        row = conn.execute(
            "SELECT id,status FROM memory_self_healing_issues "
            "WHERE issue_kind='multiple_replacement_heads' ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "awaiting_model"
    issue_id = int(row["id"])

    proposal = server.propose_memory_self_healing_resolution(
        issue_id,
        second_id,
        0.96,
        "The second candidate is the explicitly intended current version.",
    )
    assert proposal["status"] == "awaiting_user"
    assert proposal["requires_user_confirmation"] is True
    assert proposal["proposal"]["selected_memory_id"] == second_id

    confirmed = server.confirm_memory_self_healing_resolution(issue_id, True)
    assert confirmed["status"] == "repaired"
    assert confirmed["result"]["content_deleted"] is False
    assert confirmed["post_repair_current_state"]["summary"]["critical_issue_count"] == 0
    assert confirmed["backup"]["status"] == "verified"

    conn = server.get_db_connection()
    try:
        old = conn.execute("SELECT * FROM memories WHERE id=?", (old_id,)).fetchone()
        first = conn.execute("SELECT * FROM memories WHERE id=?", (first_id,)).fetchone()
        second = conn.execute("SELECT * FROM memories WHERE id=?", (second_id,)).fetchone()
        issue = conn.execute("SELECT status,user_decision FROM memory_self_healing_issues WHERE id=?", (issue_id,)).fetchone()
    finally:
        conn.close()
    assert int(old["superseded_by_memory_id"]) == second_id
    assert int(second["supersedes_memory_id"]) == old_id
    assert first["activity_state"] == "archived"
    assert first["content"] == "first possible current fact"
    assert second["content"] == "second intended current fact"
    assert tuple(issue) == ("repaired", "approved")


def test_self_healing_workshop_surface_is_available(server: Any, monkeypatch) -> None:
    monkeypatch.setenv("MCP_SURFACE_PROFILE", "admin")
    workshop = server.open_workshop("memory")
    actions = {item["action"]: item for item in workshop["actions"]}
    assert actions["self_healing_status"]["risk_class"] == "R0"
    assert actions["self_healing_issue"]["risk_class"] == "R0"
    assert actions["self_healing_propose"]["risk_class"] == "R1"
    assert actions["self_healing_confirm"]["risk_class"] == "R2"


def test_lineage_cycle_is_model_resolved_with_user_consent(server: Any) -> None:
    project = "self-healing-cycle"
    first_id = _direct(server, content="cycle version one", project_key=project)
    second_id = _direct(server, content="cycle version two", project_key=project)
    third_id = _direct(server, content="cycle intended current version", project_key=project)
    conn = server.get_db_connection()
    try:
        conn.execute("UPDATE memories SET supersedes_memory_id=? WHERE id=?", (second_id, first_id))
        conn.execute("UPDATE memories SET supersedes_memory_id=? WHERE id=?", (third_id, second_id))
        conn.execute("UPDATE memories SET supersedes_memory_id=? WHERE id=?", (first_id, third_id))
        conn.commit()
    finally:
        conn.close()

    _scan(server)
    conn = server.get_db_connection()
    try:
        row = conn.execute(
            "SELECT id,status FROM memory_self_healing_issues "
            "WHERE issue_kind='lineage_cycle' ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "awaiting_model"
    issue_id = int(row["id"])

    proposal = server.propose_memory_self_healing_resolution(
        issue_id,
        third_id,
        0.94,
        "The third version is the intended current version and the other two are historical.",
    )
    assert proposal["proposal"]["action"] == "break_lineage_cycle"
    confirmed = server.confirm_memory_self_healing_resolution(issue_id, True)
    assert confirmed["status"] == "repaired"
    assert confirmed["post_repair_current_state"]["summary"]["critical_issue_count"] == 0

    conn = server.get_db_connection()
    try:
        first = conn.execute("SELECT * FROM memories WHERE id=?", (first_id,)).fetchone()
        second = conn.execute("SELECT * FROM memories WHERE id=?", (second_id,)).fetchone()
        third = conn.execute("SELECT * FROM memories WHERE id=?", (third_id,)).fetchone()
    finally:
        conn.close()
    assert first["activity_state"] == "archived"
    assert second["activity_state"] == "archived"
    assert third["activity_state"] == "active"
    assert third["supersedes_memory_id"] is None
    assert third["superseded_by_memory_id"] is None
    assert first["content"] == "cycle version one"
    assert second["content"] == "cycle version two"
    assert third["content"] == "cycle intended current version"