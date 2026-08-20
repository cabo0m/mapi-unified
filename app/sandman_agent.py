from __future__ import annotations

import json
import sqlite3
from typing import Any

from app import lm_studio_client
from mapi_core.memory.capture_queue import create_capture_review_item
from mapi_core.memory.sensitivity import RESTRICTED_CAPTURE_CLASSES, classify_memory_sensitivity
from mapi_core.memory.write_routing import normalize_memory_content, write_input_fingerprint
from app.memory_store import utc_now_iso

LM_STUDIO_MODEL = lm_studio_client.LM_STUDIO_MODEL
DEFAULT_PROJECT_KEY = "mapi"

_HOST_CONTEXT_PROMPT = """\
HOST CONTEXT MCP MAPI-local:
- Dzialasz wewnatrz serwera MCP mapi-local.
- Nazwy narzedzi sa funkcjami hosta MAPI-local. Uzywaj ich przez JSON tool_call.
- Dla konkretnych liczbowych ID memories uzywaj bezposrednio get_memory(memory_id) i get_memory_links(memory_id).
- Nie szukaj konkretnych ID przez search_memories i nie buduj zapytan tekstowych w stylu memory_id:505 OR memory_id:506.
- Jesli uzytkownik poda kilka ID, obsluguj je kolejno. Jeden krok to jedno narzedzie.
- ID ingest_queue nie sa ID memories. Dla nich uzywaj get_ingest_item/list_ingest_queue, a nie get_memory/search_memories.
"""

_ACTION_JSON_SCHEMA: dict[str, Any] = {
    "name": "sandman_memory_action",
    "schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["tool_call", "final"]},
            "tool_name": {
                "type": "string",
                "enum": [
                    "search_memories",
                    "get_memory",
                    "get_memory_links",
                    "get_project_timeline",
                    "list_conflicted_memories",
                    "propose_memory",
                    "archive_memory",
                    "link_memories",
                    "update_memory_importance",
                    "get_sandman_ai_preview",
                    "explain_conflict",
                    "list_ingest_queue",
                    "get_ingest_item",
                    "preview_research_ingest_review",
                    "reject_ingest_item",
                    "archive_ingest_item",
                    "promote_ingest_item",
                    "none",
                ],
            },
            "arguments": {"type": "object", "additionalProperties": True},
            "answer": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["kind", "tool_name", "arguments", "answer", "reason"],
        "additionalProperties": False,
    },
}

_SYSTEM_PROMPT = """\
Jesteś agentką pamięci Sandmana z dostępem do narzędzi MAPI.
Twoim zadaniem jest odpowiadać użytkownikowi oraz utrzymywać graf pamięci: szukać, czytać, linkować i ostrożnie porządkować wspomnienia.
Dostęp do pamięci odbywa się wyłącznie przez funkcje narzędziowe hosta MCP mapi-local.
Gdy potrzebujesz danych albo zapisu, zwróć kind="tool_call" z tool_name ustawionym na jedną z funkcji.
Jeśli użytkownik poda konkretne ID wspomnień, nie używaj search_memories i nie pisz zapytań typu "memory_id:505 OR ...". Użyj get_memory(memory_id) i get_memory_links(memory_id), po jednym ID na krok.
Zwracaj zawsze wyłącznie JSON zgodny ze schematem:
- kind: "tool_call" albo "final"
- tool_name: nazwa narzędzia albo "none"
- arguments: obiekt argumentów
- answer: odpowiedź końcowa albo pusty string przy tool_call
- reason: krótkie uzasadnienie kroku

Dostępne narzędzia:
- search_memories(query, limit=5)
- get_memory(memory_id)
- get_memory_links(memory_id)
- get_project_timeline(project_key="mapi", limit=8)
- list_conflicted_memories(limit=5)
- propose_memory(content, summary_short, memory_type, importance_score=0.5, tags="", project_key="mapi")
- archive_memory(memory_id, reason)
- link_memories(from_memory_id, to_memory_id, relation_type, weight=0.8)
- update_memory_importance(memory_id, new_importance, reason)
- get_sandman_ai_preview(freedom_level=1)
- explain_conflict(memory_a_id, memory_b_id)
- list_ingest_queue(ingest_status=null, project_key=null, source_type=null, tag=null, limit=10)
- get_ingest_item(ingest_item_id)
- preview_research_ingest_review(project_key=null, limit=20)
- reject_ingest_item(ingest_item_id, reason, reviewed_by="sandman_agent")
- archive_ingest_item(ingest_item_id, reason="", reviewed_by="sandman_agent")
- promote_ingest_item(ingest_item_id, memory_content, memory_type="research_note", summary_short="", tags="", importance_score=0.5, confidence_score=0.7, reviewed_by="sandman_agent")

Zasady ingest/research quarantine:
- Ingest_queue to sluza/kwarantanna, nie normalna pamiec.
- Gdy uzytkownik mowi o itemach ingestu, uzywaj narzedzi ingestowych zamiast search_memories/get_memory.
- Promuj tylko krotkie, zweryfikowane tezy, nie surowe artykuly.
- Produkty typu agent memory layer moga byc kandydatem do promocji; vector DB infrastructure zwykle trzymaj w kwarantannie albo archiwizuj jako kontekst porownawczy.
- Jesli nie masz pewnosci, wybierz preview lub keep/archive zamiast promocji.

Zasady ogólne:
- Odpowiadaj po polsku.
- Używaj tylko jednego narzędzia na krok.
- Nie zmyślaj treści wspomnień. Jeśli potrzebujesz danych, najpierw użyj narzędzia odczytu.
- Przed zapisem, linkowaniem, zmianą ważności albo archiwizacją zbierz wystarczający kontekst.
- Archiwizuj tylko wtedy, gdy użytkownik wyraźnie tego chce albo masz bardzo mocny dowód, że wpis jest zbędny lub nieaktualny.
- Przy kind="tool_call" pole answer musi być pustym stringiem.
- Przy kind="final" ustaw tool_name="none" i wpisz gotową odpowiedź w answer.

Zasady linkowania grafu:
- Gdy użytkownik prosi o linki, graf, relacje, podpinanie, skojarzenia, konsolidację albo mocniejsze linkowanie, aktywnie twórz link_memories. Nie kończ na samym wyszukiwaniu.
- Jedno wspomnienie może mieć wiele relacji. Traktuj outgoing_links[] i incoming_links[] jako tablice.
- Najpierw znajdź lub pobierz anchor memory, potem sprawdź get_memory_links(anchor_id), żeby nie tworzyć duplikatów.
- Szukaj kandydatów po project_key, tagach, summary_short, treści i osi czasu.
- Silne powiązania: ten sam projekt, wspólne tagi, requirement z implementacją, bootstrap/core z zasadami działania, timeline/migration ze zmianą schematu, starszy wpis z nowszym następcą.
- Dozwolone relation_type: supports, contradicts, supersedes, duplicate_of, related_to, context_for, clarifies, documents, implements, configures, validates, risk_for, metric_for, same_project.
- Preferuj related_to dla ogólnego powiązania.
- Preferuj context_for, gdy wpis daje tło lub warunek.
- Preferuj supports, gdy wpis potwierdza lub wzmacnia inny wpis.
- Preferuj implements, gdy wpis opisuje implementację wymagania.
- Preferuj documents, gdy wpis dokumentuje decyzję, stan lub mechanizm.
- Preferuj supersedes, gdy nowszy wpis zastępuje starszy.
- duplicate_of używaj tylko dla realnych duplikatów.
- Nie linkuj wszystkiego ze wszystkim. Każdy link musi mieć sensowne uzasadnienie w reason.
"""


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _call_lm_studio(messages: list[dict[str, str]], *, max_tokens: int = 2048, timeout: int = 300) -> dict[str, Any]:
    text = lm_studio_client.call_lm_studio(
        messages,
        {"type": "json_schema", "json_schema": _ACTION_JSON_SCHEMA},
        max_tokens=max_tokens,
        timeout=timeout,
    ).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Model nie zwrócił obiektu JSON. Odpowiedź: {text[:1200]}")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"Model nie zwrócił obiektu JSON. Odpowiedź: {text[:1200]}")
    return parsed


