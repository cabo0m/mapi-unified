from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _bool_env(value: str | None) -> bool:
    return str(value or "").strip().casefold() in _TRUE_VALUES


def _resolved_paths(raw_value: str | None) -> tuple[Path, ...]:
    raw = str(raw_value or "").strip()
    if not raw:
        return ()
    paths: list[Path] = []
    seen: set[Path] = set()
    for item in raw.split(os.pathsep):
        value = item.strip()
        if not value:
            continue
        path = Path(value).expanduser().resolve(strict=False)
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return tuple(paths)


def _repo_id(path: Path) -> str:
    canonical = os.path.normcase(str(path.resolve(strict=False)))
    return "repo_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _normalize_project_key(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _project_bindings_from_json(raw_value: str | None) -> tuple[dict[str, tuple[Path, ...]], str | None]:
    raw = str(raw_value or "").strip()
    if not raw:
        return {}, None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}, "git_project_repos_json_invalid"
    if not isinstance(decoded, dict):
        return {}, "git_project_repos_json_must_be_object"
    result: dict[str, tuple[Path, ...]] = {}
    for raw_project, raw_paths in decoded.items():
        project = _normalize_project_key(raw_project)
        if project is None:
            return {}, "git_project_key_required"
        if not isinstance(raw_paths, list) or not all(isinstance(item, str) for item in raw_paths):
            return {}, f"git_project_repos_must_be_string_array:{project}"
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
class GitRepository:
    repo_id: str
    path: Path
    name: str
    commit_allowed: bool = False
    stage_allowed: bool = False

    def public_dict(
        self, *, commit_allowed: bool | None = None, stage_allowed: bool | None = None
    ) -> dict[str, object]:
        effective_commit = self.commit_allowed if commit_allowed is None else bool(commit_allowed)
        effective_stage = self.stage_allowed if stage_allowed is None else bool(stage_allowed)
        return {
            "repo_id": self.repo_id,
            "name": self.name,
            "mode": (
                "guarded_stage_commit"
                if effective_stage
                else ("guarded_commit" if effective_commit else "read_only")
            ),
        }


