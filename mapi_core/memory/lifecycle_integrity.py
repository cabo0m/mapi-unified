from __future__ import annotations

from collections import Counter, defaultdict, deque
from typing import Any, Callable, Iterable

from mapi_core.memory.lifecycle_contracts import (
    MEMORY_V3_HASH_ALGORITHM,
    derive_canonical_memory_state,
    project_memory_v2_status,
)
from mapi_core.schemas import normalize_optional_text


LIFECYCLE_INTEGRITY_SCHEMA_VERSION = "memory_v3_lifecycle_integrity_report.v1"

CRITICAL_LIFECYCLE_ISSUE_CODES = frozenset(
    {
        "supersedes_missing_target",
        "superseded_by_missing_target",
        "reverse_pointer_mismatch",
        "supersession_cycle",
        "multiple_active_heads",
        "cross_project_supersession",
        "cross_scope_supersession",
        "active_with_superseded_by",
        "supersedes_link_field_mismatch",
        "unknown_state_code",
    }
)

PREVIEW_BLOCKING_LIFECYCLE_ISSUE_CODES = CRITICAL_LIFECYCLE_ISSUE_CODES | frozenset(
    {
        "supersession_branch",
        "superseded_without_lineage",
    }
)

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


def _make_finding(
    issue_code: str,
    *,
    severity: str,
    memory_ids: Iterable[int],
    reason: str,
    operator_next_action: str,
    project_key: str | None = None,
    scope_code: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "issue_code": issue_code,
        "severity": severity,
        "memory_ids": sorted({int(memory_id) for memory_id in memory_ids}),
        "project_key": normalize_optional_text(project_key),
        "scope_code": normalize_optional_text(scope_code),
        "reason": reason,
        "operator_next_action": operator_next_action,
        **extra,
    }


def _memory_scope_key(memory: dict[str, Any]) -> tuple[str | None, str | None]:
    return (
        normalize_optional_text(memory.get("project_key")),
        normalize_optional_text(memory.get("scope_code")),
    )


def _memory_truth_kind(memory: dict[str, Any]) -> str | None:
    return normalize_optional_text(memory.get("truth_kind"))


def _memory_activity_state(memory: dict[str, Any]) -> str | None:
    return normalize_optional_text(memory.get("_raw", {}).get("activity_state")) or normalize_optional_text(memory.get("activity_state"))


def _memory_state_code(memory: dict[str, Any]) -> str | None:
    return normalize_optional_text(memory.get("_raw", {}).get("state_code")) or normalize_optional_text(memory.get("state_code"))


def _memory_v2_status(memory: dict[str, Any]) -> str | None:
    return normalize_optional_text(memory.get("_raw", {}).get("memory_v2_status")) or normalize_optional_text(memory.get("memory_v2_status"))