# ---------------------------------------------------------------------------
# READ TOOLS
# ---------------------------------------------------------------------------

def _tool_search_memories(conn: sqlite3.Connection, query: str, limit: int = 5) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"count": 0, "items": []}
    limit = max(1, min(int(limit or 5), 10))
    rows = None
    try:
        fts_query = '"' + q.replace('"', '""') + '"'
        rows = conn.execute(
            """
            SELECT m.id, m.summary_short, m.memory_type, m.importance_score, m.recall_count, m.tags,
                   snippet(memories_fts, 0, '[', ']', '…', 20) AS content
            FROM memories_fts
            JOIN memories m ON memories_fts.rowid = m.id
            WHERE memories_fts MATCH ?
              AND COALESCE(m.activity_state, 'active') = 'active'
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
    except Exception:
        pass
    if not rows:
        safe_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{safe_q}%"
        rows = conn.execute(
            """
            SELECT id, summary_short, memory_type, importance_score, recall_count, tags, content
            FROM memories
            WHERE COALESCE(activity_state, 'active') = 'active'
              AND (
                    COALESCE(content, '') LIKE ? ESCAPE '\\' OR
                    COALESCE(summary_short, '') LIKE ? ESCAPE '\\' OR
                    COALESCE(tags, '') LIKE ? ESCAPE '\\'
              )
            ORDER BY importance_score DESC, recall_count DESC, id DESC
            LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()
    items = []
    for row in rows:
        item = _row_to_dict(row)
        item["content"] = str(item.get("content") or "")[:280]
        items.append(item)
    return {"count": len(items), "items": items}


def _tool_get_memory(conn: sqlite3.Connection, memory_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    if row is None:
        return {"found": False, "memory_id": int(memory_id)}
    outgoing = conn.execute(
        "SELECT * FROM memory_links WHERE from_memory_id = ? ORDER BY id DESC LIMIT 12",
        (int(memory_id),),
    ).fetchall()
    incoming = conn.execute(
        "SELECT * FROM memory_links WHERE to_memory_id = ? ORDER BY id DESC LIMIT 12",
        (int(memory_id),),
    ).fetchall()
    return {
        "found": True,
        "memory": _row_to_dict(row),
        "outgoing_links": [_row_to_dict(item) for item in outgoing],
        "incoming_links": [_row_to_dict(item) for item in incoming],
    }


def _tool_get_memory_links(conn: sqlite3.Connection, memory_id: int) -> dict[str, Any]:
    outgoing = conn.execute(
        "SELECT * FROM memory_links WHERE from_memory_id = ? ORDER BY id DESC LIMIT 20",
        (int(memory_id),),
    ).fetchall()
    incoming = conn.execute(
        "SELECT * FROM memory_links WHERE to_memory_id = ? ORDER BY id DESC LIMIT 20",
        (int(memory_id),),
    ).fetchall()
    return {
        "memory_id": int(memory_id),
        "outgoing_links": [_row_to_dict(item) for item in outgoing],
        "incoming_links": [_row_to_dict(item) for item in incoming],
    }


def _tool_get_project_timeline(conn: sqlite3.Connection, project_key: str = DEFAULT_PROJECT_KEY, limit: int = 8) -> dict[str, Any]:
    limit = max(1, min(int(limit or 8), 20))
    rows = conn.execute(
        """
        SELECT id, event_type, title, payload_json, valid_at, origin, created_at
        FROM timeline_events
        WHERE project_key = ?
        ORDER BY COALESCE(valid_at, created_at) DESC, id DESC
        LIMIT ?
        """,
        (project_key or DEFAULT_PROJECT_KEY, limit),
    ).fetchall()
    items = []
    for row in rows:
        item = _row_to_dict(row)
        payload = {}
        raw_payload = item.get("payload_json")
        if isinstance(raw_payload, str) and raw_payload.strip():
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                payload = {}
        item["description"] = str(payload.get("description") or "")
        item["status"] = payload.get("status")
        item.pop("payload_json", None)
        items.append(item)
    return {"project_key": project_key or DEFAULT_PROJECT_KEY, "count": len(items), "items": items}


def _tool_list_conflicted_memories(conn: sqlite3.Connection, limit: int = 5) -> dict[str, Any]:
    limit = max(1, min(int(limit or 5), 20))
    rows = conn.execute(
        """
        SELECT id, summary_short, memory_type, importance_score, recall_count, content
        FROM memories
        WHERE COALESCE(contradiction_flag, 0) = 1
        ORDER BY importance_score DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    items = []
    for row in rows:
        item = _row_to_dict(row)
        item["content"] = str(item.get("content") or "")[:280]
        items.append(item)
    return {"count": len(items), "items": items}


# ---------------------------------------------------------------------------
# WRITE TOOLS
# ---------------------------------------------------------------------------

def _tool_propose_memory(
    conn: sqlite3.Connection,
    content: str,
    summary_short: str,
    memory_type: str,
    importance_score: float = 0.5,
    tags: str = "",
    project_key: str | None = None,
) -> dict[str, Any]:
    content = normalize_memory_content(content)
    summary_short = (summary_short or "").strip()
    memory_type = (memory_type or "working").strip()
    if not content:
        return {"status": "error", "reason": "content nie może być puste"}
    sensitivity = classify_memory_sensitivity(content, metadata={"tags": tags})
    if sensitivity["sensitivity_class"] in RESTRICTED_CAPTURE_CLASSES:
        return {
            "status": "blocked_never_store",
            "memory_created": False,
            "queue_mutated": False,
            "sensitivity_class": sensitivity["sensitivity_class"],
            "reason_codes": list(sensitivity["reason_codes"]),
        }
    resolved_project_key = str(project_key or DEFAULT_PROJECT_KEY).strip() or DEFAULT_PROJECT_KEY
    fingerprint = write_input_fingerprint(
        content=content,
        project_key=resolved_project_key,
        scope_code="project",
        source_event_ref=None,
        write_intent="agent_proposed",
    )
    proposal = {
        "content": content,
        "summary_short": summary_short or content[:140],
        "title": summary_short or content[:140],
        "memory_type": memory_type,
        "source": "sandman_agent",
        "importance_score": lm_studio_client.clamp_importance(float(importance_score or 0.5)),
        "confidence_score": 0.5,
        "tags": tags or "sandman-agent,agent-proposed",
        "project_key": resolved_project_key,
        "scope_code": "project",
        "entry_type": "project",
        "truth_kind": "proposal",
        "memory_v2_status": "proposed",
        "requires_user_confirmation": True,
    }
    created = create_capture_review_item(
        conn,
        proposal_key=f"sandman:{fingerprint}",
        proposal=proposal,
        input_fingerprint=fingerprint,
        project_key=resolved_project_key,
        scope_code="project",
        source_context="sandman_agent",
        recommended_action="capture_review",
        utc_now_iso=utc_now_iso,
        normalize_required_text=lambda value, field: str(value or "").strip() or (_ for _ in ()).throw(ValueError(f"{field} is required")),
        normalize_optional_text=lambda value: str(value).strip() if value is not None and str(value).strip() else None,
        row_to_dict=_row_to_dict,
    )
    conn.commit()
    item = dict(created["item"])
    return {
        "status": "proposed" if created["created"] else "already_proposed",
        "write_mode": "agent_proposed",
        "memory_created": False,
        "capture_review_item_id": int(item["id"]),
        "input_fingerprint": fingerprint,
        "sensitivity_class": sensitivity["sensitivity_class"],
    }


def _tool_archive_memory(conn: sqlite3.Connection, memory_id: int, reason: str) -> dict[str, Any]:
    memory_id = int(memory_id)
    row = conn.execute(
        "SELECT id, activity_state, memory_type FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    if row is None:
        return {"status": "error", "reason": f"Wspomnienie {memory_id} nie istnieje"}
    if str(row["activity_state"] or "active") == "archived":
        return {"status": "already_archived", "memory_id": memory_id}
    now = utc_now_iso()
    note = f"sandman_agent: {(reason or 'agent_decision')[:120]}"
    conn.execute(
        "UPDATE memories SET activity_state = 'archived', archived_at = ?, sandman_note = ? WHERE id = ?",
        (now, note, memory_id),
    )
    conn.commit()
    return {"status": "archived", "memory_id": memory_id, "archived_at": now, "reason": reason}


_EVIDENCE_BOUND_TRUTH_RELATIONS = frozenset({"supports", "contradicts", "supersedes", "refines", "derived_from"})


def _tool_link_memories(
    conn: sqlite3.Connection,
    from_memory_id: int,
    to_memory_id: int,
    relation_type: str,
    weight: float = 0.8,
) -> dict[str, Any]:
    from_id = int(from_memory_id)
    to_id = int(to_memory_id)

    relation_type = (relation_type or "related_to").strip()
    relation_aliases = {"relates_to": "related_to"}
    relation_type = relation_aliases.get(relation_type, relation_type)

    if relation_type in _EVIDENCE_BOUND_TRUTH_RELATIONS:
        return {
            "status": "blocked",
            "reason": "canonical_relation_requires_evidence_bound_route",
            "relation_type": relation_type,
            "allowed_direct_relations": [
                "duplicate_of", "related_to", "context_for", "clarifies", "documents",
                "implements", "configures", "validates", "risk_for", "metric_for", "same_project"
            ],
        }

    weight = min(1.0, max(0.0, float(weight or 0.8)))

    allowed = {
        "duplicate_of",
        "related_to",
        "context_for",
        "clarifies",
        "documents",
        "implements",
        "configures",
        "validates",
        "risk_for",
        "metric_for",
        "same_project",
    }

    if relation_type not in allowed:
        relation_type = "related_to"

    existing = conn.execute(
        "SELECT id FROM memory_links WHERE from_memory_id = ? AND to_memory_id = ? AND relation_type = ?",
        (from_id, to_id, relation_type),
    ).fetchone()
    if existing:
        return {"status": "already_exists", "link_id": int(existing["id"]), "relation_type": relation_type}
    now = utc_now_iso()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO memory_links (from_memory_id, to_memory_id, relation_type, weight, origin, created_at) VALUES (?, ?, ?, ?, 'sandman_agent', ?)",
        (from_id, to_id, relation_type, weight, now),
    )
    conn.commit()
    link_id = int(cursor.lastrowid)
    return {
        "status": "created",
        "link_id": link_id,
        "from_memory_id": from_id,
        "to_memory_id": to_id,
        "relation_type": relation_type,
        "weight": weight,
    }


def _tool_update_memory_importance(
    conn: sqlite3.Connection,
    memory_id: int,
    new_importance: float,
    reason: str,
) -> dict[str, Any]:
    memory_id = int(memory_id)
    row = conn.execute(
        "SELECT id, importance_score, activity_state FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    if row is None:
        return {"status": "error", "reason": f"Wspomnienie {memory_id} nie istnieje"}
    new_importance = lm_studio_client.clamp_importance(float(new_importance or 0.5))
    old_importance = float(row["importance_score"] or 0.5)
    note = f"sandman_agent: {(reason or 'agent_update')[:120]}"
    conn.execute(
        "UPDATE memories SET importance_score = ?, sandman_note = ? WHERE id = ?",
        (new_importance, note, memory_id),
    )
    conn.commit()
    return {
        "status": "updated",
        "memory_id": memory_id,
        "old_importance": old_importance,
        "new_importance": new_importance,
        "reason": reason,
    }



def _tool_explain_conflict(conn: sqlite3.Connection, memory_a_id: int, memory_b_id: int) -> dict[str, Any]:
    from app import conflict_explainer
    return conflict_explainer.explain_conflict_pair(conn, int(memory_a_id), int(memory_b_id))


def _tool_get_sandman_ai_preview(conn: sqlite3.Connection, freedom_level: int = 1) -> dict[str, Any]:
    freedom_level = max(0, min(int(freedom_level or 1), 2))
    from app import sandman_ai
    archive_decisions, downgrade_decisions, keep_decisions = sandman_ai.get_ai_decisions(conn, freedom_level)
    return {
        "freedom_level": freedom_level,
        "model": sandman_ai.LM_STUDIO_MODEL,
        "archive_count": len(archive_decisions),
        "downgrade_count": len(downgrade_decisions),
        "keep_count": len(keep_decisions),
        "archive_candidates": [
            {
                "id": d["id"],
                "summary_short": d.get("summary_short"),
                "ai_reason": d.get("ai_reason"),
                "importance_score": d.get("importance_score"),
                "memory_type": d.get("memory_type"),
            }
            for d in archive_decisions
        ],
        "downgrade_candidates": [
            {
                "id": d["id"],
                "summary_short": d.get("summary_short"),
                "ai_reason": d.get("ai_reason"),
                "importance_score": d.get("importance_score"),
                "ai_new_importance": d.get("ai_new_importance"),
                "memory_type": d.get("memory_type"),
            }
            for d in downgrade_decisions
        ],
    }




def _is_memory_sqlite_connection(conn: sqlite3.Connection) -> bool:
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except Exception:
        return False
    for row in rows:
        try:
            name = row[1]
            file_path = row[2]
        except Exception:
            continue
        if name == "main" and file_path and file_path != ":memory:":
            return True
    return False


def _ensure_promoted_memory_embedding_best_effort(conn: sqlite3.Connection, memory: dict[str, Any]) -> dict[str, Any]:
    """Best-effort embedding hook for Sandman-created promoted memories."""
    memory_id = memory.get("id")
    try:
        memory_id_int = int(memory_id)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "memory_id": memory_id,
            "error_type": "ValueError",
            "error": "memory.id is missing or invalid",
        }

    # Unit tests use in-memory SQLite DBs with tiny schemas. Do not load the
    # embedding model there; production MAPI uses a file-backed DB.
    if not _is_memory_sqlite_connection(conn):
        return {"status": "skipped_in_memory_db", "memory_id": memory_id_int}

    try:
        from vector_store import ensure_embeddings_table, embed_memory

        ensure_embeddings_table(conn)
        existing = conn.execute(
            "SELECT memory_id, model_name, created_at, updated_at "
            "FROM memory_embeddings_meta WHERE memory_id = ?",
            (memory_id_int,),
        ).fetchone()
        if existing is not None:
            keys = existing.keys()
            return {
                "status": "already_present",
                "memory_id": memory_id_int,
                "model_name": existing["model_name"] if "model_name" in keys else None,
                "created_at": existing["created_at"] if "created_at" in keys else None,
                "updated_at": existing["updated_at"] if "updated_at" in keys else None,
            }

        embed_memory(
            conn,
            {
                "id": memory_id_int,
                "content": memory.get("content"),
                "summary_short": memory.get("summary_short"),
                "tags": memory.get("tags"),
            },
        )
        embedded = conn.execute(
            "SELECT memory_id, model_name, created_at, updated_at "
            "FROM memory_embeddings_meta WHERE memory_id = ?",
            (memory_id_int,),
        ).fetchone()
        if embedded is None:
            return {"status": "missing_after_embed", "memory_id": memory_id_int}
        keys = embedded.keys()
        return {
            "status": "embedded",
            "memory_id": memory_id_int,
            "model_name": embedded["model_name"] if "model_name" in keys else None,
            "created_at": embedded["created_at"] if "created_at" in keys else None,
            "updated_at": embedded["updated_at"] if "updated_at" in keys else None,
        }
    except Exception as exc:
        return {
            "status": "error",
            "memory_id": memory_id_int,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# INGEST QUARANTINE TOOLS
# ---------------------------------------------------------------------------

_INGEST_STATUSES = {"new", "parsed", "candidate", "promoted", "rejected", "archived", "merged"}


def _tool_list_ingest_queue(
    conn: sqlite3.Connection,
    ingest_status: str | None = None,
    project_key: str | None = None,
    source_type: str | None = None,
    tag: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 10), 50))
    filters: list[str] = []
    params: list[Any] = []
    status = (ingest_status or "").strip().lower()
    if status:
        if status not in _INGEST_STATUSES:
            return {"status": "error", "reason": f"unknown ingest_status: {status}"}
        filters.append("ingest_status = ?")
        params.append(status)
    if project_key:
        filters.append("project_key = ?")
        params.append(str(project_key).strip())
    if source_type:
        filters.append("source_type = ?")
        params.append(str(source_type).strip().lower())
    if tag:
        safe_tag = str(tag).strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append("COALESCE(tags, '') LIKE ? ESCAPE '\\'")
        params.append(f"%{safe_tag}%")
    where = "WHERE " + " AND ".join(filters) if filters else ""
    rows = conn.execute(
        f"""
        SELECT id, title, source_type, source_ref, project_key, tags, ingest_status,
               quality_score, source_reliability_score, duplicate_of_ingest_id,
               promoted_memory_id, rejection_reason, created_at, reviewed_at, reviewed_by,
               substr(COALESCE(normalized_text, raw_text), 1, 420) AS preview_text
        FROM ingest_items
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return {"status": "ok", "count": len(rows), "items": [_row_to_dict(row) for row in rows]}


def _tool_get_ingest_item(conn: sqlite3.Connection, ingest_item_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ingest_items WHERE id = ?", (int(ingest_item_id),)).fetchone()
    if row is None:
        return {"status": "not_found", "ingest_item_id": int(ingest_item_id)}
    item = _row_to_dict(row)
    raw_claims = item.get("extracted_claims_json")
    if raw_claims:
        try:
            item["extracted_claims"] = json.loads(raw_claims)
        except Exception:
            item["extracted_claims"] = None
    else:
        item["extracted_claims"] = None
    return {"status": "ok", "item": item}


def _tool_preview_research_ingest_review(
    conn: sqlite3.Connection,
    project_key: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 20), 50))
    filters = ["ingest_status IN ('new', 'parsed', 'candidate')"]
    params: list[Any] = []
    if project_key:
        filters.append("project_key = ?")
        params.append(str(project_key).strip())
    rows = conn.execute(
        f"""
        SELECT * FROM ingest_items
        WHERE {' AND '.join(filters)}
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    decisions = []
    for row in rows:
        item = _row_to_dict(row)
        text = str(item.get("normalized_text") or item.get("raw_text") or "")
        quality = float(item.get("quality_score") or 0.0)
        reliability = float(item.get("source_reliability_score") or 0.0)
        combined = round((quality + reliability) / 2.0, 3)
        tags = str(item.get("tags") or "").lower()
        title = str(item.get("title") or "")
        if item.get("duplicate_of_ingest_id"):
            action = "merge"
            reason = "duplicate_of_ingest_id is set"
        elif any(token in tags for token in ("mem0", "zep", "letta", "agent-memory", "ai-agents")) and combined >= 0.70 and len(text) >= 80:
            action = "promote_candidate"
            reason = "direct agent-memory/runtime candidate with high source quality"
        elif any(token in tags for token in ("vector-db", "infrastructure", "pinecone", "qdrant", "weaviate")):
            action = "keep_in_quarantine"
            reason = "vector infrastructure, useful context but not direct agent memory layer"
        elif combined >= 0.72 and len(text) >= 80:
            action = "promote_candidate"
            reason = "high combined quality/reliability and enough content"
        elif combined < 0.35 or len(text) < 20:
            action = "reject_candidate"
            reason = "low score or too little content"
        else:
            action = "keep_in_quarantine"
            reason = "needs more evidence or manual review"
        decisions.append({
            "ingest_item_id": int(item["id"]),
            "title": title,
            "action": action,
            "reason": reason,
            "combined_score": combined,
            "tags": item.get("tags"),
        })
    return {"status": "ok", "count": len(decisions), "decisions": decisions}


def _tool_reject_ingest_item(
    conn: sqlite3.Connection,
    ingest_item_id: int,
    reason: str,
    reviewed_by: str = "sandman_agent",
) -> dict[str, Any]:
    item = _tool_get_ingest_item(conn, ingest_item_id)
    if item.get("status") != "ok":
        return item
    current = item["item"]
    if current.get("ingest_status") == "promoted":
        return {"status": "noop", "reason": "promoted ingest items cannot be rejected", "item": current}
    conn.execute(
        "UPDATE ingest_items SET ingest_status = 'rejected', rejection_reason = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?",
        ((reason or "sandman rejected")[:500], utc_now_iso(), (reviewed_by or "sandman_agent")[:120], int(ingest_item_id)),
    )
    conn.commit()
    return {"status": "rejected", "item": _tool_get_ingest_item(conn, ingest_item_id).get("item")}


def _tool_archive_ingest_item(
    conn: sqlite3.Connection,
    ingest_item_id: int,
    reason: str = "",
    reviewed_by: str = "sandman_agent",
) -> dict[str, Any]:
    item = _tool_get_ingest_item(conn, ingest_item_id)
    if item.get("status") != "ok":
        return item
    conn.execute(
        "UPDATE ingest_items SET ingest_status = 'archived', rejection_reason = COALESCE(?, rejection_reason), reviewed_at = ?, reviewed_by = ? WHERE id = ?",
        (((reason or "sandman archived")[:500]), utc_now_iso(), (reviewed_by or "sandman_agent")[:120], int(ingest_item_id)),
    )
    conn.commit()
    return {"status": "archived", "item": _tool_get_ingest_item(conn, ingest_item_id).get("item")}


def _tool_promote_ingest_item(
    conn: sqlite3.Connection,
    ingest_item_id: int,
    memory_content: str,
    memory_type: str = "research_note",
    summary_short: str = "",
    tags: str = "",
    importance_score: float = 0.5,
    confidence_score: float = 0.7,
    reviewed_by: str = "sandman_agent",
) -> dict[str, Any]:
    item_result = _tool_get_ingest_item(conn, ingest_item_id)
    if item_result.get("status") != "ok":
        return item_result
    item = item_result["item"]
    if item.get("ingest_status") == "promoted" and item.get("promoted_memory_id"):
        return {"status": "already_promoted", "promoted_memory_id": int(item["promoted_memory_id"]), "item": item}
    content = (memory_content or "").strip()
    if not content:
        return {"status": "error", "reason": "memory_content cannot be empty"}
    now = utc_now_iso()
    merged_tags = ",".join(part for part in [tags or "", item.get("tags") or "", "research-ingest,evidence-backed,sandman-promoted"] if part)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO memories (
            content, summary_short, memory_type, source,
            importance_score, confidence_score, tags,
            created_at, last_accessed_at, activity_state,
            evidence_count, contradiction_flag,
            layer_code, area_code, state_code, scope_code,
            project_key, validation_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, 0, 'working', 'knowledge', 'active', ?, ?, 'research_ingest')
        """,
        (
            content,
            (summary_short or item.get("title") or content[:120])[:500],
            (memory_type or "research_note")[:120],
            item.get("source_ref"),
            lm_studio_client.clamp_importance(float(importance_score or 0.5)),
            lm_studio_client.clamp_importance(float(confidence_score or 0.7)),
            merged_tags,
            now,
            now,
            "project" if item.get("project_key") else "global",
            item.get("project_key"),
        ),
    )
    memory_id = int(cursor.lastrowid)
    conn.execute(
        "UPDATE ingest_items SET ingest_status = 'promoted', promoted_memory_id = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?",
        (memory_id, now, (reviewed_by or "sandman_agent")[:120], int(ingest_item_id)),
    )
    memory = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    memory_dict = _row_to_dict(memory)
    memory_dict["embedding_hook"] = _ensure_promoted_memory_embedding_best_effort(conn, memory_dict)
    conn.commit()
    return {"status": "promoted", "memory": memory_dict, "item": _tool_get_ingest_item(conn, ingest_item_id).get("item")}


# ---------------------------------------------------------------------------
# DISPATCH
# ---------------------------------------------------------------------------

_TOOL_DISPATCH = {
    "search_memories": _tool_search_memories,
    "get_memory": _tool_get_memory,
    "get_memory_links": _tool_get_memory_links,
    "get_project_timeline": _tool_get_project_timeline,
    "list_conflicted_memories": _tool_list_conflicted_memories,
    "propose_memory": _tool_propose_memory,
    "archive_memory": _tool_archive_memory,
    "link_memories": _tool_link_memories,
    "update_memory_importance": _tool_update_memory_importance,
    "get_sandman_ai_preview": _tool_get_sandman_ai_preview,
    "explain_conflict": _tool_explain_conflict,
    "list_ingest_queue": _tool_list_ingest_queue,
    "get_ingest_item": _tool_get_ingest_item,
    "preview_research_ingest_review": _tool_preview_research_ingest_review,
    "reject_ingest_item": _tool_reject_ingest_item,
    "archive_ingest_item": _tool_archive_ingest_item,
    "promote_ingest_item": _tool_promote_ingest_item,
}


def _normalize_action(action: dict[str, Any]) -> dict[str, Any]:
    kind = str(action.get("kind") or "final").strip().lower()
    if kind not in {"tool_call", "final"}:
        kind = "final"
    tool_name = str(action.get("tool_name") or "none").strip()
    unknown_tool: str | None = None
    if tool_name not in _TOOL_DISPATCH and tool_name != "none":
        unknown_tool = tool_name
        tool_name = "none"
    arguments = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
    answer = str(action.get("answer") or "").strip()
    reason = str(action.get("reason") or "").strip()
    if unknown_tool:
        reason = reason or f"unknown_tool:{unknown_tool}"
    if kind == "final":
        tool_name = "none"
    return {"kind": kind, "tool_name": tool_name, "arguments": arguments, "answer": answer, "reason": reason}


def _run_tool(conn: sqlite3.Connection, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool_name not in _TOOL_DISPATCH:
        raise ValueError(f"Nieznane narzędzie: {tool_name}")

    if tool_name == "search_memories":
        return _tool_search_memories(conn, str(arguments.get("query") or ""), int(arguments.get("limit") or 5))
    if tool_name in {"get_memory", "get_memory_links"}:
        memory_id = int(arguments.get("memory_id") or 0)
        if memory_id <= 0:
            return {"status": "error", "reason": "memory_id jest wymagany i musi być > 0"}
        return _TOOL_DISPATCH[tool_name](conn, memory_id)
    if tool_name == "get_project_timeline":
        return _tool_get_project_timeline(
            conn,
            str(arguments.get("project_key") or DEFAULT_PROJECT_KEY),
            int(arguments.get("limit") or 8),
        )
    if tool_name == "list_conflicted_memories":
        return _tool_list_conflicted_memories(conn, int(arguments.get("limit") or 5))
    if tool_name == "propose_memory":
        return _tool_propose_memory(
            conn,
            str(arguments.get("content") or ""),
            str(arguments.get("summary_short") or ""),
            str(arguments.get("memory_type") or "working"),
            float(arguments.get("importance_score") or 0.5),
            str(arguments.get("tags") or ""),
            arguments.get("project_key") or None,
        )
    if tool_name == "archive_memory":
        memory_id = int(arguments.get("memory_id") or 0)
        if memory_id <= 0:
            return {"status": "error", "reason": "memory_id jest wymagany i musi być > 0"}
        return _tool_archive_memory(conn, memory_id, str(arguments.get("reason") or ""))
    if tool_name == "link_memories":
        from_id = int(arguments.get("from_memory_id") or 0)
        to_id = int(arguments.get("to_memory_id") or 0)
        if from_id <= 0 or to_id <= 0:
            return {"status": "error", "reason": "from_memory_id i to_memory_id muszą być > 0"}
        return _tool_link_memories(
            conn,
            from_id,
            to_id,
            str(arguments.get("relation_type") or "related_to"),
            float(arguments.get("weight") or 0.8),
        )
    if tool_name == "update_memory_importance":
        memory_id = int(arguments.get("memory_id") or 0)
        if memory_id <= 0:
            return {"status": "error", "reason": "memory_id jest wymagany i musi być > 0"}
        return _tool_update_memory_importance(
            conn,
            memory_id,
            float(arguments.get("new_importance") or 0.5),
            str(arguments.get("reason") or ""),
        )
    if tool_name == "get_sandman_ai_preview":
        return _tool_get_sandman_ai_preview(conn, int(arguments.get("freedom_level") or 1))
    if tool_name == "explain_conflict":
        memory_a_id = int(arguments.get("memory_a_id") or 0)
        memory_b_id = int(arguments.get("memory_b_id") or 0)
        if memory_a_id <= 0 or memory_b_id <= 0:
            return {"status": "error", "reason": "memory_a_id i memory_b_id muszą być > 0"}
        return _tool_explain_conflict(conn, memory_a_id, memory_b_id)
    if tool_name == "list_ingest_queue":
        return _tool_list_ingest_queue(
            conn,
            arguments.get("ingest_status"),
            arguments.get("project_key"),
            arguments.get("source_type"),
            arguments.get("tag"),
            int(arguments.get("limit") or 10),
        )
    if tool_name == "get_ingest_item":
        return _tool_get_ingest_item(conn, int(arguments.get("ingest_item_id") or 0))
    if tool_name == "preview_research_ingest_review":
        return _tool_preview_research_ingest_review(
            conn,
            arguments.get("project_key"),
            int(arguments.get("limit") or 20),
        )
    if tool_name == "reject_ingest_item":
        return _tool_reject_ingest_item(
            conn,
            int(arguments.get("ingest_item_id") or 0),
            str(arguments.get("reason") or "sandman rejected"),
            str(arguments.get("reviewed_by") or "sandman_agent"),
        )
    if tool_name == "archive_ingest_item":
        return _tool_archive_ingest_item(
            conn,
            int(arguments.get("ingest_item_id") or 0),
            str(arguments.get("reason") or "sandman archived"),
            str(arguments.get("reviewed_by") or "sandman_agent"),
        )
    if tool_name == "promote_ingest_item":
        return _tool_promote_ingest_item(
            conn,
            int(arguments.get("ingest_item_id") or 0),
            str(arguments.get("memory_content") or ""),
            str(arguments.get("memory_type") or "research_note"),
            str(arguments.get("summary_short") or ""),
            str(arguments.get("tags") or ""),
            float(arguments.get("importance_score") or 0.5),
            float(arguments.get("confidence_score") or 0.7),
            str(arguments.get("reviewed_by") or "sandman_agent"),
        )
    raise ValueError(f"Nieobsługiwane narzędzie: {tool_name}")


def _looks_like_linking_task(prompt: str) -> bool:
    text = (prompt or "").lower()
    needles = (
        "link", "linki", "linków", "linkowania", "graf", "relacje",
        "podpin", "podepn", "skojarz", "skojarzenia", "konsolidac",
        "memory_links", "related_to", "context_for", "supports",
    )
    return any(needle in text for needle in needles)


def _looks_like_ingest_task(prompt: str) -> bool:
    text = (prompt or "").lower()
    needles = ("ingest", "research ingest", "kwarantann", "sluza", "śluza", "ingest_queue", "pre-sluice")
    return any(needle in text for needle in needles)


def _write_tools_allowed_for_prompt(prompt: str) -> set[str]:
    if _looks_like_linking_task(prompt):
        return {"link_memories"}
    allowed = {"propose_memory", "archive_memory", "link_memories", "update_memory_importance"}
    if _looks_like_ingest_task(prompt):
        allowed |= {"reject_ingest_item", "archive_ingest_item", "promote_ingest_item"}
    return allowed


def _blocked_tool_result(tool_name: str, prompt: str) -> dict[str, Any]:
    return {
        "status": "blocked_by_host_guard",
        "tool_name": tool_name,
        "reason": "W zadaniu linkowania host dopuszcza tylko link_memories jako narzędzie zapisu.",
        "linking_task": _looks_like_linking_task(prompt),
    }


def _auto_final_after_link(
    tool_result: dict[str, Any],
    *,
    step: int,
    trace: list[dict[str, Any]],
) -> dict[str, Any]:
    status = str(tool_result.get("status") or "unknown")

    if status == "created":
        link_id = tool_result.get("link_id")
        answer = (
            "Utworzyłam link "
            f"{tool_result.get('from_memory_id')} -> {tool_result.get('to_memory_id')} "
            f"({tool_result.get('relation_type')}, weight={tool_result.get('weight')})."
        )
        if link_id is not None:
            answer += f" ID linku: {link_id}."
    elif status == "already_exists":
        answer = (
            "Nie utworzyłam duplikatu. Taki link już istnieje "
            f"(link_id={tool_result.get('link_id')}, relation_type={tool_result.get('relation_type')})."
        )
    else:
        answer = f"Próba linkowania zakończyła się statusem: {status}."

    return {
        "status": "completed",
        "model": LM_STUDIO_MODEL,
        "steps": step,
        "answer": answer,
        "reason": "auto_final_after_link_memories",
        "trace": trace,
        "auto_final_after_write": True,
    }



# ---------------------------------------------------------------------------
# HOST-SIDE INGEST ROUTER OVERRIDE
# ---------------------------------------------------------------------------

def _extract_ints_from_prompt(prompt: str) -> list[int]:
    import re

    values: list[int] = []
    for raw in re.findall(r"\b\d+\b", prompt or ""):
        try:
            value = int(raw)
        except ValueError:
            continue
        if value not in values:
            values.append(value)
    return values


def _prompt_requests_no_write(prompt: str) -> bool:
    text = (prompt or "").lower()
    needles = (
        "nie promuj",
        "do not promote",
        "bez promocji",
        "final answer only",
        "tylko odpowiedz",
        "tylko odpowiedź",
        "preview",
        "podglad",
        "podgląd",
        "classify",
        "klasyfik",
        "review",
        "przejrzyj",
        "sprawdz",
        "sprawdź",
    )
    return any(needle in text for needle in needles)


def _infer_project_key_from_prompt(prompt: str) -> str | None:
    text = prompt or ""
    lowered = text.lower()
    if "demo-project" in lowered:
        return "demo-project"
    if "mapi" in lowered:
        return "mapi"
    return None


def _infer_tag_from_prompt(prompt: str) -> str | None:
    lowered = (prompt or "").lower()
    if "pre-sluice" in lowered:
        return "pre-sluice"
    if "market-scan" in lowered:
        return "market-scan"
    if "external-memory" in lowered:
        return "external-memory"
    return None


def _classify_ingest_item_for_router(item: dict[str, Any]) -> dict[str, Any]:
    tags = str(item.get("tags") or "").lower()
    title = str(item.get("title") or "")
    text = str(item.get("normalized_text") or item.get("raw_text") or item.get("preview_text") or "")
    quality = float(item.get("quality_score") or 0.0)
    reliability = float(item.get("source_reliability_score") or 0.0)
    combined = round((quality + reliability) / 2.0, 3)

    if any(token in tags for token in ("mem0", "zep")):
        item_class = "direct_memory_layer"
        recommendation = "promote_candidate" if combined >= 0.70 and len(text) >= 80 else "keep_in_quarantine"
        reason = "direct external memory layer for AI agents"
    elif "letta" in tags:
        item_class = "agent_runtime_memory"
        recommendation = "promote_candidate" if combined >= 0.70 and len(text) >= 80 else "keep_in_quarantine"
        reason = "agent runtime with explicit memory architecture"
    elif any(token in tags for token in ("vector-db", "weaviate", "qdrant", "pinecone", "infrastructure")):
        item_class = "vector_infrastructure"
        recommendation = "keep_in_quarantine"
        reason = "vector/RAG infrastructure, not a full agent-memory policy layer"
    elif "web-disabled" in tags or combined < 0.35 or len(text) < 20:
        item_class = "low_confidence_or_placeholder"
        recommendation = "reject_candidate"
        reason = "low confidence, placeholder, or too little usable content"
    else:
        item_class = "research_candidate"
        recommendation = "promote_candidate" if combined >= 0.72 and len(text) >= 80 else "keep_in_quarantine"
        reason = "generic research candidate scored by quality/reliability"

    return {
        "ingest_item_id": int(item.get("id") or item.get("ingest_item_id") or 0),
        "title": title,
        "class": item_class,
        "recommendation": recommendation,
        "reason": reason,
        "combined_score": combined,
        "tags": item.get("tags"),
    }


def _load_ingest_items_for_router(conn: sqlite3.Connection, prompt: str) -> dict[str, Any]:
    explicit_ids = _extract_ints_from_prompt(prompt)
    # Treat small explicit numbers as likely ingest ids. Years and prices are ignored by existence checks.
    items: list[dict[str, Any]] = []
    seen: set[int] = set()

    for candidate_id in explicit_ids:
        if candidate_id <= 0 or candidate_id > 100000:
            continue
        result = _tool_get_ingest_item(conn, candidate_id)
        if result.get("status") == "ok":
            item = result["item"]
            item_id = int(item["id"])
            if item_id not in seen:
                items.append(item)
                seen.add(item_id)

    if items:
        return {"source": "explicit_ids", "items": items}

    queue = _tool_list_ingest_queue(
        conn,
        ingest_status="candidate",
        project_key=_infer_project_key_from_prompt(prompt),
        source_type=None,
        tag=_infer_tag_from_prompt(prompt),
        limit=30,
    )
    if queue.get("status") != "ok" or not queue.get("items"):
        queue = _tool_list_ingest_queue(
            conn,
            ingest_status=None,
            project_key=_infer_project_key_from_prompt(prompt),
            source_type=None,
            tag=_infer_tag_from_prompt(prompt),
            limit=30,
        )
    return {"source": "queue_filter", "items": queue.get("items", []) if queue.get("status") == "ok" else [], "queue": queue}


def _maybe_handle_ingest_task_with_host_router(
    conn: sqlite3.Connection,
    *,
    user_query: str,
    max_steps: int,
) -> dict[str, Any] | None:
    prompt = (user_query or "").strip()
    if not _looks_like_ingest_task(prompt):
        return None

    loaded = _load_ingest_items_for_router(conn, prompt)
    items = loaded.get("items", [])
    decisions = [_classify_ingest_item_for_router(item) for item in items]

    trace: list[dict[str, Any]] = [
        {
            "step": 0,
            "tool_name": "host_ingest_router",
            "arguments": {
                "project_key": _infer_project_key_from_prompt(prompt),
                "tag": _infer_tag_from_prompt(prompt),
                "source": loaded.get("source"),
                "no_write": _prompt_requests_no_write(prompt),
            },
            "reason": "Deterministic host-side routing for ingest/quarantine prompts; prevents wrong normal-memory tools.",
            "result": {"count": len(items), "decisions": decisions},
        }
    ]

    # Safe preview/classification path: return without giving the model a chance to choose a wrong tool.
    if _prompt_requests_no_write(prompt) or max_steps <= 1:
        if not decisions:
            answer = "Nie znalazłam kandydatów w ingest_queue dla podanych filtrów."
        else:
            lines = ["Host-router przejął zadanie ingestu i nie dopuścił zwykłego search_memories/list_conflicted_memories."]
            for d in decisions:
                lines.append(
                    f"{d['ingest_item_id']} {d['title']}: class={d['class']}, "
                    f"recommendation={d['recommendation']}, score={d['combined_score']}, reason={d['reason']}"
                )
            answer = "\n".join(lines)
        return {
            "status": "completed",
            "model": LM_STUDIO_MODEL,
            "steps": 0,
            "answer": answer,
            "reason": "host_ingest_router_preview",
            "trace": trace,
            "host_router_override": True,
        }

    # Conservative automatic action path. Only promote direct/agent-runtime memory candidates;
    # keep vector infrastructure in quarantine. This still records every action in trace.
    actions = []
    for d in decisions:
        item_id = int(d["ingest_item_id"])
        if d["recommendation"] == "promote_candidate" and d["class"] in {"direct_memory_layer", "agent_runtime_memory"}:
            item_result = _tool_get_ingest_item(conn, item_id)
            item = item_result.get("item") or {}
            content = (
                f"External AI memory market note: {d['title']} is classified as {d['class']}. "
                f"It is relevant to MAPI research ingest / long-term memory design because: {d['reason']}. "
                f"Source: {item.get('source_ref')}."
            )
            result = _tool_promote_ingest_item(
                conn,
                item_id,
                content,
                "research_note",
                d["title"][:160],
                "research-ingest,external-memory,sandman-promoted",
                0.62 if d["class"] == "direct_memory_layer" else 0.52,
                0.82,
                "sandman_host_router",
            )
            action = "promote_ingest_item"
        elif d["recommendation"] == "reject_candidate":
            result = _tool_reject_ingest_item(conn, item_id, d["reason"], "sandman_host_router")
            action = "reject_ingest_item"
        else:
            result = {"status": "kept_in_quarantine", "reason": d["reason"], "ingest_item_id": item_id}
            action = "keep_in_quarantine"
        actions.append({"ingest_item_id": item_id, "action": action, "result": result})

    trace.append({
        "step": 1,
        "tool_name": "host_ingest_router_actions",
        "arguments": {},
        "reason": "Conservative host-side ingest actions",
        "result": {"actions": actions},
    })
    return {
        "status": "completed",
        "model": LM_STUDIO_MODEL,
        "steps": 1,
        "answer": "Host-router wykonał konserwatywny ingest review: promuje tylko direct/agent-runtime memory candidates, a vector infrastructure zostawia w kwarantannie.",
        "reason": "host_ingest_router_actions",
        "trace": trace,
        "host_router_override": True,
    }


# ---------------------------------------------------------------------------
# AGENT LOOP
# ---------------------------------------------------------------------------

def run_memory_tool_agent(conn: sqlite3.Connection, *, user_query: str, max_steps: int = 4) -> dict[str, Any]:
    prompt = (user_query or "").strip()
    if not prompt:
        raise ValueError("user_query nie może być puste")
    max_steps = max(1, min(int(max_steps or 8), 16))
    allowed_write_tools = _write_tools_allowed_for_prompt(prompt)

    ingest_router_result = _maybe_handle_ingest_task_with_host_router(conn, user_query=prompt, max_steps=max_steps)
    if ingest_router_result is not None:
        return ingest_router_result

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "system", "content": _HOST_CONTEXT_PROMPT},
        {"role": "user", "content": prompt},
    ]
    trace: list[dict[str, Any]] = []
    write_tools = {"propose_memory", "archive_memory", "link_memories", "update_memory_importance", "reject_ingest_item", "archive_ingest_item", "promote_ingest_item"}

    for step in range(1, max_steps + 1):
        action = _normalize_action(_call_lm_studio(messages, max_tokens=2048, timeout=300))

        if action["kind"] == "final":
            return {
                "status": "completed",
                "model": LM_STUDIO_MODEL,
                "steps": step,
                "answer": action["answer"],
                "reason": action["reason"],
                "trace": trace,
            }

        if action["tool_name"] == "none":
            return {
                "status": "completed",
                "model": LM_STUDIO_MODEL,
                "steps": step,
                "answer": action["answer"] or "Nie wybrałam narzędzia ani odpowiedzi końcowej.",
                "reason": action["reason"],
                "trace": trace,
            }

        if action["tool_name"] in write_tools and action["tool_name"] not in allowed_write_tools:
            tool_result = _blocked_tool_result(action["tool_name"], prompt)
        else:
            tool_result = _run_tool(conn, action["tool_name"], action["arguments"])

        trace.append({
            "step": step,
            "tool_name": action["tool_name"],
            "arguments": action["arguments"],
            "reason": action["reason"],
            "result": tool_result,
        })

        if action["tool_name"] == "link_memories" and str(tool_result.get("status") or "") in {"created", "already_exists"}:
            return _auto_final_after_link(tool_result, step=step, trace=trace)

        messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
        messages.append({
            "role": "user",
            "content": "Wynik narzędzia: " + json.dumps(
                {"tool_name": action["tool_name"], "result": tool_result},
                ensure_ascii=False,
            ),
        })

    messages.append({
        "role": "user",
        "content": "To ostatni krok. Nie wołaj już narzędzi. Zwróć kind='final' i odpowiedź dla użytkownika na podstawie zebranych danych.",
    })
    final_action = _normalize_action(_call_lm_studio(messages, max_tokens=2048, timeout=300))
    return {
        "status": "completed",
        "model": LM_STUDIO_MODEL,
        "steps": max_steps,
        "answer": final_action.get("answer") or "Nie udało się zbudować odpowiedzi końcowej.",
        "reason": final_action.get("reason", "forced_final"),
        "trace": trace,
    }
