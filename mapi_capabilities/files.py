from __future__ import annotations

import hashlib
from pathlib import Path, PurePath
from typing import Any

from .file_config import FileCapabilityConfig, FileRoot

FILE_ROOTS_SCHEMA = "mapi_public_file_roots.v1"
FILE_READ_SCHEMA = "mapi_public_file_read.v1"
FILE_DIRECTORY_SCHEMA = "mapi_public_file_directory.v1"

_PROTECTED_DIR_NAMES = frozenset({".git", ".ssh", ".gnupg", ".mapi", ".mapi-file-backups", ".mapi-git-hooks-empty", ".mapi-git-index-backups"})
_PROTECTED_FILENAMES = frozenset({
    ".env",
    "credentials.json",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
})
_PROTECTED_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})


class FileCapabilityError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FileService:
    def __init__(self, config: FileCapabilityConfig) -> None:
        errors = config.validation_errors()
        if not config.enabled:
            raise ValueError("file_capability_disabled")
        if errors:
            raise ValueError("file_capability_invalid:" + ",".join(errors))
        self.config = config
        self._roots = {root.root_id: root for root in config.roots}

    def list_roots(self, *, project_key: str | None = None) -> dict[str, Any]:
        roots = self.config.public_roots(project_key)
        return {
            "status": "ok",
            "schema": FILE_ROOTS_SCHEMA,
            "mode": "read_only",
            "max_read_bytes": self.config.max_read_bytes,
            "count": len(roots),
            "roots": roots,
            "project_bound": bool(self.config.project_root_bindings),
        }

    def _root(self, root_id: str, *, project_key: str | None = None) -> FileRoot:
        root = self._roots.get(str(root_id or "").strip())
        if root is None:
            raise FileCapabilityError("unknown_file_root")
        if not self.config.root_bound_to_project(root.root_id, project_key):
            raise FileCapabilityError("file_root_not_bound_to_project")
        return root

    def _check_sensitive_relative_path(self, relative_path: PurePath) -> None:
        parts = [part.casefold() for part in relative_path.parts]
        if any(part in _PROTECTED_DIR_NAMES for part in parts[:-1]):
            raise FileCapabilityError("protected_path")
        filename = parts[-1] if parts else ""
        if filename in _PROTECTED_DIR_NAMES:
            raise FileCapabilityError("protected_path")
        if filename in _PROTECTED_FILENAMES or filename.startswith(".env."):
            raise FileCapabilityError("protected_path")
        if Path(filename).suffix.casefold() in _PROTECTED_SUFFIXES:
            raise FileCapabilityError("protected_path")

    def _resolve_file(
        self, root_id: str, relative_path: str, *, project_key: str | None = None
    ) -> tuple[FileRoot, Path, str]:
        root = self._root(root_id, project_key=project_key)
        raw = str(relative_path or "").strip()
        if not raw:
            raise FileCapabilityError("relative_path_required")
        relative = Path(raw)
        if relative.is_absolute():
            raise FileCapabilityError("absolute_path_not_allowed")
        self._check_sensitive_relative_path(relative)
        try:
            target = (root.path / relative).resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileCapabilityError("file_not_found") from exc
        except OSError as exc:
            raise FileCapabilityError("file_resolution_failed") from exc
        try:
            resolved_relative = target.relative_to(root.path)
        except ValueError as exc:
            raise FileCapabilityError("path_outside_allowed_root") from exc
        self._check_sensitive_relative_path(resolved_relative)
        if not target.is_file():
            raise FileCapabilityError("path_is_not_file")
        return root, target, resolved_relative.as_posix()

    def _resolve_directory(
        self, root_id: str, relative_path: str, *, project_key: str | None = None
    ) -> tuple[FileRoot, Path, str]:
        root = self._root(root_id, project_key=project_key)
        raw = str(relative_path or ".").strip() or "."
        relative = Path(raw)
        if relative.is_absolute():
            raise FileCapabilityError("absolute_path_not_allowed")
        self._check_sensitive_relative_path(relative)
        try:
            target = (root.path / relative).resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileCapabilityError("directory_not_found") from exc
        except OSError as exc:
            raise FileCapabilityError("directory_resolution_failed") from exc
        try:
            resolved_relative = target.relative_to(root.path)
        except ValueError as exc:
            raise FileCapabilityError("path_outside_allowed_root") from exc
        if resolved_relative.parts:
            self._check_sensitive_relative_path(resolved_relative)
        if not target.is_dir():
            raise FileCapabilityError("path_is_not_directory")
        safe_relative = resolved_relative.as_posix() if resolved_relative.parts else "."
        return root, target, safe_relative

    def _is_protected_child(self, relative_path: Path) -> bool:
        try:
            self._check_sensitive_relative_path(relative_path)
            return False
        except FileCapabilityError as exc:
            if exc.code == "protected_path":
                return True
            raise

    def list_directory(
        self,
        *,
        root_id: str,
        relative_path: str = ".",
        limit: int = 200,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 500))
        try:
            root, target, safe_relative = self._resolve_directory(
                root_id, relative_path, project_key=project_key
            )
        except FileCapabilityError as exc:
            return {
                "status": "denied" if exc.code in {"protected_path", "path_outside_allowed_root", "absolute_path_not_allowed", "file_root_not_bound_to_project"} else "error",
                "schema": FILE_DIRECTORY_SCHEMA,
                "error": exc.code,
            }

        items: list[dict[str, Any]] = []
        hidden_count = 0
        for child in sorted(target.iterdir(), key=lambda item: item.name.casefold()):
            child_relative = child.relative_to(root.path)
            if self._is_protected_child(child_relative):
                hidden_count += 1
                continue
            if child.is_symlink():
                kind = "symlink"
                size = None
            elif child.is_dir():
                kind = "directory"
                size = None
            elif child.is_file():
                kind = "file"
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
            else:
                kind = "other"
                size = None
            items.append(
                {
                    "name": child.name,
                    "relative_path": child_relative.as_posix(),
                    "kind": kind,
                    "size_bytes": size,
                }
            )
            if len(items) >= safe_limit:
                break
        return {
            "status": "ok",
            "schema": FILE_DIRECTORY_SCHEMA,
            "root_id": root.root_id,
            "root_name": root.name,
            "relative_path": safe_relative,
            "limit": safe_limit,
            "returned_count": len(items),
            "hidden_protected_count": hidden_count,
            "items": items,
        }

    def resolve_write_target(
        self, root_id: str, relative_path: str, *, project_key: str | None = None
    ) -> tuple[FileRoot, Path, str, bool]:
        root = self._root(root_id, project_key=project_key)
        if not root.write_allowed:
            raise FileCapabilityError("file_root_read_only")
        if not self.config.write_bound_to_project(root.root_id, project_key):
            raise FileCapabilityError("file_write_not_bound_to_project")
        raw = str(relative_path or "").strip()
        if not raw:
            raise FileCapabilityError("relative_path_required")
        relative = Path(raw)
        if relative.is_absolute():
            raise FileCapabilityError("absolute_path_not_allowed")
        self._check_sensitive_relative_path(relative)
        if relative.name in {"", ".", ".."}:
            raise FileCapabilityError("target_filename_required")
        try:
            parent = (root.path / relative.parent).resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileCapabilityError("parent_directory_not_found") from exc
        except OSError as exc:
            raise FileCapabilityError("parent_directory_resolution_failed") from exc
        try:
            parent_relative = parent.relative_to(root.path)
        except ValueError as exc:
            raise FileCapabilityError("path_outside_allowed_root") from exc
        if parent_relative.parts:
            self._check_sensitive_relative_path(parent_relative)
        if not parent.is_dir():
            raise FileCapabilityError("parent_is_not_directory")
        target = parent / relative.name
        exists = target.exists() or target.is_symlink()
        if exists:
            if target.is_symlink():
                raise FileCapabilityError("symlink_write_not_allowed")
            try:
                resolved = target.resolve(strict=True)
            except OSError as exc:
                raise FileCapabilityError("file_resolution_failed") from exc
            try:
                safe_relative = resolved.relative_to(root.path)
            except ValueError as exc:
                raise FileCapabilityError("path_outside_allowed_root") from exc
            self._check_sensitive_relative_path(safe_relative)
            if not resolved.is_file():
                raise FileCapabilityError("path_is_not_file")
            return root, resolved, safe_relative.as_posix(), True
        resolved = target.resolve(strict=False)
        try:
            safe_relative = resolved.relative_to(root.path)
        except ValueError as exc:
            raise FileCapabilityError("path_outside_allowed_root") from exc
        self._check_sensitive_relative_path(safe_relative)
        return root, resolved, safe_relative.as_posix(), False

    def read_text(
        self, *, root_id: str, relative_path: str, project_key: str | None = None
    ) -> dict[str, Any]:
        try:
            root, target, safe_relative = self._resolve_file(
                root_id, relative_path, project_key=project_key
            )
        except FileCapabilityError as exc:
            return {"status": "denied" if exc.code in {"protected_path", "path_outside_allowed_root", "absolute_path_not_allowed", "file_root_not_bound_to_project"} else "error", "schema": FILE_READ_SCHEMA, "error": exc.code}

        size = target.stat().st_size
        if size > self.config.max_read_bytes:
            return {
                "status": "denied",
                "schema": FILE_READ_SCHEMA,
                "error": "file_too_large",
                "size_bytes": size,
                "max_read_bytes": self.config.max_read_bytes,
            }
        raw = target.read_bytes()
        if b"\x00" in raw:
            return {"status": "denied", "schema": FILE_READ_SCHEMA, "error": "binary_file_not_allowed", "size_bytes": len(raw)}
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return {"status": "denied", "schema": FILE_READ_SCHEMA, "error": "utf8_text_required", "size_bytes": len(raw)}
        return {
            "status": "ok",
            "schema": FILE_READ_SCHEMA,
            "root_id": root.root_id,
            "root_name": root.name,
            "relative_path": safe_relative,
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "line_count": len(content.splitlines()),
            "encoding": "utf-8",
            "content": content,
        }
