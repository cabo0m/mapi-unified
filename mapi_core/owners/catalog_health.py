from __future__ import annotations

"""Owner catalog health and repair suggestion helpers."""

from typing import Any, Callable


def list_owner_directory_items_payload(
    conn: Any,
    *,
    owner_type: str | None = None,
    active_only: bool = False,
    normalize_optional_text: Callable[[Any], str | None],
    owner_directory_item_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    normalized_owner_type = normalize_optional_text(owner_type)
    sql = "SELECT * FROM owner_directory_items WHERE 1 = 1"
    params: list[Any] = []
    if normalized_owner_type:
        sql += " AND owner_type = ?"
        params.append(normalized_owner_type)
    if active_only:
        sql += " AND is_active = 1"
    sql += " ORDER BY owner_key ASC"
    rows = conn.execute(sql, params).fetchall()
    return {
        "count": len(rows),
        "items": [owner_directory_item_to_dict(row) for row in rows],
        "filters": {"owner_type": normalized_owner_type, "active_only": bool(active_only)},
    }


def list_owner_role_mappings_payload(
    conn: Any,
    *,
    owner_role: str | None = None,
    project_key: str | None = None,
    scope_code: str | None = None,
    active_only: bool = False,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    normalized_owner_role = normalize_optional_text(owner_role)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)
    sql = "SELECT * FROM owner_role_mappings WHERE 1 = 1"
    params: list[Any] = []
    if normalized_owner_role:
        sql += " AND owner_role = ?"
        params.append(normalized_owner_role)
    if normalized_project_key is not None:
        sql += " AND project_key = ?"
        params.append(normalized_project_key)
    if normalized_scope_code is not None:
        sql += " AND scope_code = ?"
        params.append(normalized_scope_code)
    if active_only:
        sql += " AND is_active = 1"
    sql += " ORDER BY owner_role ASC, COALESCE(project_key, ''), COALESCE(scope_code, ''), id ASC"
    rows = conn.execute(sql, params).fetchall()
    return {
        "count": len(rows),
        "items": [owner_role_mapping_to_dict(row) for row in rows],
        "filters": {
            "owner_role": normalized_owner_role,
            "project_key": normalized_project_key,
            "scope_code": normalized_scope_code,
            "active_only": bool(active_only),
        },
    }


def get_owner_catalog_health_data(
    conn: Any,
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    owner_directory_item_to_dict: Callable[[Any], dict[str, Any]],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
    owner_directory_governance_warnings: Callable[..., list[dict[str, Any]]],
    owner_mapping_governance_warnings: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)
    mapping_rows = conn.execute("SELECT * FROM owner_role_mappings WHERE is_active = 1 ORDER BY id ASC").fetchall()
    active_directory_rows = conn.execute("SELECT * FROM owner_directory_items WHERE is_active = 1 ORDER BY owner_key ASC").fetchall()
    problems: list[dict[str, Any]] = []
    governance_warnings: list[dict[str, Any]] = []
    broken_count = 0
    inactive_count = 0
    active_target_count = len(active_directory_rows)

    for owner_row in conn.execute("SELECT * FROM owner_directory_items ORDER BY owner_key ASC").fetchall():
        owner_item = owner_directory_item_to_dict(owner_row)
        owner_warnings = owner_directory_governance_warnings(
            str(owner_item.get("owner_key") or ""),
            str(owner_item.get("owner_type") or ""),
            normalize_optional_text(owner_item.get("routing_metadata_json")),
            is_active=bool(owner_item.get("is_active")),
        )
        for warning in owner_warnings:
            warning_item = dict(warning)
            warning_item.update({
                "owner_key": owner_item.get("owner_key"),
                "owner_type": owner_item.get("owner_type"),
                "source": "owner_directory_item",
            })
            governance_warnings.append(warning_item)

    for row in mapping_rows:
        mapping = owner_role_mapping_to_dict(row)
        mapping_project_key = normalize_optional_text(mapping.get("project_key"))
        mapping_scope_code = normalize_scope_code(mapping.get("scope_code"))
        if normalized_project_key is not None and mapping_project_key not in {None, normalized_project_key}:
            continue
        if normalized_scope_code is not None and mapping_scope_code not in {None, normalized_scope_code}:
            continue
        mapping_warnings = owner_mapping_governance_warnings(
            conn,
            owner_role=str(mapping.get("owner_role") or ""),
            owner_key=str(mapping.get("owner_key") or ""),
            project_key=mapping.get("project_key"),
            scope_code=mapping.get("scope_code"),
            is_active=bool(mapping.get("is_active")),
            current_mapping_id=int(mapping.get("id") or 0),
        )
        for warning in mapping_warnings:
            warning_item = dict(warning)
            warning_item.update({
                "owner_role": mapping.get("owner_role"),
                "owner_key": mapping.get("owner_key"),
                "project_key": mapping.get("project_key"),
                "scope_code": mapping.get("scope_code"),
                "mapping_id": int(mapping.get("id") or 0),
                "source": "owner_role_mapping",
            })
            governance_warnings.append(warning_item)

        owner_row = conn.execute("SELECT * FROM owner_directory_items WHERE owner_key = ?", (mapping["owner_key"],)).fetchone()
        if owner_row is None:
            broken_count += 1
            problems.append({
                "kind": "broken_owner_mapping",
                "reason": "owner_missing_in_directory",
                "owner_role": mapping.get("owner_role"),
                "owner_key": mapping.get("owner_key"),
                "project_key": mapping.get("project_key"),
                "scope_code": mapping.get("scope_code"),
                "mapping_id": int(mapping.get("id") or 0),
            })
            continue
        owner_item = owner_directory_item_to_dict(owner_row)
        if not bool(owner_item.get("is_active")):
            broken_count += 1
            inactive_count += 1
            problems.append({
                "kind": "inactive_owner_target",
                "reason": "owner_inactive",
                "owner_role": mapping.get("owner_role"),
                "owner_key": mapping.get("owner_key"),
                "project_key": mapping.get("project_key"),
                "scope_code": mapping.get("scope_code"),
                "mapping_id": int(mapping.get("id") or 0),
            })

    return {
        "broken_owner_mapping_count": broken_count,
        "inactive_owner_target_count": inactive_count,
        "active_owner_target_count": active_target_count,
        "governance_warning_count": len(governance_warnings),
        "problem_count": len(problems),
        "problems": problems,
        "governance_warnings": governance_warnings,
    }


def get_owner_catalog_health_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    owner_directory_item_to_dict: Callable[[Any], dict[str, Any]],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
    owner_directory_governance_warnings: Callable[..., list[dict[str, Any]]],
    owner_mapping_governance_warnings: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)
    health = get_owner_catalog_health_data(
        conn,
        project_key=normalized_project_key,
        scope_code=normalized_scope_code,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
        owner_directory_item_to_dict=owner_directory_item_to_dict,
        owner_role_mapping_to_dict=owner_role_mapping_to_dict,
        owner_directory_governance_warnings=owner_directory_governance_warnings,
        owner_mapping_governance_warnings=owner_mapping_governance_warnings,
    )
    return {
        "status": "ok" if int(health.get("problem_count") or 0) == 0 else "attention",
        "filters": {
            "project_key": normalized_project_key,
            "scope_code": normalized_scope_code,
        },
        "summary": {
            "broken_owner_mapping_count": int(health.get("broken_owner_mapping_count") or 0),
            "inactive_owner_target_count": int(health.get("inactive_owner_target_count") or 0),
            "active_owner_target_count": int(health.get("active_owner_target_count") or 0),
            "governance_warning_count": int(health.get("governance_warning_count") or 0),
            "problem_count": int(health.get("problem_count") or 0),
        },
        "problems": health.get("problems") or [],
        "governance_warnings": health.get("governance_warnings") or [],
    }


