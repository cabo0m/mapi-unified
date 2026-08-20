from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from mapi_core.memory.sensitivity import capture_sensitivity_gate

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
_FORBIDDEN_SHELL_NAMES = frozenset({"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe", "bash", "bash.exe", "sh", "sh.exe", "zsh", "fish", "wsl", "wsl.exe"})
_FORBIDDEN_SCRIPT_SUFFIXES = frozenset({".bat", ".cmd", ".ps1"})


def _bool_env(value: str | None) -> bool:
    return str(value or "").strip().casefold() in _TRUE_VALUES


def _recipe_id(project_key: str, name: str, argv: tuple[str, ...], workdir: Path) -> str:
    payload = json.dumps(
        {"project_key": project_key, "name": name, "argv": list(argv), "workdir": os.path.normcase(str(workdir))},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "cmd_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CommandRecipe:
    recipe_id: str
    project_key: str
    name: str
    purpose: str
    argv: tuple[str, ...]
    workdir: Path
    timeout_seconds: int
    env_allowlist: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, object]:
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "purpose": self.purpose,
            "timeout_seconds": self.timeout_seconds,
            "workdir_name": self.workdir.name or "workspace",
            "env_allowlist": list(self.env_allowlist),
        }

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "recipe_id": self.recipe_id,
            "project_key": self.project_key,
            "argv": list(self.argv),
            "workdir": os.path.normcase(str(self.workdir)),
            "timeout_seconds": self.timeout_seconds,
            "env_allowlist": list(self.env_allowlist),
        }


