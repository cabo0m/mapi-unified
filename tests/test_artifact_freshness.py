from __future__ import annotations

from pathlib import Path

from app.runtime import freshness
from app.runtime.context import configure_runtime_context, get_runtime_context


def _repo_unavailable(root: Path) -> dict[str, object]:
    return {
        "root": str(root),
        "head": None,
        "dirty": None,
        "git_available": False,
        "tracked_paths": [],
        "untracked_paths": [],
        "allowlisted_untracked_paths": [],
        "non_allowlisted_untracked_paths": [],
        "paths_truncated": False,
        "worktrees": [],
    }


def test_artifact_provenance_allows_enforced_freshness_without_git(monkeypatch, tmp_path: Path) -> None:
    previous = get_runtime_context()
    root = tmp_path / "instance"
    data = root / "data"
    db = data / "mapi.db"
    artifact = {
        "available": True,
        "distribution": "mapi-agent-memory",
        "version": "1.2.3",
        "fingerprint": "artifact-a",
        "record_sha256": "record-a",
        "reason": None,
    }
    try:
        configure_runtime_context(root=root, data_dir=data, db_path=db)
        monkeypatch.setenv("MAPI_RUNTIME_ENFORCE_FRESHNESS", "1")
        monkeypatch.delenv("MAPI_REPOSITORY_ROOT", raising=False)
        monkeypatch.delenv("MAPI_EXPECTED_COMMIT", raising=False)
        monkeypatch.setattr(freshness, "repository_state", lambda root=None: _repo_unavailable(tmp_path))
        monkeypatch.setattr(freshness, "artifact_state", lambda: dict(artifact))
        monkeypatch.setattr(freshness, "schema_tail", lambda db_path=None: "0040_common_command_runs")
        freshness.reset_runtime_metadata_for_tests()
        readiness = freshness.get_runtime_readiness(include_debug=True)
        assert readiness["status"] == "ready"
        assert readiness["mutations_allowed"] is True
        assert readiness["runtime"]["provenance_mode"] == "artifact"
        assert readiness["runtime"]["artifact_fingerprint"] == "artifact-a"
        assert readiness["reason_codes"] == []
        assert readiness["launcher"] is None
    finally:
        freshness.reset_runtime_metadata_for_tests()
        configure_runtime_context(
            root=previous.root, data_dir=previous.data_dir, db_path=previous.db_path
        )


def test_artifact_provenance_detects_package_change(monkeypatch, tmp_path: Path) -> None:
    previous = get_runtime_context()
    root = tmp_path / "instance"
    data = root / "data"
    db = data / "mapi.db"
    state = {
        "available": True,
        "distribution": "mapi-agent-memory",
        "version": "1.2.3",
        "fingerprint": "artifact-a",
        "record_sha256": "record-a",
        "reason": None,
    }
    try:
        configure_runtime_context(root=root, data_dir=data, db_path=db)
        monkeypatch.setenv("MAPI_RUNTIME_ENFORCE_FRESNESS", "1")
        monkeypatch.setenv("MAPI_RUNTIME_ENFORCE_FRESHNESS", "1")
        monkeypatch.delenv("MAPI_REPOSITORY_ROOT", raising=False)
        monkeypatch.setattr(freshness, "repository_state", lambda root=None: _repo_unavailable(tmp_path))
        monkeypatch.setattr(freshness, "artifact_state", lambda: dict(state))
        monkeypatch.setattr(freshness, "schema_tail", lambda db_path=None: "0040_common_command_runs")
        freshness.reset_runtime_metadata_for_tests()
        assert freshness.get_runtime_readiness()["status"] == "ready"
        state["fingerprint"] = "artifact-b"
        state["record_sha256"] = "record-b"
        readiness = freshness.get_runtime_readiness()
        assert readiness["status"] == "stale"
        assert readiness["mutations_allowed"] is False
        assert "artifact_fingerprint_mismatch" in readiness["reason_codes"]
    finally:
        freshness.reset_runtime_metadata_for_tests()
        configure_runtime_context(
            root=previous.root, data_dir=previous.data_dir, db_path=previous.db_path
        )
