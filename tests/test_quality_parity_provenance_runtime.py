from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import mcp_surface

from mapi_core.memory import provenance_context
from mapi_core.memory.provenance_context import resolve_write_provenance
from app.runtime.backpressure import (
    BACKPRESSURE_SCHEMA,
    BackpressureState,
    McpBackpressureMiddleware,
    transport_status_payload,
)


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def test_transport_provenance_uses_mcp_session_and_request(monkeypatch: Any) -> None:
    fake = SimpleNamespace(
        session_id="session-abc",
        origin_request_id="request-xyz",
        request_context=object(),
        request_id="request-xyz",
    )
    monkeypatch.setattr(provenance_context, "_active_fastmcp_context", lambda: fake)

    result = resolve_write_provenance(
        conversation_key=None,
        source_event_ref=None,
        normalize_optional_text=_norm,
    )

    assert result["conversation_key"] == "mcp-session:session-abc"
    assert result["source_event_ref"] == "mcp-request:session-abc:request-xyz"
    assert result["origins"] == ["mcp_session", "mcp_request"]


def test_explicit_provenance_wins_over_transport(monkeypatch: Any) -> None:
    fake = SimpleNamespace(
        session_id="session-abc",
        origin_request_id="request-xyz",
        request_context=object(),
        request_id="request-xyz",
    )
    monkeypatch.setattr(provenance_context, "_active_fastmcp_context", lambda: fake)

    result = resolve_write_provenance(
        conversation_key="client:conversation-1",
        source_event_ref="git:deadbeef",
        normalize_optional_text=_norm,
    )

    assert result["conversation_key"] == "client:conversation-1"
    assert result["source_event_ref"] == "git:deadbeef"
    assert result["origins"] == ["explicit_conversation_key", "explicit_source_event_ref"]


