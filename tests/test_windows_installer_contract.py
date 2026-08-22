from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_windows_install_scripts_parse() -> None:
    if os.name != "nt":
        pytest.skip("Windows-only PowerShell parser contract")
    if not (ROOT / "install-windows.ps1").exists():
        raise AssertionError("Windows installer missing")
    command = (
        "$null=[scriptblock]::Create((Get-Content -Raw 'install-windows.ps1'));"
        "$null=[scriptblock]::Create((Get-Content -Raw 'uninstall-windows.ps1'));"
        "$null=[scriptblock]::Create((Get-Content -Raw 'scripts/windows_install_smoke.ps1'));"
        "$null=[scriptblock]::Create((Get-Content -Raw 'scripts/configure_windows_tunnel_autostart.ps1'));"
        "$null=[scriptblock]::Create((Get-Content -Raw 'scripts/run_windows_tunnel_autostart.ps1'))"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_windows_installer_targets_unified_package_and_instance_root() -> None:
    text = (ROOT / "install-windows.ps1").read_text(encoding="utf-8")
    assert "mapi_agent_memory-*.whl" in text
    assert "mapi-public" not in text.casefold()
    assert ".mapi-agent-memory" in text
    assert "mapi_public.maintenance" not in text
    assert "-m mapi.maintenance" in text
    assert "mapi-init.exe" in text
    assert "mapi-doctor.exe" in text
    assert "mapi-migrate.exe" in text


def test_windows_tunnel_autostart_is_one_time_and_secret_safe() -> None:
    configure = (
        ROOT / "scripts" / "configure_windows_tunnel_autostart.ps1"
    ).read_text(encoding="utf-8")
    runner = (
        ROOT / "scripts" / "run_windows_tunnel_autostart.ps1"
    ).read_text(encoding="utf-8")
    assert "ConvertFrom-SecureString" in configure
    assert "DPAPI" in configure
    assert "/SC ONLOGON" in configure
    assert "CONTROL_PLANE_API_KEY" not in configure
    assert "control-plane-api-key.dpapi" in configure
    assert "CONTROL_PLANE_API_KEY" in runner
    assert 'HEALTH_LISTEN_ADDR = "127.0.0.1:0"' in runner
    assert 'Arguments @("start")' in runner
    assert 'Arguments @("run")' in runner
    assert "while ($true)" in runner


def test_windows_tunnel_docs_promise_no_repeat_setup() -> None:
    installation = (ROOT / "docs" / "INSTALLATION.md").read_text(encoding="utf-8")
    integration = (ROOT / "docs" / "MCP_INTEGRATION.md").read_text(encoding="utf-8")
    assert "configure_windows_tunnel_autostart.ps1" in installation
    assert "starts automatically after Windows sign-in" in installation
    assert "does not need to be recreated" in installation
    assert "connect ChatGPT once through Secure MCP Tunnel" in integration
    assert "does not repeat" in integration


def test_windows_release_bundle_carries_tunnel_autostart() -> None:
    builder = (ROOT / "scripts" / "build_windows_bundle.py").read_text(encoding="utf-8")
    installer = (ROOT / "install-windows.ps1").read_text(encoding="utf-8")
    uninstaller = (ROOT / "uninstall-windows.ps1").read_text(encoding="utf-8")
    for name in (
        "configure_windows_tunnel_autostart.ps1",
        "run_windows_tunnel_autostart.ps1",
    ):
        assert name in builder
        assert name in installer
    assert '$TunnelTaskName = "MAPI Aurora"' in uninstaller
    assert "@($MaintenanceTaskName, $TunnelTaskName)" in uninstaller