@dataclass(frozen=True)
class CommandCapabilityConfig:
    enabled: bool
    recipes: tuple[CommandRecipe, ...]
    max_output_bytes: int = 512 * 1024
    default_timeout_seconds: int = 120
    parse_errors: tuple[str, ...] = ()

    @classmethod
    def disabled(cls) -> "CommandCapabilityConfig":
        return cls(enabled=False, recipes=())

    @classmethod
    def from_env(cls) -> "CommandCapabilityConfig":
        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "CommandCapabilityConfig":
        enabled = _bool_env(values.get("MAPI_COMMANDS_ENABLED"))
        try:
            max_output = int(values.get("MAPI_COMMAND_MAX_OUTPUT_BYTES", str(512 * 1024)))
        except ValueError:
            max_output = -1
        try:
            default_timeout = int(values.get("MAPI_COMMAND_DEFAULT_TIMEOUT_SECONDS", "120"))
        except ValueError:
            default_timeout = -1
        raw = str(values.get("MAPI_COMMAND_RECIPES_JSON", "")).strip()
        if not raw:
            return cls(
                enabled=enabled,
                recipes=(),
                max_output_bytes=max_output,
                default_timeout_seconds=default_timeout,
            )
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return cls(enabled=enabled, recipes=(), max_output_bytes=max_output, default_timeout_seconds=default_timeout,
                       parse_errors=("command_recipes_json_invalid",))
        if not isinstance(decoded, dict):
            return cls(enabled=enabled, recipes=(), max_output_bytes=max_output, default_timeout_seconds=default_timeout,
                       parse_errors=("command_recipes_json_must_be_object",))
        recipes: list[CommandRecipe] = []
        errors: list[str] = []
        seen_ids: set[str] = set()
        for raw_project, raw_items in decoded.items():
            project = str(raw_project or "").strip()
            if not project:
                errors.append("command_project_key_required")
                continue
            if not isinstance(raw_items, list):
                errors.append(f"command_project_recipes_must_be_array:{project}")
                continue
            if len(raw_items) > 50:
                errors.append(f"command_recipe_count_exceeded:{project}")
                continue
            for index, item in enumerate(raw_items):
                if not isinstance(item, dict):
                    errors.append(f"command_recipe_must_be_object:{project}:{index}")
                    continue
                name = str(item.get("name") or "").strip()
                purpose = str(item.get("purpose") or "").strip()
                argv_raw = item.get("argv")
                env_raw = item.get("env_allowlist", [])
                workdir_raw = str(item.get("workdir") or "").strip()
                try:
                    timeout = int(item.get("timeout_seconds", default_timeout))
                except (TypeError, ValueError):
                    timeout = -1
                if not _NAME_RE.fullmatch(name):
                    errors.append(f"command_recipe_name_invalid:{project}:{index}")
                    continue
                if not purpose or len(purpose) > 240:
                    errors.append(f"command_recipe_purpose_invalid:{project}:{name}")
                    continue
                if capture_sensitivity_gate(purpose).get("status") != "allowed":
                    errors.append(f"command_recipe_purpose_sensitive:{project}:{name}")
                    continue
                if not isinstance(argv_raw, list) or not argv_raw or len(argv_raw) > 32 or not all(isinstance(arg, str) for arg in argv_raw):
                    errors.append(f"command_recipe_argv_invalid:{project}:{name}")
                    continue
                argv = tuple(arg for arg in argv_raw if arg)
                if len(argv) != len(argv_raw) or any(len(arg) > 4096 or "\x00" in arg for arg in argv):
                    errors.append(f"command_recipe_argv_invalid:{project}:{name}")
                    continue
                if capture_sensitivity_gate(" ".join(argv)).get("status") != "allowed":
                    errors.append(f"command_recipe_argv_sensitive:{project}:{name}")
                    continue
                if not isinstance(env_raw, list) or len(env_raw) > 32 or not all(isinstance(env_name, str) for env_name in env_raw):
                    errors.append(f"command_recipe_env_allowlist_invalid:{project}:{name}")
                    continue
                env_names = tuple(dict.fromkeys(env_name.strip() for env_name in env_raw if env_name.strip()))
                if len(env_names) != len([env_name for env_name in env_raw if env_name.strip()]) or any(
                    not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", env_name) for env_name in env_names
                ):
                    errors.append(f"command_recipe_env_allowlist_invalid:{project}:{name}")
                    continue
                if not workdir_raw:
                    errors.append(f"command_recipe_workdir_required:{project}:{name}")
                    continue
                workdir = Path(workdir_raw).expanduser().resolve(strict=False)
                recipe_id = _recipe_id(project, name, argv, workdir)
                if recipe_id in seen_ids:
                    errors.append(f"command_recipe_duplicate:{project}:{name}")
                    continue
                seen_ids.add(recipe_id)
                recipes.append(CommandRecipe(recipe_id, project, name, purpose, argv, workdir, timeout, env_names))
        return cls(enabled=enabled, recipes=tuple(recipes), max_output_bytes=max_output,
                   default_timeout_seconds=default_timeout, parse_errors=tuple(errors))

    def validation_errors(self) -> list[str]:
        errors = list(self.parse_errors)
        if not self.enabled:
            return errors
        if not self.recipes:
            errors.append("command_recipes_required_when_enabled")
        if not (1024 <= self.max_output_bytes <= 4 * 1024 * 1024):
            errors.append("command_max_output_bytes_out_of_range")
        if not (1 <= self.default_timeout_seconds <= 900):
            errors.append("command_default_timeout_seconds_out_of_range")
        for recipe in self.recipes:
            if not recipe.workdir.exists():
                errors.append(f"command_workdir_missing:{recipe.recipe_id}")
                continue
            if not recipe.workdir.is_dir():
                errors.append(f"command_workdir_not_directory:{recipe.recipe_id}")
            if not (1 <= recipe.timeout_seconds <= 900):
                errors.append(f"command_timeout_out_of_range:{recipe.recipe_id}")
            executable = recipe.argv[0]
            executable_name = Path(executable).name.casefold()
            if executable_name in _FORBIDDEN_SHELL_NAMES or Path(executable_name).suffix.casefold() in _FORBIDDEN_SCRIPT_SUFFIXES:
                errors.append(f"command_shell_executable_not_allowed:{recipe.recipe_id}")
                continue
            if os.path.isabs(executable) or any(sep in executable for sep in ("/", "\\")):
                candidate = Path(executable)
                if not candidate.is_absolute():
                    candidate = recipe.workdir / candidate
                resolved = candidate.resolve(strict=False)
                if not resolved.is_file():
                    errors.append(f"command_executable_missing:{recipe.recipe_id}")
                elif resolved.name.casefold() in _FORBIDDEN_SHELL_NAMES or resolved.suffix.casefold() in _FORBIDDEN_SCRIPT_SUFFIXES:
                    errors.append(f"command_shell_executable_not_allowed:{recipe.recipe_id}")
            else:
                found = shutil.which(executable)
                if found is None:
                    errors.append(f"command_executable_not_found:{recipe.recipe_id}")
                else:
                    resolved = Path(found)
                    if resolved.name.casefold() in _FORBIDDEN_SHELL_NAMES or resolved.suffix.casefold() in _FORBIDDEN_SCRIPT_SUFFIXES:
                        errors.append(f"command_shell_executable_not_allowed:{recipe.recipe_id}")
        return errors

    def recipes_for_project(self, project_key: str | None) -> tuple[CommandRecipe, ...]:
        project = str(project_key or "").strip()
        return tuple(recipe for recipe in self.recipes if recipe.project_key == project)

    def recipe(self, recipe_id: str, project_key: str | None) -> CommandRecipe | None:
        rid = str(recipe_id or "").strip()
        return next((r for r in self.recipes_for_project(project_key) if r.recipe_id == rid), None)

    @property
    def effective_enabled(self) -> bool:
        return self.enabled and bool(self.recipes) and not self.validation_errors()
