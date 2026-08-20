from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from mapi_capabilities.command_config import CommandCapabilityConfig, CommandRecipe
from mapi_capabilities.commands import CommandRecipeService
from mapi_capabilities.file_config import FileCapabilityConfig, FileRoot
from mapi_capabilities.file_writes import FileWriteService
from mapi_capabilities.files import FileCapabilityError, FileService
from mapi_capabilities.git_commits import GitCommitService
from mapi_capabilities.git_config import GitCapabilityConfig, GitRepository
from mapi_capabilities.git_service import GitService
from mapi_capabilities.git_staging import GitStageService
from mapi_capabilities.store import CapabilityStore

ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "MAPI Test")
    _git(repo, "config", "user.email", "mapi-test@example.invalid")
    (repo / "note.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "note.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_capabilities_do_not_import_aurora_or_platform_specific_processes() -> None:
    violations: list[str] = []
    for path in (ROOT / "mapi_capabilities").rglob("*.py"):
        text = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.startswith("mapi_public"):
                    violations.append(f"{path.name}:{module}")
        lowered = text.casefold()
        for marker in ("taskkill", "systemctl", "/etc/systemd/"):
            if marker in lowered:
                violations.append(f"{path.name}:{marker}")
    assert violations == []


def test_file_preview_apply_and_rollback(tmp_path: Path) -> None:
    root = tmp_path / "files"
    root.mkdir()
    target = root / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    file_root = FileRoot(root_id="root_test", path=root.resolve(), name="files", write_allowed=True)
    config = FileCapabilityConfig(
        enabled=True, roots=(file_root,), write_enabled=True,
        project_root_bindings=(("project-a", ("root_test",)),),
        project_write_bindings=(("project-a", ("root_test",)),),
    )
    files = FileService(config)
    writes = FileWriteService(files, CapabilityStore(tmp_path / "audit.db"))

    read = files.read_text(root_id="root_test", relative_path="note.txt", project_key="project-a")
    assert read["status"] == "ok"
    preview = writes.preview_write(project_key="project-a", root_id="root_test", relative_path="note.txt", content="after\n")
    assert preview["status"] == "preview_ready"
    applied = writes.apply_write(
        project_key="project-a", root_id="root_test", relative_path="note.txt", content="after\n",
        expected_preview_hash=preview["preview_hash"], confirmed=True,
    )
    assert applied["status"] == "applied"
    assert target.read_text(encoding="utf-8") == "after\n"
    rollback_preview = writes.preview_rollback(project_key="project-a", operation_id=applied["operation"]["id"])
    assert rollback_preview["status"] == "preview_ready"
    rolled = writes.rollback(
        project_key="project-a", operation_id=applied["operation"]["id"],
        expected_preview_hash=rollback_preview["preview_hash"], confirmed=True, rollback_note="test",
    )
    assert rolled["status"] == "rolled_back"
    assert target.read_text(encoding="utf-8") == "before\n"


def test_file_service_blocks_secret_paths(tmp_path: Path) -> None:
    root = tmp_path / "files"
    root.mkdir()
    (root / ".env").write_text("TOKEN=secret", encoding="utf-8")
    service = FileService(FileCapabilityConfig(enabled=True, roots=(FileRoot("root", root.resolve(), "root"),)))
    result = service.read_text(root_id="root", relative_path=".env", project_key=None)
    assert result["status"] == "denied"
    assert result["error"] == "protected_path"


def _git_services(tmp_path: Path):
    repo = _repo(tmp_path)
    repository = GitRepository(
        repo_id="repo_test", path=repo.resolve(), name="repo", commit_allowed=True, stage_allowed=True
    )
    config = GitCapabilityConfig(
        enabled=True, repositories=(repository,), commit_enabled=True, stage_enabled=True,
        project_repo_bindings=(("project-a", ("repo_test",)),),
        project_commit_bindings=(("project-a", ("repo_test",)),),
        project_stage_bindings=(("project-a", ("repo_test",)),),
    )
    service = GitService(config)
    store = CapabilityStore(tmp_path / "audit.db")
    return repo, service, GitStageService(service, store), GitCommitService(service, store)


def test_git_stage_apply_and_rollback(tmp_path: Path) -> None:
    repo, _git_service, stage, _commit = _git_services(tmp_path)
    (repo / "note.txt").write_text("two\n", encoding="utf-8")
    preview = stage.preview_stage(project_key="project-a", repo_id="repo_test", paths=["note.txt"])
    assert preview["status"] == "preview_ready"
    applied = stage.apply_stage(
        project_key="project-a", repo_id="repo_test", paths=["note.txt"],
        expected_preview_hash=preview["preview_hash"], confirmed=True,
    )
    assert applied["status"] == "applied"
    assert "note.txt" in _git(repo, "diff", "--cached", "--name-only")
    rollback_preview = stage.preview_rollback(project_key="project-a", operation_id=applied["operation"]["id"])
    assert rollback_preview["status"] == "preview_ready"
    rolled = stage.rollback(
        project_key="project-a", operation_id=applied["operation"]["id"],
        expected_preview_hash=rollback_preview["preview_hash"], confirmed=True, rollback_note="test",
    )
    assert rolled["status"] == "rolled_back"
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_git_commit_apply_and_soft_rollback(tmp_path: Path) -> None:
    repo, _git_service, stage, commit = _git_services(tmp_path)
    old_head = _git(repo, "rev-parse", "HEAD")
    (repo / "note.txt").write_text("two\n", encoding="utf-8")
    stage_preview = stage.preview_stage(project_key="project-a", repo_id="repo_test", paths=["note.txt"])
    staged = stage.apply_stage(
        project_key="project-a", repo_id="repo_test", paths=["note.txt"],
        expected_preview_hash=stage_preview["preview_hash"], confirmed=True,
    )
    assert staged["status"] == "applied"
    preview = commit.preview_commit(project_key="project-a", repo_id="repo_test", message="update note")
    assert preview["status"] == "preview_ready"
    applied = commit.apply_commit(
        project_key="project-a", repo_id="repo_test", message="update note",
        expected_preview_hash=preview["preview_hash"], confirmed=True,
    )
    assert applied["status"] == "applied"
    assert _git(repo, "rev-parse", "HEAD") != old_head
    rollback_preview = commit.preview_rollback(project_key="project-a", operation_id=applied["operation"]["id"])
    assert rollback_preview["status"] == "preview_ready"
    rolled = commit.rollback(
        project_key="project-a", operation_id=applied["operation"]["id"],
        expected_preview_hash=rollback_preview["preview_hash"], confirmed=True, rollback_note="test",
    )
    assert rolled["status"] == "rolled_back"
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert "note.txt" in _git(repo, "diff", "--cached", "--name-only")


def test_fixed_command_recipe_runs_without_shell(tmp_path: Path) -> None:
    script = tmp_path / "emit.py"
    script.write_text('print("hello from recipe")\n', encoding="utf-8")
    recipe = CommandRecipe(
        recipe_id="cmd_test", project_key="project-a", name="test", purpose="test fixed command",
        argv=(sys.executable, "emit.py"), workdir=tmp_path.resolve(), timeout_seconds=10,
    )
    service = CommandRecipeService(
        CommandCapabilityConfig(enabled=True, recipes=(recipe,)), CapabilityStore(tmp_path / "audit.db")
    )
    preview = service.preview(project_key="project-a", recipe_id="cmd_test")
    assert preview["status"] == "preview_ready"
    result = service.run(
        project_key="project-a", recipe_id="cmd_test", expected_preview_hash=preview["preview_hash"], confirmed=True
    )
    assert result["status"] == "completed"
    assert result["output"]["blocked"] is False
    assert "hello from recipe" in result["output"]["stdout"]
    runs = service.list_runs(project_key="project-a", recipe_id="cmd_test", limit=10)
    assert runs["count"] == 1
