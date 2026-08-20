from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import secrets
import stat
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from mapi_core.core.time_score import utc_now_iso
from .git_service import GitCapabilityError, GitService
from mapi_core.memory.sensitivity import capture_sensitivity_gate
from .store import CapabilityStore, row_to_dict

GIT_STAGE_PREVIEW_SCHEMA = "mapi_public_git_stage_preview.v1"
GIT_STAGE_APPLY_SCHEMA = "mapi_public_git_stage_apply.v1"
GIT_STAGE_OPERATIONS_SCHEMA = "mapi_public_git_stage_operations.v1"
GIT_STAGE_ROLLBACK_PREVIEW_SCHEMA = "mapi_public_git_stage_rollback_preview.v1"
GIT_STAGE_ROLLBACK_APPLY_SCHEMA = "mapi_public_git_stage_rollback_apply.v1"

_MAX_PATHS = 50
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_ALLOWED_MODES = frozenset({"100644", "100755"})


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
        "repo_id",
        "branch",
        "status",
        "preview_hash",
        "head",
        "paths_json",
        "index_sha256_before",
        "index_sha256_after",
        "prospective_diff_sha256",
        "backup_sha256",
        "applied_at",
        "rolled_back_at",
        "rollback_note",
        "created_at",
        "updated_at",
    )
    result = {field: item.get(field) for field in fields}
    raw_paths = result.pop("paths_json", "[]")
    try:
        result["paths"] = json.loads(str(raw_paths or "[]"))
    except json.JSONDecodeError:
        result["paths"] = []
    return result


