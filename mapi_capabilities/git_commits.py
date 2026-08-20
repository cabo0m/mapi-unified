from __future__ import annotations

import hashlib
import json
import secrets
import threading
from pathlib import Path
from typing import Any

from mapi_core.core.time_score import utc_now_iso
from .git_service import GitCapabilityError, GitService
from mapi_core.memory.sensitivity import capture_sensitivity_gate
from .store import CapabilityStore, row_to_dict

GIT_COMMIT_PREVIEW_SCHEMA = "mapi_public_git_commit_preview.v1"
GIT_COMMIT_APPLY_SCHEMA = "mapi_public_git_commit_apply.v1"
GIT_COMMIT_OPERATIONS_SCHEMA = "mapi_public_git_commit_operations.v1"
GIT_COMMIT_ROLLBACK_PREVIEW_SCHEMA = "mapi_public_git_commit_rollback_preview.v1"
GIT_COMMIT_ROLLBACK_APPLY_SCHEMA = "mapi_public_git_commit_rollback_apply.v1"


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
        "old_head",
        "new_head",
        "index_sha256_before",
        "index_sha256_after",
        "staged_diff_sha256",
        "commit_message",
        "commit_message_sha256",
        "applied_at",
        "rolled_back_at",
        "rollback_note",
        "created_at",
        "updated_at",
    )
    return {field: item.get(field) for field in fields}


