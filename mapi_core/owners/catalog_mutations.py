from __future__ import annotations

"""Owner catalog mutation payload helpers."""

import json
from typing import Any, Callable


def upsert_owner_directory_item_payload(
    conn: Any,
    *,
    owner_key: str,
    owner_type: str,
    display_name: str,
    is_active: bool = True,
    routing_metadata_json: str | None = None,
    allow_unsafe_deactivation: bool = False,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    owner_deactivation_guardrail_warnings: Callable[..., list[dict[str, Any]]],
    owner_directory_item_to_dict: Callable[[Any], dict[str, Any]],
    owner_directory_governance_warnings: Callable[..., list[dict[str, Any]]],
    record_project_event: Callable[..., int],
) -> dict[str, Any]:
    normalized_owner_key = normalize_required_text(owner_key, "owner_key")
    normalized_owner_type = normalize_required_text(owner_type, "owner_type")
    normalized_display_name = normalize_required_text(display_name, "display_name")
    normalized_routing_metadata_json = normalize_optional_text(routing_metadata_json)
    if normalized_routing_metadata_json is not None:
        json.loads(normalized_routing_metadata_json)
    now_iso = utc_now_iso()
    preflight_warnings = owner_deactivation_guardrail_warnings(
        conn,
        normalized_owner_key,
        requested_is_active=bool(is_active),
    )
    if preflight_warnings and not bool(allow_unsafe_deactivation):
        first_warning = preflight_warnings[0]
        active_mapping_ids = first_warning.get("active_mapping_ids") or []
        return {
            "status": "error",
            "error": (
                "Unsafe owner deactivation blocked: active owner role mappings still reference "
                f"{normalized_owner_key}; active_mapping_ids={active_mapping_ids}. Pass "
                "allow_unsafe_deactivation=True only after remap/approval."
            ),
        }
    prev_row = conn.execute(
        "SELECT is_active FROM owner_directory_items WHERE owner_key = ?",
        (normalized_owner_key,),
    ).fetchone()
    if prev_row is None:
        change_kind = "created"
    elif not bool(is_active) and bool(prev_row["is_active"]):
        change_kind = "deactivated"
    elif bool(is_active) and not bool(prev_row["is_active"]):
        change_kind = "reactivated"
    else:
        change_kind = "updated"
    conn.execute(
        """
        INSERT INTO owner_directory_items (owner_key, owner_type, display_name, is_active, routing_metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(owner_key) DO UPDATE SET
            owner_type = excluded.owner_type,
            display_name = excluded.display_name,
            is_active = excluded.is_active,
            routing_metadata_json = excluded.routing_metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            normalized_owner_key,
            normalized_owner_type,
            normalized_display_name,
            1 if is_active else 0,
            normalized_routing_metadata_json,
            now_iso,
            now_iso,
        ),
    )
    row = conn.execute("SELECT * FROM owner_directory_items WHERE owner_key = ?", (normalized_owner_key,)).fetchone()
    item = owner_directory_item_to_dict(row)
    warnings = owner_directory_governance_warnings(
        item["owner_key"],
        item["owner_type"],
        normalize_optional_text(item.get("routing_metadata_json")),
        is_active=bool(item.get("is_active")),
    )
    warnings.extend(preflight_warnings)
    audit_event_id = record_project_event(
        conn,
        project_key="global_owner_catalog",
        event_type="project.note_recorded",
        title=f"Owner catalog change: {normalized_owner_key} {change_kind}",
        description=(
            f"owner_key={normalized_owner_key}; change_kind={change_kind}; "
            f"owner_type={normalized_owner_type}; is_active={bool(is_active)}"
        ),
        origin="system",
        tags=["owner_directory_change", change_kind],
        status="completed",
        canonical=True,
        category="owner_directory_change",
        now_fn=utc_now_iso,
    )
    conn.commit()
    return {
        "status": "upserted",
        "owner_directory_item": item,
        "governance_warnings": warnings,
        "governance_warning_count": len(warnings),
        "audit_event": {"id": audit_event_id, "event_type": "project.note_recorded"},
    }


def upsert_owner_role_mapping_payload(
    conn: Any,
    *,
    owner_role: str,
    owner_key: str,
    project_key: str | None = None,
    scope_code: str | None = None,
    is_active: bool = True,
    notes: str | None = None,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    owner_catalog_audit_project_key: Callable[[Any], str],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
    owner_mapping_governance_warnings: Callable[..., list[dict[str, Any]]],
    record_project_event: Callable[..., int],
) -> dict[str, Any]:
    normalized_owner_role = normalize_required_text(owner_role, "owner_role")
    normalized_owner_key = normalize_required_text(owner_key, "owner_key")
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)
    normalized_notes = normalize_optional_text(notes)
    now_iso = utc_now_iso()
    owner_row = conn.execute("SELECT * FROM owner_directory_items WHERE owner_key = ?", (normalized_owner_key,)).fetchone()
    if owner_row is None:
        return {"status": "error", "error": f"Owner directory item not found: {normalized_owner_key}"}
    prev_mapping_row = conn.execute(
        "SELECT is_active FROM owner_role_mappings "
        "WHERE owner_role = ? AND project_key IS ? AND scope_code IS ?",
        (normalized_owner_role, normalized_project_key, normalized_scope_code),
    ).fetchone()
    if prev_mapping_row is None:
        mapping_change_kind = "created"
    elif not bool(is_active) and bool(prev_mapping_row["is_active"]):
        mapping_change_kind = "deactivated"
    elif bool(is_active) and not bool(prev_mapping_row["is_active"]):
        mapping_change_kind = "reactivated"
    else:
        mapping_change_kind = "updated"
    conn.execute(
        """
        INSERT INTO owner_role_mappings (owner_role, owner_key, project_key, scope_code, is_active, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(owner_role, project_key, scope_code) DO UPDATE SET
            owner_key = excluded.owner_key,
            is_active = excluded.is_active,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            normalized_owner_role,
            normalized_owner_key,
            normalized_project_key,
            normalized_scope_code,
            1 if is_active else 0,
            normalized_notes,
            now_iso,
            now_iso,
        ),
    )
    row = conn.execute(
        "SELECT * FROM owner_role_mappings WHERE owner_role = ? AND project_key IS ? AND scope_code IS ?",
        (normalized_owner_role, normalized_project_key, normalized_scope_code),
    ).fetchone()
    mapping_item = owner_role_mapping_to_dict(row)
    warnings = owner_mapping_governance_warnings(
        conn,
        owner_role=mapping_item["owner_role"],
        owner_key=mapping_item["owner_key"],
        project_key=mapping_item.get("project_key"),
        scope_code=mapping_item.get("scope_code"),
        is_active=bool(mapping_item.get("is_active")),
        current_mapping_id=int(mapping_item.get("id") or 0),
    )
    mapping_audit_event_id = record_project_event(
        conn,
        project_key=owner_catalog_audit_project_key(normalized_project_key),
        event_type="project.note_recorded",
        title=f"Owner mapping {mapping_change_kind}: {normalized_owner_role}",
        description=(
            f"owner_key={normalized_owner_key}; owner_role={normalized_owner_role}; "
            f"project_key={normalized_project_key}; scope_code={normalized_scope_code}; "
            f"change_kind={mapping_change_kind}; is_active={bool(is_active)}"
        ),
        origin="system",
        tags=["owner_role_mapping_change", mapping_change_kind],
        status="completed",
        canonical=True,
        category="owner_role_mapping_change",
        now_fn=utc_now_iso,
    )
    conn.commit()
    return {
        "status": "upserted",
        "owner_role_mapping": mapping_item,
        "governance_warnings": warnings,
        "governance_warning_count": len(warnings),
        "audit_event": {"id": mapping_audit_event_id, "event_type": "project.note_recorded"},
    }


