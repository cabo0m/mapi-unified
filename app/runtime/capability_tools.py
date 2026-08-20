from __future__ import annotations

from typing import Any

from app import memory_config
from mapi_capabilities.command_config import CommandCapabilityConfig
from mapi_capabilities.commands import CommandRecipeService
from mapi_capabilities.file_config import FileCapabilityConfig
from mapi_capabilities.file_writes import FileWriteService
from mapi_capabilities.files import FileService
from mapi_capabilities.git_commits import GitCommitService
from mapi_capabilities.git_config import GitCapabilityConfig
from mapi_capabilities.git_service import GitService
from mapi_capabilities.git_staging import GitStageService
from mapi_capabilities.store import CapabilityStore


def _store() -> CapabilityStore:
    return CapabilityStore(memory_config.DB_PATH)


def _files() -> FileService:
    return FileService(FileCapabilityConfig.from_env())


def _file_writes() -> FileWriteService:
    return FileWriteService(_files(), _store())


def _git() -> GitService:
    return GitService(GitCapabilityConfig.from_env())


def _git_stage() -> GitStageService:
    return GitStageService(_git(), _store())


def _git_commit() -> GitCommitService:
    return GitCommitService(_git(), _store())


def _commands() -> CommandRecipeService:
    return CommandRecipeService(CommandCapabilityConfig.from_env(), _store())


def list_project_file_roots(project_key: str | None = None) -> dict[str, Any]:
    return _files().list_roots(project_key=project_key)


def list_project_directory(project_key: str | None, root_id: str, relative_path: str = ".", limit: int = 200) -> dict[str, Any]:
    return _files().list_directory(root_id=root_id, relative_path=relative_path, limit=limit, project_key=project_key)


def read_project_file_text(project_key: str | None, root_id: str, relative_path: str) -> dict[str, Any]:
    return _files().read_text(root_id=root_id, relative_path=relative_path, project_key=project_key)


def preview_project_file_write(project_key: str | None, root_id: str, relative_path: str, content: str) -> dict[str, Any]:
    return _file_writes().preview_write(project_key=project_key, root_id=root_id, relative_path=relative_path, content=content)


def apply_project_file_write(project_key: str | None, root_id: str, relative_path: str, content: str, expected_preview_hash: str, confirmed: bool) -> dict[str, Any]:
    return _file_writes().apply_write(project_key=project_key, root_id=root_id, relative_path=relative_path, content=content, expected_preview_hash=expected_preview_hash, confirmed=confirmed)


def list_project_file_operations(project_key: str | None, status: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _file_writes().list_operations(project_key=project_key, status=status, limit=limit)


def preview_project_file_rollback(project_key: str | None, operation_id: int) -> dict[str, Any]:
    return _file_writes().preview_rollback(project_key=project_key, operation_id=operation_id)


def rollback_project_file_write(project_key: str | None, operation_id: int, expected_preview_hash: str, confirmed: bool, rollback_note: str | None = None) -> dict[str, Any]:
    return _file_writes().rollback(project_key=project_key, operation_id=operation_id, expected_preview_hash=expected_preview_hash, confirmed=confirmed, rollback_note=rollback_note)


def list_project_git_repositories(project_key: str | None = None) -> dict[str, Any]:
    return _git().list_repositories(project_key=project_key)


def project_git_info(project_key: str | None, repo_id: str) -> dict[str, Any]:
    return _git().info(repo_id=repo_id, project_key=project_key)


def project_git_status(project_key: str | None, repo_id: str) -> dict[str, Any]:
    return _git().status(repo_id=repo_id, project_key=project_key)


def project_git_diff(project_key: str | None, repo_id: str, staged: bool = False) -> dict[str, Any]:
    return _git().diff(repo_id=repo_id, staged=staged, project_key=project_key)


def project_git_log(project_key: str | None, repo_id: str, limit: int = 20) -> dict[str, Any]:
    return _git().log(repo_id=repo_id, limit=limit, project_key=project_key)


def preview_project_git_stage(project_key: str | None, repo_id: str, paths: list[str]) -> dict[str, Any]:
    return _git_stage().preview_stage(project_key=project_key, repo_id=repo_id, paths=paths)


def apply_project_git_stage(project_key: str | None, repo_id: str, paths: list[str], expected_preview_hash: str, confirmed: bool) -> dict[str, Any]:
    return _git_stage().apply_stage(project_key=project_key, repo_id=repo_id, paths=paths, expected_preview_hash=expected_preview_hash, confirmed=confirmed)


def list_project_git_stage_operations(project_key: str | None, status: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _git_stage().list_operations(project_key=project_key, status=status, limit=limit)


def preview_project_git_stage_rollback(project_key: str | None, operation_id: int) -> dict[str, Any]:
    return _git_stage().preview_rollback(project_key=project_key, operation_id=operation_id)


def rollback_project_git_stage(project_key: str | None, operation_id: int, expected_preview_hash: str, confirmed: bool, rollback_note: str | None = None) -> dict[str, Any]:
    return _git_stage().rollback(project_key=project_key, operation_id=operation_id, expected_preview_hash=expected_preview_hash, confirmed=confirmed, rollback_note=rollback_note)


def preview_project_git_commit(project_key: str | None, repo_id: str, message: str) -> dict[str, Any]:
    return _git_commit().preview_commit(project_key=project_key, repo_id=repo_id, message=message)


def apply_project_git_commit(project_key: str | None, repo_id: str, message: str, expected_preview_hash: str, confirmed: bool) -> dict[str, Any]:
    return _git_commit().apply_commit(project_key=project_key, repo_id=repo_id, message=message, expected_preview_hash=expected_preview_hash, confirmed=confirmed)


def list_project_git_commit_operations(project_key: str | None, status: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _git_commit().list_operations(project_key=project_key, status=status, limit=limit)


def preview_project_git_commit_rollback(project_key: str | None, operation_id: int) -> dict[str, Any]:
    return _git_commit().preview_rollback(project_key=project_key, operation_id=operation_id)


def rollback_project_git_commit(project_key: str | None, operation_id: int, expected_preview_hash: str, confirmed: bool, rollback_note: str | None = None) -> dict[str, Any]:
    return _git_commit().rollback(project_key=project_key, operation_id=operation_id, expected_preview_hash=expected_preview_hash, confirmed=confirmed, rollback_note=rollback_note)


def list_project_command_recipes(project_key: str | None) -> dict[str, Any]:
    return _commands().list_recipes(project_key=project_key)


def preview_project_command_recipe(project_key: str | None, recipe_id: str) -> dict[str, Any]:
    return _commands().preview(project_key=project_key, recipe_id=recipe_id)


def run_project_command_recipe(project_key: str | None, recipe_id: str, expected_preview_hash: str, confirmed: bool) -> dict[str, Any]:
    return _commands().run(project_key=project_key, recipe_id=recipe_id, expected_preview_hash=expected_preview_hash, confirmed=confirmed)


def list_project_command_runs(project_key: str | None, recipe_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _commands().list_runs(project_key=project_key, recipe_id=recipe_id, limit=limit)
