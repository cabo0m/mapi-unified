from __future__ import annotations

"""Pure owner catalog governance warning helpers."""

import json
from typing import Any, Callable

OWNER_KEY_ALLOWED_PREFIXES = ("global_", "project_", "workspace_")
OWNER_KEY_FORBIDDEN_BOOTSTRAP = {"maintainer", "knowledge_curator", "review_team", "project_maintainer"}
OWNER_TYPE_ALLOWED_VALUES = {"team", "person", "alias", "system"}


def owner_key_governance_warnings(owner_key: str) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if owner_key in OWNER_KEY_FORBIDDEN_BOOTSTRAP:
        warnings.append({"kind": "bootstrap_owner_key", "severity": "high", "message": "owner_key must not be a raw owner role"})
    if not owner_key.startswith(OWNER_KEY_ALLOWED_PREFIXES):
        warnings.append({"kind": "invalid_owner_key_format", "severity": "medium", "message": "owner_key should start with global_, project_, or workspace_"})
    if owner_key.lower() != owner_key or " " in owner_key:
        warnings.append({"kind": "invalid_owner_key_format", "severity": "medium", "message": "owner_key should be lowercase snake_case without spaces"})
    return warnings


def owner_metadata_governance_warnings(
    owner_key: str,
    owner_type: str,
    routing_metadata_json: str | None,
    *,
    normalize_optional_text: Callable[[Any], str | None],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if owner_type not in OWNER_TYPE_ALLOWED_VALUES:
        warnings.append({"kind": "invalid_owner_type", "severity": "medium", "message": "owner_type should be one of: team, person, alias, system"})
    if routing_metadata_json is None:
        warnings.append({"kind": "missing_routing_metadata", "severity": "medium", "message": "active owner should have routing_metadata_json"})
        return warnings
    try:
        metadata = json.loads(routing_metadata_json)
    except json.JSONDecodeError:
        warnings.append({"kind": "invalid_routing_metadata_json", "severity": "high", "message": "routing_metadata_json must be valid JSON"})
        return warnings
    if not isinstance(metadata, dict):
        warnings.append({"kind": "invalid_routing_metadata_json", "severity": "high", "message": "routing_metadata_json must decode to an object"})
        return warnings
    if owner_key.startswith("global_"):
        for required_key in ["domain", "tier", "scope"]:
            if normalize_optional_text(metadata.get(required_key)) is None:
                warnings.append({"kind": "missing_routing_metadata", "severity": "medium", "message": f"global owner metadata should include {required_key}"})
        if normalize_optional_text(metadata.get("scope")) != "global":
            warnings.append({"kind": "metadata_scope_mismatch", "severity": "medium", "message": "global owner metadata scope should be global"})
    if owner_key.startswith("project_"):
        for required_key in ["domain", "project_key", "scope"]:
            if normalize_optional_text(metadata.get(required_key)) is None:
                warnings.append({"kind": "missing_routing_metadata", "severity": "medium", "message": f"project owner metadata should include {required_key}"})
        if normalize_optional_text(metadata.get("scope")) != "project":
            warnings.append({"kind": "metadata_scope_mismatch", "severity": "medium", "message": "project owner metadata scope should be project"})
    return warnings


def owner_directory_governance_warnings(
    owner_key: str,
    owner_type: str,
    routing_metadata_json: str | None,
    *,
    is_active: bool,
    normalize_optional_text: Callable[[Any], str | None],
) -> list[dict[str, Any]]:
    normalized_owner_key = normalize_optional_text(owner_key) or ""
    if not bool(is_active) and normalized_owner_key in OWNER_KEY_FORBIDDEN_BOOTSTRAP:
        return []
    warnings = owner_key_governance_warnings(owner_key)
    if is_active:
        warnings.extend(
            owner_metadata_governance_warnings(
                owner_key,
                owner_type,
                routing_metadata_json,
                normalize_optional_text=normalize_optional_text,
            )
        )
    return warnings


def owner_deactivation_guardrail_warnings(
    conn: Any,
    owner_key: str,
    *,
    requested_is_active: bool,
    normalize_optional_text: Callable[[Any], str | None],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    normalized_owner_key = normalize_optional_text(owner_key)
    if normalized_owner_key is None or requested_is_active:
        return warnings
    mapping_rows = conn.execute(
        """
        SELECT * FROM owner_role_mappings
        WHERE owner_key = ? AND is_active = 1
        ORDER BY owner_role ASC, COALESCE(project_key, ''), COALESCE(scope_code, ''), id ASC
        """,
        (normalized_owner_key,),
    ).fetchall()
    mappings = [owner_role_mapping_to_dict(row) for row in mapping_rows]
    if mappings:
        warnings.append({
            "kind": "unsafe_deactivation_candidate",
            "severity": "high",
            "message": "owner target is still used by active owner role mappings",
            "active_mapping_count": len(mappings),
            "active_mapping_ids": [int(item.get("id") or 0) for item in mappings],
            "active_mappings": mappings,
            "recommended_action": "remap active mappings before deactivation",
        })
    return warnings


def owner_mapping_governance_warnings(
    conn: Any,
    *,
    owner_role: str,
    owner_key: str,
    project_key: str | None,
    scope_code: str | None,
    is_active: bool,
    current_mapping_id: int | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    owner_directory_item_to_dict: Callable[[Any], dict[str, Any]],
    owner_role_mapping_to_dict: Callable[[Any], dict[str, Any]],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    normalized_owner_role = normalize_optional_text(owner_role)
    normalized_owner_key = normalize_optional_text(owner_key)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)

    if normalized_owner_key is None:
        warnings.append({"kind": "missing_owner_target", "severity": "high", "message": "owner mapping must point to owner_key"})
        return warnings

    owner_row = conn.execute("SELECT * FROM owner_directory_items WHERE owner_key = ?", (normalized_owner_key,)).fetchone()
    if owner_row is None:
        warnings.append({"kind": "missing_owner_target", "severity": "high", "message": "owner mapping points to missing owner_key"})
    else:
        owner_item = owner_directory_item_to_dict(owner_row)
        if is_active and not bool(owner_item.get("is_active")):
            warnings.append({"kind": "inactive_owner_target", "severity": "high", "message": "active owner mapping points to inactive owner target"})
        warnings.extend(
            owner_directory_governance_warnings(
                str(owner_item.get("owner_key") or ""),
                str(owner_item.get("owner_type") or ""),
                normalize_optional_text(owner_item.get("routing_metadata_json")),
                is_active=bool(owner_item.get("is_active")),
                normalize_optional_text=normalize_optional_text,
            )
        )
        if normalized_project_key is not None:
            metadata_json = normalize_optional_text(owner_item.get("routing_metadata_json"))
            metadata_project_key = None
            if metadata_json is not None:
                try:
                    metadata = json.loads(metadata_json)
                    if isinstance(metadata, dict):
                        metadata_project_key = normalize_optional_text(metadata.get("project_key"))
                except json.JSONDecodeError:
                    metadata_project_key = None
            if (
                str(owner_item.get("owner_key") or "").startswith("project_")
                and metadata_project_key is not None
                and metadata_project_key != normalized_project_key
            ):
                warnings.append({
                    "kind": "project_owner_metadata_mismatch",
                    "severity": "medium",
                    "message": "project owner metadata project_key does not match mapping project_key",
                })

    if is_active and normalized_owner_role is not None:
        rows = conn.execute(
            """
            SELECT * FROM owner_role_mappings
            WHERE owner_role = ?
              AND is_active = 1
              AND COALESCE(project_key, '') = COALESCE(?, '')
              AND COALESCE(scope_code, '') = COALESCE(?, '')
            ORDER BY id ASC
            """,
            (normalized_owner_role, normalized_project_key, normalized_scope_code),
        ).fetchall()
        conflicting = []
        for row in rows:
            mapping = owner_role_mapping_to_dict(row)
            mapping_id = int(mapping.get("id") or 0)
            if current_mapping_id is not None and mapping_id == int(current_mapping_id):
                continue
            if normalize_optional_text(mapping.get("owner_key")) != normalized_owner_key:
                conflicting.append(mapping)
        if conflicting:
            warnings.append({
                "kind": "ambiguous_owner_role_mapping",
                "severity": "high",
                "message": "multiple active mappings for the same owner_role/project/scope point to different targets",
                "conflicting_mapping_ids": [int(item.get("id") or 0) for item in conflicting],
            })

    if is_active and normalized_project_key is None and normalized_scope_code is not None:
        warnings.append({
            "kind": "scope_without_project_mapping",
            "severity": "low",
            "message": "scope-specific mapping without project_key should be intentional",
        })

    return warnings
