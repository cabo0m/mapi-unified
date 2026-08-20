from __future__ import annotations

"""Canonical current-state projection for memory retrieval and project surfaces."""

from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any, Callable, Iterable

CURRENT_STATE_SCHEMA = "memory_current_state.v1"
CURRENT_STATE_INVENTORY_SCHEMA = "memory_current_state_inventory.v1"
FULL_RELATIONS = frozenset({"supersedes"})
PARTIAL_RELATIONS = frozenset({"refines", "partially_supersedes"})
LINEAGE_RELATIONS = FULL_RELATIONS | PARTIAL_RELATIONS
HISTORICAL_STATES = frozenset({"superseded", "archived", "expired", "rejected", "cancelled"})
DECISIVE_TRUTH_KINDS = frozenset({"fact", "decision", "preference"})
DECISIVE_MEMORY_TYPES = frozenset({"project_decision", "project_state", "operator_preference", "continuity"})
QUESTION_TYPES = frozenset({"open_question", "question", "project_question"})
QUESTION_TAGS = frozenset({
    "open-question", "open_question", "question", "do-ustalenia", "needs-decision",
    "blocked", "blocker", "pending", "decision-review", "review-required", "needs-review",
})
RESOLUTION_TAGS = frozenset({
    "resolved", "completed", "complete", "ready", "accepted", "executed", "fixed", "closed", "freeze",
})
GENERIC_TOPIC_TAGS = frozenset({
    "mapi", "mapi", "memory-v3", "project", "fact", "decision", "next-step",
    "operator-preference", "workflow", "verified", "audit",
})


def _dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _tags(item: dict[str, Any]) -> set[str]:
    value = item.get("tags")
    if isinstance(value, str):
        return {_text(part) for part in value.split(",") if _text(part)}
    if isinstance(value, (list, tuple, set)):
        return {_text(part) for part in value if _text(part)}
    return set()


def _state(item: dict[str, Any]) -> str:
    for key in ("state_code", "memory_v2_status", "activity_state", "status"):
        normalized = _text(item.get(key))
        if normalized:
            return normalized
    return "active"


def _is_historical(item: dict[str, Any]) -> bool:
    if _state(item) in HISTORICAL_STATES:
        return True
    raw_valid_to = str(item.get("valid_to") or "").strip()
    if not raw_valid_to:
        return False
    try:
        valid_to = datetime.fromisoformat(raw_valid_to.replace("Z", "+00:00"))
        if valid_to.tzinfo is None:
            valid_to = valid_to.replace(tzinfo=UTC)
        return valid_to <= datetime.now(UTC)
    except ValueError:
        return True


def _is_question(item: dict[str, Any]) -> bool:
    memory_type = _text(item.get("memory_type"))
    truth_kind = _text(item.get("truth_kind"))
    blob = " ".join(
        filter(
            None,
            (
                _text(item.get("title")),
                _text(item.get("summary_short")),
                _text(item.get("content")),
            ),
        )
    )
    return (
        memory_type in QUESTION_TYPES
        or truth_kind in {"proposal", "question"}
        or bool(_tags(item) & QUESTION_TAGS)
        or "open question" in blob
        or "pytanie otwarte" in blob
        or "do ustalenia" in blob
        or "explicit authorization still required" in blob
        or "wymaga decyzji" in blob
        or "pozostaje zablokowany" in blob
    )


def _is_decisive(item: dict[str, Any]) -> bool:
    return _text(item.get("truth_kind")) in DECISIVE_TRUTH_KINDS or _text(item.get("memory_type")) in DECISIVE_MEMORY_TYPES




def _topic_tags(item: dict[str, Any]) -> set[str]:
    return {tag for tag in _tags(item) if tag not in GENERIC_TOPIC_TAGS and len(tag) >= 3}




def _load_resolution_candidates(conn: Any, memories: dict[int, dict[str, Any]]) -> None:
    questions = [item for item in memories.values() if _is_question(item) and not _is_historical(item)]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in questions:
        grouped[(_text(item.get("project_key")), _text(item.get("scope_code")))].append(item)
    for (project_key, scope_code), items in grouped.items():
        if not project_key:
            continue
        minimum_created = min(str(item.get("created_at") or "") for item in items)
        params: list[Any] = [project_key, minimum_created]
        scope_sql = ""
        if scope_code:
            scope_sql = " AND (scope_code = ? OR scope_code IS NULL OR trim(scope_code) = '')"
            params.append(scope_code)
        rows = conn.execute(
            f"""
            SELECT * FROM memories
            WHERE project_key = ?
              AND COALESCE(created_at, '') >= ?
              {scope_sql}
              AND COALESCE(state_code, 'active') NOT IN ('superseded','archived','expired','rejected','cancelled')
              AND COALESCE(memory_v2_status, 'active') NOT IN ('superseded','archived','expired','rejected','cancelled')
            ORDER BY created_at DESC, id DESC
            LIMIT 200
            """,
            params,
        ).fetchall()
        for row in rows:
            candidate = _dict(row)
            if _is_decisive(candidate):
                memories[int(candidate["id"])] = candidate

