from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from mapi import legacy_aurora_runtime_config as legacy
from mapi.env import parse_environment_file
from mapi.initialize import render_env
from mapi_capabilities.command_config import CommandCapabilityConfig
from mapi_capabilities.file_config import FileCapabilityConfig
from mapi_capabilities.git_config import GitCapabilityConfig

CONFIG_IMPORT_SCHEMA = "mapi_aurora_config_import.v1"

# Only project-bound host capabilities are eligible for automatic translation.
# Admin deliberately stays explicit in the new instance because its trust model changed.
ALLOWED_PREFIXES = (
    "MAPI_FILES_",
    "MAPI_FILE_",
    "MAPI_GIT_",
    "MAPI_COMMANDS_",
    "MAPI_COMMAND_",
)
FORBIDDEN_KEY_MARKERS = ("AUTH", "TOKEN", "PASSWORD", "SECRET", "PRIVATE_KEY")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _source_document(path: Path) -> dict[str, Any]:
    loaded = legacy.load_config_file(path)
    if not isinstance(loaded, dict):
        raise ValueError("legacy_config_document_invalid")
    return loaded


def _legacy_config_to_env(document: dict[str, Any], source_path: Path) -> dict[str, str]:
    """Call the frozen converter while adapting to its exact historical signature."""
    signature = inspect.signature(legacy.config_to_env)
    positional: list[Any] = []
    keyword: dict[str, Any] = {}
    for index, parameter in enumerate(signature.parameters.values()):
        if index == 0:
            positional.append(document)
            continue
        name = parameter.name.casefold()
        if "base" in name and "dir" in name:
            value: Any = source_path.parent
        elif "config" in name and "path" in name:
            value = source_path
        elif name in {"path", "source_path"}:
            value = source_path
        elif "dir" in name:
            value = source_path.parent
        elif parameter.default is not inspect.Parameter.empty:
            continue
        else:
            raise RuntimeError(f"unsupported_legacy_config_to_env_parameter:{parameter.name}")
        if parameter.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}:
            positional.append(value)
        else:
            keyword[parameter.name] = value
    result = legacy.config_to_env(*positional, **keyword)
    if not isinstance(result, dict):
        raise ValueError("legacy_config_env_invalid")
    return {str(key): str(value) for key, value in result.items()}


def _translated_values(source_path: Path) -> tuple[dict[str, str], list[str], dict[str, Any]]:
    document = _source_document(source_path)
    legacy_env = _legacy_config_to_env(document, source_path)
    translated = {
        key: value
        for key, value in legacy_env.items()
        if any(key.startswith(prefix) for prefix in ALLOWED_PREFIXES)
    }
    forbidden = sorted(
        key
        for key in translated
        if any(marker in key.upper() for marker in FORBIDDEN_KEY_MARKERS)
    )
    if forbidden:
        raise ValueError("forbidden_capability_env_keys:" + ",".join(forbidden))
    warnings: list[str] = []
    admin = document.get("admin")
    if isinstance(admin, dict) and bool(admin.get("enabled")):
        warnings.append("legacy_admin_configuration_not_imported")
    if not translated:
        warnings.append("no_legacy_host_capabilities_configured")
    metadata = {
        "legacy_schema": document.get("schema"),
        "files_enabled": translated.get("MAPI_FILES_ENABLED", "0") in {"1", "true", "True"},
        "git_enabled": translated.get("MAPI_GIT_ENABLED", "0") in {"1", "true", "True"},
        "commands_enabled": translated.get("MAPI_COMMANDS_ENABLED", "0") in {"1", "true", "True"},
        "translated_key_count": len(translated),
    }
    return translated, warnings, metadata


def _target_values(target_env: Path) -> dict[str, str]:
    return parse_environment_file(target_env) if target_env.is_file() else {}


@contextmanager
def _isolated_mapi_environment(values: dict[str, str]) -> Iterator[None]:
    original = dict(os.environ)
    try:
        cleaned = {key: value for key, value in original.items() if not key.startswith("MAPI_")}
        os.environ.clear()
        os.environ.update(cleaned)
        os.environ.update(values)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _validate_capabilities(values: dict[str, str]) -> dict[str, Any]:
    with _isolated_mapi_environment(values):
        files = FileCapabilityConfig.from_env()
        git = GitCapabilityConfig.from_env()
        commands = CommandCapabilityConfig.from_env()
    return {
        "files": {
            "enabled": files.enabled,
            "root_count": len(files.roots),
            "write_enabled": files.write_enabled,
        },
        "git": {
            "enabled": git.enabled,
            "repository_count": len(git.repositories),
            "commit_enabled": git.commit_enabled,
            "stage_enabled": git.stage_enabled,
        },
        "commands": {
            "enabled": commands.enabled,
            "recipe_count": len(commands.recipes),
        },
    }


