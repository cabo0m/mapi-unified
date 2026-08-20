from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_PROTECTED_DIR_NAMES = frozenset({".git", ".ssh", ".gnupg", ".mapi", ".mapi-file-backups", ".mapi-git-hooks-empty", ".mapi-git-index-backups"})


def _bool_env(value: str | None) -> bool:
    return str(value or "").strip().casefold() in _TRUE_VALUES


def _resolved_env_paths(raw_value: str | None) -> tuple[Path, ...]:
    raw = str(raw_value or "").strip()
    if not raw:
        return ()
    result: list[Path] = []
    seen: set[Path] = set()
    for item in raw.split(os.pathsep):
        value = item.strip()
        if not value:
            continue
        path = Path(value).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return tuple(result)


def _root_id(path: Path) -> str:
    canonical = os.path.normcase(str(path.resolve(strict=False)))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"root_{digest}"


def _project_paths_from_json(raw_value: str | None) -> tuple[dict[str, tuple[Path, ...]], str | None]:
    raw = str(raw_value or "").strip()
    if not raw:
        return {}, None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}, "file_project_roots_json_invalid"
    if not isinstance(decoded, dict):
        return {}, "file_project_roots_json_must_be_object"
    result: dict[str, tuple[Path, ...]] = {}
    for raw_project, raw_paths in decoded.items():
        project = str(raw_project or "").strip()
        if not project:
            return {}, "file_project_key_required"
        if not isinstance(raw_paths, list) or not all(isinstance(item, str) for item in raw_paths):
            return {}, f"file_project_roots_must_be_string_array:{project}"
        paths: list[Path] = []
        seen: set[Path] = set()
        for item in raw_paths:
            value = item.strip()
            if not value:
                continue
            path = Path(value).expanduser().resolve(strict=False)
            if path in seen:
                continue
            seen.add(path)
            paths.append(path)
        result[project] = tuple(paths)
    return result, None


@dataclass(frozen=True)
class FileRoot:
    root_id: str
    path: Path
    name: str
    write_allowed: bool = False

    def public_dict(self, *, write_allowed: bool | None = None) -> dict[str, object]:
        effective_write = self.write_allowed if write_allowed is None else bool(write_allowed)
        return {
            "root_id": self.root_id,
            "name": self.name,
            "mode": "guarded_write" if effective_write else "read_only",
        }


