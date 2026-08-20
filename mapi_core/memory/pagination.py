from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from typing import Any, Mapping, Sequence


MEMORY_LIST_PAGE_SCHEMA = "mapi_memory_list_page.v1"
MEMORY_LIST_CURSOR_SCHEMA = "mapi_memory_list_cursor.v1"
MEMORY_LIST_ORDER = "created_at_desc_id_desc"

COMPACT_FIELDS = (
    "id",
    "title",
    "summary_short",
    "memory_type",
    "project_key",
    "state_code",
    "created_at",
)

PROJECTION_FIELDS = (
    "id",
    "title",
    "summary_short",
    "content",
    "memory_type",
    "entry_type",
    "truth_kind",
    "project_key",
    "scope_code",
    "state_code",
    "memory_v2_status",
    "importance_score",
    "confidence_score",
    "tags",
    "source",
    "source_context",
    "source_event_ref",
    "conversation_key",
    "created_at",
    "updated_at",
    "valid_from",
    "valid_to",
    "archived_at",
)

DEFAULT_FIELDS = tuple(field for field in PROJECTION_FIELDS if field != "content")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def normalize_projection(*, fields: Sequence[str] | None, compact: bool) -> tuple[str, ...]:
    if fields is None:
        return COMPACT_FIELDS if compact else DEFAULT_FIELDS
    normalized: list[str] = ["id"]
    seen = {"id"}
    for raw in fields:
        if not isinstance(raw, str):
            raise ValueError("projection_fields_must_be_strings")
        field = raw.strip()
        if not field or field in seen:
            continue
        if field not in PROJECTION_FIELDS:
            raise ValueError(f"unsupported_projection_field:{field}")
        seen.add(field)
        normalized.append(field)
    return tuple(normalized)


def parse_fields_json(fields_json: str | None) -> list[str] | None:
    if fields_json is None or not str(fields_json).strip():
        return None
    try:
        value = json.loads(str(fields_json))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid_fields_json:{exc.msg}") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("fields_json_must_be_string_array")
    return list(value)


def query_contract(*, filters: Mapping[str, Any], fields: Sequence[str], compact: bool) -> dict[str, Any]:
    return {
        "order": MEMORY_LIST_ORDER,
        "filters": dict(filters),
        "fields": list(fields),
        "compact": bool(compact),
    }


def encode_cursor(*, query_fingerprint: str, snapshot_max_id: int, created_at: str, memory_id: int) -> str:
    payload = {
        "schema": MEMORY_LIST_CURSOR_SCHEMA,
        "query_fingerprint": str(query_fingerprint),
        "snapshot_max_id": int(snapshot_max_id),
        "after": {
            "created_at": str(created_at),
            "id": int(memory_id),
        },
    }
    envelope = {
        "payload": payload,
        "checksum": _fingerprint(payload),
    }
    return _b64encode(_canonical_json(envelope).encode("utf-8"))


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        envelope = json.loads(_b64decode(str(cursor)).decode("utf-8"))
        payload = envelope["payload"]
        checksum = str(envelope["checksum"])
    except Exception as exc:
        raise ValueError("invalid_cursor") from exc
    if not isinstance(payload, dict) or payload.get("schema") != MEMORY_LIST_CURSOR_SCHEMA:
        raise ValueError("invalid_cursor_schema")
    if _fingerprint(payload) != checksum:
        raise ValueError("invalid_cursor_checksum")
    after = payload.get("after")
    if not isinstance(after, dict):
        raise ValueError("invalid_cursor_after")
    try:
        snapshot_max_id = int(payload["snapshot_max_id"])
        memory_id = int(after["id"])
        created_at = str(after["created_at"])
    except Exception as exc:
        raise ValueError("invalid_cursor_values") from exc
    if snapshot_max_id <= 0 or memory_id <= 0:
        raise ValueError("invalid_cursor_values")
    return {
        "schema": MEMORY_LIST_CURSOR_SCHEMA,
        "query_fingerprint": str(payload.get("query_fingerprint") or ""),
        "snapshot_max_id": snapshot_max_id,
        "after": {"created_at": created_at, "id": memory_id},
    }


def _row_to_dict(cursor: sqlite3.Cursor, row: Any) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    columns = [str(desc[0]) for desc in cursor.description or []]
    return dict(zip(columns, row))


