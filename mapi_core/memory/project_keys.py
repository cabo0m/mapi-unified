from __future__ import annotations

"""Project key registry and alias helpers.

`project_key` remains the strict memory namespace. Alias expansion is explicit
and is meant for operator-controlled project families, not silent scope merging.
"""

from typing import Any, Callable

PROJECT_KEY_MODES = frozenset({"exact", "aliases"})

DEFAULT_PROJECT_ALIASES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "demo-project": (
        ("demo-project", "canonical", "Primary synthetic demo namespace."),
        ("demo", "short_alias", "Short alias used by public examples."),
    ),
    "sample-research": (
        ("sample-research", "canonical", "Secondary synthetic project namespace."),
        ("research-demo", "short_alias", "Alternative fictional project key."),
    ),
}

BOOTSTRAP_PROJECT_KEY_VALUES: dict[str, tuple[str, ...]] = {
    "demo-project": ("demo-project", "demo"),
    "sample-research": ("sample-research", "research-demo"),
}


def _default_alias_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for canonical_project_key, aliases in DEFAULT_PROJECT_ALIASES.items():
        for alias_project_key, alias_kind, notes in aliases:
            rows.append({
                "alias_project_key": alias_project_key,
                "canonical_project_key": canonical_project_key,
                "alias_kind": alias_kind,
                "status": "active",
                "notes": notes,
            })
    return rows


DEFAULT_ALIAS_ROWS = tuple(_default_alias_rows())
DEFAULT_ALIAS_LOOKUP = {
    row["alias_project_key"]: row
    for row in DEFAULT_ALIAS_ROWS
}
DEFAULT_ALIASES_BY_CANONICAL: dict[str, tuple[dict[str, str], ...]] = {
    canonical_project_key: tuple(
        row for row in DEFAULT_ALIAS_ROWS
        if row["canonical_project_key"] == canonical_project_key
    )
    for canonical_project_key in DEFAULT_PROJECT_ALIASES
}


def _merged_alias_rows(
    conn: Any,
    *,
    canonical_project_key: str | None = None,
) -> list[dict[str, Any]]:
    rows_by_alias: dict[str, dict[str, Any]] = {}
    if canonical_project_key is None:
        for row in DEFAULT_ALIAS_ROWS:
            rows_by_alias[row["alias_project_key"]] = dict(row)
    else:
        for row in DEFAULT_ALIASES_BY_CANONICAL.get(canonical_project_key, ()):
            rows_by_alias[row["alias_project_key"]] = dict(row)

    where_sql = ""
    params: list[Any] = []
    if canonical_project_key is not None:
        where_sql = "WHERE canonical_project_key = ?"
        params.append(canonical_project_key)

    db_rows = conn.execute(
        f"""
        SELECT alias_project_key, canonical_project_key, alias_kind, status, notes, created_at, updated_at
        FROM project_key_aliases
        {where_sql}
        ORDER BY canonical_project_key ASC, alias_project_key ASC
        """,
        params,
    ).fetchall()
    for row in db_rows:
        alias_project_key = str(row["alias_project_key"])
        status = str(row["status"])
        if status == "inactive":
            rows_by_alias.pop(alias_project_key, None)
            continue
        rows_by_alias[alias_project_key] = dict(row)
    return sorted(
        rows_by_alias.values(),
        key=lambda item: (str(item["canonical_project_key"]), str(item["alias_project_key"])),
    )


def normalize_project_key_mode(value: Any, *, normalize_optional_text: Callable[[Any], str | None]) -> str:
    normalized = (normalize_optional_text(value) or "exact").lower()
    if normalized not in PROJECT_KEY_MODES:
        raise ValueError("project_key_mode musi być 'exact' albo 'aliases'")
    return normalized


