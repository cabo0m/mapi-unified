from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from mapi_core.memory import lifecycle_snapshots


def _create_memory(server: Any, **overrides: Any) -> int:
    payload = {
        "content": "Lifecycle snapshot memory.",
        "memory_type": "project_note",
        "summary_short": "Lifecycle snapshot memory",
        "project_key": "mapi",
        "scope_code": "project",
        "state_code": "validated",
        "memory_v2_status": "active",
        "truth_kind": "fact",
        "entry_type": "project",
        "confidence_score": 0.9,
        "importance_score": 0.75,
    }
    payload.update(overrides)
    return int(server.create_memory(**payload)["memory"]["id"])


def _sample_before(old_id: int, new_id: int) -> dict[str, Any]:
    return {
        "old_memory": {"id": old_id, "state_code": "validated", "superseded_by_memory_id": None},
        "new_memory": {"id": new_id, "state_code": "validated", "supersedes_memory_id": None},
    }


def _sample_after(old_id: int, new_id: int) -> dict[str, Any]:
    return {
        "old_memory": {"id": old_id, "state_code": "superseded", "superseded_by_memory_id": new_id},
        "new_memory": {"id": new_id, "state_code": "validated", "supersedes_memory_id": old_id},
    }


def test_lifecycle_snapshot_repository_create_get_and_list(server: Any) -> None:
    old_id = _create_memory(server, summary_short="Old")
    new_id = _create_memory(server, summary_short="New")
    conn = server.get_db_connection()
    try:
        created = lifecycle_snapshots.create_lifecycle_snapshot_payload(
            conn,
            operation_key="supersession:test:1",
            new_memory_id=new_id,
            old_memory_id=old_id,
            relation_kind="replacement",
            reason="Repository test",
            input_fingerprint="input-hash",
            candidate_set_fingerprint="candidate-hash",
            preview_hash="preview-hash",
            before_snapshot=_sample_before(old_id, new_id),
            after_snapshot=_sample_after(old_id, new_id),
            link_snapshot={"before": None, "after": {"from_memory_id": new_id, "to_memory_id": old_id}},
            event_snapshot={"before_count": 0, "after_count": 2},
            applied_at="2026-07-13T00:00:00Z",
            applied_by="pytest",
            apply_note="initial apply",
            utc_now_iso=server.utc_now_iso,
            normalize_required_text=server.normalize_required_text,
            normalize_optional_text=server.normalize_optional_text,
            row_to_dict=server.row_to_dict,
        )
        conn.commit()

        fetched = lifecycle_snapshots.get_lifecycle_snapshot_payload(
            conn,
            snapshot_id=int(created["id"]),
            row_to_dict=server.row_to_dict,
        )
        listed = lifecycle_snapshots.list_lifecycle_snapshots_payload(
            conn,
            project_key="mapi",
            status="applied",
            limit=10,
            normalize_optional_text=server.normalize_optional_text,
            row_to_dict=server.row_to_dict,
        )
    finally:
        conn.close()

    assert created["operation_key"] == "supersession:test:1"
    assert created["before_snapshot"] == _sample_before(old_id, new_id)
    assert fetched["after_snapshot"] == _sample_after(old_id, new_id)
    assert listed["status"] == "ok"
    assert listed["summary"]["total_returned"] == 1
    assert listed["runs"][0]["id"] == created["id"]


