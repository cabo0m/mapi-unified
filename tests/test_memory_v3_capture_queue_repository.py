from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from app import db_migrations
from mapi_core.memory.capture_queue import (
    create_capture_review_item,
    get_capture_review_item,
    list_capture_review_items,
    review_capture_item,
    update_capture_reconciliation_preview,
    expire_capture_item,
)
from mapi_core.schemas import normalize_optional_text, normalize_required_text


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db_migrations.apply_all_migrations(conn)
    return conn


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _utc_now_values() -> Any:
    values = iter(
        [
            "2026-07-13T01:56:06Z",
            "2026-07-13T01:56:07Z",
            "2026-07-13T01:56:08Z",
            "2026-07-13T01:56:09Z",
            "2026-07-13T01:56:10Z",
        ]
    )
    return lambda: next(values)


def _proposal(**overrides: Any) -> dict[str, Any]:
    payload = {
        "content": "Zapamiętaj decyzję projektową.",
        "memory_type": "project_decision",
        "summary_short": "Decyzja projektowa",
        "project_key": "mapi",
        "scope_code": "project",
        "source_context": "pytest",
        "entry_type": "decision",
        "truth_kind": "decision",
        "memory_v2_status": "proposed",
        "requires_user_confirmation": True,
    }
    payload.update(overrides)
    return payload


def test_create_get_list_and_round_trip_json() -> None:
    conn = _make_conn()
    utc_now_iso = _utc_now_values()

    created = create_capture_review_item(
        conn,
        proposal_key="proposal:abc",
        proposal=_proposal(),
        input_fingerprint="input:abc",
        project_key="mapi",
        scope_code="project",
        conversation_key="conv-1",
        source_context="pytest",
        source_event_ref="evt-1",
        recommended_action="review",
        expires_at="2026-08-01T00:00:00Z",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )

    fetched = get_capture_review_item(conn, item_id=int(created["item"]["id"]), row_to_dict=_row_to_dict)
    listed = list_capture_review_items(
        conn,
        status=None,
        project_key="mapi",
        scope_code="project",
        created_after=None,
        created_before=None,
        limit=10,
        include_expired=False,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )

    assert created["status"] == "created"
    assert created["created"] is True
    assert fetched["proposal"]["content"] == "Zapamiętaj decyzję projektową."
    assert fetched["matched_memory_ids"] == []
    assert fetched["reconciliation"] is None
    assert listed["status"] == "ok"
    assert listed["summary"]["total_returned"] == 1
    assert listed["items"][0]["proposal_key"] == "proposal:abc"


def test_create_is_idempotent_by_proposal_key_and_immutable_fields() -> None:
    conn = _make_conn()
    utc_now_iso = _utc_now_values()

    first = create_capture_review_item(
        conn,
        proposal_key="proposal:same",
        proposal=_proposal(summary_short="One"),
        input_fingerprint="input:same",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )
    second = create_capture_review_item(
        conn,
        proposal_key="proposal:same",
        proposal=_proposal(summary_short="Two"),
        input_fingerprint="input:other",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )

    assert first["status"] == "created"
    assert second["status"] == "already_exists"
    assert first["item"]["id"] == second["item"]["id"]
    assert second["item"]["proposal"]["summary_short"] == "One"
    assert second["item"]["input_fingerprint"] == "input:same"


def test_review_transition_matrix_and_terminal_blocks() -> None:
    conn = _make_conn()
    utc_now_iso = _utc_now_values()
    created = create_capture_review_item(
        conn,
        proposal_key="proposal:review",
        proposal=_proposal(),
        input_fingerprint="input:review",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )
    item_id = int(created["item"]["id"])

    reject_missing_reason = review_capture_item(
        conn,
        item_id=item_id,
        decision="reject",
        review_note=None,
        reviewed_by="pytest",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )
    approved = review_capture_item(
        conn,
        item_id=item_id,
        decision="approve",
        review_note="looks good",
        reviewed_by="pytest",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )
    repeated = review_capture_item(
        conn,
        item_id=item_id,
        decision="approve",
        review_note="looks good",
        reviewed_by="pytest",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )
    conflicting = review_capture_item(
        conn,
        item_id=item_id,
        decision="reject",
        review_note="too late",
        reviewed_by="pytest",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )

    assert reject_missing_reason["status"] == "error"
    assert approved["status"] == "updated"
    assert approved["item"]["status"] == "approved"
    assert repeated["status"] == "already_decided"
    assert conflicting["status"] == "blocked"


def test_expire_and_reconciliation_preview_updates_without_status_change() -> None:
    conn = _make_conn()
    utc_now_iso = _utc_now_values()
    created = create_capture_review_item(
        conn,
        proposal_key="proposal:expire",
        proposal=_proposal(),
        input_fingerprint="input:expire",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )
    item_id = int(created["item"]["id"])

    updated_preview = update_capture_reconciliation_preview(
        conn,
        item_id=item_id,
        recommended_action="abstain",
        matched_memory_ids=[3, 4],
        reconciliation={"outcome": "abstain", "reason_codes": ["ambiguous_candidates"]},
        candidate_set_fingerprint="candidate:1",
        reconciliation_preview_hash="preview:1",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        row_to_dict=_row_to_dict,
    )
    expired = expire_capture_item(
        conn,
        item_id=item_id,
        reason="stale review queue item",
        expired_by="pytest",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )
    repeated = expire_capture_item(
        conn,
        item_id=item_id,
        reason="stale review queue item",
        expired_by="pytest",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )
    blocked_preview = update_capture_reconciliation_preview(
        conn,
        item_id=item_id,
        recommended_action="create_new",
        matched_memory_ids=[],
        reconciliation={"outcome": "create_new", "reason_codes": ["no_candidates"]},
        candidate_set_fingerprint="candidate:2",
        reconciliation_preview_hash="preview:2",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        row_to_dict=_row_to_dict,
    )

    assert updated_preview["status"] == "updated"
    assert updated_preview["item"]["status"] == "pending"
    assert updated_preview["item"]["matched_memory_ids"] == [3, 4]
    assert expired["status"] == "expired"
    assert expired["item"]["status"] == "expired"
    assert repeated["status"] == "already_expired"
    assert blocked_preview["status"] == "blocked"


def test_reserved_statuses_cannot_be_set_by_review_api() -> None:
    conn = _make_conn()
    utc_now_iso = _utc_now_values()
    created = create_capture_review_item(
        conn,
        proposal_key="proposal:reserved",
        proposal=_proposal(),
        input_fingerprint="input:reserved",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )
    item_id = int(created["item"]["id"])
    conn.execute("UPDATE memory_capture_review_items SET status = 'applied' WHERE id = ?", (item_id,))

    reviewed = review_capture_item(
        conn,
        item_id=item_id,
        decision="approve",
        review_note="noop",
        reviewed_by="pytest",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )
    expired = expire_capture_item(
        conn,
        item_id=item_id,
        reason="noop",
        expired_by="pytest",
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=_row_to_dict,
    )

    assert reviewed["status"] == "blocked"
    assert expired["status"] == "blocked"
