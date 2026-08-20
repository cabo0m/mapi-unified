from mapi_core.memory.retention import preview_memory_retention_policy_payload
from tests.memory_v3_retention_test_helpers import NOW, canonical_hash, insert_memory, make_conn


def _preview(conn, memory_id):
    return preview_memory_retention_policy_payload(conn, memory_id=memory_id, as_of=NOW, include_debug=False, row_to_dict=dict, canonical_json_hash=canonical_hash, utc_now_iso=lambda: NOW)


def test_protected_classes_never_offer_archive_or_expire() -> None:
    conn = make_conn()
    ids = [
        insert_memory(conn, entry_type="core", valid_to="2026-01-01"),
        insert_memory(conn, memory_type="identity", valid_to="2026-01-01"),
        insert_memory(conn, entry_type="decision", valid_to="2026-01-01"),
        insert_memory(conn, entry_type="preference", valid_to="2026-01-01"),
        insert_memory(conn, content="patient diagnosis: private", valid_to="2026-01-01"),
        insert_memory(conn, state_code="conflicted", memory_v2_status="contradicted", contradiction_flag=1, valid_to="2026-01-01"),
    ]
    for memory_id in ids:
        result = _preview(conn, memory_id)
        assert result["policy_outcome"] in {"protected", "blocked_never_store"}
        assert result["proposed_action"] is None
        assert result["guard"]["apply_eligible"] is False


def test_existing_secret_is_manual_remediation_only_and_redacted() -> None:
    conn = make_conn()
    raw = "api_key=Abcd1234!real-value"
    memory_id = insert_memory(conn, content=raw, valid_to="2026-01-01")
    result = _preview(conn, memory_id)
    assert result["policy_outcome"] == "blocked_never_store"
    assert raw not in repr(result)
