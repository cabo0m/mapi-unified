from __future__ import annotations

import json
from typing import Any, Callable


LIFECYCLE_SNAPSHOT_SCHEMA_VERSION = "memory_v3_lifecycle_snapshot.v1"
LIFECYCLE_SNAPSHOT_OPERATION_TYPES = frozenset(
    {"supersession", "legacy_lineage_remediation", "pointer_lineage_remediation"}
)
LIFECYCLE_SNAPSHOT_STATUSES = frozenset({"applying", "applied", "rolled_back", "failed"})
APPLYING_STATUS = "applying"
APPLIED_STATUS = "applied"
ROLLED_BACK_STATUS = "rolled_back"
FAILED_STATUS = "failed"


def _normalize_enum(value: str | None, *, allowed: set[str] | frozenset[str], field_name: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized not in allowed:
        raise ValueError(f"{field_name} must be one of: {', '.join(sorted(allowed))}")
    return normalized


def _require_json_object(payload: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return dict(payload)


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_pending_snapshot(payload: Any, *, field_name: str) -> dict[str, Any]:
    normalized = _require_json_object(payload, field_name=field_name)
    if set(normalized) != {"pending"}:
        raise ValueError(f"{field_name} must contain exactly one pending section")
    return normalized


def lifecycle_snapshot_to_dict(
    row: Any,
    *,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    item = row_to_dict(row)
    for key in (
        "before_snapshot_json",
        "after_snapshot_json",
        "link_snapshot_json",
        "event_snapshot_json",
        "rollback_snapshot_json",
    ):
        raw = item.get(key)
        decoded = json.loads(raw) if isinstance(raw, str) and raw.strip() else None
        item[key.removesuffix("_json")] = decoded
    item["storage_operation_type"] = item.get("operation_type")
    if item.get("operation_type") == "supersession" and item.get("relation_kind") == "legacy_chain_repair":
        item["operation_type"] = "legacy_lineage_remediation"
    item["schema_version"] = LIFECYCLE_SNAPSHOT_SCHEMA_VERSION
    return item


def _require_snapshot_row(
    conn: Any,
    *,
    snapshot_id: int,
) -> Any:
    row = conn.execute(
        "SELECT * FROM memory_lifecycle_snapshots WHERE id = ?",
        (int(snapshot_id),),
    ).fetchone()
    if row is None:
        raise FileNotFoundError(f"Nie znaleziono lifecycle snapshot o id={snapshot_id}")
    return row


def get_lifecycle_snapshot_payload(
    conn: Any,
    *,
    snapshot_id: int,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    row = _require_snapshot_row(conn, snapshot_id=int(snapshot_id))
    return lifecycle_snapshot_to_dict(row, row_to_dict=row_to_dict)


def find_lifecycle_snapshot_by_operation_key(
    conn: Any,
    *,
    operation_key: str,
    normalize_required_text: Callable[[Any, str], str],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any] | None:
    normalized_operation_key = normalize_required_text(operation_key, "operation_key")
    row = conn.execute(
        "SELECT * FROM memory_lifecycle_snapshots WHERE operation_key = ?",
        (normalized_operation_key,),
    ).fetchone()
    if row is None:
        return None
    return lifecycle_snapshot_to_dict(row, row_to_dict=row_to_dict)


def list_lifecycle_snapshots_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    new_memory_id: int | None = None,
    old_memory_id: int | None = None,
    status: str | None = None,
    operation_type: str | None = "supersession",
    limit: int = 50,
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 200:
        return {
            "status": "error",
            "schema_version": "memory_v3_lifecycle_snapshots.v1",
            "error": "limit musi byc w zakresie 1..200",
        }

    normalized_project_key = normalize_optional_text(project_key)
    normalized_status = (
        _normalize_enum(status, allowed=LIFECYCLE_SNAPSHOT_STATUSES, field_name="status")
        if normalize_optional_text(status) is not None
        else None
    )
    normalized_operation_type = (
        _normalize_enum(
            operation_type,
            allowed=LIFECYCLE_SNAPSHOT_OPERATION_TYPES,
            field_name="operation_type",
        )
        if normalize_optional_text(operation_type) is not None
        else None
    )
    sql = """
        SELECT s.*
        FROM memory_lifecycle_snapshots s
        JOIN memories new_memory ON new_memory.id = s.new_memory_id
        JOIN memories old_memory ON old_memory.id = s.old_memory_id
        WHERE 1 = 1
    """
    params: list[Any] = []
    if normalized_project_key is not None:
        sql += " AND new_memory.project_key = ? AND old_memory.project_key = ?"
        params.extend([normalized_project_key, normalized_project_key])
    if new_memory_id is not None:
        sql += " AND s.new_memory_id = ?"
        params.append(int(new_memory_id))
    if old_memory_id is not None:
        sql += " AND s.old_memory_id = ?"
        params.append(int(old_memory_id))
    if normalized_status is not None:
        sql += " AND s.status = ?"
        params.append(normalized_status)
    if normalized_operation_type == "legacy_lineage_remediation":
        sql += " AND s.operation_type = 'supersession' AND s.relation_kind = 'legacy_chain_repair'"
    elif normalized_operation_type == "supersession":
        sql += " AND s.operation_type = 'supersession' AND s.relation_kind != 'legacy_chain_repair'"
    sql += " ORDER BY s.id DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    items = [lifecycle_snapshot_to_dict(row, row_to_dict=row_to_dict) for row in rows]
    return {
        "status": "ok",
        "schema_version": "memory_v3_lifecycle_snapshots.v1",
        "filters": {
            "project_key": normalized_project_key,
            "new_memory_id": None if new_memory_id is None else int(new_memory_id),
            "old_memory_id": None if old_memory_id is None else int(old_memory_id),
            "status": normalized_status,
            "operation_type": normalized_operation_type,
            "limit": int(limit),
        },
        "summary": {
            "total_returned": len(items),
        },
        "runs": items,
        "safety": {
            "read_only": True,
            "mutates_memory_entries": False,
        },
    }


def create_lifecycle_snapshot_payload(
    conn: Any,
    *,
    operation_key: str,
    new_memory_id: int,
    old_memory_id: int,
    relation_kind: str,
    reason: str,
    input_fingerprint: str,
    candidate_set_fingerprint: str,
    preview_hash: str,
    before_snapshot: dict[str, Any],
    after_snapshot: dict[str, Any],
    link_snapshot: dict[str, Any],
    event_snapshot: dict[str, Any],
    applied_at: str | None = None,
    applied_by: str | None = None,
    apply_note: str | None = None,
    operation_type: str = "supersession",
    status: str = "applied",
    utc_now_iso: Callable[[], str] | None = None,
    normalize_required_text: Callable[[Any, str], str] | None = None,
    normalize_optional_text: Callable[[Any], str | None] | None = None,
    row_to_dict: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if utc_now_iso is None or normalize_required_text is None or normalize_optional_text is None or row_to_dict is None:
        raise ValueError("required helper callbacks are missing")

    normalized_operation_key = normalize_required_text(operation_key, "operation_key")
    normalized_operation_type = _normalize_enum(
        operation_type,
        allowed=LIFECYCLE_SNAPSHOT_OPERATION_TYPES,
        field_name="operation_type",
    )
    storage_operation_type = (
        "supersession"
        if normalized_operation_type == "legacy_lineage_remediation"
        else normalized_operation_type
    )
    normalized_status = _normalize_enum(
        status,
        allowed=LIFECYCLE_SNAPSHOT_STATUSES,
        field_name="status",
    )
    created_at = utc_now_iso()
    effective_applied_at = normalize_optional_text(applied_at) or created_at

    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO memory_lifecycle_snapshots (
            operation_key,
            operation_type,
            status,
            new_memory_id,
            old_memory_id,
            relation_kind,
            reason,
            input_fingerprint,
            candidate_set_fingerprint,
            preview_hash,
            before_snapshot_json,
            after_snapshot_json,
            link_snapshot_json,
            event_snapshot_json,
            applied_at,
            started_at,
            applied_by,
            apply_note,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            normalized_operation_key,
            storage_operation_type,
            normalized_status,
            int(new_memory_id),
            int(old_memory_id),
            normalize_required_text(relation_kind, "relation_kind"),
            normalize_required_text(reason, "reason"),
            normalize_required_text(input_fingerprint, "input_fingerprint"),
            normalize_required_text(candidate_set_fingerprint, "candidate_set_fingerprint"),
            normalize_required_text(preview_hash, "preview_hash"),
            _json_dumps(_require_json_object(before_snapshot, field_name="before_snapshot")),
            _json_dumps(_require_json_object(after_snapshot, field_name="after_snapshot")),
            _json_dumps(_require_json_object(link_snapshot, field_name="link_snapshot")),
            _json_dumps(_require_json_object(event_snapshot, field_name="event_snapshot")),
            effective_applied_at,
            created_at,
            normalize_optional_text(applied_by),
            normalize_optional_text(apply_note),
            created_at,
            created_at,
        ),
    )
    row = conn.execute(
        "SELECT * FROM memory_lifecycle_snapshots WHERE id = ?",
        (int(cursor.lastrowid),),
    ).fetchone()
    return lifecycle_snapshot_to_dict(row, row_to_dict=row_to_dict)


def create_applying_lifecycle_snapshot_payload(
    conn: Any,
    *,
    operation_key: str,
    new_memory_id: int,
    old_memory_id: int,
    relation_kind: str,
    reason: str,
    input_fingerprint: str,
    candidate_set_fingerprint: str,
    preview_hash: str,
    before_snapshot: dict[str, Any],
    after_snapshot: dict[str, Any],
    link_snapshot: dict[str, Any],
    event_snapshot: dict[str, Any],
    applied_by: str,
    apply_note: str | None = None,
    utc_now_iso: Callable[[], str] | None = None,
    normalize_required_text: Callable[[Any, str], str] | None = None,
    normalize_optional_text: Callable[[Any], str | None] | None = None,
    row_to_dict: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if utc_now_iso is None or normalize_required_text is None or normalize_optional_text is None or row_to_dict is None:
        raise ValueError("required helper callbacks are missing")

    created_at = utc_now_iso()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO memory_lifecycle_snapshots (
            operation_key,
            operation_type,
            status,
            new_memory_id,
            old_memory_id,
            relation_kind,
            reason,
            input_fingerprint,
            candidate_set_fingerprint,
            preview_hash,
            before_snapshot_json,
            after_snapshot_json,
            link_snapshot_json,
            event_snapshot_json,
            applied_at,
            started_at,
            applied_by,
            apply_note,
            created_at,
            updated_at
        )
        VALUES (?, 'pointer_lineage_remediation', 'applying', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        (
            normalize_required_text(operation_key, "operation_key"),
            int(new_memory_id),
            int(old_memory_id),
            normalize_required_text(relation_kind, "relation_kind"),
            normalize_required_text(reason, "reason"),
            normalize_required_text(input_fingerprint, "input_fingerprint"),
            normalize_required_text(candidate_set_fingerprint, "candidate_set_fingerprint"),
            normalize_required_text(preview_hash, "preview_hash"),
            _json_dumps(_require_json_object(before_snapshot, field_name="before_snapshot")),
            _json_dumps(_require_pending_snapshot(after_snapshot, field_name="after_snapshot")),
            _json_dumps(_require_pending_snapshot(link_snapshot, field_name="link_snapshot")),
            _json_dumps(_require_pending_snapshot(event_snapshot, field_name="event_snapshot")),
            created_at,
            normalize_required_text(applied_by, "applied_by"),
            normalize_optional_text(apply_note),
            created_at,
            created_at,
        ),
    )
    return get_lifecycle_snapshot_payload(
        conn,
        snapshot_id=int(cursor.lastrowid),
        row_to_dict=row_to_dict,
    )


def finalize_lifecycle_snapshot_applied_payload(
    conn: Any,
    *,
    snapshot_id: int,
    after_snapshot: dict[str, Any],
    link_snapshot: dict[str, Any],
    event_snapshot: dict[str, Any],
    applied_at: str | None = None,
    utc_now_iso: Callable[[], str] | None = None,
    normalize_optional_text: Callable[[Any], str | None] | None = None,
    row_to_dict: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if utc_now_iso is None or normalize_optional_text is None or row_to_dict is None:
        raise ValueError("required helper callbacks are missing")
    current = get_lifecycle_snapshot_payload(
        conn,
        snapshot_id=int(snapshot_id),
        row_to_dict=row_to_dict,
    )
    normalized_after = _require_json_object(after_snapshot, field_name="after_snapshot")
    normalized_links = _require_json_object(link_snapshot, field_name="link_snapshot")
    normalized_events = _require_json_object(event_snapshot, field_name="event_snapshot")
    effective_applied_at = normalize_optional_text(applied_at) or utc_now_iso()

    if current["status"] == APPLIED_STATUS:
        if (
            current.get("after_snapshot") == normalized_after
            and current.get("link_snapshot") == normalized_links
            and current.get("event_snapshot") == normalized_events
            and normalize_optional_text(current.get("applied_at")) == effective_applied_at
        ):
            return current
        raise ValueError("lifecycle snapshot is already applied with different final evidence")
    if current["status"] != APPLYING_STATUS:
        raise ValueError("only an applying lifecycle snapshot can be finalized")

    cursor = conn.execute(
        """
        UPDATE memory_lifecycle_snapshots
        SET status = 'applied',
            after_snapshot_json = ?,
            link_snapshot_json = ?,
            event_snapshot_json = ?,
            applied_at = ?,
            updated_at = ?
        WHERE id = ? AND status = 'applying'
        """,
        (
            _json_dumps(normalized_after),
            _json_dumps(normalized_links),
            _json_dumps(normalized_events),
            effective_applied_at,
            utc_now_iso(),
            int(snapshot_id),
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("applying lifecycle snapshot finalization did not update exactly one row")
    return get_lifecycle_snapshot_payload(conn, snapshot_id=int(snapshot_id), row_to_dict=row_to_dict)


def mark_lifecycle_snapshot_failed_payload(
    conn: Any,
    *,
    snapshot_id: int,
    failure_note: str,
    utc_now_iso: Callable[[], str] | None = None,
    normalize_required_text: Callable[[Any, str], str] | None = None,
    row_to_dict: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if utc_now_iso is None or normalize_required_text is None or row_to_dict is None:
        raise ValueError("required helper callbacks are missing")
    current = get_lifecycle_snapshot_payload(conn, snapshot_id=int(snapshot_id), row_to_dict=row_to_dict)
    normalized_note = normalize_required_text(failure_note, "failure_note")
    if current["status"] == FAILED_STATUS:
        if current.get("apply_note") == normalized_note and current.get("applied_at") is None:
            return current
        raise ValueError("lifecycle snapshot is already failed with different evidence")
    if current["status"] != APPLYING_STATUS:
        raise ValueError("only an applying lifecycle snapshot can be marked failed")
    cursor = conn.execute(
        """
        UPDATE memory_lifecycle_snapshots
        SET status = 'failed', apply_note = ?, updated_at = ?
        WHERE id = ? AND status = 'applying'
        """,
        (normalized_note, utc_now_iso(), int(snapshot_id)),
    )
    if cursor.rowcount != 1:
        raise ValueError("applying lifecycle snapshot failure mark did not update exactly one row")
    return get_lifecycle_snapshot_payload(conn, snapshot_id=int(snapshot_id), row_to_dict=row_to_dict)


def mark_lifecycle_snapshot_rolled_back_payload(
    conn: Any,
    *,
    snapshot_id: int,
    rollback_preview_hash: str,
    rollback_snapshot: dict[str, Any],
    rolled_back_at: str | None = None,
    rolled_back_by: str | None = None,
    rollback_note: str | None = None,
    utc_now_iso: Callable[[], str] | None = None,
    normalize_required_text: Callable[[Any, str], str] | None = None,
    normalize_optional_text: Callable[[Any], str | None] | None = None,
    row_to_dict: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if utc_now_iso is None or normalize_required_text is None or normalize_optional_text is None or row_to_dict is None:
        raise ValueError("required helper callbacks are missing")

    current = lifecycle_snapshot_to_dict(
        _require_snapshot_row(conn, snapshot_id=int(snapshot_id)),
        row_to_dict=row_to_dict,
    )
    normalized_preview_hash = normalize_required_text(rollback_preview_hash, "rollback_preview_hash")
    rollback_payload = _require_json_object(rollback_snapshot, field_name="rollback_snapshot")
    effective_rolled_back_at = normalize_optional_text(rolled_back_at) or utc_now_iso()
    normalized_rolled_back_by = normalize_optional_text(rolled_back_by)
    normalized_rollback_note = normalize_optional_text(rollback_note)

    if current["status"] == ROLLED_BACK_STATUS:
        if (
            normalize_optional_text(current.get("rollback_preview_hash")) == normalized_preview_hash
            and current.get("rollback_snapshot") == rollback_payload
            and normalize_optional_text(current.get("rolled_back_at")) == effective_rolled_back_at
            and normalize_optional_text(current.get("rolled_back_by")) == normalized_rolled_back_by
            and normalize_optional_text(current.get("rollback_note")) == normalized_rollback_note
        ):
            return current
        raise ValueError("lifecycle snapshot is already rolled_back with different rollback evidence")

    if current["status"] != APPLIED_STATUS:
        raise ValueError("only an applied lifecycle snapshot can be marked rolled_back")

    updated_at = utc_now_iso()
    cursor = conn.execute(
        """
        UPDATE memory_lifecycle_snapshots
        SET status = ?,
            rollback_preview_hash = ?,
            rollback_snapshot_json = ?,
            rolled_back_at = ?,
            rolled_back_by = ?,
            rollback_note = ?,
            updated_at = ?
        WHERE id = ? AND status = 'applied'
        """,
        (
            ROLLED_BACK_STATUS,
            normalized_preview_hash,
            _json_dumps(rollback_payload),
            effective_rolled_back_at,
            normalized_rolled_back_by,
            normalized_rollback_note,
            updated_at,
            int(snapshot_id),
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("applied lifecycle snapshot rollback mark did not update exactly one row")
    row = _require_snapshot_row(conn, snapshot_id=int(snapshot_id))
    return lifecycle_snapshot_to_dict(row, row_to_dict=row_to_dict)
