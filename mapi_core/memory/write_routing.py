from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from mapi_core.memory.current_state import resolve_current_memory_state
from mapi_core.memory.sensitivity import RESTRICTED_CAPTURE_CLASSES, classify_memory_sensitivity

WRITE_PREFLIGHT_SCHEMA = "memory_write_preflight.v1"
WRITE_RESULT_SCHEMA = "memory_write_result.v1"
WRITE_ROUTING_VERSION = "memory_write_routing.v1"
ALLOWED_WRITE_INTENTS = frozenset({"user_explicit", "agent_autonomous"})

_STOPWORDS = frozenset({
    "a", "an", "and", "are", "be", "by", "dla", "do", "for", "from", "i", "in", "is", "jest",
    "ma", "na", "of", "or", "oraz", "powinien", "powinna", "powinno", "się", "sie", "the", "to",
    "w", "with", "z", "że", "ze",
})
_POSITIVE = ("is enabled", "remains enabled", "must be enabled", "jest włączony", "jest wlaczony", "działa", "dziala", "allowed", "active", "enabled", "true")
_NEGATIVE = ("is not enabled", "must not be enabled", "is disabled", "do not allow", "jest wyłączony", "jest wylaczony", "nie działa", "nie dziala", "forbidden", "blocked", "disabled", "false")
_POLARITY = frozenset({"active", "allowed", "blocked", "disabled", "dziala", "działa", "enabled", "false", "forbidden", "nie", "not", "true", "wlaczony", "włączony", "wylaczony", "wyłączony"})


def normalize_memory_content(value: str) -> str:
    value = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return re.sub(r"\s+", " ", value).strip()


