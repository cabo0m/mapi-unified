from __future__ import annotations

import json
from pathlib import Path

from mapi.aurora_config_import import apply_aurora_config_import, preview_aurora_config_import
from mapi.initialize import render_env
from mapi.legacy_aurora_runtime_config import default_config_document


def _legacy_config(path: Path, project: Path) -> None:
    document = default_config_document()
    document["database_path"] = "legacy-data/aurora.db"
    document["runtime"]["host"] = "0.0.0.0"
    document["runtime"]["port"] = 9999
    document["files"] = {
        "enabled": True,
        "roots": [{"id": "proj", "name": "Project", "path": str(project)}],
        "write_enabled": True,
        "write_roots": ["proj"],
        "project_roots": {"alpha": ["proj"]},
        "project_write_roots": {"alpha": ["proj"]},
        "max_read_bytes": 123456,
        "max_write_bytes": 65432,
    }
    document["git"] = {
        "enabled": True,
        "repositories": [{"id": "repo", "name": "Repo", "path": str(project)}],
        "max_output_bytes": 120000,
        "timeout_seconds": 7,
        "commit_enabled": True,
        "commit_repositories": ["repo"],
        "project_repositories": {"alpha": ["repo"]},
        "project_commit_repositories": {"alpha": ["repo"]},
        "max_commit_message_chars": 180,
        "stage_enabled": True,
        "stage_repositories": ["repo"],
        "project_stage_repositories": {"alpha": ["repo"]},
        "stage_max_file_bytes": 222222,
    }
    document["commands"] = {
        "enabled": True,
        "max_output_bytes": 100000,
        "default_timeout_seconds": 20,
        "env_allowlist": ["CI"],
        "recipes": [
            {
                "id": "tests",
                "project_key": "alpha",
                "name": "Tests",
                "purpose": "Run fixed tests",
                "argv": ["python", "-c", "print('ok')"],
                "workdir": str(project),
                "timeout_seconds": 15,
                "env": {},
            }
        ],
    }
    document["admin"] = {
        "enabled": True,
        "roots": [str(project)],
        "sql_write_enabled": True,
        "shell_enabled": True,
        "git_push_enabled": True,
        "default_timeout_seconds": 30,
        "max_output_bytes": 100000,
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def _target_env(path: Path, db: Path) -> None:
    values = {
        "MAPI_ROOT": str(path.parent),
        "MAPI_DATA_DIR": str(path.parent / "data"),
        "MAPI_DB_PATH": str(db),
        "MAPI_RUNTIME_HOST": "127.0.0.1",
        "MAPI_RUNTIME_PORT": "8015",
        "MAPI_DISTRIBUTION_NAME": "Aurora",
        "MAPI_REMOTE_AUTH_ENABLED": "false",
    }
    path.write_text(render_env(values), encoding="utf-8")


def test_preview_and_apply_import_only_project_capabilities(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "legacy-config.json"
    target = tmp_path / "instance" / ".env"
    target.parent.mkdir()
    _legacy_config(source, project)
    _target_env(target, target.parent / "data" / "mapi.db")

    preview = preview_aurora_config_import(source_config=source, target_env=target)
    assert preview["status"] == "preview_ready"
    assert preview["metadata"]["files_enabled"] is True
    assert preview["metadata"]["git_enabled"] is True
    assert preview["metadata"]["commands_enabled"] is True
    assert "legacy_admin_configuration_not_imported" in preview["warnings"]
    assert all(key.startswith(("MAPI_FILES_", "MAPI_FILE_", "MAPI_GIT_", "MAPI_COMMANDS_", "MAPI_COMMAND_")) for key in preview["add_keys"])
    assert not any("ADMIN" in key or "AUTH" in key for key in preview["add_keys"])

    stale = apply_aurora_config_import(
        source_config=source, target_env=target, expected_preview_hash="wrong"
    )
    assert stale["status"] == "stale_preview"

    result = apply_aurora_config_import(
        source_config=source, target_env=target, expected_preview_hash=preview["preview_hash"]
    )
    assert result["status"] == "completed"
    assert Path(result["backup_path"]).is_file()
    written = target.read_text(encoding="utf-8")
    assert "MAPI_RUNTIME_HOST=127.0.0.1" in written
    assert "MAPI_RUNTIME_PORT=8015" in written
    assert "MAPI_DISTRIBUTION_NAME=Aurora" in written
    assert "MAPI_FILES_ENABLED=1" in written
    assert "MAPI_GIT_ENABLED=1" in written
    assert "MAPI_COMMANDS_ENABLED=1" in written
    assert "MAPI_ADMIN_" not in written
    assert "0.0.0.0" not in written
    assert "9999" not in written
    assert result["validation"]["files"]["root_count"] == 1
    assert result["validation"]["git"]["repository_count"] == 1
    assert result["validation"]["commands"]["recipe_count"] == 1


def test_conflict_blocks_unless_replace_is_explicit(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "legacy-config.json"
    target = tmp_path / "instance" / ".env"
    target.parent.mkdir()
    _legacy_config(source, project)
    _target_env(target, target.parent / "data" / "mapi.db")
    target.write_text(target.read_text(encoding="utf-8") + "MAPI_FILES_ENABLED=0\n", encoding="utf-8")

    blocked = preview_aurora_config_import(source_config=source, target_env=target)
    assert blocked["status"] == "blocked"
    assert "MAPI_FILES_ENABLED" in blocked["conflict_keys"]

    preview = preview_aurora_config_import(
        source_config=source, target_env=target, replace_existing_capabilities=True
    )
    assert preview["status"] == "preview_ready"
    assert "MAPI_FILES_ENABLED" in preview["change_keys"]
    result = apply_aurora_config_import(
        source_config=source,
        target_env=target,
        expected_preview_hash=preview["preview_hash"],
        replace_existing_capabilities=True,
    )
    assert result["status"] == "completed"
    assert "MAPI_FILES_ENABLED=1" in target.read_text(encoding="utf-8")


def test_preview_does_not_mutate_source_or_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "legacy-config.json"
    target = tmp_path / "instance" / ".env"
    target.parent.mkdir()
    _legacy_config(source, project)
    _target_env(target, target.parent / "data" / "mapi.db")
    source_before = source.read_bytes()
    target_before = target.read_bytes()
    preview = preview_aurora_config_import(source_config=source, target_env=target)
    assert preview["status"] == "preview_ready"
    assert source.read_bytes() == source_before
    assert target.read_bytes() == target_before
    assert preview["safety"]["legacy_auth_or_secrets_imported"] is False
    assert preview["safety"]["legacy_admin_configuration_imported"] is False
