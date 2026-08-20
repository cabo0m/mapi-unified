from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from mapi_platform.selector import current_platform
from mapi_platform.shell import shell_command, shell_name

from app import backfill_logic, db_migrations, timeline as _timeline
from app.runtime.context import configure_runtime_context, get_runtime_context

from app.workshops.runtime_registry import bind_workshop_handlers
from app.workshops.idempotency import execute_idempotent
import server_core as _base

mcp = _base.mcp


def _replace_mcp_tool(func):
    """Register a server.py override without FastMCP duplicate-tool warnings.

    server_core imports already register a broad tool set. server.py intentionally
    overrides a small subset with timeline-aware wrappers, so remove the inherited
    local tool before registering the wrapper under the same name.
    """
    try:
        mcp.local_provider.remove_tool(func.__name__)
    except Exception:
        pass
    return mcp.tool(func)


_CONTEXT = get_runtime_context()
ROOT = _CONTEXT.root
DATA_DIR = _CONTEXT.data_dir
DB_PATH = _CONTEXT.db_path


def configure_runtime_paths(
    *,
    root: str | Path | None = None,
    data_dir: str | Path | None = None,
    db_path: str | Path | None = None,
):
    global ROOT, DATA_DIR, DB_PATH
    context = configure_runtime_context(root=root, data_dir=data_dir, db_path=db_path)
    ROOT = context.root
    DATA_DIR = context.data_dir
    DB_PATH = context.db_path
    return context


def _sync_base_config() -> None:
    configure_runtime_paths(root=ROOT, data_dir=DATA_DIR, db_path=DB_PATH)


_base._sync_config = _sync_base_config

SAFE_ROLLBACK_ACTION_TYPES = set(_base.SAFE_ROLLBACK_ACTION_TYPES) | {
    "summary_memory_updated",
    "summary_link_deleted",
}

DEPENDENCY_BLOCKING_MODES = {
    "run",
    "conflict_run",
    "conflict_resolution_run",
    "consolidation_run",
    "consolidation_apply_run",
    "rollback",
}


if not hasattr(_base, "_timeline_original_insert_memory"):
    _base._timeline_original_insert_memory = _base._insert_memory
if not hasattr(_base, "_timeline_original_create_link"):
    _base._timeline_original_create_link = _base._create_link

_ORIGINAL_INSERT_MEMORY = _base._timeline_original_insert_memory
_ORIGINAL_CREATE_LINK = _base._timeline_original_create_link


def _resolve_shell_workdir(workdir: str | None) -> Path:
    _sync_base_config()
    repo_root = Path(ROOT).resolve()

    if workdir is None or not str(workdir).strip():
        target = repo_root
    else:
        raw_target = Path(str(workdir).strip()).expanduser()
        target = (repo_root / raw_target).resolve() if not raw_target.is_absolute() else raw_target.resolve()

    try:
        target.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"workdir musi być w obrębie repo: {repo_root}") from exc

    if not target.exists():
        raise ValueError(f"workdir nie istnieje: {target}")

    if not target.is_dir():
        raise ValueError(f"workdir nie jest katalogiem: {target}")

    return target


def _resolve_repo_file(path: str) -> Path:
    _sync_base_config()
    repo_root = Path(ROOT).resolve()

    if not isinstance(path, str) or not path.strip():
        raise ValueError("path musi być niepustym stringiem")

    raw_target = Path(path.strip()).expanduser()
    target = (repo_root / raw_target).resolve() if not raw_target.is_absolute() else raw_target.resolve()

    try:
        target.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"path musi być w obrębie repo: {repo_root}") from exc

    if not target.exists():
        raise ValueError(f"plik nie istnieje: {target}")

    if not target.is_file():
        raise ValueError(f"path nie jest plikiem: {target}")

    return target


