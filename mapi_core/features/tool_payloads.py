from __future__ import annotations

from typing import Any, Callable

ROLLOUT_MODE_ALIASES = {
    "all": "global",
    "projects": "project",
    "scopes": "scope",
    "projects_and_scopes": "scoped_project",
    "off": "off",
}


def compatibility_feature_flag(flag: dict[str, Any]) -> dict[str, Any]:
    compatibility_flag = dict(flag)
    compatibility_flag["key"] = compatibility_flag.get("flag_key")
    compatibility_flag["enabled"] = bool(int(compatibility_flag.get("is_enabled") or 0))
    compatibility_flag["rollout_scope"] = compatibility_flag.get("allowed_scope_codes")
    compatibility_flag["rollout_project_key"] = compatibility_flag.get("allowed_project_keys")
    compatibility_flag["rollout_mode"] = ROLLOUT_MODE_ALIASES.get(
        str(compatibility_flag.get("rollout_mode") or "off"),
        compatibility_flag.get("rollout_mode"),
    )
    return compatibility_flag


def list_feature_flags_payload(
    conn: Any,
    *,
    feature_flag_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM feature_flags ORDER BY flag_key ASC").fetchall()
    items = [compatibility_feature_flag(feature_flag_to_dict(row)) for row in rows]
    return {"count": len(items), "items": items}


def get_feature_flag_payload(
    conn: Any,
    *,
    flag_key: str,
    get_feature_flag_config: Callable[[Any, str], dict[str, Any]],
) -> dict[str, Any]:
    flag = get_feature_flag_config(conn, flag_key)
    return {"feature_flag": compatibility_feature_flag(flag)}


def upsert_feature_flag_payload(
    conn: Any,
    *,
    flag_key: str,
    is_enabled: bool = True,
    rollout_mode: str = "all",
    allowed_project_keys: str | None = None,
    allowed_scope_codes: str | None = None,
    read_only_mode: bool = False,
    notes: str | None = None,
    normalize_feature_flag_key: Callable[[str], str],
    normalize_rollout_mode: Callable[[str | None], str],
    normalize_csv_tokens: Callable[..., list[str]],
    serialize_csv_tokens: Callable[[list[str]], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    get_feature_flag_config: Callable[[Any, str], dict[str, Any]],
) -> dict[str, Any]:
    normalized_flag_key = normalize_feature_flag_key(flag_key)
    normalized_rollout_mode = normalize_rollout_mode(rollout_mode)
    serialized_project_keys = serialize_csv_tokens(normalize_csv_tokens(allowed_project_keys))
    serialized_scope_codes = serialize_csv_tokens(normalize_csv_tokens(allowed_scope_codes, normalizer=normalize_scope_code))
    normalized_notes = normalize_optional_text(notes)
    updated_at = utc_now_iso()

    conn.execute(
        """
        INSERT INTO feature_flags (
            flag_key, is_enabled, rollout_mode, allowed_project_keys, allowed_scope_codes, read_only_mode, notes, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(flag_key) DO UPDATE SET
            is_enabled = excluded.is_enabled,
            rollout_mode = excluded.rollout_mode,
            allowed_project_keys = excluded.allowed_project_keys,
            allowed_scope_codes = excluded.allowed_scope_codes,
            read_only_mode = excluded.read_only_mode,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            normalized_flag_key,
            1 if bool(is_enabled) else 0,
            normalized_rollout_mode,
            serialized_project_keys,
            serialized_scope_codes,
            1 if bool(read_only_mode) else 0,
            normalized_notes,
            updated_at,
        ),
    )
    conn.commit()
    flag = get_feature_flag_config(conn, normalized_flag_key)
    return {"status": "upserted", "feature_flag": flag}


def evaluate_feature_flag_payload(
    conn: Any,
    *,
    flag_key: str,
    project_key: str | None = None,
    scope_code: str | None = None,
    get_feature_flag_config: Callable[[Any, str], dict[str, Any]],
    evaluate_feature_flag_config: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    flag = get_feature_flag_config(conn, flag_key)
    evaluation = evaluate_feature_flag_config(flag, project_key=project_key, scope_code=scope_code)
    return {
        "feature_flag": compatibility_feature_flag(flag),
        "evaluation": evaluation,
        "enabled": evaluation["enabled"],
        "project_key": evaluation["project_key"],
        "scope_code": evaluation["scope_code"],
        "key": evaluation["flag_key"],
    }


def set_feature_flag_payload(
    conn: Any,
    *,
    key: str,
    enabled: bool,
    rollout_mode: str = "all",
    rollout_scope: str | None = None,
    rollout_project_key: str | None = None,
    description: str | None = None,
    read_only_mode: bool = False,
    normalize_required_text: Callable[[Any, str], str],
    normalize_feature_flag_key: Callable[[str], str],
    normalize_rollout_mode: Callable[[str | None], str],
    normalize_csv_tokens: Callable[..., list[str]],
    serialize_csv_tokens: Callable[[list[str]], str | None],
    normalize_scope_code: Callable[[Any], str | None],
    normalize_optional_text: Callable[[Any], str | None],
    utc_now_iso: Callable[[], str],
    get_feature_flag_config: Callable[[Any, str], dict[str, Any]],
) -> dict[str, Any]:
    legacy_mode = normalize_required_text(rollout_mode, "rollout_mode").lower().replace("-", "_").replace(" ", "_")
    rollout_mode_map = {
        "off": "off",
        "global": "all",
        "all": "all",
        "scope": "scopes",
        "scopes": "scopes",
        "project": "projects",
        "projects": "projects",
        "scoped_project": "projects_and_scopes",
        "projects_and_scopes": "projects_and_scopes",
        "read_only": "all",
    }
    normalized_rollout_mode = rollout_mode_map.get(legacy_mode)
    if normalized_rollout_mode is None:
        return {
            "status": "error",
            "error": 'rollout_mode must be one of: off, global, all, scope, scopes, project, projects, scoped_project, projects_and_scopes, read_only',
        }

    effective_read_only = bool(read_only_mode or legacy_mode == "read_only")
    result = upsert_feature_flag_payload(
        conn,
        flag_key=key,
        is_enabled=enabled,
        rollout_mode=normalized_rollout_mode,
        allowed_project_keys=rollout_project_key,
        allowed_scope_codes=rollout_scope,
        read_only_mode=effective_read_only,
        notes=description,
        normalize_feature_flag_key=normalize_feature_flag_key,
        normalize_rollout_mode=normalize_rollout_mode,
        normalize_csv_tokens=normalize_csv_tokens,
        serialize_csv_tokens=serialize_csv_tokens,
        normalize_scope_code=normalize_scope_code,
        normalize_optional_text=normalize_optional_text,
        utc_now_iso=utc_now_iso,
        get_feature_flag_config=get_feature_flag_config,
    )
    return {"status": "updated", "feature_flag": compatibility_feature_flag(result["feature_flag"])}