def test_lifecycle_snapshot_repository_enforces_unique_operation_key(server: Any) -> None:
    old_id = _create_memory(server, summary_short="Old")
    new_id = _create_memory(server, summary_short="New")
    conn = server.get_db_connection()
    try:
        lifecycle_snapshots.create_lifecycle_snapshot_payload(
            conn,
            operation_key="supersession:test:dup",
            new_memory_id=new_id,
            old_memory_id=old_id,
            relation_kind="replacement",
            reason="Repository test",
            input_fingerprint="input-hash",
            candidate_set_fingerprint="candidate-hash",
            preview_hash="preview-hash",
            before_snapshot=_sample_before(old_id, new_id),
            after_snapshot=_sample_after(old_id, new_id),
            link_snapshot={"before": None, "after": {"from_memory_id": new_id, "to_memory_id": old_id}},
            event_snapshot={"before_count": 0, "after_count": 2},
            utc_now_iso=server.utc_now_iso,
            normalize_required_text=server.normalize_required_text,
            normalize_optional_text=server.normalize_optional_text,
            row_to_dict=server.row_to_dict,
        )
        with pytest.raises(sqlite3.IntegrityError):
            lifecycle_snapshots.create_lifecycle_snapshot_payload(
                conn,
                operation_key="supersession:test:dup",
                new_memory_id=new_id,
                old_memory_id=old_id,
                relation_kind="replacement",
                reason="Repository test duplicate",
                input_fingerprint="input-hash-2",
                candidate_set_fingerprint="candidate-hash-2",
                preview_hash="preview-hash-2",
                before_snapshot=_sample_before(old_id, new_id),
                after_snapshot=_sample_after(old_id, new_id),
                link_snapshot={"before": None, "after": {"from_memory_id": new_id, "to_memory_id": old_id}},
                event_snapshot={"before_count": 0, "after_count": 2},
                utc_now_iso=server.utc_now_iso,
                normalize_required_text=server.normalize_required_text,
                normalize_optional_text=server.normalize_optional_text,
                row_to_dict=server.row_to_dict,
            )
    finally:
        conn.close()


def test_lifecycle_snapshot_repository_marks_rollback_idempotently_and_keeps_immutable_fields(server: Any) -> None:
    old_id = _create_memory(server, summary_short="Old")
    new_id = _create_memory(server, summary_short="New")
    conn = server.get_db_connection()
    try:
        created = lifecycle_snapshots.create_lifecycle_snapshot_payload(
            conn,
            operation_key="supersession:test:rollback",
            new_memory_id=new_id,
            old_memory_id=old_id,
            relation_kind="replacement",
            reason="Repository test",
            input_fingerprint="input-hash",
            candidate_set_fingerprint="candidate-hash",
            preview_hash="preview-hash",
            before_snapshot=_sample_before(old_id, new_id),
            after_snapshot=_sample_after(old_id, new_id),
            link_snapshot={"before": None, "after": {"from_memory_id": new_id, "to_memory_id": old_id}},
            event_snapshot={"before_count": 0, "after_count": 2},
            utc_now_iso=server.utc_now_iso,
            normalize_required_text=server.normalize_required_text,
            normalize_optional_text=server.normalize_optional_text,
            row_to_dict=server.row_to_dict,
        )
        conn.commit()

        first = lifecycle_snapshots.mark_lifecycle_snapshot_rolled_back_payload(
            conn,
            snapshot_id=int(created["id"]),
            rollback_preview_hash="rollback-preview-hash",
            rollback_snapshot={"restored": True, "memory_ids": [old_id, new_id]},
            rolled_back_at="2026-07-13T00:10:00Z",
            rolled_back_by="pytest",
            rollback_note="rollback ok",
            utc_now_iso=server.utc_now_iso,
            normalize_required_text=server.normalize_required_text,
            normalize_optional_text=server.normalize_optional_text,
            row_to_dict=server.row_to_dict,
        )
        conn.commit()
        second = lifecycle_snapshots.mark_lifecycle_snapshot_rolled_back_payload(
            conn,
            snapshot_id=int(created["id"]),
            rollback_preview_hash="rollback-preview-hash",
            rollback_snapshot={"restored": True, "memory_ids": [old_id, new_id]},
            rolled_back_at="2026-07-13T00:10:00Z",
            rolled_back_by="pytest",
            rollback_note="rollback ok",
            utc_now_iso=server.utc_now_iso,
            normalize_required_text=server.normalize_required_text,
            normalize_optional_text=server.normalize_optional_text,
            row_to_dict=server.row_to_dict,
        )
        with pytest.raises(ValueError, match="different rollback evidence"):
            lifecycle_snapshots.mark_lifecycle_snapshot_rolled_back_payload(
                conn,
                snapshot_id=int(created["id"]),
                rollback_preview_hash="different-hash",
                rollback_snapshot={"restored": False},
                rolled_back_at="2026-07-13T00:10:00Z",
                rolled_back_by="pytest",
                rollback_note="rollback ok",
                utc_now_iso=server.utc_now_iso,
                normalize_required_text=server.normalize_required_text,
                normalize_optional_text=server.normalize_optional_text,
                row_to_dict=server.row_to_dict,
            )
    finally:
        conn.close()

    assert first["status"] == "rolled_back"
    assert second["status"] == "rolled_back"
    assert first["before_snapshot"] == created["before_snapshot"]
    assert first["after_snapshot"] == created["after_snapshot"]
    assert first["rollback_snapshot"] == {"restored": True, "memory_ids": [old_id, new_id]}
    assert second["id"] == first["id"]


