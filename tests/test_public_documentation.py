from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import audit_public_repository as public_audit


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
EXAMPLE_MODULE = re.compile(
    r"<!-- example-module: (?P<path>[^ ]+) -->\s*"
    r"```python\n(?P<source>.*?)\n```",
    re.DOTALL,
)


def _markdown_files() -> list[Path]:
    return sorted(
        [*ROOT.glob("*.md"), *ROOT.joinpath("docs").rglob("*.md")],
        key=lambda path: path.as_posix(),
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _temporary_public_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "public-repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", public_audit.ALLOWED_AUTHOR_NAME)
    _git(repository, "config", "user.email", public_audit.ALLOWED_PUBLIC_EMAIL)
    repository.joinpath("README.md").write_text("# Public test repository\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial public test commit")
    return repository


def _scan_temporary_repository(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    monkeypatch.setattr(public_audit, "ROOT", repository)
    failures: list[dict[str, str]] = []
    return public_audit._scan_git_metadata(failures), failures


def test_readme_quickstart_matches_console_entry_points() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = metadata["project"]["scripts"]
    required = {
        "mapi": "mapi.cli:main",
        "mapi-maintenance": "mapi.maintenance:main",
        "mapi-init": "mapi.cli:init",
        "mapi-server": "mapi.cli:server",
        "mapi-migrate": "mapi.cli:migrate",
        "mapi-doctor": "mapi.cli:doctor",
        "mapi-recover": "mapi.cli:recover",
        "mapi-seed-demo": "mapi.cli:seed_demo",
        "mapi-demo": "mapi.cli:demo",
        "mapi-capabilities": "mapi.capabilities:main",
    }
    assert scripts == required
    for command in ("mapi", "mapi-init", "mapi-doctor", "mapi-recover", "mapi-server", "mapi-demo"):
        assert command in readme
    assert "pip install -e ." in readme


def test_installation_and_mcp_docs_are_unified_and_platform_specific() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    installation = (ROOT / "docs" / "INSTALLATION.md").read_text(encoding="utf-8")
    integration = (ROOT / "docs" / "MCP_INTEGRATION.md").read_text(encoding="utf-8")
    historical_release = (ROOT / "docs" / "RELEASE_NOTES_0.1.0_RC2.md").read_text(encoding="utf-8")

    for current_doc in (readme, installation, integration):
        assert "cd mapi-agent-memory" not in current_doc
        assert "github.com/cabo0m/mapi-agent-memory" not in current_doc

    assert "cd mapi-unified" in readme
    assert "cd mapi-unified" in installation
    assert "## Windows: local installation step by step" in installation
    assert "## Linux: local installation step by step" in installation
    assert "## Linux/VPS: remote installation for ChatGPT web" in installation
    assert "## Windows: connect a local MCP client" in integration
    assert "## Linux: connect a local MCP client" in integration
    assert "## Linux/VPS: connect ChatGPT web to remote MAPI" in integration
    assert "ChatGPT does not directly connect to a localhost MCP server" in integration
    assert "http://127.0.0.1:8015/mcp/" in integration
    assert "vps-remote-auth" in integration
    assert "Archive only" in historical_release


def test_all_local_markdown_links_exist() -> None:
    failures: list[str] = []
    for document in _markdown_files():
        text = document.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            path_text = target.split("#", 1)[0]
            if not path_text:
                continue
            resolved = (document.parent / path_text).resolve()
            if not resolved.exists():
                failures.append(f"{document.relative_to(ROOT)} -> {target}")
    assert failures == []


def test_mermaid_blocks_are_closed_and_nonempty() -> None:
    failures: list[str] = []
    for document in _markdown_files():
        lines = document.read_text(encoding="utf-8").splitlines()
        in_mermaid = False
        content_lines = 0
        for line_number, line in enumerate(lines, start=1):
            if line.strip() == "```mermaid":
                if in_mermaid:
                    failures.append(f"{document.relative_to(ROOT)}:{line_number}:nested")
                in_mermaid = True
                content_lines = 0
            elif in_mermaid and line.strip() == "```":
                if content_lines == 0:
                    failures.append(f"{document.relative_to(ROOT)}:{line_number}:empty")
                in_mermaid = False
            elif in_mermaid and line.strip():
                content_lines += 1
        if in_mermaid:
            failures.append(f"{document.relative_to(ROOT)}:unclosed")
    assert failures == []


def test_implementation_guide_example_modules_compile() -> None:
    guide = (ROOT / "docs" / "IMPLEMENTATION_GUIDE.md").read_text(encoding="utf-8")
    modules = list(EXAMPLE_MODULE.finditer(guide))
    assert {match.group("path") for match in modules} == {
        "app/workshops/example/__init__.py",
        "app/workshops/example/manifest.py",
        "app/workshops/example/handlers.py",
    }
    for match in modules:
        compile(
            match.group("source"),
            f"docs/IMPLEMENTATION_GUIDE.md::{match.group('path')}",
            "exec",
        )


def test_public_documentation_is_english_only() -> None:
    failures: list[dict[str, str]] = []
    checked = public_audit._scan_language_policy(
        failures,
        public_audit._load_json(ROOT / "public_audit_allowlist.json"),
        public_audit._tracked_candidate_paths(),
    )
    assert checked >= 20
    assert failures == []


def test_license_and_public_email_are_consistent() -> None:
    failures: list[dict[str, str]] = []
    result = public_audit._scan_license_policy(
        failures,
        public_audit._tracked_candidate_paths(),
    )
    assert result["license"] == "Apache-2.0"
    assert result["license_sha256"] == public_audit.EXPECTED_APACHE_LICENSE_SHA256
    assert failures == []

    public_email = public_audit.ALLOWED_PUBLIC_EMAIL
    assert public_email in (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    private_email = bytes.fromhex(public_audit.FORBIDDEN_PRIVATE_EMAIL_HEX).decode("ascii")
    for path in public_audit._tracked_candidate_paths():
        assert private_email.casefold() not in path.read_text(
            encoding="utf-8",
            errors="ignore",
        ).casefold()


def test_runtime_dependencies_include_timezone_data() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = metadata["project"]["dependencies"]
    assert any(dependency.startswith("tzdata>=") for dependency in dependencies)
    assert ZoneInfo("Europe/Warsaw").key == "Europe/Warsaw"


def test_git_policy_accepts_release_candidate_without_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _temporary_public_repository(tmp_path)
    result, failures = _scan_temporary_repository(monkeypatch, repository)
    assert result["repository_state"] == "release_candidate"
    assert result["canonical_origin"] is None
    assert failures == []


@pytest.mark.parametrize(
    ("origin", "canonical"),
    [
        (public_audit.CANONICAL_HTTPS_ORIGIN, public_audit.CANONICAL_HTTPS_ORIGIN),
        (public_audit.CANONICAL_SSH_ORIGIN, public_audit.CANONICAL_SSH_ORIGIN),
        (
            public_audit.CANONICAL_HTTPS_ORIGIN.removesuffix(".git"),
            public_audit.CANONICAL_HTTPS_ORIGIN,
        ),
    ],
)
def test_git_policy_accepts_canonical_published_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    canonical: str,
) -> None:
    repository = _temporary_public_repository(tmp_path)
    _git(repository, "remote", "add", "origin", origin)
    result, failures = _scan_temporary_repository(monkeypatch, repository)
    assert result["repository_state"] == "published"
    assert result["canonical_origin"] == canonical
    assert failures == []


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/another-owner/mapi-agent-memory.git",
        "https://github.com/cabo0m/another-repository.git",
        "https://user:token@github.com/cabo0m/mapi-agent-memory.git",
        "https://github.com/cabo0m/mapi-agent-memory.git?token=secret",
        "https://github.com/cabo0m/mapi-agent-memory.git#fragment",
        "ftp://github.com/cabo0m/mapi-agent-memory.git",
    ],
)
def test_git_policy_rejects_noncanonical_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    repository = _temporary_public_repository(tmp_path)
    _git(repository, "remote", "add", "origin", origin)
    result, failures = _scan_temporary_repository(monkeypatch, repository)
    assert result["repository_state"] == "invalid"
    assert {failure["rule"] for failure in failures} == {
        "origin_fetch_url_not_canonical"
    }


