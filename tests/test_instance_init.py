from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from app.runtime.owner_credentials import hash_owner_password
from mapi.env import apply_runtime_environment, load_environment_file, parse_environment_file
from mapi.initialize import InitOptions, initialize_instance, validate_init_options


def _options(root: Path, **overrides) -> InitOptions:
    values = dict(
        root=root,
        mode="local",
        owner_key="alpha-owner",
        agent_subject_key="alpha",
        agent_display_name="Alpha Agent",
        agent_project_key="alpha-self",
        port=8015,
        profile="agent",
    )
    values.update(overrides)
    return InitOptions(**values)


def _memory_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id,summary_short,memory_type,project_key,source_event_ref,tags FROM memories ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_environment_file_loader_preserves_explicit_process_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('MAPI_RUNTIME_PORT=9000\nMAPI_AGENT_DISPLAY_NAME="Env Agent"\n', encoding="utf-8")
    monkeypatch.setenv("MAPI_RUNTIME_PORT", "7777")
    monkeypatch.delenv("MAPI_AGENT_DISPLAY_NAME", raising=False)
    try:
        result = load_environment_file(env_file)
        assert result["status"] == "loaded"
        assert os.environ["MAPI_RUNTIME_PORT"] == "7777"
        assert os.environ["MAPI_AGENT_DISPLAY_NAME"] == "Env Agent"
        assert "MAPI_RUNTIME_PORT" not in result["loaded_keys"]
    finally:
        os.environ.pop("MAPI_AGENT_DISPLAY_NAME", None)