@dataclass(frozen=True)
class FileCapabilityConfig:
    enabled: bool
    roots: tuple[FileRoot, ...]
    max_read_bytes: int = 256 * 1024
    write_enabled: bool = False
    max_write_bytes: int = 256 * 1024
    unmatched_write_roots: tuple[Path, ...] = ()
    project_root_bindings: tuple[tuple[str, tuple[str, ...]], ...] = ()
    project_write_bindings: tuple[tuple[str, tuple[str, ...]], ...] = ()
    project_binding_error: str | None = None
    project_write_binding_error: str | None = None
    unmatched_project_roots: tuple[Path, ...] = ()
    unmatched_project_write_roots: tuple[Path, ...] = ()

    @classmethod
    def disabled(cls) -> "FileCapabilityConfig":
        return cls(enabled=False, roots=())

    @classmethod
    def from_env(cls) -> "FileCapabilityConfig":
        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "FileCapabilityConfig":
        enabled = _bool_env(values.get("MAPI_FILES_ENABLED"))
        write_enabled = _bool_env(values.get("MAPI_FILE_WRITE_ENABLED"))
        read_paths = _resolved_env_paths(values.get("MAPI_FILE_ROOTS"))
        write_paths = _resolved_env_paths(values.get("MAPI_FILE_WRITE_ROOTS"))
        raw_bindings, binding_error = _project_paths_from_json(values.get("MAPI_FILE_PROJECT_ROOTS_JSON"))
        raw_write_value = values.get("MAPI_FILE_PROJECT_WRITE_ROOTS_JSON")
        raw_write_bindings, write_binding_error = _project_paths_from_json(raw_write_value)
        if not str(raw_write_value or "").strip() and write_enabled and raw_bindings:
            write_set = set(write_paths)
            raw_write_bindings = {
                project: tuple(path for path in paths if path in write_set)
                for project, paths in raw_bindings.items()
                if any(path in write_set for path in paths)
            }
        try:
            max_read_bytes = int(str(values.get("MAPI_FILE_READ_MAX_BYTES", str(256 * 1024))).strip())
        except ValueError:
            max_read_bytes = -1
        try:
            max_write_bytes = int(str(values.get("MAPI_FILE_WRITE_MAX_BYTES", str(256 * 1024))).strip())
        except ValueError:
            max_write_bytes = -1

        write_set = set(write_paths)
        roots = tuple(
            FileRoot(
                root_id=_root_id(path),
                path=path,
                name=path.name or path.anchor or "root",
                write_allowed=path in write_set,
            )
            for path in read_paths
        )
        read_set = set(read_paths)
        unmatched = tuple(path for path in write_paths if path not in read_set)
        root_id_by_path = {root.path: root.root_id for root in roots}
        unmatched_project: list[Path] = []
        binding_rows: list[tuple[str, tuple[str, ...]]] = []
        for project, paths in sorted(raw_bindings.items()):
            root_ids: list[str] = []
            for path in paths:
                root_id = root_id_by_path.get(path)
                if root_id is None:
                    unmatched_project.append(path)
                    continue
                if root_id not in root_ids:
                    root_ids.append(root_id)
            binding_rows.append((project, tuple(root_ids)))
        write_root_id_by_path = {root.path: root.root_id for root in roots if root.write_allowed}
        unmatched_project_write: list[Path] = []
        write_binding_rows: list[tuple[str, tuple[str, ...]]] = []
        for project, paths in sorted(raw_write_bindings.items()):
            root_ids: list[str] = []
            for path in paths:
                root_id = write_root_id_by_path.get(path)
                if root_id is None:
                    unmatched_project_write.append(path)
                    continue
                if root_id not in root_ids:
                    root_ids.append(root_id)
            write_binding_rows.append((project, tuple(root_ids)))
        return cls(
            enabled=enabled,
            roots=roots,
            max_read_bytes=max_read_bytes,
            write_enabled=write_enabled,
            max_write_bytes=max_write_bytes,
            unmatched_write_roots=unmatched,
            project_root_bindings=tuple(binding_rows),
            project_write_bindings=tuple(write_binding_rows),
            project_binding_error=binding_error,
            project_write_binding_error=write_binding_error,
            unmatched_project_roots=tuple(dict.fromkeys(unmatched_project)),
            unmatched_project_write_roots=tuple(dict.fromkeys(unmatched_project_write)),
        )

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if self.write_enabled and not self.enabled:
            errors.append("file_write_requires_files_enabled")
        if self.project_binding_error:
            errors.append(self.project_binding_error)
        if self.project_write_binding_error:
            errors.append(self.project_write_binding_error)
        if not self.enabled:
            return errors
        if self.max_read_bytes < 1 or self.max_read_bytes > 2 * 1024 * 1024:
            errors.append("file_read_max_bytes_out_of_range")
        if not self.roots:
            errors.append("file_roots_required_when_enabled")
        for root in self.roots:
            if not root.path.is_absolute():
                errors.append(f"file_root_not_absolute:{root.root_id}")
                continue
            if not root.path.exists():
                errors.append(f"file_root_missing:{root.root_id}")
                continue
            if not root.path.is_dir():
                errors.append(f"file_root_not_directory:{root.root_id}")
            if root.path.name.casefold() in _PROTECTED_DIR_NAMES:
                errors.append(f"protected_file_root:{root.root_id}")
        if self.unmatched_project_roots:
            errors.append("file_project_roots_must_be_subset_of_read_roots")
        if self.unmatched_project_write_roots:
            errors.append("file_project_write_roots_must_be_subset_of_write_roots")
        if self.write_enabled:
            if self.max_write_bytes < 1 or self.max_write_bytes > 2 * 1024 * 1024:
                errors.append("file_write_max_bytes_out_of_range")
            if not any(root.write_allowed for root in self.roots):
                errors.append("file_write_roots_required_when_enabled")
            if self.unmatched_write_roots:
                errors.append("file_write_roots_must_be_subset_of_read_roots")
            if not self.project_write_bindings:
                errors.append("file_project_write_bindings_required_when_write_enabled")
            else:
                read_map = dict(self.project_root_bindings)
                write_map = dict(self.project_write_bindings)
                for project, write_ids in write_map.items():
                    read_ids = set(read_map.get(project, ()))
                    for root_id in write_ids:
                        if root_id not in read_ids:
                            errors.append(f"file_project_write_requires_read_binding:{project}:{root_id}")
                bound_write_ids = {root_id for _project, root_ids in self.project_write_bindings for root_id in root_ids}
                for root in self.roots:
                    if root.write_allowed and root.root_id not in bound_write_ids:
                        errors.append(f"file_write_root_requires_project_write_binding:{root.root_id}")
        return errors

    def _binding_map(self) -> dict[str, tuple[str, ...]]:
        return dict(self.project_root_bindings)

    def root_ids_for_project(self, project_key: str | None) -> tuple[str, ...]:
        mapping = self._binding_map()
        if not mapping:
            return tuple(root.root_id for root in self.roots)
        project = str(project_key or "").strip()
        if not project:
            return ()
        return mapping.get(project, ())

    def root_bound_to_project(self, root_id: str, project_key: str | None) -> bool:
        return str(root_id) in set(self.root_ids_for_project(project_key))

    def write_root_ids_for_project(self, project_key: str | None) -> tuple[str, ...]:
        mapping = dict(self.project_write_bindings)
        project = str(project_key or "").strip()
        if not project:
            return ()
        return mapping.get(project, ())

    def write_bound_to_project(self, root_id: str, project_key: str | None) -> bool:
        root = next((item for item in self.roots if item.root_id == str(root_id)), None)
        return bool(
            self.write_enabled
            and root
            and root.write_allowed
            and str(root_id) in set(self.write_root_ids_for_project(project_key))
        )

    def public_roots(self, project_key: str | None = None) -> list[dict[str, object]]:
        if project_key is None:
            return [root.public_dict(write_allowed=self.write_enabled and root.write_allowed) for root in self.roots]
        allowed = set(self.root_ids_for_project(project_key))
        writable = set(self.write_root_ids_for_project(project_key))
        return [
            root.public_dict(write_allowed=root.root_id in writable)
            for root in self.roots
            if root.root_id in allowed
        ]

    @property
    def effective_write_enabled(self) -> bool:
        return (
            self.enabled
            and self.write_enabled
            and any(root.write_allowed for root in self.roots)
            and bool(self.project_write_bindings)
            and not self.validation_errors()
        )
