from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from mapi_core.features import flag_helpers


def _normalize_required_text(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def _normalize_optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_scope_code(value: Any) -> str | None:
    normalized = _normalize_optional_text(value)
    return normalized.lower() if normalized is not None else None


def _normalize_feature_flag_key(value: str) -> str:
    return flag_helpers.normalize_feature_flag_key(
        value,
        normalize_required_text=_normalize_required_text,
    )


def _normalize_rollout_mode(value: str | None) -> str:
    return flag_helpers.normalize_rollout_mode(
        value,
        normalize_optional_text=_normalize_optional_text,
    )


def _normalize_csv_tokens(value: str | None, *, normalizer=None) -> list[str]:
    return flag_helpers.normalize_csv_tokens(
        value,
        normalize_optional_text=_normalize_optional_text,
        normalize_required_text=_normalize_required_text,
        normalizer=normalizer,
    )


def _feature_flag_to_dict(row: Any) -> dict[str, Any]:
    return flag_helpers.feature_flag_to_dict(
        row,
        row_to_dict=dict,
        cross_project_flag_key="cross_project_knowledge_layer",
        normalize_rollout_mode_func=_normalize_rollout_mode,
        normalize_feature_flag_key_func=_normalize_feature_flag_key,
    )


def _get_feature_flag_config(conn: sqlite3.Connection, flag_key: str) -> dict[str, Any]:
    return flag_helpers.get_feature_flag_config(
        conn,
        flag_key,
        normalize_feature_flag_key_func=_normalize_feature_flag_key,
        feature_flag_to_dict_func=_feature_flag_to_dict,
    )


def _evaluate_feature_flag_config(
    flag: dict[str, Any],
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
) -> dict[str, Any]:
    return flag_helpers.evaluate_feature_flag_config(
        flag,
        project_key=project_key,
        scope_code=scope_code,
        normalize_optional_text=_normalize_optional_text,
        normalize_scope_code=_normalize_scope_code,
        normalize_rollout_mode_func=_normalize_rollout_mode,
        normalize_csv_tokens_func=_normalize_csv_tokens,
    )


@pytest.fixture
def flag_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE feature_flags (
            flag_key TEXT PRIMARY KEY,
            is_enabled INTEGER NOT NULL,
            rollout_mode TEXT NOT NULL,
            allowed_project_keys TEXT,
            allowed_scope_codes TEXT,
            read_only_mode INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            updated_at TEXT
        )
        """
    )
    try:
        yield conn
    finally:
        conn.close()


def _insert_flag(
    conn: sqlite3.Connection,
    flag_key: str,
    *,
    enabled: bool,
    rollout_mode: str,
    projects: str | None = None,
    scopes: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO feature_flags (
            flag_key, is_enabled, rollout_mode,
            allowed_project_keys, allowed_scope_codes
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (flag_key, int(enabled), rollout_mode, projects, scopes),
    )


def _evaluate_from_db(
    conn: sqlite3.Connection,
    flag_key: str,
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = _get_feature_flag_config(conn, flag_key)
    evaluation = _evaluate_feature_flag_config(
        config,
        project_key=project_key,
        scope_code=scope_code,
    )
    return config, evaluation


def test_missing_database_row_is_implicitly_disabled(flag_db: sqlite3.Connection) -> None:
    config, evaluation = _evaluate_from_db(flag_db, "declared_in_code_only")

    assert config["flag_key"] == "declared_in_code_only"
    assert config["is_implicit_default"] is True
    assert config["is_enabled"] == 0
    assert config["rollout_mode"] == "off"
    assert evaluation["enabled"] is False
    assert evaluation["reason"] == "flag_disabled"


@pytest.mark.parametrize(
    ("enabled", "rollout_mode", "expected", "reason"),
    [
        (True, "all", True, "rollout_all"),
        (False, "all", False, "flag_disabled"),
        (True, "off", False, "rollout_off"),
    ],
)
def test_global_enabled_disabled_and_off_contract(
    flag_db: sqlite3.Connection,
    enabled: bool,
    rollout_mode: str,
    expected: bool,
    reason: str,
) -> None:
    _insert_flag(flag_db, "global_contract", enabled=enabled, rollout_mode=rollout_mode)

    config, evaluation = _evaluate_from_db(flag_db, "global_contract")

    assert config["is_implicit_default"] is False
    assert evaluation["enabled"] is expected
    assert evaluation["reason"] == reason


def test_scoped_rollout_requires_project_and_scope_match(flag_db: sqlite3.Connection) -> None:
    _insert_flag(
        flag_db,
        "scoped_contract",
        enabled=True,
        rollout_mode="projects_and_scopes",
        projects="mapi",
        scopes="project",
    )

    _, matching = _evaluate_from_db(
        flag_db,
        "scoped_contract",
        project_key="mapi",
        scope_code="project",
    )
    _, wrong_project = _evaluate_from_db(
        flag_db,
        "scoped_contract",
        project_key="other-project",
        scope_code="project",
    )
    _, wrong_scope = _evaluate_from_db(
        flag_db,
        "scoped_contract",
        project_key="mapi",
        scope_code="global",
    )

    assert matching["enabled"] is True
    assert matching["reason"] == "project_and_scope_allowed"
    assert wrong_project["enabled"] is False
    assert wrong_scope["enabled"] is False


def test_database_only_flag_uses_the_same_contract(flag_db: sqlite3.Connection) -> None:
    _insert_flag(flag_db, "database_only", enabled=True, rollout_mode="all")

    config, evaluation = _evaluate_from_db(flag_db, "database_only")

    assert config["is_implicit_default"] is False
    assert evaluation["enabled"] is True


def test_simple_helper_delegates_to_the_canonical_contract(flag_db: sqlite3.Connection) -> None:
    assert flag_helpers.is_simple_feature_active(
        flag_db,
        "declared_in_code_only",
        get_feature_flag_config_func=_get_feature_flag_config,
        evaluate_feature_flag_config_func=_evaluate_feature_flag_config,
    ) is False

    _insert_flag(flag_db, "database_only", enabled=True, rollout_mode="all")

    assert flag_helpers.is_simple_feature_active(
        flag_db,
        "database_only",
        get_feature_flag_config_func=_get_feature_flag_config,
        evaluate_feature_flag_config_func=_evaluate_feature_flag_config,
    ) is True
    _insert_flag(
        flag_db,
        "scoped_without_context",
        enabled=True,
        rollout_mode="projects_and_scopes",
        projects="mapi",
        scopes="project",
    )
    assert flag_helpers.is_simple_feature_active(
        flag_db,
        "scoped_without_context",
        get_feature_flag_config_func=_get_feature_flag_config,
        evaluate_feature_flag_config_func=_evaluate_feature_flag_config,
    ) is False