def _infer_resolution_edges(memories: dict[int, dict[str, Any]]) -> dict[int, int]:
    unresolved = [item for item in memories.values() if _is_question(item) and not _is_historical(item)]
    decisive = [
        item
        for item in memories.values()
        if _is_decisive(item) and not _is_historical(item) and bool(_tags(item) & RESOLUTION_TAGS)
    ]
    resolved: dict[int, int] = {}
    for old in unresolved:
        old_id = int(old.get("id") or 0)
        old_tags = _topic_tags(old)
        old_created = str(old.get("created_at") or "")
        candidates: list[tuple[int, tuple[int, str, int], dict[str, Any]]] = []
        for new in decisive:
            new_id = int(new.get("id") or 0)
            if not new_id or new_id == old_id or not _same_domain(new, old):
                continue
            new_created = str(new.get("created_at") or "")
            if (new_created, new_id) <= (old_created, old_id):
                continue
            overlap = old_tags & _topic_tags(new)
            if len(overlap) < 2:
                continue
            candidates.append((len(overlap), _rank(new), new))
        if candidates:
            candidates.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
            resolved[old_id] = int(candidates[0][2]["id"])
    return resolved

def _rank(item: dict[str, Any]) -> tuple[int, str, int]:
    return (
        0 if _is_historical(item) else 1,
        str(item.get("updated_at") or item.get("created_at") or ""),
        int(item.get("id") or 0),
    )


