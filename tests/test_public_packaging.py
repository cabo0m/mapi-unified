from __future__ import annotations

import sqlite3
from pathlib import Path

from app import db_migrations
from mapi.capabilities import render_capabilities
from mapi.cli import _module_available
from mapi.seed import seed_demo_database


def test_fresh_migration_reaches_current_tail(tmp_path: Path) -> None:
    path = tmp_path / "mapi.db"
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        db_migrations.apply_all_migrations(connection)
        versions = sorted(db_migrations.applied_migration_versions(connection))
    assert versions[-1] == "0041_revocable_service_auth"


def test_demo_seed_is_deterministic_and_repeatable(tmp_path: Path) -> None:
    path = tmp_path / "mapi.db"
    first = seed_demo_database(path)
    second = seed_demo_database(path)
    assert first["status"] == "seeded"
    assert first["projects"] == ["demo-project", "sample-research"]
    assert second["status"] == "already_seeded"
    assert second["memory_ids"] == first["memory_ids"]


def test_capability_document_matches_registry() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "docs" / "CAPABILITIES.md").read_text(encoding="utf-8") == render_capabilities()


def test_missing_parent_package_is_reported_unavailable() -> None:
    assert _module_available("package_that_does_not_exist.child") is False


def test_corpus_json_is_declared_as_package_data() -> None:
    import tomllib
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = metadata["tool"]["setuptools"]["package-data"]
    assert package_data["app.sandman.corpora"] == ["*.json"]
    assert package_data["mapi_core.memory.corpora"] == ["*.json"]
