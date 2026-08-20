from __future__ import annotations

from .selector import current_platform


def shell_name() -> str:
    platform = current_platform()
    if platform == "windows":
        from .windows.shell import shell_name as implementation
    elif platform == "linux":
        from .linux.shell import shell_name as implementation
    else:
        return "unsupported"
    return implementation()


def shell_command(script: str) -> list[str]:
    platform = current_platform()
    if platform == "windows":
        from .windows.shell import shell_command as implementation
    elif platform == "linux":
        from .linux.shell import shell_command as implementation
    else:
        raise RuntimeError(f"unsupported_platform:{platform}")
    return implementation(script)


__all__ = ["shell_name", "shell_command"]
