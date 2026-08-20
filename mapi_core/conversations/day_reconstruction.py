from __future__ import annotations

"""Evidence-first reconstruction of one local calendar day."""

from datetime import date as date_type, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _parse_day(value: str) -> date_type:
    try:
        return date_type.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("date must use YYYY-MM-DD") from exc


def _parse_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(value).strip())
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f"unknown timezone: {value}") from exc


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_text(value: str | None, tz: ZoneInfo) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(tz).isoformat(timespec="seconds")
    except ValueError:
        return None


def _fetch_limited(conn: Any, query: str, params: tuple[Any, ...], limit: int) -> tuple[list[dict[str, Any]], bool]:
    rows = conn.execute(query, (*params, limit + 1)).fetchall()
    truncated = len(rows) > limit
    return [dict(row) for row in rows[:limit]], truncated


def reconstruct_day_payload(
    conn: Any,
    *,
    date: str,
    timezone_name: str = "Europe/Warsaw",
    project_key: str | None = None,
    limit: int = 200,
    include_content: bool = True,
) -> dict[str, Any]:
    """Reconstruct a local day from durable first-party MAPI evidence.

    This is deliberately not semantic retrieval. It checks bounded temporal rows
    plus exact ISO-date mentions across memories/conversation archives, reports
    truncation explicitly, and only permits a "no data" claim when every checked
    source completed without truncation and yielded no evidence.
    """
    if limit < 1 or limit > 500:
        return {"status": "error", "error": "limit must be between 1 and 500"}

    try:
        target_day = _parse_day(date)
        tz = _parse_timezone(timezone_name)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    local_start = datetime.combine(target_day, time.min, tzinfo=tz)
    local_end = datetime.combine(target_day.fromordinal(target_day.toordinal() + 1), time.min, tzinfo=tz)
    utc_start = _utc_text(local_start)
    utc_end = _utc_text(local_end)
    date_text = target_day.isoformat()

    project_clause = " AND project_key = ?" if project_key else ""
    project_params: tuple[Any, ...] = (project_key,) if project_key else ()

    memory_columns = """
        id, created_at, updated_at, project_key, memory_type, entry_type, truth_kind,
        title, summary_short, content, tags, source, source_context, source_event_ref,
        conversation_key, importance_score, confidence_score
    """
    memories, mem_truncated = _fetch_limited(
        conn,
        f"""
        SELECT {memory_columns}
        FROM memories
        WHERE archived_at IS NULL
          AND datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
          {project_clause}
        ORDER BY datetime(created_at), id
        LIMIT ?
        """,
        (utc_start, utc_end, *project_params),
        limit,
    )

    direct_memory_ids = {int(row["id"]) for row in memories}
    mention_like = f"%{date_text}%"
    mention_project_clause = " AND m.project_key = ?" if project_key else ""
    mention_rows, mention_truncated = _fetch_limited(
        conn,
        f"""
        SELECT {', '.join('m.' + col.strip() for col in memory_columns.split(','))}
        FROM memories m
        WHERE m.archived_at IS NULL
          AND (
              m.content LIKE ? OR COALESCE(m.summary_short, '') LIKE ? OR
              COALESCE(m.title, '') LIKE ? OR COALESCE(m.source_context, '') LIKE ? OR
              COALESCE(m.source_event_ref, '') LIKE ? OR COALESCE(m.tags, '') LIKE ?
          )
          AND NOT (
              datetime(m.created_at) >= datetime(?) AND datetime(m.created_at) < datetime(?)
          )
          {mention_project_clause}
        ORDER BY datetime(m.created_at), m.id
        LIMIT ?
        """,
        (
            mention_like, mention_like, mention_like, mention_like, mention_like, mention_like,
            utc_start, utc_end, *project_params,
        ),
        limit,
    )
    mention_rows = [row for row in mention_rows if int(row["id"]) not in direct_memory_ids]

    conversation_project_clause = " AND project_key = ?" if project_key else ""
    conversations, conv_truncated = _fetch_limited(
        conn,
        f"""
        SELECT id, conversation_id, title, source, project_key, tags, word_count,
               created_at, archived_at, content
        FROM conversation_archives
        WHERE datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
          {conversation_project_clause}
        ORDER BY datetime(created_at), id
        LIMIT ?
        """,
        (utc_start, utc_end, *project_params),
        limit,
    )

    direct_conversation_ids = {int(row["id"]) for row in conversations}
    conv_mention_project_clause = " AND project_key = ?" if project_key else ""
    conversation_mentions, conv_mention_truncated = _fetch_limited(
        conn,
        f"""
        SELECT id, conversation_id, title, source, project_key, tags, word_count,
               created_at, archived_at, content
        FROM conversation_archives
        WHERE (content LIKE ? OR COALESCE(title, '') LIKE ? OR COALESCE(tags, '') LIKE ?)
          AND NOT (
              datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)
          )
          {conv_mention_project_clause}
        ORDER BY datetime(created_at), id
        LIMIT ?
        """,
        (mention_like, mention_like, mention_like, utc_start, utc_end, *project_params),
        limit,
    )
    conversation_mentions = [
        row for row in conversation_mentions if int(row["id"]) not in direct_conversation_ids
    ]

    timeline_project_clause = " AND project_key = ?" if project_key else ""
    timeline_events, timeline_truncated = _fetch_limited(
        conn,
        f"""
        SELECT id, event_time, event_type, memory_id, related_memory_id, run_id,
               operation_id, source_table, source_row_id, origin, reconstructed,
               payload_json, created_at, timeline_scope, semantic_kind, title,
               project_key, valid_at, actor_type
        FROM timeline_events
        WHERE datetime(event_time) >= datetime(?)
          AND datetime(event_time) < datetime(?)
          {timeline_project_clause}
        ORDER BY datetime(event_time), id
        LIMIT ?
        """,
        (utc_start, utc_end, *project_params),
        limit,
    )

    items: list[dict[str, Any]] = []
    for row in memories:
        item = dict(row)
        item["kind"] = "memory"
        item["evidence_kind"] = "created_on_target_day"
        item["event_time"] = row.get("created_at")
        item["local_time"] = _local_text(row.get("created_at"), tz)
        if not include_content:
            item.pop("content", None)
        items.append(item)

    for row in conversations:
        item = dict(row)
        item["kind"] = "conversation"
        item["evidence_kind"] = "created_on_target_day"
        item["event_time"] = row.get("created_at")
        item["local_time"] = _local_text(row.get("created_at"), tz)
        if not include_content:
            item.pop("content", None)
        items.append(item)

    for row in timeline_events:
        item = dict(row)
        item["kind"] = "timeline_event"
        item["evidence_kind"] = "event_time_on_target_day"
        item["local_time"] = _local_text(row.get("event_time"), tz)
        items.append(item)

    items.sort(key=lambda item: (item.get("local_time") or "", item.get("kind") or "", int(item.get("id") or 0)))

    supporting_items: list[dict[str, Any]] = []
    for row in mention_rows:
        item = dict(row)
        item["kind"] = "memory_date_mention"
        item["evidence_kind"] = "exact_date_mention_created_outside_target_day"
        item["event_time"] = None
        item["local_time"] = None
        if not include_content:
            item.pop("content", None)
        supporting_items.append(item)
    for row in conversation_mentions:
        item = dict(row)
        item["kind"] = "conversation_date_mention"
        item["evidence_kind"] = "exact_date_mention_created_outside_target_day"
        item["event_time"] = None
        item["local_time"] = None
        if not include_content:
            item.pop("content", None)
        supporting_items.append(item)

    truncation = {
        "memories": mem_truncated,
        "memory_date_mentions": mention_truncated,
        "conversations": conv_truncated,
        "conversation_date_mentions": conv_mention_truncated,
        "timeline_events": timeline_truncated,
    }
    bounded_complete = not any(truncation.values())
    total_evidence = len(items) + len(supporting_items)
    if not bounded_complete:
        coverage_state = "truncated"
    elif items:
        coverage_state = "bounded_complete"
    elif supporting_items:
        coverage_state = "date_mentions_only"
    else:
        coverage_state = "no_evidence"

    project_keys = sorted({
        str(item["project_key"])
        for item in [*items, *supporting_items]
        if item.get("project_key")
    })
    direct_memories = [item for item in items if item.get("kind") == "memory"]
    provenance_gaps = {
        "memories_without_conversation_key": sum(1 for item in direct_memories if not item.get("conversation_key")),
        "memories_without_source_event_ref": sum(1 for item in direct_memories if not item.get("source_event_ref")),
    }

    return {
        "status": "ok",
        "date": date_text,
        "timezone": timezone_name,
        "utc_window": {"start": utc_start, "end": utc_end},
        "project_key": project_key,
        "items_count": len(items),
        "supporting_items_count": len(supporting_items),
        "items": items,
        "supporting_items": supporting_items,
        "projects": project_keys,
        "coverage": {
            "state": coverage_state,
            "bounded_complete": bounded_complete,
            "absence_claim_allowed": bounded_complete and total_evidence == 0,
            "sources_checked": [
                "memories.created_at",
                "memories.exact_date_mentions",
                "conversation_archives.created_at",
                "conversation_archives.exact_date_mentions",
                "timeline_events.event_time",
            ],
            "truncated": truncation,
            "counts": {
                "memories": len(memories),
                "memory_date_mentions": len(mention_rows),
                "conversations": len(conversations),
                "conversation_date_mentions": len(conversation_mentions),
                "timeline_events": len(timeline_events),
            },
            "provenance_gaps": provenance_gaps,
            "note": (
                "absence_claim_allowed only means no evidence exists in the checked first-party MAPI sources "
                "within the configured bounded scan; external systems such as Gmail are not checked here."
            ),
        },
    }