def test_lifecycle_snapshot_repository_round_trips_json_payloads(server: Any) -> None:
    old_id = _create_memory(server, summary_short="Old")
    new_id = _create_memory(server, summary_short="New")
    conn = server.get_db_connection()
    try:
        created = lifecycle_snapshots.create_lifecycle_snapshot_payload(
            conn,
            operation_key="supersession:test:json",
            new_memory_id=new_id,
            old_memory_id=old_id,
            relation_kind="correction",
            reason="JSON round trip",
            input_fingerprint="input-hash",
            candidate_set_fingerprint="candidate-hash",
            preview_hash="preview-hash",
            before_snapshot={"old": {"id": old_id, "fields": {"tags": ["a", "b"]}}},
            after_snapshot={"new": {"id": new_id, "fields": {"tags": ["c"], "score": 0.7}}},
            link_snapshot={"before": [], "after": [{"from": new_id, "to": old_id}]},
            event_snapshot={"events": [{"event_type": "version.superseded"}]},
            utc_now_iso=server.utc_now_iso,
            normalize_required_text=server.normalize_required_text,
            normalize_optional_text=server.normalize_optional_text,
            row_to_dict=server.row_to_dict,
        )
    finally:
        conn.close()

    assert created["before_snapshot"]["old"]["fields"]["tags"] == ["a", "b"]
    assert created["after_snapshot"]["new"]["fields"]["score"] == 0.7
    assert created["event_snapshot"]["events"][0]["event_type"] == "version.superseded"


def test_pointer_snapshot_repository_creates_applying_and_finalizes_exactly(server: Any) -> None:
    old_id = _create_memory(server, summary_short="Old")
    new_id = _create_memory(server, summary_short="New")
    conn = server.get_db_connection()
    try:
        applying = lifecycle_snapshots.create_applying_lifecycle_snapshot_payload(
            conn,
            operation_key="pointer:test:applying",
            new_memory_id=new_id,
            old_memory_id=old_id,
            relation_kind="pointer_only_chain_repair",
            reason="Pointer repository test",
            input_fingerprint="manifest-hash",
            candidate_set_fingerprint="operation-identity-hash",
            preview_hash="preview-hash",
            before_snapshot={"operation_identity": {"operation_key": "pointer:test:applying"}},
            after_snapshot={"pending": {"target_states": True}},
            link_snapshot={"pending": {"created_link_ids": []}},
            event_snapshot={"pending": {"created_apply_event_ids": []}},
            applied_by="pytest",
            utc_now_iso=server.utc_now_iso,
            normalize_required_text=server.normalize_required_text,
            normalize_optional_text=server.normalize_optional_text,
            row_to_dict=server.row_to_dict,
        )
        assert applying["status"] == "applying"
        assert applying["operation_type"] == "pointer_lineage_remediation"
        assert applying["applied_at"] is None
        assert applying["started_at"]

        applied_at = "2026-07-19T01:00:00Z"
        final = lifecycle_snapshots.finalize_lifecycle_snapshot_applied_payload(
            conn,
            snapshot_id=int(applying["id"]),
            after_snapshot={"target_states": [{"id": old_id, "state_code": "superseded"}]},
            link_snapshot={"created_link_ids": [11]},
            event_snapshot={"created_apply_event_ids": [21, 22, 23]},
            applied_at=applied_at,
            utc_now_iso=server.utc_now_iso,
            normalize_optional_text=server.normalize_optional_text,
            row_to_dict=server.row_to_dict,
        )
        repeated = lifecycle_snapshots.finalize_lifecycle_snapshot_applied_payload(
            conn,
            snapshot_id=int(applying["id"]),
            after_snapshot=final["after_snapshot"],
            link_snapshot=final["link_snapshot"],
            event_snapshot=final["event_snapshot"],
            applied_at=applied_at,
            utc_now_iso=server.utc_now_iso,
            normalize_optional_text=server.normalize_optional_text,
            row_to_dict=server.row_to_dict,
        )
    finally:
        conn.close()

    assert final["status"] == "applied"
    assert final["applied_at"] == applied_at
    assert repeated == final
    assert final["operation_key"] == applying["operation_key"]


