from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

from mapi_core.memory.lifecycle_contracts import derive_canonical_memory_state, is_transition_allowed, project_memory_v2_status
from mapi_core.memory.retention import RETENTION_POLICY_VERSION, SUPPORTED_RETENTION_ACTIONS
from mapi_core.memory.retention_review import get_retention_review_item


RETENTION_APPLY_SCHEMA_VERSION = "memory_v3_retention_apply.v1"
RETENTION_ROLLBACK_PREVIEW_SCHEMA_VERSION = "memory_v3_retention_rollback_preview.v1"
ROLLBACK_ACTIONS = frozenset({"archive_candidate", "expire_candidate"})
ROLLBACK_FIELDS = (
    "state_code", "memory_v2_status", "activity_state", "archived_at", "valid_from", "valid_to",
    "requires_user_confirmation", "review_due_at", "revalidation_due_at", "expired_due_at",
    "last_validated_at", "last_confirmed_at", "validation_source", "updated_at",
)


class RetentionBlocked(RuntimeError):
    pass


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) and value.strip() else None


def _snapshot(row: Any, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    memory = row_to_dict(row)
    return {field: memory.get(field) for field in ROLLBACK_FIELDS}


def _result_hash(
    canonical_json_hash: Callable[[Any], str],
    *,
    item_id: int,
    action: str,
    before: dict,
    after: dict,
    event_ids: list[int],
    batch_manifest_identifiers: dict[str, Any] | None = None,
) -> str:
    return canonical_json_hash(
        {
            "item_id": item_id,
            "action": action,
            "before": before,
            "after": after,
            "event_ids": event_ids,
            "batch_manifest_identifiers": batch_manifest_identifiers,
        }
    )


def _applied_audit_is_complete(
    conn: Any,
    item: dict[str, Any],
    *,
    current_snapshot: dict[str, Any] | None,
    canonical_json_hash: Callable[[Any], str],
) -> bool:
    before = item.get("before_snapshot")
    applied = item.get("applied_snapshot")
    event_ids = item.get("created_event_ids")
    action = item.get("proposed_action")
    if not str(item.get("preview_hash") or "").strip() or action not in SUPPORTED_RETENTION_ACTIONS:
        return False
    if not isinstance(before, dict) or not isinstance(applied, dict):
        return False
    if set(ROLLBACK_FIELDS) - set(before) or set(ROLLBACK_FIELDS) - set(applied):
        return False
    if not isinstance(event_ids, list) or not event_ids or any(not isinstance(value, int) for value in event_ids):
        return False
    if not str(item.get("apply_result_fingerprint") or "").strip() or current_snapshot != applied:
        return False
    rows = conn.execute(
        f"SELECT id, payload_json FROM memory_events WHERE id IN ({','.join('?' for _ in event_ids)}) ORDER BY id",
        event_ids,
    ).fetchall()
    if [int(row["id"]) for row in rows] != sorted(event_ids):
        return False
    batch_identifiers = None
    for row in rows:
        payload = _loads(row["payload_json"]) or {}
        current_identifiers = payload.get("batch_manifest_identifiers")
        if current_identifiers is not None:
            if batch_identifiers is not None and batch_identifiers != current_identifiers:
                return False
            batch_identifiers = current_identifiers
    expected_fingerprint = _result_hash(
        canonical_json_hash,
        item_id=int(item["id"]),
        action=str(action),
        before=before,
        after=applied,
        event_ids=event_ids,
        batch_manifest_identifiers=batch_identifiers,
    )
    return expected_fingerprint == item["apply_result_fingerprint"]


def _fresh_preview(preview_func: Callable[..., dict[str, Any]], conn: Any, item: dict[str, Any]) -> dict[str, Any]:
    return preview_func(conn, memory_id=int(item["memory_id"]), as_of=item["as_of"], include_debug=False)


def _preflight(
    conn: Any,
    *,
    item_id: int,
    expected_preview_hash: str,
    applied_by: str,
    row_to_dict: Callable[[Any], dict[str, Any]],
    preview_func: Callable[..., dict[str, Any]],
    memory_v2_enabled: Callable[[Any], bool],
    retention_flag_evaluation: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not str(applied_by or "").strip():
        raise RetentionBlocked("applied_by_required")
    if not memory_v2_enabled(conn):
        raise RetentionBlocked("memory_v2_feature_flag_off")
    item = get_retention_review_item(conn, review_item_id=int(item_id), row_to_dict=row_to_dict)
    if item["status"] != "approved":
        raise RetentionBlocked(f"review_status_not_approved:{item['status']}")
    flag = retention_flag_evaluation(conn, project_key=item.get("project_key"), scope_code=item.get("scope_code"))
    if not flag["enabled"]:
        raise RetentionBlocked("memory_v3_retention_feature_flag_off")
    if bool(flag.get("read_only_mode")):
        raise RetentionBlocked("memory_v3_retention_read_only")
    action = item.get("proposed_action")
    if action not in SUPPORTED_RETENTION_ACTIONS:
        raise RetentionBlocked("outcome_not_supported")
    fresh = _fresh_preview(preview_func, conn, item)
    if fresh.get("status") != "preview_ready":
        raise RetentionBlocked("fresh_preview_not_ready")
    expected = str(expected_preview_hash or "").strip()
    if expected != item["preview_hash"] or fresh["preview_hash"] != item["preview_hash"]:
        raise RetentionBlocked("stale_preview")
    for field in ("memory_id", "project_key", "scope_code", "workspace_id", "policy_outcome", "proposed_action"):
        if fresh.get(field) != item.get(field):
            raise RetentionBlocked(f"immutable_boundary_changed:{field}")
    if action in {"archive_candidate", "expire_candidate"} and fresh.get("protected_reasons"):
        raise RetentionBlocked("protected_memory_action_blocked")
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(item["memory_id"]),)).fetchone()
    if row is None:
        raise RetentionBlocked("memory_not_found")
    return item, fresh, row_to_dict(row)