def suggest_owner_mapping_repairs(
    conn: Any,
    *,
    owner_role: str | None,
    owner_key: str | None,
    project_key: str | None,
    scope_code: str | None,
    reason: str | None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    owner_directory_item_to_dict: Callable[[Any], dict[str, Any]],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_owner_role = normalize_optional_text(owner_role)
    normalized_owner_key = normalize_optional_text(owner_key)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)
    normalized_reason = normalize_optional_text(reason)
    suggestions: list[dict[str, Any]] = []

    def _score_suggestion(suggestion: dict[str, Any]) -> int:
        kind_value = normalize_optional_text(suggestion.get("kind"))
        score = 10
        if kind_value == "reactivate_owner_target":
            score = 100 if normalized_reason == "owner_inactive" else 70
        elif kind_value == "remap_to_existing_role_target":
            score = 95
            mapping_scope = suggestion.get("mapping_scope") or {}
            if normalize_optional_text(mapping_scope.get("project_key")) == normalized_project_key:
                score += 5
            if normalize_scope_code(mapping_scope.get("scope_code")) == normalized_scope_code:
                score += 3
        elif kind_value == "remap_to_active_same_type_target":
            score = 75
        elif kind_value == "create_missing_owner_target":
            score = 60 if normalized_reason == "owner_missing_in_directory" else 40
        return int(score)

    if normalized_owner_key is not None:
        owner_row = conn.execute("SELECT * FROM owner_directory_items WHERE owner_key = ?", (normalized_owner_key,)).fetchone()
        if owner_row is not None:
            owner_item = owner_directory_item_to_dict(owner_row)
            if not bool(owner_item.get("is_active")):
                suggestions.append({
                    "kind": "reactivate_owner_target",
                    "owner_key": normalized_owner_key,
                    "display_name": owner_item.get("display_name"),
                })
            owner_type = normalize_optional_text(owner_item.get("owner_type"))
        else:
            owner_type = None
            suggestions.append({
                "kind": "create_missing_owner_target",
                "owner_key": normalized_owner_key,
            })
    else:
        owner_type = None

    rows = conn.execute("SELECT * FROM owner_role_mappings WHERE owner_role = ? AND is_active = 1 ORDER BY id ASC", (normalized_owner_role,)).fetchall() if normalized_owner_role else []
    for row in rows:
        mapping = owner_role_mapping_to_dict(row)
        mapping_project_key = normalize_optional_text(mapping.get("project_key"))
        mapping_scope_code = normalize_scope_code(mapping.get("scope_code"))
        if mapping_project_key is not None and mapping_project_key != normalized_project_key:
            continue
        if mapping_scope_code is not None and mapping_scope_code != normalized_scope_code:
            continue
        candidate_owner_key = normalize_optional_text(mapping.get("owner_key"))
        if candidate_owner_key is None or candidate_owner_key == normalized_owner_key:
            continue
        candidate_row = conn.execute("SELECT * FROM owner_directory_items WHERE owner_key = ?", (candidate_owner_key,)).fetchone()
        if candidate_row is None:
            continue
        candidate_item = owner_directory_item_to_dict(candidate_row)
        if not bool(candidate_item.get("is_active")):
            continue
        suggestions.append({
            "kind": "remap_to_existing_role_target",
            "owner_key": candidate_owner_key,
            "display_name": candidate_item.get("display_name"),
            "owner_type": candidate_item.get("owner_type"),
            "mapping_scope": {"project_key": mapping.get("project_key"), "scope_code": mapping.get("scope_code")},
        })

    if owner_type is not None:
        directory_rows = conn.execute("SELECT * FROM owner_directory_items WHERE owner_type = ? AND is_active = 1 ORDER BY owner_key ASC", (owner_type,)).fetchall()
        for row in directory_rows:
            candidate_item = owner_directory_item_to_dict(row)
            candidate_owner_key = normalize_optional_text(candidate_item.get("owner_key"))
            if candidate_owner_key is None or candidate_owner_key == normalized_owner_key:
                continue
            suggestions.append({
                "kind": "remap_to_active_same_type_target",
                "owner_key": candidate_owner_key,
                "display_name": candidate_item.get("display_name"),
                "owner_type": candidate_item.get("owner_type"),
            })

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for suggestion in suggestions:
        key = (str(suggestion.get("kind") or ""), normalize_optional_text(suggestion.get("owner_key")))
        if key in seen:
            continue
        seen.add(key)
        suggestion["score"] = _score_suggestion(suggestion)
        deduped.append(suggestion)
    deduped.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("kind") or ""), str(item.get("owner_key") or "")))
    for index, suggestion in enumerate(deduped, start=1):
        suggestion["rank"] = int(index)
        suggestion["is_recommended"] = index == 1
    return deduped