def test_fresh_local_init_creates_private_runtime_state_and_self_model(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    result = initialize_instance(_options(root))
    assert result["status"] == "ready_to_start"
    assert result["migration_tail"] == "0042_legacy_aurora_import"
    assert result["doctor_status"] in {"READY", "ATTENTION"}
    assert result["safety"] == {
        "existing_state_overwritten": False,
        "loopback_runtime": True,
        "admin_tools_enabled": False,
        "demo_seeded": False,
        "privileged_system_changes_performed": False,
        "reverse_proxy_auth_required": False,
    }
    for relative in (".env", "data/mapi.db", "backups", "logs", "generated/mapi-init-manifest.json"):
        assert (root / relative).exists()
    env = parse_environment_file(root / ".env")
    assert env["MAPI_ROOT"] == str(root.resolve())
    assert env["MAPI_RUNTIME_HOST"] == "127.0.0.1"
    assert env["MAPI_REMOTE_AUTH_ENABLED"] == "false"
    assert env["MAPI_AGENT_PROJECT_KEY"] == "alpha-self"
    rows = _memory_rows(root / "data" / "mapi.db")
    assert len(rows) == 2
    assert {row["project_key"] for row in rows} == {"alpha-self"}
    assert {row["memory_type"] for row in rows} == {"identity", "guardrail"}
    assert all(str(row["source_event_ref"]).startswith("mapi-init:alpha:") for row in rows)
    assert not any(row["project_key"] in {"demo-project", "sample-research"} for row in rows)


def test_resume_is_idempotent_and_does_not_duplicate_self_evidence(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    first = initialize_instance(_options(root))
    second = initialize_instance(_options(root, resume=True))
    assert second["status"] == "ready_to_start"
    assert second["migrations_applied_now"] == []
    assert second["migration_tail"] == "0042_legacy_aurora_import"
    assert second["self_memory_ids"] == first["self_memory_ids"]
    assert len(_memory_rows(root / "data" / "mapi.db")) == 2


def test_init_refuses_existing_instance_without_resume(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    initialize_instance(_options(root))
    with pytest.raises(RuntimeError, match="existing_env_detected_use_resume"):
        initialize_instance(_options(root))


def test_resume_refuses_identity_or_runtime_reconfiguration(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    initialize_instance(_options(root))
    with pytest.raises(RuntimeError, match="resume_config_mismatch"):
        initialize_instance(_options(root, agent_display_name="Changed Agent", resume=True))
    assert len(_memory_rows(root / "data" / "mapi.db")) == 2


def test_generated_env_is_used_by_runtime_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "instance"
    initialize_instance(_options(root, port=9123))
    keys = (
        "MAPI_ROOT",
        "MAPI_DATA_DIR",
        "MAPI_DB_PATH",
        "MAPI_RUNTIME_PORT",
        "MAPI_AGENT_SUBJECT_KEY",
        "MAPI_AGENT_DISPLAY_NAME",
        "MAPI_AGENT_PROJECT_KEY",
    )
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        runtime = apply_runtime_environment(root / ".env")
        assert runtime["root"] == str(root.resolve())
        assert runtime["db_path"] == str((root / "data" / "mapi.db").resolve())
        assert os.environ["MAPI_RUNTIME_PORT"] == "9123"
        assert os.environ["MAPI_AGENT_DISPLAY_NAME"] == "Alpha Agent"
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_vps_proxy_init_keeps_runtime_loopback_and_generates_operator_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    result = initialize_instance(
        _options(
            root,
            mode="vps-proxy",
            public_url="https://mapi.example.test",
            service_user="mapi",
            recovery_command_json='["systemctl","restart","mapi"]',
        )
    )
    assert result["status"] == "ready_to_start"
    env = parse_environment_file(root / ".env")
    assert env["MAPI_RUNTIME_HOST"] == "127.0.0.1"
    assert env["MAPI_REMOTE_AUTH_ENABLED"] == "false"
    assert env["MAPI_REMOTE_BASE_URL"] == "https://mapi.example.test"
    unit = (root / "generated" / "mapi.service").read_text(encoding="utf-8")
    proxy = (root / "generated" / "reverse-proxy-security-template.txt").read_text(encoding="utf-8")
    assert "User=mapi" in unit
    assert f"EnvironmentFile={root.resolve() / '.env'}" in unit
    assert "Restart=on-failure" in unit
    assert "0.0.0.0" not in unit
    assert "MUST authenticate every request" in proxy
    assert "SECURITY TEMPLATE ONLY" in proxy
    assert result["safety"]["privileged_system_changes_performed"] is False


def test_remote_auth_init_supports_dynamic_registration_without_static_redirect(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    password_hash = hash_owner_password("a sufficiently long owner password")
    validated = validate_init_options(
        _options(
            root,
            mode="vps-remote-auth",
            public_url="https://mapi.example.test",
            profile="admin",
            owner_password_hash=password_hash,
        )
    )
    assert validated["oauth_redirect_uris"] == ()
    with pytest.raises(ValueError, match="public_url_must_be_https_origin"):
        validate_init_options(
            _options(
                root,
                mode="vps-remote-auth",
                public_url="http://mapi.example.test",
                oauth_redirect_uris=("https://chat.example/callback",),
                owner_password_hash=password_hash,
                profile="admin",
            )
        )


def test_remote_auth_init_generates_dynamic_registration_instance_without_static_callback(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    password_hash = hash_owner_password("a sufficiently long owner password")
    result = initialize_instance(
        _options(
            root,
            mode="vps-remote-auth",
            public_url="https://mapi.example.test",
            oauth_redirect_uris=(),
            owner_login="michal",
            owner_password_hash=password_hash,
            profile="admin",
        )
    )
    assert result["status"] == "ready_to_start"
    env = parse_environment_file(root / ".env")
    assert env["MAPI_REMOTE_AUTH_ENABLED"] == "true"
    assert env["MAPI_REMOTE_OAUTH_REDIRECT_URIS"] == ""
    assert env["MAPI_REMOTE_OWNER_LOGIN"] == "michal"
    assert env["MAPI_REMOTE_OWNER_PASSWORD_HASH"] == password_hash
    rows = _memory_rows(root / "data" / "mapi.db")
    assert len(rows) == 1
    assert rows[0]["memory_type"] == "guardrail"
    assert rows[0]["source_event_ref"].endswith(":namespace-guardrail")
    conn = sqlite3.connect(root / "data" / "mapi.db")
    conn.row_factory = sqlite3.Row
    try:
        onboarding = conn.execute("SELECT status, current_step, answers_json FROM polaris_onboarding WHERE id=1").fetchone()
        assert onboarding["status"] == "not_started"
        assert onboarding["current_step"] == "agent_name"
        assert onboarding["answers_json"] == "{}"
    finally:
        conn.close()


def test_remote_auth_requires_owner_password_hash(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="owner_password_hash_required"):
        validate_init_options(
            _options(
                tmp_path / "instance",
                mode="vps-remote-auth",
                public_url="https://mapi.example.test",
                oauth_redirect_uris=("https://chat.example/callback",),
                profile="admin",
            )
        )


def test_remote_auth_init_writes_owner_login_and_hash_but_manifest_does_not_copy_hash(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    password_hash = hash_owner_password("a sufficiently long owner password")
    result = initialize_instance(
        _options(
            root,
            mode="vps-remote-auth",
            public_url="https://mapi.example.test",
            oauth_redirect_uris=("https://chat.example/callback",),
            owner_login="michal",
            owner_password_hash=password_hash,
            profile="admin",
        )
    )
    assert result["status"] == "ready_to_start"
    env_text = (root / ".env").read_text(encoding="utf-8")
    manifest_text = (root / "generated" / "mapi-init-manifest.json").read_text(encoding="utf-8")
    proxy_text = (root / "generated" / "reverse-proxy-security-template.txt").read_text(encoding="utf-8")
    assert "MAPI_REMOTE_AUTH_ENABLED=true" in env_text
    assert "MCP_SURFACE_PROFILE=admin" in env_text
    assert "MAPI_ADMIN_TOOLS_ENABLED=true" in env_text
    assert "MAPI_REMOTE_OWNER_LOGIN=michal" in env_text
    assert password_hash in env_text
    assert password_hash not in manifest_text
    assert "MAPI_REMOTE_IDENTITY_HEADER" not in env_text
    assert "MAPI_REMOTE_IDENTITY_VALUE" not in env_text
    assert "do not add Basic Auth" in proxy_text
    assert "identity-header injection" in proxy_text
    assert "127.0.0.1" in env_text
    assert "remote_auth_enabled" in manifest_text


def test_unauthenticated_vps_proxy_rejects_admin_surface_profile(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unauthenticated_vps_admin_profile_not_allowed"):
        validate_init_options(
            _options(tmp_path / "instance", mode="vps-proxy", public_url="https://mapi.example.test", profile="admin")
        )


def test_remote_auth_requires_single_owner_admin_profile(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="single_owner_remote_auth_requires_admin_profile"):
        validate_init_options(
            _options(
                tmp_path / "instance",
                mode="vps-remote-auth",
                public_url="https://mapi.example.test",
                oauth_redirect_uris=("https://chat.example/callback",),
                owner_password_hash=hash_owner_password("a sufficiently long owner password"),
                profile="agent",
            )
        )


def test_init_always_returns_exact_mcp_connection_address(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    local = initialize_instance(_options(root))
    assert local["connection"]["recommended_mcp_url"] == "http://127.0.0.1:8015/mcp/"
    assert local["connection"]["status"] == "configured"


def test_vps_service_install_marks_listener_ready_and_reports_public_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mapi.initialize as init_module

    root = tmp_path / "instance"
    monkeypatch.setattr(
        init_module,
        "install_systemd_service",
        lambda *args, **kwargs: {"status": "active", "active": True, "service_name": "mapi.service"},
    )
    monkeypatch.setattr(
        init_module,
        "install_systemd_maintenance_timer",
        lambda *args, **kwargs: {
            "status": "active",
            "active": True,
            "enabled": True,
            "timer_name": "mapi-maintenance.timer",
        },
    )
    monkeypatch.setattr(
        init_module,
        "wait_for_listener",
        lambda host, port: {"status": "ready", "host": host, "port": port, "attempts": 1},
    )
    monkeypatch.setattr(
        init_module,
        "probe_http_endpoint",
        lambda url: {"status": "reachable", "url": url, "http_status": 401},
    )
    result = initialize_instance(
        _options(
            root,
            mode="vps-proxy",
            public_url="https://mapi.example.test",
            install_service=True,
        )
    )
    assert result["status"] == "ready"
    assert result["connection"]["recommended_mcp_url"] == "https://mapi.example.test/mcp/"
    assert result["connection"]["loopback_mcp_url"] == "http://127.0.0.1:8015/mcp/"
    assert result["connection"]["status"] == "public_endpoint_reachable"
    assert result["system_service"]["active"] is True
    assert result["safety"]["privileged_system_changes_performed"] is True


def test_requested_service_install_failure_blocks_init_but_preserves_connection_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mapi.initialize as init_module

    root = tmp_path / "instance"
    monkeypatch.setattr(init_module, "install_systemd_service", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sudo_failed")))
    result = initialize_instance(
        _options(
            root,
            mode="vps-proxy",
            public_url="https://mapi.example.test",
            install_service=True,
        )
    )
    assert result["status"] == "blocked"
    assert result["system_service"]["status"] == "failed"
    assert result["connection"]["recommended_mcp_url"] == "https://mapi.example.test/mcp/"


def test_custom_service_name_flows_through_init_and_generated_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.runtime.doctor as doctor_module
    import mapi.initialize as init_module

    captured: dict[str, object] = {}

    def install(unit_path, *, service_name, allow_sudo_prompt):
        captured["unit_path"] = str(unit_path)
        captured["service_name"] = service_name
        captured["allow_sudo_prompt"] = allow_sudo_prompt
        return {"status": "active", "active": True, "service_name": service_name}

    monkeypatch.setattr(init_module, "install_systemd_service", install)
    monkeypatch.setattr(
        init_module,
        "install_systemd_maintenance_timer",
        lambda *args, **kwargs: {
            "status": "active",
            "active": True,
            "enabled": True,
            "timer_name": "polaris-maintenance.timer",
        },
    )
    monkeypatch.setattr(init_module, "wait_for_listener", lambda host, port: {"status": "ready", "host": host, "port": port})
    monkeypatch.setattr(doctor_module, "collect_doctor_report", lambda **kwargs: {"status": "READY", "findings": []})

    root = tmp_path / "instance"
    result = initialize_instance(
        _options(
            root,
            mode="vps-proxy",
            public_url="https://mapi.example.test",
            service_name="polaris",
            install_service=True,
            verify_endpoint=False,
        )
    )

    env = parse_environment_file(root / ".env")
    assert env["MAPI_SYSTEMD_SERVICE_NAME"] == "polaris.service"
    assert captured["service_name"] == "polaris.service"
    assert str(captured["unit_path"]).endswith("polaris.service")
    assert result["system_service"]["service_name"] == "polaris.service"
    assert result["artifacts"]["systemd_unit"].endswith("polaris.service")
    assert result["artifacts"]["maintenance_systemd_service"].endswith("polaris-maintenance.service")
    assert result["artifacts"]["maintenance_systemd_timer"].endswith("polaris-maintenance.timer")
    assert result["maintenance_service"]["active"] is True
    maintenance_service_text = Path(result["artifacts"]["maintenance_systemd_service"]).read_text(encoding="utf-8")
    maintenance_timer_text = Path(result["artifacts"]["maintenance_systemd_timer"]).read_text(encoding="utf-8")
    assert "-m mapi.maintenance" in maintenance_service_text
    assert "Persistent=true" in maintenance_timer_text
    assert result["initial_backup"]["status"] == "created"


def test_final_doctor_runs_after_service_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.runtime.doctor as doctor_module
    import mapi.initialize as init_module

    events: list[str] = []

    def install(*args, **kwargs):
        events.append("service_started")
        return {"status": "active", "active": True, "service_name": "mapi.service"}

    def doctor(**kwargs):
        assert events == ["service_started"]
        events.append("doctor")
        return {"status": "READY", "findings": []}

    monkeypatch.setattr(init_module, "install_systemd_service", install)
    monkeypatch.setattr(
        init_module,
        "install_systemd_maintenance_timer",
        lambda *args, **kwargs: {
            "status": "active",
            "active": True,
            "enabled": True,
            "timer_name": "mapi-maintenance.timer",
        },
    )
    monkeypatch.setattr(init_module, "wait_for_listener", lambda host, port: {"status": "ready", "host": host, "port": port})
    monkeypatch.setattr(doctor_module, "collect_doctor_report", doctor)

    result = initialize_instance(
        _options(
            tmp_path / "instance",
            mode="vps-proxy",
            public_url="https://mapi.example.test",
            install_service=True,
            verify_endpoint=False,
        )
    )

    assert events == ["service_started", "doctor"]
    assert result["doctor_status"] == "READY"


def test_resume_reuses_verified_initial_backup(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    first = initialize_instance(_options(root))
    second = initialize_instance(_options(root, resume=True))
    assert first["initial_backup"]["status"] == "created"
    assert second["initial_backup"]["status"] == "existing_verified"
    assert second["initial_backup"]["path"] == first["initial_backup"]["path"]


def test_init_persists_detected_repository_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mapi.initialize as init_module

    repository_root = tmp_path / "source-checkout"
    repository_root.mkdir()
    monkeypatch.setattr(init_module, "detect_repository_root", lambda: repository_root)

    root = tmp_path / "instance"
    initialize_instance(_options(root))
    env = parse_environment_file(root / ".env")
    assert env["MAPI_REPOSITORY_ROOT"] == str(repository_root.resolve())
