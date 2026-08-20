from __future__ import annotations

import difflib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .git_config import GitCapabilityConfig, GitRepository
from mapi_core.memory.sensitivity import capture_sensitivity_gate

GIT_REPOSITORIES_SCHEMA = "mapi_public_git_repositories.v1"
GIT_INFO_SCHEMA = "mapi_public_git_info.v1"
GIT_STATUS_SCHEMA = "mapi_public_git_status.v1"
GIT_DIFF_SCHEMA = "mapi_public_git_diff.v1"
GIT_LOG_SCHEMA = "mapi_public_git_log.v1"


class GitCapabilityError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GitService:
    def __init__(self, config: GitCapabilityConfig) -> None:
        errors = config.validation_errors()
        if not config.enabled:
            raise ValueError("git_capability_disabled")
        if errors:
            raise ValueError("git_capability_invalid:" + ",".join(errors))
        executable = shutil.which("git")
        if not executable:
            raise ValueError("git_executable_not_found")
        self.config = config
        self.git_executable = executable
        self._repos = {repo.repo_id: repo for repo in config.repositories}
        self._validate_repositories()

    def _run(
        self, repo: GitRepository, args: list[str], *, input_bytes: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        command = [
            self.git_executable,
            "-c",
            "core.pager=cat",
            "-c",
            "color.ui=false",
            "-c",
            "core.fsmonitor=false",
            *args,
        ]
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("GIT_")
        }
        env["GIT_PAGER"] = "cat"
        env["PAGER"] = "cat"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_NO_LAZY_FETCH"] = "1"
        try:
            completed = subprocess.run(
                command,
                cwd=str(repo.path),
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCapabilityError("git_command_timeout") from exc
        if len(completed.stdout) > self.config.max_output_bytes or len(completed.stderr) > self.config.max_output_bytes:
            raise GitCapabilityError("git_output_too_large")
        return completed

    def _text(self, raw: bytes) -> str:
        return raw.decode("utf-8", errors="replace")

    def _require_success(self, completed: subprocess.CompletedProcess[bytes], code: str) -> bytes:
        if completed.returncode != 0:
            raise GitCapabilityError(code)
        return completed.stdout

    def _validate_repositories(self) -> None:
        for repo in self.config.repositories:
            result = self._run(repo, ["rev-parse", "--show-toplevel"])
            raw = self._require_success(result, "not_a_git_repository")
            top = Path(self._text(raw).strip()).resolve(strict=True)
            if top != repo.path.resolve(strict=True):
                raise ValueError(f"git_repository_must_be_top_level:{repo.repo_id}")

    def _repo(self, repo_id: str, *, project_key: str | None = None) -> GitRepository:
        repo = self._repos.get(str(repo_id or "").strip())
        if repo is None:
            raise GitCapabilityError("unknown_git_repository")
        if not self.config.repository_bound_to_project(repo.repo_id, project_key):
            raise GitCapabilityError("git_repository_not_bound_to_project")
        return repo

    def list_repositories(self, *, project_key: str | None = None) -> dict[str, Any]:
        repositories = self.config.public_repositories(project_key)
        return {
            "status": "ok",
            "schema": GIT_REPOSITORIES_SCHEMA,
            "count": len(repositories),
            "repositories": repositories,
            "project_bound": bool(self.config.project_repo_bindings),
        }

    def _index_entries(self, repo: GitRepository) -> tuple[dict[str, tuple[str, str]], set[str]]:
        raw = self._require_success(
            self._run(repo, ["ls-files", "--stage", "-z"]),
            "git_index_read_failed",
        )
        entries: dict[str, tuple[str, str]] = {}
        unmerged: set[str] = set()
        for record in raw.split(b"\x00"):
            if not record:
                continue
            try:
                metadata, raw_path = record.split(b"\t", 1)
                mode, oid, stage = metadata.decode("ascii").split(" ", 2)
                path = raw_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise GitCapabilityError("git_index_entry_invalid") from exc
            if stage != "0":
                unmerged.add(path)
                continue
            entries[path] = (mode, oid)
        return entries, unmerged

    def _head_entries(self, repo: GitRepository) -> dict[str, tuple[str, str]]:
        completed = self._run(repo, ["ls-tree", "-r", "-z", "HEAD"])
        if completed.returncode != 0:
            return {}
        entries: dict[str, tuple[str, str]] = {}
        for record in completed.stdout.split(b"\x00"):
            if not record:
                continue
            try:
                metadata, raw_path = record.split(b"\t", 1)
                mode, object_type, oid = metadata.decode("ascii").split(" ", 2)
                path = raw_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise GitCapabilityError("git_head_entry_invalid") from exc
            if object_type == "blob" or mode == "160000":
                entries[path] = (mode, oid)
        return entries

    def _worktree_oid(self, repo: GitRepository, path: str, mode: str) -> str | None:
        target = repo.path.joinpath(*Path(path).parts)
        if not target.exists() and not target.is_symlink():
            return None
        if mode == "160000" or target.is_dir():
            return "__submodule_or_directory__"
        if target.is_symlink():
            try:
                raw = os.readlink(target).encode("utf-8")
            except (OSError, UnicodeEncodeError) as exc:
                raise GitCapabilityError("git_worktree_symlink_read_failed") from exc
            completed = self._run(
                repo,
                ["hash-object", "--no-filters", "--stdin"],
                input_bytes=raw,
            )
        else:
            completed = self._run(repo, ["hash-object", "--no-filters", "--", path])
        return self._text(self._require_success(completed, "git_worktree_hash_failed")).strip()

    def _safe_status_entries(self, repo: GitRepository) -> list[dict[str, str]]:
        index, unmerged = self._index_entries(repo)
        head = self._head_entries(repo)
        entries: list[dict[str, str]] = []
        tracked_paths = sorted(set(head) | set(index) | unmerged)
        for path in tracked_paths:
            if path in unmerged:
                entries.append({"xy": "UU", "path": path})
                continue
            head_entry = head.get(path)
            index_entry = index.get(path)
            if head_entry is None and index_entry is not None:
                staged = "A"
            elif head_entry is not None and index_entry is None:
                staged = "D"
            elif head_entry != index_entry:
                staged = "M"
            else:
                staged = " "
            unstaged = " "
            if index_entry is not None:
                mode, oid = index_entry
                worktree_oid = self._worktree_oid(repo, path, mode)
                if worktree_oid is None:
                    unstaged = "D"
                elif worktree_oid == "__submodule_or_directory__":
                    # Do not enter a nested repository or execute its configuration.
                    unstaged = "M" if mode != "160000" else " "
                elif worktree_oid != oid:
                    unstaged = "M"
            if staged != " " or unstaged != " ":
                entries.append({"xy": staged + unstaged, "path": path})
        raw_untracked = self._require_success(
            self._run(repo, ["ls-files", "--others", "--exclude-standard", "-z"]),
            "git_untracked_read_failed",
        )
        for raw_path in raw_untracked.split(b"\x00"):
            if not raw_path:
                continue
            try:
                path = raw_path.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GitCapabilityError("git_untracked_path_invalid") from exc
            entries.append({"xy": "??", "path": path})
        return sorted(entries, key=lambda item: item["path"])

    def _blob_bytes(self, repo: GitRepository, oid: str) -> bytes:
        size_raw = self._require_success(
            self._run(repo, ["cat-file", "-s", oid]),
            "git_blob_size_failed",
        )
        try:
            size = int(self._text(size_raw).strip())
        except ValueError as exc:
            raise GitCapabilityError("git_blob_size_invalid") from exc
        if size > self.config.max_output_bytes:
            raise GitCapabilityError("git_output_too_large")
        return self._require_success(
            self._run(repo, ["cat-file", "blob", oid]),
            "git_blob_read_failed",
        )

    def _raw_unstaged_diff(self, repo: GitRepository) -> str:
        index, unmerged = self._index_entries(repo)
        if unmerged:
            raise GitCapabilityError("git_unmerged_index_not_supported")
        chunks: list[str] = []
        total_bytes = 0
        for path in sorted(index):
            mode, oid = index[path]
            if mode == "160000":
                continue
            worktree_oid = self._worktree_oid(repo, path, mode)
            if worktree_oid == oid:
                continue
            old_raw = self._blob_bytes(repo, oid)
            target = repo.path.joinpath(*Path(path).parts)
            if worktree_oid is None:
                new_raw = b""
            elif target.is_symlink():
                try:
                    new_raw = os.readlink(target).encode("utf-8")
                except (OSError, UnicodeEncodeError) as exc:
                    raise GitCapabilityError("git_worktree_symlink_read_failed") from exc
            elif target.is_file():
                try:
                    size = target.stat().st_size
                except OSError as exc:
                    raise GitCapabilityError("git_worktree_stat_failed") from exc
                if size > self.config.max_output_bytes:
                    raise GitCapabilityError("git_output_too_large")
                new_raw = target.read_bytes()
            else:
                continue
            if b"\x00" in old_raw or b"\x00" in new_raw:
                chunk = f"Binary files a/{path} and b/{path} differ\n"
            else:
                try:
                    old_text = old_raw.decode("utf-8")
                    new_text = new_raw.decode("utf-8")
                except UnicodeDecodeError:
                    chunk = f"Binary files a/{path} and b/{path} differ\n"
                else:
                    chunk = "".join(
                        difflib.unified_diff(
                            old_text.splitlines(keepends=True),
                            new_text.splitlines(keepends=True),
                            fromfile=f"a/{path}",
                            tofile=f"b/{path}",
                            lineterm="\n",
                        )
                    )
                    if chunk and not chunk.endswith("\n"):
                        chunk += "\n"
            total_bytes += len(chunk.encode("utf-8"))
            if total_bytes > self.config.max_output_bytes:
                raise GitCapabilityError("git_output_too_large")
            chunks.append(chunk)
        return "".join(chunks)

    def info(self, *, repo_id: str, project_key: str | None = None) -> dict[str, Any]:
        try:
            repo = self._repo(repo_id, project_key=project_key)
            head = self._text(self._require_success(self._run(repo, ["rev-parse", "HEAD"]), "git_head_failed")).strip()
            branch_result = self._run(repo, ["branch", "--show-current"])
            branch = self._text(self._require_success(branch_result, "git_branch_failed")).strip() or None
            entries = self._safe_status_entries(repo)
            dirty = bool(entries)
        except GitCapabilityError as exc:
            return {"status": "error", "schema": GIT_INFO_SCHEMA, "error": exc.code}
        return {
            "status": "ok",
            "schema": GIT_INFO_SCHEMA,
            "repo_id": repo.repo_id,
            "name": repo.name,
            "head": head,
            "short_head": head[:12],
            "branch": branch,
            "detached": branch is None,
            "dirty": dirty,
        }

    def status(self, *, repo_id: str, project_key: str | None = None) -> dict[str, Any]:
        try:
            repo = self._repo(repo_id, project_key=project_key)
            entries = self._safe_status_entries(repo)
        except GitCapabilityError as exc:
            return {"status": "error", "schema": GIT_STATUS_SCHEMA, "error": exc.code}
        return {
            "status": "ok",
            "schema": GIT_STATUS_SCHEMA,
            "repo_id": repo.repo_id,
            "name": repo.name,
            "dirty": bool(entries),
            "count": len(entries),
            "entries": entries,
        }

    def diff(self, *, repo_id: str, staged: bool = False, project_key: str | None = None) -> dict[str, Any]:
        try:
            repo = self._repo(repo_id, project_key=project_key)
            if staged:
                raw = self._require_success(
                    self._run(
                        repo,
                        ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color", "--unified=3"],
                    ),
                    "git_diff_failed",
                )
                if len(raw) > self.config.max_output_bytes:
                    raise GitCapabilityError("git_output_too_large")
                text = self._text(raw)
            else:
                text = self._raw_unstaged_diff(repo)
        except GitCapabilityError as exc:
            return {"status": "error", "schema": GIT_DIFF_SCHEMA, "error": exc.code}
        sensitivity = capture_sensitivity_gate(text)
        if sensitivity["status"] != "allowed":
            return {
                "status": "denied",
                "schema": GIT_DIFF_SCHEMA,
                "error": "sensitive_diff_not_returned",
                "sensitivity_class": sensitivity["sensitivity_class"],
                "reason_codes": list(sensitivity["reason_codes"]),
            }
        return {
            "status": "ok",
            "schema": GIT_DIFF_SCHEMA,
            "repo_id": repo.repo_id,
            "name": repo.name,
            "staged": bool(staged),
            "empty": not bool(text),
            "size_bytes": len(text.encode("utf-8")),
            "diff": text,
        }

    def log(self, *, repo_id: str, limit: int = 20, project_key: str | None = None) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            return {"status": "error", "schema": GIT_LOG_SCHEMA, "error": "limit_out_of_range", "allowed_range": [1, 100]}
        try:
            repo = self._repo(repo_id, project_key=project_key)
            raw = self._require_success(
                self._run(
                    repo,
                    ["log", f"-n{int(limit)}", "--no-decorate", "--format=%H%x00%an%x00%aI%x00%s%x00"],
                ),
                "git_log_failed",
            )
        except GitCapabilityError as exc:
            return {"status": "error", "schema": GIT_LOG_SCHEMA, "error": exc.code}
        values = self._text(raw).split("\x00")
        if values and values[-1] == "":
            values.pop()
        items: list[dict[str, Any]] = []
        for index in range(0, len(values), 4):
            chunk = values[index:index + 4]
            if len(chunk) != 4:
                break
            commit_hash, author_name, authored_at, subject = chunk
            items.append(
                {
                    "hash": commit_hash,
                    "short_hash": commit_hash[:12],
                    "author_name": author_name,
                    "authored_at": authored_at,
                    "subject": subject,
                }
            )
        return {
            "status": "ok",
            "schema": GIT_LOG_SCHEMA,
            "repo_id": repo.repo_id,
            "name": repo.name,
            "count": len(items),
            "items": items,
        }
