from __future__ import annotations

"""Research ingest quarantine payloads."""

import json
from typing import Any, Callable

INGEST_STATUSES = {"new", "parsed", "candidate", "promoted", "rejected", "archived", "merged"}
INGEST_SOURCE_TYPES = {"url", "pdf", "doc", "repo", "manual", "web", "rss", "note", "other"}


def normalize_ingest_status(value: str | None, *, default: str = "new", normalize_optional_text: Callable[[Any], str | None]) -> str:
    normalized = normalize_optional_text(value) or default
    normalized = normalized.lower().strip()
    if normalized not in INGEST_STATUSES:
        raise ValueError(f"ingest_status must be one of: {', '.join(sorted(INGEST_STATUSES))}")
    return normalized


def normalize_source_type(value: str | None, *, normalize_optional_text: Callable[[Any], str | None]) -> str:
    normalized = (normalize_optional_text(value) or "manual").lower().strip()
    if normalized not in INGEST_SOURCE_TYPES:
        normalized = "other"
    return normalized


def normalize_claims_json(
    extracted_claims_json: str | None,
    normalized_text: str,
    *,
    normalize_optional_text: Callable[[Any], str | None],
) -> str | None:
    raw = normalize_optional_text(extracted_claims_json)
    if raw is None:
        claims = [line.strip(" -\t") for line in normalized_text.splitlines() if line.strip()]
        claims = claims[:10]
        return json.dumps(claims, ensure_ascii=False) if claims else None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"extracted_claims_json is not valid JSON: {exc}") from exc
    return json.dumps(parsed, ensure_ascii=False)