def ensure_project_key_alias_schema(conn: Any) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS project_key_aliases (
            alias_project_key TEXT PRIMARY KEY,
            canonical_project_key TEXT NOT NULL,
            alias_kind TEXT NOT NULL DEFAULT 'alias',
            status TEXT NOT NULL DEFAULT 'active',
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_key_aliases_canonical ON project_key_aliases(canonical_project_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_key_aliases_status ON project_key_aliases(status)"
    )


def seed_default_project_key_aliases(conn: Any) -> None:
    ensure_project_key_alias_schema(conn)
    for canonical_project_key, aliases in DEFAULT_PROJECT_ALIASES.items():
        for alias_project_key, alias_kind, notes in aliases:
            conn.execute(
                """
                INSERT INTO project_key_aliases
                    (alias_project_key, canonical_project_key, alias_kind, status, notes)
                VALUES (?, ?, ?, 'active', ?)
                ON CONFLICT(alias_project_key) DO NOTHING
                """,
                (alias_project_key, canonical_project_key, alias_kind, notes),
            )


def resolve_canonical_project_key(
    conn: Any,
    project_key: str,
    *,
    normalize_optional_text: Callable[[Any], str | None],
) -> str:
    normalized_project_key = normalize_optional_text(project_key)
    if normalized_project_key is None:
        raise ValueError("project_key cannot be empty")
    ensure_project_key_alias_schema(conn)
    row = conn.execute(
        """
        SELECT canonical_project_key
        FROM project_key_aliases
        WHERE alias_project_key = ? AND status = 'active'
        LIMIT 1
        """,
        (normalized_project_key,),
    ).fetchone()
    if row is None:
        default_row = DEFAULT_ALIAS_LOOKUP.get(normalized_project_key)
        if default_row is not None:
            return str(default_row["canonical_project_key"])
        return normalized_project_key
    return str(row["canonical_project_key"])


def project_key_filter_values(
    conn: Any,
    project_key: str | None,
    *,
    project_key_mode: str = "exact",
    normalize_optional_text: Callable[[Any], str | None],
) -> tuple[list[str] | None, str, str | None]:
    normalized_project_key = normalize_optional_text(project_key)
    if normalized_project_key is None:
        return None, "exact", None

    normalized_mode = normalize_project_key_mode(project_key_mode, normalize_optional_text=normalize_optional_text)
    if normalized_mode == "exact":
        return [normalized_project_key], normalized_mode, normalized_project_key

    canonical_project_key = resolve_canonical_project_key(
        conn,
        normalized_project_key,
        normalize_optional_text=normalize_optional_text,
    )
    rows = _merged_alias_rows(conn, canonical_project_key=canonical_project_key)

    values: list[str] = []
    seen: set[str] = set()
    for value in [canonical_project_key, normalized_project_key] + [str(row["alias_project_key"]) for row in rows]:
        if value not in seen:
            seen.add(value)
            values.append(value)
    return values, normalized_mode, canonical_project_key


def bootstrap_project_key_values(
    conn: Any,
    project_key: str | None,
    *,
    normalize_optional_text: Callable[[Any], str | None],
) -> tuple[str, tuple[str, ...]]:
    normalized_project_key = normalize_optional_text(project_key)
    if normalized_project_key is None:
        return "demo-project", ("demo-project",)

    canonical_project_key = resolve_canonical_project_key(
        conn,
        normalized_project_key,
        normalize_optional_text=normalize_optional_text,
    )
    values = BOOTSTRAP_PROJECT_KEY_VALUES.get(canonical_project_key, (canonical_project_key,))
    seen: set[str] = set()
    ordered_values: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered_values.append(value)
    return canonical_project_key, tuple(ordered_values)


def list_project_key_aliases_payload(
    conn: Any,
    *,
    canonical_project_key: str | None = None,
    include_inactive: bool = False,
    normalize_optional_text: Callable[[Any], str | None],
) -> dict[str, Any]:
    ensure_project_key_alias_schema(conn)
    normalized_canonical = normalize_optional_text(canonical_project_key)
    if normalized_canonical:
        canonical = resolve_canonical_project_key(
            conn,
            normalized_canonical,
            normalize_optional_text=normalize_optional_text,
        )
        normalized_canonical = canonical
    items = _merged_alias_rows(conn, canonical_project_key=normalized_canonical)
    if not include_inactive:
        items = [item for item in items if str(item.get("status")) == "active"]
    return {
        "count": len(items),
        "items": items,
        "filters": {
            "canonical_project_key": normalized_canonical,
            "include_inactive": bool(include_inactive),
        },
    }


def upsert_project_key_alias_payload(
    conn: Any,
    *,
    canonical_project_key: str,
    alias_project_key: str,
    alias_kind: str | None = "alias",
    status: str | None = "active",
    notes: str | None = None,
    normalize_optional_text: Callable[[Any], str | None],
) -> dict[str, Any]:
    ensure_project_key_alias_schema(conn)
    canonical = normalize_optional_text(canonical_project_key)
    alias = normalize_optional_text(alias_project_key)
    if canonical is None:
        return {"status": "error", "error": "canonical_project_key cannot be empty"}
    if alias is None:
        return {"status": "error", "error": "alias_project_key cannot be empty"}
    normalized_status = (normalize_optional_text(status) or "active").lower()
    if normalized_status not in {"active", "inactive"}:
        return {"status": "error", "error": "status must be active or inactive"}
    normalized_kind = normalize_optional_text(alias_kind) or "alias"
    normalized_notes = normalize_optional_text(notes)
    conn.execute(
        """
        INSERT INTO project_key_aliases
            (alias_project_key, canonical_project_key, alias_kind, status, notes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(alias_project_key) DO UPDATE SET
            canonical_project_key = excluded.canonical_project_key,
            alias_kind = excluded.alias_kind,
            status = excluded.status,
            notes = excluded.notes,
            updated_at = CURRENT_TIMESTAMP
        """,
        (alias, canonical, normalized_kind, normalized_status, normalized_notes),
    )
    conn.commit()
    return {
        "status": "ok",
        "alias": {
            "alias_project_key": alias,
            "canonical_project_key": canonical,
            "alias_kind": normalized_kind,
            "status": normalized_status,
            "notes": normalized_notes,
        },
    }
