from __future__ import annotations

import shutil


def shell_name() -> str:
    if shutil.which("bash"):
        return "bash"
    if shutil.which("sh"):
        return "sh"
    return "unavailable"


def shell_command(script: str) -> list[str]:
    executable = shutil.which("bash") or shutil.which("sh")
    if not executable:
        raise RuntimeError("posix_shell_unavailable")
    return [executable, "-lc", str(script)]