def write_input_fingerprint(*, content: str, project_key: str | None, scope_code: str | None, source_event_ref: str | None, write_intent: str) -> str:
    payload = {
        "schema": WRITE_ROUTING_VERSION,
        "content": normalize_memory_content(content).casefold(),
        "project_key": str(project_key or "").strip().casefold() or None,
        "scope_code": str(scope_code or "").strip().casefold() or None,
        "source_event_ref": str(source_event_ref or "").strip() or None,
        "write_intent": str(write_intent or "").strip().casefold(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _domain_clause(project_key: str | None, scope_code: str | None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if str(project_key or "").strip():
        clauses.append("project_key = ?")
        params.append(str(project_key).strip())
    else:
        clauses.append("(project_key IS NULL OR trim(project_key) = '')")
    if str(scope_code or "").strip():
        clauses.append("scope_code = ?")
        params.append(str(scope_code).strip())
    else:
        clauses.append("(scope_code IS NULL OR trim(scope_code) = '')")
    return " AND ".join(clauses), params


def _current_domain_memories(conn: Any, *, project_key: str | None, scope_code: str | None) -> list[dict[str, Any]]:
    where, params = _domain_clause(project_key, scope_code)
    rows = conn.execute(
        f"""SELECT * FROM memories WHERE {where}
        AND COALESCE(state_code,'active') NOT IN ('archived','expired','rejected','cancelled')
        AND COALESCE(memory_v2_status,'active') NOT IN ('archived','expired','rejected','cancelled')
        ORDER BY COALESCE(updated_at,created_at,'') DESC,id DESC LIMIT 250""",
        params,
    ).fetchall()
    return list(resolve_current_memory_state(conn, [_row_dict(row) for row in rows], include_history=False)["items"])


def _source_event_match(
    conn: Any,
    source_event_ref: str | None,
    *,
    project_key: str | None,
    scope_code: str | None,
) -> dict[str, Any] | None:
    ref = str(source_event_ref or "").strip()
    if not ref:
        return None
    where, params = _domain_clause(project_key, scope_code)
    row = conn.execute(
        f"SELECT * FROM memories WHERE source_event_ref=? AND {where} ORDER BY id DESC LIMIT 1",
        [ref, *params],
    ).fetchone()
    return _row_dict(row) if row is not None else None


def _exact_duplicate(memories: Iterable[dict[str, Any]], content: str) -> dict[str, Any] | None:
    target = normalize_memory_content(content).casefold()
    return next((item for item in memories if normalize_memory_content(str(item.get("content") or "")).casefold() == target), None)


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-ząćęłńóśźż0-9_-]+", normalize_memory_content(value).casefold()) if token not in _STOPWORDS and token not in _POLARITY and len(token) >= 2}


def _polarity(value: str) -> int:
    text = normalize_memory_content(value).casefold()
    if any(phrase in text for phrase in _NEGATIVE):
        return -1
    if any(phrase in text for phrase in _POSITIVE):
        return 1
    return 0


def _subject_overlap(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if len(a) < 2 or len(b) < 2:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def _high_risk_conflict(memories: Iterable[dict[str, Any]], *, content: str, supersedes_memory_id: int | None) -> dict[str, Any] | None:
    polarity = _polarity(content)
    if polarity == 0:
        return None
    ignored = int(supersedes_memory_id) if supersedes_memory_id is not None else None
    best: tuple[float, dict[str, Any]] | None = None
    for item in memories:
        memory_id = int(item.get("id") or 0)
        if ignored == memory_id:
            continue
        existing = str(item.get("content") or "")
        if _polarity(existing) in {0, polarity}:
            continue
        overlap = _subject_overlap(content, existing)
        if overlap < 0.75:
            continue
        if best is None or (overlap, memory_id) > (best[0], int(best[1].get("id") or 0)):
            best = (overlap, item)
    if best is None:
        return None
    return {
        "existing_memory_id": int(best[1]["id"]),
        "existing_summary_short": best[1].get("summary_short"),
        "existing_title": best[1].get("title"),
        "subject_overlap": round(float(best[0]), 4),
        "recommended_relation": "supersedes",
    }


def memory_write_preflight(conn: Any, *, content: str, project_key: str | None, scope_code: str | None, source_event_ref: str | None, write_intent: str, tags: str | None = None, supersedes_memory_id: int | None = None) -> dict[str, Any]:
    content = normalize_memory_content(content)
    intent = str(write_intent or "").strip().casefold()
    fingerprint = write_input_fingerprint(content=content, project_key=project_key, scope_code=scope_code, source_event_ref=source_event_ref, write_intent=intent)
    if intent not in ALLOWED_WRITE_INTENTS:
        return {"status": "blocked", "schema": WRITE_PREFLIGHT_SCHEMA, "reason": "invalid_write_intent", "allowed_write_intents": sorted(ALLOWED_WRITE_INTENTS), "input_fingerprint": fingerprint}
    sensitivity = classify_memory_sensitivity(content, metadata={"tags": tags})
    if sensitivity["sensitivity_class"] in RESTRICTED_CAPTURE_CLASSES:
        return {"status": "blocked_never_store", "schema": WRITE_PREFLIGHT_SCHEMA, "reason": "restricted_sensitivity_class", "reason_codes": list(sensitivity["reason_codes"]), "sensitivity_class": sensitivity["sensitivity_class"], "input_fingerprint": fingerprint}
    replay = _source_event_match(
        conn,
        source_event_ref,
        project_key=project_key,
        scope_code=scope_code,
    )
    if replay is not None:
        return {"status": "duplicate_existing", "schema": WRITE_PREFLIGHT_SCHEMA, "reason": "source_event_ref_match", "existing_memory": replay, "input_fingerprint": fingerprint, "sensitivity": sensitivity}
    current = _current_domain_memories(conn, project_key=project_key, scope_code=scope_code)
    duplicate = _exact_duplicate(current, content)
    if duplicate is not None:
        return {"status": "duplicate_existing", "schema": WRITE_PREFLIGHT_SCHEMA, "reason": "exact_content_match", "existing_memory": duplicate, "input_fingerprint": fingerprint, "sensitivity": sensitivity}
    conflict = _high_risk_conflict(current, content=content, supersedes_memory_id=supersedes_memory_id)
    if conflict is not None:
        return {"status": "conflict_requires_resolution", "schema": WRITE_PREFLIGHT_SCHEMA, "input_fingerprint": fingerprint, "sensitivity": sensitivity, **conflict}
    return {"status": "allowed", "schema": WRITE_PREFLIGHT_SCHEMA, "input_fingerprint": fingerprint, "sensitivity": sensitivity, "write_intent": intent, "current_candidate_count": len(current)}