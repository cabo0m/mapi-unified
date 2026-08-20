from __future__ import annotations

"""Evidence-bound repair for legacy memory provenance gaps.

The repair never invents a historical conversation. It can:
1. attach a unique internal audit-event reference to a memory that has no
   source_event_ref but does have at least one durable memory_event;
2. recover conversation_key only when existing event payloads contain exactly
   one explicit conversation_key value.

Anything else remains an explicit unresolved legacy gap.
"""

import hashlib
import json
import re
from typing import Any, Callable

PREVIEW_SCHEMA = "memory_provenance_backfill_preview.v1"
APPLY_SCHEMA = "memory_provenance_backfill_apply.v1"
POLICY_VERSION = "legacy_provenance_backfill.v1"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _canonical_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


_SANDMAN_MARA_SOURCE = re.compile(r"^sandman_mara:(?P<run_id>[A-Za-z0-9._:-]+)$")


def _structured_source_event_ref(source: Any) -> str | None:
    """Return a source locator only for explicitly structured, opaque origins."""
    value = _text(source)
    if not value:
        return None
    match = _SANDMAN_MARA_SOURCE.fullmatch(value)
    if match:
        return value
    return None


def _event_payload(row: Any) -> dict[str, Any]:
    raw = row["payload_json"]
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_provenance_backfill_preview(
    conn: Any,
    *,
    project_key: str | None = None,
    sample_limit: int = 50,
) -> dict[str, Any]:
    normalized_project = _text(project_key)
    clauses = [
        "((source_event_ref IS NULL OR trim(source_event_ref)='') OR (conversation_key IS NULL OR trim(conversation_key)=''))"
    ]
    params: list[Any] = []
    if normalized_project:
        clauses.append("project_key=?")
        params.append(normalized_project)
    rows = conn.execute(
        f"""
        SELECT id, project_key, source, source_context, source_event_ref, conversation_key, created_at
        FROM memories
        WHERE {' AND '.join(clauses)}
        ORDER BY id
        """,
        params,
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    unresolved_source_event_ids: list[int] = []
    unresolved_conversation_ids: list[int] = []

    for memory in rows:
        memory_id = int(memory["id"])
        existing_source_ref = _text(memory["source_event_ref"])
        existing_conversation = _text(memory["conversation_key"])
        events = conn.execute(
            "SELECT id, event_type, payload_json, created_at FROM memory_events WHERE memory_id=? ORDER BY id",
            (memory_id,),
        ).fetchall()

        proposed_source_ref: str | None = None
        evidence_event_id: int | None = None
        source_ref_evidence_kind: str | None = None
        if not existing_source_ref:
            if events:
                evidence_event_id = int(events[0]["id"])
                proposed_source_ref = f"legacy-evidence-event:{evidence_event_id}"
                source_ref_evidence_kind = "durable_memory_event"
            else:
                proposed_source_ref = _structured_source_event_ref(memory["source"])
                if proposed_source_ref:
                    source_ref_evidence_kind = "structured_source_locator"
                else:
                    unresolved_source_event_ids.append(memory_id)

        proposed_conversation: str | None = None
        if not existing_conversation:
            explicit_conversations = {
                value
                for event in events
                if (value := _text(_event_payload(event).get("conversation_key")))
            }
            if len(explicit_conversations) == 1:
                proposed_conversation = next(iter(explicit_conversations))
            else:
                unresolved_conversation_ids.append(memory_id)

        if proposed_source_ref or proposed_conversation:
            candidates.append(
                {
                    "memory_id": memory_id,
                    "project_key": _text(memory["project_key"]),
                    "existing_source_event_ref": existing_source_ref,
                    "proposed_source_event_ref": proposed_source_ref,
                    "source_ref_evidence_kind": source_ref_evidence_kind,
                    "source_ref_evidence_event_id": evidence_event_id,
                    "source_ref_evidence_source": _text(memory["source"]) if source_ref_evidence_kind == "structured_source_locator" else None,
                    "existing_conversation_key": existing_conversation,
                    "proposed_conversation_key": proposed_conversation,
                }
            )

    fingerprint_payload = {
        "policy_version": POLICY_VERSION,
        "project_key": normalized_project,
        "candidates": candidates,
    }
    preview_hash = _canonical_hash(fingerprint_payload)
    source_ref_candidates = sum(1 for item in candidates if item["proposed_source_event_ref"])
    conversation_candidates = sum(1 for item in candidates if item["proposed_conversation_key"])
    return {
        "status": "ok",
        "schema": PREVIEW_SCHEMA,
        "policy_version": POLICY_VERSION,
        "project_key": normalized_project,
        "candidate_count": len(candidates),
        "source_event_ref_candidate_count": source_ref_candidates,
        "conversation_key_candidate_count": conversation_candidates,
        "unresolved_source_event_ref_count": len(unresolved_source_event_ids),
        "unresolved_conversation_key_count": len(unresolved_conversation_ids),
        "unresolved_source_event_ref_sample": unresolved_source_event_ids[:sample_limit],
        "unresolved_conversation_key_sample": unresolved_conversation_ids[:sample_limit],
        "sample": candidates[:sample_limit],
        "preview_hash": preview_hash,
        "candidate_fingerprint": _canonical_hash(candidates),
        "_candidates": candidates,
    }


def apply_provenance_backfill(
    conn: Any,
    *,
    expected_preview_hash: str,
    project_key: str | None,
    applied_by: str,
    backup_ref: str,
    insert_memory_event: Callable[..., Any],
    utc_now_iso: Callable[[], str],
) -> dict[str, Any]:
    preview = build_provenance_backfill_preview(conn, project_key=project_key)
    if preview["preview_hash"] != str(expected_preview_hash or "").strip():
        return {
            "status": "blocked",
            "schema": APPLY_SCHEMA,
            "reason": "preview_hash_mismatch",
            "expected_preview_hash": expected_preview_hash,
            "current_preview_hash": preview["preview_hash"],
            "candidate_count": preview["candidate_count"],
        }

    candidates = list(preview["_candidates"])
    updated_ids: list[int] = []
    source_ref_updates = 0
    conversation_updates = 0
    now = utc_now_iso()

    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in candidates:
            memory_id = int(item["memory_id"])
            sets: list[str] = []
            params: list[Any] = []
            new_source_ref = _text(item.get("proposed_source_event_ref"))
            new_conversation = _text(item.get("proposed_conversation_key"))
            if new_source_ref:
                sets.append("source_event_ref=?")
                params.append(new_source_ref)
            if new_conversation:
                sets.append("conversation_key=?")
                params.append(new_conversation)
            if not sets:
                continue
            sets.append("updated_at=?")
            params.append(now)
            params.append(memory_id)
            cursor = conn.execute(
                f"UPDATE memories SET {', '.join(sets)} WHERE id=?",
                params,
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"provenance_backfill_row_drift:{memory_id}")
            insert_memory_event(
                conn,
                memory_id=memory_id,
                event_type="memory.provenance_backfilled",
                payload={
                    "policy_version": POLICY_VERSION,
                    "applied_by": _text(applied_by),
                    "backup_ref": _text(backup_ref),
                    "source_event_ref": new_source_ref,
                    "source_ref_evidence_kind": item.get("source_ref_evidence_kind"),
                    "source_ref_evidence_event_id": item.get("source_ref_evidence_event_id"),
                    "source_ref_evidence_source": item.get("source_ref_evidence_source"),
                    "conversation_key": new_conversation,
                    "reason": "evidence_bound_legacy_provenance_repair",
                },
            )
            updated_ids.append(memory_id)
            if new_source_ref:
                source_ref_updates += 1
            if new_conversation:
                conversation_updates += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "status": "applied",
        "schema": APPLY_SCHEMA,
        "policy_version": POLICY_VERSION,
        "project_key": _text(project_key),
        "updated_count": len(updated_ids),
        "source_event_ref_updates": source_ref_updates,
        "conversation_key_updates": conversation_updates,
        "updated_memory_ids": updated_ids,
        "backup_ref": _text(backup_ref),
        "preview_hash": preview["preview_hash"],
    }