def list_memory_page(
    conn: sqlite3.Connection,
    *,
    filters: Mapping[str, Any],
    fields: Sequence[str],
    compact: bool,
    page_size: int,
    cursor: str | None,
) -> dict[str, Any]:
    safe_page_size = max(1, min(int(page_size), 100))
    selected_fields = tuple(fields)
    contract = query_contract(filters=filters, fields=selected_fields, compact=compact)
    query_fingerprint = _fingerprint(contract)

    decoded: dict[str, Any] | None = None
    if cursor:
        try:
            decoded = decode_cursor(cursor)
        except ValueError as exc:
            return {
                "status": "error",
                "schema": MEMORY_LIST_PAGE_SCHEMA,
                "error": str(exc),
            }
        if decoded["query_fingerprint"] != query_fingerprint:
            return {
                "status": "error",
                "schema": MEMORY_LIST_PAGE_SCHEMA,
                "error": "cursor_query_mismatch",
            }
        snapshot_max_id = int(decoded["snapshot_max_id"])
    else:
        snapshot_max_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM memories").fetchone()[0])

    if snapshot_max_id <= 0:
        return {
            "status": "ok",
            "schema": MEMORY_LIST_PAGE_SCHEMA,
            "cursor_schema": MEMORY_LIST_CURSOR_SCHEMA,
            "order": MEMORY_LIST_ORDER,
            "query_fingerprint": query_fingerprint,
            "snapshot_max_id": 0,
            "page_size": safe_page_size,
            "returned_count": 0,
            "has_more": False,
            "next_cursor": None,
            "filters": dict(filters),
            "projection": {"fields": list(selected_fields), "compact": bool(compact)},
            "items": [],
        }

    where = ["id <= ?"]
    params: list[Any] = [snapshot_max_id]
    if not bool(filters.get("include_archived")):
        where.append("archived_at IS NULL")

    project_key_values = [
        str(value)
        for value in (filters.get("project_key_values") or [])
        if str(value or "").strip()
    ]
    if project_key_values:
        placeholders = ", ".join("?" for _ in project_key_values)
        where.append(f"project_key IN ({placeholders})")
        params.extend(project_key_values)

    for field in ("scope_code", "memory_type", "state_code", "truth_kind"):
        value = filters.get(field)
        if value is not None:
            where.append(f"{field} = ?")
            params.append(value)
    tag = filters.get("tag")
    if tag is not None:
        where.append("(',' || REPLACE(COALESCE(tags,''), ' ', '') || ',') LIKE ?")
        params.append(f"%,{str(tag).replace(' ', '')},%")
    if decoded is not None:
        after_created_at = str(decoded["after"]["created_at"])
        where.append("(COALESCE(created_at, '') < ? OR (COALESCE(created_at, '') = ? AND id < ?))")
        params.extend([
            after_created_at,
            after_created_at,
            int(decoded["after"]["id"]),
        ])

    internal_fields = list(dict.fromkeys([*selected_fields, "created_at", "id"]))
    sql = (
        f"SELECT {', '.join(internal_fields)} FROM memories "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(created_at, '') DESC, id DESC LIMIT ?"
    )
    params.append(safe_page_size + 1)
    db_cursor = conn.execute(sql, params)
    raw_rows = db_cursor.fetchall()
    rows = [_row_to_dict(db_cursor, row) for row in raw_rows]
    has_more = len(rows) > safe_page_size
    page_rows = rows[:safe_page_size]
    items = [
        {field: row.get(field) for field in selected_fields}
        for row in page_rows
    ]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            query_fingerprint=query_fingerprint,
            snapshot_max_id=snapshot_max_id,
            created_at=str(last.get("created_at") or ""),
            memory_id=int(last["id"]),
        )
    return {
        "status": "ok",
        "schema": MEMORY_LIST_PAGE_SCHEMA,
        "cursor_schema": MEMORY_LIST_CURSOR_SCHEMA,
        "order": MEMORY_LIST_ORDER,
        "query_fingerprint": query_fingerprint,
        "snapshot_max_id": snapshot_max_id,
        "page_size": safe_page_size,
        "returned_count": len(items),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "filters": dict(filters),
        "projection": {"fields": list(selected_fields), "compact": bool(compact)},
        "items": items,
        "safety": {
            "read_only": True,
            "snapshot_bounded": True,
            "offset_pagination_used": False,
        },
    }

