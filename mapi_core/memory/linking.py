"""Memory Linking Pass V1 / V1.1 — deterministic graph-link algorithm.

All private helpers are pure functions (stdlib only).  The two payload
functions accept external dependencies (sleep-run helpers, create_link,
row_to_dict) via keyword callables so the module has no import cycle with
server_core.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Generic / specific tag helpers
# ---------------------------------------------------------------------------

_GENERIC_TAGS: set[str] = {
    "project", "projekt", "project_status", "project_note", "project_context",
    "project_decision", "status", "done", "next-step", "todo", "wishlist",
    "pamiec", "pamięć", "pamiec-jagody", "agent", "jagody", "memory",
    "docs", "documentation", "cleanup", "success", "debug", "deploy",
    "runbook", "vps", "mcp", "api", "build-success",
}

_GENERIC_TOKENS: set[str] = {
    "oraz", "jest", "dla", "przez", "jako", "with", "from", "this", "that",
    "the", "and", "czy", "bez", "pod", "nad", "wraz", "into", "about", "memory",
    "wspomnienie", "wspomnien", "wspomnień", "projekt", "project", "status",
    "działa", "dodano", "trzeba", "aktualny", "kolejny", "następny", "next-step",
    "pamiec-jagody", "project_status", "project_context", "project_note",
}

_STOP_WORDS: set[str] = {
    "oraz", "jest", "dla", "przez", "jako", "with", "from", "this", "that",
    "the", "and", "czy", "bez", "pod", "nad", "wraz", "into", "about", "memory",
    "wspomnienie", "wspomnien", "wspomnień", "projekt", "project", "status",
}


def split_tags(raw: str | None) -> set[str]:
    text = (raw or "").replace(";", ",")
    return {item.strip().lower() for item in text.split(",") if item.strip()}


def tokenize(*parts: object) -> set[str]:
    text = " ".join(str(part or "") for part in parts).lower()
    return {
        token
        for token in re.findall(r"[a-zA-Z0-9_\-ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]{4,}", text)
        if token not in _STOP_WORDS
    }


def specific_tags(raw: str | None) -> set[str]:
    return {tag for tag in split_tags(raw) if tag not in _GENERIC_TAGS and len(tag) >= 3}


def specific_tokens(*parts: object) -> set[str]:
    return {
        t for t in tokenize(*parts)
        if t not in _GENERIC_TOKENS and t not in _GENERIC_TAGS
    }


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def log_squash(raw_score: float, *, resistance: float = 1.35, ceiling: float = 0.97) -> float:
    """Convert additive evidence score into a bounded confidence value."""
    raw = max(0.0, float(raw_score or 0.0))
    resistance = max(0.05, float(resistance or 1.35))
    ceiling = max(0.05, min(float(ceiling or 0.97), 0.999))
    normalized = math.log1p(raw) / (math.log1p(raw) + resistance) if raw > 0.0 else 0.0
    return round(min(ceiling, max(0.0, normalized)), 3)


def weight_from_score(score: float, *, link_class: str) -> float:
    if link_class == "semantic":
        return round(max(0.55, min(float(score), 0.93)), 2)
    return round(max(0.35, min(float(score), 0.68)), 2)


# ---------------------------------------------------------------------------
# Relation helpers
# ---------------------------------------------------------------------------

def has_requirement_implementation(a: dict[str, Any], b: dict[str, Any]) -> bool:
    a_type = str(a.get("memory_type") or "").lower()
    b_type = str(b.get("memory_type") or "").lower()
    a_text = f"{a.get('summary_short') or ''} {a.get('content') or ''}".lower()
    b_text = f"{b.get('summary_short') or ''} {b.get('content') or ''}".lower()
    requirement_types = {"project_requirement", "requirement"}
    impl_words = (
        "implement", "wdroż", "wdroz", "dodano", "napraw",
        "patched", "migration", "migrac", "status", "zrobion", "działa",
    )
    return (
        a_type in requirement_types and any(w in b_text for w in impl_words)
    ) or (
        b_type in requirement_types and any(w in a_text for w in impl_words)
    )


def memory_blob(memory: dict[str, Any]) -> str:
    return " ".join(
        str(memory.get(k) or "")
        for k in ("summary_short", "content", "memory_type", "tags", "project_key")
    ).lower()


def any_word(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def doc_like(memory: dict[str, Any]) -> bool:
    text = memory_blob(memory)
    mtype = str(memory.get("memory_type") or "").lower()
    return mtype in {"fact", "runbook", "documentation", "doc", "technical_note"} or any_word(
        text,
        ("runbook", "docs", "dokumentac", "readme", "checklist", "instrukcj", "wyjaśnienie", "wyjasnienie"),
    )


def status_relation(memory: dict[str, Any]) -> str | None:
    text = memory_blob(memory)
    mtype = str(memory.get("memory_type") or "").lower()
    if mtype not in {
        "project_status", "project_note", "project_context",
        "consolidated_summary", "fact",
    }:
        return None
    if any_word(text, ("wdroż", "wdroz", "dodano", "zaimplement", "implemented", "patched", "napraw", "zmigrow")):
        return "implements"
    if any_word(text, ("działa", "potwierdz", "success", "passed", "przeszed", "wykonan")):
        return "validates"
    return None


def domain_overlap(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    a_tags = specific_tags(a.get("tags"))
    b_tags = specific_tags(b.get("tags"))
    a_tokens = specific_tokens(a.get("summary_short"), a.get("tags"), a.get("memory_type"), a.get("project_key"))
    b_tokens = specific_tokens(b.get("summary_short"), b.get("tags"), b.get("memory_type"), b.get("project_key"))
    common_tags = sorted(a_tags & b_tags)
    common_tokens = sorted(a_tokens & b_tokens)
    return {
        "common_tags": common_tags,
        "common_tokens": common_tokens,
        "has_overlap": bool(common_tags or common_tokens),
    }


def relation_and_direction(
    a: dict[str, Any],
    b: dict[str, Any],
    reasons: list[str],
) -> tuple[int, int, str]:
    a_id, b_id = int(a["id"]), int(b["id"])
    a_type = str(a.get("memory_type") or "").lower()
    b_type = str(b.get("memory_type") or "").lower()

    if int(a.get("supersedes_memory_id") or 0) == b_id:
        return a_id, b_id, "supersedes"
    if int(b.get("supersedes_memory_id") or 0) == a_id:
        return b_id, a_id, "supersedes"
    if int(a.get("parent_memory_id") or 0) == b_id:
        return a_id, b_id, "context_for"
    if int(b.get("parent_memory_id") or 0) == a_id:
        return b_id, a_id, "context_for"

    decisions = {"project_decision", "project_requirement", "requirement"}
    a_status = status_relation(a)
    b_status = status_relation(b)
    overlap = domain_overlap(a, b)
    has_domain_overlap = bool(overlap["has_overlap"])
    has_req_impl_reason = has_requirement_implementation(a, b) or any(
        "requirement/implementation" in r for r in reasons
    )

    if has_req_impl_reason:
        if not has_domain_overlap:
            return a_id, b_id, "related_to"
        if a_type in decisions and b_type not in decisions:
            return b_id, a_id, b_status or "implements"
        if b_type in decisions and a_type not in decisions:
            return a_id, b_id, a_status or "implements"
        if a_status and not b_status:
            return a_id, b_id, a_status
        if b_status and not a_status:
            return b_id, a_id, b_status
        return a_id, b_id, "related_to"

    if a_status and b_type in decisions:
        return a_id, b_id, a_status
    if b_status and a_type in decisions:
        return b_id, a_id, b_status

    a_doc = doc_like(a)
    b_doc = doc_like(b)
    if a_doc and not b_doc:
        return a_id, b_id, "documents"
    if b_doc and not a_doc:
        return b_id, a_id, "documents"

    if a_type.startswith("personal") or b_type.startswith("personal"):
        return a_id, b_id, "related_to"
    if "bootstrap/core" in reasons:
        return a_id, b_id, "context_for"
    if any(r.startswith("semantic") or r.startswith("strong_semantic") for r in reasons):
        return a_id, b_id, "related_to"
    return a_id, b_id, "same_project"


# ---------------------------------------------------------------------------
# Link-existence check
# ---------------------------------------------------------------------------

def link_exists_any_direction(
    conn: Any,
    a_id: int,
    b_id: int,
    relation_type: str | None = None,
) -> bool:
    if relation_type:
        row = conn.execute(
            """
            SELECT id FROM memory_links
            WHERE archived_at IS NULL
              AND relation_type = ?
              AND ((from_memory_id = ? AND to_memory_id = ?)
                   OR (from_memory_id = ? AND to_memory_id = ?))
            LIMIT 1
            """,
            (relation_type, a_id, b_id, b_id, a_id),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id FROM memory_links
            WHERE archived_at IS NULL
              AND ((from_memory_id = ? AND to_memory_id = ?)
                   OR (from_memory_id = ? AND to_memory_id = ?))
            LIMIT 1
            """,
            (a_id, b_id, b_id, a_id),
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Candidate collection (V1.1 scoring)
# ---------------------------------------------------------------------------

