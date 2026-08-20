from __future__ import annotations

"""Owned handlers for the local-only admin workshop."""

from typing import Any

import server_core as _base
from app.runtime import server_runtime as _runtime


def get_db_info() -> dict[str, Any]:
    return _base.get_db_info()


def query_sql(
    query: str,
    params_json: str = "[]",
    allow_write: bool = False,
    max_rows: int = 100,
) -> dict[str, Any]:
    return _base.query_sql(query=query, params_json=params_json, allow_write=allow_write, max_rows=max_rows)


def read_file_text(path: str, encoding: str = "utf-8", errors: str = "strict") -> dict[str, Any]:
    return _base.read_file_text(path=path, encoding=encoding, errors=errors)


def write_file_text(
    path: str,
    content: str,
    encoding: str = "utf-8",
    create_parents: bool = True,
) -> dict[str, Any]:
    return _base.write_file_text(
        path=path,
        content=content,
        encoding=encoding,
        create_parents=create_parents,
    )


def insert_before_marker(
    path: str,
    marker: str,
    content: str,
    encoding: str = "utf-8",
    dry_run: bool = False,
    backup: bool = True,
    require_marker_once: bool = True,
) -> dict[str, Any]:
    return _base.insert_before_marker(
        path=path,
        marker=marker,
        content=content,
        encoding=encoding,
        dry_run=dry_run,
        backup=backup,
        require_marker_once=require_marker_once,
    )


def insert_after_marker(
    path: str,
    marker: str,
    content: str,
    encoding: str = "utf-8",
    dry_run: bool = False,
    backup: bool = True,
    require_marker_once: bool = True,
) -> dict[str, Any]:
    return _base.insert_after_marker(
        path=path,
        marker=marker,
        content=content,
        encoding=encoding,
        dry_run=dry_run,
        backup=backup,
        require_marker_once=require_marker_once,
    )


def replace_once(
    path: str,
    find: str,
    replace: str,
    encoding: str = "utf-8",
    dry_run: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    return _base.replace_once(
        path=path,
        find=find,
        replace=replace,
        encoding=encoding,
        dry_run=dry_run,
        backup=backup,
    )


def delete_path(path: str, recursive: bool = True) -> dict[str, Any]:
    return _base.delete_path(path=path, recursive=recursive)


def run_shell(
    script: str,
    workdir: str | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    return _runtime.run_shell(
        script=script,
        workdir=workdir,
        timeout_seconds=timeout_seconds,
    )


def run_powershell(
    script: str,
    workdir: str | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    return _runtime.run_powershell(
        script=script,
        workdir=workdir,
        timeout_seconds=timeout_seconds,
    )


def run_pytest(
    test_path: str | None = None,
    timeout_seconds: int = 120,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    return _runtime.run_pytest(
        test_path=test_path,
        timeout_seconds=timeout_seconds,
        extra_args=extra_args,
    )


def git_status(workdir: str | None = None) -> dict[str, Any]:
    return _base.git_status(workdir=workdir)


def git_commit(
    message: str,
    workdir: str | None = None,
    stage_all: bool = True,
) -> dict[str, Any]:
    return _base.git_commit(message=message, workdir=workdir, stage_all=stage_all)


def git_push(
    remote: str = "origin",
    branch: str | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    return _base.git_push(remote=remote, branch=branch, workdir=workdir)