def _fetch_memory_row(
    conn: Any,
    memory_id: int,
    *,
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    if row is None:
        return None
    raw = row_to_dict(row)
    enriched = enrich_memory_dict(raw)
    enriched["_raw"] = raw
    return enriched


def load_lifecycle_graph(
    conn: Any,
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
    memory_id: int | None = None,
    include_archived: bool = True,
    limit: int = 100,
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    if memory_id is not None:
        base_row = _fetch_memory_row(
            conn,
            int(memory_id),
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
        base_rows = [] if base_row is None else [base_row]
    else:
        clauses: list[str] = []
        params: list[Any] = []
        normalized_project_key = normalize_optional_text(project_key)
        normalized_scope_code = normalize_optional_text(scope_code)
        if normalized_project_key is not None:
            clauses.append("project_key = ?")
            params.append(normalized_project_key)
        if normalized_scope_code is not None:
            clauses.append("scope_code = ?")
            params.append(normalized_scope_code)
        if not include_archived:
            clauses.append("COALESCE(activity_state, 'active') != 'archived'")
        sql = "SELECT * FROM memories"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(int(limit))
        base_rows = []
        for row in conn.execute(sql, params).fetchall():
            raw = row_to_dict(row)
            enriched = enrich_memory_dict(raw)
            enriched["_raw"] = raw
            base_rows.append(enriched)

    base_ids = [int(item["id"]) for item in base_rows]
    queue: deque[int] = deque(base_ids)
    visited: set[int] = set()
    memories_by_id: dict[int, dict[str, Any]] = {}

    while queue:
        current_id = int(queue.popleft())
        if current_id in visited:
            continue
        visited.add(current_id)
        current = _fetch_memory_row(
            conn,
            current_id,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
        if current is None:
            continue
        memories_by_id[current_id] = current

        parent_id = current.get("supersedes_memory_id")
        if parent_id is not None:
            queue.append(int(parent_id))
        forward_id = current.get("superseded_by_memory_id")
        if forward_id is not None:
            queue.append(int(forward_id))
        child_rows = conn.execute(
            "SELECT id FROM memories WHERE supersedes_memory_id = ? ORDER BY id ASC",
            (current_id,),
        ).fetchall()
        for child_row in child_rows:
            queue.append(int(child_row["id"]))

    graph_ids = sorted(memories_by_id)
    link_rows: list[dict[str, Any]] = []
    if graph_ids:
        placeholders = ", ".join("?" for _ in graph_ids)
        sql = (
            "SELECT * FROM memory_links "
            "WHERE relation_type = 'supersedes' "
            f"AND (from_memory_id IN ({placeholders}) OR to_memory_id IN ({placeholders})) "
            "ORDER BY id ASC"
        )
        params = graph_ids + graph_ids
        link_rows = [row_to_dict(row) for row in conn.execute(sql, params).fetchall()]

    return {
        "base_rows": base_rows,
        "base_ids": base_ids,
        "memories_by_id": memories_by_id,
        "links": link_rows,
    }


def evaluate_lifecycle_integrity_graph(
    graph: dict[str, Any],
    *,
    sample_limit: int = 20,
    include_debug: bool = False,
) -> dict[str, Any]:
    memories_by_id = dict(graph["memories_by_id"])
    links = list(graph["links"])
    findings: list[dict[str, Any]] = []
    unsupported_metrics: list[str] = []
    issue_counts: Counter[str] = Counter()
    source_memory_ids: set[int] = set()

    link_pairs = {
        (int(link["from_memory_id"]), int(link["to_memory_id"]))
        for link in links
        if link.get("from_memory_id") is not None and link.get("to_memory_id") is not None
    }
    legacy_state_alias_ids: list[int] = []

    children_by_parent: dict[int, list[int]] = defaultdict(list)
    for memory in memories_by_id.values():
        parent_id = memory.get("supersedes_memory_id")
        if parent_id is not None and int(parent_id) in memories_by_id:
            children_by_parent[int(parent_id)].append(int(memory["id"]))

    for memory in memories_by_id.values():
        memory_id = int(memory["id"])
        raw_state = _memory_state_code(memory)
        raw_activity_state = _memory_activity_state(memory)
        raw_status = _memory_v2_status(memory)
        if raw_state == "active":
            legacy_state_alias_ids.append(memory_id)

        try:
            canonical_state = derive_canonical_memory_state(
                state_code=raw_state,
                activity_state=raw_activity_state,
                contradiction_flag=memory.get("contradiction_flag"),
            )
        except ValueError:
            findings.append(
                _make_finding(
                    "unknown_state_code",
                    severity="critical",
                    memory_ids=[memory_id],
                    reason=f"Unsupported lifecycle state_code={raw_state!r}.",
                    operator_next_action="resolve_integrity_issue",
                    project_key=memory.get("project_key"),
                    scope_code=memory.get("scope_code"),
                )
            )
            continue

        expected_status = project_memory_v2_status(
            state_code=raw_state,
            activity_state=raw_activity_state,
            contradiction_flag=memory.get("contradiction_flag"),
        )
        if raw_status is not None and raw_status != expected_status:
            findings.append(
                _make_finding(
                    "state_projection_mismatch",
                    severity="warning",
                    memory_ids=[memory_id],
                    reason=f"Stored memory_v2_status={raw_status!r} differs from expected projection {expected_status!r} for state_code={raw_state!r}.",
                    operator_next_action="inspect_projection",
                    project_key=memory.get("project_key"),
                    scope_code=memory.get("scope_code"),
                    expected_memory_v2_status=expected_status,
                    actual_memory_v2_status=raw_status,
                )
            )

        if (raw_activity_state == "archived" and canonical_state != "archived") or (
            canonical_state == "archived" and raw_activity_state != "archived"
        ):
            findings.append(
                _make_finding(
                    "activity_state_mismatch",
                    severity="warning",
                    memory_ids=[memory_id],
                    reason="activity_state and canonical lifecycle state disagree on archival state.",
                    operator_next_action="inspect_projection",
                    project_key=memory.get("project_key"),
                    scope_code=memory.get("scope_code"),
                    state_code=raw_state,
                    activity_state=raw_activity_state,
                )
            )

        parent_id = memory.get("supersedes_memory_id")
        child_id = memory.get("superseded_by_memory_id")
        if parent_id is not None:
            parent_id = int(parent_id)
            parent_memory = memories_by_id.get(parent_id)
            if parent_memory is None:
                findings.append(
                    _make_finding(
                        "supersedes_missing_target",
                        severity="critical",
                        memory_ids=[memory_id, parent_id],
                        reason="supersedes_memory_id points to a missing memory.",
                        operator_next_action="resolve_lineage_target",
                        project_key=memory.get("project_key"),
                        scope_code=memory.get("scope_code"),
                    )
                )
            else:
                if memory.get("superseded_by_memory_id") is None and canonical_state not in {"archived", "superseded"}:
                    pass
                parent_project_key, parent_scope_code = _memory_scope_key(parent_memory)
                current_project_key, current_scope_code = _memory_scope_key(memory)
                if current_project_key != parent_project_key and not (
                    current_project_key is None and parent_project_key is None
                ):
                    findings.append(
                        _make_finding(
                            "cross_project_supersession",
                            severity="critical",
                            memory_ids=[memory_id, parent_id],
                            reason="Supersession lineage crosses project_key boundaries.",
                            operator_next_action="resolve_integrity_issue",
                            project_key=current_project_key or parent_project_key,
                            scope_code=current_scope_code or parent_scope_code,
                        )
                    )
                if current_scope_code != parent_scope_code and not (
                    current_scope_code is None and parent_scope_code is None
                ):
                    findings.append(
                        _make_finding(
                            "cross_scope_supersession",
                            severity="critical",
                            memory_ids=[memory_id, parent_id],
                            reason="Supersession lineage crosses scope_code boundaries.",
                            operator_next_action="resolve_integrity_issue",
                            project_key=current_project_key or parent_project_key,
                            scope_code=current_scope_code or parent_scope_code,
                        )
                    )
                if int(parent_memory.get("superseded_by_memory_id") or 0) != memory_id:
                    parent_replacement_id = parent_memory.get("superseded_by_memory_id")
                    findings.append(
                        _make_finding(
                            "reverse_pointer_mismatch",
                            severity="critical",
                            memory_ids=[
                                memory_id,
                                parent_id,
                                *(
                                    [int(parent_replacement_id)]
                                    if parent_replacement_id is not None
                                    else []
                                ),
                            ],
                            reason="Parent memory points to a different replacement than the child lineage field expects.",
                            operator_next_action="resolve_integrity_issue",
                            project_key=memory.get("project_key"),
                            scope_code=memory.get("scope_code"),
                        )
                    )
                if (memory_id, parent_id) not in link_pairs:
                    findings.append(
                        _make_finding(
                            "supersedes_link_field_mismatch",
                            severity="critical",
                            memory_ids=[memory_id, parent_id],
                            reason="supersedes_memory_id field is present without a matching supersedes relation link.",
                            operator_next_action="resolve_integrity_issue",
                            project_key=memory.get("project_key"),
                            scope_code=memory.get("scope_code"),
                        )
                    )

        if child_id is not None:
            child_id = int(child_id)
            child_memory = memories_by_id.get(child_id)
            if child_memory is None:
                findings.append(
                    _make_finding(
                        "superseded_by_missing_target",
                        severity="critical",
                        memory_ids=[memory_id, child_id],
                        reason="superseded_by_memory_id points to a missing memory.",
                        operator_next_action="resolve_lineage_target",
                        project_key=memory.get("project_key"),
                        scope_code=memory.get("scope_code"),
                    )
                )
            else:
                if int(child_memory.get("supersedes_memory_id") or 0) != memory_id:
                    child_parent_id = child_memory.get("supersedes_memory_id")
                    findings.append(
                        _make_finding(
                            "reverse_pointer_mismatch",
                            severity="critical",
                            memory_ids=[
                                memory_id,
                                child_id,
                                *([int(child_parent_id)] if child_parent_id is not None else []),
                            ],
                            reason="Replacement memory points to a different parent than superseded_by_memory_id expects.",
                            operator_next_action="resolve_integrity_issue",
                            project_key=memory.get("project_key"),
                            scope_code=memory.get("scope_code"),
                        )
                    )

        if child_id is not None and canonical_state not in {"superseded", "archived"}:
            findings.append(
                _make_finding(
                    "active_with_superseded_by",
                    severity="critical",
                    memory_ids=[memory_id, child_id],
                    reason="Memory still looks active while superseded_by_memory_id already points to a replacement head.",
                    operator_next_action="resolve_integrity_issue",
                    project_key=memory.get("project_key"),
                    scope_code=memory.get("scope_code"),
                )
            )

        if canonical_state == "superseded":
            has_lineage = parent_id is not None or child_id is not None or any(
                memory_id in pair for pair in link_pairs
            )
            if not has_lineage:
                findings.append(
                    _make_finding(
                        "superseded_without_lineage",
                        severity="warning",
                        memory_ids=[memory_id],
                        reason="Memory is superseded but has no lineage pointers or supersedes link.",
                        operator_next_action="inspect_lineage",
                        project_key=memory.get("project_key"),
                        scope_code=memory.get("scope_code"),
                    )
                )

    for child_id, parent_id in sorted(link_pairs):
        child_memory = memories_by_id.get(int(child_id))
        if child_memory is None:
            continue
        if int(child_memory.get("supersedes_memory_id") or 0) != int(parent_id):
            findings.append(
                _make_finding(
                    "supersedes_link_field_mismatch",
                    severity="critical",
                    memory_ids=[child_id, parent_id],
                    reason="supersedes relation link exists without matching supersedes_memory_id field.",
                    operator_next_action="resolve_integrity_issue",
                    project_key=child_memory.get("project_key"),
                    scope_code=child_memory.get("scope_code"),
                )
            )

    for parent_id, child_ids in sorted(children_by_parent.items()):
        unique_children = sorted({int(child_id) for child_id in child_ids})
        if len(unique_children) > 1:
            findings.append(
                _make_finding(
                    "supersession_branch",
                    severity="warning",
                    memory_ids=[parent_id, *unique_children],
                    reason="A single lineage ancestor has multiple replacement children; this is ambiguous until reviewed.",
                    operator_next_action="inspect_branch",
                    project_key=memories_by_id[parent_id].get("project_key"),
                    scope_code=memories_by_id[parent_id].get("scope_code"),
                )
            )

    graph_edges = {
        int(memory_id): [
            int(memory.get("supersedes_memory_id"))
            for memory_id, memory in memories_by_id.items()
            if memory.get("supersedes_memory_id") is not None
        ]
    }
    # Normalize edge map to one outgoing edge per child.
    graph_edges = {
        int(memory_id): [int(memory["supersedes_memory_id"])]
        for memory_id, memory in memories_by_id.items()
        if memory.get("supersedes_memory_id") is not None and int(memory["supersedes_memory_id"]) in memories_by_id
    }
    visited_cycle: set[int] = set()
    stack: list[int] = []
    on_stack: set[int] = set()
    cycle_paths: list[list[int]] = []

    def _walk_cycle(node_id: int) -> None:
        visited_cycle.add(node_id)
        stack.append(node_id)
        on_stack.add(node_id)
        for neighbor_id in graph_edges.get(node_id, []):
            if neighbor_id not in visited_cycle:
                _walk_cycle(neighbor_id)
            elif neighbor_id in on_stack:
                cycle_start = stack.index(neighbor_id)
                cycle_paths.append(stack[cycle_start:] + [neighbor_id])
        on_stack.remove(node_id)
        stack.pop()

    for node_id in sorted(memories_by_id):
        if node_id not in visited_cycle:
            _walk_cycle(node_id)

    for cycle_path in cycle_paths:
        findings.append(
            _make_finding(
                "supersession_cycle",
                severity="critical",
                memory_ids=cycle_path,
                reason="Supersession lineage contains a cycle.",
                operator_next_action="resolve_integrity_issue",
                project_key=memories_by_id[cycle_path[0]].get("project_key"),
                scope_code=memories_by_id[cycle_path[0]].get("scope_code"),
                cycle_path=cycle_path,
            )
        )

    undirected_neighbors: dict[int, set[int]] = defaultdict(set)
    for child_id, parent_ids in graph_edges.items():
        for parent_id in parent_ids:
            undirected_neighbors[child_id].add(parent_id)
            undirected_neighbors[parent_id].add(child_id)
    for child_id, parent_id in link_pairs:
        if child_id in memories_by_id and parent_id in memories_by_id:
            undirected_neighbors[child_id].add(parent_id)
            undirected_neighbors[parent_id].add(child_id)

    components: list[list[int]] = []
    remaining = set(undirected_neighbors)
    while remaining:
        root = remaining.pop()
        queue = deque([root])
        component = [root]
        while queue:
            current = queue.popleft()
            for neighbor in undirected_neighbors[current]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    queue.append(neighbor)
                    component.append(neighbor)
        components.append(sorted(component))

    for component in components:
        active_heads = []
        for memory_id in component:
            memory = memories_by_id[memory_id]
            try:
                canonical_state = derive_canonical_memory_state(
                    state_code=_memory_state_code(memory),
                    activity_state=_memory_activity_state(memory),
                    contradiction_flag=memory.get("contradiction_flag"),
                )
            except ValueError:
                continue
            if memory.get("superseded_by_memory_id") is None and canonical_state not in {"archived", "superseded"}:
                active_heads.append(memory_id)
        if len(active_heads) > 1:
            findings.append(
                _make_finding(
                    "multiple_active_heads",
                    severity="critical",
                    memory_ids=active_heads,
                    reason="Lineage has more than one active head candidate.",
                    operator_next_action="resolve_integrity_issue",
                    project_key=memories_by_id[active_heads[0]].get("project_key"),
                    scope_code=memories_by_id[active_heads[0]].get("scope_code"),
                )
            )

    if legacy_state_alias_ids:
        unsupported_metrics.append(
            "legacy state_code='active' is treated as canonical validated for lifecycle projection and integrity checks"
        )

    deduped_findings_map: dict[tuple[Any, ...], dict[str, Any]] = {}
    for finding in findings:
        key = (
            finding["issue_code"],
            tuple(finding["memory_ids"]),
            finding["reason"],
        )
        deduped_findings_map[key] = finding
    ordered_findings = sorted(
        deduped_findings_map.values(),
        key=lambda item: (
            SEVERITY_RANK.get(str(item["severity"]), 99),
            str(item["issue_code"]),
            tuple(item["memory_ids"]),
        ),
    )

    for finding in ordered_findings:
        issue_counts[str(finding["issue_code"])] += 1
        source_memory_ids.update(int(memory_id) for memory_id in finding["memory_ids"])

    critical_issues = sum(
        1 for finding in ordered_findings if str(finding["issue_code"]) in CRITICAL_LIFECYCLE_ISSUE_CODES
    )

    recommended_actions: list[str] = []
    if issue_counts.get("unknown_state_code"):
        recommended_actions.append("Normalize unsupported lifecycle states before trusting lifecycle automation.")
    if issue_counts.get("state_projection_mismatch") or issue_counts.get("activity_state_mismatch"):
        recommended_actions.append("Consolidate lifecycle field semantics around state_code and the compatibility projection.")
    if issue_counts.get("supersedes_missing_target") or issue_counts.get("superseded_by_missing_target"):
        recommended_actions.append("Repair missing lineage targets before enabling supersession apply in later batches.")
    if issue_counts.get("reverse_pointer_mismatch") or issue_counts.get("supersedes_link_field_mismatch"):
        recommended_actions.append("Repair lineage pointers and supersedes links before trusting lineage-based previews.")
    if issue_counts.get("supersession_cycle") or issue_counts.get("multiple_active_heads"):
        recommended_actions.append("Resolve cycle or multi-head lineage ambiguity before attempting any supersession workflow.")
    if issue_counts.get("cross_project_supersession") or issue_counts.get("cross_scope_supersession"):
        recommended_actions.append("Resolve cross-project or cross-scope lineage before allowing guarded supersession preview.")

    result = {
        "status": "ok" if not ordered_findings and not unsupported_metrics else ("warning" if ordered_findings or unsupported_metrics else "ok"),
        "schema_version": LIFECYCLE_INTEGRITY_SCHEMA_VERSION,
        "summary": {
            "memories_checked": len(graph["base_rows"]),
            "lineages_checked": len(components),
            "issues_total": len(ordered_findings),
            "critical_issues": critical_issues,
        },
        "issue_counts": dict(issue_counts),
        "findings": ordered_findings[: int(sample_limit)],
        "recommended_actions": recommended_actions,
        "safety": {
            "read_only": True,
            "mutations_performed": 0,
        },
        "unsupported_metrics": unsupported_metrics,
        "source_memory_ids": sorted(source_memory_ids),
    }
    if critical_issues:
        result["status"] = "error"
    elif ordered_findings or unsupported_metrics:
        result["status"] = "warning"
    if include_debug:
        result["debug"] = {
            "graph_memory_ids": sorted(memories_by_id),
            "supersedes_link_pairs": sorted(link_pairs),
            "lineage_components": components,
            "hash_algorithm": MEMORY_V3_HASH_ALGORITHM,
        }
    return result


def get_memory_lifecycle_integrity_report_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
    memory_id: int | None = None,
    include_archived: bool = True,
    limit: int = 100,
    sample_limit: int = 20,
    include_debug: bool = False,
    row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 500:
        return {
            "status": "error",
            "schema_version": LIFECYCLE_INTEGRITY_SCHEMA_VERSION,
            "error": "limit musi byc w zakresie 1..500",
        }
    if sample_limit < 1 or sample_limit > 100:
        return {
            "status": "error",
            "schema_version": LIFECYCLE_INTEGRITY_SCHEMA_VERSION,
            "error": "sample_limit musi byc w zakresie 1..100",
        }
    if memory_id is not None and int(memory_id) < 1:
        return {
            "status": "error",
            "schema_version": LIFECYCLE_INTEGRITY_SCHEMA_VERSION,
            "error": "memory_id is invalid",
        }

    graph = load_lifecycle_graph(
        conn,
        project_key=project_key,
        scope_code=scope_code,
        memory_id=memory_id,
        include_archived=include_archived,
        limit=limit,
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
    )
    result = evaluate_lifecycle_integrity_graph(
        graph,
        sample_limit=sample_limit,
        include_debug=include_debug,
    )
    result["filters"] = {
        "project_key": normalize_optional_text(project_key),
        "scope_code": normalize_optional_text(scope_code),
        "memory_id": None if memory_id is None else int(memory_id),
        "include_archived": bool(include_archived),
        "limit": int(limit),
        "sample_limit": int(sample_limit),
    }
    return result
