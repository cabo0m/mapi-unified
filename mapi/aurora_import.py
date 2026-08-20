from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from app.runtime.legacy_import import ensure_aurora_import_schema

IMPORT_SCHEMA = "mapi_aurora_import.v1"
TARGET_SCHEMA_VERSION = "0042_legacy_aurora_import"
ACTIVE_SENSITIVITY_CLASSES = frozenset({"public", "internal", "personal"})
QUARANTINE_CONTENT_CLASSES = frozenset({"health_sensitive", "financial_sensitive"})
SECRET_OMIT_CLASSES = frozenset({"credential_secret", "private_key", "never_store"})

ACTIVE_SOURCE_TABLES = frozenset(
    {
        "memories",
        "memory_events",
        "memory_links",
        "conversation_archives",
        "timeline_events",
        "aurora_onboarding",
    }
)
SECURITY_OR_DERIVED_TABLES = frozenset(
    {
        "schema_migrations",
        "memory_embeddings",
        "runtime_writer_leases",
        "workshop_idempotency",
    }
)
TARGET_ACTIVITY_TABLES = (
    "conversation_archives",
    "timeline_events",
    "memory_links",
    "memory_capture_review_items",
    "memory_consolidation_review_items",
    "ingest_items",
    "sleep_runs",
    "file_operations",
    "git_commit_operations",
    "git_stage_operations",
    "command_recipe_runs",
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    else:
        conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _verified_snapshot(source: Path, destination: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    src = _connect(source, read_only=True)
    dst = sqlite3.connect(destination, timeout=30)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    verify = _connect(destination, read_only=True)
    try:
        quick = str(verify.execute("PRAGMA quick_check").fetchone()[0])
        foreign_keys = verify.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        verify.close()
    if quick.casefold() != "ok" or foreign_keys:
        destination.unlink(missing_ok=True)
        raise RuntimeError("sqlite_snapshot_verification_failed")
    return {
        "path": destination,
        "sha256": _sha256_file(destination),
        "quick_check": quick,
        "foreign_key_findings": len(foreign_keys),
    }


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}


def _schema_tail(conn: sqlite3.Connection) -> str | None:
    if not _table_exists(conn, "schema_migrations"):
        return None
    row = conn.execute("SELECT version FROM schema_migrations ORDER BY rowid DESC LIMIT 1").fetchone()
    return str(row[0]) if row else None


def _recognize_aurora(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    required_tables = {"memories", "memory_events", "memory_links", "schema_migrations"}
    missing = sorted(required_tables - _table_names(conn))
    if missing:
        errors.append("aurora_required_tables_missing:" + ",".join(missing))
    memory_columns = _columns(conn, "memories")
    for required in ("sensitivity_class", "input_fingerprint", "agent_key"):
        if required not in memory_columns:
            errors.append(f"aurora_memory_column_missing:{required}")
    return errors


def _recognize_target(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    if _schema_tail(conn) != TARGET_SCHEMA_VERSION:
        errors.append(f"target_migration_required:{TARGET_SCHEMA_VERSION}")
    if not _table_exists(conn, "legacy_aurora_import_runs"):
        errors.append("target_import_ledger_missing")
    if "recall_count" not in _columns(conn, "memories"):
        errors.append("target_unified_memory_schema_missing")
    return errors


def _fresh_target_state(conn: sqlite3.Connection) -> dict[str, Any]:
    non_bootstrap = int(
        conn.execute(
            "SELECT COUNT(*) FROM memories WHERE COALESCE(source,'') <> 'mapi-init'"
        ).fetchone()[0]
    )
    activity: dict[str, int] = {}
    for table in TARGET_ACTIVITY_TABLES:
        if _table_exists(conn, table):
            activity[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    busy = {key: value for key, value in activity.items() if value > 0}
    return {
        "fresh": non_bootstrap == 0 and not busy,
        "non_bootstrap_memory_count": non_bootstrap,
        "activity_counts": activity,
        "blocking_activity": busy,
    }


def _sensitivity(value: Any) -> str:
    normalized = str(value or "internal").strip().casefold()
    return normalized or "internal"


def _source_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _table_names(conn)
    memories = [dict(row) for row in conn.execute("SELECT * FROM memories ORDER BY id")]
    active_ids = {
        int(row["id"])
        for row in memories
        if _sensitivity(row.get("sensitivity_class")) in ACTIVE_SENSITIVITY_CLASSES
    }
    quarantine_ids = {int(row["id"]) for row in memories} - active_ids
    counts: dict[str, Any] = {
        "memories_total": len(memories),
        "memories_active_import": len(active_ids),
        "memories_quarantine_or_omit": len(quarantine_ids),
    }
    if "memory_events" in tables:
        counts["memory_events_active_import"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_events WHERE memory_id IN ({})".format(
                    ",".join("?" for _ in active_ids) or "NULL"
                ),
                tuple(sorted(active_ids)),
            ).fetchone()[0]
        )
    if "memory_links" in tables:
        counts["memory_links_active_import"] = sum(
            1
            for row in conn.execute("SELECT from_memory_id,to_memory_id FROM memory_links")
            if int(row[0]) in active_ids and int(row[1]) in active_ids
        )
    if "conversation_archives" in tables:
        conversations = [dict(row) for row in conn.execute("SELECT * FROM conversation_archives")]
        counts["conversation_archives_total"] = len(conversations)
        counts["conversation_archives_active_import"] = sum(
            1
            for row in conversations
            if _sensitivity(row.get("sensitivity_class")) in ACTIVE_SENSITIVITY_CLASSES
        )
    if "timeline_events" in tables:
        counts["timeline_events_import"] = int(conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0])
    counts["archive_table_rows"] = sum(
        int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
        if _archive_table(table)
    )
    counts["security_or_derived_rows_omitted"] = sum(
        int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
        if _skip_table(table)
    )
    return counts


def _skip_table(table: str) -> bool:
    return (
        table in SECURITY_OR_DERIVED_TABLES
        or table.startswith("remote_auth_")
        or table.startswith("memories_fts")
        or table.startswith("conversations_fts")
    )


def _archive_table(table: str) -> bool:
    return table not in ACTIVE_SOURCE_TABLES and not _skip_table(table)


def _warnings(conn: sqlite3.Connection, counts: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if int(counts.get("memories_quarantine_or_omit", 0)):
        warnings.append("sensitive_memories_not_activated")
    if int(counts.get("security_or_derived_rows_omitted", 0)):
        warnings.append("security_derived_or_ephemeral_rows_not_imported")
    if any(table.startswith("remote_auth_") for table in _table_names(conn)):
        warnings.append("remote_auth_credentials_must_be_reissued")
    if _table_exists(conn, "memory_embeddings"):
        warnings.append("legacy_embeddings_not_imported_reindex_required")
    warnings.append("legacy_operational_rows_archived_not_reactivated")
    warnings.append("target_visibility_defaults_to_private")
    return sorted(set(warnings))


def _existing_import(conn: sqlite3.Connection, source_fingerprint: str) -> sqlite3.Row | None:
    if not _table_exists(conn, "legacy_aurora_import_runs"):
        return None
    return conn.execute(
        "SELECT * FROM legacy_aurora_import_runs WHERE source_fingerprint=? AND status='completed'",
        (source_fingerprint,),
    ).fetchone()


def _preview_from_snapshots(
    *,
    source_snapshot: Path,
    source_fingerprint: str,
    target_snapshot: Path,
    target_fingerprint: str,
) -> dict[str, Any]:
    source = _connect(source_snapshot, read_only=True)
    target = _connect(target_snapshot, read_only=True)
    try:
        source_errors = _recognize_aurora(source)
        target_errors = _recognize_target(target)
        source_tail = _schema_tail(source)
        target_tail = _schema_tail(target)
        target_state = _fresh_target_state(target) if not target_errors else None
        existing = _existing_import(target, source_fingerprint) if not target_errors else None
        counts = _source_counts(source) if not source_errors else {}
        warnings = _warnings(source, counts) if not source_errors else []
        base = {
            "schema": IMPORT_SCHEMA,
            "source_fingerprint": source_fingerprint,
            "target_fingerprint": target_fingerprint,
            "source_schema_tail": source_tail,
            "target_schema_tail": target_tail,
            "counts": counts,
            "warnings": warnings,
        }
        preview_hash = _sha256_text(_canonical_json(base))
        if source_errors or target_errors:
            return {
                "status": "blocked",
                **base,
                "preview_hash": preview_hash,
                "errors": [*source_errors, *target_errors],
                "mutations_performed": 0,
            }
        if existing is not None:
            return {
                "status": "already_imported",
                **base,
                "preview_hash": preview_hash,
                "import_run_id": int(existing["id"]),
                "mutations_performed": 0,
            }
        assert target_state is not None
        if not target_state["fresh"]:
            return {
                "status": "blocked",
                **base,
                "preview_hash": preview_hash,
                "errors": ["target_not_fresh"],
                "target_state": target_state,
                "mutations_performed": 0,
            }
        return {
            "status": "preview_ready",
            **base,
            "preview_hash": preview_hash,
            "target_state": target_state,
            "safety": {
                "source_database_mutated": False,
                "target_database_mutated": False,
                "remote_auth_imported": False,
                "legacy_embeddings_imported": False,
                "restricted_memories_activated": False,
            },
        }
    finally:
        source.close()
        target.close()


def preview_aurora_import(*, source_db: str | Path, target_db: str | Path) -> dict[str, Any]:
    source_path = Path(source_db).expanduser().resolve()
    target_path = Path(target_db).expanduser().resolve()
    if not source_path.is_file():
        return {"status": "blocked", "schema": IMPORT_SCHEMA, "errors": ["source_database_missing"], "mutations_performed": 0}
    if not target_path.is_file():
        return {"status": "blocked", "schema": IMPORT_SCHEMA, "errors": ["target_database_missing"], "mutations_performed": 0}
    with tempfile.TemporaryDirectory(prefix="mapi-aurora-import-preview-") as td:
        root = Path(td)
        source_snapshot = _verified_snapshot(source_path, root / "source.db")
        target_snapshot = _verified_snapshot(target_path, root / "target.db")
        return _preview_from_snapshots(
            source_snapshot=source_snapshot["path"],
            source_fingerprint=source_snapshot["sha256"],
            target_snapshot=target_snapshot["path"],
            target_fingerprint=target_snapshot["sha256"],
        )


def _source_id(row: dict[str, Any]) -> str | None:
    value = row.get("id")
    return None if value is None else str(value)


def _insert_item(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    item_type: str,
    source_table: str,
    source_id: str | None,
    target_id: int | None,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO legacy_aurora_import_items (
            import_run_id,item_type,source_table,source_id,target_id,status,metadata_json
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (run_id, item_type, source_table, source_id, target_id, status, _canonical_json(_json_safe(metadata or {}))),
    )


def _redacted_secret_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    for key in ("content", "summary_short", "title", "source_context"):
        value = payload.pop(key, None)
        if value is not None:
            payload[f"{key}_sha256"] = _sha256_text(str(value))
    return payload


def _archive_row(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    source_table: str,
    row: dict[str, Any],
    sensitivity_class: str | None = None,
    redact_content: bool = False,
) -> None:
    payload = _redacted_secret_payload(row) if redact_content else dict(row)
    payload = _json_safe(payload)
    payload_json = _canonical_json(payload)
    conn.execute(
        """
        INSERT INTO legacy_aurora_import_archive (
            import_run_id,source_table,source_id,sensitivity_class,payload_json,payload_sha256,redacted,created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            source_table,
            _source_id(row),
            sensitivity_class,
            payload_json,
            _sha256_text(payload_json),
            1 if redact_content else 0,
            _utc_now(),
        ),
    )


def _insert_memory(target: sqlite3.Connection, row: dict[str, Any]) -> int:
    archived_at = row.get("archived_at")
    values = {
        "content": row.get("content"),
        "summary_short": row.get("summary_short"),
        "memory_type": row.get("memory_type") or "project_note",
        "source": row.get("source"),
        "importance_score": row.get("importance_score"),
        "confidence_score": row.get("confidence_score"),
        "tags": row.get("tags"),
        "created_at": row.get("created_at"),
        "last_accessed_at": row.get("updated_at") or row.get("created_at"),
        "activity_state": "archived" if archived_at else "active",
        "contradiction_flag": 1 if str(row.get("state_code") or "").casefold() == "conflicted" else 0,
        "archived_at": archived_at,
        "layer_code": row.get("layer_code"),
        "state_code": row.get("state_code"),
        "scope_code": row.get("scope_code"),
        "version": int(row.get("version") or 1),
        "valid_from": row.get("valid_from"),
        "valid_to": row.get("valid_to"),
        "project_key": row.get("project_key"),
        "conversation_key": row.get("conversation_key"),
        "last_validated_at": row.get("last_validated_at"),
        "validation_source": row.get("validation_source"),
        "review_due_at": row.get("review_due_at"),
        "revalidation_due_at": row.get("revalidation_due_at"),
        "expired_due_at": row.get("expired_due_at"),
        "visibility_scope": "private",
        "sharing_policy": "explicit",
        "priority": row.get("priority") or "normal",
        "schema_version": 1,
        "entry_type": row.get("entry_type"),
        "truth_kind": row.get("truth_kind"),
        "title": row.get("title"),
        "source_context": row.get("source_context"),
        "source_event_ref": row.get("source_event_ref"),
        "updated_at": row.get("updated_at"),
        "memory_v2_status": row.get("memory_v2_status"),
        "importance_level": row.get("importance_level"),
        "requires_user_confirmation": 0,
    }
    columns = list(values)
    cursor = target.execute(
        f"INSERT INTO memories ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    return int(cursor.lastrowid)


def _archive_bootstrap_identity_if_needed(
    target: sqlite3.Connection, *, run_id: int, onboarding_answers: dict[str, Any]
) -> list[int]:
    if not str(onboarding_answers.get("agent_name") or "").strip():
        return []
    rows = target.execute(
        """
        SELECT id FROM memories
        WHERE source='mapi-init' AND memory_type='identity' AND archived_at IS NULL
        ORDER BY id
        """
    ).fetchall()
    now = _utc_now()
    archived: list[int] = []
    for row in rows:
        memory_id = int(row[0])
        target.execute(
            """
            UPDATE memories SET archived_at=?,activity_state='archived',state_code='archived',
                memory_v2_status='archived',updated_at=? WHERE id=?
            """,
            (now, now, memory_id),
        )
        _insert_item(
            target,
            run_id=run_id,
            item_type="target_bootstrap_identity",
            source_table="memories",
            source_id=None,
            target_id=memory_id,
            status="archived_before_import",
            metadata={"reason": "legacy_onboarding_contains_agent_name"},
        )
        archived.append(memory_id)
    return archived


def _copy_onboarding(source: sqlite3.Connection, target: sqlite3.Connection, *, run_id: int) -> dict[str, Any]:
    if not _table_exists(source, "aurora_onboarding"):
        return {"status": "missing", "answers": {}}
    row = source.execute("SELECT * FROM aurora_onboarding WHERE id=1").fetchone()
    if row is None:
        return {"status": "missing", "answers": {}}
    data = dict(row)
    try:
        answers = json.loads(str(data.get("answers_json") or "{}"))
    except json.JSONDecodeError:
        answers = {}
    target.execute(
        """
        UPDATE polaris_onboarding SET
            schema_version=2,status=?,current_step=?,answers_json=?,created_at=?,updated_at=?,
            completed_at=?,skipped_at=?,skip_reason=? WHERE id=1
        """,
        (
            data.get("status") or "not_started",
            data.get("current_step"),
            data.get("answers_json") or "{}",
            data.get("created_at") or _utc_now(),
            data.get("updated_at") or _utc_now(),
            data.get("completed_at"),
            data.get("skipped_at"),
            data.get("skip_reason"),
        ),
    )
    _insert_item(
        target,
        run_id=run_id,
        item_type="onboarding",
        source_table="aurora_onboarding",
        source_id="1",
        target_id=1,
        status="translated",
        metadata={"source_schema_version": data.get("schema_version")},
    )
    return {"status": "translated", "answers": answers}


def _copy_conversations(source: sqlite3.Connection, target: sqlite3.Connection, *, run_id: int) -> dict[str, int]:
    if not _table_exists(source, "conversation_archives"):
        return {"imported": 0, "quarantined": 0, "omitted_secret": 0}
    imported = quarantined = omitted = 0
    for raw in source.execute("SELECT * FROM conversation_archives ORDER BY id"):
        row = dict(raw)
        sensitivity = _sensitivity(row.get("sensitivity_class"))
        if sensitivity not in ACTIVE_SENSITIVITY_CLASSES:
            redact = sensitivity in SECRET_OMIT_CLASSES
            _archive_row(target, run_id=run_id, source_table="conversation_archives", row=row, sensitivity_class=sensitivity, redact_content=redact)
            if redact:
                omitted += 1
            else:
                quarantined += 1
            continue
        values = {
            "conversation_id": row.get("conversation_id"),
            "title": row.get("title"),
            "source": row.get("source") or "manual",
            "content": row.get("content"),
            "project_key": row.get("project_key"),
            "tags": row.get("tags"),
            "word_count": int(row.get("word_count") or 0),
            "created_at": row.get("created_at") or _utc_now(),
            "archived_at": row.get("archived_at") or row.get("created_at") or _utc_now(),
        }
        columns = list(values)
        cursor = target.execute(
            f"INSERT INTO conversation_archives ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )
        _insert_item(target, run_id=run_id, item_type="conversation", source_table="conversation_archives", source_id=_source_id(row), target_id=int(cursor.lastrowid), status="imported", metadata={"legacy_sensitivity_class": sensitivity})
        imported += 1
    return {"imported": imported, "quarantined": quarantined, "omitted_secret": omitted}


def _copy_timeline(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    run_id: int,
    memory_map: dict[int, int],
) -> int:
    if not _table_exists(source, "timeline_events"):
        return 0
    target_columns = _columns(target, "timeline_events")
    imported = 0
    for raw in source.execute("SELECT * FROM timeline_events ORDER BY id"):
        row = dict(raw)
        values = {key: value for key, value in row.items() if key in target_columns and key not in {"id", "memory_id", "related_memory_id", "run_id"}}
        old_memory = row.get("memory_id")
        old_related = row.get("related_memory_id")
        values["memory_id"] = memory_map.get(int(old_memory)) if old_memory is not None else None
        values["related_memory_id"] = memory_map.get(int(old_related)) if old_related is not None else None
        columns = list(values)
        cursor = target.execute(
            f"INSERT INTO timeline_events ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )
        metadata = {}
        if old_memory is not None and int(old_memory) not in memory_map:
            metadata["legacy_memory_id_unmapped"] = int(old_memory)
        if old_related is not None and int(old_related) not in memory_map:
            metadata["legacy_related_memory_id_unmapped"] = int(old_related)
        _insert_item(target, run_id=run_id, item_type="timeline_event", source_table="timeline_events", source_id=_source_id(row), target_id=int(cursor.lastrowid), status="imported", metadata=metadata)
        imported += 1
    return imported


def _archive_other_tables(source: sqlite3.Connection, target: sqlite3.Connection, *, run_id: int) -> dict[str, int]:
    archived = skipped = 0
    for table in sorted(_table_names(source)):
        if _archive_table(table):
            for raw in source.execute(f"SELECT * FROM {table}"):
                row = dict(raw)
                _archive_row(target, run_id=run_id, source_table=table, row=row)
                archived += 1
        elif _skip_table(table):
            skipped += int(source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return {"archived": archived, "skipped_security_or_derived": skipped}


def _perform_import(
    *,
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    run_id: int,
) -> dict[str, Any]:
    onboarding = _copy_onboarding(source, target, run_id=run_id)
    archived_seed_ids = _archive_bootstrap_identity_if_needed(
        target, run_id=run_id, onboarding_answers=dict(onboarding.get("answers") or {})
    )

    memory_map: dict[int, int] = {}
    old_rows: dict[int, dict[str, Any]] = {}
    quarantined = omitted_secret = 0
    for raw in source.execute("SELECT * FROM memories ORDER BY id"):
        row = dict(raw)
        old_id = int(row["id"])
        old_rows[old_id] = row
        sensitivity = _sensitivity(row.get("sensitivity_class"))
        metadata = {
            "legacy_sensitivity_class": sensitivity,
            "legacy_input_fingerprint": row.get("input_fingerprint"),
            "legacy_agent_key": row.get("agent_key"),
            "legacy_supersedes_memory_id": row.get("supersedes_memory_id"),
            "legacy_superseded_by_memory_id": row.get("superseded_by_memory_id"),
        }
        if sensitivity not in ACTIVE_SENSITIVITY_CLASSES:
            redact = sensitivity in SECRET_OMIT_CLASSES
            _archive_row(target, run_id=run_id, source_table="memories", row=row, sensitivity_class=sensitivity, redact_content=redact)
            _insert_item(target, run_id=run_id, item_type="memory", source_table="memories", source_id=str(old_id), target_id=None, status="omitted_secret" if redact else "quarantined", metadata=metadata)
            if redact:
                omitted_secret += 1
            else:
                quarantined += 1
            continue
        new_id = _insert_memory(target, row)
        memory_map[old_id] = new_id
        _insert_item(target, run_id=run_id, item_type="memory", source_table="memories", source_id=str(old_id), target_id=new_id, status="imported", metadata=metadata)

    # Translate supersession pointers only when both endpoints were activated.
    supersession_updates = 0
    for old_id, new_id in memory_map.items():
        row = old_rows[old_id]
        old_supersedes = row.get("supersedes_memory_id")
        old_superseded_by = row.get("superseded_by_memory_id")
        mapped_supersedes = memory_map.get(int(old_supersedes)) if old_supersedes is not None else None
        mapped_superseded_by = memory_map.get(int(old_superseded_by)) if old_superseded_by is not None else None
        if mapped_supersedes is not None or mapped_superseded_by is not None:
            target.execute(
                "UPDATE memories SET supersedes_memory_id=?,superseded_by_memory_id=? WHERE id=?",
                (mapped_supersedes, mapped_superseded_by, new_id),
            )
            supersession_updates += 1

    events_imported = events_archived = 0
    if _table_exists(source, "memory_events"):
        for raw in source.execute("SELECT * FROM memory_events ORDER BY id"):
            row = dict(raw)
            old_memory_id = int(row["memory_id"])
            new_memory_id = memory_map.get(old_memory_id)
            if new_memory_id is None:
                _archive_row(target, run_id=run_id, source_table="memory_events", row=row)
                events_archived += 1
                continue
            cursor = target.execute(
                "INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
                (new_memory_id, row.get("event_type"), row.get("payload_json"), row.get("created_at") or _utc_now()),
            )
            _insert_item(target, run_id=run_id, item_type="memory_event", source_table="memory_events", source_id=_source_id(row), target_id=int(cursor.lastrowid), status="imported", metadata={"legacy_memory_id": old_memory_id})
            events_imported += 1

    links_imported = links_archived = 0
    if _table_exists(source, "memory_links"):
        for raw in source.execute("SELECT * FROM memory_links ORDER BY id"):
            row = dict(raw)
            old_from = int(row["from_memory_id"])
            old_to = int(row["to_memory_id"])
            new_from = memory_map.get(old_from)
            new_to = memory_map.get(old_to)
            extra = {
                key: row.get(key)
                for key in ("evidence_kind", "evidence_ref", "reason", "applied_by", "preview_hash")
                if row.get(key) is not None
            }
            if new_from is None or new_to is None:
                _archive_row(target, run_id=run_id, source_table="memory_links", row=row)
                links_archived += 1
                continue
            cursor = target.execute(
                """
                INSERT INTO memory_links(from_memory_id,to_memory_id,relation_type,weight,origin,created_at,archived_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (new_from, new_to, row.get("relation_type"), float(row.get("weight") or 1.0), row.get("origin"), row.get("created_at"), row.get("archived_at")),
            )
            _insert_item(target, run_id=run_id, item_type="memory_link", source_table="memory_links", source_id=_source_id(row), target_id=int(cursor.lastrowid), status="imported", metadata={"legacy_from_memory_id": old_from, "legacy_to_memory_id": old_to, **extra})
            links_imported += 1

    conversations = _copy_conversations(source, target, run_id=run_id)
    timeline_imported = _copy_timeline(source, target, run_id=run_id, memory_map=memory_map)
    archive = _archive_other_tables(source, target, run_id=run_id)
    return {
        "onboarding": onboarding["status"],
        "bootstrap_identity_archived": len(archived_seed_ids),
        "memories_imported": len(memory_map),
        "memories_quarantined": quarantined,
        "memories_secret_omitted": omitted_secret,
        "supersession_updates": supersession_updates,
        "memory_events_imported": events_imported,
        "memory_events_archived": events_archived,
        "memory_links_imported": links_imported,
        "memory_links_archived": links_archived,
        "conversations": conversations,
        "timeline_events_imported": timeline_imported,
        "legacy_rows_archived": archive["archived"],
        "security_or_derived_rows_skipped": archive["skipped_security_or_derived"],
        "memory_id_map": {str(key): value for key, value in sorted(memory_map.items())},
    }


def _backup_destination(target_snapshot: Path, target_db: Path) -> Path:
    if target_db.parent.name.casefold() == "data":
        backup_dir = target_db.parent.parent / "backups"
    else:
        backup_dir = target_db.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"mapi-before-aurora-import-{stamp}.db"
    shutil.copy2(target_snapshot, destination)
    verify = _connect(destination, read_only=True)
    try:
        if str(verify.execute("PRAGMA quick_check").fetchone()[0]).casefold() != "ok":
            raise RuntimeError("target_backup_quick_check_failed")
        if verify.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("target_backup_foreign_key_check_failed")
    finally:
        verify.close()
    return destination


def apply_aurora_import(
    *,
    source_db: str | Path,
    target_db: str | Path,
    expected_preview_hash: str | None,
) -> dict[str, Any]:
    if not expected_preview_hash:
        return {"status": "blocked", "schema": IMPORT_SCHEMA, "errors": ["expected_preview_hash_required"], "mutations_performed": 0}
    source_path = Path(source_db).expanduser().resolve()
    target_path = Path(target_db).expanduser().resolve()
    if not source_path.is_file() or not target_path.is_file():
        return preview_aurora_import(source_db=source_path, target_db=target_path)

    with tempfile.TemporaryDirectory(prefix="mapi-aurora-import-apply-") as td:
        root = Path(td)
        source_snapshot = _verified_snapshot(source_path, root / "source.db")
        target_snapshot = _verified_snapshot(target_path, root / "target.db")
        preview = _preview_from_snapshots(
            source_snapshot=source_snapshot["path"],
            source_fingerprint=source_snapshot["sha256"],
            target_snapshot=target_snapshot["path"],
            target_fingerprint=target_snapshot["sha256"],
        )
        if preview["status"] == "already_imported":
            return preview
        if preview["status"] != "preview_ready":
            return preview
        if str(expected_preview_hash) != str(preview["preview_hash"]):
            return {
                "status": "stale_preview",
                "schema": IMPORT_SCHEMA,
                "expected_preview_hash": expected_preview_hash,
                "current_preview_hash": preview["preview_hash"],
                "mutations_performed": 0,
            }

        backup = _backup_destination(target_snapshot["path"], target_path)
        source = _connect(source_snapshot["path"], read_only=True)
        target = _connect(target_path)
        try:
            target.execute("BEGIN IMMEDIATE")
            ensure_aurora_import_schema(target)
            if not _fresh_target_state(target)["fresh"]:
                target.rollback()
                return {"status": "blocked", "schema": IMPORT_SCHEMA, "errors": ["target_not_fresh_after_lock"], "mutations_performed": 0}
            existing = _existing_import(target, source_snapshot["sha256"])
            if existing is not None:
                target.rollback()
                return {"status": "already_imported", "schema": IMPORT_SCHEMA, "import_run_id": int(existing["id"]), "mutations_performed": 0}
            now = _utc_now()
            cursor = target.execute(
                """
                INSERT INTO legacy_aurora_import_runs (
                    source_fingerprint,source_path_sha256,source_schema_tail,target_schema_tail,
                    preview_hash,status,backup_path,counts_json,warnings_json,created_at
                ) VALUES (?,?,?,?,?,'running',?,?,?,?)
                """,
                (
                    source_snapshot["sha256"],
                    _sha256_text(str(source_path)),
                    preview.get("source_schema_tail"),
                    preview.get("target_schema_tail"),
                    preview["preview_hash"],
                    str(backup),
                    _canonical_json(preview.get("counts") or {}),
                    _canonical_json(preview.get("warnings") or []),
                    now,
                ),
            )
            run_id = int(cursor.lastrowid)
            result = _perform_import(source=source, target=target, run_id=run_id)
            foreign_keys = target.execute("PRAGMA foreign_key_check").fetchall()
            quick = str(target.execute("PRAGMA quick_check").fetchone()[0])
            if foreign_keys or quick.casefold() != "ok":
                raise RuntimeError("post_import_integrity_failed")
            completed_at = _utc_now()
            target.execute(
                "UPDATE legacy_aurora_import_runs SET status='completed',completed_at=?,counts_json=? WHERE id=?",
                (completed_at, _canonical_json(result), run_id),
            )
            target.commit()
        except Exception:
            target.rollback()
            raise
        finally:
            source.close()
            target.close()

        return {
            "status": "completed",
            "schema": IMPORT_SCHEMA,
            "import_run_id": run_id,
            "source_fingerprint": source_snapshot["sha256"],
            "preview_hash": preview["preview_hash"],
            "backup_path": str(backup),
            "result": result,
            "warnings": preview.get("warnings") or [],
            "safety": {
                "source_database_mutated": False,
                "target_backup_verified": True,
                "remote_auth_imported": False,
                "legacy_embeddings_imported": False,
                "secret_like_memory_content_copied": False,
                "restricted_memories_activated": False,
            },
            "next_actions": [
                "Reconfigure project Files/Git/Command bindings for the unified runtime.",
                "Reissue remote OAuth/service credentials instead of reusing legacy tokens.",
                "Review legacy_aurora_import_archive before deciding whether quarantined sensitive material should become active memory.",
                "Rebuild semantic embeddings through the unified semantic workflow if needed.",
            ],
        }
