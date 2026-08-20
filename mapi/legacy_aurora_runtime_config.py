"""Frozen Aurora 0.2.x JSON configuration parser for one-way migration compatibility.

Do not use this module for new Unified MAPI configuration.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

CONFIG_SCHEMA = "mapi_public_config.v1"
MAX_CONFIG_BYTES = 1024 * 1024

_TOP_KEYS = frozenset({"schema", "memory_policy", "database_path", "project_aliases", "feature_flags", "runtime", "semantic", "files", "git", "commands", "admin"})
_RUNTIME_KEYS = frozenset({"host", "port", "surface_profile", "legacy_flat_surface", "showcase_project_key", "max_in_flight_actions", "writer_mode", "writer_lease_seconds"})
_SEMANTIC_KEYS = frozenset({"provider", "model", "dimensions", "base_url", "api_key_env", "timeout_seconds"})
_FILES_KEYS = frozenset({
    "enabled", "roots", "max_read_bytes", "write_enabled", "write_roots", "max_write_bytes",
    "project_roots", "project_write_roots",
})
_GIT_KEYS = frozenset({
    "enabled", "repositories", "max_output_bytes", "timeout_seconds", "commit_enabled",
    "commit_repositories", "stage_enabled", "stage_repositories", "stage_max_file_bytes", "max_commit_message_chars",
    "project_repositories", "project_commit_repositories", "project_stage_repositories",
})
_COMMAND_KEYS = frozenset({"enabled", "max_output_bytes", "default_timeout_seconds", "recipes"})
_ADMIN_KEYS = frozenset({"enabled", "roots", "shell_enabled", "sql_write_enabled", "git_push_enabled", "default_timeout_seconds", "max_output_bytes"})
_RECIPE_KEYS = frozenset({"name", "purpose", "argv", "workdir", "timeout_seconds", "env_allowlist"})
_SUPPORTED_ENV_KEYS = frozenset({
    "MAPI_MEMORY_POLICY", "MAPI_DB_PATH", "MAPI_RUNTIME_HOST", "MAPI_RUNTIME_PORT",
    "MAPI_SURFACE_PROFILE", "MAPI_LEGACY_FLAT_SURFACE", "MAPI_SHOWCASE_PROJECT_KEY", "MAPI_PROJECT_ALIASES_JSON",
    "MAPI_FEATURE_FLAGS_JSON", "MAPI_MAX_IN_FLIGHT_ACTIONS", "MAPI_WRITER_MODE", "MAPI_WRITER_LEASE_SECONDS",
    "MAPI_EMBEDDING_PROVIDER", "MAPI_EMBEDDING_MODEL", "MAPI_EMBEDDING_DIMENSIONS",
    "MAPI_EMBEDDING_BASE_URL", "MAPI_EMBEDDING_API_KEY_ENV", "MAPI_EMBEDDING_TIMEOUT_SECONDS",
    "MAPI_FILES_ENABLED", "MAPI_FILE_ROOTS", "MAPI_FILE_READ_MAX_BYTES",
    "MAPI_FILE_WRITE_ENABLED", "MAPI_FILE_WRITE_ROOTS", "MAPI_FILE_WRITE_MAX_BYTES",
    "MAPI_FILE_PROJECT_ROOTS_JSON", "MAPI_FILE_PROJECT_WRITE_ROOTS_JSON",
    "MAPI_GIT_ENABLED", "MAPI_GIT_REPOS", "MAPI_GIT_MAX_OUTPUT_BYTES", "MAPI_GIT_TIMEOUT_SECONDS",
    "MAPI_GIT_COMMIT_ENABLED", "MAPI_GIT_COMMIT_REPOS", "MAPI_GIT_STAGE_ENABLED",
    "MAPI_GIT_STAGE_REPOS", "MAPI_GIT_STAGE_MAX_FILE_BYTES", "MAPI_GIT_COMMIT_MAX_MESSAGE_CHARS", "MAPI_GIT_PROJECT_REPOS_JSON",
    "MAPI_GIT_PROJECT_COMMIT_REPOS_JSON", "MAPI_GIT_PROJECT_STAGE_REPOS_JSON",
    "MAPI_COMMANDS_ENABLED", "MAPI_COMMAND_MAX_OUTPUT_BYTES", "MAPI_COMMAND_DEFAULT_TIMEOUT_SECONDS",
    "MAPI_COMMAND_RECIPES_JSON",
})


class RuntimeConfigError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = str(env.get("MAPI_CONFIG_PATH", "")).strip()
    if configured:
        return Path(configured).expanduser().absolute()
    return (Path.home() / ".mapi" / "config.json").absolute()


def default_config_document() -> dict[str, Any]:
    return {
        "schema": CONFIG_SCHEMA,
        "memory_policy": "balanced",
        "database_path": "~/.mapi/mapi.db",
        "project_aliases": {},
        "feature_flags": {},
        "runtime": {"host": "127.0.0.1", "port": 8015, "surface_profile": "standard", "legacy_flat_surface": False, "showcase_project_key": "", "max_in_flight_actions": 16, "writer_mode": "active", "writer_lease_seconds": 30},
        "semantic": {
            "provider": "deterministic_hash",
            "model": "hashing-subword-v1",
            "dimensions": 256,
            "base_url": "",
            "api_key_env": "",
            "timeout_seconds": 30,
        },
        "files": {
            "enabled": False,
            "roots": [],
            "max_read_bytes": 256 * 1024,
            "write_enabled": False,
            "write_roots": [],
            "max_write_bytes": 256 * 1024,
            "project_roots": {},
            "project_write_roots": {},
        },
        "git": {
            "enabled": False,
            "repositories": [],
            "max_output_bytes": 512 * 1024,
            "timeout_seconds": 20,
            "commit_enabled": False,
            "commit_repositories": [],
            "stage_enabled": False,
            "stage_repositories": [],
            "stage_max_file_bytes": 1024 * 1024,
            "max_commit_message_chars": 200,
            "project_repositories": {},
            "project_commit_repositories": {},
            "project_stage_repositories": {},
        },
        "commands": {
            "enabled": False,
            "max_output_bytes": 512 * 1024,
            "default_timeout_seconds": 120,
            "recipes": {},
        },
        "admin": {
            "enabled": False,
            "roots": [],
            "shell_enabled": False,
            "sql_write_enabled": False,
            "git_push_enabled": False,
            "default_timeout_seconds": 120,
            "max_output_bytes": 1024 * 1024,
        },
    }


def _require_object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeConfigError(code)
    return value


def _reject_unknown_keys(value: dict[str, Any], allowed: frozenset[str], code: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RuntimeConfigError(f"{code}:{','.join(unknown)}")


def _string_list(value: Any, code: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeConfigError(code)
    return list(value)


def _validate_recipe_map(value: Any) -> dict[str, list[dict[str, Any]]]:
    mapping = _require_object(value, "config_commands_recipes_must_be_object")
    validated: dict[str, list[dict[str, Any]]] = {}
    for raw_project, raw_items in mapping.items():
        project = str(raw_project or "").strip()
        if not project:
            raise RuntimeConfigError("config_command_project_key_required")
        if not isinstance(raw_items, list):
            raise RuntimeConfigError(f"config_command_project_recipes_must_be_array:{project}")
        items: list[dict[str, Any]] = []
        for index, raw_item in enumerate(raw_items):
            item = _require_object(raw_item, f"config_command_recipe_must_be_object:{project}:{index}")
            _reject_unknown_keys(item, _RECIPE_KEYS, f"config_command_recipe_unknown_keys:{project}:{index}")
            items.append(dict(item))
        validated[project] = items
    return validated


def validate_config_document(document: Any) -> dict[str, Any]:
    root = _require_object(document, "config_root_must_be_object")
    _reject_unknown_keys(root, _TOP_KEYS, "config_unknown_top_level_keys")
    if root.get("schema") != CONFIG_SCHEMA:
        raise RuntimeConfigError("config_schema_invalid")

    result = default_config_document()
    if "memory_policy" in root:
        if not isinstance(root["memory_policy"], str):
            raise RuntimeConfigError("config_memory_policy_must_be_string")
        result["memory_policy"] = root["memory_policy"]
    if "database_path" in root:
        if not isinstance(root["database_path"], str) or not root["database_path"].strip():
            raise RuntimeConfigError("config_database_path_must_be_string")
        result["database_path"] = root["database_path"]
    aliases = _require_object(root.get("project_aliases", {}), "config_project_aliases_must_be_object")
    project_key_re = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    normalized_aliases: dict[str, str] = {}
    for raw_alias, raw_canonical in aliases.items():
        alias = str(raw_alias or "").strip(); canonical = str(raw_canonical or "").strip()
        if not project_key_re.fullmatch(alias) or not project_key_re.fullmatch(canonical) or alias == canonical:
            raise RuntimeConfigError("config_project_alias_invalid")
        normalized_aliases[alias] = canonical
    result["project_aliases"] = normalized_aliases
    raw_flags = _require_object(root.get("feature_flags", {}), "config_feature_flags_must_be_object")
    feature_flags: dict[str, bool] = {}
    flag_re = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    for raw_key, raw_value in raw_flags.items():
        key = str(raw_key or "").strip()
        if not flag_re.fullmatch(key) or not isinstance(raw_value, bool):
            raise RuntimeConfigError("config_feature_flag_invalid")
        feature_flags[key] = raw_value
    result["feature_flags"] = feature_flags

    raw_files_section = root.get("files") if isinstance(root.get("files"), dict) else {}
    raw_git_section = root.get("git") if isinstance(root.get("git"), dict) else {}

    for section_name, allowed in (("runtime", _RUNTIME_KEYS), ("semantic", _SEMANTIC_KEYS), ("files", _FILES_KEYS), ("git", _GIT_KEYS), ("commands", _COMMAND_KEYS), ("admin", _ADMIN_KEYS)):
        if section_name not in root:
            continue
        section = _require_object(root[section_name], f"config_{section_name}_must_be_object")
        _reject_unknown_keys(section, allowed, f"config_{section_name}_unknown_keys")
        result[section_name].update(section)

    runtime = result["runtime"]
    if not isinstance(runtime.get("host"), str) or not runtime["host"].strip():
        raise RuntimeConfigError("config_runtime_host_must_be_string")
    if not isinstance(runtime.get("port"), int) or isinstance(runtime.get("port"), bool):
        raise RuntimeConfigError("config_runtime_port_must_be_integer")
    if str(runtime.get("surface_profile") or "").strip().casefold() not in {"safe", "standard", "agent", "showcase", "admin"}:
        raise RuntimeConfigError("config_surface_profile_invalid")
    if not isinstance(runtime.get("legacy_flat_surface"), bool):
        raise RuntimeConfigError("config_legacy_flat_surface_must_be_boolean")
    if not isinstance(runtime.get("showcase_project_key"), str):
        raise RuntimeConfigError("config_showcase_project_key_must_be_string")
    if not isinstance(runtime.get("max_in_flight_actions"), int) or isinstance(runtime.get("max_in_flight_actions"), bool) or not 1 <= runtime["max_in_flight_actions"] <= 256:
        raise RuntimeConfigError("config_max_in_flight_actions_invalid")
    if str(runtime.get("writer_mode") or "").strip().casefold() not in {"active", "read_only"}:
        raise RuntimeConfigError("config_writer_mode_invalid")
    if not isinstance(runtime.get("writer_lease_seconds"), int) or isinstance(runtime.get("writer_lease_seconds"), bool) or not 5 <= runtime["writer_lease_seconds"] <= 300:
        raise RuntimeConfigError("config_writer_lease_seconds_invalid")

    semantic = result["semantic"]
    if str(semantic.get("provider") or "").strip().casefold() not in {
        "disabled", "deterministic_hash", "openai_compatible"
    }:
        raise RuntimeConfigError("config_semantic_provider_invalid")
    if not isinstance(semantic.get("model"), str):
        raise RuntimeConfigError("config_semantic_model_must_be_string")
    if not isinstance(semantic.get("dimensions"), int) or isinstance(semantic.get("dimensions"), bool):
        raise RuntimeConfigError("config_semantic_dimensions_must_be_integer")
    if semantic["dimensions"] < 0 or semantic["dimensions"] > 65536:
        raise RuntimeConfigError("config_semantic_dimensions_out_of_range")
    if not isinstance(semantic.get("base_url"), str) or not isinstance(semantic.get("api_key_env"), str):
        raise RuntimeConfigError("config_semantic_endpoint_fields_must_be_strings")
    if not isinstance(semantic.get("timeout_seconds"), int) or isinstance(semantic.get("timeout_seconds"), bool):
        raise RuntimeConfigError("config_semantic_timeout_must_be_integer")
    if semantic["timeout_seconds"] < 1 or semantic["timeout_seconds"] > 300:
        raise RuntimeConfigError("config_semantic_timeout_out_of_range")
    if str(semantic["provider"]).casefold() == "deterministic_hash" and semantic["dimensions"] < 32:
        raise RuntimeConfigError("config_semantic_hash_dimensions_too_small")
    if str(semantic["provider"]).casefold() == "openai_compatible":
        if semantic["dimensions"] < 1:
            raise RuntimeConfigError("config_semantic_dimensions_required")
        if not semantic["model"].strip():
            raise RuntimeConfigError("config_semantic_model_required")
        if not semantic["base_url"].strip():
            raise RuntimeConfigError("config_semantic_base_url_required")

    files = result["files"]
    for key in ("enabled", "write_enabled"):
        if not isinstance(files.get(key), bool):
            raise RuntimeConfigError(f"config_files_{key}_must_be_boolean")
    files["roots"] = _string_list(files.get("roots"), "config_file_roots_must_be_string_array")
    files["write_roots"] = _string_list(files.get("write_roots"), "config_file_write_roots_must_be_string_array")
    project_roots = _require_object(files.get("project_roots"), "config_file_project_roots_must_be_object")
    files["project_roots"] = {
        str(project): _string_list(paths, f"config_file_project_roots_must_be_string_array:{project}")
        for project, paths in project_roots.items()
        if str(project).strip()
    }
    if len(files["project_roots"]) != len(project_roots):
        raise RuntimeConfigError("config_file_project_key_required")
    project_write_roots = _require_object(
        files.get("project_write_roots"), "config_file_project_write_roots_must_be_object"
    )
    files["project_write_roots"] = {
        str(project): _string_list(paths, f"config_file_project_write_roots_must_be_string_array:{project}")
        for project, paths in project_write_roots.items()
        if str(project).strip()
    }
    if len(files["project_write_roots"]) != len(project_write_roots):
        raise RuntimeConfigError("config_file_project_write_key_required")
    if "project_write_roots" not in raw_files_section and files.get("write_enabled"):
        write_set = set(files["write_roots"])
        files["project_write_roots"] = {
            project: [path for path in paths if path in write_set]
            for project, paths in files["project_roots"].items()
            if any(path in write_set for path in paths)
        }
    for key in ("max_read_bytes", "max_write_bytes"):
        if not isinstance(files.get(key), int) or isinstance(files.get(key), bool):
            raise RuntimeConfigError(f"config_files_{key}_must_be_integer")

    git = result["git"]
    for key in ("enabled", "commit_enabled", "stage_enabled"):
        if not isinstance(git.get(key), bool):
            raise RuntimeConfigError(f"config_git_{key}_must_be_boolean")
    git["repositories"] = _string_list(git.get("repositories"), "config_git_repositories_must_be_string_array")
    git["commit_repositories"] = _string_list(git.get("commit_repositories"), "config_git_commit_repositories_must_be_string_array")
    git["stage_repositories"] = _string_list(git.get("stage_repositories"), "config_git_stage_repositories_must_be_string_array")
    for key in ("max_output_bytes", "timeout_seconds", "stage_max_file_bytes", "max_commit_message_chars"):
        if not isinstance(git.get(key), int) or isinstance(git.get(key), bool):
            raise RuntimeConfigError(f"config_git_{key}_must_be_integer")
    project_repos = _require_object(git.get("project_repositories"), "config_git_project_repositories_must_be_object")
    git["project_repositories"] = {
        str(project): _string_list(paths, f"config_git_project_repositories_must_be_string_array:{project}")
        for project, paths in project_repos.items()
        if str(project).strip()
    }
    if len(git["project_repositories"]) != len(project_repos):
        raise RuntimeConfigError("config_git_project_key_required")
    project_commit = _require_object(
        git.get("project_commit_repositories"), "config_git_project_commit_repositories_must_be_object"
    )
    git["project_commit_repositories"] = {
        str(project): _string_list(paths, f"config_git_project_commit_repositories_must_be_string_array:{project}")
        for project, paths in project_commit.items()
        if str(project).strip()
    }
    if len(git["project_commit_repositories"]) != len(project_commit):
        raise RuntimeConfigError("config_git_project_commit_key_required")
    project_stage = _require_object(
        git.get("project_stage_repositories"), "config_git_project_stage_repositories_must_be_object"
    )
    git["project_stage_repositories"] = {
        str(project): _string_list(paths, f"config_git_project_stage_repositories_must_be_string_array:{project}")
        for project, paths in project_stage.items()
        if str(project).strip()
    }
    if len(git["project_stage_repositories"]) != len(project_stage):
        raise RuntimeConfigError("config_git_project_stage_key_required")
    if "project_commit_repositories" not in raw_git_section and git.get("commit_enabled"):
        commit_set = set(git["commit_repositories"])
        git["project_commit_repositories"] = {
            project: [path for path in paths if path in commit_set]
            for project, paths in git["project_repositories"].items()
            if any(path in commit_set for path in paths)
        }
    if "project_stage_repositories" not in raw_git_section and git.get("stage_enabled"):
        stage_set = set(git["stage_repositories"])
        git["project_stage_repositories"] = {
            project: [path for path in paths if path in stage_set]
            for project, paths in git["project_repositories"].items()
            if any(path in stage_set for path in paths)
        }

    commands = result["commands"]
    if not isinstance(commands.get("enabled"), bool):
        raise RuntimeConfigError("config_commands_enabled_must_be_boolean")
    for key in ("max_output_bytes", "default_timeout_seconds"):
        if not isinstance(commands.get(key), int) or isinstance(commands.get(key), bool):
            raise RuntimeConfigError(f"config_commands_{key}_must_be_integer")
    commands["recipes"] = _validate_recipe_map(commands.get("recipes"))

    admin = result["admin"]
    for key in ("enabled", "shell_enabled", "sql_write_enabled", "git_push_enabled"):
        if not isinstance(admin.get(key), bool):
            raise RuntimeConfigError(f"config_admin_{key}_must_be_boolean")
    admin["roots"] = _string_list(admin.get("roots"), "config_admin_roots_must_be_string_array")
    for key in ("default_timeout_seconds", "max_output_bytes"):
        if not isinstance(admin.get(key), int) or isinstance(admin.get(key), bool):
            raise RuntimeConfigError(f"config_admin_{key}_must_be_integer")
    if admin["enabled"] and not admin["roots"]:
        raise RuntimeConfigError("config_admin_roots_required")
    if not 1 <= admin["default_timeout_seconds"] <= 900:
        raise RuntimeConfigError("config_admin_default_timeout_out_of_range")
    if not 1024 <= admin["max_output_bytes"] <= 16 * 1024 * 1024:
        raise RuntimeConfigError("config_admin_max_output_bytes_out_of_range")
    return result


def load_config_file(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=False)
    if resolved.is_symlink() or path.is_symlink():
        raise RuntimeConfigError("config_symlink_not_allowed")
    if not resolved.exists():
        return default_config_document()
    if not resolved.is_file():
        raise RuntimeConfigError("config_path_not_file")
    size = resolved.stat().st_size
    if size > MAX_CONFIG_BYTES:
        raise RuntimeConfigError("config_file_too_large")
    try:
        raw = resolved.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeConfigError("config_not_utf8") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeConfigError("config_json_invalid") from exc
    return validate_config_document(document)


def _path_value(value: str, *, base_dir: Path | None = None) -> str:
    try:
        candidate = Path(value).expanduser()
    except RuntimeError as exc:
        raise RuntimeConfigError("home_directory_unavailable") from exc
    if not candidate.is_absolute() and base_dir is not None:
        candidate = base_dir / candidate
    return str(candidate.resolve(strict=False))


def config_to_env(document: dict[str, Any], *, base_dir: Path | None = None) -> dict[str, str]:
    doc = validate_config_document(document)
    files, git, commands, runtime, semantic = doc["files"], doc["git"], doc["commands"], doc["runtime"], doc["semantic"]
    admin = doc["admin"]
    values = {
        "MAPI_MEMORY_POLICY": str(doc["memory_policy"]),
        "MAPI_DB_PATH": _path_value(str(doc["database_path"]), base_dir=base_dir),
        "MAPI_RUNTIME_HOST": str(runtime["host"]),
        "MAPI_RUNTIME_PORT": str(runtime["port"]),
        "MAPI_SURFACE_PROFILE": str(runtime["surface_profile"]),
        "MAPI_LEGACY_FLAT_SURFACE": str(runtime["legacy_flat_surface"]).lower(),
        "MAPI_SHOWCASE_PROJECT_KEY": str(runtime["showcase_project_key"]),
        "MAPI_PROJECT_ALIASES_JSON": json.dumps(doc["project_aliases"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "MAPI_FEATURE_FLAGS_JSON": json.dumps(doc["feature_flags"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "MAPI_MAX_IN_FLIGHT_ACTIONS": str(runtime["max_in_flight_actions"]),
        "MAPI_WRITER_MODE": str(runtime["writer_mode"]),
        "MAPI_WRITER_LEASE_SECONDS": str(runtime["writer_lease_seconds"]),
        "MAPI_EMBEDDING_PROVIDER": str(semantic["provider"]),
        "MAPI_EMBEDDING_MODEL": str(semantic["model"]),
        "MAPI_EMBEDDING_DIMENSIONS": str(semantic["dimensions"]),
        "MAPI_EMBEDDING_BASE_URL": str(semantic["base_url"]),
        "MAPI_EMBEDDING_API_KEY_ENV": str(semantic["api_key_env"]),
        "MAPI_EMBEDDING_TIMEOUT_SECONDS": str(semantic["timeout_seconds"]),
        "MAPI_FILES_ENABLED": str(files["enabled"]).lower(),
        "MAPI_FILE_ROOTS": os.pathsep.join(_path_value(item, base_dir=base_dir) for item in files["roots"]),
        "MAPI_FILE_READ_MAX_BYTES": str(files["max_read_bytes"]),
        "MAPI_FILE_WRITE_ENABLED": str(files["write_enabled"]).lower(),
        "MAPI_FILE_WRITE_ROOTS": os.pathsep.join(_path_value(item, base_dir=base_dir) for item in files["write_roots"]),
        "MAPI_FILE_WRITE_MAX_BYTES": str(files["max_write_bytes"]),
        "MAPI_FILE_PROJECT_ROOTS_JSON": json.dumps(
            {
                project: [_path_value(item, base_dir=base_dir) for item in paths]
                for project, paths in files["project_roots"].items()
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "MAPI_FILE_PROJECT_WRITE_ROOTS_JSON": json.dumps(
            {
                project: [_path_value(item, base_dir=base_dir) for item in paths]
                for project, paths in files["project_write_roots"].items()
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "MAPI_FILE_PROJECT_WRITE_ROOTS_JSON": json.dumps(
            {
                project: [_path_value(item, base_dir=base_dir) for item in paths]
                for project, paths in files["project_write_roots"].items()
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "MAPI_GIT_ENABLED": str(git["enabled"]).lower(),
        "MAPI_GIT_REPOS": os.pathsep.join(_path_value(item, base_dir=base_dir) for item in git["repositories"]),
        "MAPI_GIT_MAX_OUTPUT_BYTES": str(git["max_output_bytes"]),
        "MAPI_GIT_TIMEOUT_SECONDS": str(git["timeout_seconds"]),
        "MAPI_GIT_COMMIT_ENABLED": str(git["commit_enabled"]).lower(),
        "MAPI_GIT_COMMIT_REPOS": os.pathsep.join(_path_value(item, base_dir=base_dir) for item in git["commit_repositories"]),
        "MAPI_GIT_STAGE_ENABLED": str(git["stage_enabled"]).lower(),
        "MAPI_GIT_STAGE_REPOS": os.pathsep.join(
            _path_value(item, base_dir=base_dir) for item in git["stage_repositories"]
        ),
        "MAPI_GIT_STAGE_MAX_FILE_BYTES": str(git["stage_max_file_bytes"]),
        "MAPI_GIT_COMMIT_MAX_MESSAGE_CHARS": str(git["max_commit_message_chars"]),
        "MAPI_GIT_PROJECT_REPOS_JSON": json.dumps(
            {project: [_path_value(item, base_dir=base_dir) for item in paths] for project, paths in git["project_repositories"].items()},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "MAPI_GIT_PROJECT_COMMIT_REPOS_JSON": json.dumps(
            {project: [_path_value(item, base_dir=base_dir) for item in paths] for project, paths in git["project_commit_repositories"].items()},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "MAPI_GIT_PROJECT_STAGE_REPOS_JSON": json.dumps(
            {project: [_path_value(item, base_dir=base_dir) for item in paths] for project, paths in git["project_stage_repositories"].items()},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "MAPI_GIT_PROJECT_COMMIT_REPOS_JSON": json.dumps(
            {project: [_path_value(item, base_dir=base_dir) for item in paths] for project, paths in git["project_commit_repositories"].items()},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "MAPI_GIT_PROJECT_STAGE_REPOS_JSON": json.dumps(
            {project: [_path_value(item, base_dir=base_dir) for item in paths] for project, paths in git["project_stage_repositories"].items()},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "MAPI_COMMANDS_ENABLED": str(commands["enabled"]).lower(),
        "MAPI_COMMAND_MAX_OUTPUT_BYTES": str(commands["max_output_bytes"]),
        "MAPI_COMMAND_DEFAULT_TIMEOUT_SECONDS": str(commands["default_timeout_seconds"]),
        "MAPI_COMMAND_RECIPES_JSON": json.dumps(
            {
                project: [
                    {
                        **recipe,
                        "workdir": _path_value(str(recipe["workdir"]), base_dir=base_dir)
                        if recipe.get("workdir") else recipe.get("workdir"),
                    }
                    for recipe in recipes
                ]
                for project, recipes in commands["recipes"].items()
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "MAPI_ADMIN_ENABLED": str(admin["enabled"]).lower(),
        "MAPI_ADMIN_ROOTS": os.pathsep.join(_path_value(item, base_dir=base_dir) for item in admin["roots"]),
        "MAPI_ADMIN_SHELL_ENABLED": str(admin["shell_enabled"]).lower(),
        "MAPI_ADMIN_SQL_WRITE_ENABLED": str(admin["sql_write_enabled"]).lower(),
        "MAPI_ADMIN_GIT_PUSH_ENABLED": str(admin["git_push_enabled"]).lower(),
        "MAPI_ADMIN_DEFAULT_TIMEOUT_SECONDS": str(admin["default_timeout_seconds"]),
        "MAPI_ADMIN_MAX_OUTPUT_BYTES": str(admin["max_output_bytes"]),
    }
    return values


@dataclass(frozen=True)
class EffectiveRuntimeSettings:
    config_path: Path
    config_exists: bool
    values: dict[str, str]
    env_override_keys: tuple[str, ...]

    def safe_summary(self) -> dict[str, Any]:
        raw_port = self.values.get("MAPI_RUNTIME_PORT", "8015")
        try:
            runtime_port: int | str = int(raw_port)
        except ValueError:
            runtime_port = str(raw_port)
        raw_dimensions = self.values.get("MAPI_EMBEDDING_DIMENSIONS", "256")
        try:
            semantic_dimensions: int | str = int(raw_dimensions)
        except ValueError:
            semantic_dimensions = str(raw_dimensions)
        return {
            "config_path": str(self.config_path),
            "config_exists": self.config_exists,
            "memory_policy": self.values.get("MAPI_MEMORY_POLICY", "balanced"),
            "semantic_provider": self.values.get("MAPI_EMBEDDING_PROVIDER", "deterministic_hash"),
            "semantic_model": self.values.get("MAPI_EMBEDDING_MODEL", "hashing-subword-v1"),
            "semantic_dimensions": semantic_dimensions,
            "runtime_host": self.values.get("MAPI_RUNTIME_HOST", "127.0.0.1"),
            "runtime_port": runtime_port,
            "surface_profile": self.values.get("MAPI_SURFACE_PROFILE", "standard"),
            "legacy_flat_surface": self.values.get("MAPI_LEGACY_FLAT_SURFACE", "false").lower() in {"1", "true", "yes", "on"},
            "showcase_project_configured": bool(self.values.get("MAPI_SHOWCASE_PROJECT_KEY", "").strip()),
            "project_alias_count": len(json.loads(self.values.get("MAPI_PROJECT_ALIASES_JSON", "{}") or "{}")),
            "feature_flag_count": len(json.loads(self.values.get("MAPI_FEATURE_FLAGS_JSON", "{}") or "{}")),
            "max_in_flight_actions": int(self.values.get("MAPI_MAX_IN_FLIGHT_ACTIONS", "16")),
            "writer_mode": self.values.get("MAPI_WRITER_MODE", "active"),
            "writer_lease_seconds": int(self.values.get("MAPI_WRITER_LEASE_SECONDS", "30")),
            "files_enabled": self.values.get("MAPI_FILES_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            "git_enabled": self.values.get("MAPI_GIT_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            "commands_enabled": self.values.get("MAPI_COMMANDS_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            "admin_enabled": self.values.get("MAPI_ADMIN_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            "admin_root_count": len([v for v in self.values.get("MAPI_ADMIN_ROOTS", "").split(os.pathsep) if v]),
            "env_override_keys": list(self.env_override_keys),
        }


def load_effective_settings(environ: Mapping[str, str] | None = None, config_path: Path | None = None) -> EffectiveRuntimeSettings:
    env = dict(os.environ if environ is None else environ)
    path = (config_path or default_config_path(env)).expanduser().absolute()
    config_exists = path.exists()
    document = load_config_file(path)
    values = config_to_env(document, base_dir=path.parent)
    override_keys: list[str] = []
    if not config_exists:
        for key, value in env.items():
            if key in _SUPPORTED_ENV_KEYS:
                values[key] = str(value)
                override_keys.append(key)
    return EffectiveRuntimeSettings(
        config_path=path,
        config_exists=config_exists,
        values=values,
        env_override_keys=tuple(sorted(override_keys)),
    )


def write_config_document(path: Path, document: dict[str, Any], *, allow_create: bool = True) -> Path:
    validated = validate_config_document(document)
    target = path.expanduser().absolute()
    if target.is_symlink() or path.is_symlink():
        raise RuntimeConfigError("config_symlink_not_allowed")
    if target.exists() and not target.is_file():
        raise RuntimeConfigError("config_path_not_file")
    if not target.exists() and not allow_create:
        raise RuntimeConfigError("config_not_found")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(validated, ensure_ascii=False, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        if os.name != "nt":
            os.chmod(target, 0o600)
        try:
            dir_fd = os.open(str(target.parent), os.O_RDONLY)
        except (AttributeError, OSError):
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
    finally:
        temp.unlink(missing_ok=True)
    return target


def write_default_config(path: Path, *, overwrite: bool = False) -> Path:
    target = path.expanduser().absolute()
    if target.exists() and not overwrite:
        raise RuntimeConfigError("config_already_exists")
    return write_config_document(target, default_config_document(), allow_create=True)


def validate_effective_settings(settings: EffectiveRuntimeSettings) -> dict[str, Any]:
    from .command_config import CommandCapabilityConfig
    from .file_config import FileCapabilityConfig
    from .git_config import GitCapabilityConfig
    from .git_service import GitService
    from .memory.semantic import build_embedding_provider
    from .policies import get_memory_policy

    values = settings.values
    errors: list[str] = []
    try:
        from .workshops.access_policy import normalize_profile
        profile = normalize_profile(values.get("MAPI_SURFACE_PROFILE", "standard"))
        if profile == "showcase" and not str(values.get("MAPI_SHOWCASE_PROJECT_KEY", "")).strip():
            errors.append("showcase_project_key_required")
    except ValueError as exc:
        errors.append(str(exc))
    try:
        get_memory_policy(values.get("MAPI_MEMORY_POLICY", "balanced"))
    except ValueError as exc:
        errors.append(str(exc))

    semantic_provider = None
    try:
        semantic_provider = build_embedding_provider(values)
    except (ValueError, RuntimeError) as exc:
        errors.append(f"semantic_runtime_invalid:{exc}")

    host = str(values.get("MAPI_RUNTIME_HOST", "127.0.0.1")).strip().lower()
    if host not in {"127.0.0.1", "::1", "localhost"}:
        errors.append("runtime_host_must_be_loopback")
    try:
        port = int(values.get("MAPI_RUNTIME_PORT", "8015"))
    except ValueError:
        port = -1
    if port < 1 or port > 65535:
        errors.append("runtime_port_out_of_range")

    file_config = FileCapabilityConfig.from_mapping(values)
    errors.extend(file_config.validation_errors())
    git_config = GitCapabilityConfig.from_mapping(values)
    git_errors = git_config.validation_errors()
    errors.extend(git_errors)
    if git_config.enabled and not git_errors:
        try:
            GitService(git_config)
        except Exception as exc:
            errors.append(f"git_runtime_invalid:{type(exc).__name__}:{exc}")
    command_config = CommandCapabilityConfig.from_mapping(values)
    errors.extend(command_config.validation_errors())

    return {
        "status": "valid" if not errors else "invalid",
        "schema": "mapi_public_config_check.v1",
        "summary": settings.safe_summary(),
        "errors": errors,
        "capabilities": {
            "semantic": {
                "enabled": semantic_provider is not None,
                "provider": None if semantic_provider is None else semantic_provider.provider_id,
                "model": None if semantic_provider is None else semantic_provider.model_id,
                "dimensions": None if semantic_provider is None else int(semantic_provider.dimensions),
            },
            "files": {
                "enabled": file_config.enabled,
                "write_enabled": file_config.effective_write_enabled,
                "root_count": len(file_config.roots),
                "project_bound": bool(file_config.project_root_bindings),
                "project_binding_count": len(file_config.project_root_bindings),
            },
            "git": {
                "enabled": git_config.enabled,
                "commit_enabled": git_config.effective_commit_enabled,
                "stage_enabled": git_config.effective_stage_enabled,
                "repository_count": len(git_config.repositories),
                "commit_repository_count": sum(1 for repo in git_config.repositories if repo.commit_allowed),
                "stage_repository_count": sum(1 for repo in git_config.repositories if repo.stage_allowed),
            },
            "commands": {
                "enabled": command_config.effective_enabled,
                "recipe_count": len(command_config.recipes),
                "project_count": len({recipe.project_key for recipe in command_config.recipes}),
            },
        },
    }