class GitCommitService:
    def __init__(self, git_service: GitService, store: CapabilityStore) -> None:
        if not git_service.config.effective_commit_enabled:
            raise ValueError("git_commit_capability_disabled")
        self.git_service = git_service
        self.store = store
        self._lock = threading.RLock()
        self.empty_hooks_dir = self.store.db_path.parent / ".mapi-git-hooks-empty"
        self.empty_hooks_dir.mkdir(parents=True, exist_ok=True)

    def _repo(self, repo_id: str, *, project_key: str | None):
        try:
            repo = self.git_service._repo(repo_id, project_key=project_key)
        except GitCapabilityError as exc:
            raise GitCapabilityError(exc.code) from exc
        if not repo.commit_allowed:
            raise GitCapabilityError("git_repository_read_only")
        if not self.git_service.config.commit_bound_to_project(repo.repo_id, project_key):
            raise GitCapabilityError("git_commit_not_bound_to_project")
        return repo

    def _required_message(self, message: str) -> tuple[str | None, dict[str, Any] | None]:
        value = str(message or "").strip()
        if not value:
            return None, {"status": "error", "error": "commit_message_required"}
        if "\n" in value or "\r" in value:
            return None, {"status": "denied", "error": "multiline_commit_message_not_allowed"}
        if len(value) > self.git_service.config.max_commit_message_chars:
            return None, {
                "status": "denied",
                "error": "commit_message_too_long",
                "max_chars": self.git_service.config.max_commit_message_chars,
            }
        sensitivity = capture_sensitivity_gate(value)
        if sensitivity["status"] != "allowed":
            return None, {
                "status": "denied",
                "error": "sensitive_commit_message_not_allowed",
                "sensitivity_class": sensitivity["sensitivity_class"],
            }
        return value, None

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
            raise GitCapabilityError("detached_head_commit_not_allowed")
        return branch

    def _index_hash(self, repo) -> str:
        raw = self._stdout(repo, ["ls-files", "--stage", "-z"], "git_index_read_failed")
        return _sha256(raw)

    def _remote_refs_containing(self, repo, commit_hash: str) -> list[str]:
        raw = self._stdout(
            repo,
            ["for-each-ref", f"--contains={commit_hash}", "--format=%(refname:short)", "refs/remotes"],
            "git_remote_ref_check_failed",
        )
        return [line.strip() for line in raw.decode("utf-8", "replace").splitlines() if line.strip()]

    def _load_operation(self, conn, *, operation_id: int, project_key: str | None) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT * FROM git_commit_operations
            WHERE id=? AND COALESCE(project_key,'')=COALESCE(?, '')
            """,
            (int(operation_id), _project(project_key)),
        ).fetchone()
        return row_to_dict(row)

    def preview_commit(
        self,
        *,
        project_key: str | None,
        repo_id: str,
        message: str,
    ) -> dict[str, Any]:
        normalized_message, error = self._required_message(message)
        if error is not None:
            return {"schema": GIT_COMMIT_PREVIEW_SCHEMA, **error}
        assert normalized_message is not None
        try:
            repo = self._repo(repo_id, project_key=project_key)
            branch = self._branch(repo)
            old_head = self._head(repo)
            index_sha = self._index_hash(repo)
        except GitCapabilityError as exc:
            return {"status": "denied" if exc.code in {"git_repository_read_only", "git_repository_not_bound_to_project", "git_commit_not_bound_to_project", "detached_head_commit_not_allowed"} else "error", "schema": GIT_COMMIT_PREVIEW_SCHEMA, "error": exc.code}
        staged = self.git_service.diff(repo_id=repo.repo_id, staged=True, project_key=project_key)
        if staged.get("status") != "ok":
            return {**staged, "schema": GIT_COMMIT_PREVIEW_SCHEMA}
        if staged.get("empty"):
            return {"status": "blocked", "schema": GIT_COMMIT_PREVIEW_SCHEMA, "error": "no_staged_changes"}
        staged_text = str(staged.get("diff") or "")
        payload = {
            "project_key": _project(project_key),
            "repo_id": repo.repo_id,
            "branch": branch,
            "old_head": old_head,
            "index_sha256": index_sha,
            "staged_diff_sha256": _sha256(staged_text.encode("utf-8")),
            "commit_message_sha256": _sha256(normalized_message.encode("utf-8")),
        }
        return {
            "status": "preview_ready",
            "schema": GIT_COMMIT_PREVIEW_SCHEMA,
            "preview_hash": _fingerprint(payload),
            "candidate": {
                **payload,
                "repo_name": repo.name,
                "short_old_head": old_head[:12],
                "commit_message": normalized_message,
                "staged_diff": staged_text,
            },
            "safety": {
                "read_only": True,
                "git_mutations_performed": 0,
                "hooks_will_run": False,
                "signing_will_run": False,
                "commit_uses_index_only_plumbing": True,
            },
        }

    def _reset_soft(self, repo, target_head: str) -> bool:
        completed = self.git_service._run(repo, ["reset", "--soft", target_head])
        return completed.returncode == 0

    def apply_commit(
        self,
        *,
        project_key: str | None,
        repo_id: str,
        message: str,
        expected_preview_hash: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            return {"status": "confirmation_required", "schema": GIT_COMMIT_APPLY_SCHEMA}
        with self._lock:
            preview = self.preview_commit(project_key=project_key, repo_id=repo_id, message=message)
            if preview.get("status") != "preview_ready":
                return {**preview, "schema": GIT_COMMIT_APPLY_SCHEMA}
            if str(expected_preview_hash or "") != str(preview["preview_hash"]):
                return {
                    "status": "stale_preview",
                    "schema": GIT_COMMIT_APPLY_SCHEMA,
                    "expected_preview_hash": str(expected_preview_hash or ""),
                    "current_preview_hash": preview["preview_hash"],
                }
            candidate = preview["candidate"]
            try:
                repo = self._repo(repo_id, project_key=project_key)
            except GitCapabilityError as exc:
                return {"status": "stale_preview", "schema": GIT_COMMIT_APPLY_SCHEMA, "error": exc.code}
            try:
                tree = self._stdout(repo, ["write-tree"], "git_write_tree_failed").decode("ascii").strip()
                new_commit = self._stdout(
                    repo,
                    [
                        "commit-tree", tree,
                        "-p", str(candidate["old_head"]),
                        "-m", str(candidate["commit_message"]),
                    ],
                    "git_commit_tree_failed",
                ).decode("ascii").strip()
            except (GitCapabilityError, UnicodeDecodeError):
                return {"status": "error", "schema": GIT_COMMIT_APPLY_SCHEMA, "error": "git_commit_failed"}
            if not new_commit:
                return {"status": "error", "schema": GIT_COMMIT_APPLY_SCHEMA, "error": "git_commit_failed"}
            completed = self.git_service._run(
                repo,
                ["update-ref", "HEAD", new_commit, str(candidate["old_head"])],
            )
            if completed.returncode != 0:
                return {"status": "stale_preview", "schema": GIT_COMMIT_APPLY_SCHEMA, "error": "git_head_changed"}
            try:
                new_head = self._head(repo)
                index_after = self._index_hash(repo)
            except GitCapabilityError:
                self._reset_soft(repo, str(candidate["old_head"]))
                return {"status": "error", "schema": GIT_COMMIT_APPLY_SCHEMA, "error": "post_commit_verification_failed"}
            if new_head == candidate["old_head"]:
                return {"status": "error", "schema": GIT_COMMIT_APPLY_SCHEMA, "error": "commit_head_did_not_advance"}

            operation_key = secrets.token_hex(16)
            now = utc_now_iso()
            try:
                with self.store.connection() as conn:
                    cursor = conn.execute(
                        """
                        INSERT INTO git_commit_operations (
                            operation_key,project_key,repo_id,branch,status,preview_hash,old_head,new_head,
                            index_sha256_before,index_sha256_after,staged_diff_sha256,commit_message,
                            commit_message_sha256,applied_at,created_at,updated_at
                        ) VALUES (?,?,?,?,'applied',?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            operation_key,
                            _project(project_key),
                            repo.repo_id,
                            candidate["branch"],
                            preview["preview_hash"],
                            candidate["old_head"],
                            new_head,
                            candidate["index_sha256"],
                            index_after,
                            candidate["staged_diff_sha256"],
                            candidate["commit_message"],
                            candidate["commit_message_sha256"],
                            now,
                            now,
                            now,
                        ),
                    )
                    operation_id = int(cursor.lastrowid)
                    conn.commit()
                    operation = row_to_dict(conn.execute("SELECT * FROM git_commit_operations WHERE id=?", (operation_id,)).fetchone()) or {}
            except Exception:
                current_head = None
                try:
                    current_head = self._head(repo)
                except GitCapabilityError:
                    pass
                if current_head == new_head:
                    self._reset_soft(repo, str(candidate["old_head"]))
                return {"status": "error", "schema": GIT_COMMIT_APPLY_SCHEMA, "error": "git_commit_audit_failed"}
            return {
                "status": "applied",
                "schema": GIT_COMMIT_APPLY_SCHEMA,
                "operation_id": operation_id,
                "operation": _public_operation(operation),
            }

    def list_operations(self, *, project_key: str | None, status: str | None = None, limit: int = 20) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            return {"status": "error", "schema": GIT_COMMIT_OPERATIONS_SCHEMA, "error": "limit_out_of_range"}
        normalized_status = str(status or "").strip() or None
        if normalized_status not in {None, "applied", "rolled_back"}:
            return {"status": "error", "schema": GIT_COMMIT_OPERATIONS_SCHEMA, "error": "unsupported_operation_status"}
        sql = "SELECT * FROM git_commit_operations WHERE COALESCE(project_key,'')=COALESCE(?, '')"
        params: list[Any] = [_project(project_key)]
        if normalized_status is not None:
            sql += " AND status=?"
            params.append(normalized_status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self.store.connection() as conn:
            items = [_public_operation(row_to_dict(row) or {}) for row in conn.execute(sql, params).fetchall()]
        return {"status": "ok", "schema": GIT_COMMIT_OPERATIONS_SCHEMA, "count": len(items), "items": items, "operation_status": normalized_status}

    def preview_rollback(self, *, project_key: str | None, operation_id: int) -> dict[str, Any]:
        with self.store.connection() as conn:
            operation = self._load_operation(conn, operation_id=int(operation_id), project_key=project_key)
        if operation is None:
            return {"status": "not_found", "schema": GIT_COMMIT_ROLLBACK_PREVIEW_SCHEMA, "operation_id": int(operation_id)}
        if operation["status"] == "rolled_back":
            return {"status": "already_rolled_back", "schema": GIT_COMMIT_ROLLBACK_PREVIEW_SCHEMA, "operation": _public_operation(operation)}
        try:
            repo = self._repo(str(operation["repo_id"]), project_key=project_key)
            branch = self._branch(repo)
            head = self._head(repo)
            index_sha = self._index_hash(repo)
            remote_refs = self._remote_refs_containing(repo, str(operation["new_head"]))
        except GitCapabilityError as exc:
            return {"status": "blocked", "schema": GIT_COMMIT_ROLLBACK_PREVIEW_SCHEMA, "error": exc.code}
        if branch != operation["branch"]:
            return {"status": "stale_operation", "schema": GIT_COMMIT_ROLLBACK_PREVIEW_SCHEMA, "error": "branch_changed"}
        if head != operation["new_head"]:
            return {"status": "stale_operation", "schema": GIT_COMMIT_ROLLBACK_PREVIEW_SCHEMA, "error": "head_changed_since_commit"}
        if index_sha != operation["index_sha256_after"]:
            return {"status": "stale_operation", "schema": GIT_COMMIT_ROLLBACK_PREVIEW_SCHEMA, "error": "index_changed_since_commit"}
        if remote_refs:
            return {
                "status": "rollback_unavailable",
                "schema": GIT_COMMIT_ROLLBACK_PREVIEW_SCHEMA,
                "error": "commit_reachable_from_remote_ref",
                "remote_ref_count": len(remote_refs),
            }
        payload = {
            "operation_id": int(operation_id),
            "operation_key": operation["operation_key"],
            "project_key": _project(project_key),
            "repo_id": operation["repo_id"],
            "branch": operation["branch"],
            "old_head": operation["old_head"],
            "new_head": operation["new_head"],
            "index_sha256": index_sha,
        }
        return {
            "status": "preview_ready",
            "schema": GIT_COMMIT_ROLLBACK_PREVIEW_SCHEMA,
            "preview_hash": _fingerprint(payload),
            "rollback": payload,
            "safety": {"read_only": True, "git_mutations_performed": 0, "reset_mode": "soft"},
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
            return {"status": "confirmation_required", "schema": GIT_COMMIT_ROLLBACK_APPLY_SCHEMA}
        with self._lock:
            preview = self.preview_rollback(project_key=project_key, operation_id=int(operation_id))
            if preview.get("status") == "already_rolled_back":
                return {**preview, "schema": GIT_COMMIT_ROLLBACK_APPLY_SCHEMA}
            if preview.get("status") != "preview_ready":
                return {**preview, "schema": GIT_COMMIT_ROLLBACK_APPLY_SCHEMA}
            if str(expected_preview_hash or "") != str(preview["preview_hash"]):
                return {
                    "status": "stale_preview",
                    "schema": GIT_COMMIT_ROLLBACK_APPLY_SCHEMA,
                    "expected_preview_hash": str(expected_preview_hash or ""),
                    "current_preview_hash": preview["preview_hash"],
                }
            with self.store.connection() as conn:
                operation = self._load_operation(conn, operation_id=int(operation_id), project_key=project_key)
            if operation is None or operation.get("status") != "applied":
                return {"status": "stale_preview", "schema": GIT_COMMIT_ROLLBACK_APPLY_SCHEMA}
            try:
                repo = self._repo(str(operation["repo_id"]), project_key=project_key)
            except GitCapabilityError as exc:
                return {"status": "stale_preview", "schema": GIT_COMMIT_ROLLBACK_APPLY_SCHEMA, "error": exc.code}
            if not self._reset_soft(repo, str(operation["old_head"])):
                return {"status": "error", "schema": GIT_COMMIT_ROLLBACK_APPLY_SCHEMA, "error": "git_soft_reset_failed"}
            try:
                if self._head(repo) != operation["old_head"]:
                    raise RuntimeError("rollback_head_verification_failed")
                now = utc_now_iso()
                with self.store.connection() as conn:
                    cursor = conn.execute(
                        """
                        UPDATE git_commit_operations
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
                self._reset_soft(repo, str(operation["new_head"]))
                return {"status": "error", "schema": GIT_COMMIT_ROLLBACK_APPLY_SCHEMA, "error": "git_commit_rollback_audit_failed"}
            return {
                "status": "rolled_back",
                "schema": GIT_COMMIT_ROLLBACK_APPLY_SCHEMA,
                "operation_id": int(operation_id),
                "operation": _public_operation(updated),
            }