def get_problematic_owner_mappings_payload(
    conn: Any,
    *,
    limit: int = 50,
    project_key: str | None = None,
    scope_code: str | None = None,
    kind: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    owner_directory_item_to_dict: Callable[[Any], dict[str, Any]],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
    owner_directory_governance_warnings: Callable[..., list[dict[str, Any]]],
    owner_mapping_governance_warnings: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)
    normalized_kind = normalize_optional_text(kind)
    health = get_owner_catalog_health_data(
        conn,
        project_key=normalized_project_key,
        scope_code=normalized_scope_code,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
        owner_directory_item_to_dict=owner_directory_item_to_dict,
        owner_role_mapping_to_dict=owner_role_mapping_to_dict,
        owner_directory_governance_warnings=owner_directory_governance_warnings,
        owner_mapping_governance_warnings=owner_mapping_governance_warnings,
    )
    items: list[dict[str, Any]] = []
    for problem in health.get("problems") or []:
        if normalized_kind is not None and normalize_optional_text(problem.get("kind")) != normalized_kind:
            continue
        problem_item = dict(problem)
        priority = "P1" if normalize_optional_text(problem.get("kind")) in {"broken_owner_mapping", "inactive_owner_target"} else "P2"
        problem_item["priority"] = priority
        problem_item["repair_suggestions"] = suggest_owner_mapping_repairs(
            conn,
            owner_role=problem.get("owner_role"),
            owner_key=problem.get("owner_key"),
            project_key=problem.get("project_key"),
            scope_code=problem.get("scope_code"),
            reason=problem.get("reason"),
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            owner_directory_item_to_dict=owner_directory_item_to_dict,
            owner_role_mapping_to_dict=owner_role_mapping_to_dict,
        )
        problem_item["recommended_repair"] = problem_item["repair_suggestions"][0] if problem_item["repair_suggestions"] else None
        items.append(problem_item)

    items.sort(key=lambda item: (0 if item.get("priority") == "P1" else 1, str(item.get("owner_role") or ""), int(item.get("mapping_id") or 0)))
    return {
        "status": "ok" if not items else "attention",
        "count": len(items),
        "items": items[: int(limit)],
        "filters": {
            "limit": int(limit),
            "project_key": normalized_project_key,
            "scope_code": normalized_scope_code,
            "kind": normalized_kind,
        },
    }


