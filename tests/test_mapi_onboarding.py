from __future__ import annotations

import mcp_surface


def _seed_technical_agent_identity(server) -> int:
    conn = server.get_db_connection()
    try:
        created = server._insert_memory(
            conn,
            content="Agent is the configured agent identity for this MAPI instance.",
            summary_short="Agent identity: Agent",
            memory_type="identity",
            source="mapi-init",
            importance_score=0.9,
            confidence_score=1.0,
            tags="agent-self,self-model,self-evidence,identity,bootstrap,subject:agent,agent:agent",
            layer_code="identity",
            area_code="identity",
            state_code="validated",
            scope_code="project",
            identity_weight=1.0,
            project_key="agent-self",
            entry_type="user_profile",
            truth_kind="fact",
            title="Agent identity: Agent",
            source_context="Generated from explicit first-run operator configuration.",
            source_event_ref="mapi-init:agent:identity",
            importance_level="high",
            priority="high",
            ensure_embedding=False,
        )
        conn.commit()
        return int(created["id"])
    finally:
        conn.close()


def _memory_count(server) -> int:
    conn = server.get_db_connection()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    finally:
        conn.close()


def test_onboarding_v2_reviews_before_atomic_profile_commit(server, monkeypatch) -> None:
    monkeypatch.setenv("MAPI_AGENT_SUBJECT_KEY", "agent")
    monkeypatch.setenv("MAPI_AGENT_PROJECT_KEY", "agent-self")
    monkeypatch.setenv("MAPI_AGENT_DISPLAY_NAME", "Agent")
    monkeypatch.setenv("MAPI_DISTRIBUTION_NAME", "MAPI")
    old_identity_id = _seed_technical_agent_identity(server)
    initial_count = _memory_count(server)

    bootstrap = server.bootstrap_agent_context()
    onboarding = bootstrap["onboarding"]
    assert onboarding["schema"] == "mapi_onboarding.v2"
    assert onboarding["current_step"] == "agent_name"
    assert onboarding["onboarding_required"] is True
    assert "Luna" in onboarding["next_action"]["delegated_choice_rule"]

    step1 = server.advance_mapi_onboarding("agent_name", "Mira")
    assert step1["current_step"] == "user_name"
    assert step1["created_memory_ids"] == []
    assert step1["durable_profile_committed"] is False
    assert _memory_count(server) == initial_count

    assert server.advance_mapi_onboarding("user_name", "Adam")["current_step"] == "work_context"
    assert server.advance_mapi_onboarding(
        "work_context", "Tworzę oprogramowanie i chcę pomocy w projektach."
    )["current_step"] == "autonomy_level"
    assert server.advance_mapi_onboarding("autonomy_level", "proactive")["current_step"] == "memory_policy"
    assert server.advance_mapi_onboarding("memory_policy", "automatic_important")["current_step"] == "memory_exclusions"
    no_exclusions = server.advance_mapi_onboarding("memory_exclusions", "Brak wykluczeń.")
    assert no_exclusions["current_step"] == "first_project"
    assert no_exclusions["answers"]["memory_exclusions"] is None

    review = server.advance_mapi_onboarding("first_project", "Utworzyć pierwszy projekt o nazwie „Polaris”.")
    assert review["current_step"] == "summary_confirmation"
    assert review["status"] == "onboarding_required"
    assert review["created_memory_ids"] == []
    assert review["review_summary"]["assistant_name"] == "Mira"
    assert review["review_summary"]["autonomy_level"] == "proactive"
    assert review["review_summary"]["first_project"] == "Polaris"
    assert review["review_summary"]["memory_exclusions"] is None
    assert "Tak Cię zrozumiałem/am" in review["next_question"]
    assert _memory_count(server) == initial_count

    revised = server.revise_mapi_onboarding("user_name", "Adam Nowy")
    assert revised["current_step"] == "summary_confirmation"
    assert revised["review_summary"]["user_name"] == "Adam Nowy"
    assert revised["created_memory_ids"] == []
    assert _memory_count(server) == initial_count

    completed = server.advance_mapi_onboarding("summary_confirmation", "confirmed")
    assert completed["status"] == "completed"
    assert completed["onboarding_required"] is False
    assert completed["durable_profile_committed"] is True
    assert completed["summary"]["assistant_name"] == "Mira"
    assert completed["summary"]["user_name"] == "Adam Nowy"
    assert completed["summary"]["autonomy_level"] == "proactive"
    assert completed["summary"]["memory_policy"] == "automatic_important"
    assert completed["summary"]["first_project"] == "Polaris"
    assert completed["summary"]["memory_exclusions"] is None
    assert "zapamiętaj to" in completed["user_controls"]
    assert "co o mnie pamiętasz?" in completed["user_controls"]
    assert "continuity across conversations" in completed["completion_note"]

    conn = server.get_db_connection()
    try:
        old = conn.execute("SELECT * FROM memories WHERE id=?", (old_identity_id,)).fetchone()
        assert old["state_code"] == "superseded"
        rows = conn.execute(
            "SELECT source_event_ref, project_key, area_code, scope_code, content FROM memories "
            "WHERE source='mapi-onboarding' ORDER BY id"
        ).fetchall()
        refs = {str(row["source_event_ref"]) for row in rows}
        assert {
            "mapi-onboarding:v2:agent_name",
            "mapi-onboarding:v2:user_name",
            "mapi-onboarding:v2:work_context",
            "mapi-onboarding:v2:autonomy_level",
            "mapi-onboarding:v2:memory_policy",
            "mapi-onboarding:v2:first_project",
        }.issubset(refs)
        assert "mapi-onboarding:v2:memory_exclusions" not in refs
        autonomy = conn.execute(
            "SELECT content FROM memories WHERE source_event_ref='mapi-onboarding:v2:autonomy_level'"
        ).fetchone()
        assert "actively surface problems" in autonomy["content"]
        project = conn.execute(
            "SELECT project_key, area_code, scope_code FROM memories "
            "WHERE source_event_ref='mapi-onboarding:v2:first_project'"
        ).fetchone()
        assert tuple(project) == ("Polaris", "projects", "project")
    finally:
        conn.close()

    snapshot = server.get_agent_self_snapshot()
    assert snapshot["subject"]["display_name"] == "Mira"


def test_onboarding_can_be_skipped_without_committing_draft_profile(server) -> None:
    initial_count = _memory_count(server)
    server.advance_mapi_onboarding("agent_name", "Kaja")
    skipped = server.skip_mapi_onboarding("Chcę od razu pracować")
    assert skipped["status"] == "skipped"
    assert skipped["onboarding_required"] is False
    assert _memory_count(server) == initial_count


def test_onboarding_v2_workshop_actions_are_visible() -> None:
    workshop = mcp_surface.open_workshop_payload("memory", profile="clean_operator")
    actions = {item["action"] for item in workshop["actions"]}
    assert {
        "onboarding_status",
        "onboarding_advance",
        "onboarding_revise",
        "onboarding_skip",
    }.issubset(actions)
