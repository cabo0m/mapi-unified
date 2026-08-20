from __future__ import annotations

"""Owner catalog validation helpers."""

import re
from typing import Any, Callable
import json

OWNER_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_:./-]{0,98}[a-z0-9]$|^[a-z0-9]$")
ALLOWED_OWNER_TYPES = frozenset({"team", "person", "service_account", "automated", "external"})


def owner_catalog_audit_project_key(
    project_key: str | None,
    *,
    normalize_optional_text: Callable[[Any], str | None],
) -> str:
    normalized_project_key = normalize_optional_text(project_key)
    return normalized_project_key or "global_owner_catalog"


def validate_owner_key_format(owner_key: str) -> list[str]:
    """Returns list of violation messages (empty = valid)."""
    violations: list[str] = []
    if not owner_key:
        violations.append("owner_key nie mo\u0139\u013de by\xc4\u2021 puste")
        return violations
    if len(owner_key) > 100:
        violations.append(f"owner_key jest za d\u0139\u201augi ({len(owner_key)} znak\u0102\u0142w, max 100)")
    if not OWNER_KEY_PATTERN.match(owner_key):
        violations.append(
            "owner_key musi sk\u0139\u201aada\xc4\u2021 si\xc4\u2122 z ma\u0139\u201aych liter, cyfr, "
            "podkre\u0139\u203ale\u0139\u201e, dwukropk\u0102\u0142w, kropek lub my\u0139\u203alnik\u0102\u0142w "
            "i zaczyna\xc4\u2021/ko\u0139\u201eczy\xc4\u2021 si\xc4\u2122 znakiem alfanumerycznym"
        )
    if owner_key != owner_key.lower():
        violations.append("owner_key musi by\xc4\u2021 w ca\u0139\u201ao\u0139\u203aci ma\u0139\u201aymi literami")
    return violations


def validate_new_owner_target_payload(
    conn: Any,
    *,
    owner_key: str,
    owner_type: str,
    display_name: str,
    routing_metadata_json: str | None = None,
    owner_key_validator: Callable[[str], list[str]],
    allowed_owner_types: frozenset[str],
) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []

    normalized_owner_key = (owner_key or "").strip().lower()
    key_violations = owner_key_validator(normalized_owner_key)
    for msg in key_violations:
        violations.append({"field": "owner_key", "severity": "error", "message": msg})

    normalized_owner_type = (owner_type or "").strip().lower()
    if not normalized_owner_type:
        violations.append({"field": "owner_type", "severity": "error", "message": "owner_type jest wymagany"})
    elif normalized_owner_type not in allowed_owner_types:
        violations.append({
            "field": "owner_type",
            "severity": "error",
            "message": f"owner_type '{normalized_owner_type}' jest niedozwolony. DostÄ™pne: {', '.join(sorted(allowed_owner_types))}",
        })

    normalized_display_name = (display_name or "").strip()
    if not normalized_display_name:
        violations.append({"field": "display_name", "severity": "error", "message": "display_name jest wymagany"})
    elif len(normalized_display_name) < 3:
        violations.append({"field": "display_name", "severity": "warning", "message": "display_name jest bardzo krĂłtki (< 3 znaki)"})

    if routing_metadata_json:
        try:
            json.loads(routing_metadata_json)
        except (ValueError, TypeError):
            violations.append({"field": "routing_metadata_json", "severity": "error", "message": "routing_metadata_json nie jest poprawnym JSON"})

    existing = conn.execute(
        "SELECT owner_key, is_active FROM owner_directory_items WHERE owner_key = ?",
        (normalized_owner_key,),
    ).fetchone()
    if existing is not None:
        existing_active = bool(existing["is_active"])
        violations.append({
            "field": "owner_key",
            "severity": "error",
            "message": f"owner_key '{normalized_owner_key}' juĹĽ istnieje w katalogu (is_active={existing_active})",
        })

    errors = [violation for violation in violations if violation["severity"] == "error"]
    warnings = [violation for violation in violations if violation["severity"] == "warning"]
    return {
        "valid": len(errors) == 0,
        "owner_key": normalized_owner_key,
        "owner_type": normalized_owner_type,
        "display_name": normalized_display_name,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "violations": violations,
        "recommendation": "ok_to_create" if len(errors) == 0 else "fix_errors_before_create",
    }


def validate_project_override_payload(
    conn: Any,
    *,
    project_key: str,
    owner_role: str,
    target_owner_key: str,
    normalize_required_text: Callable[[Any, str], str],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    normalized_project_key = normalize_required_text(project_key, "project_key")
    normalized_owner_role = normalize_required_text(owner_role, "owner_role")
    normalized_target_key = normalize_required_text(target_owner_key, "target_owner_key")

    issues: list[dict[str, Any]] = []
    target_row = conn.execute(
        "SELECT * FROM owner_directory_items WHERE owner_key = ?",
        (normalized_target_key,),
    ).fetchone()
    if target_row is None:
        issues.append({"kind": "error", "code": "target_missing", "message": f"Target '{normalized_target_key}' nie istnieje w katalogu"})
    elif not bool(target_row["is_active"]):
        issues.append({"kind": "error", "code": "target_inactive", "message": f"Target '{normalized_target_key}' jest nieaktywny"})

    global_row = conn.execute(
        "SELECT * FROM owner_role_mappings WHERE owner_role = ? AND project_key IS NULL AND is_active = 1",
        (normalized_owner_role,),
    ).fetchone()
    if global_row is None:
        issues.append({
            "kind": "warning",
            "code": "no_global_mapping",
            "message": f"Brak globalnego mapowania dla roli '{normalized_owner_role}' â€” override nie ma wartoĹ›ci fallbackowej",
        })
    else:
        global_owner_key = global_row["owner_key"]
        if global_owner_key == normalized_target_key:
            issues.append({
                "kind": "warning",
                "code": "redundant_override",
                "message": f"Override wskazuje ten sam target co globalne mapowanie ('{global_owner_key}') â€” nie jest konieczny",
            })

    existing_override = conn.execute(
        "SELECT * FROM owner_role_mappings WHERE owner_role = ? AND project_key = ?",
        (normalized_owner_role, normalized_project_key),
    ).fetchone()
    existing_info = None
    if existing_override is not None:
        existing_info = owner_role_mapping_to_dict(existing_override)
        if existing_info.get("owner_key") == normalized_target_key:
            issues.append({
                "kind": "info",
                "code": "override_identical",
                "message": "Identyczny override juĹĽ istnieje â€” upsert bÄ™dzie noop",
            })

    errors = [issue for issue in issues if issue["kind"] == "error"]
    return {
        "valid": len(errors) == 0,
        "project_key": normalized_project_key,
        "owner_role": normalized_owner_role,
        "target_owner_key": normalized_target_key,
        "error_count": len(errors),
        "issue_count": len(issues),
        "issues": issues,
        "existing_override": existing_info,
        "recommendation": "ok_to_create" if len(errors) == 0 else "fix_errors_before_override",
    }
