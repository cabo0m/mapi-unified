from __future__ import annotations

"""Memory item filtering helpers."""

from typing import Any, Callable


def memory_matches_operational_filters(
    memory: dict[str, Any],
    *,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    normalize_scope_code: Callable[[Any], str | None],
    normalize_optional_text: Callable[[Any], str | None],
    normalize_layer_code: Callable[[Any], str | None],
    normalize_area_code: Callable[[Any], str | None],
) -> bool:
    normalized_scope = normalize_scope_code(scope_code)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_layer = normalize_layer_code(layer_code)
    normalized_area = normalize_area_code(area_code)
    normalized_memory_type = normalize_optional_text(memory_type)
    normalized_tag = normalize_optional_text(tag)
    normalized_text_query = normalize_optional_text(text_query)

    if normalized_scope and memory.get("scope_code") != normalized_scope:
        return False
    if normalized_project_key and memory.get("project_key") != normalized_project_key:
        return False
    if normalized_layer and memory.get("layer_code") != normalized_layer:
        return False
    if normalized_area and memory.get("area_code") != normalized_area:
        return False
    if normalized_memory_type and memory.get("memory_type") != normalized_memory_type:
        return False
    if normalized_tag:
        tags_value = normalize_optional_text(memory.get("tags")) or ""
        if normalized_tag not in tags_value:
            return False
    if normalized_text_query:
        haystack = " ".join(
            [
                normalize_optional_text(memory.get("content")) or "",
                normalize_optional_text(memory.get("summary_short")) or "",
                normalize_optional_text(memory.get("tags")) or "",
            ]
        )
        if normalized_text_query not in haystack:
            return False
    return True
