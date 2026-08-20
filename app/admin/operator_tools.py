from __future__ import annotations

"""Operator command helpers for the admin MAPI surface.

These helpers are intentionally not MCP tools themselves. server_core.py keeps
the public tool names and delegates here, preserving the connector contract while
shrinking the entrypoint.
"""

import subprocess
from pathlib import Path
from typing import Any

from mapi_platform.selector import current_platform
from mapi_platform.shell import shell_command, shell_name


def resolve_command_workdir(*, root: str | Path, workdir: str | None = None) -> Path:
    if workdir is None or not str(workdir).strip():
        return Path(root).resolve()
    candidate = Path(str(workdir)).expanduser()
    if not candidate.is_absolute():
        candidate = Path(root) / candidate
    return candidate.resolve()


def run_subprocess_command(command: list[str], *, cwd: Path, timeout_seconds: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            "status": "completed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "cwd": str(cwd),
            "command": command,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "returncode": None,
            "cwd": str(cwd),
            "command": command,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "error": f"Command timed out after {timeout_seconds}s",
        }


def run_shell_command(*, root: str | Path, script: str, workdir: str | None = None, timeout_seconds: int = 60) -> dict[str, Any]:
    if not isinstance(script, str) or not script.strip():
        return {"status": "error", "error": "script must be a non-empty string"}
    timeout_seconds = max(1, min(int(timeout_seconds or 60), 600))
    try:
        command = shell_command(script)
    except RuntimeError as exc:
        return {"status": "error", "error": str(exc), "shell": shell_name()}
    result = run_subprocess_command(
        command,
        cwd=resolve_command_workdir(root=root, workdir=workdir),
        timeout_seconds=timeout_seconds,
    )
    result["shell"] = shell_name()
    return result


def run_powershell_command(*, root: str | Path, script: str, workdir: str | None = None, timeout_seconds: int = 60) -> dict[str, Any]:
    if current_platform() != "windows":
        return {"status": "error", "error": "powershell_windows_only", "shell": shell_name()}
    return run_shell_command(root=root, script=script, workdir=workdir, timeout_seconds=timeout_seconds)


def run_pytest_command(*, root: str | Path, test_path: str | None = None, timeout_seconds: int = 120, extra_args: list[str] | None = None) -> dict[str, Any]:
    command = ["python", "-m", "pytest"]
    if test_path and str(test_path).strip():
        command.append(str(test_path).strip())
    if extra_args:
        command.extend(str(item) for item in extra_args)
    return run_subprocess_command(
        command,
        cwd=resolve_command_workdir(root=root, workdir=None),
        timeout_seconds=max(1, min(int(timeout_seconds or 120), 900)),
    )


def git_status_command(*, root: str | Path, workdir: str | None = None) -> dict[str, Any]:
    return run_subprocess_command(["git", "status", "--short"], cwd=resolve_command_workdir(root=root, workdir=workdir), timeout_seconds=60)


def git_commit_command(*, root: str | Path, message: str, workdir: str | None = None, stage_all: bool = True) -> dict[str, Any]:
    if not isinstance(message, str) or not message.strip():
        return {"status": "error", "error": "message must be a non-empty string"}
    cwd = resolve_command_workdir(root=root, workdir=workdir)
    steps: list[dict[str, Any]] = []
    if stage_all:
        steps.append(run_subprocess_command(["git", "add", "-A"], cwd=cwd, timeout_seconds=120))
        if steps[-1]["status"] != "completed":
            return {"status": "failed", "step": "git_add", "steps": steps}
    steps.append(run_subprocess_command(["git", "commit", "-m", message.strip()], cwd=cwd, timeout_seconds=120))
    return {"status": steps[-1]["status"], "steps": steps}


def git_push_command(*, root: str | Path, remote: str = "origin", branch: str | None = None, workdir: str | None = None) -> dict[str, Any]:
    command = ["git", "push", str(remote or "origin")]
    if branch and str(branch).strip():
        command.append(str(branch).strip())
    return run_subprocess_command(command, cwd=resolve_command_workdir(root=root, workdir=workdir), timeout_seconds=300)