def get_candidates(
    conn: Any,
    *,
    project_key: str | None = None,
    limit: int = 100,
    max_links_per_memory: int = 4,
    min_score: float = 0.45,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> list[dict[str, Any]]:
    """V1.1 two-track scoring: semantic vs project_neighborhood."""
    limit = max(1, min(int(limit or 100), 500))
    max_links_per_memory = max(1, min(int(max_links_per_memory or 4), 20))
    min_score = max(0.05, min(float(min_score or 0.45), 1.0))

    params: list[Any] = []
    where = "COALESCE(activity_state, 'active') = 'active'"
    if project_key:
        where += " AND project_key = ?"
        params.append(project_key)

    rows = conn.execute(
        f"""
        SELECT id, summary_short, content, memory_type, project_key, tags,
               importance_score, parent_memory_id, supersedes_memory_id, created_at
        FROM memories
        WHERE {where}
        ORDER BY COALESCE(importance_score, 0) DESC, id DESC
        LIMIT 450
        """,
        tuple(params),
    ).fetchall()
    memories = [row_to_dict(row) for row in rows]

    active_link_counts: dict[int, int] = {}
    for row in conn.execute(
        """
        SELECT memory_id, COUNT(*) AS link_count FROM (
            SELECT from_memory_id AS memory_id FROM memory_links WHERE archived_at IS NULL
            UNION ALL
            SELECT to_memory_id AS memory_id FROM memory_links WHERE archived_at IS NULL
        ) GROUP BY memory_id
        """
    ).fetchall():
        active_link_counts[int(row["memory_id"])] = int(row["link_count"] or 0)

    candidates: list[dict[str, Any]] = []
    candidate_counts: dict[int, int] = {}

    for idx, a in enumerate(memories):
        a_id = int(a["id"])
        if active_link_counts.get(a_id, 0) >= max_links_per_memory:
            continue
        a_tags = specific_tags(a.get("tags"))
        a_tokens = specific_tokens(
            a.get("summary_short"), a.get("tags"),
            a.get("memory_type"), a.get("project_key"),
        )
        for b in memories[idx + 1:]:
            b_id = int(b["id"])
            if a_id == b_id:
                continue
            if active_link_counts.get(b_id, 0) >= max_links_per_memory:
                continue
            if (
                candidate_counts.get(a_id, 0) >= max_links_per_memory
                or candidate_counts.get(b_id, 0) >= max_links_per_memory
            ):
                continue
            if link_exists_any_direction(conn, a_id, b_id):
                continue

            same_project = bool(
                a.get("project_key") and a.get("project_key") == b.get("project_key")
            )
            b_tags = specific_tags(b.get("tags"))
            b_tokens = specific_tokens(
                b.get("summary_short"), b.get("tags"),
                b.get("memory_type"), b.get("project_key"),
            )
            common_tags = sorted(a_tags & b_tags)
            common_tokens = sorted(a_tokens & b_tokens)

            semantic_raw = 0.0
            neighborhood_raw = 0.0
            reasons: list[str] = []

            if same_project:
                neighborhood_raw += 0.35
                reasons.append("same project")

            if common_tags:
                semantic_raw += 0.28 * len(common_tags)
                neighborhood_raw += 0.08 * len(common_tags)
                reasons.append("semantic shared tags: " + ", ".join(common_tags[:5]))
            if common_tokens:
                semantic_raw += 0.14 * len(common_tokens)
                neighborhood_raw += 0.04 * len(common_tokens)
                reasons.append("semantic shared tokens: " + ", ".join(common_tokens[:5]))

            if (
                int(a.get("parent_memory_id") or 0) == b_id
                or int(b.get("parent_memory_id") or 0) == a_id
            ):
                semantic_raw += 1.35
                reasons.append("strong_semantic parent/child")
            if (
                int(a.get("supersedes_memory_id") or 0) == b_id
                or int(b.get("supersedes_memory_id") or 0) == a_id
            ):
                semantic_raw += 1.55
                reasons.append("strong_semantic supersedes lineage")
            if has_requirement_implementation(a, b):
                semantic_raw += 1.25
                reasons.append("strong_semantic requirement/implementation")

            blob = " ".join(
                str(x or "")
                for x in [
                    a.get("summary_short"), a.get("content"),
                    b.get("summary_short"), b.get("content"),
                    a.get("tags"), b.get("tags"),
                ]
            ).lower()
            has_schema = any_word(blob, ("schema", "migration", "migrac", "migracja"))
            has_timeline = any_word(blob, ("timeline", "valid_at", "event", "oś", "osi"))
            has_bootstrap = any_word(blob, ("bootstrap", "identity", "tożsamo", "tozsamo", "core"))
            has_semantic_overlap = semantic_raw >= 0.42 or len(common_tags) >= 2 or len(common_tokens) >= 3

            if has_schema and has_semantic_overlap:
                semantic_raw += 0.35
                reasons.append("schema/migration")
            elif has_schema and same_project:
                neighborhood_raw += 0.12
                reasons.append("project-neighborhood schema/migration")
            if has_timeline and has_semantic_overlap:
                semantic_raw += 0.28
                reasons.append("timeline")
            elif has_timeline and same_project:
                neighborhood_raw += 0.08
                reasons.append("project-neighborhood timeline")
            if has_bootstrap and has_semantic_overlap:
                semantic_raw += 0.30
                reasons.append("bootstrap/core")
            elif has_bootstrap and same_project:
                neighborhood_raw += 0.10
                reasons.append("project-neighborhood bootstrap/core")

            semantic_score = log_squash(semantic_raw, resistance=1.20, ceiling=0.97)
            neighborhood_score = log_squash(neighborhood_raw, resistance=1.65, ceiling=0.72)

            if semantic_score >= min_score:
                link_class = "semantic"
                score = semantic_score
                wt = weight_from_score(score, link_class=link_class)
            elif same_project and neighborhood_score >= max(0.42, min_score - 0.22):
                link_class = "project_neighborhood"
                score = neighborhood_score
                wt = weight_from_score(score, link_class=link_class)
            else:
                continue

            from_id, to_id, relation_type = relation_and_direction(
                a, b,
                reasons if link_class == "semantic" else ["project_neighborhood"],
            )
            if link_class == "project_neighborhood":
                relation_type = "same_project"
            if link_exists_any_direction(conn, from_id, to_id, relation_type):
                continue

            candidate = {
                "from_memory_id": from_id,
                "to_memory_id": to_id,
                "relation_type": relation_type,
                "weight": wt,
                "score": round(score, 3),
                "semantic_score": round(semantic_score, 3),
                "neighborhood_score": round(neighborhood_score, 3),
                "semantic_raw": round(semantic_raw, 3),
                "neighborhood_raw": round(neighborhood_raw, 3),
                "score_model": "log_squash_v1",
                "link_class": link_class,
                "reasons": reasons[:8],
                "from_summary": a.get("summary_short") if from_id == a_id else b.get("summary_short"),
                "to_summary": b.get("summary_short") if to_id == b_id else a.get("summary_short"),
            }
            candidates.append(candidate)
            candidate_counts[a_id] = candidate_counts.get(a_id, 0) + 1
            candidate_counts[b_id] = candidate_counts.get(b_id, 0) + 1

    candidates.sort(
        key=lambda item: (
            1 if item.get("link_class") == "semantic" else 0,
            float(item.get("semantic_score") or 0),
            float(item.get("score") or 0),
            float(item.get("weight") or 0),
        ),
        reverse=True,
    )
    return candidates[:limit]


# ---------------------------------------------------------------------------
# Payload functions (called by MCP tool wrappers in server_core.py)
# ---------------------------------------------------------------------------

def preview_memory_linking_pass_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    limit: int = 50,
    max_links_per_memory: int = 4,
    min_score: float = 0.45,
    notes: str | None = None,
    row_to_dict: Callable[[Any], dict[str, Any]],
    create_sleep_run: Callable[..., int],
    add_sleep_action: Callable[..., None],
    finalize_sleep_run: Callable[..., None],
) -> dict[str, Any]:
    candidates = get_candidates(
        conn,
        project_key=project_key,
        limit=limit,
        max_links_per_memory=max_links_per_memory,
        min_score=min_score,
        row_to_dict=row_to_dict,
    )
    run_id = create_sleep_run(conn, mode="memory_linking_preview", freedom_level=0, notes=notes)
    scanned_count = conn.execute(
        "SELECT COUNT(*) AS count FROM memories WHERE COALESCE(activity_state, 'active') = 'active'"
    ).fetchone()["count"]
    for candidate in candidates:
        add_sleep_action(
            conn,
            run_id,
            "memory_link_candidate",
            int(candidate["from_memory_id"]),
            None,
            candidate,
            "memory_linking_pass_v1_preview",
        )
    finalize_sleep_run(
        conn, run_id,
        status="preview_completed",
        scanned_count=int(scanned_count),
        changed_count=0, archived_count=0, downgraded_count=0,
        duplicate_count=0, conflict_count=0, created_summary_count=0,
    )
    return {
        "status": "preview_completed",
        "run_id": run_id,
        "project_key": project_key,
        "scanned_count": int(scanned_count),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "summary": {
            "candidate_count": len(candidates),
            "relation_type_counts": {
                rtype: sum(1 for item in candidates if item["relation_type"] == rtype)
                for rtype in sorted({item["relation_type"] for item in candidates})
            },
        },
    }