def get_owner_mapping_batch_candidates_payload(
    conn: Any,
    *,
    limit: int = 20,
    max_groups: int = 10,
    project_key: str | None = None,
    scope_code: str | None = None,
    kind: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    owner_directory_item_to_dict: Callable[[Any], dict[str, Any]],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
    owner_directory_governance_warnings: Callable[..., list[dict[str, Any]]],
    owner_mapping_governance_warnings: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}
    if max_groups < 1 or max_groups > 200:
        return {"status": "error", "error": "max_groups musi byÄ‡ w zakresie 1..200"}
    problems = get_problematic_owner_mappings_payload(
        conn,
        limit=max(int(limit), 200),
        project_key=project_key,
        scope_code=scope_code,
        kind=kind,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
        owner_directory_item_to_dict=owner_directory_item_to_dict,
        owner_role_mapping_to_dict=owner_role_mapping_to_dict,
        owner_directory_governance_warnings=owner_directory_governance_warnings,
        owner_mapping_governance_warnings=owner_mapping_governance_warnings,
    )
    grouped: dict[str, dict[str, Any]] = {}
    for item in problems.get("items") or []:
        recommended = item.get("recommended_repair") or {}
        repair_kind = normalize_optional_text(recommended.get("kind"))
        mapping_id = int(item.get("mapping_id") or 0)
        if repair_kind is None or mapping_id < 1:
            continue
        if repair_kind == "reactivate_owner_target":
            target_owner_key = normalize_optional_text(item.get("owner_key"))
        else:
            target_owner_key = normalize_optional_text(recommended.get("owner_key"))
        group_key = f"{repair_kind}::{target_owner_key or '__none__'}"
        group = grouped.setdefault(group_key, {
            "group_key": group_key,
            "repair_kind": repair_kind,
            "target_owner_key": target_owner_key,
            "mapping_ids": [],
            "problem_count": 0,
            "priority_counts": {"P1": 0, "P2": 0},
            "problems": [],
            "can_preview": True,
        })
        group["mapping_ids"].append(mapping_id)
        group["problem_count"] += 1
        priority_value = str(item.get("priority") or "P2")
        if priority_value not in group["priority_counts"]:
            group["priority_counts"][priority_value] = 0
        group["priority_counts"][priority_value] += 1
        group["problems"].append({
            "mapping_id": mapping_id,
            "owner_role": item.get("owner_role"),
            "owner_key": item.get("owner_key"),
            "kind": item.get("kind"),
            "priority": item.get("priority"),
            "recommended_repair": recommended,
        })
        if repair_kind in {"remap_to_existing_role_target", "remap_to_active_same_type_target"}:
            group["preview_params"] = {
                "repair_kind": "remap_to_target",
                "target_owner_key": target_owner_key,
            }
            group["execution_params"] = {
                "repair_kind": "remap_to_target",
                "target_owner_key": target_owner_key,
            }
        elif repair_kind == "reactivate_owner_target":
            group["preview_params"] = {"repair_kind": "reactivate_owner_target"}
            group["execution_params"] = {"repair_kind": "reactivate_owner_target"}
        elif repair_kind == "create_missing_owner_target":
            group["preview_params"] = {
                "repair_kind": "create_missing_owner_target",
                "target_owner_key": target_owner_key,
            }
            group["execution_params"] = {
                "repair_kind": "create_missing_owner_target",
                "target_owner_key": target_owner_key,
            }
        else:
            group["can_preview"] = False
            group["preview_params"] = None
            group["execution_params"] = None

    groups = sorted(
        grouped.values(),
        key=lambda item: (-int(item.get("priority_counts", {}).get("P1", 0)), -int(item.get("problem_count") or 0), str(item.get("repair_kind") or ""), str(item.get("target_owner_key") or "")),
    )
    for index, group in enumerate(groups, start=1):
        group["rank"] = int(index)
    return {
        "status": "ok" if not groups else "attention",
        "count": len(groups[: int(max_groups)]),
        "groups": groups[: int(max_groups)],
        "filters": {
            "limit": int(limit),
            "max_groups": int(max_groups),
            "project_key": normalize_optional_text(project_key),
            "scope_code": normalize_scope_code(scope_code),
            "kind": normalize_optional_text(kind),
        },
    }