def _preview_payload(*, source_config: Path, target_env: Path, replace_existing: bool) -> dict[str, Any]:
    if not source_config.is_file():
        return {
            "status": "blocked",
            "schema": CONFIG_IMPORT_SCHEMA,
            "errors": ["source_config_missing"],
            "mutations_performed": 0,
        }
    if not target_env.is_file():
        return {
            "status": "blocked",
            "schema": CONFIG_IMPORT_SCHEMA,
            "errors": ["target_env_missing"],
            "mutations_performed": 0,
        }
    try:
        translated, warnings, metadata = _translated_values(source_config)
    except Exception as exc:
        return {
            "status": "blocked",
            "schema": CONFIG_IMPORT_SCHEMA,
            "errors": [f"legacy_config_invalid:{type(exc).__name__}:{exc}"],
            "mutations_performed": 0,
        }
    target = _target_values(target_env)
    add: list[str] = []
    change: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []
    for key in sorted(translated):
        if key not in target:
            add.append(key)
        elif target[key] == translated[key]:
            unchanged.append(key)
        else:
            change.append(key)
            if not replace_existing:
                conflicts.append(key)
    merged = dict(target)
    for key, value in translated.items():
        if key not in target or replace_existing or target.get(key) == value:
            merged[key] = value
    try:
        validation = _validate_capabilities(merged)
    except Exception as exc:
        return {
            "status": "blocked",
            "schema": CONFIG_IMPORT_SCHEMA,
            "errors": [f"translated_capability_config_invalid:{type(exc).__name__}:{exc}"],
            "mutations_performed": 0,
        }
    binding = {
        "schema": CONFIG_IMPORT_SCHEMA,
        "source_fingerprint": _sha256_bytes(source_config.read_bytes()),
        "target_fingerprint": _sha256_bytes(target_env.read_bytes()),
        "translated_values_fingerprint": _sha256_text(_canonical_json(translated)),
        "replace_existing_capabilities": bool(replace_existing),
        "add_keys": add,
        "change_keys": change,
        "unchanged_keys": unchanged,
        "conflict_keys": conflicts,
        "metadata": metadata,
        "validation": validation,
        "warnings": warnings,
    }
    preview_hash = _sha256_text(_canonical_json(binding))
    status = "blocked" if conflicts else "preview_ready"
    payload = {
        "status": status,
        **binding,
        "preview_hash": preview_hash,
        "errors": ["capability_key_conflicts"] if conflicts else [],
        "mutations_performed": 0,
        "safety": {
            "legacy_runtime_settings_imported": False,
            "legacy_database_path_imported": False,
            "legacy_auth_or_secrets_imported": False,
            "legacy_admin_configuration_imported": False,
            "target_unrelated_env_keys_preserved": True,
        },
    }
    return payload


def preview_aurora_config_import(
    *,
    source_config: str | Path,
    target_env: str | Path,
    replace_existing_capabilities: bool = False,
) -> dict[str, Any]:
    return _preview_payload(
        source_config=Path(source_config).expanduser().resolve(),
        target_env=Path(target_env).expanduser().resolve(),
        replace_existing=replace_existing_capabilities,
    )


def _backup_path(target_env: Path) -> Path:
    root = target_env.parent
    backup_dir = root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return backup_dir / f"mapi-before-aurora-config-{stamp}.env"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        temp.write_text(content, encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def apply_aurora_config_import(
    *,
    source_config: str | Path,
    target_env: str | Path,
    expected_preview_hash: str | None,
    replace_existing_capabilities: bool = False,
) -> dict[str, Any]:
    source_path = Path(source_config).expanduser().resolve()
    target_path = Path(target_env).expanduser().resolve()
    if not expected_preview_hash:
        return {
            "status": "blocked",
            "schema": CONFIG_IMPORT_SCHEMA,
            "errors": ["expected_preview_hash_required"],
            "mutations_performed": 0,
        }
    preview = _preview_payload(
        source_config=source_path,
        target_env=target_path,
        replace_existing=replace_existing_capabilities,
    )
    if preview.get("status") != "preview_ready":
        return preview
    if str(preview["preview_hash"]) != str(expected_preview_hash):
        return {
            "status": "stale_preview",
            "schema": CONFIG_IMPORT_SCHEMA,
            "expected_preview_hash": expected_preview_hash,
            "current_preview_hash": preview["preview_hash"],
            "mutations_performed": 0,
        }
    translated, warnings, metadata = _translated_values(source_path)
    current = _target_values(target_path)
    merged = dict(current)
    for key, value in translated.items():
        if key not in current or replace_existing_capabilities or current.get(key) == value:
            merged[key] = value
    validation = _validate_capabilities(merged)
    backup = _backup_path(target_path)
    shutil.copy2(target_path, backup)
    if _sha256_bytes(backup.read_bytes()) != preview["target_fingerprint"]:
        backup.unlink(missing_ok=True)
        raise RuntimeError("target_env_backup_verification_failed")
    _atomic_write(target_path, render_env(merged))
    written = _target_values(target_path)
    post_validation = _validate_capabilities(written)
    changed_keys = sorted(set(preview["add_keys"]) | set(preview["change_keys"]))
    report = {
        "schema": CONFIG_IMPORT_SCHEMA,
        "status": "completed",
        "source_fingerprint": preview["source_fingerprint"],
        "preview_hash": preview["preview_hash"],
        "backup_path": str(backup),
        "changed_keys": changed_keys,
        "unchanged_keys": preview["unchanged_keys"],
        "metadata": metadata,
        "validation": post_validation,
        "warnings": warnings,
        "safety": preview["safety"],
        "next_actions": ["Restart the MAPI runtime.", "Run `mapi doctor` after restart."],
    }
    report_dir = target_path.parent / "generated"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "latest-aurora-config-import.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report