def repair_owner_mapping_issue_payload(
    conn: Any,
    *,
    mapping_id: int,
    repair_kind: str,
    target_owner_key: str | None = None,
    owner_type: str | None = None,
    display_name: str | None = None,
    notes: str | None = None,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    owner_catalog_audit_project_key: Callable[[Any], str],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
    owner_directory_item_to_dict: Callable[[Any], dict[str, Any]],
    record_project_event: Callable[..., int],
    timeline_rows_to_dicts: Callable[..., list[dict[str, Any]]],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    normalized_repair_kind = normalize_required_text(repair_kind, "repair_kind")
    normalized_target_owner_key = normalize_optional_text(target_owner_key)
    normalized_owner_type = normalize_optional_text(owner_type) or "team"
    normalized_display_name = normalize_optional_text(display_name)
    normalized_notes = normalize_optional_text(notes)

    mapping_row = conn.execute("SELECT * FROM owner_role_mappings WHERE id = ?", (int(mapping_id),)).fetchone()
    if mapping_row is None:
        return {"status": "error", "error": f"Owner role mapping not found: {mapping_id}"}
    mapping = owner_role_mapping_to_dict(mapping_row)
    current_owner_key = normalize_optional_text(mapping.get("owner_key"))
    if current_owner_key is None:
        return {"status": "error", "error": "Mapping nie ma owner_key"}

    if normalized_repair_kind == "reactivate_owner_target":
        owner_row = conn.execute("SELECT * FROM owner_directory_items WHERE owner_key = ?", (current_owner_key,)).fetchone()
        if owner_row is None:
            return {"status": "error", "error": f"Owner directory item not found: {current_owner_key}"}
        conn.execute(
            "UPDATE owner_directory_items SET is_active = 1, updated_at = ? WHERE owner_key = ?",
            (utc_now_iso(), current_owner_key),
        )
    elif normalized_repair_kind == "remap_to_target":
        if normalized_target_owner_key is None:
            return {"status": "error", "error": "target_owner_key jest wymagany dla remap_to_target"}
        owner_row = conn.execute("SELECT * FROM owner_directory_items WHERE owner_key = ?", (normalized_target_owner_key,)).fetchone()
        if owner_row is None:
            return {"status": "error", "error": f"Owner directory item not found: {normalized_target_owner_key}"}
        owner_item = owner_directory_item_to_dict(owner_row)
        if not bool(owner_item.get("is_active")):
            return {"status": "error", "error": "Nie mozna przepiac mapowania na nieaktywny target"}
        conn.execute(
            "UPDATE owner_role_mappings SET owner_key = ?, notes = COALESCE(?, notes), updated_at = ? WHERE id = ?",
            (normalized_target_owner_key, normalized_notes, utc_now_iso(), int(mapping_id)),
        )
    elif normalized_repair_kind == "create_missing_owner_target":
        create_owner_key = normalized_target_owner_key or current_owner_key
        resolved_display_name = normalized_display_name or create_owner_key.replace("_", " ").title()
        conn.execute(
            """
            INSERT INTO owner_directory_items (owner_key, owner_type, display_name, is_active, routing_metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, 1, NULL, ?, ?)
            ON CONFLICT(owner_key) DO UPDATE SET
                owner_type = excluded.owner_type,
                display_name = excluded.display_name,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (create_owner_key, normalized_owner_type, resolved_display_name, utc_now_iso(), utc_now_iso()),
        )
        if create_owner_key != current_owner_key:
            conn.execute(
                "UPDATE owner_role_mappings SET owner_key = ?, notes = COALESCE(?, notes), updated_at = ? WHERE id = ?",
                (create_owner_key, normalized_notes, utc_now_iso(), int(mapping_id)),
            )
    else:
        return {
            "status": "error",
            "error": "repair_kind musi byc jednym z: reactivate_owner_target, remap_to_target, create_missing_owner_target",
        }

    updated_mapping_row = conn.execute("SELECT * FROM owner_role_mappings WHERE id = ?", (int(mapping_id),)).fetchone()
    updated_mapping = owner_role_mapping_to_dict(updated_mapping_row)
    updated_owner_row = conn.execute(
        "SELECT * FROM owner_directory_items WHERE owner_key = ?",
        (updated_mapping["owner_key"],),
    ).fetchone()
    updated_owner = None if updated_owner_row is None else owner_directory_item_to_dict(updated_owner_row)
    audit_event_id = record_project_event(
        conn,
        project_key=owner_catalog_audit_project_key(updated_mapping.get("project_key")),
        event_type="project.note_recorded",
        title=f"Owner mapping repaired: {updated_mapping.get('owner_role')}",
        description=f"repair_kind={normalized_repair_kind}; owner_key={updated_mapping.get('owner_key')}",
        origin="system",
        tags=["owner_mapping_repair", normalized_repair_kind],
        status="completed",
        canonical=True,
        category="owner_mapping_repair",
        now_fn=utc_now_iso,
    )
    audit_row = conn.execute("SELECT * FROM timeline_events WHERE id = ?", (audit_event_id,)).fetchone()
    audit_item = timeline_rows_to_dicts([audit_row], row_to_dict=row_to_dict)[0] if audit_row is not None else None
    conn.commit()
    return {
        "status": "repaired",
        "repair_kind": normalized_repair_kind,
        "owner_role_mapping": updated_mapping,
        "owner_directory_item": updated_owner,
        "audit_event": audit_item,
    }


def preview_bulk_repair_owner_mappings_payload(
    conn: Any,
    *,
    mapping_ids: list[int],
    repair_kind: str,
    target_owner_key: str | None = None,
    owner_type: str | None = None,
    display_name: str | None = None,
    notes: str | None = None,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
    owner_directory_item_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if not mapping_ids:
        return {"status": "error", "error": "mapping_ids nie moze byc puste"}
    normalized_repair_kind = normalize_required_text(repair_kind, "repair_kind")
    normalized_target_owner_key = normalize_optional_text(target_owner_key)
    normalized_owner_type = normalize_optional_text(owner_type) or "team"
    normalized_display_name = normalize_optional_text(display_name)
    normalized_notes = normalize_optional_text(notes)
    normalized_ids: list[int] = []
    for item in mapping_ids:
        value = int(item)
        if value < 1:
            return {"status": "error", "error": "mapping_ids musza byc dodatnie"}
        if value not in normalized_ids:
            normalized_ids.append(value)

    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for mapping_id in normalized_ids:
        mapping_row = conn.execute("SELECT * FROM owner_role_mappings WHERE id = ?", (int(mapping_id),)).fetchone()
        if mapping_row is None:
            errors.append({"mapping_id": int(mapping_id), "error_type": "FileNotFoundError", "message": f"Owner role mapping not found: {mapping_id}"})
            continue
        mapping = owner_role_mapping_to_dict(mapping_row)
        current_owner_key = normalize_optional_text(mapping.get("owner_key"))
        current_owner_row = (
            None
            if current_owner_key is None
            else conn.execute("SELECT * FROM owner_directory_items WHERE owner_key = ?", (current_owner_key,)).fetchone()
        )
        current_owner = None if current_owner_row is None else owner_directory_item_to_dict(current_owner_row)

        projected_owner_key = current_owner_key
        projected_owner_active = None if current_owner is None else bool(current_owner.get("is_active"))
        action_summary = normalized_repair_kind

        try:
            if normalized_repair_kind == "reactivate_owner_target":
                if current_owner_key is None:
                    raise ValueError("Mapping nie ma owner_key")
                if current_owner is None:
                    raise ValueError(f"Owner directory item not found: {current_owner_key}")
                projected_owner_active = True
                action_summary = f"reactivate {current_owner_key}"
            elif normalized_repair_kind == "remap_to_target":
                if normalized_target_owner_key is None:
                    raise ValueError("target_owner_key jest wymagany dla remap_to_target")
                target_row = conn.execute("SELECT * FROM owner_directory_items WHERE owner_key = ?", (normalized_target_owner_key,)).fetchone()
                if target_row is None:
                    raise ValueError(f"Owner directory item not found: {normalized_target_owner_key}")
                target_item = owner_directory_item_to_dict(target_row)
                if not bool(target_item.get("is_active")):
                    raise ValueError("Nie mozna przepiac mapowania na nieaktywny target")
                projected_owner_key = normalized_target_owner_key
                projected_owner_active = True
                action_summary = f"remap {current_owner_key} -> {normalized_target_owner_key}"
            elif normalized_repair_kind == "create_missing_owner_target":
                create_owner_key = normalized_target_owner_key or current_owner_key
                if create_owner_key is None:
                    raise ValueError("Nie mozna utworzyc targetu bez owner_key")
                projected_owner_key = create_owner_key
                projected_owner_active = True
                action_summary = f"create target {create_owner_key}"
            else:
                raise ValueError("repair_kind musi byc jednym z: reactivate_owner_target, remap_to_target, create_missing_owner_target")
        except Exception as exc:
            errors.append({"mapping_id": int(mapping_id), "error_type": type(exc).__name__, "message": str(exc)})
            continue

        items.append({
            "mapping_id": int(mapping_id),
            "repair_kind": normalized_repair_kind,
            "current_mapping": mapping,
            "current_owner_directory_item": current_owner,
            "projected_owner_key": projected_owner_key,
            "projected_owner_active": projected_owner_active,
            "projected_owner_type": (
                normalized_owner_type
                if normalized_repair_kind == "create_missing_owner_target"
                else (None if current_owner is None else current_owner.get("owner_type"))
            ),
            "projected_display_name": (
                normalized_display_name
                if normalized_repair_kind == "create_missing_owner_target"
                else (None if current_owner is None else current_owner.get("display_name"))
            ),
            "action_summary": action_summary,
            "notes": normalized_notes,
        })

    return {
        "status": "ok",
        "repair_kind": normalized_repair_kind,
        "requested_count": len(normalized_ids),
        "preview_count": len(items),
        "error_count": len(errors),
        "can_execute": len(items) > 0 and len(errors) == 0,
        "items": items,
        "errors": errors,
    }


def bulk_repair_owner_mappings_payload(
    *,
    mapping_ids: list[int],
    repair_kind: str,
    target_owner_key: str | None = None,
    owner_type: str | None = None,
    display_name: str | None = None,
    notes: str | None = None,
    normalize_required_text: Callable[[Any, str], str],
    utc_now_iso: Callable[[], str],
    owner_catalog_audit_project_key: Callable[[Any], str],
    repair_owner_mapping_issue: Callable[..., dict[str, Any]],
    get_db_connection: Callable[[], Any],
    record_project_event: Callable[..., int],
    timeline_rows_to_dicts: Callable[..., list[dict[str, Any]]],
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if not mapping_ids:
        return {"status": "error", "error": "mapping_ids nie moze byc puste"}
    normalized_ids: list[int] = []
    for item in mapping_ids:
        value = int(item)
        if value < 1:
            return {"status": "error", "error": "mapping_ids musza byc dodatnie"}
        if value not in normalized_ids:
            normalized_ids.append(value)

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for mapping_id in normalized_ids:
        repair_result = repair_owner_mapping_issue(
            mapping_id=mapping_id,
            repair_kind=repair_kind,
            target_owner_key=target_owner_key,
            owner_type=owner_type,
            display_name=display_name,
            notes=notes,
        )
        if repair_result.get("status") == "error":
            errors.append({
                "mapping_id": int(mapping_id),
                "error_type": "ValueError",
                "message": repair_result.get("error", ""),
            })
        else:
            results.append(repair_result)

    if errors and results:
        status = "partial"
    elif errors:
        status = "failed"
    else:
        status = "completed"

    normalized_repair_kind = normalize_required_text(repair_kind, "repair_kind")
    audit_project_key = owner_catalog_audit_project_key((results[0].get("owner_role_mapping") or {}).get("project_key") if results else None)
    conn = get_db_connection()
    try:
        audit_event_id = record_project_event(
            conn,
            project_key=audit_project_key,
            event_type="project.note_recorded",
            title=f"Owner mapping bulk repair: {normalized_repair_kind}",
            description=f"status={status}; repaired={len(results)}; errors={len(errors)}",
            origin="system",
            tags=["owner_mapping_bulk_repair", normalized_repair_kind, status],
            status=status,
            canonical=True,
            category="owner_mapping_bulk_repair",
            now_fn=utc_now_iso,
        )
        audit_row = conn.execute("SELECT * FROM timeline_events WHERE id = ?", (audit_event_id,)).fetchone()
        audit_item = timeline_rows_to_dicts([audit_row], row_to_dict=row_to_dict)[0] if audit_row is not None else None
        conn.commit()
    finally:
        conn.close()

    return {
        "status": status,
        "repair_kind": normalized_repair_kind,
        "requested_count": len(normalized_ids),
        "repaired_count": len(results),
        "error_count": len(errors),
        "results": results,
        "errors": errors,
        "audit_event": audit_item,
    }


def rollout_owner_catalog_to_project_payload(
    conn: Any,
    *,
    project_key: str,
    mappings: list[dict[str, Any]],
    dry_run: bool = False,
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    owner_catalog_audit_project_key: Callable[[Any], str],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
    record_project_event: Callable[..., int],
) -> dict[str, Any]:
    normalized_project_key = normalize_required_text(project_key, "project_key")
    if not mappings:
        return {"status": "error", "error": "mappings nie moze byc puste"}

    rolled_out: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for idx, mapping_spec in enumerate(mappings):
        owner_role = normalize_optional_text(str(mapping_spec.get("owner_role") or ""))
        owner_key = normalize_optional_text(str(mapping_spec.get("owner_key") or ""))
        notes = normalize_optional_text(str(mapping_spec.get("notes") or ""))

        if not owner_role or not owner_key:
            errors.append({"index": idx, "spec": mapping_spec, "error": "owner_role i owner_key sa wymagane"})
            continue

        target_row = conn.execute(
            "SELECT * FROM owner_directory_items WHERE owner_key = ?",
            (owner_key,),
        ).fetchone()
        if target_row is None:
            errors.append({"index": idx, "owner_role": owner_role, "owner_key": owner_key, "error": f"Target '{owner_key}' nie istnieje w katalogu"})
            continue
        if not bool(target_row["is_active"]):
            errors.append({"index": idx, "owner_role": owner_role, "owner_key": owner_key, "error": f"Target '{owner_key}' jest nieaktywny"})
            continue

        existing = conn.execute(
            "SELECT * FROM owner_role_mappings WHERE owner_role = ? AND project_key = ?",
            (owner_role, normalized_project_key),
        ).fetchone()
        if existing is not None:
            existing_dict = owner_role_mapping_to_dict(existing)
            if existing_dict.get("owner_key") == owner_key:
                skipped.append({"owner_role": owner_role, "owner_key": owner_key, "reason": "identical_mapping_exists"})
                continue

        if dry_run:
            rolled_out.append({"owner_role": owner_role, "owner_key": owner_key, "dry_run": True})
            continue

        conn.execute(
            """
            INSERT INTO owner_role_mappings (owner_role, owner_key, project_key, is_active, notes, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(owner_role, project_key, scope_code) DO UPDATE SET
                owner_key = excluded.owner_key,
                is_active = 1,
                notes = COALESCE(excluded.notes, notes),
                updated_at = excluded.updated_at
            """,
            (owner_role, owner_key, normalized_project_key, notes, utc_now_iso(), utc_now_iso()),
        )
        rolled_out.append({"owner_role": owner_role, "owner_key": owner_key})

    if not dry_run and rolled_out:
        record_project_event(
            conn,
            project_key=owner_catalog_audit_project_key(normalized_project_key),
            event_type="project.note_recorded",
            title=f"Owner catalog rollout: {normalized_project_key}",
            description=f"Rolled out {len(rolled_out)} mappings to project '{normalized_project_key}'",
            origin="system",
            tags=["owner_catalog_rollout", normalized_project_key],
            status="completed",
            canonical=True,
            category="owner_catalog_rollout",
            now_fn=utc_now_iso,
        )
        conn.commit()

    status = "dry_run" if dry_run else ("completed" if not errors else "partial" if rolled_out else "failed")
    return {
        "status": status,
        "project_key": normalized_project_key,
        "dry_run": dry_run,
        "requested_count": len(mappings),
        "rolled_out_count": len(rolled_out),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "rolled_out": rolled_out,
        "skipped": skipped,
        "errors": errors,
    }


def set_owner_target_active_payload(
    conn: Any,
    *,
    owner_key: str,
    is_active: bool,
    reason: str | None = None,
    normalize_required_text: Callable[[Any, str], str],
    utc_now_iso: Callable[[], str],
    owner_catalog_audit_project_key: Callable[[Any], str],
    owner_directory_item_to_dict: Callable[[Any], dict[str, Any]],
    record_project_event: Callable[..., int],
) -> dict[str, Any]:
    normalized_owner_key = normalize_required_text(owner_key, "owner_key")
    row = conn.execute(
        "SELECT * FROM owner_directory_items WHERE owner_key = ?",
        (normalized_owner_key,),
    ).fetchone()
    if row is None:
        return {"status": "error", "error": f"Owner target '{normalized_owner_key}' nie istnieje w katalogu"}
    item = owner_directory_item_to_dict(row)
    old_active = bool(item.get("is_active"))
    new_active = bool(is_active)
    if old_active == new_active:
        return {
            "status": "noop",
            "message": f"Owner target '{normalized_owner_key}' jest juz {'aktywny' if new_active else 'nieaktywny'}",
            "owner_directory_item": item,
        }
    conn.execute(
        "UPDATE owner_directory_items SET is_active = ?, updated_at = ? WHERE owner_key = ?",
        (1 if new_active else 0, utc_now_iso(), normalized_owner_key),
    )
    action = "activated" if new_active else "deactivated"
    audit_description = f"owner_key={normalized_owner_key}; action={action}"
    if reason:
        audit_description += f"; reason={reason.strip()}"
    record_project_event(
        conn,
        project_key=owner_catalog_audit_project_key(None),
        event_type="project.note_recorded",
        title=f"Owner target {action}: {normalized_owner_key}",
        description=audit_description,
        origin="system",
        tags=["owner_target_status_change", action],
        status="completed",
        canonical=True,
        category="owner_target_status_change",
        now_fn=utc_now_iso,
    )
    conn.commit()
    updated_row = conn.execute(
        "SELECT * FROM owner_directory_items WHERE owner_key = ?",
        (normalized_owner_key,),
    ).fetchone()
    return {
        "status": action,
        "owner_key": normalized_owner_key,
        "was_active": old_active,
        "is_active": new_active,
        "owner_directory_item": owner_directory_item_to_dict(updated_row),
    }