def get_owner_catalog_governance_history_payload(
    conn: Any,
    *,
    limit: int = 50,
    offset: int = 0,
    project_key: str | None = None,
    owner_catalog_audit_project_key: Callable[[str | None], str],
    timeline_rows_to_dicts: Callable[..., list[dict[str, Any]]],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}
    if offset < 0:
        return {"status": "error", "error": "offset musi byÄ‡ >= 0"}
    audit_project_key = owner_catalog_audit_project_key(project_key)
    rows = conn.execute(
        """
        SELECT * FROM timeline_events
        WHERE project_key = ?
          AND event_type = 'project.note_recorded'
          AND (
                payload_json LIKE '%owner_directory_change%'
             OR payload_json LIKE '%owner_role_mapping_change%'
             OR payload_json LIKE '%owner_catalog_governance%'
          )
        ORDER BY event_time DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (audit_project_key, int(limit), int(offset)),
    ).fetchall()
    total_row = conn.execute(
        """
        SELECT COUNT(*) AS total_count FROM timeline_events
        WHERE project_key = ?
          AND event_type = 'project.note_recorded'
          AND (
                payload_json LIKE '%owner_directory_change%'
             OR payload_json LIKE '%owner_role_mapping_change%'
             OR payload_json LIKE '%owner_catalog_governance%'
          )
        """,
        (audit_project_key,),
    ).fetchone()
    items = timeline_rows_to_dicts(rows, row_to_dict=row_to_dict)
    total_count = int((row_to_dict(total_row) or {}).get("total_count") or 0) if total_row is not None else 0
    return {
        "status": "ok",
        "count": len(items),
        "total_count": total_count,
        "items": items,
        "filters": {"limit": int(limit), "offset": int(offset), "project_key": audit_project_key},
    }


def get_owner_mapping_repair_audit_payload(
    conn: Any,
    *,
    limit: int = 50,
    offset: int = 0,
    project_key: str | None = None,
    owner_catalog_audit_project_key: Callable[[str | None], str],
    timeline_rows_to_dicts: Callable[..., list[dict[str, Any]]],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}
    if offset < 0:
        return {"status": "error", "error": "offset musi byÄ‡ >= 0"}
    normalized_project_key = owner_catalog_audit_project_key(project_key)
    rows = conn.execute(
        """
        SELECT * FROM timeline_events
        WHERE project_key = ?
          AND event_type = 'project.note_recorded'
          AND payload_json LIKE '%owner_mapping_repair%'
        ORDER BY event_time DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (normalized_project_key, int(limit), int(offset)),
    ).fetchall()
    total_row = conn.execute(
        """
        SELECT COUNT(*) AS total_count FROM timeline_events
        WHERE project_key = ?
          AND event_type = 'project.note_recorded'
          AND payload_json LIKE '%owner_mapping_repair%'
        """,
        (normalized_project_key,),
    ).fetchone()
    items = timeline_rows_to_dicts(rows, row_to_dict=row_to_dict)
    total_count = int((row_to_dict(total_row) or {}).get("total_count") or 0) if total_row is not None else 0
    return {
        "status": "ok",
        "count": len(items),
        "total_count": total_count,
        "items": items,
        "filters": {
            "limit": int(limit),
            "offset": int(offset),
            "project_key": normalized_project_key,
        },
    }


