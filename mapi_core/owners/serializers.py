from __future__ import annotations

"""Small owner row serializers and ranking helpers."""

from typing import Any, Callable


def owner_directory_item_to_dict(row: Any, *, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    item = row_to_dict(row)
    item["is_active"] = bool(int(item.get("is_active") or 0))
    return item


def owner_role_mapping_to_dict(row: Any, *, row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    item = row_to_dict(row)
    item["id"] = int(item["id"])
    item["is_active"] = bool(int(item.get("is_active") or 0))
    return item


def owner_mapping_rank(
    mapping: dict[str, Any],
    *,
    project_key: str | None,
    scope_code: str | None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
) -> tuple[int, int, int]:
    mapping_project_key = normalize_optional_text(mapping.get("project_key"))
    mapping_scope_code = normalize_scope_code(mapping.get("scope_code"))
    project_score = 2 if mapping_project_key and mapping_project_key == project_key else 0
    scope_score = 1 if mapping_scope_code and mapping_scope_code == scope_code else 0
    specificity = (1 if mapping_project_key else 0) + (1 if mapping_scope_code else 0)
    return (project_score + scope_score, specificity, int(mapping.get("id") or 0))