def row_to_ingest_item(row: Any, *, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    item = row_to_dict(row)
    claims_raw = item.get("extracted_claims_json")
    if claims_raw:
        try:
            item["extracted_claims"] = json.loads(claims_raw)
        except Exception:
            item["extracted_claims"] = None
    else:
        item["extracted_claims"] = None
    return item


def require_ingest_item(
    conn: Any,
    ingest_item_id: int,
    *,
    row_to_ingest_item: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ingest_items WHERE id = ?", (int(ingest_item_id),)).fetchone()
    if row is None:
        raise ValueError(f"ingest item #{ingest_item_id} does not exist")
    return row_to_ingest_item(row)


def ensure_ingest_source(
    conn: Any,
    source_ref: str | None,
    source_type: str,
    title: str | None,
    reliability_score: float,
    notes: str | None = None,
    *,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_score: Callable[[float], float],
    utc_now_iso: Callable[[], str],
) -> int | None:
    normalized_ref = normalize_optional_text(source_ref)
    if normalized_ref is None:
        return None
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO ingest_sources (source_ref, source_type, title, reliability_score, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_ref) DO UPDATE SET
            source_type = excluded.source_type,
            title = COALESCE(excluded.title, ingest_sources.title),
            reliability_score = excluded.reliability_score,
            notes = COALESCE(excluded.notes, ingest_sources.notes),
            updated_at = excluded.updated_at
        """,
        (normalized_ref, source_type, normalize_optional_text(title), normalize_score(reliability_score), normalize_optional_text(notes), now, now),
    )
    row = conn.execute("SELECT id FROM ingest_sources WHERE source_ref = ?", (normalized_ref,)).fetchone()
    return int(row["id"]) if row else None


def create_ingest_item_payload(
    conn: Any,
    *,
    raw_text: str,
    source_type: str = "manual",
    source_ref: str | None = None,
    title: str | None = None,
    normalized_text: str | None = None,
    extracted_claims_json: str | None = None,
    project_key: str | None = None,
    tags: str | None = None,
    quality_score: float = 0.5,
    source_reliability_score: float = 0.5,
    ingest_status: str = "new",
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    normalize_score: Callable[[float], float],
    utc_now_iso: Callable[[], str],
    normalize_source_type: Callable[[str | None], str],
    normalize_ingest_status: Callable[[str | None], str],
    normalize_claims_json: Callable[[str | None, str], str | None],
    ensure_ingest_source: Callable[..., int | None],
    require_ingest_item: Callable[[Any, int], dict[str, Any]],
) -> dict[str, Any]:
    raw = normalize_required_text(raw_text, "raw_text")
    source_type_norm = normalize_source_type(source_type)
    status = normalize_ingest_status(ingest_status)
    normalized = normalize_optional_text(normalized_text) or raw.strip()
    claims_json = normalize_claims_json(extracted_claims_json, normalized)
    source_id = ensure_ingest_source(
        conn,
        source_ref=source_ref,
        source_type=source_type_norm,
        title=title,
        reliability_score=source_reliability_score,
    )
    cursor = conn.execute(
        """
        INSERT INTO ingest_items (
            source_id, source_ref, source_type, title, raw_text, normalized_text,
            extracted_claims_json, project_key, tags, ingest_status, quality_score,
            source_reliability_score, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            normalize_optional_text(source_ref),
            source_type_norm,
            normalize_optional_text(title),
            raw,
            normalized,
            claims_json,
            normalize_optional_text(project_key),
            normalize_optional_text(tags),
            status,
            normalize_score(quality_score),
            normalize_score(source_reliability_score),
            utc_now_iso(),
        ),
    )
    item_id = int(cursor.lastrowid)
    conn.commit()
    item = require_ingest_item(conn, item_id)
    return {"status": "created", "item": item, "quarantine": True, "normal_memory_created": False}


def list_ingest_queue_payload(
    conn: Any,
    *,
    ingest_status: str | None = None,
    project_key: str | None = None,
    source_type: str | None = None,
    tag: str | None = None,
    limit: int = 20,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_ingest_status: Callable[[str | None], str],
    normalize_source_type: Callable[[str | None], str],
    row_to_ingest_item: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 20), 100))
    filters: list[str] = []
    params: list[Any] = []
    if ingest_status is not None:
        filters.append("ingest_status = ?")
        params.append(normalize_ingest_status(ingest_status))
    if project_key is not None:
        filters.append("project_key = ?")
        params.append(normalize_optional_text(project_key))
    if source_type is not None:
        filters.append("source_type = ?")
        params.append(normalize_source_type(source_type))
    if tag is not None:
        filters.append("tags LIKE ?")
        params.append(f"%{normalize_optional_text(tag)}%")
    where = "WHERE " + " AND ".join(filters) if filters else ""
    rows = conn.execute(
        f"SELECT * FROM ingest_items {where} ORDER BY created_at DESC, id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return {"status": "ok", "count": len(rows), "items": [row_to_ingest_item(row) for row in rows]}


def get_ingest_item_payload(
    conn: Any,
    *,
    ingest_item_id: int,
    require_ingest_item: Callable[[Any, int], dict[str, Any]],
) -> dict[str, Any]:
    return {"status": "ok", "item": require_ingest_item(conn, int(ingest_item_id))}


def reject_ingest_item_payload(
    conn: Any,
    *,
    ingest_item_id: int,
    reason: str,
    reviewed_by: str | None = None,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    require_ingest_item: Callable[[Any, int], dict[str, Any]],
) -> dict[str, Any]:
    reason_norm = normalize_required_text(reason, "reason")
    item = require_ingest_item(conn, int(ingest_item_id))
    if item["ingest_status"] == "promoted":
        return {"status": "noop", "message": "promoted ingest items cannot be rejected", "item": item}
    conn.execute(
        "UPDATE ingest_items SET ingest_status = 'rejected', rejection_reason = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?",
        (reason_norm, utc_now_iso(), normalize_optional_text(reviewed_by), int(ingest_item_id)),
    )
    conn.commit()
    return {"status": "rejected", "item": require_ingest_item(conn, int(ingest_item_id))}


def archive_ingest_item_payload(
    conn: Any,
    *,
    ingest_item_id: int,
    reason: str | None = None,
    reviewed_by: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    require_ingest_item: Callable[[Any, int], dict[str, Any]],
) -> dict[str, Any]:
    require_ingest_item(conn, int(ingest_item_id))
    conn.execute(
        "UPDATE ingest_items SET ingest_status = 'archived', rejection_reason = COALESCE(?, rejection_reason), reviewed_at = ?, reviewed_by = ? WHERE id = ?",
        (normalize_optional_text(reason), utc_now_iso(), normalize_optional_text(reviewed_by), int(ingest_item_id)),
    )
    conn.commit()
    return {"status": "archived", "item": require_ingest_item(conn, int(ingest_item_id))}


def promote_ingest_item_payload(
    conn: Any,
    *,
    ingest_item_id: int,
    memory_content: str,
    memory_type: str = "research_note",
    summary_short: str | None = None,
    tags: str | None = None,
    importance_score: float = 0.5,
    confidence_score: float = 0.6,
    reviewed_by: str | None = None,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    require_ingest_item: Callable[[Any, int], dict[str, Any]],
    insert_memory: Callable[..., dict[str, Any]],
    ensure_memory_embedding_best_effort: Callable[[Any, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    content_norm = normalize_required_text(memory_content, "memory_content")
    item = require_ingest_item(conn, int(ingest_item_id))
    if item["ingest_status"] == "promoted" and item.get("promoted_memory_id"):
        return {"status": "already_promoted", "item": item, "promoted_memory_id": item.get("promoted_memory_id")}
    merged_tags = ",".join([part for part in [normalize_optional_text(tags), item.get("tags"), "research-ingest,evidence-backed"] if part])
    memory = insert_memory(
        conn,
        content=content_norm,
        memory_type=memory_type,
        summary_short=summary_short or item.get("title") or content_norm[:120],
        source=item.get("source_ref"),
        importance_score=importance_score,
        confidence_score=confidence_score,
        tags=merged_tags,
        layer_code="working",
        area_code="knowledge",
        scope_code="project" if item.get("project_key") else "global",
        project_key=item.get("project_key"),
        validation_source="research_ingest",
    )
    conn.execute(
        "UPDATE ingest_items SET ingest_status = 'promoted', promoted_memory_id = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?",
        (int(memory["id"]), utc_now_iso(), normalize_optional_text(reviewed_by), int(ingest_item_id)),
    )
    conn.commit()
    memory["embedding_hook"] = ensure_memory_embedding_best_effort(conn, memory)
    conn.commit()
    return {"status": "promoted", "item": require_ingest_item(conn, int(ingest_item_id)), "memory": memory}


def preview_research_ingest_review_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    limit: int = 20,
    normalize_optional_text: Callable[[Any], str | None],
    row_to_ingest_item: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 20), 100))
    filters = ["ingest_status IN ('new', 'parsed', 'candidate')"]
    params: list[Any] = []
    if project_key is not None:
        filters.append("project_key = ?")
        params.append(normalize_optional_text(project_key))
    rows = conn.execute(
        f"SELECT * FROM ingest_items WHERE {' AND '.join(filters)} ORDER BY created_at ASC, id ASC LIMIT ?",
        (*params, limit),
    ).fetchall()
    decisions = []
    for row in rows:
        item = row_to_ingest_item(row)
        text_len = len(item.get("normalized_text") or item.get("raw_text") or "")
        quality = float(item.get("quality_score") or 0.0)
        reliability = float(item.get("source_reliability_score") or 0.0)
        combined = round((quality + reliability) / 2, 3)
        if item.get("duplicate_of_ingest_id"):
            action = "merge"
            reason = "duplicate_of_ingest_id is set"
        elif combined >= 0.72 and text_len >= 80:
            action = "promote_candidate"
            reason = "high combined quality/reliability and enough content"
        elif combined < 0.35 or text_len < 20:
            action = "reject_candidate"
            reason = "low score or too little content"
        else:
            action = "keep_in_quarantine"
            reason = "needs more evidence or manual review"
        decisions.append({"ingest_item_id": item["id"], "action": action, "reason": reason, "combined_score": combined, "title": item.get("title")})
    return {"status": "ok", "count": len(decisions), "decisions": decisions}
