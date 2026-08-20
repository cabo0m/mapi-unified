from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
from pathlib import Path
from typing import Any

from mapi_core.core.time_score import utc_now_iso
from .files import FileCapabilityError, FileService
from mapi_core.memory.sensitivity import capture_sensitivity_gate
from .store import CapabilityStore, row_to_dict

FILE_WRITE_PREVIEW_SCHEMA = "mapi_public_file_write_preview.v1"
FILE_WRITE_APPLY_SCHEMA = "mapi_public_file_write_apply.v1"
FILE_OPERATION_LIST_SCHEMA = "mapi_public_file_operations.v1"
FILE_ROLLBACK_PREVIEW_SCHEMA = "mapi_public_file_rollback_preview.v1"
FILE_ROLLBACK_APPLY_SCHEMA = "mapi_public_file_rollback_apply.v1"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(raw)


def _project(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _public_operation(item: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "operation_key",
        "project_key",
        "root_id",
        "relative_path",
        "operation_kind",
        "status",
        "preview_hash",
        "old_sha256",
        "new_sha256",
        "old_size_bytes",
        "new_size_bytes",
        "backup_sha256",
        "applied_at",
        "rolled_back_at",
        "rollback_note",
        "created_at",
        "updated_at",
    )
    return {field: item.get(field) for field in fields}


class FileWriteService:
    def __init__(self, file_service: FileService, store: CapabilityStore) -> None:
        if not file_service.config.effective_write_enabled:
            raise ValueError("file_write_capability_disabled")
        self.file_service = file_service
        self.store = store
        self._lock = threading.RLock()
        self.backup_dir = self.store.db_path.parent / ".mapi-file-backups"

    def _new_content_bytes(self, content: str) -> tuple[bytes | None, dict[str, Any] | None]:
        if not isinstance(content, str):
            return None, {"status": "error", "error": "content_must_be_string"}
        if "\x00" in content:
            return None, {"status": "denied", "error": "binary_content_not_allowed"}
        raw = content.encode("utf-8")
        if len(raw) > self.file_service.config.max_write_bytes:
            return None, {
                "status": "denied",
                "error": "write_content_too_large",
                "size_bytes": len(raw),
                "max_write_bytes": self.file_service.config.max_write_bytes,
            }
        sensitivity = capture_sensitivity_gate(content)
        if sensitivity["status"] != "allowed":
            return None, {
                "status": "denied",
                "error": "sensitive_write_content_not_allowed",
                "sensitivity_class": sensitivity["sensitivity_class"],
                "reason_codes": list(sensitivity["reason_codes"]),
            }
        return raw, None

    def _existing_bytes(self, target: Path) -> tuple[bytes | None, dict[str, Any] | None]:
        size = target.stat().st_size
        if size > self.file_service.config.max_write_bytes:
            return None, {
                "status": "denied",
                "error": "existing_file_too_large",
                "size_bytes": size,
                "max_write_bytes": self.file_service.config.max_write_bytes,
            }
        raw = target.read_bytes()
        if b"\x00" in raw:
            return None, {"status": "denied", "error": "existing_binary_file_not_allowed"}
        try:
            raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None, {"status": "denied", "error": "existing_utf8_text_required"}
        return raw, None

    def preview_write(
        self,
        *,
        project_key: str | None,
        root_id: str,
        relative_path: str,
        content: str,
    ) -> dict[str, Any]:
        new_raw, error = self._new_content_bytes(content)
        if error is not None:
            return {"schema": FILE_WRITE_PREVIEW_SCHEMA, **error}
        assert new_raw is not None
        try:
            root, target, safe_relative, exists = self.file_service.resolve_write_target(
                root_id, relative_path, project_key=project_key
            )
        except FileCapabilityError as exc:
            denied = {
                "file_root_read_only",
                "file_root_not_bound_to_project",
                "file_write_not_bound_to_project",
                "protected_path",
                "path_outside_allowed_root",
                "absolute_path_not_allowed",
                "symlink_write_not_allowed",
            }
            return {
                "status": "denied" if exc.code in denied else "error",
                "schema": FILE_WRITE_PREVIEW_SCHEMA,
                "error": exc.code,
            }

        old_raw: bytes | None = None
        if exists:
            old_raw, old_error = self._existing_bytes(target)
            if old_error is not None:
                return {"schema": FILE_WRITE_PREVIEW_SCHEMA, **old_error}
        old_sha = _sha256(old_raw) if old_raw is not None else None
        new_sha = _sha256(new_raw)
        operation_kind = "update" if exists else "create"
        payload = {
            "project_key": _project(project_key),
            "root_id": root.root_id,
            "relative_path": safe_relative,
            "operation_kind": operation_kind,
            "old_sha256": old_sha,
            "new_sha256": new_sha,
            "old_size_bytes": len(old_raw) if old_raw is not None else None,
            "new_size_bytes": len(new_raw),
        }
        preview_hash = _fingerprint(payload)
        if old_sha == new_sha:
            return {
                "status": "no_change",
                "schema": FILE_WRITE_PREVIEW_SCHEMA,
                "preview_hash": preview_hash,
                "candidate": payload,
                "safety": {"read_only": True, "filesystem_mutations_performed": 0},
            }
        return {
            "status": "preview_ready",
            "schema": FILE_WRITE_PREVIEW_SCHEMA,
            "preview_hash": preview_hash,
            "candidate": payload,
            "safety": {"read_only": True, "filesystem_mutations_performed": 0},
        }

    def _atomic_replace(self, target: Path, raw: bytes, *, operation_key: str, preserve_mode: int | None = None) -> None:
        temp = target.parent / f".{target.name}.mapi-tmp-{operation_key}"
        try:
            with open(temp, "xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if preserve_mode is not None:
                os.chmod(temp, preserve_mode)
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)

    def _write_backup(self, path: Path, raw: bytes) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())

    def _load_operation(self, conn, *, operation_id: int, project_key: str | None) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT * FROM file_operations
            WHERE id = ? AND COALESCE(project_key,'') = COALESCE(?, '')
            """,
            (int(operation_id), _project(project_key)),
        ).fetchone()
        return row_to_dict(row)

    def apply_write(
        self,
        *,
        project_key: str | None,
        root_id: str,
        relative_path: str,
        content: str,
        expected_preview_hash: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            return {"status": "confirmation_required", "schema": FILE_WRITE_APPLY_SCHEMA}
        with self._lock:
            preview = self.preview_write(
                project_key=project_key,
                root_id=root_id,
                relative_path=relative_path,
                content=content,
            )
            if preview.get("status") == "no_change":
                return {**preview, "schema": FILE_WRITE_APPLY_SCHEMA}
            if preview.get("status") != "preview_ready":
                return {**preview, "schema": FILE_WRITE_APPLY_SCHEMA}
            if str(expected_preview_hash or "") != str(preview["preview_hash"]):
                return {
                    "status": "stale_preview",
                    "schema": FILE_WRITE_APPLY_SCHEMA,
                    "expected_preview_hash": str(expected_preview_hash or ""),
                    "current_preview_hash": preview["preview_hash"],
                }
            new_raw, error = self._new_content_bytes(content)
            if error is not None or new_raw is None:
                return {"schema": FILE_WRITE_APPLY_SCHEMA, **(error or {"status": "error", "error": "content_invalid"})}
            try:
                root, target, safe_relative, exists = self.file_service.resolve_write_target(
                root_id, relative_path, project_key=project_key
            )
            except FileCapabilityError as exc:
                return {"status": "stale_preview", "schema": FILE_WRITE_APPLY_SCHEMA, "error": exc.code}
            old_raw: bytes | None = None
            preserve_mode: int | None = None
            if exists:
                old_raw, old_error = self._existing_bytes(target)
                if old_error is not None or old_raw is None:
                    return {"status": "stale_preview", "schema": FILE_WRITE_APPLY_SCHEMA, "error": (old_error or {}).get("error", "current_file_unreadable")}
                preserve_mode = stat.S_IMODE(target.stat().st_mode)
            candidate = preview["candidate"]
            if (candidate.get("old_sha256") or None) != (_sha256(old_raw) if old_raw is not None else None):
                return {"status": "stale_preview", "schema": FILE_WRITE_APPLY_SCHEMA, "error": "current_file_changed"}

            operation_key = secrets.token_hex(16)
            backup_path: Path | None = None
            backup_sha: str | None = None
            if old_raw is not None:
                backup_path = self.backup_dir / f"{operation_key}.bak"
                self._write_backup(backup_path, old_raw)
                backup_sha = _sha256(old_raw)

            wrote_target = False
            try:
                self._atomic_replace(target, new_raw, operation_key=operation_key, preserve_mode=preserve_mode)
                wrote_target = True
                if _sha256(target.read_bytes()) != candidate["new_sha256"]:
                    raise RuntimeError("post_write_hash_mismatch")
                now = utc_now_iso()
                with self.store.connection() as conn:
                    cursor = conn.execute(
                        """
                        INSERT INTO file_operations (
                            operation_key,project_key,root_id,relative_path,operation_kind,status,preview_hash,
                            old_sha256,new_sha256,old_size_bytes,new_size_bytes,backup_path,backup_sha256,
                            applied_at,created_at,updated_at
                        ) VALUES (?,?,?,?,?,'applied',?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            operation_key,
                            _project(project_key),
                            root.root_id,
                            safe_relative,
                            candidate["operation_kind"],
                            preview["preview_hash"],
                            candidate.get("old_sha256"),
                            candidate["new_sha256"],
                            candidate.get("old_size_bytes"),
                            candidate["new_size_bytes"],
                            str(backup_path) if backup_path is not None else None,
                            backup_sha,
                            now,
                            now,
                            now,
                        ),
                    )
                    operation_id = int(cursor.lastrowid)
                    conn.commit()
                    operation = row_to_dict(conn.execute("SELECT * FROM file_operations WHERE id=?", (operation_id,)).fetchone()) or {}
            except Exception:
                if wrote_target:
                    if old_raw is None:
                        target.unlink(missing_ok=True)
                    else:
                        self._atomic_replace(target, old_raw, operation_key=operation_key + "-audit-recovery", preserve_mode=preserve_mode)
                if backup_path is not None:
                    backup_path.unlink(missing_ok=True)
                return {"status": "error", "schema": FILE_WRITE_APPLY_SCHEMA, "error": "file_write_audit_failed"}

            return {
                "status": "applied",
                "schema": FILE_WRITE_APPLY_SCHEMA,
                "operation_id": operation_id,
                "operation": _public_operation(operation),
            }

    def list_operations(
        self,
        *,
        project_key: str | None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            return {"status": "error", "schema": FILE_OPERATION_LIST_SCHEMA, "error": "limit_out_of_range"}
        normalized_status = str(status or "").strip() or None
        if normalized_status not in {None, "applied", "rolled_back"}:
            return {"status": "error", "schema": FILE_OPERATION_LIST_SCHEMA, "error": "unsupported_operation_status"}
        sql = "SELECT * FROM file_operations WHERE COALESCE(project_key,'') = COALESCE(?, '')"
        params: list[Any] = [_project(project_key)]
        if normalized_status is not None:
            sql += " AND status = ?"
            params.append(normalized_status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self.store.connection() as conn:
            items = [_public_operation(row_to_dict(row) or {}) for row in conn.execute(sql, params).fetchall()]
        return {
            "status": "ok",
            "schema": FILE_OPERATION_LIST_SCHEMA,
            "project_key": _project(project_key),
            "operation_status": normalized_status,
            "count": len(items),
            "items": items,
        }

    def preview_rollback(self, *, project_key: str | None, operation_id: int) -> dict[str, Any]:
        with self.store.connection() as conn:
            operation = self._load_operation(conn, operation_id=int(operation_id), project_key=project_key)
        if operation is None:
            return {"status": "not_found", "schema": FILE_ROLLBACK_PREVIEW_SCHEMA, "operation_id": int(operation_id)}
        if operation["status"] == "rolled_back":
            return {
                "status": "already_rolled_back",
                "schema": FILE_ROLLBACK_PREVIEW_SCHEMA,
                "operation_id": int(operation_id),
                "operation": _public_operation(operation),
            }
        try:
            _root, target, safe_relative, exists = self.file_service.resolve_write_target(
                str(operation["root_id"]), str(operation["relative_path"]), project_key=project_key
            )
        except FileCapabilityError as exc:
            return {"status": "blocked", "schema": FILE_ROLLBACK_PREVIEW_SCHEMA, "error": exc.code}
        if not exists:
            return {"status": "stale_operation", "schema": FILE_ROLLBACK_PREVIEW_SCHEMA, "error": "target_missing"}
        current_raw, current_error = self._existing_bytes(target)
        if current_error is not None or current_raw is None:
            return {"status": "stale_operation", "schema": FILE_ROLLBACK_PREVIEW_SCHEMA, "error": (current_error or {}).get("error", "current_file_unreadable")}
        current_sha = _sha256(current_raw)
        if current_sha != operation["new_sha256"]:
            return {
                "status": "stale_operation",
                "schema": FILE_ROLLBACK_PREVIEW_SCHEMA,
                "error": "target_changed_since_operation",
                "current_sha256": current_sha,
                "expected_sha256": operation["new_sha256"],
            }
        backup_sha: str | None = None
        if operation["operation_kind"] == "update":
            backup_value = str(operation.get("backup_path") or "").strip()
            if not backup_value:
                return {"status": "rollback_unavailable", "schema": FILE_ROLLBACK_PREVIEW_SCHEMA, "error": "backup_missing"}
            backup_path = Path(backup_value)
            if not backup_path.is_file():
                return {"status": "rollback_unavailable", "schema": FILE_ROLLBACK_PREVIEW_SCHEMA, "error": "backup_missing"}
            backup_raw = backup_path.read_bytes()
            backup_sha = _sha256(backup_raw)
            if backup_sha != operation.get("backup_sha256") or backup_sha != operation.get("old_sha256"):
                return {"status": "rollback_unavailable", "schema": FILE_ROLLBACK_PREVIEW_SCHEMA, "error": "backup_hash_mismatch"}

        payload = {
            "operation_id": int(operation_id),
            "operation_key": operation["operation_key"],
            "project_key": _project(project_key),
            "root_id": operation["root_id"],
            "relative_path": safe_relative,
            "operation_kind": operation["operation_kind"],
            "current_sha256": current_sha,
            "restore_sha256": operation.get("old_sha256"),
            "backup_sha256": backup_sha,
        }
        return {
            "status": "preview_ready",
            "schema": FILE_ROLLBACK_PREVIEW_SCHEMA,
            "operation_id": int(operation_id),
            "preview_hash": _fingerprint(payload),
            "rollback": payload,
            "safety": {"read_only": True, "filesystem_mutations_performed": 0},
        }

    def rollback(
        self,
        *,
        project_key: str | None,
        operation_id: int,
        expected_preview_hash: str,
        confirmed: bool,
        rollback_note: str | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            return {"status": "confirmation_required", "schema": FILE_ROLLBACK_APPLY_SCHEMA}
        with self._lock:
            preview = self.preview_rollback(project_key=project_key, operation_id=int(operation_id))
            if preview.get("status") == "already_rolled_back":
                return {**preview, "schema": FILE_ROLLBACK_APPLY_SCHEMA}
            if preview.get("status") != "preview_ready":
                return {**preview, "schema": FILE_ROLLBACK_APPLY_SCHEMA}
            if str(expected_preview_hash or "") != str(preview["preview_hash"]):
                return {
                    "status": "stale_preview",
                    "schema": FILE_ROLLBACK_APPLY_SCHEMA,
                    "expected_preview_hash": str(expected_preview_hash or ""),
                    "current_preview_hash": preview["preview_hash"],
                }
            with self.store.connection() as conn:
                operation = self._load_operation(conn, operation_id=int(operation_id), project_key=project_key)
            if operation is None or operation.get("status") != "applied":
                return {"status": "stale_preview", "schema": FILE_ROLLBACK_APPLY_SCHEMA}
            try:
                _root, target, _safe_relative, exists = self.file_service.resolve_write_target(
                    str(operation["root_id"]), str(operation["relative_path"]), project_key=project_key
                )
            except FileCapabilityError as exc:
                return {"status": "stale_preview", "schema": FILE_ROLLBACK_APPLY_SCHEMA, "error": exc.code}
            if not exists:
                return {"status": "stale_preview", "schema": FILE_ROLLBACK_APPLY_SCHEMA, "error": "target_missing"}
            current_raw = target.read_bytes()
            if _sha256(current_raw) != operation["new_sha256"]:
                return {"status": "stale_preview", "schema": FILE_ROLLBACK_APPLY_SCHEMA, "error": "target_changed_since_operation"}
            preserve_mode = stat.S_IMODE(target.stat().st_mode)
            backup_raw: bytes | None = None
            if operation["operation_kind"] == "update":
                backup_path = Path(str(operation["backup_path"]))
                if not backup_path.is_file():
                    return {"status": "rollback_unavailable", "schema": FILE_ROLLBACK_APPLY_SCHEMA, "error": "backup_missing"}
                backup_raw = backup_path.read_bytes()
                if _sha256(backup_raw) != operation["old_sha256"]:
                    return {"status": "rollback_unavailable", "schema": FILE_ROLLBACK_APPLY_SCHEMA, "error": "backup_hash_mismatch"}

            rollback_key = str(operation["operation_key"]) + "-rollback"
            try:
                if operation["operation_kind"] == "create":
                    target.unlink()
                else:
                    assert backup_raw is not None
                    self._atomic_replace(target, backup_raw, operation_key=rollback_key, preserve_mode=preserve_mode)
                now = utc_now_iso()
                with self.store.connection() as conn:
                    cursor = conn.execute(
                        """
                        UPDATE file_operations
                        SET status='rolled_back', rolled_back_at=?, rollback_note=?, updated_at=?
                        WHERE id=? AND status='applied' AND COALESCE(project_key,'') = COALESCE(?, '')
                        """,
                        (now, str(rollback_note or "").strip() or None, now, int(operation_id), _project(project_key)),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("rollback_audit_state_changed")
                    conn.commit()
                    updated = self._load_operation(conn, operation_id=int(operation_id), project_key=project_key) or {}
            except Exception:
                if operation["operation_kind"] == "create":
                    self._atomic_replace(target, current_raw, operation_key=rollback_key + "-recovery", preserve_mode=preserve_mode)
                else:
                    self._atomic_replace(target, current_raw, operation_key=rollback_key + "-recovery", preserve_mode=preserve_mode)
                return {"status": "error", "schema": FILE_ROLLBACK_APPLY_SCHEMA, "error": "rollback_audit_failed"}

            return {
                "status": "rolled_back",
                "schema": FILE_ROLLBACK_APPLY_SCHEMA,
                "operation_id": int(operation_id),
                "operation": _public_operation(updated),
            }
