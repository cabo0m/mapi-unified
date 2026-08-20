from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_windows_install_scripts_parse() -> None:
    if not (ROOT / "install-windows.ps1").exists():
        raise AssertionError("Windows installer missing")
    command = (
        "$null=[scriptblock]::Create((Get-Content -Raw 'install-windows.ps1'));"
        "$null=[scriptblock]::Create((Get-Content -Raw 'uninstall-windows.ps1'));"
        "$null=[scriptblock]::Create((Get-Content -Raw 'scripts/windows_install_smoke.ps1'))"
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