def _apply_mutation(
    conn: Any,
    *,
    item: dict[str, Any],
    memory: dict[str, Any],
    applied_by: str,
    notes: str | None,
    now: str,
    row_to_dict: Callable[[Any], dict[str, Any]],
    insert_memory_event: Callable[..., dict[str, Any]],
    canonical_json_hash: Callable[[Any], str],
    compute_sla_days: Callable[..., int],
    shift_iso_days: Callable[[str, int], str],
    batch_manifest_identifiers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action = str(item["proposed_action"])
    before = {field: memory.get(field) for field in ROLLBACK_FIELDS}
    canonical = derive_canonical_memory_state(
        state_code=memory.get("state_code"), activity_state=memory.get("activity_state"),
        contradiction_flag=memory.get("contradiction_flag"),
    )
    if action == "revalidate":
        if canonical != "validated":
            raise RetentionBlocked("revalidate_requires_validated")
        days = compute_sla_days(conn, "revalidation", memory.get("priority"), memory.get("memory_type"), memory.get("scope_code"), memory.get("project_key"))
        conn.execute(
            "UPDATE memories SET last_validated_at=?,last_confirmed_at=?,validation_source=?,revalidation_due_at=?,requires_user_confirmation=0,updated_at=? WHERE id=?",
            (now, now, "memory_v3_retention_revalidate", shift_iso_days(now, days), now, int(item["memory_id"])),
        )
        event_type = "memory_v3.retention_revalidated"
    elif action == "archive_candidate":
        if not is_transition_allowed(canonical, "archived"):
            raise RetentionBlocked(f"archive_transition_not_allowed:{canonical}")
        conn.execute(
            "UPDATE memories SET state_code='archived',memory_v2_status=?,activity_state='archived',archived_at=?,updated_at=? WHERE id=?",
            (project_memory_v2_status(state_code="archived"), now, now, int(item["memory_id"])),
        )
        event_type = "memory_v3.retention_archived"
    elif action == "expire_candidate":
        if canonical != "validated" or not is_transition_allowed(canonical, "stale"):
            raise RetentionBlocked(f"expire_transition_not_allowed:{canonical}")
        days = compute_sla_days(conn, "review", memory.get("priority"), memory.get("memory_type"), memory.get("scope_code"), memory.get("project_key"))
        conn.execute(
            "UPDATE memories SET state_code='stale',memory_v2_status=?,activity_state='active',valid_to=COALESCE(valid_to,?),requires_user_confirmation=1,review_due_at=?,updated_at=? WHERE id=?",
            (project_memory_v2_status(state_code="stale"), now, shift_iso_days(now, days), now, int(item["memory_id"])),
        )
        event_type = "memory_v3.retention_expired"
    else:
        raise RetentionBlocked("outcome_not_supported")
    event_payload = {
        "review_item_id": int(item["id"]),
        "action": action,
        "applied_at": now,
        "applied_by": str(applied_by).strip(),
        "preview_hash": item["preview_hash"],
        "source": "memory_v3_retention_apply",
    }
    if batch_manifest_identifiers is not None:
        event_payload["batch_manifest_identifiers"] = batch_manifest_identifiers
    event = insert_memory_event(
        conn, memory_id=int(item["memory_id"]), event_type=event_type,
        payload=event_payload,
    )
    after_row = conn.execute("SELECT * FROM memories WHERE id=?", (int(item["memory_id"]),)).fetchone()
    after = _snapshot(after_row, row_to_dict)
    event_ids = [int(event["id"])]
    fingerprint = _result_hash(
        canonical_json_hash,
        item_id=int(item["id"]),
        action=action,
        before=before,
        after=after,
        event_ids=event_ids,
        batch_manifest_identifiers=batch_manifest_identifiers,
    )
    conn.execute(
        "UPDATE memory_retention_review_items SET status='applied',before_snapshot_json=?,applied_snapshot_json=?,created_event_ids_json=?,applied_at=?,applied_by=?,apply_note=?,apply_result_fingerprint=?,updated_at=? WHERE id=? AND status='approved'",
        (_dumps(before), _dumps(after), _dumps(event_ids), now, str(applied_by).strip(), str(notes).strip() if notes else None, fingerprint, now, int(item["id"])),
    )
    return {"status": "applied", "schema_version": RETENTION_APPLY_SCHEMA_VERSION, "review_item_id": int(item["id"]), "memory_id": int(item["memory_id"]), "action": action, "created_event_ids": event_ids, "apply_result_fingerprint": fingerprint, "safety": {"physical_delete_performed": False}}


def apply_memory_retention_review_payload(
    conn: Any, *, review_item_id: int, expected_preview_hash: str, applied_by: str, notes: str | None,
    include_debug: bool, row_to_dict: Callable, preview_func: Callable, memory_v2_enabled: Callable,
    retention_flag_evaluation: Callable, insert_memory_event: Callable, canonical_json_hash: Callable,
    utc_now_iso: Callable[[], str], compute_sla_days: Callable, shift_iso_days: Callable,
) -> dict[str, Any]:
    existing = get_retention_review_item(conn, review_item_id=int(review_item_id), row_to_dict=row_to_dict)
    if existing["status"] == "applied":
        normalized_expected = str(expected_preview_hash or "").strip()
        stored_preview_hash = str(existing.get("preview_hash") or "").strip()
        contract_fields = {
            "expected_preview_hash": normalized_expected,
            "stored_preview_hash": stored_preview_hash,
        }
        if not normalized_expected or normalized_expected != stored_preview_hash:
            return {
                "status": "blocked",
                "schema_version": RETENTION_APPLY_SCHEMA_VERSION,
                "review_item_id": int(review_item_id),
                "blocking_reasons": ["applied_item_contract_mismatch"],
                **contract_fields,
            }
        current = conn.execute("SELECT * FROM memories WHERE id=?", (int(existing["memory_id"]),)).fetchone()
        current_snapshot = _snapshot(current, row_to_dict) if current is not None else None
        if _applied_audit_is_complete(conn, existing, current_snapshot=current_snapshot, canonical_json_hash=canonical_json_hash):
            return {
                "status": "already_applied",
                "schema_version": RETENTION_APPLY_SCHEMA_VERSION,
                "review_item_id": int(review_item_id),
                "created_event_ids": existing["created_event_ids"],
                **contract_fields,
            }
        return {
            "status": "blocked",
            "schema_version": RETENTION_APPLY_SCHEMA_VERSION,
            "review_item_id": int(review_item_id),
            "blocking_reasons": ["applied_item_audit_integrity_mismatch"],
            **contract_fields,
        }
    try:
        conn.execute("BEGIN IMMEDIATE")
        item, _fresh, memory = _preflight(conn, item_id=int(review_item_id), expected_preview_hash=expected_preview_hash, applied_by=applied_by, row_to_dict=row_to_dict, preview_func=preview_func, memory_v2_enabled=memory_v2_enabled, retention_flag_evaluation=retention_flag_evaluation)
        result = _apply_mutation(conn, item=item, memory=memory, applied_by=applied_by, notes=notes, now=utc_now_iso(), row_to_dict=row_to_dict, insert_memory_event=insert_memory_event, canonical_json_hash=canonical_json_hash, compute_sla_days=compute_sla_days, shift_iso_days=shift_iso_days)
        conn.commit()
        return result
    except RetentionBlocked as exc:
        conn.rollback()
        reason = str(exc)
        return {"status": "stale_preview" if reason == "stale_preview" else ("outcome_not_supported" if reason == "outcome_not_supported" else "blocked"), "schema_version": RETENTION_APPLY_SCHEMA_VERSION, "blocking_reasons": [reason]}
    except Exception as exc:
        conn.rollback()
        return {"status": "error", "schema_version": RETENTION_APPLY_SCHEMA_VERSION, "error_type": type(exc).__name__, "safety": {"transaction_rolled_back": True}}


def preview_memory_retention_rollback_payload(conn: Any, *, review_item_id: int, row_to_dict: Callable, canonical_json_hash: Callable) -> dict[str, Any]:
    item = get_retention_review_item(conn, review_item_id=int(review_item_id), row_to_dict=row_to_dict)
    if item["status"] != "applied" or item.get("proposed_action") not in ROLLBACK_ACTIONS:
        return {"status": "blocked", "schema_version": RETENTION_ROLLBACK_PREVIEW_SCHEMA_VERSION, "blocking_reasons": ["rollback_not_supported_for_item_state"]}
    row = conn.execute("SELECT * FROM memories WHERE id=?", (int(item["memory_id"]),)).fetchone()
    current = _snapshot(row, row_to_dict) if row is not None else None
    blockers = []
    if current != item.get("applied_snapshot"):
        blockers.append("memory_changed_after_apply")
    later = conn.execute("SELECT id FROM memory_retention_review_items WHERE memory_id=? AND status='applied' AND applied_at>? AND id<>? LIMIT 1", (int(item["memory_id"]), item["applied_at"], int(item["id"]))).fetchone()
    if later is not None:
        blockers.append("later_retention_operation_exists")
    contract = {"schema_version": RETENTION_ROLLBACK_PREVIEW_SCHEMA_VERSION, "review_item_id": int(item["id"]), "memory_id": int(item["memory_id"]), "action": item["proposed_action"], "before_snapshot": item.get("before_snapshot"), "applied_snapshot": item.get("applied_snapshot"), "current_snapshot": current, "apply_result_fingerprint": item.get("apply_result_fingerprint")}
    rollback_hash = canonical_json_hash(contract)
    return {**contract, "status": "preview_ready" if not blockers else "blocked", "rollback_preview_hash": rollback_hash, "guard": {"allowed": not blockers, "blockers": blockers}, "safety": {"read_only": True, "raw_secret_exposed": False}}


def rollback_memory_retention_review_payload(conn: Any, *, review_item_id: int, expected_rollback_preview_hash: str, rolled_back_by: str, notes: str | None, row_to_dict: Callable, canonical_json_hash: Callable, utc_now_iso: Callable[[], str], insert_memory_event: Callable) -> dict[str, Any]:
    if not str(rolled_back_by or "").strip():
        return {"status": "blocked", "blocking_reasons": ["rolled_back_by_required"]}
    item = get_retention_review_item(conn, review_item_id=int(review_item_id), row_to_dict=row_to_dict)
    if item["status"] == "rolled_back":
        return {"status": "already_rolled_back", "review_item_id": int(review_item_id)}
    preview = preview_memory_retention_rollback_payload(conn, review_item_id=int(review_item_id), row_to_dict=row_to_dict, canonical_json_hash=canonical_json_hash)
    if preview["status"] != "preview_ready":
        return preview
    if str(expected_rollback_preview_hash or "").strip() != preview["rollback_preview_hash"]:
        return {"status": "stale_preview", "blocking_reasons": ["rollback_preview_hash_mismatch"]}
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh = preview_memory_retention_rollback_payload(conn, review_item_id=int(review_item_id), row_to_dict=row_to_dict, canonical_json_hash=canonical_json_hash)
        if fresh["status"] != "preview_ready" or fresh["rollback_preview_hash"] != preview["rollback_preview_hash"]:
            raise RetentionBlocked("rollback_state_changed")
        before = item["before_snapshot"]
        assignments = ",".join(f"{field}=?" for field in ROLLBACK_FIELDS)
        conn.execute(f"UPDATE memories SET {assignments} WHERE id=?", [*(before[field] for field in ROLLBACK_FIELDS), int(item["memory_id"])])
        now = utc_now_iso()
        event = insert_memory_event(conn, memory_id=int(item["memory_id"]), event_type="memory_v3.retention_rolled_back", payload={"review_item_id": int(item["id"]), "rolled_back_at": now, "rolled_back_by": str(rolled_back_by).strip(), "apply_result_fingerprint": item["apply_result_fingerprint"]})
        restored = _snapshot(conn.execute("SELECT * FROM memories WHERE id=?", (int(item["memory_id"]),)).fetchone(), row_to_dict)
        conn.execute("UPDATE memory_retention_review_items SET status='rolled_back',rollback_preview_hash=?,rollback_snapshot_json=?,rolled_back_at=?,rolled_back_by=?,rollback_note=?,updated_at=? WHERE id=?", (preview["rollback_preview_hash"], _dumps({"restored_snapshot": restored, "rollback_event_id": int(event["id"])}), now, str(rolled_back_by).strip(), str(notes).strip() if notes else None, now, int(item["id"])))
        conn.commit()
        return {"status": "rolled_back", "review_item_id": int(item["id"]), "memory_id": int(item["memory_id"]), "rollback_event_id": int(event["id"])}
    except Exception as exc:
        conn.rollback()
        return {"status": "blocked" if isinstance(exc, RetentionBlocked) else "error", "blocking_reasons": [str(exc)] if isinstance(exc, RetentionBlocked) else [], "error_type": type(exc).__name__}


BATCH_BACKUP_SCHEMA_VERSION = "memory_v3_retention_batch_backup.v1"
BATCH_APPLY_SCHEMA_VERSION = "memory_v3_retention_batch_apply.v2"
PROTECTED_TABLES = (
    "memories",
    "memory_links",
    "memory_events",
    "memory_lifecycle_snapshots",
    "memory_capture_review_items",
    "memory_retention_review_items",
    "timeline_events",
)
REVIEW_IMMUTABLE_FIELDS = (
    "id",
    "operation_key",
    "memory_id",
    "status",
    "project_key",
    "scope_code",
    "workspace_id",
    "as_of",
    "sensitivity_class",
    "retention_class",
    "policy_outcome",
    "proposed_action",
    "input_fingerprint",
    "preview_hash",
)


def _trusted_source_path(conn: Any, source_db_path: Path) -> Path:
    rows = conn.execute("PRAGMA database_list").fetchall()
    main = next((row for row in rows if str(row[1]) == "main"), None)
    actual = Path(str(main[2])).resolve() if main is not None and str(main[2]).strip() else None
    trusted = Path(source_db_path).resolve()
    if actual is None or actual != trusted or not trusted.is_file():
        raise RetentionBlocked("trusted_source_identity_mismatch")
    return trusted


def _batch_logical_state(
    conn: Any,
    *,
    review_item_ids: list[int],
    expected_preview_hashes: dict[str, str],
    boundary: tuple[Any, Any, Any],
    batch_operation_key: str,
    source_identity_hash: str,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    migrations = [str(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC")]
    protected_counts = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in PROTECTED_TABLES
    }
    review_items: list[dict[str, Any]] = []
    target_snapshots: list[dict[str, Any]] = []
    for item_id in review_item_ids:
        row = conn.execute("SELECT * FROM memory_retention_review_items WHERE id=?", (int(item_id),)).fetchone()
        if row is None:
            raise RetentionBlocked(f"review_item_not_found:{item_id}")
        item = row_to_dict(row)
        review_items.append({field: item.get(field) for field in REVIEW_IMMUTABLE_FIELDS})
        memory_row = conn.execute("SELECT * FROM memories WHERE id=?", (int(item["memory_id"]),)).fetchone()
        if memory_row is None:
            raise RetentionBlocked(f"memory_not_found:{item['memory_id']}")
        target_snapshots.append(
            {
                "memory_id": int(item["memory_id"]),
                "rollback_snapshot": _snapshot(memory_row, row_to_dict),
            }
        )
    return {
        "schema_version": "memory_v3_retention_batch_state.v1",
        "policy_version": RETENTION_POLICY_VERSION,
        "batch_operation_key": batch_operation_key,
        "source_identity_hash": source_identity_hash,
        "schema_migrations": migrations,
        "latest_migration": migrations[-1] if migrations else None,
        "protected_table_counts": protected_counts,
        "review_item_immutable_snapshots": review_items,
        "target_memory_rollback_snapshots": target_snapshots,
        "expected_preview_hashes": [
            {"review_item_id": item_id, "preview_hash": str(expected_preview_hashes[str(item_id)])}
            for item_id in review_item_ids
        ],
        "boundary": {
            "project_key": boundary[0],
            "scope_code": boundary[1],
            "workspace_id": boundary[2],
        },
    }


def create_verified_retention_batch_backup(
    *,
    source_db_path: Path,
    backups_root: Path,
    batch_operation_key: str,
    expected_logical_state: dict[str, Any],
    expected_state_fingerprint: str,
    review_item_ids: list[int],
    expected_preview_hashes: dict[str, str],
    boundary: tuple[Any, Any, Any],
    source_identity_hash: str,
    utc_now_iso: Callable[[], str],
    canonical_json_hash: Callable[[Any], str],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    source_path = Path(source_db_path).resolve()
    root = Path(backups_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    created_at = utc_now_iso()
    timestamp = re.sub(r"[^0-9]", "", created_at)[:14] or "unknown"
    backup_path = root / f"agent_memory-retention-batch-{timestamp}-{batch_operation_key[:12]}.db"
    if backup_path.exists():
        raise RetentionBlocked("trusted_backup_path_collision")
    source = None
    destination = None
    try:
        source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        destination = sqlite3.connect(backup_path)
        destination.row_factory = sqlite3.Row
        source.backup(destination)
        destination.commit()
        destination.close()
        destination = None
        source.close()
        source = None

        check = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
        check.row_factory = sqlite3.Row
        try:
            quick_check = str(check.execute("PRAGMA quick_check").fetchone()[0])
            if quick_check != "ok":
                raise RetentionBlocked("backup_quick_check_failed")
            backup_state = _batch_logical_state(
                check,
                review_item_ids=review_item_ids,
                expected_preview_hashes=expected_preview_hashes,
                boundary=boundary,
                batch_operation_key=batch_operation_key,
                source_identity_hash=source_identity_hash,
                row_to_dict=row_to_dict,
            )
        finally:
            check.close()
        backup_fingerprint = canonical_json_hash(backup_state)
        if backup_state != expected_logical_state or backup_fingerprint != expected_state_fingerprint:
            raise RetentionBlocked("source_backup_logical_fingerprint_mismatch")
        backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        try:
            relative_path = backup_path.relative_to(root.parent).as_posix()
        except ValueError:
            relative_path = backup_path.name
        return {
            "schema_version": BATCH_BACKUP_SCHEMA_VERSION,
            "batch_operation_key": batch_operation_key,
            "backup_relative_path": relative_path,
            "backup_sha256": backup_sha256,
            "size_bytes": backup_path.stat().st_size,
            "quick_check": "ok",
            "created_at": created_at,
            "source_identity_hash": source_identity_hash,
            "pre_batch_state_fingerprint": expected_state_fingerprint,
            "latest_migration": expected_logical_state["latest_migration"],
            "protected_table_counts": expected_logical_state["protected_table_counts"],
        }
    except Exception:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        if backup_path.exists():
            backup_path.unlink()
        raise


def _batch_manifest_identifiers(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_operation_key": manifest["batch_operation_key"],
        "backup_sha256": manifest["backup_sha256"],
        "backup_relative_path": manifest["backup_relative_path"],
        "pre_batch_state_fingerprint": manifest["pre_batch_state_fingerprint"],
    }


def apply_memory_retention_batch_payload(
    conn: Any,
    *,
    review_item_ids: list[int],
    expected_preview_hashes: dict[str, str],
    applied_by: str,
    notes: str | None,
    source_db_path: Path,
    backups_root: Path,
    row_to_dict: Callable,
    preview_func: Callable,
    memory_v2_enabled: Callable,
    retention_flag_evaluation: Callable,
    insert_memory_event: Callable,
    canonical_json_hash: Callable,
    utc_now_iso: Callable[[], str],
    compute_sla_days: Callable,
    shift_iso_days: Callable,
    create_batch_backup: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    backup_helper = create_batch_backup or create_verified_retention_batch_backup
    try:
        ids = [int(value) for value in review_item_ids]
        if not str(applied_by or "").strip():
            raise RetentionBlocked("applied_by_required")
        if not 1 <= len(ids) <= 10 or len(set(ids)) != len(ids):
            raise RetentionBlocked("batch_size_or_duplicate_item_invalid")
        normalized_hashes = {str(key): str(value or "").strip() for key, value in expected_preview_hashes.items()}
        if set(normalized_hashes) != {str(item_id) for item_id in ids} or any(not value for value in normalized_hashes.values()):
            raise RetentionBlocked("expected_preview_hashes_contract_invalid")
        trusted_source = _trusted_source_path(conn, Path(source_db_path))

        conn.execute("BEGIN IMMEDIATE")
        prepared: list[tuple[dict[str, Any], dict[str, Any]]] = []
        boundaries: set[tuple[Any, Any, Any]] = set()
        memory_ids: set[int] = set()
        for item_id in ids:
            item, _fresh, memory = _preflight(
                conn,
                item_id=item_id,
                expected_preview_hash=normalized_hashes[str(item_id)],
                applied_by=applied_by,
                row_to_dict=row_to_dict,
                preview_func=preview_func,
                memory_v2_enabled=memory_v2_enabled,
                retention_flag_evaluation=retention_flag_evaluation,
            )
            boundaries.add((item.get("project_key"), item.get("scope_code"), item.get("workspace_id")))
            if int(item["memory_id"]) in memory_ids:
                raise RetentionBlocked("duplicate_memory_id_in_batch")
            memory_ids.add(int(item["memory_id"]))
            prepared.append((item, memory))
        if len(boundaries) != 1:
            raise RetentionBlocked("cross_boundary_batch")
        boundary = next(iter(boundaries))
        source_identity_hash = canonical_json_hash({"trusted_source_path": str(trusted_source).casefold()})
        batch_operation_key = canonical_json_hash(
            {
                "schema_version": BATCH_APPLY_SCHEMA_VERSION,
                "review_item_ids": ids,
                "expected_preview_hashes": normalized_hashes,
                "boundary": boundary,
                "source_identity_hash": source_identity_hash,
                "policy_version": RETENTION_POLICY_VERSION,
            }
        )
        pre_batch_state = _batch_logical_state(
            conn,
            review_item_ids=ids,
            expected_preview_hashes=normalized_hashes,
            boundary=boundary,
            batch_operation_key=batch_operation_key,
            source_identity_hash=source_identity_hash,
            row_to_dict=row_to_dict,
        )
        pre_batch_state_fingerprint = canonical_json_hash(pre_batch_state)
        manifest = backup_helper(
            source_db_path=trusted_source,
            backups_root=backups_root,
            batch_operation_key=batch_operation_key,
            expected_logical_state=pre_batch_state,
            expected_state_fingerprint=pre_batch_state_fingerprint,
            review_item_ids=ids,
            expected_preview_hashes=normalized_hashes,
            boundary=boundary,
            source_identity_hash=source_identity_hash,
            utc_now_iso=utc_now_iso,
            canonical_json_hash=canonical_json_hash,
            row_to_dict=row_to_dict,
        )

        fresh_prepared: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item_id in ids:
            item, _fresh, memory = _preflight(
                conn,
                item_id=item_id,
                expected_preview_hash=normalized_hashes[str(item_id)],
                applied_by=applied_by,
                row_to_dict=row_to_dict,
                preview_func=preview_func,
                memory_v2_enabled=memory_v2_enabled,
                retention_flag_evaluation=retention_flag_evaluation,
            )
            fresh_prepared.append((item, memory))
        fresh_state = _batch_logical_state(
            conn,
            review_item_ids=ids,
            expected_preview_hashes=normalized_hashes,
            boundary=boundary,
            batch_operation_key=batch_operation_key,
            source_identity_hash=source_identity_hash,
            row_to_dict=row_to_dict,
        )
        if canonical_json_hash(fresh_state) != pre_batch_state_fingerprint:
            raise RetentionBlocked("fresh_state_changed_after_backup")

        identifiers = _batch_manifest_identifiers(manifest)
        now = utc_now_iso()
        results = [
            _apply_mutation(
                conn,
                item=item,
                memory=memory,
                applied_by=applied_by,
                notes=notes,
                now=now,
                row_to_dict=row_to_dict,
                insert_memory_event=insert_memory_event,
                canonical_json_hash=canonical_json_hash,
                compute_sla_days=compute_sla_days,
                shift_iso_days=shift_iso_days,
                batch_manifest_identifiers=identifiers,
            )
            for item, memory in fresh_prepared
        ]
        conn.commit()
        return {
            "status": "applied",
            "schema_version": BATCH_APPLY_SCHEMA_VERSION,
            "batch_operation_key": batch_operation_key,
            "results": results,
            "backup_manifest": manifest,
        }
    except RetentionBlocked as exc:
        conn.rollback()
        return {"status": "blocked", "schema_version": BATCH_APPLY_SCHEMA_VERSION, "blocking_reasons": [str(exc)]}
    except Exception as exc:
        conn.rollback()
        return {
            "status": "error",
            "schema_version": BATCH_APPLY_SCHEMA_VERSION,
            "error_type": type(exc).__name__,
            "safety": {"transaction_rolled_back": True},
        }