def test_git_policy_rejects_second_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _temporary_public_repository(tmp_path)
    _git(repository, "remote", "add", "origin", public_audit.CANONICAL_HTTPS_ORIGIN)
    _git(repository, "remote", "add", "backup", public_audit.CANONICAL_HTTPS_ORIGIN)
    result, failures = _scan_temporary_repository(monkeypatch, repository)
    assert result["repository_state"] == "invalid"
    assert {failure["rule"] for failure in failures} == {"unexpected_remote_names"}


def test_git_policy_rejects_second_push_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _temporary_public_repository(tmp_path)
    _git(repository, "remote", "add", "origin", public_audit.CANONICAL_HTTPS_ORIGIN)
    _git(repository, "config", "--add", "remote.origin.pushurl", public_audit.CANONICAL_HTTPS_ORIGIN)
    _git(repository, "config", "--add", "remote.origin.pushurl", public_audit.CANONICAL_SSH_ORIGIN)
    result, failures = _scan_temporary_repository(monkeypatch, repository)
    assert result["repository_state"] == "invalid"
    assert {failure["rule"] for failure in failures} == {
        "unexpected_origin_push_url_count"
    }


def test_git_policy_rejects_instead_of_rewriting_to_another_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _temporary_public_repository(tmp_path)
    _git(repository, "remote", "add", "origin", public_audit.CANONICAL_HTTPS_ORIGIN)
    _git(
        repository,
        "config",
        "url.https://github.com/another-owner/.insteadOf",
        "https://github.com/cabo0m/",
    )
    result, failures = _scan_temporary_repository(monkeypatch, repository)
    assert result["repository_state"] == "invalid"
    assert {failure["rule"] for failure in failures} == {"origin_fetch_url_rewritten"}