def get_owner_governance_history_payload(
    conn: Any,
    *,
    owner_key: str | None = None,
    owner_role: str | None = None,
    project_key: str | None = None,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
    normalize_optional_text: Callable[[Any], str | None],
    timeline_rows_to_dicts: Callable[..., list[dict[str, Any]]],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        return {"status": "error", "error": "limit musi byÄ‡ w zakresie 1..1000"}
    if offset < 0:
        return {"status": "error", "error": "offset musi byÄ‡ >= 0"}
    normalized_owner_key = normalize_optional_text(owner_key)
    normalized_owner_role = normalize_optional_text(owner_role)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_category = normalize_optional_text(category)

    owner_key_pattern = f"%owner_key={normalized_owner_key}%" if normalized_owner_key else None
    owner_role_pattern = f"%owner_role={normalized_owner_role}%" if normalized_owner_role else None
    category_pattern = f'%"{normalized_category}"%' if normalized_category else None

    base_sql = """
        SELECT * FROM timeline_events
        WHERE event_type = 'project.note_recorded'
          AND (
            payload_json LIKE '%owner_directory_change%'
            OR payload_json LIKE '%owner_role_mapping_change%'
            OR payload_json LIKE '%owner_mapping_repair%'
            OR payload_json LIKE '%owner_mapping_bulk_repair%'
            OR payload_json LIKE '%owner_target_status_change%'
            OR payload_json LIKE '%sla_policy_change%'
            OR payload_json LIKE '%"escalation"%'
          )
          AND (? IS NULL OR project_key = ?)
          AND (? IS NULL OR payload_json LIKE ?)
          AND (? IS NULL OR payload_json LIKE ?)
          AND (? IS NULL OR payload_json LIKE ?)
    """
    params_query: list[Any] = [
        normalized_project_key, normalized_project_key,
        owner_key_pattern, owner_key_pattern,
        owner_role_pattern, owner_role_pattern,
        category_pattern, category_pattern,
    ]

    rows = conn.execute(
        base_sql + " ORDER BY event_time DESC, id DESC LIMIT ? OFFSET ?",
        params_query + [int(limit), int(offset)],
    ).fetchall()
    total_row = conn.execute(
        "SELECT COUNT(*) AS total_count FROM (" + base_sql + ")",
        params_query,
    ).fetchone()

    items = timeline_rows_to_dicts(rows, row_to_dict=row_to_dict)
    total_count = int((row_to_dict(total_row) or {}).get("total_count") or 0) if total_row is not None else 0
    return {
        "status": "ok",
        "count": len(items),
        "total_count": total_count,
        "items": items,
        "filters": {
            "owner_key": normalized_owner_key,
            "owner_role": normalized_owner_role,
            "project_key": normalized_project_key,
            "category": normalized_category,
            "limit": int(limit),
            "offset": int(offset),
        },
    }


GOVERNANCE_CHECKLISTS: dict[str, list[dict[str, Any]]] = {
    "new_owner_target": [
        {"id": "nt_01", "description": "owner_key jest w poprawnym formacie (lowercase, bez spacji)", "required": True, "tool_hint": "validate_new_owner_target(owner_key, owner_type, display_name)"},
        {"id": "nt_02", "description": "owner_type jest jednym z dozwolonych wartoĹ›ci: team, person, service_account, automated, external", "required": True, "tool_hint": "validate_new_owner_target(...)"},
        {"id": "nt_03", "description": "display_name jest wypeĹ‚niony i opisowy (min. 3 znaki)", "required": True, "tool_hint": "validate_new_owner_target(...)"},
        {"id": "nt_04", "description": "Nie istnieje duplikat owner_key w katalogu", "required": True, "tool_hint": "validate_new_owner_target(...) lub get_owner_catalog_health(...)"},
        {"id": "nt_05", "description": "routing_metadata_json jest poprawnym JSON (jeĹ›li podany)", "required": False, "tool_hint": "validate_new_owner_target(...)"},
        {"id": "nt_06", "description": "Nowy target ma przypisane przynajmniej jedno mapowanie roli po stworzeniu", "required": False, "tool_hint": "upsert_owner_role_mapping(...)"},
    ],
    "deactivate_target": [
        {"id": "dt_01", "description": "SprawdĹş, ktĂłre aktywne mapowania wskazujÄ… ten target", "required": True, "tool_hint": "get_problematic_owner_mappings(kind='inactive_owner_target') po deaktywacji"},
        {"id": "dt_02", "description": "Upewnij siÄ™, ĹĽe istnieje fallback lub alternatywne mapowanie dla tej roli", "required": True, "tool_hint": "get_owner_catalog_health(...)"},
        {"id": "dt_03", "description": "Przepnij aktywne mapowania na inny target lub zdeaktywuj je zanim wygasisz target", "required": True, "tool_hint": "repair_owner_mapping_issue(repair_kind='remap_to_target', ...)"},
        {"id": "dt_04", "description": "Zapisz powĂłd deaktywacji w polu reason", "required": False, "tool_hint": "set_owner_target_active(owner_key, is_active=False, reason=...)"},
        {"id": "dt_05", "description": "Uruchom get_owner_catalog_health() po deaktywacji i sprawdĹş, ĹĽe nie ma nowych broken_owner_mapping", "required": True, "tool_hint": "get_owner_catalog_health(...)"},
    ],
    "migrate_mappings": [
        {"id": "mm_01", "description": "ZrĂłb preview bulk repair przed wykonaniem zmian", "required": True, "tool_hint": "preview_bulk_repair_owner_mappings(mapping_ids, repair_kind, ...)"},
        {"id": "mm_02", "description": "SprawdĹş health katalogu przed migracjÄ…", "required": True, "tool_hint": "get_owner_catalog_health(...)"},
        {"id": "mm_03", "description": "Zapisz listÄ™ mapping_ids przed zmianÄ… (do ewentualnego rollbacku)", "required": True, "tool_hint": "get_problematic_owner_mappings(...)"},
        {"id": "mm_04", "description": "Wykonaj bulk repair i zweryfikuj audit_event w odpowiedzi", "required": True, "tool_hint": "bulk_repair_owner_mappings(...)"},
        {"id": "mm_05", "description": "SprawdĹş health katalogu po migracji â€” problem_count powinien byÄ‡ 0", "required": True, "tool_hint": "get_owner_catalog_health(...)"},
        {"id": "mm_06", "description": "SprawdĹş get_owner_catalog_repair_summary() dla potwierdzenia audytu", "required": False, "tool_hint": "get_owner_catalog_repair_summary(...)"},
    ],
    "rollout_project": [
        {"id": "rp_01", "description": "Zdefiniuj listÄ™ rĂłl i targetĂłw dla projektu", "required": True, "tool_hint": "Lista: [{owner_role, owner_key}, ...]"},
        {"id": "rp_02", "description": "Waliduj kaĹĽdy target przed rolloutem", "required": True, "tool_hint": "validate_new_owner_target(...) lub sprawdĹş get_owner_catalog_health()"},
        {"id": "rp_03", "description": "Uruchom rollout_owner_catalog_to_project z dry_run=True i sprawdĹş bĹ‚Ä™dy", "required": True, "tool_hint": "rollout_owner_catalog_to_project(project_key, mappings, dry_run=True)"},
        {"id": "rp_04", "description": "Wykonaj wĹ‚aĹ›ciwy rollout", "required": True, "tool_hint": "rollout_owner_catalog_to_project(project_key, mappings, dry_run=False)"},
        {"id": "rp_05", "description": "Waliduj overrides projektowe", "required": True, "tool_hint": "validate_project_override(project_key, owner_role, owner_key) dla kaĹĽdego mapowania"},
        {"id": "rp_06", "description": "Uruchom get_owner_catalog_health(project_key=...) po rolloutcie", "required": True, "tool_hint": "get_owner_catalog_health(project_key=...)"},
        {"id": "rp_07", "description": "SprawdĹş alerty po rolloutcie", "required": False, "tool_hint": "get_quality_alerts(project_key=...)"},
    ],
}


def get_owner_catalog_governance_checklist_payload(
    operation: str,
    *,
    project_key: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
) -> dict[str, Any]:
    normalized_op = (operation or "").strip().lower()
    allowed_operations = list(GOVERNANCE_CHECKLISTS.keys())
    if normalized_op not in GOVERNANCE_CHECKLISTS:
        return {"status": "error", "error": f"Nieznana operacja: '{normalized_op}'. DostÄ™pne: {', '.join(allowed_operations)}"}

    checklist = GOVERNANCE_CHECKLISTS[normalized_op]
    required_count = sum(1 for item in checklist if item["required"])
    return {
        "status": "ok",
        "operation": normalized_op,
        "project_key": normalize_optional_text(project_key),
        "item_count": len(checklist),
        "required_count": required_count,
        "optional_count": len(checklist) - required_count,
        "checklist": checklist,
    }


def get_owner_rollout_summary_payload(
    conn: Any,
    *,
    scope_code: str | None = None,
    include_health_check: bool = True,
    normalize_scope_code: Callable[[Any], str | None],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
    get_owner_catalog_health_data: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    normalized_scope_code = normalize_scope_code(scope_code)

    all_active_rows = conn.execute(
        "SELECT * FROM owner_role_mappings WHERE is_active = 1 ORDER BY owner_role ASC"
    ).fetchall()
    all_active = [owner_role_mapping_to_dict(row) for row in all_active_rows]

    if normalized_scope_code is not None:
        all_active = [
            mapping for mapping in all_active
            if mapping.get("scope_code") == normalized_scope_code or mapping.get("scope_code") is None
        ]

    fallback_mappings = [mapping for mapping in all_active if mapping.get("project_key") is None]
    override_mappings = [mapping for mapping in all_active if mapping.get("project_key") is not None]

    project_overrides: dict[str, list[dict[str, Any]]] = {}
    for mapping in override_mappings:
        project_key = str(mapping["project_key"])
        project_overrides.setdefault(project_key, []).append(mapping)

    global_roles = {mapping["owner_role"] for mapping in fallback_mappings}

    projects: list[dict[str, Any]] = []
    projects_with_attention: list[dict[str, Any]] = []

    for project_key in sorted(project_overrides.keys()):
        project_mappings = project_overrides[project_key]
        roles_overridden = sorted({mapping["owner_role"] for mapping in project_mappings})
        roles_on_fallback = sorted(global_roles - set(roles_overridden))

        project_entry: dict[str, Any] = {
            "project_key": project_key,
            "override_count": len(project_mappings),
            "roles_overridden": roles_overridden,
            "roles_on_fallback": roles_on_fallback,
        }

        if include_health_check:
            health_data = get_owner_catalog_health_data(
                conn,
                project_key=project_key,
                scope_code=normalized_scope_code,
            )
            problem_count = int(health_data.get("problem_count") or 0)
            governance_warning_count = int(health_data.get("governance_warning_count") or 0)
            health_status = "attention" if problem_count > 0 else "ok"
            project_entry["health_status"] = health_status
            project_entry["problem_count"] = problem_count
            project_entry["governance_warning_count"] = governance_warning_count
            if health_status == "attention":
                projects_with_attention.append(project_entry)
        else:
            project_entry["health_status"] = "not_checked"
            project_entry["problem_count"] = 0
            project_entry["governance_warning_count"] = 0

        projects.append(project_entry)

    overall_status = "attention" if projects_with_attention else "ok"
    return {
        "status": overall_status,
        "summary": {
            "projects_with_override_count": len(project_overrides),
            "global_fallback_role_count": len(global_roles),
            "projects_with_attention_count": len(projects_with_attention),
            "total_override_mapping_count": len(override_mappings),
        },
        "global_fallback_mappings": [
            {
                "owner_role": mapping["owner_role"],
                "owner_key": mapping["owner_key"],
                "is_active": bool(mapping.get("is_active")),
            }
            for mapping in fallback_mappings
        ],
        "projects": projects,
        "projects_with_attention": projects_with_attention,
    }


def get_owner_catalog_repair_summary_payload(
    conn: Any,
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
    limit_recent_audits: int = 10,
    max_groups: int = 10,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    owner_catalog_audit_project_key: Callable[[str | None], str],
    timeline_rows_to_dicts: Callable[..., list[dict[str, Any]]],
    row_to_dict: Callable[[Any], dict[str, Any]],
    owner_directory_item_to_dict: Callable[[Any], dict[str, Any]],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
    owner_directory_governance_warnings: Callable[..., list[dict[str, Any]]],
    owner_mapping_governance_warnings: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    if limit_recent_audits < 1 or limit_recent_audits > 1000:
        return {"status": "error", "error": "limit_recent_audits musi byÄ‡ w zakresie 1..1000"}
    if max_groups < 1 or max_groups > 200:
        return {"status": "error", "error": "max_groups musi byÄ‡ w zakresie 1..200"}
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)
    audit_project_key = owner_catalog_audit_project_key(normalized_project_key)

    health = get_owner_catalog_health_payload(
        conn,
        project_key=normalized_project_key,
        scope_code=normalized_scope_code,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
        owner_directory_item_to_dict=owner_directory_item_to_dict,
        owner_role_mapping_to_dict=owner_role_mapping_to_dict,
        owner_directory_governance_warnings=owner_directory_governance_warnings,
        owner_mapping_governance_warnings=owner_mapping_governance_warnings,
    )
    batch_candidates = get_owner_mapping_batch_candidates_payload(
        conn,
        project_key=normalized_project_key,
        scope_code=normalized_scope_code,
        max_groups=max_groups,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
        owner_directory_item_to_dict=owner_directory_item_to_dict,
        owner_role_mapping_to_dict=owner_role_mapping_to_dict,
        owner_directory_governance_warnings=owner_directory_governance_warnings,
        owner_mapping_governance_warnings=owner_mapping_governance_warnings,
    )
    single_audit = get_owner_mapping_repair_audit_payload(
        conn,
        project_key=normalized_project_key,
        limit=limit_recent_audits,
        owner_catalog_audit_project_key=owner_catalog_audit_project_key,
        timeline_rows_to_dicts=timeline_rows_to_dicts,
        row_to_dict=row_to_dict,
    )
    governance_history = get_owner_catalog_governance_history_payload(
        conn,
        project_key=normalized_project_key,
        limit=limit_recent_audits,
        owner_catalog_audit_project_key=owner_catalog_audit_project_key,
        timeline_rows_to_dicts=timeline_rows_to_dicts,
        row_to_dict=row_to_dict,
    )

    rows = conn.execute(
        """
        SELECT * FROM timeline_events
        WHERE project_key = ?
          AND event_type = 'project.note_recorded'
          AND payload_json LIKE '%owner_mapping_bulk_repair%'
        ORDER BY event_time DESC, id DESC
        LIMIT ?
        """,
        (audit_project_key, int(limit_recent_audits)),
    ).fetchall()
    count_rows = conn.execute(
        """
        SELECT
            COUNT(*) AS total_count,
            SUM(CASE WHEN payload_json LIKE '%"status": "completed"%' THEN 1 ELSE 0 END) AS completed_count,
            SUM(CASE WHEN payload_json LIKE '%"status": "partial"%' THEN 1 ELSE 0 END) AS partial_count,
            SUM(CASE WHEN payload_json LIKE '%"status": "failed"%' THEN 1 ELSE 0 END) AS failed_count
        FROM timeline_events
        WHERE project_key = ?
          AND event_type = 'project.note_recorded'
          AND payload_json LIKE '%owner_mapping_bulk_repair%'
        """,
        (audit_project_key,),
    ).fetchone()

    bulk_items = timeline_rows_to_dicts(rows, row_to_dict=row_to_dict)
    counts = row_to_dict(count_rows) if count_rows is not None else {}

    return {
        "status": "ok",
        "filters": {
            "project_key": normalized_project_key,
            "scope_code": normalized_scope_code,
            "limit_recent_audits": int(limit_recent_audits),
            "max_groups": int(max_groups),
        },
        "health": health,
        "batch_candidates_summary": {
            "count": int(batch_candidates.get("count") or 0),
            "groups": batch_candidates.get("groups") or [],
        },
        "repair_audit_summary": {
            "single_repair_count": int(single_audit.get("total_count") or 0),
            "bulk_repair_count": int((counts or {}).get("total_count") or 0),
            "bulk_repair_completed_count": int((counts or {}).get("completed_count") or 0),
            "bulk_repair_partial_count": int((counts or {}).get("partial_count") or 0),
            "bulk_repair_failed_count": int((counts or {}).get("failed_count") or 0),
            "recent_single_repairs": single_audit.get("items") or [],
            "recent_bulk_repairs": bulk_items,
        },
        "governance_history_summary": {
            "governance_event_count": int(governance_history.get("total_count") or 0),
            "recent_governance_events": governance_history.get("items") or [],
        },
    }