class GitStageService:
    def __init__(self, git_service: GitService, store: CapabilityStore) -> None:
        if not git_service.config.effective_stage_enabled:
            raise ValueError("git_stage_capability_disabled")
        self.git_service = git_service
        self.store = store
        self._lock = threading.RLock()
        self.backup_dir = self.store.db_path.parent / ".mapi-git-index-backups"

    def _repo(self, repo_id: str, *, project_key: str | None):
        try:
            repo = self.git_service._repo(repo_id, project_key=project_key)
        except GitCapabilityError as exc:
            raise GitCapabilityError(exc.code) from exc
        if not repo.stage_allowed:
            raise GitCapabilityError("git_repository_stage_disabled")
        if not self.git_service.config.stage_bound_to_project(repo.repo_id, project_key):
            raise GitCapabilityError("git_stage_not_bound_to_project")
        return repo

    def _stdout(self, repo, args: list[str], error_code: str) -> bytes:
        completed = self.git_service._run(repo, args)
        if completed.returncode != 0:
            raise GitCapabilityError(error_code)
        return completed.stdout

    def _head(self, repo) -> str:
        return self._stdout(repo, ["rev-parse", "HEAD"], "git_head_required").decode("utf-8", "replace").strip()

    def _branch(self, repo) -> str:
        branch = self._stdout(repo, ["branch", "--show-current"], "git_branch_failed").decode("utf-8", "replace").strip()
        if not branch:
            raise GitCapabilityError("detached_head_stage_not_allowed")
        return branch

    def _index_path(self, repo) -> Path:
        raw = self._stdout(repo, ["rev-parse", "--git-path", "index"], "git_index_path_failed").decode("utf-8", "replace").strip()
        if not raw:
            raise GitCapabilityError("git_index_path_failed")
        candidate = Path(raw)
        path = candidate.resolve(strict=False) if candidate.is_absolute() else (repo.path / candidate).resolve(strict=False)
        if not path.is_file():
            raise GitCapabilityError("git_index_missing")
        return path

    def _index_lock_exists(self, index_path: Path) -> bool:
        return index_path.with_name(index_path.name + ".lock").exists()

    def _index_state_sha256(self, repo) -> str:
        raw = self._stdout(repo, ["ls-files", "--stage", "-z"], "git_index_read_failed")
        return _sha256(raw)

    def _normalize_paths(self, paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(paths, (list, tuple)) or not paths:
            raise GitCapabilityError("git_stage_paths_required")
        if len(paths) > _MAX_PATHS:
            raise GitCapabilityError("git_stage_too_many_paths")
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in paths:
            if not isinstance(raw, str):
                raise GitCapabilityError("git_stage_paths_must_be_strings")
            value = raw.strip().replace("\\", "/")
            if not value or "\x00" in value or ":" in value:
                raise GitCapabilityError("invalid_git_stage_path")
            if value.startswith("/") or _DRIVE_PREFIX.match(value):
                raise GitCapabilityError("invalid_git_stage_path")
            pure = PurePosixPath(value)
            if any(part in {"", ".", ".."} for part in pure.parts) or ".git" in {part.casefold() for part in pure.parts}:
                raise GitCapabilityError("invalid_git_stage_path")
            canonical = pure.as_posix()
            if canonical not in seen:
                seen.add(canonical)
                normalized.append(canonical)
        if not normalized:
            raise GitCapabilityError("git_stage_paths_required")
        return tuple(sorted(normalized))

    def _head_entry(self, repo, path: str) -> tuple[str, str]:
        raw = self._stdout(repo, ["ls-tree", "-z", "HEAD", "--", path], "git_stage_head_lookup_failed")
        if not raw:
            raise GitCapabilityError("git_stage_tracked_files_only")
        try:
            metadata, returned_path = raw.split(b"\x00", 1)[0].split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
            decoded_path = returned_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitCapabilityError("git_stage_head_entry_invalid") from exc
        if decoded_path != path or object_type != "blob" or mode not in _ALLOWED_MODES:
            raise GitCapabilityError("git_stage_regular_tracked_files_only")
        return mode, object_id

    def _blob_bytes(self, repo, object_id: str) -> bytes:
        return self._stdout(repo, ["cat-file", "blob", object_id], "git_stage_blob_read_failed")

    def _worktree_path(self, repo, path: str) -> Path:
        root = repo.path.resolve(strict=True)
        target = root
        for part in PurePosixPath(path).parts:
            target = target / part
            if target.is_symlink():
                raise GitCapabilityError("git_stage_symlinks_not_allowed")
        return target

    def _read_worktree(self, repo, path: str) -> bytes | None:
        target = self._worktree_path(repo, path)
        if not target.exists():
            return None
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise GitCapabilityError("git_stage_regular_tracked_files_only")
        if info.st_size > self.git_service.config.stage_max_file_bytes:
            raise GitCapabilityError("git_stage_file_too_large")
        raw = target.read_bytes()
        if b"\x00" in raw:
            raise GitCapabilityError("git_stage_text_files_only")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitCapabilityError("git_stage_utf8_text_only") from exc
        return raw

    def _hash_worktree_no_filters(self, repo, path: str) -> str:
        raw = self._stdout(repo, ["hash-object", "--no-filters", "--", path], "git_stage_hash_failed")
        return raw.decode("ascii", "replace").strip()

    def _canonical_diff(self, repo, paths: tuple[str, ...]) -> tuple[str, list[dict[str, Any]]]:
        chunks: list[str] = []
        entries: list[dict[str, Any]] = []
        for path in paths:
            mode, head_oid = self._head_entry(repo, path)
            old_raw = self._blob_bytes(repo, head_oid)
            if len(old_raw) > self.git_service.config.stage_max_file_bytes or b"\x00" in old_raw:
                raise GitCapabilityError("git_stage_text_files_only")
            try:
                old_text = old_raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GitCapabilityError("git_stage_utf8_text_only") from exc
            new_raw = self._read_worktree(repo, path)
            if new_raw is None:
                new_text, action, new_oid, new_sha, new_size = "", "delete", None, None, 0
            else:
                new_text, action = new_raw.decode("utf-8"), "update"
                new_oid = self._hash_worktree_no_filters(repo, path)
                new_sha, new_size = _sha256(new_raw), len(new_raw)
            diff = "".join(difflib.unified_diff(
                old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="\n",
            ))
            if not diff:
                continue
            chunks.append(diff if diff.endswith("\n") else diff + "\n")
            entries.append({"path": path, "action": action, "mode": mode, "head_oid": head_oid,
                            "new_oid": new_oid, "new_sha256": new_sha, "new_size_bytes": new_size})
        combined = "".join(chunks)
        sensitivity = capture_sensitivity_gate(combined)
        if sensitivity["status"] != "allowed":
            raise GitCapabilityError("sensitive_stage_diff_not_allowed")
        return combined, entries

    def _current_index_entry(self, repo, path: str) -> tuple[str, str] | None:
        raw = self._stdout(repo, ["ls-files", "--stage", "-z", "--", path], "git_index_read_failed")
        if not raw:
            return None
        metadata, returned_path = raw.split(b"\x00", 1)[0].split(b"\t", 1)
        mode, object_id, stage = metadata.decode("ascii").split(" ", 2)
        if returned_path.decode("utf-8") != path or stage != "0":
            raise GitCapabilityError("git_stage_index_entry_invalid")
        return mode, object_id

    def _entries_already_staged(self, repo, entries: list[dict[str, Any]]) -> bool:
        for item in entries:
            current = self._current_index_entry(repo, item["path"])
            if item["action"] == "delete":
                if current is not None:
                    return False
            elif current != (item["mode"], item["new_oid"]):
                return False
        return True

    def _write_backup(self, path: Path, raw: bytes) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())

    def _restore_index(self, index_path: Path, raw: bytes, *, operation_key: str) -> None:
        if self._index_lock_exists(index_path):
            raise GitCapabilityError("git_index_locked")
        temp = index_path.parent / f".{index_path.name}.mapi-stage-{operation_key}.tmp"
        try:
            with open(temp, "xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, index_path)
        finally:
            temp.unlink(missing_ok=True)

    def _load_operation(self, conn, *, operation_id: int, project_key: str | None) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM git_stage_operations WHERE id=? AND COALESCE(project_key,'')=COALESCE(?, '')",
            (int(operation_id), _project(project_key)),
        ).fetchone()
        return row_to_dict(row)

    def preview_stage(self, *, project_key: str | None, repo_id: str, paths: list[str] | tuple[str, ...]) -> dict[str, Any]:
        try:
            repo = self._repo(repo_id, project_key=project_key)
            normalized = self._normalize_paths(paths)
            branch = self._branch(repo)
            head = self._head(repo)
            index_path = self._index_path(repo)
            if self._index_lock_exists(index_path):
                raise GitCapabilityError("git_index_locked")
            index_before_sha = self._index_state_sha256(repo)
            prospective, entries = self._canonical_diff(repo, normalized)
            if not entries:
                return {"status": "blocked", "schema": GIT_STAGE_PREVIEW_SCHEMA, "error": "no_changes_for_selected_paths"}
            already_staged = self._entries_already_staged(repo, entries)
        except GitCapabilityError as exc:
            denied = exc.code in {
                "git_repository_stage_disabled", "git_repository_not_bound_to_project", "git_stage_not_bound_to_project",
                "git_stage_tracked_files_only", "git_stage_regular_tracked_files_only", "invalid_git_stage_path",
                "sensitive_stage_diff_not_allowed", "git_stage_text_files_only", "git_stage_utf8_text_only",
                "git_stage_file_too_large", "git_stage_symlinks_not_allowed", "detached_head_stage_not_allowed",
            }
            return {"status": "denied" if denied else "error", "schema": GIT_STAGE_PREVIEW_SCHEMA, "error": exc.code}
        payload = {
            "project_key": _project(project_key), "repo_id": repo.repo_id, "branch": branch, "head": head,
            "paths": [item["path"] for item in entries], "entries": entries,
            "index_sha256_before": index_before_sha,
            "prospective_diff_sha256": _sha256(prospective.encode("utf-8")),
        }
        return {
            "status": "already_staged" if already_staged else "preview_ready",
            "schema": GIT_STAGE_PREVIEW_SCHEMA,
            "preview_hash": _fingerprint(payload),
            "candidate": {**payload, "repo_name": repo.name, "prospective_diff": prospective},
            "safety": {
                "read_only": True, "git_mutations_performed": 0, "tracked_files_only": True,
                "utf8_text_only": True, "git_filters_used": False, "git_hooks_used": False,
            },
        }

    def _apply_entry(self, repo, item: dict[str, Any]) -> None:
        if item["action"] == "delete":
            completed = self.git_service._run(repo, ["update-index", "--remove", "--", item["path"]])
            if completed.returncode != 0:
                raise GitCapabilityError("git_stage_update_index_failed")
            return
        raw = self._stdout(repo, ["hash-object", "-w", "--no-filters", "--", item["path"]], "git_stage_hash_write_failed")
        written_oid = raw.decode("ascii", "replace").strip()
        if written_oid != item["new_oid"]:
            raise GitCapabilityError("git_stage_blob_hash_changed")
        completed = self.git_service._run(
            repo, ["update-index", "--add", "--cacheinfo", item["mode"], written_oid, item["path"]]
        )
        if completed.returncode != 0:
            raise GitCapabilityError("git_stage_update_index_failed")

    def apply_stage(
        self, *, project_key: str | None, repo_id: str, paths: list[str] | tuple[str, ...],
        expected_preview_hash: str, confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            return {"status": "confirmation_required", "schema": GIT_STAGE_APPLY_SCHEMA}
        with self._lock:
            preview = self.preview_stage(project_key=project_key, repo_id=repo_id, paths=paths)
            if preview.get("status") == "already_staged":
                return {**preview, "schema": GIT_STAGE_APPLY_SCHEMA}
            if preview.get("status") != "preview_ready":
                return {**preview, "schema": GIT_STAGE_APPLY_SCHEMA}
            if str(expected_preview_hash or "") != str(preview["preview_hash"]):
                return {
                    "status": "stale_preview", "schema": GIT_STAGE_APPLY_SCHEMA,
                    "expected_preview_hash": str(expected_preview_hash or ""), "current_preview_hash": preview["preview_hash"],
                }
            candidate = preview["candidate"]
            try:
                repo = self._repo(repo_id, project_key=project_key)
                index_path = self._index_path(repo)
                if self._index_lock_exists(index_path):
                    raise GitCapabilityError("git_index_locked")
                before_raw = index_path.read_bytes()
            except GitCapabilityError as exc:
                return {"status": "stale_preview", "schema": GIT_STAGE_APPLY_SCHEMA, "error": exc.code}
            if self._index_state_sha256(repo) != candidate["index_sha256_before"]:
                return {"status": "stale_preview", "schema": GIT_STAGE_APPLY_SCHEMA, "error": "git_index_changed"}
            operation_key = secrets.token_hex(16)
            backup_path = self.backup_dir / f"{operation_key}.idx"
            backup_sha = _sha256(before_raw)
            self._write_backup(backup_path, before_raw)
            try:
                for item in candidate["entries"]:
                    self._apply_entry(repo, item)
                after_raw = index_path.read_bytes()
                after_state_sha = self._index_state_sha256(repo)
                if after_state_sha == candidate["index_sha256_before"]:
                    raise RuntimeError("git_index_did_not_change")
                if not self._entries_already_staged(repo, candidate["entries"]):
                    raise RuntimeError("stage_entry_verification_failed")
                now = utc_now_iso()
                with self.store.connection() as conn:
                    cursor = conn.execute(
                        """
                        INSERT INTO git_stage_operations (
                            operation_key,project_key,repo_id,branch,status,preview_hash,head,paths_json,
                            index_sha256_before,index_sha256_after,prospective_diff_sha256,backup_path,
                            backup_sha256,applied_at,created_at,updated_at
                        ) VALUES (?,?,?,?,'applied',?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            operation_key, _project(project_key), repo.repo_id, candidate["branch"], preview["preview_hash"],
                            candidate["head"], json.dumps(candidate["paths"], ensure_ascii=False), candidate["index_sha256_before"],
                            after_state_sha, candidate["prospective_diff_sha256"], str(backup_path), backup_sha, now, now, now,
                        ),
                    )
                    operation_id = int(cursor.lastrowid)
                    conn.commit()
                    operation = row_to_dict(conn.execute("SELECT * FROM git_stage_operations WHERE id=?", (operation_id,)).fetchone()) or {}
            except Exception:
                try:
                    self._restore_index(index_path, before_raw, operation_key=operation_key + "-recovery")
                finally:
                    backup_path.unlink(missing_ok=True)
                return {"status": "error", "schema": GIT_STAGE_APPLY_SCHEMA, "error": "git_stage_audit_or_verification_failed"}
            return {
                "status": "applied", "schema": GIT_STAGE_APPLY_SCHEMA, "operation_id": operation_id,
                "operation": _public_operation(operation),
            }

    def list_operations(self, *, project_key: str | None, status: str | None = None, limit: int = 20) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            return {"status": "error", "schema": GIT_STAGE_OPERATIONS_SCHEMA, "error": "limit_out_of_range"}
        normalized_status = str(status or "").strip() or None
        if normalized_status not in {None, "applied", "rolled_back"}:
            return {"status": "error", "schema": GIT_STAGE_OPERATIONS_SCHEMA, "error": "unsupported_operation_status"}
        sql = "SELECT * FROM git_stage_operations WHERE COALESCE(project_key,'')=COALESCE(?, '')"
        params: list[Any] = [_project(project_key)]
        if normalized_status is not None:
            sql += " AND status=?"
            params.append(normalized_status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self.store.connection() as conn:
            items = [_public_operation(row_to_dict(row) or {}) for row in conn.execute(sql, params).fetchall()]
        return {"status": "ok", "schema": GIT_STAGE_OPERATIONS_SCHEMA, "count": len(items), "items": items, "operation_status": normalized_status}

    def preview_rollback(self, *, project_key: str | None, operation_id: int) -> dict[str, Any]:
        with self.store.connection() as conn:
            operation = self._load_operation(conn, operation_id=int(operation_id), project_key=project_key)
        if operation is None:
            return {"status": "not_found", "schema": GIT_STAGE_ROLLBACK_PREVIEW_SCHEMA, "operation_id": int(operation_id)}
        if operation["status"] == "rolled_back":
            return {"status": "already_rolled_back", "schema": GIT_STAGE_ROLLBACK_PREVIEW_SCHEMA, "operation": _public_operation(operation)}
        try:
            repo = self._repo(str(operation["repo_id"]), project_key=project_key)
            branch = self._branch(repo)
            head = self._head(repo)
            index_path = self._index_path(repo)
            if self._index_lock_exists(index_path):
                raise GitCapabilityError("git_index_locked")
            current_raw = index_path.read_bytes()
        except GitCapabilityError as exc:
            return {"status": "blocked", "schema": GIT_STAGE_ROLLBACK_PREVIEW_SCHEMA, "error": exc.code}
        if branch != operation["branch"]:
            return {"status": "stale_operation", "schema": GIT_STAGE_ROLLBACK_PREVIEW_SCHEMA, "error": "branch_changed"}
        if head != operation["head"]:
            return {"status": "stale_operation", "schema": GIT_STAGE_ROLLBACK_PREVIEW_SCHEMA, "error": "head_changed_since_stage"}
        if self._index_state_sha256(repo) != operation["index_sha256_after"]:
            return {"status": "stale_operation", "schema": GIT_STAGE_ROLLBACK_PREVIEW_SCHEMA, "error": "index_changed_since_stage"}
        backup_path = Path(str(operation.get("backup_path") or ""))
        if not backup_path.is_file():
            return {"status": "rollback_unavailable", "schema": GIT_STAGE_ROLLBACK_PREVIEW_SCHEMA, "error": "index_backup_missing"}
        backup_raw = backup_path.read_bytes()
        if _sha256(backup_raw) != operation["backup_sha256"]:
            return {"status": "rollback_unavailable", "schema": GIT_STAGE_ROLLBACK_PREVIEW_SCHEMA, "error": "index_backup_hash_mismatch"}
        payload = {
            "operation_id": int(operation_id),
            "operation_key": operation["operation_key"],
            "project_key": _project(project_key),
            "repo_id": operation["repo_id"],
            "branch": operation["branch"],
            "head": operation["head"],
            "index_sha256_current": operation["index_sha256_after"],
            "index_sha256_restore": operation["index_sha256_before"],
        }
        return {
            "status": "preview_ready",
            "schema": GIT_STAGE_ROLLBACK_PREVIEW_SCHEMA,
            "preview_hash": _fingerprint(payload),
            "rollback": payload,
            "safety": {"read_only": True, "git_mutations_performed": 0, "working_tree_mutated": False},
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
            return {"status": "confirmation_required", "schema": GIT_STAGE_ROLLBACK_APPLY_SCHEMA}
        with self._lock:
            preview = self.preview_rollback(project_key=project_key, operation_id=int(operation_id))
            if preview.get("status") == "already_rolled_back":
                return {**preview, "schema": GIT_STAGE_ROLLBACK_APPLY_SCHEMA}
            if preview.get("status") != "preview_ready":
                return {**preview, "schema": GIT_STAGE_ROLLBACK_APPLY_SCHEMA}
            if str(expected_preview_hash or "") != str(preview["preview_hash"]):
                return {
                    "status": "stale_preview",
                    "schema": GIT_STAGE_ROLLBACK_APPLY_SCHEMA,
                    "expected_preview_hash": str(expected_preview_hash or ""),
                    "current_preview_hash": preview["preview_hash"],
                }
            with self.store.connection() as conn:
                operation = self._load_operation(conn, operation_id=int(operation_id), project_key=project_key)
            if operation is None or operation.get("status") != "applied":
                return {"status": "stale_preview", "schema": GIT_STAGE_ROLLBACK_APPLY_SCHEMA}
            try:
                repo = self._repo(str(operation["repo_id"]), project_key=project_key)
                index_path = self._index_path(repo)
                if self._index_lock_exists(index_path):
                    raise GitCapabilityError("git_index_locked")
                current_raw = index_path.read_bytes()
            except GitCapabilityError as exc:
                return {"status": "stale_preview", "schema": GIT_STAGE_ROLLBACK_APPLY_SCHEMA, "error": exc.code}
            if self._index_state_sha256(repo) != operation["index_sha256_after"]:
                return {"status": "stale_preview", "schema": GIT_STAGE_ROLLBACK_APPLY_SCHEMA, "error": "index_changed_since_stage"}
            backup_path = Path(str(operation.get("backup_path") or ""))
            if not backup_path.is_file():
                return {"status": "rollback_unavailable", "schema": GIT_STAGE_ROLLBACK_APPLY_SCHEMA, "error": "index_backup_missing"}
            backup_raw = backup_path.read_bytes()
            if _sha256(backup_raw) != operation["backup_sha256"]:
                return {"status": "rollback_unavailable", "schema": GIT_STAGE_ROLLBACK_APPLY_SCHEMA, "error": "index_backup_hash_mismatch"}
            rollback_key = str(operation["operation_key"]) + "-rollback"
            try:
                self._restore_index(index_path, backup_raw, operation_key=rollback_key)
                if self._index_state_sha256(repo) != operation["index_sha256_before"]:
                    raise RuntimeError("rollback_index_verification_failed")
                now = utc_now_iso()
                with self.store.connection() as conn:
                    cursor = conn.execute(
                        """
                        UPDATE git_stage_operations
                        SET status='rolled_back',rolled_back_at=?,rollback_note=?,updated_at=?
                        WHERE id=? AND status='applied' AND COALESCE(project_key,'')=COALESCE(?, '')
                        """,
                        (now, str(rollback_note or "").strip() or None, now, int(operation_id), _project(project_key)),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("rollback_audit_state_changed")
                    conn.commit()
                    updated = self._load_operation(conn, operation_id=int(operation_id), project_key=project_key) or {}
            except Exception:
                self._restore_index(index_path, current_raw, operation_key=rollback_key + "-recovery")
                return {"status": "error", "schema": GIT_STAGE_ROLLBACK_APPLY_SCHEMA, "error": "git_stage_rollback_audit_failed"}
            return {
                "status": "rolled_back",
                "schema": GIT_STAGE_ROLLBACK_APPLY_SCHEMA,
                "operation_id": int(operation_id),
                "operation": _public_operation(updated),
            }