@dataclass(frozen=True)
class GitCapabilityConfig:
    enabled: bool
    repositories: tuple[GitRepository, ...]
    max_output_bytes: int = 512 * 1024
    timeout_seconds: int = 20
    commit_enabled: bool = False
    stage_enabled: bool = False
    stage_max_file_bytes: int = 1024 * 1024
    unmatched_commit_repositories: tuple[Path, ...] = ()
    unmatched_stage_repositories: tuple[Path, ...] = ()
    max_commit_message_chars: int = 200
    project_repo_bindings: tuple[tuple[str, tuple[str, ...]], ...] = ()
    project_commit_bindings: tuple[tuple[str, tuple[str, ...]], ...] = ()
    project_stage_bindings: tuple[tuple[str, tuple[str, ...]], ...] = ()
    project_binding_error: str | None = None
    project_commit_binding_error: str | None = None
    project_stage_binding_error: str | None = None
    unmatched_project_repositories: tuple[Path, ...] = ()
    unmatched_project_commit_repositories: tuple[Path, ...] = ()
    unmatched_project_stage_repositories: tuple[Path, ...] = ()

    @classmethod
    def disabled(cls) -> "GitCapabilityConfig":
        return cls(enabled=False, repositories=())

    @classmethod
    def from_env(cls) -> "GitCapabilityConfig":
        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "GitCapabilityConfig":
        enabled = _bool_env(values.get("MAPI_GIT_ENABLED"))
        commit_enabled = _bool_env(values.get("MAPI_GIT_COMMIT_ENABLED"))
        stage_enabled = _bool_env(values.get("MAPI_GIT_STAGE_ENABLED"))
        read_paths = _resolved_paths(values.get("MAPI_GIT_REPOS"))
        commit_paths = _resolved_paths(values.get("MAPI_GIT_COMMIT_REPOS"))
        stage_paths = _resolved_paths(values.get("MAPI_GIT_STAGE_REPOS"))
        raw_bindings, binding_error = _project_bindings_from_json(values.get("MAPI_GIT_PROJECT_REPOS_JSON"))
        raw_commit_value = values.get("MAPI_GIT_PROJECT_COMMIT_REPOS_JSON")
        raw_commit_bindings, commit_binding_error = _project_bindings_from_json(raw_commit_value)
        raw_stage_value = values.get("MAPI_GIT_PROJECT_STAGE_REPOS_JSON")
        raw_stage_bindings, stage_binding_error = _project_bindings_from_json(raw_stage_value)
        try:
            max_output = int(str(values.get("MAPI_GIT_MAX_OUTPUT_BYTES", str(512 * 1024))).strip())
        except ValueError:
            max_output = -1
        try:
            timeout = int(str(values.get("MAPI_GIT_TIMEOUT_SECONDS", "20")).strip())
        except ValueError:
            timeout = -1
        try:
            max_message = int(str(values.get("MAPI_GIT_COMMIT_MAX_MESSAGE_CHARS", "200")).strip())
        except ValueError:
            max_message = -1
        try:
            stage_max_file = int(str(values.get("MAPI_GIT_STAGE_MAX_FILE_BYTES", str(1024 * 1024))).strip())
        except ValueError:
            stage_max_file = -1
        read_set = set(read_paths)
        commit_set = set(commit_paths)
        stage_set = set(stage_paths)
        if not str(raw_commit_value or "").strip() and commit_enabled and raw_bindings:
            raw_commit_bindings = {
                project: tuple(path for path in paths if path in commit_set)
                for project, paths in raw_bindings.items()
                if any(path in commit_set for path in paths)
            }
        if not str(raw_stage_value or "").strip() and stage_enabled and raw_bindings:
            raw_stage_bindings = {
                project: tuple(path for path in paths if path in stage_set)
                for project, paths in raw_bindings.items()
                if any(path in stage_set for path in paths)
            }
        repositories = tuple(
            GitRepository(
                repo_id=_repo_id(path),
                path=path,
                name=path.name or "repository",
                commit_allowed=path in commit_set,
                stage_allowed=path in stage_set,
            )
            for path in read_paths
        )
        id_by_path = {repo.path: repo.repo_id for repo in repositories}
        unmatched_project: list[Path] = []
        binding_rows: list[tuple[str, tuple[str, ...]]] = []
        for project, paths in sorted(raw_bindings.items()):
            repo_ids: list[str] = []
            for path in paths:
                repo_id = id_by_path.get(path)
                if repo_id is None:
                    unmatched_project.append(path)
                    continue
                if repo_id not in repo_ids:
                    repo_ids.append(repo_id)
            binding_rows.append((project, tuple(repo_ids)))
        commit_id_by_path = {repo.path: repo.repo_id for repo in repositories if repo.commit_allowed}
        unmatched_project_commit: list[Path] = []
        commit_binding_rows: list[tuple[str, tuple[str, ...]]] = []
        for project, paths in sorted(raw_commit_bindings.items()):
            repo_ids: list[str] = []
            for path in paths:
                repo_id = commit_id_by_path.get(path)
                if repo_id is None:
                    unmatched_project_commit.append(path)
                    continue
                if repo_id not in repo_ids:
                    repo_ids.append(repo_id)
            commit_binding_rows.append((project, tuple(repo_ids)))
        stage_id_by_path = {repo.path: repo.repo_id for repo in repositories if repo.stage_allowed}
        unmatched_project_stage: list[Path] = []
        stage_binding_rows: list[tuple[str, tuple[str, ...]]] = []
        for project, paths in sorted(raw_stage_bindings.items()):
            repo_ids: list[str] = []
            for path in paths:
                repo_id = stage_id_by_path.get(path)
                if repo_id is None:
                    unmatched_project_stage.append(path)
                    continue
                if repo_id not in repo_ids:
                    repo_ids.append(repo_id)
            stage_binding_rows.append((project, tuple(repo_ids)))
        unmatched = tuple(path for path in commit_paths if path not in read_set)
        unmatched_stage = tuple(path for path in stage_paths if path not in commit_set)
        return cls(
            enabled=enabled,
            repositories=repositories,
            max_output_bytes=max_output,
            timeout_seconds=timeout,
            commit_enabled=commit_enabled,
            stage_enabled=stage_enabled,
            stage_max_file_bytes=stage_max_file,
            unmatched_commit_repositories=unmatched,
            unmatched_stage_repositories=unmatched_stage,
            max_commit_message_chars=max_message,
            project_repo_bindings=tuple(binding_rows),
            project_commit_bindings=tuple(commit_binding_rows),
            project_stage_bindings=tuple(stage_binding_rows),
            project_binding_error=binding_error,
            project_commit_binding_error=commit_binding_error,
            project_stage_binding_error=stage_binding_error,
            unmatched_project_repositories=tuple(dict.fromkeys(unmatched_project)),
            unmatched_project_commit_repositories=tuple(dict.fromkeys(unmatched_project_commit)),
            unmatched_project_stage_repositories=tuple(dict.fromkeys(unmatched_project_stage)),
        )

    def _binding_map(self) -> dict[str, tuple[str, ...]]:
        return dict(self.project_repo_bindings)

    def repo_ids_for_project(self, project_key: str | None) -> tuple[str, ...]:
        mapping = self._binding_map()
        if not mapping:
            return tuple(repo.repo_id for repo in self.repositories)
        project = _normalize_project_key(project_key)
        if project is None:
            return ()
        return mapping.get(project, ())

    def repository_bound_to_project(self, repo_id: str, project_key: str | None) -> bool:
        return str(repo_id) in set(self.repo_ids_for_project(project_key))

    def commit_repo_ids_for_project(self, project_key: str | None) -> tuple[str, ...]:
        project = str(project_key or "").strip()
        if not project:
            return ()
        return dict(self.project_commit_bindings).get(project, ())

    def stage_repo_ids_for_project(self, project_key: str | None) -> tuple[str, ...]:
        project = str(project_key or "").strip()
        if not project:
            return ()
        return dict(self.project_stage_bindings).get(project, ())

    def commit_bound_to_project(self, repo_id: str, project_key: str | None) -> bool:
        repo = next((item for item in self.repositories if item.repo_id == str(repo_id)), None)
        return bool(
            self.commit_enabled
            and repo
            and repo.commit_allowed
            and str(repo_id) in set(self.commit_repo_ids_for_project(project_key))
        )

    def stage_bound_to_project(self, repo_id: str, project_key: str | None) -> bool:
        repo = next((item for item in self.repositories if item.repo_id == str(repo_id)), None)
        return bool(
            self.stage_enabled
            and repo
            and repo.stage_allowed
            and str(repo_id) in set(self.stage_repo_ids_for_project(project_key))
        )

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if self.commit_enabled and not self.enabled:
            errors.append("git_commit_requires_git_enabled")
        if self.stage_enabled and not self.commit_enabled:
            errors.append("git_stage_requires_commit_enabled")
        if self.stage_enabled and not (1024 <= self.stage_max_file_bytes <= 16 * 1024 * 1024):
            errors.append("git_stage_max_file_bytes_out_of_range")
        if self.stage_enabled and not any(repo.stage_allowed for repo in self.repositories):
            errors.append("git_stage_repositories_required_when_enabled")
        if self.unmatched_stage_repositories:
            errors.append("git_stage_repositories_must_be_subset_of_commit_repositories")
        if self.project_binding_error:
            errors.append(self.project_binding_error)
        if self.project_commit_binding_error:
            errors.append(self.project_commit_binding_error)
        if self.project_stage_binding_error:
            errors.append(self.project_stage_binding_error)
        if not self.enabled:
            return errors
        if not self.repositories:
            errors.append("git_repositories_required_when_enabled")
        if self.max_output_bytes < 1024 or self.max_output_bytes > 4 * 1024 * 1024:
            errors.append("git_max_output_bytes_out_of_range")
        if self.timeout_seconds < 1 or self.timeout_seconds > 120:
            errors.append("git_timeout_seconds_out_of_range")
        for repo in self.repositories:
            if not repo.path.exists():
                errors.append(f"git_repository_missing:{repo.repo_id}")
            elif not repo.path.is_dir():
                errors.append(f"git_repository_not_directory:{repo.repo_id}")
            if repo.stage_allowed and not repo.commit_allowed:
                errors.append(f"git_stage_repository_requires_commit_permission:{repo.repo_id}")
        if self.unmatched_project_repositories:
            errors.append("git_project_repositories_must_be_subset_of_read_repositories")
        if self.unmatched_project_commit_repositories:
            errors.append("git_project_commit_repositories_must_be_subset_of_commit_repositories")
        if self.unmatched_project_stage_repositories:
            errors.append("git_project_stage_repositories_must_be_subset_of_stage_repositories")
        if self.commit_enabled:
            if not any(repo.commit_allowed for repo in self.repositories):
                errors.append("git_commit_repositories_required_when_enabled")
            if self.unmatched_commit_repositories:
                errors.append("git_commit_repositories_must_be_subset_of_read_repositories")
            if self.max_commit_message_chars < 1 or self.max_commit_message_chars > 1000:
                errors.append("git_commit_message_limit_out_of_range")
            if not self.project_commit_bindings:
                errors.append("git_project_commit_bindings_required_when_commit_enabled")
            else:
                read_map = dict(self.project_repo_bindings)
                commit_map = dict(self.project_commit_bindings)
                for project, commit_ids in commit_map.items():
                    read_ids = set(read_map.get(project, ()))
                    for repo_id in commit_ids:
                        if repo_id not in read_ids:
                            errors.append(f"git_project_commit_requires_read_binding:{project}:{repo_id}")
                bound_commit_ids = {repo_id for _project, repo_ids in self.project_commit_bindings for repo_id in repo_ids}
                for repo in self.repositories:
                    if repo.commit_allowed and repo.repo_id not in bound_commit_ids:
                        errors.append(f"git_commit_repository_requires_project_commit_binding:{repo.repo_id}")
        if self.stage_enabled:
            if not self.project_stage_bindings:
                errors.append("git_project_stage_bindings_required_when_stage_enabled")
            else:
                commit_map = dict(self.project_commit_bindings)
                stage_map = dict(self.project_stage_bindings)
                for project, stage_ids in stage_map.items():
                    commit_ids = set(commit_map.get(project, ()))
                    for repo_id in stage_ids:
                        if repo_id not in commit_ids:
                            errors.append(f"git_project_stage_requires_commit_binding:{project}:{repo_id}")
                bound_stage_ids = {repo_id for _project, repo_ids in self.project_stage_bindings for repo_id in repo_ids}
                for repo in self.repositories:
                    if repo.stage_allowed and repo.repo_id not in bound_stage_ids:
                        errors.append(f"git_stage_repository_requires_project_stage_binding:{repo.repo_id}")
        return errors

    def public_repositories(self, project_key: str | None = None) -> list[dict[str, object]]:
        if project_key is None:
            return [
                repo.public_dict(
                    commit_allowed=self.commit_enabled and repo.commit_allowed,
                    stage_allowed=self.stage_enabled and repo.stage_allowed,
                )
                for repo in self.repositories
            ]
        allowed = set(self.repo_ids_for_project(project_key))
        commit_allowed = set(self.commit_repo_ids_for_project(project_key))
        stage_allowed = set(self.stage_repo_ids_for_project(project_key))
        return [
            repo.public_dict(
                commit_allowed=repo.repo_id in commit_allowed,
                stage_allowed=repo.repo_id in stage_allowed,
            )
            for repo in self.repositories
            if repo.repo_id in allowed
        ]

    @property
    def effective_stage_enabled(self) -> bool:
        return (
            self.stage_enabled
            and self.effective_commit_enabled
            and any(repo.stage_allowed for repo in self.repositories)
            and bool(self.project_stage_bindings)
            and not self.validation_errors()
        )

    @property
    def effective_commit_enabled(self) -> bool:
        return (
            self.enabled
            and self.commit_enabled
            and any(repo.commit_allowed for repo in self.repositories)
            and bool(self.project_commit_bindings)
            and not self.validation_errors()
        )