def run_memory_linking_pass_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    limit: int = 50,
    max_links_per_memory: int = 4,
    min_score: float = 0.45,
    notes: str | None = None,
    row_to_dict: Callable[[Any], dict[str, Any]],
    create_sleep_run: Callable[..., int],
    add_sleep_action: Callable[..., None],
    finalize_sleep_run: Callable[..., None],
    create_link: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    candidates = get_candidates(
        conn,
        project_key=project_key,
        limit=limit,
        max_links_per_memory=max_links_per_memory,
        min_score=min_score,
        row_to_dict=row_to_dict,
    )
    run_id = create_sleep_run(conn, mode="memory_linking_run", freedom_level=0, notes=notes)
    scanned_count = conn.execute(
        "SELECT COUNT(*) AS count FROM memories WHERE COALESCE(activity_state, 'active') = 'active'"
    ).fetchone()["count"]
    links_created: list[dict[str, Any]] = []
    skipped_existing: list[dict[str, Any]] = []
    origin = "memory_linking_pass_v1"

    for candidate in candidates:
        from_id = int(candidate["from_memory_id"])
        to_id = int(candidate["to_memory_id"])
        relation_type = str(candidate["relation_type"])
        if link_exists_any_direction(conn, from_id, to_id, relation_type):
            skipped_existing.append(candidate)
            continue
        item = create_link(conn, from_id, to_id, relation_type, float(candidate["weight"]), origin)
        item["score"] = candidate["score"]
        item["reasons"] = candidate["reasons"]
        links_created.append(item)
        add_sleep_action(
            conn,
            run_id,
            "memory_link_created",
            from_id,
            None,
            {**candidate, "link_id": item["id"], "origin": origin},
            "memory_linking_pass_v1",
        )

    finalize_sleep_run(
        conn, run_id,
        status="completed",
        scanned_count=int(scanned_count),
        changed_count=len(links_created),
        archived_count=0, downgraded_count=0,
        duplicate_count=0, conflict_count=0, created_summary_count=0,
    )
    return {
        "status": "completed",
        "run_id": run_id,
        "project_key": project_key,
        "scanned_count": int(scanned_count),
        "links_created": links_created,
        "skipped_existing_count": len(skipped_existing),
        "summary": {
            "created_count": len(links_created),
            "candidate_count": len(candidates),
            "relation_type_counts": {
                rtype: sum(1 for item in links_created if item["relation_type"] == rtype)
                for rtype in sorted({item["relation_type"] for item in links_created})
            },
            "origin": origin,
        },
    }
