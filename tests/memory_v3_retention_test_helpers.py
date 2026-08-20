from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from app import db_migrations
from mapi_core.memory.retention import preview_memory_retention_policy_payload
from mapi_core.memory.retention_apply import apply_memory_retention_review_payload
from mapi_core.memory.retention_review import decide_retention_review_item, save_retention_review_item
from mapi_core.memory.sla import compute_sla_days


NOW = "2026-07-16T08:00:00+02:00"


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def shift_iso_days(value: str, days: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(value) + timedelta(days=days)).isoformat()


def make_conn(path: Path | str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db_migrations.apply_all_migrations(conn)
    conn.execute("UPDATE feature_flags SET is_enabled=1,rollout_mode='all' WHERE flag_key IN ('memory_v2_enabled','memory_v3_retention_enabled')")
    conn.commit()
    return conn


def insert_memory(conn, *, content="opaque", memory_type="temporary", state_code="validated", project_key="p", scope_code="project", workspace_id=None, **values) -> int:
    base = {"content": content, "memory_type": memory_type, "state_code": state_code, "memory_v2_status": "active" if state_code == "validated" else state_code, "activity_state": "active", "project_key": project_key, "scope_code": scope_code, "workspace_id": workspace_id}
    base.update(values)
    columns = list(base)
    return int(conn.execute(f"INSERT INTO memories({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", list(base.values())).lastrowid)


def preview(conn, **kwargs):
    return preview_memory_retention_policy_payload(conn, row_to_dict=dict, canonical_json_hash=canonical_hash, utc_now_iso=lambda: NOW, **kwargs)


def insert_event(conn, *, memory_id, event_type, payload):
    cursor = conn.execute("INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES(?,?,?,?)", (memory_id, event_type, json.dumps(payload, sort_keys=True), NOW))
    return {"id": int(cursor.lastrowid), "event_type": event_type}


def approved_item(conn, memory_id: int, *, as_of=NOW) -> dict:
    fresh = preview(conn, memory_id=memory_id, as_of=as_of, include_debug=False)
    saved = save_retention_review_item(conn, memory_id=memory_id, expected_preview_hash=fresh["preview_hash"], as_of=as_of, preview_func=preview, canonical_json_hash=canonical_hash, utc_now_iso=lambda: NOW, row_to_dict=dict)
    item = saved["item"]
    decided = decide_retention_review_item(conn, review_item_id=item["id"], decision="approve", reviewed_by="operator", review_note=None, utc_now_iso=lambda: NOW, row_to_dict=dict)
    conn.commit()
    return decided["item"]


def apply_item(conn, item: dict, *, insert_event_func=insert_event):
    return apply_memory_retention_review_payload(
        conn, review_item_id=item["id"], expected_preview_hash=item["preview_hash"], applied_by="operator", notes=None, include_debug=False,
        row_to_dict=dict, preview_func=preview, memory_v2_enabled=lambda _conn: True,
        retention_flag_evaluation=lambda _conn, **_kwargs: {"enabled": True, "reason": "test"},
        insert_memory_event=insert_event_func, canonical_json_hash=canonical_hash, utc_now_iso=lambda: NOW,
        compute_sla_days=compute_sla_days, shift_iso_days=shift_iso_days,
    )
