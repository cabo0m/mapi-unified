from __future__ import annotations

from mapi_platform.windows import task_scheduler
from mapi_platform.windows.task_scheduler import CommandResult


def test_install_daily_task_uses_safe_fixed_schtasks_shape(monkeypatch) -> None:
    calls: list[list[str]] = []

    def runner(argv):
        values = [str(item) for item in argv]
        calls.append(values)
        return CommandResult(tuple(values), 0, "ok", "")

    monkeypatch.setattr(task_scheduler, "task_scheduler_available", lambda: True)
    monkeypatch.setattr(task_scheduler, "_schtasks_executable", lambda: "schtasks.exe")
    result = task_scheduler.install_daily_task(
        command='"maintenance.cmd"',
        task_name="MAPI Smoke Task",
        time_local="03:17",
        runner=runner,
    )
    assert result["status"] == "installed"
    assert calls == [[
        "schtasks.exe", "/Create", "/F", "/SC", "DAILY", "/ST", "03:17",
        "/TN", "MAPI Smoke Task", "/TR", '"maintenance.cmd"',
    ]]


def test_query_and_remove_task_are_idempotent(monkeypatch) -> None:
    responses = [
        CommandResult(("query",), 0, "found", ""),
        CommandResult(("delete",), 0, "removed", ""),
        CommandResult(("delete",), 1, "", "not found"),
    ]

    def runner(argv):
        return responses.pop(0)

    monkeypatch.setattr(task_scheduler, "task_scheduler_available", lambda: True)
    monkeypatch.setattr(task_scheduler, "_schtasks_executable", lambda: "schtasks.exe")
    assert task_scheduler.query_task("MAPI Test", runner=runner)["exists"] is True
    assert task_scheduler.remove_task("MAPI Test", runner=runner)["status"] == "removed"
    assert task_scheduler.remove_task("MAPI Test", runner=runner)["status"] == "not_found"


def test_task_name_and_time_validation_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(task_scheduler, "task_scheduler_available", lambda: True)
    monkeypatch.setattr(task_scheduler, "_schtasks_executable", lambda: "schtasks.exe")
    try:
        task_scheduler.install_daily_task(command="x", task_name="bad\nname", runner=lambda argv: None)
    except ValueError as exc:
        assert str(exc) == "invalid_windows_task_name"
    else:
        raise AssertionError("newline task name accepted")
    try:
        task_scheduler.install_daily_task(command="x", time_local="25:99", runner=lambda argv: None)
    except ValueError as exc:
        assert str(exc) == "invalid_windows_task_time"
    else:
        raise AssertionError("invalid task time accepted")