def _same_domain(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_project = _text(left.get("project_key"))
    right_project = _text(right.get("project_key"))
    left_scope = _text(left.get("scope_code"))
    right_scope = _text(right.get("scope_code"))
    project_ok = not left_project or not right_project or left_project == right_project
    scope_ok = not left_scope or not right_scope or left_scope == right_scope
    return project_ok and scope_ok


def _fetch_memories(conn: Any, ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    normalized = sorted({int(value) for value in ids if int(value) > 0})
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    rows = conn.execute(f"SELECT * FROM memories WHERE id IN ({placeholders})", normalized).fetchall()
    return {int(row["id"]): _dict(row) for row in rows}


def _expand_lineage(conn: Any, seed_ids: Iterable[int], *, max_nodes: int = 500) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    pending = deque(sorted({int(value) for value in seed_ids if int(value) > 0}))
    memories: dict[int, dict[str, Any]] = {}
    links_by_id: dict[int, dict[str, Any]] = {}
    rounds = 0
    while pending and len(memories) < max_nodes and rounds < 12:
        rounds += 1
        batch: list[int] = []
        while pending and len(batch) < 100:
            memory_id = pending.popleft()
            if memory_id not in memories:
                batch.append(memory_id)
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT * FROM memories
            WHERE id IN ({placeholders})
               OR supersedes_memory_id IN ({placeholders})
               OR superseded_by_memory_id IN ({placeholders})
            """,
            batch * 3,
        ).fetchall()
        for row in rows:
            item = _dict(row)
            memory_id = int(item["id"])
            if memory_id not in memories:
                memories[memory_id] = item
                pending.append(memory_id)
            for key in ("supersedes_memory_id", "superseded_by_memory_id"):
                related = item.get(key)
                if related is not None and int(related) not in memories:
                    pending.append(int(related))
        known_ids = sorted(set(memories) | set(batch))
        if not known_ids:
            continue
        placeholders = ",".join("?" for _ in known_ids)
        link_rows = conn.execute(
            f"""
            SELECT * FROM memory_links
            WHERE relation_type IN ('supersedes','refines','partially_supersedes')
              AND archived_at IS NULL
              AND (from_memory_id IN ({placeholders}) OR to_memory_id IN ({placeholders}))
            ORDER BY id ASC
            """,
            known_ids * 2,
        ).fetchall()
        for row in link_rows:
            link = _dict(row)
            links_by_id[int(link["id"])] = link
            for key in ("from_memory_id", "to_memory_id"):
                related = int(link[key])
                if related not in memories:
                    pending.append(related)
    missing_ids: set[int] = set()
    for link in links_by_id.values():
        missing_ids.update({int(link["from_memory_id"]), int(link["to_memory_id"])})
    memories.update({key: value for key, value in _fetch_memories(conn, missing_ids).items() if key not in memories})
    return memories, list(links_by_id.values())


def _build_edges(
    memories: dict[int, dict[str, Any]],
    links: list[dict[str, Any]],
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]], list[dict[str, Any]]]:
    full_by_old: dict[int, list[dict[str, Any]]] = defaultdict(list)
    partial_by_old: dict[int, list[dict[str, Any]]] = defaultdict(list)
    issues: list[dict[str, Any]] = []
    explicit_pairs: dict[tuple[int, int], str] = {}
    for link in links:
        new_id = int(link["from_memory_id"])
        old_id = int(link["to_memory_id"])
        relation = _text(link.get("relation_type"))
        new = memories.get(new_id)
        old = memories.get(old_id)
        if new is None or old is None:
            issues.append({"issue_code": "lineage_link_missing_memory", "new_memory_id": new_id, "old_memory_id": old_id})
            continue
        if not _same_domain(new, old):
            issues.append({"issue_code": "cross_domain_lineage_ignored", "new_memory_id": new_id, "old_memory_id": old_id, "relation": relation})
            continue
        explicit_pairs[(new_id, old_id)] = relation
        edge = {"new_memory_id": new_id, "old_memory_id": old_id, "relation": relation, "evidence": "link"}
        (full_by_old if relation in FULL_RELATIONS else partial_by_old)[old_id].append(edge)

    for new_id, new in memories.items():
        old_raw = new.get("supersedes_memory_id")
        if old_raw is None:
            continue
        old_id = int(old_raw)
        old = memories.get(old_id)
        if old is None:
            issues.append({"issue_code": "supersedes_missing_target", "new_memory_id": new_id, "old_memory_id": old_id})
            continue
        if not _same_domain(new, old):
            issues.append({"issue_code": "cross_domain_pointer_ignored", "new_memory_id": new_id, "old_memory_id": old_id})
            continue
        explicit_relation = explicit_pairs.get((new_id, old_id))
        if explicit_relation in PARTIAL_RELATIONS:
            continue
        edge = {
            "new_memory_id": new_id,
            "old_memory_id": old_id,
            "relation": "supersedes",
            "evidence": "pointer" if explicit_relation is None else "pointer+link",
        }
        if not any(existing["new_memory_id"] == new_id for existing in full_by_old[old_id]):
            full_by_old[old_id].append(edge)
        reverse_ok = int(old.get("superseded_by_memory_id") or 0) == new_id
        state_ok = _state(old) == "superseded"
        link_ok = explicit_relation == "supersedes"
        if not (reverse_ok and state_ok and link_ok):
            issues.append(
                {
                    "issue_code": "half_supersession",
                    "new_memory_id": new_id,
                    "old_memory_id": old_id,
                    "reverse_pointer_ok": reverse_ok,
                    "old_state_ok": state_ok,
                    "link_ok": link_ok,
                }
            )

    for old_id, old in memories.items():
        child_raw = old.get("superseded_by_memory_id")
        if child_raw is None:
            continue
        new_id = int(child_raw)
        new = memories.get(new_id)
        if new is None:
            issues.append({"issue_code": "superseded_by_missing_target", "new_memory_id": new_id, "old_memory_id": old_id})
            continue
        if not _same_domain(new, old):
            continue
        if not any(existing["new_memory_id"] == new_id for existing in full_by_old[old_id]):
            full_by_old[old_id].append(
                {"new_memory_id": new_id, "old_memory_id": old_id, "relation": "supersedes", "evidence": "reverse_pointer"}
            )
    return full_by_old, partial_by_old, issues


def _head_for(
    memory_id: int,
    memories: dict[int, dict[str, Any]],
    full_by_old: dict[int, list[dict[str, Any]]],
) -> tuple[int, list[int], list[dict[str, Any]]]:
    current = int(memory_id)
    lineage = [current]
    issues: list[dict[str, Any]] = []
    visited = {current}
    while full_by_old.get(current):
        candidates = [edge for edge in full_by_old[current] if edge["new_memory_id"] in memories]
        if not candidates:
            break
        candidates.sort(key=lambda edge: _rank(memories[edge["new_memory_id"]]), reverse=True)
        if len({edge["new_memory_id"] for edge in candidates}) > 1:
            issues.append(
                {
                    "issue_code": "multiple_replacement_heads",
                    "old_memory_id": current,
                    "candidate_memory_ids": sorted({edge["new_memory_id"] for edge in candidates}),
                }
            )
        next_id = int(candidates[0]["new_memory_id"])
        if next_id in visited:
            issues.append({"issue_code": "lineage_cycle", "memory_ids": lineage + [next_id]})
            break
        current = next_id
        lineage.append(current)
        visited.add(current)
    return current, lineage, issues




def _issue_severity(issue_code: str) -> str:
    if issue_code in {"lineage_cycle", "multiple_replacement_heads", "supersedes_missing_target", "superseded_by_missing_target", "lineage_link_missing_memory"}:
        return "critical"
    if issue_code in {"half_supersession", "cross_domain_pointer_ignored"}:
        return "warning"
    return "info"


def _canonical_lineages(lineages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_head: dict[int, dict[str, Any]] = {}
    for lineage in lineages:
        head_id = int(lineage.get("current_memory_id") or 0)
        existing = by_head.get(head_id)
        if existing is None or len(lineage.get("memory_ids") or []) > len(existing.get("memory_ids") or []):
            by_head[head_id] = lineage
    return sorted(by_head.values(), key=lambda item: (int(item.get("current_memory_id") or 0), len(item.get("memory_ids") or [])))

def resolve_current_memory_state(
    conn: Any,
    items: Iterable[dict[str, Any]],
    *,
    include_history: bool = False,
) -> dict[str, Any]:
    seed_items = [dict(item) for item in items]
    seed_ids = [int(item.get("id") or 0) for item in seed_items if int(item.get("id") or 0) > 0]
    memories, links = _expand_lineage(conn, seed_ids)
    for item in seed_items:
        memory_id = int(item.get("id") or 0)
        if memory_id:
            memories[memory_id] = {**memories.get(memory_id, {}), **item}
    full_by_old, partial_by_old, issues = _build_edges(memories, links)
    _load_resolution_candidates(conn, memories)

    resolved_by: dict[int, int] = _infer_resolution_edges(memories)
    all_relation_old_ids = set(full_by_old) | set(partial_by_old)
    for old_id in all_relation_old_ids:
        edges = list(full_by_old.get(old_id, [])) + list(partial_by_old.get(old_id, []))
        old = memories.get(old_id)
        if old is None or not _is_question(old):
            continue
        candidates = [memories.get(int(edge["new_memory_id"])) for edge in edges]
        candidates = [candidate for candidate in candidates if candidate and _is_decisive(candidate) and not _is_historical(candidate)]
        if candidates:
            candidates.sort(key=_rank, reverse=True)
            resolved_by[old_id] = int(candidates[0]["id"])

    projected: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    seen: set[int] = set()
    matched_history: dict[int, list[int]] = defaultdict(list)
    lineages: list[dict[str, Any]] = []
    seen_lineages: set[tuple[int, ...]] = set()

    for seed in seed_items:
        seed_id = int(seed.get("id") or 0)
        if not seed_id:
            projected.append(seed)
            continue
        head_id, lineage, head_issues = _head_for(seed_id, memories, full_by_old)
        if head_id == seed_id and seed_id in resolved_by:
            head_id = int(resolved_by[seed_id])
            lineage = [seed_id, head_id]
        issues.extend(head_issues)
        lineage_key = tuple(lineage)
        if lineage_key not in seen_lineages:
            seen_lineages.add(lineage_key)
            lineages.append(
                {
                    "seed_memory_id": seed_id,
                    "current_memory_id": head_id,
                    "memory_ids": lineage,
                    "refinement_memory_ids": sorted(
                        {
                            int(edge["new_memory_id"])
                            for memory_id in lineage
                            for edge in partial_by_old.get(memory_id, [])
                        }
                    ),
                }
            )
        head = dict(memories.get(head_id) or seed)
        if head_id != seed_id:
            matched_history[head_id].append(seed_id)
            history.append({**seed, "current_state": {"status": "history", "current_memory_id": head_id, "lineage_ids": lineage}})
        elif _is_historical(seed):
            history.append({**seed, "current_state": {"status": "history", "current_memory_id": head_id, "lineage_ids": lineage}})
            if not include_history:
                continue
        if head_id in seen:
            continue
        seen.add(head_id)
        partial_children = [int(edge["new_memory_id"]) for edge in partial_by_old.get(head_id, [])]
        annotation = {
            "schema": CURRENT_STATE_SCHEMA,
            "status": "history" if _is_historical(head) else "current",
            "current_memory_id": head_id,
            "lineage_ids": lineage,
            "matched_history_ids": sorted(set(matched_history.get(head_id, []))),
            "refined_by_memory_ids": sorted(set(partial_children)),
            "resolved_by_memory_id": resolved_by.get(head_id),
        }
        projected.append({**head, "current_state": annotation})

    for index, item in enumerate(projected):
        memory_id = int(item.get("id") or 0)
        annotation = dict(item.get("current_state") or {})
        annotation["matched_history_ids"] = sorted(set(matched_history.get(memory_id, [])))
        annotation["resolved_by_memory_id"] = resolved_by.get(memory_id)
        projected[index] = {**item, "current_state": annotation}

    if include_history:
        for item in history:
            memory_id = int(item.get("id") or 0)
            if memory_id and memory_id not in seen:
                seen.add(memory_id)
                projected.append(item)

    for issue in issues:
        issue.setdefault("severity", _issue_severity(str(issue.get("issue_code") or "unknown")))
    lineages = _canonical_lineages(lineages)

    return {
        "schema": CURRENT_STATE_SCHEMA,
        "items": projected,
        "history": history,
        "issues": issues,
        "resolved_question_ids": sorted(resolved_by),
        "lineages": lineages,
        "counts": {
            "seed_items": len(seed_items),
            "returned_items": len(projected),
            "history_items": len(history),
            "issue_count": len(issues),
        },
    }


def get_memory_current_state_payload(
    conn: Any,
    *,
    memory_id: int,
    include_history: bool = True,
    include_debug: bool = False,
) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchall()
    if not rows:
        return {"status": "error", "error": "memory_not_found", "memory_id": int(memory_id)}
    projection = resolve_current_memory_state(conn, [_dict(rows[0])], include_history=include_history)
    result = {
        "status": "ok",
        "schema": CURRENT_STATE_SCHEMA,
        "memory_id": int(memory_id),
        "current": projection["items"][0] if projection["items"] else None,
        "history": projection["history"],
        "issues": projection["issues"],
        "resolved_question_ids": projection["resolved_question_ids"],
    }
    if include_debug:
        result["debug"] = {"counts": projection["counts"]}
    return result


def get_memory_current_state_inventory_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    limit: int = 200,
    include_debug: bool = False,
) -> dict[str, Any]:
    params: list[Any] = []
    project_filter = ""
    if _text(project_key):
        project_filter = " AND m.project_key = ?"
        params.append(str(project_key).strip())
    safe_limit = max(1, min(int(limit or 200), 1000))
    rows = conn.execute(
        f"""
        SELECT m.* FROM memories AS m
        WHERE (
            m.supersedes_memory_id IS NOT NULL
            OR m.superseded_by_memory_id IS NOT NULL
            OR m.state_code = 'superseded'
            OR m.memory_v2_status = 'superseded'
            OR m.id IN (
                SELECT supersedes_memory_id FROM memories WHERE supersedes_memory_id IS NOT NULL
                UNION
                SELECT superseded_by_memory_id FROM memories WHERE superseded_by_memory_id IS NOT NULL
                UNION
                SELECT from_memory_id FROM memory_links
                WHERE relation_type IN ('supersedes','refines','partially_supersedes')
                  AND archived_at IS NULL
                UNION
                SELECT to_memory_id FROM memory_links
                WHERE relation_type IN ('supersedes','refines','partially_supersedes')
                  AND archived_at IS NULL
            )
        ) {project_filter}
        ORDER BY m.id DESC
        LIMIT ?
        """,
        [*params, safe_limit],
    ).fetchall()
    items = [_dict(row) for row in rows]
    projection = resolve_current_memory_state(conn, items, include_history=True)
    issue_counts: dict[str, int] = defaultdict(int)
    severity_counts: dict[str, int] = defaultdict(int)
    for issue in projection["issues"]:
        issue_counts[str(issue.get("issue_code") or "unknown")] += 1
        severity_counts[str(issue.get("severity") or "info")] += 1
    result = {
        "status": "ok" if not projection["issues"] else "attention",
        "schema": CURRENT_STATE_INVENTORY_SCHEMA,
        "project_key": project_key,
        "summary": {
            "candidate_count": len(items),
            "history_count": len(projection["history"]),
            "issue_count": len(projection["issues"]),
            "critical_issue_count": int(severity_counts.get("critical") or 0),
            "warning_issue_count": int(severity_counts.get("warning") or 0),
            "info_issue_count": int(severity_counts.get("info") or 0),
            "issue_counts": dict(sorted(issue_counts.items())),
            "severity_counts": dict(sorted(severity_counts.items())),
        },
        "issues": projection["issues"],
        "lineages": projection["lineages"],
    }
    if include_debug:
        result["debug"] = {"candidate_memory_ids": [int(item["id"]) for item in items], "projection_counts": projection["counts"]}
    return result


def apply_direct_supersession_transition(
    conn: Any,
    *,
    new_memory_id: int,
    old_memory_id: int,
    relation: str = "supersedes",
    scope_note: str | None = None,
    now_iso: Callable[[], str] | None = None,
    insert_event: Callable[..., Any] | None = None,
    source: str = "operator_confirmed_direct",
) -> dict[str, Any]:
    normalized_relation = _text(relation) or "supersedes"
    if normalized_relation not in LINEAGE_RELATIONS:
        raise ValueError("supersession_relation must be supersedes, refines or partially_supersedes")
    if normalized_relation in PARTIAL_RELATIONS and not _text(scope_note):
        raise ValueError("supersession_scope is required for refines or partially_supersedes")
    rows = conn.execute("SELECT * FROM memories WHERE id IN (?, ?)", (int(new_memory_id), int(old_memory_id))).fetchall()
    memories = {int(row["id"]): _dict(row) for row in rows}
    new = memories.get(int(new_memory_id))
    old = memories.get(int(old_memory_id))
    if new is None or old is None:
        raise ValueError("supersession target memory not found")
    if int(new_memory_id) == int(old_memory_id):
        raise ValueError("memory cannot supersede itself")
    if not _same_domain(new, old):
        raise ValueError("supersession cannot cross project or scope boundaries")
    timestamp = (now_iso or (lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")))()

    existing_child = old.get("superseded_by_memory_id")
    if normalized_relation == "supersedes" and existing_child not in {None, int(new_memory_id)}:
        raise ValueError("superseded memory already points to another replacement")

    if normalized_relation == "supersedes":
        conn.execute("UPDATE memories SET supersedes_memory_id = ? WHERE id = ?", (int(old_memory_id), int(new_memory_id)))
        conn.execute(
            """
            UPDATE memories
            SET superseded_by_memory_id = ?, state_code = 'superseded', memory_v2_status = 'superseded',
                activity_state = 'superseded', valid_to = COALESCE(valid_to, ?), updated_at = ?
            WHERE id = ?
            """,
            (int(new_memory_id), timestamp, timestamp, int(old_memory_id)),
        )
    else:
        conn.execute("UPDATE memories SET supersedes_memory_id = NULL WHERE id = ?", (int(new_memory_id),))

    existing_link = conn.execute(
        """
        SELECT id FROM memory_links
        WHERE from_memory_id = ? AND to_memory_id = ? AND relation_type = ? AND archived_at IS NULL
        ORDER BY id LIMIT 1
        """,
        (int(new_memory_id), int(old_memory_id), normalized_relation),
    ).fetchone()
    if existing_link is None:
        conn.execute(
            """
            INSERT INTO memory_links (from_memory_id, to_memory_id, relation_type, weight, origin, created_at)
            VALUES (?, ?, ?, 1.0, ?, ?)
            """,
            (int(new_memory_id), int(old_memory_id), normalized_relation, source, timestamp),
        )

    if insert_event is not None:
        insert_event(
            conn,
            memory_id=int(new_memory_id),
            event_type="version.supersession_applied" if normalized_relation == "supersedes" else f"version.{normalized_relation}",
            payload={
                "old_memory_id": int(old_memory_id),
                "relation": normalized_relation,
                "scope_note": scope_note,
                "source": source,
            },
        )
        insert_event(
            conn,
            memory_id=int(old_memory_id),
            event_type="version.superseded" if normalized_relation == "supersedes" else f"version.{normalized_relation}_by",
            payload={
                "new_memory_id": int(new_memory_id),
                "relation": normalized_relation,
                "scope_note": scope_note,
                "source": source,
            },
        )
    return {
        "relation": normalized_relation,
        "new_memory_id": int(new_memory_id),
        "old_memory_id": int(old_memory_id),
        "scope_note": scope_note,
        "full_supersession": normalized_relation == "supersedes",
    }