def test_save_memory_gets_internal_event_provenance_without_mcp(server: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("MCP_SURFACE_PROFILE", "agent")
    monkeypatch.setattr(provenance_context, "_active_fastmcp_context", lambda: None)

    result = server.save_memory(
        content="A durable memory with internal provenance fallback.",
        project_key="demo-project",
        write_intent="user_explicit",
    )

    assert result["status"] == "created"
    assert result["memory"]["source_event_ref"].startswith("memory-event:")
    assert result["memory"]["conversation_key"] is None
    assert result["provenance"]["conversation_key"] is None


def test_save_memory_preserves_explicit_provenance(server: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("MCP_SURFACE_PROFILE", "agent")
    result = server.save_memory(
        content="Explicit provenance integration memory.",
        project_key="demo-project",
        conversation_key="client:test-conversation",
        source_event_ref="client:test-event",
    )

    assert result["status"] == "created"
    assert result["memory"]["conversation_key"] == "client:test-conversation"
    assert result["memory"]["source_event_ref"] == "client:test-event"
    assert result["provenance"]["origins"] == [
        "explicit_conversation_key",
        "explicit_source_event_ref",
    ]


def test_provenance_backfill_repairs_only_from_durable_evidence(server: Any) -> None:
    created = server.create_memory(
        content="Legacy provenance repair candidate.",
        memory_type="project_note",
        project_key="demo-project",
    )
    memory_id = int(created["memory"]["id"])

    conn = server.get_db_connection()
    try:
        conn.execute(
            "UPDATE memories SET source_event_ref=NULL, conversation_key=NULL WHERE id=?",
            (memory_id,),
        )
        conn.commit()
    finally:
        conn.close()

    preview = server.preview_memory_provenance_backfill(project_key="demo-project")
    assert preview["status"] == "ok"
    assert preview["candidate_count"] >= 1
    candidate = next(item for item in preview["sample"] if int(item["memory_id"]) == memory_id)
    assert candidate["proposed_source_event_ref"].startswith("legacy-evidence-event:")
    assert candidate["source_ref_evidence_kind"] == "durable_memory_event"
    assert candidate["proposed_conversation_key"] is None

    applied = server.apply_memory_provenance_backfill(
        expected_preview_hash=preview["preview_hash"],
        project_key="demo-project",
        applied_by="pytest",
        confirm_provenance_repair=True,
    )
    assert applied["status"] == "applied"
    assert applied["updated_count"] >= 1

    loaded = server.get_memory(memory_id)
    assert loaded["memory"]["source_event_ref"].startswith("legacy-evidence-event:")
    assert loaded["memory"]["conversation_key"] is None


def _set_memory_time(server: Any, memory_id: int, created_at: str) -> None:
    conn = server.get_db_connection()
    try:
        conn.execute(
            "UPDATE memories SET created_at=?, last_accessed_at=?, updated_at=? WHERE id=?",
            (created_at, created_at, created_at, memory_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_reconstruct_day_is_timezone_aware_project_scoped_and_evidence_first(server: Any) -> None:
    first = server.create_memory(
        content="2026-07-17: project-a checkpoint.",
        memory_type="project_note",
        project_key="project-a",
    )
    first_id = int(first["memory"]["id"])
    _set_memory_time(server, first_id, "2026-07-17T18:18:10Z")

    other = server.create_memory(
        content="2026-07-17: project-b checkpoint.",
        memory_type="project_note",
        project_key="project-b",
    )
    other_id = int(other["memory"]["id"])
    _set_memory_time(server, other_id, "2026-07-17T19:00:00Z")

    result = server.reconstruct_day(
        date="2026-07-17",
        timezone="Europe/Warsaw",
        project_key="project-a",
    )

    assert result["status"] == "ok"
    assert result["coverage"]["bounded_complete"] is True
    assert result["coverage"]["absence_claim_allowed"] is False
    assert set(result["projects"]) == {"project-a"}
    memory_ids = {
        int(item["id"])
        for item in result["items"]
        if item["kind"] == "memory"
    }
    assert first_id in memory_ids
    assert other_id not in memory_ids
    assert any(
        item["local_time"].startswith("2026-07-17T20:18:10")
        for item in result["items"]
        if item["kind"] == "memory" and int(item["id"]) == first_id
    )


def test_reconstruct_day_absence_claim_requires_complete_empty_scan(server: Any) -> None:
    result = server.reconstruct_day(date="2020-01-02", timezone="Europe/Warsaw")
    assert result["status"] == "ok"
    assert result["items"] == []
    assert result["supporting_items"] == []
    assert result["coverage"]["state"] == "no_evidence"
    assert result["coverage"]["bounded_complete"] is True
    assert result["coverage"]["absence_claim_allowed"] is True
    assert "external" in result["coverage"]["note"].lower()


def test_reconstruct_day_validates_inputs(server: Any) -> None:
    assert server.reconstruct_day(date="17-07-2026")["status"] == "error"
    assert server.reconstruct_day(date="2026-07-17", timezone="Mars/Olympus")["status"] == "error"
    assert server.reconstruct_day(date="2026-07-17", limit=0)["status"] == "error"


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _send_recorder(target: list[dict[str, Any]]):
    async def send(message: dict[str, Any]) -> None:
        target.append(message)
    return send


def _scope(method: str = "POST") -> dict[str, Any]:
    return {"type": "http", "method": method, "path": "/mcp/", "headers": []}


def test_backpressure_allows_sequential_posts() -> None:
    async def scenario() -> None:
        state = BackpressureState(max_in_flight_posts=4, retry_after_seconds=1, keepalive_seconds=30)
        calls = {"count": 0}

        async def downstream(scope, receive, send):
            calls["count"] += 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        app = McpBackpressureMiddleware(downstream, state=state)
        for _ in range(100):
            messages: list[dict[str, Any]] = []
            await app(_scope(), _noop_receive, _send_recorder(messages))
            assert messages[0]["status"] == 200
        assert calls["count"] == 100
        assert state.accepted_total == 100
        assert state.rejected_total == 0
        assert state.active_posts == 0

    asyncio.run(scenario())


def test_excess_concurrent_post_gets_429_and_retry_after() -> None:
    async def scenario() -> None:
        state = BackpressureState(max_in_flight_posts=1, retry_after_seconds=2, keepalive_seconds=30)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def downstream(scope, receive, send):
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        app = McpBackpressureMiddleware(downstream, state=state)
        first_messages: list[dict[str, Any]] = []
        first = asyncio.create_task(app(_scope(), _noop_receive, _send_recorder(first_messages)))
        await entered.wait()

        rejected: list[dict[str, Any]] = []
        await app(_scope(), _noop_receive, _send_recorder(rejected))
        assert rejected[0]["status"] == 429
        headers = {key.lower(): value for key, value in rejected[0]["headers"]}
        assert headers[b"retry-after"] == b"2"
        body = json.loads(rejected[1]["body"].decode("utf-8"))
        assert body == {
            "status": "error",
            "error": "mapi_backpressure",
            "schema": BACKPRESSURE_SCHEMA,
            "retry_after_seconds": 2,
        }

        release.set()
        await first
        assert first_messages[0]["status"] == 200
        assert state.active_posts == 0

    asyncio.run(scenario())


def test_get_does_not_consume_post_capacity() -> None:
    async def scenario() -> None:
        state = BackpressureState(max_in_flight_posts=1)

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        app = McpBackpressureMiddleware(downstream, state=state)
        messages: list[dict[str, Any]] = []
        await app(_scope("GET"), _noop_receive, _send_recorder(messages))
        assert messages[0]["status"] == 200
        assert state.accepted_total == 0
        assert state.rejected_total == 0

    asyncio.run(scenario())


def test_transport_status_is_exposed_via_governance_workshop(server: Any) -> None:
    payload = transport_status_payload()
    assert payload["status"] == "ok"
    assert payload["streamable_http"] is True
    assert payload["stateful_session"] is True
    assert payload["overload_contract"]["status_code"] == 429

    workshop = mcp_surface.open_workshop_payload("governance", profile="reader")
    actions = {item["action"]: item for item in workshop["actions"]}
    assert actions["transport_status"]["tool_name"] == "get_mcp_transport_status"
    assert actions["transport_status"]["risk_class"] == "R0"

    status = server.get_mcp_transport_status()
    assert status["stateful_session"] is True
    assert status["overload_contract"]["retry_after_header"] == "Retry-After"