@pytest.mark.parametrize(
    "origin",
    [
        "https://"
        + bytes.fromhex(public_audit.FORBIDDEN_PRIVATE_EMAIL_HEX).decode("ascii")
        + "@github.com/cabo0m/mapi-agent-memory.git",
        bytes.fromhex(public_audit.FORBIDDEN_TEXT_HEX[2]).decode("utf-8"),
    ],
)
def test_git_policy_rejects_private_origin_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    repository = _temporary_public_repository(tmp_path)
    _git(repository, "remote", "add", "origin", origin)
    result, failures = _scan_temporary_repository(monkeypatch, repository)
    assert result["repository_state"] == "invalid"
    assert {failure["rule"] for failure in failures} == {
        "origin_fetch_url_not_canonical"
    }


def test_pull_request_audit_still_scans_reachable_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _temporary_public_repository(tmp_path)
    private_email = bytes.fromhex(public_audit.FORBIDDEN_PRIVATE_EMAIL_HEX).decode("ascii")
    repository.joinpath("temporary.txt").write_text(private_email, encoding="utf-8")
    _git(repository, "add", "temporary.txt")
    _git(repository, "commit", "-m", "Add temporary historical value")
    _git(repository, "rm", "temporary.txt")
    _git(repository, "commit", "-m", "Remove temporary historical value")
    synthetic_commit = _git(repository, "rev-parse", "HEAD")

    monkeypatch.setattr(public_audit, "ROOT", repository)
    failures: list[dict[str, str]] = []
    result = public_audit._scan_git_metadata(
        failures,
        synthetic_commit=synthetic_commit,
    )

    assert result["reachable_blobs"] >= 2
    assert "private_value_in_reachable_blob" in {
        failure["rule"] for failure in failures
    }


def test_clean_release_git_metadata_passes_public_policy() -> None:
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        pytest.skip("GitHub tests pull requests through a synthetic merge commit")
    status = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain=v1"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    if status.strip():
        pytest.skip("Git metadata invariant is evaluated after the release tree is committed")
    failures: list[dict[str, str]] = []
    result = public_audit._scan_git_metadata(failures)
    assert result["commit_count"] >= 1
    if result["remotes"]:
        assert result["repository_state"] == "published"
        assert result["canonical_origin"] == public_audit.CANONICAL_HTTPS_ORIGIN
    else:
        assert result["repository_state"] == "release_candidate"
        assert result["canonical_origin"] is None
    assert failures == []
