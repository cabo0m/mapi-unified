from __future__ import annotations

import shutil


def shell_name() -> str:
    return "powershell"


def shell_command(script: str) -> list[str]:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not executable:
        raise RuntimeError("powershell_unavailable")
    return [
        executable,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        str(script),
    ]
