from __future__ import annotations

"""Feature-flag normalization, evaluation, and guard helpers."""

from typing import Any, Callable

FEATURE_FLAG_ROLLOUT_MODES = {"off", "all", "projects", "scopes", "projects_and_scopes"}


def normalize_feature_flag_key(flag_key: str, *, normalize_required_text: Callable[[Any, str], str]) -> str:
    return normalize_required_text(flag_key, "flag_key").lower().replace("-", "_").replace(" ", "_")


def normalize_rollout_mode(rollout_mode: str | None, *, normalize_optional_text: Callable[[Any], str | None]) -> str:
    value = normalize_optional_text(rollout_mode) or "all"
    value = value.lower().replace("-", "_").replace(" ", "_")
    if value not in FEATURE_FLAG_ROLLOUT_MODES:
        raise ValueError(f"rollout_mode musi być jednym z: {', '.join(sorted(FEATURE_FLAG_ROLLOUT_MODES))}")
    return value


def normalize_csv_tokens(
    value: str | None,
    *,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_required_text: Callable[[Any, str], str],
    normalizer=None,
) -> list[str]:
    normalized_value = normalize_optional_text(value)
    if normalized_value is None:
        return []
    items: list[str] = []
    seen: set[str] = set()
    for raw_part in normalized_value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        token = normalizer(part) if normalizer is not None else normalize_required_text(part, "csv_token")
        if token not in seen:
            seen.add(token)
            items.append(token)
    return items


def serialize_csv_tokens(tokens: list[str]) -> str | None:
    return None if not tokens else ",".join(tokens)


def feature_flag_to_dict(
    row: Any,
    *,
    row_to_dict: Callable[[Any], dict[str, Any]],
    cross_project_flag_key: str,
    normalize_rollout_mode_func: Callable[[str | None], str],
    normalize_feature_flag_key_func: Callable[[str], str],
) -> dict[str, Any]:
    if row is None:
        return {
            "flag_key": cross_project_flag_key,
            "is_enabled": 0,
            "rollout_mode": "off",
            "allowed_project_keys": None,
            "allowed_scope_codes": None,
            "read_only_mode": 0,
            "notes": "Implicit default rollout disabled",
            "updated_at": None,
            "is_implicit_default": True,
        }
    item = row_to_dict(row)
    item["is_enabled"] = int(item.get("is_enabled") or 0)
    item["read_only_mode"] = int(item.get("read_only_mode") or 0)
    item["rollout_mode"] = normalize_rollout_mode_func(item.get("rollout_mode"))
    item["flag_key"] = normalize_feature_flag_key_func(item.get("flag_key"))
    item["is_implicit_default"] = False
    return item


def get_feature_flag_config(
    conn: Any,
    flag_key: str,
    *,
    normalize_feature_flag_key_func: Callable[[str], str],
    feature_flag_to_dict_func: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    normalized_flag_key = normalize_feature_flag_key_func(flag_key)
    row = conn.execute("SELECT * FROM feature_flags WHERE flag_key = ?", (normalized_flag_key,)).fetchone()
    item = feature_flag_to_dict_func(row)
    item["flag_key"] = normalized_flag_key
    return item


def evaluate_feature_flag_config(
    flag: dict[str, Any],
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    normalize_rollout_mode_func: Callable[[str | None], str],
    normalize_csv_tokens_func: Callable[..., list[str]],
) -> dict[str, Any]:
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)
    rollout_mode = normalize_rollout_mode_func(flag.get("rollout_mode"))
    allowed_project_keys = normalize_csv_tokens_func(flag.get("allowed_project_keys"))
    allowed_scope_codes = normalize_csv_tokens_func(flag.get("allowed_scope_codes"), normalizer=normalize_scope_code)
    is_enabled = bool(int(flag.get("is_enabled") or 0))
    read_only_mode = bool(int(flag.get("read_only_mode") or 0))

    matches_project = True if rollout_mode in {"all", "off", "scopes"} else bool(normalized_project_key and normalized_project_key in allowed_project_keys)
    matches_scope = True if rollout_mode in {"all", "off", "projects"} else bool(normalized_scope_code and normalized_scope_code in allowed_scope_codes)

    if not is_enabled:
        enabled = False
        reason = "flag_disabled"
    elif rollout_mode == "off":
        enabled = False
        reason = "rollout_off"
    elif rollout_mode == "all":
        enabled = True
        reason = "rollout_all"
    elif rollout_mode == "projects":
        enabled = matches_project
        reason = "project_allowed" if enabled else "project_not_allowed"
    elif rollout_mode == "scopes":
        enabled = matches_scope
        reason = "scope_allowed" if enabled else "scope_not_allowed"
    else:
        enabled = matches_project and matches_scope
        reason = "project_and_scope_allowed" if enabled else "project_or_scope_not_allowed"

    return {
        "flag_key": flag["flag_key"],
        "enabled": enabled,
        "read_only_mode": read_only_mode,
        "reason": reason,
        "project_key": normalized_project_key,
        "scope_code": normalized_scope_code,
        "rollout_mode": rollout_mode,
        "allowed_project_keys": allowed_project_keys,
        "allowed_scope_codes": allowed_scope_codes,
        "is_implicit_default": bool(flag.get("is_implicit_default")),
    }


def require_feature_flag_write_access(
    conn: Any,
    *,
    flag_key: str,
    project_key: str | None,
    scope_code: str | None,
    operation_name: str,
    get_feature_flag_config_func: Callable[[Any, str], dict[str, Any]],
    evaluate_feature_flag_config_func: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    flag = get_feature_flag_config_func(conn, flag_key)
    evaluation = evaluate_feature_flag_config_func(flag, project_key=project_key, scope_code=scope_code)
    if not evaluation["enabled"]:
        raise ValueError(f"Feature flag {flag_key} blokuje operację {operation_name}: {evaluation['reason']}")
    if evaluation["read_only_mode"]:
        raise ValueError(f"Feature flag {flag_key} jest w trybie read-only. Operacja {operation_name} jest zablokowana")
    return evaluation


def is_simple_feature_active(
    conn: Any,
    flag_key: str,
    *,
    get_feature_flag_config_func: Callable[[Any, str], dict[str, Any]],
    evaluate_feature_flag_config_func: Callable[..., dict[str, Any]],
    project_key: str | None = None,
    scope_code: str | None = None,
) -> bool:
    """Evaluate a boolean gate through the canonical feature-flag contract.

    A missing database row is represented by ``get_feature_flag_config_func``
    as an implicit disabled/off configuration.  Keeping simple gates on the
    same path prevents compatibility callers from inventing a second default.
    """
    flag = get_feature_flag_config_func(conn, flag_key)
    evaluation = evaluate_feature_flag_config_func(
        flag,
        project_key=project_key,
        scope_code=scope_code,
    )
    return bool(evaluation["enabled"])
