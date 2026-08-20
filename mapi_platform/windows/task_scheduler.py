from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Sequence

DEFAULT_MAINTENANCE_TASK_NAME = "MAPI Aurora Memory Maintenance"
_TASK_TIME = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def _default_runner(argv: Sequence[str]) -> CommandResult:
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=False,
    )
    return CommandResult(tuple(str(item) for item in argv), completed.returncode, completed.stdout, completed.stderr)


def normalize_task_name(value: str | None) -> str:
    name = str(value or DEFAULT_MAINTENANCE_TASK_NAME).strip()
    if not name or len(name) > 200 or any(ch in name for ch in "\r\n\0"):
        raise ValueError("invalid_windows_task_name")
    return name


def task_scheduler_available() -> bool:
    return os.name == "nt" and shutil.which("schtasks.exe") is not None


def _schtasks_executable() -> str:
    executable = shutil.which("schtasks.exe")
    if not executable:
        raise RuntimeError("windows_task_scheduler_unavailable")
    return executable


def install_daily_task(
    *,
    command: str,
    task_name: str = DEFAULT_MAINTENANCE_TASK_NAME,
    time_local: str = "03:17",
    runner: Callable[[Sequence[str]], CommandResult] = _default_runner,
) -> dict[str, Any]:
    if not task_scheduler_available():
        raise RuntimeError("windows_task_scheduler_unavailable")
    name = normalize_task_name(task_name)
    if not _TASK_TIME.fullmatch(str(time_local)):
        raise ValueError("invalid_windows_task_time")
    raw_command = str(command or "").strip()
    if not raw_command or "\r" in raw_command or "\n" in raw_command:
        raise ValueError("invalid_windows_task_command")
    result = runner(
        [
            _schtasks_executable(),
            "/Create",
            "/F",
            "/SC",
            "DAILY",
            "/ST",
            str(time_local),
            "/TN",
            name,
            "/TR",
            raw_command,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError("windows_task_create_failed")
    return {
        "status": "installed",
        "task_name": name,
        "schedule": "daily",
        "time_local": str(time_local),
        "command": raw_command,
    }


def query_task(
    task_name: str = DEFAULT_MAINTENANCE_TASK_NAME,
    *,
    runner: Callable[[Sequence[str]], CommandResult] = _default_runner,
) -> dict[str, Any]:
    if not task_scheduler_available():
        return {"status": "unavailable", "task_name": normalize_task_name(task_name), "exists": False}
    name = normalize_task_name(task_name)
    result = runner([_schtasks_executable(), "/Query", "/TN", name])
    return {
        "status": "found" if result.returncode == 0 else "not_found",
        "task_name": name,
        "exists": result.returncode == 0,
    }


def remove_task(
    task_name: str = DEFAULT_MAINTENANCE_TASK_NAME,
    *,
    runner: Callable[[Sequence[str]], CommandResult] = _default_runner,
) -> dict[str, Any]:
    if not task_scheduler_available():
        return {"status": "unavailable", "task_name": normalize_task_name(task_name), "removed": False}
    name = normalize_task_name(task_name)
    result = runner([_schtasks_executable(), "/Delete", "/F", "/TN", name])
    if result.returncode not in {0, 1}:
        raise RuntimeError("windows_task_remove_failed")
    return {
        "status": "removed" if result.returncode == 0 else "not_found",
        "task_name": name,
        "removed": result.returncode == 0,
    }
