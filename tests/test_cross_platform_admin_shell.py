from __future__ import annotations

import subprocess
from pathlib import Path

from app.admin import operator_tools
from mapi_platform.linux import shell as linux_shell
from mapi_platform.windows import shell as windows_shell


def test_windows_shell_command_shape(monkeypatch) -> None:
    monkeypatch.setattr(windows_shell.shutil, "which", lambda name: "powershell.exe" if name == "powershell.exe" else None)
    assert windows_shell.shell_name() == "powershell"
    command = windows_shell.shell_command("Write-Output test")
    assert command == [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "Write-Output test"
    ]


def test_linux_shell_prefers_bash_and_falls_back_to_sh(monkeypatch) -> None:
    monkeypatch.setattr(linux_shell.shutil, "which", lambda name: "/bin/bash" if name == "bash" else None)
    assert linux_shell.shell_name() == "bash"
    assert linux_shell.shell_command("printf test") == ["/bin/bash", "-lc", "printf test"]
    monkeypatch.setattr(linux_shell.shutil, "which", lambda name: "/bin/sh" if name == "sh" else None)
    assert linux_shell.shell_name() == "sh"
    assert linux_shell.shell_command("printf test") == ["/bin/sh", "-lc", "printf test"]


def test_run_shell_command_delegates_to_platform_command(monkeypatch, tmp_path: Path) -> None:
    captured: list[object] = []

    monkeypatch.setattr(operator_tools, "shell_command", lambda script: ["native-shell", "-c", script])
    monkeypatch.setattr(operator_tools, "shell_name", lambda: "native")
    monkeypatch.setattr(
        operator_tools,
        "run_subprocess_command",
        lambda command, *, cwd, timeout_seconds: captured.append((command, cwd, timeout_seconds)) or {
            "status": "completed", "returncode": 0, "cwd": str(cwd), "command": command, "stdout": "ok", "stderr": ""
        },
    )
    result = operator_tools.run_shell_command(root=tmp_path, script="echo ok", timeout_seconds=17)
    assert result["status"] == "completed"
    assert result["shell"] == "native"
    assert captured == [(["native-shell", "-c", "echo ok"], tmp_path.resolve(), 17)]


def test_powershell_compatibility_is_windows_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(operator_tools, "current_platform", lambda: "linux")
    monkeypatch.setattr(operator_tools, "shell_name", lambda: "bash")
    result = operator_tools.run_powershell_command(root=tmp_path, script="echo nope")
    assert result == {"status": "error", "error": "powershell_windows_only", "shell": "bash"}