def _run_subprocess_command(command: list[str], *, cwd: Path, timeout_seconds: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "status": "completed",
            "exit_code": int(completed.returncode),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "workdir": str(cwd),
            "command": command,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (
            exc.stdout.decode("utf-8", "replace") if exc.stdout else ""
        )
        stderr = exc.stderr if isinstance(exc.stderr, str) else (
            exc.stderr.decode("utf-8", "replace") if exc.stderr else ""
        )
        return {
            "status": "timeout",
            "exit_code": None,
            "stdout": stdout,
            "stderr": stderr,
            "workdir": str(cwd),
            "command": command,
            "timeout_seconds": timeout_seconds,
        }


def _get_rollbackable_actions(conn, target_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM sleep_run_actions WHERE run_id = ? ORDER BY id DESC",
        (target_run_id,),
    ).fetchall()
    actions = [_base.row_to_dict(row) for row in rows]
    filtered = [item for item in actions if item["action_type"] in SAFE_ROLLBACK_ACTION_TYPES]
    rollback_priority = {
        "summary_link_deleted": 0,
        "summary_link_created": 1,
        "duplicate_link_created": 1,
        "support_link_created": 1,
        "conflict_link_created": 1,
        "summary_memory_updated": 2,
        "summary_memory_created": 9,
    }
    filtered.sort(key=lambda item: (rollback_priority.get(str(item["action_type"]), 5), -int(item["id"])))
    return filtered


def _collect_ints_from_payload(value: Any, interesting_keys: set[str]) -> set[int]:
    decoded = _base._decode_action_value(value)
    result: set[int] = set()

    if isinstance(decoded, dict):
        for key, nested in decoded.items():
            if key in interesting_keys and nested is not None:
                try:
                    result.add(int(nested))
                except (TypeError, ValueError):
                    pass
            if isinstance(nested, (dict, list)):
                result.update(_collect_ints_from_payload(nested, interesting_keys))
    elif isinstance(decoded, list):
        for item in decoded:
            result.update(_collect_ints_from_payload(item, interesting_keys))

    return result


def _action_memory_ids(action: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    memory_id = action.get("memory_id")
    if memory_id is not None:
        ids.add(int(memory_id))

    interesting_keys = {
        "memory_id",
        "restored_memory_id",
        "deleted_memory_id",
        "from_memory_id",
        "to_memory_id",
        "canonical_memory_id",
        "duplicate_memory_id",
        "memory_a_id",
        "memory_b_id",
    }
    ids.update(_collect_ints_from_payload(action.get("old_value"), interesting_keys))
    ids.update(_collect_ints_from_payload(action.get("new_value"), interesting_keys))
    return ids


def _action_link_ids(action: dict[str, Any]) -> set[int]:
    interesting_keys = {"link_id", "deleted_link_id"}
    ids: set[int] = set()
    ids.update(_collect_ints_from_payload(action.get("old_value"), interesting_keys))
    ids.update(_collect_ints_from_payload(action.get("new_value"), interesting_keys))
    return ids


def _summary_memory_payload(memory_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary_short": memory_row.get("summary_short"),
        "content": memory_row.get("content"),
        "source": memory_row.get("source"),
        "importance_score": round(float(memory_row.get("importance_score") or 0.5), 3),
        "confidence_score": round(float(memory_row.get("confidence_score") or 0.5), 3),
        "tags": memory_row.get("tags"),
    }


def _get_dependent_runs(conn, target_run_id: int) -> list[dict[str, Any]]:
    target_actions = _get_rollbackable_actions(conn, target_run_id)
    target_memory_ids: set[int] = set()
    target_link_ids: set[int] = set()
    for action in target_actions:
        target_memory_ids.update(_action_memory_ids(action))
        target_link_ids.update(_action_link_ids(action))

    mode_placeholders = ",".join("?" for _ in DEPENDENCY_BLOCKING_MODES)
    later_runs = conn.execute(
        f"""
        SELECT *
        FROM sleep_runs
        WHERE id > ?
          AND status LIKE 'completed%'
          AND mode IN ({mode_placeholders})
        ORDER BY id ASC
        """,
        (target_run_id, *sorted(DEPENDENCY_BLOCKING_MODES)),
    ).fetchall()

    dependencies: list[dict[str, Any]] = []
    for run in later_runs:
        run_id = int(run["id"])
        rows = conn.execute(
            "SELECT * FROM sleep_run_actions WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        ).fetchall()
        actions = [_base.row_to_dict(row) for row in rows]
        run_memory_ids: set[int] = set()
        run_link_ids: set[int] = set()
        for action in actions:
            run_memory_ids.update(_action_memory_ids(action))
            run_link_ids.update(_action_link_ids(action))

        touched_memory_ids = sorted(target_memory_ids & run_memory_ids)
        touched_link_ids = sorted(target_link_ids & run_link_ids)
        if touched_memory_ids or touched_link_ids:
            dependencies.append(
                {
                    "run_id": run_id,
                    "mode": str(run["mode"]),
                    "status": str(run["status"]),
                    "touched_memory_ids": touched_memory_ids,
                    "touched_link_ids": touched_link_ids,
                }
            )

    return dependencies


def _insert_memory(
    conn,
    *,
    content: str,
    memory_type: str,
    summary_short: str | None = None,
    source: str | None = None,
    importance_score: float = 0.5,
    confidence_score: float = 0.5,
    tags: str | None = None,
    operation_id: str | None = None,
    **extra_fields: Any,
) -> dict[str, Any]:
    original_kwargs: dict[str, Any] = {
        "content": content,
        "memory_type": memory_type,
        "summary_short": summary_short,
        "source": source,
        "importance_score": importance_score,
        "confidence_score": confidence_score,
        "tags": tags,
    }
    original_kwargs.update(extra_fields)
    memory = _ORIGINAL_INSERT_MEMORY(
        conn,
        **original_kwargs,
    )
    _timeline.record_timeline_event(
        conn,
        event_type="memory.created",
        event_time=memory.get("created_at"),
        memory_id=int(memory["id"]),
        operation_id=operation_id,
        source_table="memories",
        source_row_id=int(memory["id"]),
        origin=source or "api",
        payload={
            "memory_type": memory.get("memory_type"),
            "summary_short": memory.get("summary_short"),
            "importance_score": memory.get("importance_score"),
            "confidence_score": memory.get("confidence_score"),
            "tags": memory.get("tags"),
        },
        now_fn=_base.utc_now_iso,
    )
    return memory


def _create_link(conn, from_memory_id: int, to_memory_id: int, relation_type: str, weight: float, origin: str | None, operation_id: str | None = None) -> dict[str, Any]:
    link = _ORIGINAL_CREATE_LINK(conn, from_memory_id, to_memory_id, relation_type, weight, origin)
    created_at = link.get("created_at") or _base.utc_now_iso()
    conn.execute("UPDATE memory_links SET created_at = COALESCE(created_at, ?) WHERE id = ?", (created_at, int(link["id"])))
    row = conn.execute("SELECT * FROM memory_links WHERE id = ?", (int(link["id"]),)).fetchone()
    link = _base.row_to_dict(row)
    _timeline.record_timeline_event(
        conn,
        event_type="link.created",
        event_time=created_at,
        memory_id=int(from_memory_id),
        related_memory_id=int(to_memory_id),
        operation_id=operation_id,
        source_table="memory_links",
        source_row_id=int(link["id"]),
        origin=origin or "api",
        payload={
            "relation_type": relation_type,
            "weight": float(weight),
        },
        now_fn=_base.utc_now_iso,
    )
    return link


def create_sleep_run(conn, mode: str, freedom_level: int, notes: str | None = None, rollback_of_run_id: int | None = None, workspace_id: int | None = None, project_key: str | None = None) -> int:
    started_at = _base.utc_now_iso()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO sleep_runs (started_at, status, mode, freedom_level, notes, rollback_of_run_id, workspace_id, project_key)
        VALUES (?, 'started', ?, ?, ?, ?, ?, ?)
        """,
        (started_at, mode, int(freedom_level), notes, rollback_of_run_id, workspace_id, project_key),
    )
    run_id = int(cursor.lastrowid)
    operation_id = _timeline.run_operation_id(run_id)
    _timeline.record_timeline_event(
        conn,
        event_type="sleep_run.started",
        event_time=started_at,
        run_id=run_id,
        operation_id=operation_id,
        source_table="sleep_runs",
        source_row_id=run_id,
        origin="system",
        payload={
            "mode": mode,
            "freedom_level": int(freedom_level),
            "notes": notes,
            "rollback_of_run_id": rollback_of_run_id,
            "workspace_id": workspace_id,
            "project_key": project_key,
        },
        now_fn=_base.utc_now_iso,
    )
    conn.commit()
    return run_id


def add_sleep_action(conn, run_id: int, action_type: str, memory_id: int | None, old_value: Any, new_value: Any, reason: str) -> None:
    created_at = _base.utc_now_iso()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO sleep_run_actions (run_id, memory_id, action_type, old_value, new_value, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            memory_id,
            action_type,
            None if old_value is None else json.dumps(old_value, ensure_ascii=False),
            None if new_value is None else json.dumps(new_value, ensure_ascii=False),
            reason,
            created_at,
        ),
    )
    action_id = int(cursor.lastrowid)
    operation_id = _timeline.run_operation_id(int(run_id))
    _timeline.record_timeline_event(
        conn,
        event_type=f"sleep_action.{action_type}",
        event_time=created_at,
        memory_id=None if memory_id is None else int(memory_id),
        run_id=int(run_id),
        operation_id=operation_id,
        source_table="sleep_run_actions",
        source_row_id=action_id,
        origin="system",
        payload={"reason": reason, "old_value": old_value, "new_value": new_value},
        now_fn=_base.utc_now_iso,
    )


def finalize_sleep_run(conn, run_id: int, **kwargs: Any) -> None:
    finished_at = _base.utc_now_iso()
    conn.execute(
        """
        UPDATE sleep_runs
        SET
            finished_at = ?,
            status = ?,
            scanned_count = ?,
            changed_count = ?,
            archived_count = ?,
            downgraded_count = ?,
            duplicate_count = ?,
            conflict_count = ?,
            created_summary_count = ?
        WHERE id = ?
        """,
        (
            finished_at,
            kwargs["status"],
            kwargs["scanned_count"],
            kwargs["changed_count"],
            kwargs["archived_count"],
            kwargs["downgraded_count"],
            kwargs["duplicate_count"],
            kwargs.get("conflict_count", 0),
            kwargs.get("created_summary_count", 0),
            run_id,
        ),
    )
    _timeline.record_timeline_event(
        conn,
        event_type="sleep_run.finished",
        event_time=finished_at,
        run_id=int(run_id),
        operation_id=_timeline.run_operation_id(int(run_id)),
        source_table="sleep_runs",
        source_row_id=int(run_id),
        origin="system",
        payload={
            "status": kwargs["status"],
            "scanned_count": kwargs["scanned_count"],
            "changed_count": kwargs["changed_count"],
            "archived_count": kwargs["archived_count"],
            "downgraded_count": kwargs["downgraded_count"],
            "duplicate_count": kwargs["duplicate_count"],
            "conflict_count": kwargs.get("conflict_count", 0),
            "created_summary_count": kwargs.get("created_summary_count", 0),
        },
        now_fn=_base.utc_now_iso,
    )
    conn.commit()


def _rollback_single_action(conn, rollback_run_id: int, action: dict[str, Any]) -> dict[str, Any]:
    return _base._rollback_single_action(conn, rollback_run_id, action)


@_replace_mcp_tool
def preview_undo_run(run_id: int) -> dict[str, Any]:
    conn = _base.get_db_connection()
    try:
        run = _base.require_sleep_run_row(conn, run_id)
        existing_rollback_run_id = _base._existing_rollback_run_id(conn, run_id)
        rollbackable_actions = _get_rollbackable_actions(conn, run_id)
        dependent_runs = _get_dependent_runs(conn, run_id)
        summary: dict[str, int] = {}
        for action in rollbackable_actions:
            summary[action["action_type"]] = summary.get(action["action_type"], 0) + 1
        return {
            "status": "preview_completed",
            "target_run": _base.row_to_dict(run),
            "already_rolled_back": existing_rollback_run_id is not None,
            "existing_rollback_run_id": existing_rollback_run_id,
            "rollbackable_action_count": len(rollbackable_actions),
            "rollbackable_action_summary": summary,
            "rollbackable_actions": rollbackable_actions,
            "has_dependent_runs": len(dependent_runs) > 0,
            "dependent_run_count": len(dependent_runs),
            "dependent_runs": dependent_runs,
        }
    finally:
        conn.close()


@_replace_mcp_tool
def undo_run(run_id: int, notes: str | None = None) -> dict[str, Any]:
    conn = _base.get_db_connection()
    try:
        run = _base.require_sleep_run_row(conn, run_id)
        mode = str(run["mode"])
        status = str(run["status"])
        if mode not in {"run", "conflict_run", "conflict_resolution_run", "consolidation_run", "consolidation_apply_run"}:
            return {"status": "error", "error": 'Undo obsługuje tylko przebiegi wykonawcze: run, conflict_run, conflict_resolution_run, consolidation_run albo consolidation_apply_run'}
        if not status.startswith("completed"):
            return {"status": "error", "error": 'Undo można wykonać tylko dla zakończonego przebiegu completed'}
        existing_rollback_run_id = _base._existing_rollback_run_id(conn, run_id)
        if existing_rollback_run_id is not None:
            return {"status": "error", "error": f'Ten run został już cofnięty przez rollback run_id={existing_rollback_run_id}'}

        dependent_runs = _get_dependent_runs(conn, run_id)
        if dependent_runs:
            dependent_run_ids = ", ".join(str(item["run_id"]) for item in dependent_runs)
            return {"status": "error", "error": f'Nie można cofnąć run_id={run_id}, bo zależne późniejsze runy dotknęły tych samych obiektów: {dependent_run_ids}'}

        rollbackable_actions = _get_rollbackable_actions(conn, run_id)
        rollback_run_id = create_sleep_run(
            conn,
            mode="rollback",
            freedom_level=0,
            notes=notes or f"rollback_of_run_{run_id}",
            rollback_of_run_id=run_id,
        )
        restored_items = [_rollback_single_action(conn, rollback_run_id, action) for action in rollbackable_actions]
        _timeline.record_timeline_event(
            conn,
            event_type="undo.applied",
            run_id=rollback_run_id,
            operation_id=_timeline.run_operation_id(int(rollback_run_id)),
            source_table="sleep_runs",
            source_row_id=rollback_run_id,
            origin="system",
            payload={"target_run_id": run_id, "restored_count": len(restored_items)},
            now_fn=_base.utc_now_iso,
        )
        conn.commit()
        finalize_sleep_run(
            conn,
            rollback_run_id,
            status="completed",
            scanned_count=len(rollbackable_actions),
            changed_count=len(restored_items),
            archived_count=0,
            downgraded_count=0,
            duplicate_count=0,
            conflict_count=0,
            created_summary_count=0,
        )
        return {
            "status": "completed",
            "rollback_run_id": rollback_run_id,
            "target_run_id": run_id,
            "restored_count": len(restored_items),
            "restored_items": restored_items,
        }
    finally:
        conn.close()


def _recall_memory_once(
    *,
    memory_id: int,
    strength: float,
    recall_type: str,
    source: str | None,
) -> dict[str, Any]:
    conn = _base.get_db_connection()
    try:
        result = _base.recall_memory_payload(
            conn,
            memory_id=memory_id,
            strength=strength,
            recall_type=recall_type,
            source=source,
            commit=False,
            require_memory_row=_base.require_memory_row,
            normalize_score=_base.normalize_score,
            normalize_optional_text=_base.normalize_optional_text,
            utc_now_iso=_base.utc_now_iso,
            insert_memory_event=_base.insert_memory_event,
            row_to_dict=_base.row_to_dict,
            enrich_memory_dict=_base.enrich_memory_dict,
        )
        recalled_at = result["telemetry_changes"]["last_recalled_at"]
        operation_id = _timeline.new_operation_id("mem")
        _timeline.record_timeline_event(
            conn,
            event_type="memory.recalled",
            event_time=recalled_at,
            memory_id=int(memory_id),
            operation_id=operation_id,
            source_table="memory_events",
            source_row_id=int(result["event"]["id"]),
            origin=result["recall_type"],
            payload={
                "schema": result["schema"],
                "strength": result["strength"],
                "source": result["source"],
                "old_recall_count": result["telemetry_changes"]["old_recall_count"],
                "new_recall_count": result["telemetry_changes"]["new_recall_count"],
                "importance_score": result["importance_decoupling"]["new_importance_score"],
                "importance_changed": False,
            },
            now_fn=_base.utc_now_iso,
        )
        conn.commit()
        result["timeline_operation_id"] = operation_id
        return result
    finally:
        conn.close()


def recall_memory(
    memory_id: int,
    strength: float = 0.1,
    recall_type: str = "manual",
    source: str | None = "runtime",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    request_payload = {
        "memory_id": int(memory_id),
        "strength": float(strength),
        "recall_type": recall_type,
        "source": source,
    }
    return execute_idempotent(
        get_db_connection=get_db_connection,
        idempotency_key=idempotency_key,
        operation_name="direct:recall_memory",
        request_payload=request_payload,
        handler=lambda: _recall_memory_once(
            memory_id=memory_id,
            strength=strength,
            recall_type=recall_type,
            source=source,
        ),
    )


@_replace_mcp_tool
def get_timeline(
    limit: int = 100,
    offset: int = 0,
    memory_id: int | None = None,
    run_id: int | None = None,
    operation_id: str | None = None,
    event_type: str | None = None,
    timeline_scope: str | None = None,
    semantic_kind: str | None = None,
    project_key: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    if limit < 1:
        return {"status": "error", "error": 'limit musi być >= 1'}
    if offset < 0:
        return {"status": "error", "error": 'offset musi być >= 0'}
    conn = _base.get_db_connection()
    try:
        items = _timeline.timeline_query(
            conn,
            limit=limit,
            offset=offset,
            memory_id=memory_id,
            run_id=run_id,
            operation_id=operation_id,
            event_type=event_type,
            timeline_scope=timeline_scope,
            semantic_kind=semantic_kind,
            project_key=project_key,
            from_time=from_time,
            to_time=to_time,
            row_to_dict=_base.row_to_dict,
        )
    finally:
        conn.close()
    return {
        "count": len(items),
        "items": items,
        "filters": {
            "limit": limit,
            "offset": offset,
            "memory_id": memory_id,
            "run_id": run_id,
            "operation_id": operation_id,
            "event_type": event_type,
            "timeline_scope": timeline_scope,
            "semantic_kind": semantic_kind,
            "project_key": project_key,
            "from_time": from_time,
            "to_time": to_time,
        },
    }


@_replace_mcp_tool
def get_project_timeline(
    project_key: str,
    limit: int = 100,
    offset: int = 0,
    semantic_kind: str | None = None,
    event_type: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
) -> dict[str, Any]:
    if not isinstance(project_key, str) or not project_key.strip():
        return {"status": "error", "error": 'project_key musi być niepustym stringiem'}
    if limit < 1:
        return {"status": "error", "error": 'limit musi być >= 1'}
    if offset < 0:
        return {"status": "error", "error": 'offset musi być >= 0'}

    normalized_project_key = project_key.strip()
    conn = _base.get_db_connection()
    try:
        items = _timeline.timeline_query(
            conn,
            limit=limit,
            offset=offset,
            timeline_scope="project",
            semantic_kind=semantic_kind,
            project_key=normalized_project_key,
            event_type=event_type,
            from_time=from_time,
            to_time=to_time,
            row_to_dict=_base.row_to_dict,
        )
    finally:
        conn.close()

    return {
        "project_key": normalized_project_key,
        "count": len(items),
        "items": items,
        "filters": {
            "limit": limit,
            "offset": offset,
            "semantic_kind": semantic_kind,
            "event_type": event_type,
            "from_time": from_time,
            "to_time": to_time,
        },
    }


@_replace_mcp_tool
def record_project_timeline_event(
    project_key: str,
    event_type: str,
    title: str,
    description: str | None = None,
    valid_at: str | None = None,
    origin: str | None = "manual",
    memory_ids: list[int] | None = None,
    run_ids: list[int] | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    canonical: bool = True,
    category: str | None = None,
) -> dict[str, Any]:
    conn = _base.get_db_connection()
    try:
        event_id = _timeline.record_project_event(
            conn,
            project_key=project_key,
            event_type=event_type,
            title=title,
            description=description,
            valid_at=valid_at,
            origin=origin,
            memory_ids=memory_ids,
            run_ids=run_ids,
            tags=tags,
            status=status,
            canonical=canonical,
            category=category,
            now_fn=_base.utc_now_iso,
        )
        conn.commit()
        row = conn.execute("SELECT * FROM timeline_events WHERE id = ?", (event_id,)).fetchone()
        item = _timeline.timeline_rows_to_dicts([row], row_to_dict=_base.row_to_dict)[0] if row is not None else None
    finally:
        conn.close()

    return {
        "status": "completed",
        "event_id": event_id,
        "item": item,
    }


@_replace_mcp_tool
def get_memory_timeline(memory_id: int, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    if limit < 1:
        return {"status": "error", "error": 'limit musi być >= 1'}
    if offset < 0:
        return {"status": "error", "error": 'offset musi być >= 0'}
    conn = _base.get_db_connection()
    try:
        _base.require_memory_row(conn, memory_id)
        items = _timeline.timeline_query(
            conn,
            limit=limit,
            offset=offset,
            memory_id=memory_id,
            row_to_dict=_base.row_to_dict,
        )
    finally:
        conn.close()
    return {"memory_id": memory_id, "count": len(items), "items": items, "limit": limit, "offset": offset}


@_replace_mcp_tool
def backfill_timeline() -> dict[str, Any]:
    conn = _base.get_db_connection()
    try:
        inserted_count = _timeline.backfill_timeline(conn)
        conn.commit()
    finally:
        conn.close()
    return {"status": "completed", "inserted_count": inserted_count}


@_replace_mcp_tool
def get_schema_migrations() -> dict[str, Any]:
    conn = _base.store.get_db_connection()
    try:
        versions = sorted(db_migrations.applied_migration_versions(conn))
    finally:
        conn.close()
    return {
        "count": len(versions),
        "versions": versions,
        "latest": versions[-1] if versions else None,
    }


@_replace_mcp_tool
def apply_schema_migrations() -> dict[str, Any]:
    _sync_base_config()
    conn = _base.store.get_db_connection()
    try:
        ran = db_migrations.apply_all_migrations(conn)
        conn.commit()
        versions = sorted(db_migrations.applied_migration_versions(conn))
    finally:
        conn.close()
    return {
        "status": "completed",
        "applied_now": ran,
        "count_applied_now": len(ran),
        "all_versions": versions,
        "latest": versions[-1] if versions else None,
    }


def _run_consolidation_v1_impl(notes: str | None = None) -> dict[str, Any]:
    conn = _base.get_db_connection()
    try:
        run_id = create_sleep_run(conn, mode="consolidation_run", freedom_level=0, notes=notes)
        candidates = _base.consolidation_logic.get_consolidation_candidates(conn)
        support_links_created: list[dict[str, Any]] = []
        summary_links_created: list[dict[str, Any]] = []
        summary_links_deleted: list[dict[str, Any]] = []
        created_summary_memories: list[dict[str, Any]] = []
        updated_summary_memories: list[dict[str, Any]] = []
        central_evidence_boosted: list[dict[str, Any]] = []

        for candidate in candidates:
            central_id = int(candidate["central_memory_id"])
            links_created_for_cluster = 0
            for member_id in candidate["supporting_memory_ids"]:
                if _base.consolidation_logic.support_link_exists(conn, int(member_id), central_id):
                    continue
                item = _create_link(conn, int(member_id), central_id, "supports", float(candidate["average_gravity"] or 0.5), "consolidation_v1_auto")
                support_links_created.append(item)
                links_created_for_cluster += 1
                add_sleep_action(conn, run_id, "support_link_created", int(member_id), None, {"link_id": item["id"], "from_memory_id": int(member_id), "to_memory_id": central_id, "relation_type": "supports"}, "gravity_support_link")

            proposed_summary = dict(candidate["proposed_summary_memory"])
            summary_memory_id = candidate.get("existing_summary_memory_id")
            if summary_memory_id is None:
                created_summary = _insert_memory(
                    conn,
                    content=str(proposed_summary["content"]),
                    memory_type=str(proposed_summary["memory_type"]),
                    summary_short=proposed_summary.get("summary_short"),
                    source=proposed_summary.get("source"),
                    importance_score=float(proposed_summary.get("importance_score") or 0.5),
                    confidence_score=float(proposed_summary.get("confidence_score") or 0.5),
                    tags=proposed_summary.get("tags"),
                )
                summary_memory_id = int(created_summary["id"])
                created_summary_memories.append(created_summary)
                add_sleep_action(conn, run_id, "summary_memory_created", summary_memory_id, None, {"memory_id": summary_memory_id, "memory_type": created_summary["memory_type"], "summary_short": created_summary.get("summary_short")}, "gravity_summary_memory_created")
            else:
                existing_summary = _base.row_to_dict(_base.require_memory_row(conn, int(summary_memory_id)))
                old_payload = _summary_memory_payload(existing_summary)
                new_payload = {
                    "summary_short": proposed_summary.get("summary_short"),
                    "content": proposed_summary.get("content"),
                    "source": proposed_summary.get("source"),
                    "importance_score": round(float(proposed_summary.get("importance_score") or 0.5), 3),
                    "confidence_score": round(float(proposed_summary.get("confidence_score") or 0.5), 3),
                    "tags": proposed_summary.get("tags"),
                }
                if old_payload != new_payload:
                    updated_at = _base.utc_now_iso()
                    conn.execute(
                        """
                        UPDATE memories
                        SET summary_short = ?, content = ?, source = ?, importance_score = ?, confidence_score = ?, tags = ?, last_accessed_at = ?
                        WHERE id = ?
                        """,
                        (
                            new_payload["summary_short"],
                            new_payload["content"],
                            new_payload["source"],
                            float(new_payload["importance_score"]),
                            float(new_payload["confidence_score"]),
                            new_payload["tags"],
                            updated_at,
                            int(summary_memory_id),
                        ),
                    )
                    _timeline.record_timeline_event(
                        conn,
                        event_type="memory.updated",
                        event_time=updated_at,
                        memory_id=int(summary_memory_id),
                        source_table="memories",
                        source_row_id=int(summary_memory_id),
                        origin="consolidation_v1_auto",
                        payload={"old": old_payload, "new": new_payload, "reason": "gravity_summary_memory_refreshed"},
                        now_fn=_base.utc_now_iso,
                    )
                    updated_summary = _base.row_to_dict(_base.require_memory_row(conn, int(summary_memory_id)))
                    updated_summary_memories.append(updated_summary)
                    add_sleep_action(conn, run_id, "summary_memory_updated", int(summary_memory_id), old_payload, new_payload, "gravity_summary_memory_refreshed")

            summarize_rows = conn.execute(
                "SELECT * FROM memory_links WHERE from_memory_id = ? AND relation_type = 'summarizes' ORDER BY id ASC",
                (int(summary_memory_id),),
            ).fetchall()
            summarize_target_exists = False
            for row in summarize_rows:
                row_dict = _base.row_to_dict(row)
                if int(row_dict["to_memory_id"]) == central_id:
                    summarize_target_exists = True
                    continue
                archived_at = _base.utc_now_iso()
                conn.execute("UPDATE memory_links SET archived_at = ? WHERE id = ?", (archived_at, int(row_dict["id"])))
                row_dict["archived_at"] = archived_at
                _timeline.record_timeline_event(
                    conn,
                    event_type="link.archived",
                    event_time=archived_at,
                    memory_id=int(row_dict["from_memory_id"]),
                    related_memory_id=int(row_dict["to_memory_id"]),
                    source_table="memory_links",
                    source_row_id=int(row_dict["id"]),
                    origin="consolidation_v1_auto",
                    payload={"relation_type": row_dict["relation_type"], "reason": "gravity_summary_link_removed", "archived_at": archived_at},
                    now_fn=_base.utc_now_iso,
                )
                conn.execute("DELETE FROM memory_links WHERE id = ?", (int(row_dict["id"]),))
                summary_links_deleted.append(row_dict)
                add_sleep_action(conn, run_id, "summary_link_deleted", int(summary_memory_id), row_dict, None, "gravity_summary_link_removed")
            if not summarize_target_exists:
                item = _create_link(conn, int(summary_memory_id), central_id, "summarizes", 1.0, "consolidation_v1_auto")
                summary_links_created.append(item)
                add_sleep_action(conn, run_id, "summary_link_created", int(summary_memory_id), None, {"link_id": item["id"], "from_memory_id": int(summary_memory_id), "to_memory_id": central_id, "relation_type": "summarizes"}, "gravity_summary_link")

            current_member_ids = {int(item) for item in candidate["member_ids"]}
            consolidated_rows = conn.execute(
                "SELECT * FROM memory_links WHERE from_memory_id = ? AND relation_type = 'consolidated_from' ORDER BY id ASC",
                (int(summary_memory_id),),
            ).fetchall()
            existing_member_links: set[int] = set()
            for row in consolidated_rows:
                row_dict = _base.row_to_dict(row)
                target_memory_id = int(row_dict["to_memory_id"])
                if target_memory_id in current_member_ids:
                    existing_member_links.add(target_memory_id)
                    continue
                archived_at = _base.utc_now_iso()
                conn.execute("UPDATE memory_links SET archived_at = ? WHERE id = ?", (archived_at, int(row_dict["id"])))
                row_dict["archived_at"] = archived_at
                _timeline.record_timeline_event(
                    conn,
                    event_type="link.archived",
                    event_time=archived_at,
                    memory_id=int(row_dict["from_memory_id"]),
                    related_memory_id=int(row_dict["to_memory_id"]),
                    source_table="memory_links",
                    source_row_id=int(row_dict["id"]),
                    origin="consolidation_v1_auto",
                    payload={"relation_type": row_dict["relation_type"], "reason": "gravity_summary_link_removed", "archived_at": archived_at},
                    now_fn=_base.utc_now_iso,
                )
                conn.execute("DELETE FROM memory_links WHERE id = ?", (int(row_dict["id"]),))
                summary_links_deleted.append(row_dict)
                add_sleep_action(conn, run_id, "summary_link_deleted", int(summary_memory_id), row_dict, None, "gravity_summary_link_removed")
            for member_id in sorted(current_member_ids - existing_member_links):
                item = _create_link(conn, int(summary_memory_id), int(member_id), "consolidated_from", 1.0, "consolidation_v1_auto")
                summary_links_created.append(item)
                add_sleep_action(conn, run_id, "summary_link_created", int(summary_memory_id), None, {"link_id": item["id"], "from_memory_id": int(summary_memory_id), "to_memory_id": int(member_id), "relation_type": "consolidated_from"}, "gravity_summary_link")

            if links_created_for_cluster > 0:
                central_memory = _base.require_memory_row(conn, central_id)
                old_evidence_count = int(central_memory["evidence_count"] or 1)
                new_evidence_count = old_evidence_count + links_created_for_cluster
                changed_at = _base.utc_now_iso()
                conn.execute("UPDATE memories SET evidence_count = ?, sandman_note = ? WHERE id = ?", (new_evidence_count, f"Consolidation V1: gravity cluster of {candidate['member_count']} memories", central_id))
                _timeline.record_timeline_event(
                    conn,
                    event_type="memory.updated",
                    event_time=changed_at,
                    memory_id=central_id,
                    source_table="memories",
                    source_row_id=central_id,
                    origin="consolidation_v1_auto",
                    payload={
                        "changed_fields": ["evidence_count", "sandman_note"],
                        "old": {"evidence_count": old_evidence_count},
                        "new": {"evidence_count": new_evidence_count},
                        "reason": "gravity_cluster_support_bonus",
                    },
                    now_fn=_base.utc_now_iso,
                )
                boosted = {"memory_id": central_id, "old_evidence_count": old_evidence_count, "new_evidence_count": new_evidence_count}
                central_evidence_boosted.append(boosted)
                add_sleep_action(conn, run_id, "canonical_evidence_boosted", central_id, {"evidence_count": old_evidence_count}, {"evidence_count": new_evidence_count}, "gravity_cluster_support_bonus")

        conn.commit()
        scanned_count = conn.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
        changed_count = len(support_links_created) + len(summary_links_created) + len(summary_links_deleted) + len(created_summary_memories) + len(updated_summary_memories) + len(central_evidence_boosted)
        finalize_sleep_run(conn, run_id, status="completed", scanned_count=int(scanned_count), changed_count=changed_count, archived_count=0, downgraded_count=0, duplicate_count=0, conflict_count=0, created_summary_count=len(created_summary_memories))
        return {
            "status": "completed",
            "run_id": run_id,
            "scanned_count": int(scanned_count),
            "consolidation_candidates": candidates,
            "support_links_created": support_links_created,
            "summary_links_created": summary_links_created,
            "summary_links_deleted": summary_links_deleted,
            "created_summary_memories": created_summary_memories,
            "updated_summary_memories": updated_summary_memories,
            "central_evidence_boosted": central_evidence_boosted,
            "summary": {
                "cluster_count": len(candidates),
                "links_created_count": len(support_links_created),
                "support_links_created_count": len(support_links_created),
                "summary_links_created_count": len(summary_links_created),
                "summary_links_deleted_count": len(summary_links_deleted),
                "summary_memories_created_count": len(created_summary_memories),
                "summary_memories_updated_count": len(updated_summary_memories),
                "central_evidence_boost_count": len(central_evidence_boosted),
                "changed_count": changed_count,
            },
        }
    finally:
        conn.close()


@_replace_mcp_tool
def run_shell(script: str, workdir: str | None = None, timeout_seconds: int = 60) -> dict[str, Any]:
    if not isinstance(script, str) or not script.strip():
        return {"status": "error", "error": "script_must_be_non_empty"}
    timeout_seconds = int(timeout_seconds)
    if timeout_seconds < 1 or timeout_seconds > 300:
        return {"status": "error", "error": "timeout_seconds_out_of_range"}
    cwd = _resolve_shell_workdir(workdir)
    try:
        command = shell_command(script)
    except RuntimeError as exc:
        return {"status": "error", "error": str(exc), "shell": shell_name()}
    result = _run_subprocess_command(command, cwd=cwd, timeout_seconds=timeout_seconds)
    result["shell"] = shell_name()
    return result


@_replace_mcp_tool
def run_powershell(script: str, workdir: str | None = None, timeout_seconds: int = 60) -> dict[str, Any]:
    if current_platform() != "windows":
        return {"status": "error", "error": "powershell_windows_only", "shell": shell_name()}
    return run_shell(script=script, workdir=workdir, timeout_seconds=timeout_seconds)


@_replace_mcp_tool
def run_pytest(test_path: str | None = None, timeout_seconds: int = 120, extra_args: list[str] | None = None) -> dict[str, Any]:
    timeout_seconds = int(timeout_seconds)
    if timeout_seconds < 1 or timeout_seconds > 600:
        return {"status": "error", "error": 'timeout_seconds musi być w zakresie 1..600'}

    cwd = _resolve_shell_workdir(None)
    command = ["python", "-m", "pytest"]

    if test_path and str(test_path).strip():
        command.append(str(test_path).strip())

    if extra_args:
        command.extend(str(item) for item in extra_args)

    return _run_subprocess_command(command, cwd=cwd, timeout_seconds=timeout_seconds)


@_replace_mcp_tool
def run_python_file(path: str, args: list[str] | None = None, timeout_seconds: int = 120) -> dict[str, Any]:
    timeout_seconds = int(timeout_seconds)
    if timeout_seconds < 1 or timeout_seconds > 600:
        return {"status": "error", "error": 'timeout_seconds musi być w zakresie 1..600'}

    target = _resolve_repo_file(path)
    if target.suffix.lower() != ".py":
        return {"status": "error", "error": 'dozwolone są tylko pliki .py'}

    cwd = _resolve_shell_workdir(None)
    command = ["python", str(target)]

    if args:
        command.extend(str(item) for item in args)

    return _run_subprocess_command(command, cwd=cwd, timeout_seconds=timeout_seconds)


PROJECT_TIMELINE_KEY = "mapi"
AUTO_PROJECT_RUN_TRIGGER_RULES = {
    "sandman_v1": {
        "event_type": "project.phase_completed",
        "title": "Sandman V1 run zakończony zmianami kanonicznymi",
        "status": "completed",
        "origin": "sandman_v1_auto",
        "tags": ["timeline", "automation", "sandman"],
    },
    "sandman_ai": {
        "event_type": "project.phase_completed",
        "title": "Sandman AI run zakończony zmianami kanonicznymi",
        "status": "completed",
        "origin": "sandman_ai_auto",
        "tags": ["timeline", "automation", "sandman", "ai"],
    },
    "conflict_v1": {
        "event_type": "project.phase_completed",
        "title": "Conflict V1 run zakończony wykryciem konfliktów",
        "status": "completed",
        "origin": "conflict_v1_auto",
        "tags": ["timeline", "automation", "conflict"],
    },
    "consolidation_v1": {
        "event_type": "project.phase_completed",
        "title": "Consolidation V1 run zakończony zmianami kanonicznymi",
        "status": "completed",
        "origin": "consolidation_v1_auto",
        "tags": ["timeline", "automation", "consolidation"],
    },
}

AUTO_PROJECT_STATUS_TRIGGER_RULES = {
    "sandman_v1": {
        "event_type": "project.status_changed",
        "title": "Sandman V1 osiągnął stabilny stan operacyjny",
        "status": "stable",
        "origin": "sandman_v1_auto",
        "tags": ["timeline", "automation", "sandman", "stability"],
        "required_phase_title": "Sandman V1 run zakończony zmianami kanonicznymi",
    },
    "sandman_ai": {
        "event_type": "project.status_changed",
        "title": "Sandman AI osiągnął stabilny stan operacyjny",
        "status": "stable",
        "origin": "sandman_ai_auto",
        "tags": ["timeline", "automation", "sandman", "ai", "stability"],
        "required_phase_title": "Sandman AI run zakończony zmianami kanonicznymi",
    },
    "conflict_v1": {
        "event_type": "project.status_changed",
        "title": "Conflict V1 osiągnął stabilny stan operacyjny",
        "status": "stable",
        "origin": "conflict_v1_auto",
        "tags": ["timeline", "automation", "conflict", "stability"],
        "required_phase_title": "Conflict V1 run zakończony wykryciem konfliktów",
    },
    "consolidation_v1": {
        "event_type": "project.status_changed",
        "title": "Consolidation V1 osiągnęła stabilny stan operacyjny",
        "status": "stable",
        "origin": "consolidation_v1_auto",
        "tags": ["timeline", "automation", "consolidation", "stability"],
        "required_phase_title": "Consolidation V1 run zakończony zmianami kanonicznymi",
    },
}


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0



def _project_event_exists(conn, *, project_key: str, title: str) -> bool:
    row = conn.execute(
        "SELECT id FROM timeline_events WHERE project_key = ? AND title = ? ORDER BY id DESC LIMIT 1",
        (project_key, title),
    ).fetchone()
    return row is not None



def _normalize_valid_at(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip().replace(" ", "T")
    if not raw:
        return None
    if raw.endswith("Z") or "+" in raw[10:] or "-" in raw[10:]:
        candidate = raw
    else:
        candidate = raw + "Z"
    return _timeline.normalize_runtime_timestamp(candidate)



def _collect_run_memory_ids(conn, run_id: int, *, limit: int = 12) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT memory_id FROM sleep_run_actions WHERE run_id = ? AND memory_id IS NOT NULL ORDER BY memory_id ASC LIMIT ?",
        (int(run_id), int(limit)),
    ).fetchall()
    return [int(row["memory_id"]) for row in rows]



def _run_valid_at(conn, run_id: int) -> str | None:
    if run_id <= 0:
        return None
    run_row = conn.execute("SELECT finished_at, started_at FROM sleep_runs WHERE id = ?", (run_id,)).fetchone()
    if run_row is None:
        return None
    return _normalize_valid_at(run_row["finished_at"] or run_row["started_at"])



def _auto_project_event_payload(mode: str, result: dict[str, Any]) -> tuple[bool, str]:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    changed_count = _safe_int(summary.get("changed_count", result.get("changed_count")))
    if mode == "sandman_v1":
        archived_count = _safe_int(summary.get("archived_count"))
        duplicate_count = _safe_int(summary.get("duplicate_count"))
        downgraded_count = _safe_int(summary.get("downgraded_count"))
        evidence_boost_count = _safe_int(summary.get("canonical_evidence_boost_count"))
        is_significant = changed_count > 0 and (archived_count > 0 or duplicate_count > 0 or downgraded_count > 0 or evidence_boost_count > 0)
        description = (
            f"Pierwszy istotny run Sandman V1 zakończył się zmianami kanonicznymi. "
            f"run_id={result.get('run_id')}, changed_count={changed_count}, archived_count={archived_count}, "
            f"downgraded_count={downgraded_count}, duplicate_count={duplicate_count}, evidence_boost_count={evidence_boost_count}."
        )
        return is_significant, description
    if mode == "conflict_v1":
        conflict_count = _safe_int(summary.get("conflict_count"))
        links_created_count = _safe_int(summary.get("links_created_count"))
        flagged_memory_count = _safe_int(summary.get("flagged_memory_count"))
        is_significant = conflict_count > 0 and (links_created_count > 0 or flagged_memory_count > 0)
        description = (
            f"Pierwszy istotny run Conflict V1 zakończył się wykryciem konfliktów. "
            f"run_id={result.get('run_id')}, conflict_count={conflict_count}, links_created_count={links_created_count}, "
            f"flagged_memory_count={flagged_memory_count}."
        )
        return is_significant, description
    if mode == "consolidation_v1":
        cluster_count = _safe_int(summary.get("cluster_count"))
        support_links_created_count = _safe_int(summary.get("support_links_created_count"))
        summary_links_created_count = _safe_int(summary.get("summary_links_created_count"))
        summary_memories_created_count = _safe_int(summary.get("summary_memories_created_count"))
        central_evidence_boost_count = _safe_int(summary.get("central_evidence_boost_count"))
        is_significant = changed_count > 0 and cluster_count > 0 and (
            support_links_created_count > 0 or summary_links_created_count > 0 or summary_memories_created_count > 0 or central_evidence_boost_count > 0
        )
        description = (
            f"Pierwszy istotny run Consolidation V1 zakończył się zmianami kanonicznymi. "
            f"run_id={result.get('run_id')}, cluster_count={cluster_count}, changed_count={changed_count}, "
            f"support_links_created_count={support_links_created_count}, summary_links_created_count={summary_links_created_count}, "
            f"summary_memories_created_count={summary_memories_created_count}, central_evidence_boost_count={central_evidence_boost_count}."
        )
        return is_significant, description
    return False, ""



def _auto_project_status_payload(mode: str, result: dict[str, Any]) -> tuple[bool, str]:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    changed_count = _safe_int(summary.get("changed_count", result.get("changed_count")))
    if changed_count != 0:
        return False, ""
    if mode == "sandman_v1":
        duplicate_count = _safe_int(summary.get("duplicate_count"))
        downgraded_count = _safe_int(summary.get("downgraded_count"))
        archived_count = _safe_int(summary.get("archived_count"))
        description = (
            f"Kolejny run Sandman V1 zakończył się bez nowych zmian kanonicznych po wcześniejszym istotnym wdrożeniu. "
            f"To traktujemy jako sygnał stabilności operacyjnej. run_id={result.get('run_id')}, "
            f"changed_count={changed_count}, archived_count={archived_count}, downgraded_count={downgraded_count}, duplicate_count={duplicate_count}."
        )
        return True, description
    if mode == "conflict_v1":
        conflict_count = _safe_int(summary.get("conflict_count"))
        links_created_count = _safe_int(summary.get("links_created_count"))
        flagged_memory_count = _safe_int(summary.get("flagged_memory_count"))
        if conflict_count <= 0:
            return False, ""
        description = (
            f"Kolejny run Conflict V1 zakończył się bez nowych zmian kanonicznych, mimo że detektor nadal widzi konflikty. "
            f"To traktujemy jako sygnał stabilności operacyjnej modułu. run_id={result.get('run_id')}, "
            f"conflict_count={conflict_count}, links_created_count={links_created_count}, flagged_memory_count={flagged_memory_count}."
        )
        return True, description
    if mode == "consolidation_v1":
        cluster_count = _safe_int(summary.get("cluster_count"))
        summary_memories_created_count = _safe_int(summary.get("summary_memories_created_count"))
        summary_links_created_count = _safe_int(summary.get("summary_links_created_count"))
        if cluster_count <= 0:
            return False, ""
        description = (
            f"Kolejny run Consolidation V1 zakończył się bez nowych zmian kanonicznych po wcześniejszym istotnym wdrożeniu. "
            f"To traktujemy jako sygnał stabilności operacyjnej modułu. run_id={result.get('run_id')}, "
            f"cluster_count={cluster_count}, summary_memories_created_count={summary_memories_created_count}, summary_links_created_count={summary_links_created_count}."
        )
        return True, description
    return False, ""



def _record_auto_project_phase_event(mode: str, result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("status") != "completed":
        return result
    rule = AUTO_PROJECT_RUN_TRIGGER_RULES.get(mode)
    if rule is None:
        return result
    is_significant, description = _auto_project_event_payload(mode, result)
    if not is_significant:
        return result

    conn = _base.get_db_connection()
    try:
        if _project_event_exists(conn, project_key=PROJECT_TIMELINE_KEY, title=str(rule["title"])):
            return result
        run_id = _safe_int(result.get("run_id"))
        memory_ids = _collect_run_memory_ids(conn, run_id) if run_id > 0 else []
        valid_at = _run_valid_at(conn, run_id)
        event_id = _timeline.record_project_event(
            conn,
            project_key=PROJECT_TIMELINE_KEY,
            event_type=str(rule["event_type"]),
            title=str(rule["title"]),
            description=description,
            valid_at=valid_at,
            origin=str(rule["origin"]),
            memory_ids=memory_ids,
            run_ids=[run_id] if run_id > 0 else None,
            tags=list(rule["tags"]),
            status=str(rule["status"]),
            canonical=True,
            now_fn=_base.utc_now_iso,
        )
        conn.commit()
        result.setdefault("project_timeline_events", []).append({
            "event_id": event_id,
            "title": rule["title"],
            "project_key": PROJECT_TIMELINE_KEY,
            "mode": mode,
            "auto_recorded": True,
        })
        return result
    finally:
        conn.close()



def _record_auto_project_status_event(mode: str, result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("status") != "completed":
        return result
    rule = AUTO_PROJECT_STATUS_TRIGGER_RULES.get(mode)
    if rule is None:
        return result
    is_stable, description = _auto_project_status_payload(mode, result)
    if not is_stable:
        return result

    conn = _base.get_db_connection()
    try:
        required_phase_title = str(rule["required_phase_title"])
        if not _project_event_exists(conn, project_key=PROJECT_TIMELINE_KEY, title=required_phase_title):
            return result
        if _project_event_exists(conn, project_key=PROJECT_TIMELINE_KEY, title=str(rule["title"])):
            return result
        run_id = _safe_int(result.get("run_id"))
        memory_ids = _collect_run_memory_ids(conn, run_id) if run_id > 0 else []
        valid_at = _run_valid_at(conn, run_id)
        event_id = _timeline.record_project_event(
            conn,
            project_key=PROJECT_TIMELINE_KEY,
            event_type=str(rule["event_type"]),
            title=str(rule["title"]),
            description=description,
            valid_at=valid_at,
            origin=str(rule["origin"]),
            memory_ids=memory_ids,
            run_ids=[run_id] if run_id > 0 else None,
            tags=list(rule["tags"]),
            status=str(rule["status"]),
            canonical=True,
            now_fn=_base.utc_now_iso,
        )
        conn.commit()
        result.setdefault("project_timeline_events", []).append({
            "event_id": event_id,
            "title": rule["title"],
            "project_key": PROJECT_TIMELINE_KEY,
            "mode": mode,
            "auto_recorded": True,
        })
        return result
    finally:
        conn.close()



def _apply_auto_project_timeline_triggers(mode: str, result: dict[str, Any]) -> dict[str, Any]:
    result = _record_auto_project_phase_event(mode, result)
    result = _record_auto_project_status_event(mode, result)
    return result


_EXISTING_RUN_CONSOLIDATION_V1 = _run_consolidation_v1_impl


@_replace_mcp_tool
def run_conflicts_v1(notes: str | None = None) -> dict[str, Any]:
    result = _base.run_conflicts_v1(notes=notes)
    return _apply_auto_project_timeline_triggers("conflict_v1", result)


@_replace_mcp_tool
def run_sandman_v1(freedom_level: int = 1, notes: str | None = None, workspace_key: str | None = None, project_key: str | None = None) -> dict[str, Any]:
    result = _base.run_sandman_v1(freedom_level=freedom_level, notes=notes, workspace_key=workspace_key, project_key=project_key)
    return _apply_auto_project_timeline_triggers("sandman_v1", result)


@_replace_mcp_tool
def run_sandman_ai(freedom_level: int = 1, notes: str | None = None) -> dict[str, Any]:
    result = _base.run_sandman_ai(freedom_level=freedom_level, notes=notes)
    return _apply_auto_project_timeline_triggers("sandman_ai", result)


@_replace_mcp_tool
def run_consolidation_v1(notes: str | None = None) -> dict[str, Any]:
    result = _EXISTING_RUN_CONSOLIDATION_V1(notes=notes)
    return _apply_auto_project_timeline_triggers("consolidation_v1", result)




# Runtime sync patch for unittest-style DB overrides.
def get_db_connection():
    _sync_base_config()
    return _base.store.get_db_connection()


def install_runtime_overrides() -> None:
    """Reapply compatibility bindings after dynamic server facade imports."""
    _base._sync_config = _sync_base_config
    _base.get_db_connection = get_db_connection
    _base._insert_memory = _insert_memory
    _base._create_link = _create_link
    _base.create_sleep_run = create_sleep_run
    _base.add_sleep_action = add_sleep_action
    _base.finalize_sleep_run = finalize_sleep_run
    _base.recall_memory = recall_memory


install_runtime_overrides()




@_replace_mcp_tool
def preview_memory_backfill_v1(limit_review: int = 20, only_missing: bool = True) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        result = backfill_logic.apply_backfill_plan(conn, only_missing=only_missing, dry_run=True)
    finally:
        conn.close()
    result["review_items"] = result.get("review_items", [])[: max(int(limit_review), 0)]
    return result


@_replace_mcp_tool
def run_memory_backfill_v1(limit_review: int = 20, only_missing: bool = True) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        result = backfill_logic.apply_backfill_plan(conn, only_missing=only_missing, dry_run=False)
    finally:
        conn.close()
    result["review_items"] = result.get("review_items", [])[: max(int(limit_review), 0)]
    result["layer_report"] = get_memory_layer_report()
    return result


@_replace_mcp_tool
def get_memory_layer_report(top_n: int = 5) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return backfill_logic.get_layer_report(conn, top_n=max(int(top_n), 1))
    finally:
        conn.close()

# Replace core bindings with the final server.py wrappers and verify completeness.
bind_workshop_handlers(sys.modules[__name__], replace=True, strict=False, local_only=True)

def _apply_startup_migrations() -> None:
    """Apply pending schema migrations before runtime metadata is frozen."""
    _sync_base_config()
    conn = _base.get_db_connection()
    try:
        pass
    finally:
        conn.close()


def run_server() -> None:
    """Start the MAPI HTTP runtime with optional private remote authentication."""
    from app.runtime.freshness import initialize_runtime_metadata
    from app.runtime.remote_auth import configure_remote_auth
    from app.runtime.remote_auth_config import RemoteAuthConfig
    from app.runtime.writer_guard import configure_writer_guard
    from app.runtime.backpressure import McpBackpressureMiddleware, configure_backpressure_from_env
    from starlette.middleware import Middleware

    host = os.environ.get("MAPI_RUNTIME_HOST", "127.0.0.1")
    port = int(os.environ.get("MAPI_RUNTIME_PORT", "8015"))
    remote_config = RemoteAuthConfig.from_env()
    host_errors = remote_config.validate_runtime_host(host)
    if host_errors:
        raise RuntimeError("remote_auth_runtime_invalid:" + ",".join(host_errors))
    configure_writer_guard(db_path=DB_PATH)
    _apply_startup_migrations()
    configure_remote_auth(mcp, db_path=DB_PATH, config=remote_config)
    backpressure = configure_backpressure_from_env()
    initialize_runtime_metadata(force=True)
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    mcp.run(
        transport="http",
        host=host,
        port=port,
        path="/mcp/",
        stateless_http=False,
        middleware=[Middleware(McpBackpressureMiddleware, state=backpressure)],
        uvicorn_config={"timeout_keep_alive": int(backpressure.keepalive_seconds)},
    )