def test_pointer_snapshot_repository_rejects_invalid_transitions_and_marks_failed(server: Any) -> None:
    old_id = _create_memory(server, summary_short="Old")
    new_id = _create_memory(server, summary_short="New")
    conn = server.get_db_connection()
    try:
        with pytest.raises(ValueError, match="pending section"):
            lifecycle_snapshots.create_applying_lifecycle_snapshot_payload(
                conn,
                operation_key="pointer:test:invalid-pending",
                new_memory_id=new_id,
                old_memory_id=old_id,
                relation_kind="pointer_only_chain_repair",
                reason="Pointer repository test",
                input_fingerprint="manifest-hash",
                candidate_set_fingerprint="identity-hash",
                preview_hash="preview-hash",
                before_snapshot={},
                after_snapshot={"not_pending": {}},
                link_snapshot={"pending": {}},
                event_snapshot={"pending": {}},
                applied_by="pytest",
                utc_now_iso=server.utc_now_iso,
                normalize_required_text=server.normalize_required_text,
                normalize_optional_text=server.normalize_optional_text,
                row_to_dict=server.row_to_dict,
            )

        applying = lifecycle_snapshots.create_applying_lifecycle_snapshot_payload(
            conn,
            operation_key="pointer:test:failed",
            new_memory_id=new_id,
            old_memory_id=old_id,
            relation_kind="pointer_only_chain_repair",
            reason="Pointer repository test",
            input_fingerprint="manifest-hash",
            candidate_set_fingerprint="identity-hash",
            preview_hash="preview-hash",
            before_snapshot={},
            after_snapshot={"pending": {}},
            link_snapshot={"pending": {}},
            event_snapshot={"pending": {}},
            applied_by="pytest",
            utc_now_iso=server.utc_now_iso,
            normalize_required_text=server.normalize_required_text,
            normalize_optional_text=server.normalize_optional_text,
            row_to_dict=server.row_to_dict,
        )
        with pytest.raises(ValueError, match="only an applied"):
            lifecycle_snapshots.mark_lifecycle_snapshot_rolled_back_payload(
                conn, snapshot_id=int(applying["id"]), rollback_preview_hash="rollback-hash",
                rollback_snapshot={"invalid": True}, utc_now_iso=server.utc_now_iso,
                normalize_required_text=server.normalize_required_text,
                normalize_optional_text=server.normalize_optional_text, row_to_dict=server.row_to_dict,
            )
        failed = lifecycle_snapshots.mark_lifecycle_snapshot_failed_payload(
            conn,
            snapshot_id=int(applying["id"]),
            failure_note="operator persisted failure",
            utc_now_iso=server.utc_now_iso,
            normalize_required_text=server.normalize_required_text,
            row_to_dict=server.row_to_dict,
        )
        assert failed["status"] == "failed"
        assert failed["applied_at"] is None
        with pytest.raises(ValueError, match="only an applied"):
            lifecycle_snapshots.mark_lifecycle_snapshot_rolled_back_payload(
                conn, snapshot_id=int(failed["id"]), rollback_preview_hash="rollback-hash",
                rollback_snapshot={"invalid": True}, utc_now_iso=server.utc_now_iso,
                normalize_required_text=server.normalize_required_text,
                normalize_optional_text=server.normalize_optional_text, row_to_dict=server.row_to_dict,
            )
        with pytest.raises(ValueError, match="only an applying"):
            lifecycle_snapshots.finalize_lifecycle_snapshot_applied_payload(
                conn,
                snapshot_id=int(failed["id"]),
                after_snapshot={},
                link_snapshot={},
                event_snapshot={},
                utc_now_iso=server.utc_now_iso,
                normalize_optional_text=server.normalize_optional_text,
                row_to_dict=server.row_to_dict,
            )
    finally:
        conn.close()
