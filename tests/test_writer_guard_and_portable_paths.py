from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.runtime import freshness
from app.runtime.writer_guard import (
    configure_writer_guard,
    mutation_writer_guard,
    release_writer_guard,
    writer_guard_status,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
WRITER_PROBE = REPO_ROOT / "tests" / "helpers" / "writer_guard_probe.py"
CONFIG_PROBE = REPO_ROOT / "tests" / "helpers" / "memory_config_probe.py"


def _run_probe(*args: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WRITER_PROBE), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def test_portable_memory_config_respects_explicit_environment(tmp_path: Path) -> None:
    root = (tmp_path / "vps-app").resolve()
    data = (tmp_path / "vps-data").resolve()
    db = (data / "memory.db").resolve()
    env = dict(os.environ)
    env.pop("MAPI_PYTEST_SESSION_ROOT", None)
    env.pop("MAPI_PYTEST_SESSION_DB_PATH", None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(
        {
            "MAPI_ROOT": str(root),
            "MAPI_DATA_DIR": str(data),
            "MAPI_DB_PATH": str(db),
        }
    )
    completed = subprocess.run(
        [sys.executable, str(CONFIG_PROBE)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert Path(payload["root"]) == root
    assert Path(payload["data_dir"]) == data
    assert Path(payload["db_path"]) == db
    assert root in {Path(item) for item in payload["allowed_roots"]}


def test_writer_guard_blocks_second_process_and_reclaims_dead_owner(tmp_path: Path) -> None:
    db = tmp_path / "candidate.db"
    holder = subprocess.Popen(
        [
            sys.executable,
            str(WRITER_PROBE),
            "--db-path",
            str(db),
            "--mode",
            "hold",
            "--instance-key",
            "writer-a",
            "--hold-seconds",
            "30",
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        assert holder.stdout is not None
        first_line = holder.stdout.readline().strip()
        first = json.loads(first_line)
        assert first["status"] == "ready"
        assert first["lease_held"] is True
        assert first["mutations_allowed"] is True

        denied = _run_probe(
            "--db-path",
            str(db),
            "--mode",
            "try",
            "--instance-key",
            "writer-b",
        )
        assert denied.returncode == 2
        denial = json.loads(denied.stdout.splitlines()[-1])
        assert denial["status"] == "denied"
        assert "single_writer_lease_held:writer-a" in denial["error"]
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    reclaimed = _run_probe(
        "--db-path",
        str(db),
        "--mode",
        "try",
        "--instance-key",
        "writer-c",
    )
    assert reclaimed.returncode == 0, reclaimed.stdout + reclaimed.stderr
    payload = json.loads(reclaimed.stdout.splitlines()[-1])
    assert payload["status"] == "ready"
    assert payload["instance_key"] == "writer-c"
    assert payload["mutations_allowed"] is True


def test_read_only_mode_blocks_domain_mutation_but_allows_admin_recovery(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MAPI_WRITER_GUARD_ENABLED", "1")
    monkeypatch.setenv("MAPI_WRITER_MODE", "read_only")
    monkeypatch.setenv("MAPI_WRITER_INSTANCE_KEY", "desktop-frozen")
    monkeypatch.setenv("MAPI_RUNTIME_ENFORCE_FRESHNESS", "0")
    try:
        status = configure_writer_guard(db_path=tmp_path / "frozen.db")
        assert status["mode"] == "read_only"
        assert status["lease_held"] is False
        assert status["mutations_allowed"] is False

        direct = mutation_writer_guard(required=True)
        assert direct["allowed"] is False
        assert direct["reason_codes"] == ["writer_read_only_mode"]

        domain = freshness.mutation_freshness_guard(
            area="memory", action="save", risk_class="R1", payload={}
        )
        admin_shell = freshness.mutation_freshness_guard(
            area="admin", action="run_shell", risk_class="R3", payload={}
        )
        admin = freshness.mutation_freshness_guard(
            area="admin", action="run_powershell", risk_class="R3", payload={}
        )
        write_sql = freshness.mutation_freshness_guard(
            area="admin",
            action="query_sql",
            risk_class="R3",
            payload={"allow_write": True},
        )
        assert domain["allowed"] is False
        assert domain["reason_codes"] == ["writer_read_only_mode"]
        assert admin_shell == {"allowed": True, "required": False, "reason_codes": []}
        assert admin == {"allowed": True, "required": False, "reason_codes": []}
        assert write_sql["allowed"] is False
        assert write_sql["reason_codes"] == ["writer_read_only_mode"]
    finally:
        release_writer_guard()


def test_release_resets_process_state_for_following_tests(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAPI_WRITER_GUARD_ENABLED", "1")
    monkeypatch.setenv("MAPI_WRITER_MODE", "read_only")
    configure_writer_guard(db_path=tmp_path / "reset.db")
    assert writer_guard_status()["enabled"] is True
    assert mutation_writer_guard(required=True)["allowed"] is False

    release_writer_guard()

    status = writer_guard_status()
    assert status["configured"] is False
    assert status["enabled"] is False
    assert status["mode"] == "active"
    assert status["lease_held"] is False
    assert mutation_writer_guard(required=True)["allowed"] is True


def test_disabled_writer_guard_preserves_legacy_local_behavior(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAPI_WRITER_GUARD_ENABLED", "0")
    monkeypatch.setenv("MAPI_WRITER_MODE", "active")
    try:
        status = configure_writer_guard(db_path=tmp_path / "legacy.db")
        assert status["enabled"] is False
        assert status["lease_held"] is False
        assert status["mutations_allowed"] is True
        assert mutation_writer_guard(required=True)["allowed"] is True
        assert writer_guard_status()["enabled"] is False
    finally:
        release_writer_guard()
