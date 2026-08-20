from __future__ import annotations

"""Unattended, operator-only memory maintenance for VPS MAPI instances.

The scheduled path auto-applies deterministic reversible metadata hygiene and
unambiguous structural repairs after a verified SQLite backup. Ambiguous semantic
branches are queued for the connected assistant model and, when necessary, user
consent. Content is never deleted by scheduled maintenance.
"""

import argparse
try:
    import fcntl
except ImportError:  # Windows import safety; scheduled maintenance is Linux/systemd only.
    fcntl = None  # type: ignore[assignment]
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from mapi.env import parse_environment_file

MAINTENANCE_SCHEMA = "mapi_memory_maintenance.v1"
REPORT_RETENTION_DAYS = 30
BACKUP_RETENTION_DAYS = 14


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(now: datetime | None = None) -> str:
    return (now or _utc_now()).strftime("%Y%m%dT%H%M%SZ")


def _json_default(value: Any) -> str:
    return str(value)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )
    os.chmod(temp, 0o600)
    temp.replace(path)


def _load_runtime(root: Path) -> dict[str, str]:
    root = root.expanduser().resolve()
    env_file = root / ".env"
    if not env_file.is_file():
        raise RuntimeError("maintenance_runtime_env_missing")
    values = parse_environment_file(env_file)
    required = ("MAPI_DATA_DIR", "MAPI_DB_PATH", "MAPI_BACKUP_DIR", "MAPI_LOG_DIR")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise RuntimeError("maintenance_runtime_env_incomplete:" + ",".join(missing))
    for key, value in values.items():
        os.environ.setdefault(key, value)
    os.environ["MAPI_ROOT"] = str(root)

    from app.runtime.context import configure_runtime_context

    configure_runtime_context(
        root=root,
        data_dir=Path(values["MAPI_DATA_DIR"]).expanduser().resolve(),
        db_path=Path(values["MAPI_DB_PATH"]).expanduser().resolve(),
    )
    return values


@contextmanager
def _exclusive_lock(root: Path) -> Iterator[None]:
    lock_path = root / "generated" / "memory-maintenance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is None:
            yield
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("maintenance_already_running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _verified_backup(db_path: Path, backup_dir: Path, *, stamp: str) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    final = backup_dir / f"mapi-maintenance-{stamp}.db"
    temp = backup_dir / f".mapi-maintenance-{stamp}.tmp.db"
    if temp.exists():
        temp.unlink()
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(temp)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    check = sqlite3.connect(temp)
    try:
        quick = str(check.execute("PRAGMA quick_check").fetchone()[0])
        foreign_keys = check.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        check.close()
    if quick.casefold() != "ok" or foreign_keys:
        temp.unlink(missing_ok=True)
        raise RuntimeError("maintenance_backup_verification_failed")
    os.chmod(temp, 0o600)
    temp.replace(final)
    return {
        "status": "verified",
        "path": str(final.resolve()),
        "quick_check": quick,
        "foreign_key_issue_count": len(foreign_keys),
    }


def _cleanup_owned_files(directory: Path, pattern: str, *, older_than_days: int, now: datetime) -> int:
    if not directory.exists():
        return 0
    cutoff = now - timedelta(days=max(1, int(older_than_days)))
    removed = 0
    for path in directory.glob(pattern):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if modified < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def _project_keys(server_core: Any) -> list[str]:
    conn = server_core.get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT project_key
            FROM memories
            WHERE project_key IS NOT NULL AND TRIM(project_key) <> ''
            ORDER BY project_key
            """
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        conn.close()


def _safe_call(label: str, fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result = fn(*args, **kwargs)
        if isinstance(result, dict) and str(result.get("status") or "").casefold() in {"error", "failed", "blocked"}:
            return {
                "status": "error",
                "error": f"{label}:reported_{result.get('status')}",
                "result": result,
            }
        return {"status": "ok", "result": result}
    except Exception as exc:  # maintenance report must survive one diagnostic failure
        return {"status": "error", "error": f"{label}:{type(exc).__name__}:{exc}"}


def _structural_diagnostics(server_core: Any) -> dict[str, Any]:
    current = _safe_call(
        "current_state_inventory",
        server_core.get_memory_current_state_inventory,
        project_key=None,
        limit=500,
        include_debug=False,
    )
    lifecycle = _safe_call(
        "lifecycle_integrity",
        server_core.get_memory_lifecycle_integrity_report,
        memory_id=None,
        project_key=None,
        scope_code=None,
        include_archived=True,
        sample_limit=100,
        include_debug=False,
    )
    health = _safe_call(
        "health_report",
        server_core.get_memory_health_report,
        project_key=None,
        limit=50,
        include_debug=False,
        include_consolidation_snapshot_integrity=True,
        snapshot_integrity_sample_limit=10,
    )

    critical = 0
    if current["status"] == "ok":
        critical += int((current["result"].get("summary") or {}).get("critical_issue_count") or 0)
    if lifecycle["status"] == "ok":
        critical += int((lifecycle["result"].get("summary") or {}).get("critical_issues") or 0)
    diagnostic_errors = sum(1 for item in (current, lifecycle, health) if item["status"] != "ok")
    return {
        "current_state_inventory": current,
        "lifecycle_integrity": lifecycle,
        "health_report": health,
        "critical_issue_count": critical,
        "diagnostic_error_count": diagnostic_errors,
        "auto_apply_blocked": critical > 0 or diagnostic_errors > 0,
    }


def _pending_review_counts(server_core: Any) -> dict[str, int]:
    conn = server_core.get_db_connection()
    try:
        queries = {
            "capture_pending": "SELECT COUNT(*) FROM memory_capture_review_items WHERE status='pending'",
            "retention_pending": "SELECT COUNT(*) FROM memory_retention_review_items WHERE status='pending'",
            "consolidation_pending": "SELECT COUNT(*) FROM memory_consolidation_review_items WHERE status='pending'",
        }
        result: dict[str, int] = {}
        for key, query in queries.items():
            try:
                result[key] = int(conn.execute(query).fetchone()[0])
            except sqlite3.Error:
                result[key] = 0
        return result
    finally:
        conn.close()


def run_maintenance(
    *,
    root: str | Path,
    apply_safe_metadata: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    started = now or _utc_now()
    root_path = Path(root).expanduser().resolve()
    values = _load_runtime(root_path)
    db_path = Path(values["MAPI_DB_PATH"]).expanduser().resolve()
    backup_dir = Path(values["MAPI_BACKUP_DIR"]).expanduser().resolve()
    log_dir = Path(values["MAPI_LOG_DIR"]).expanduser().resolve() / "maintenance"
    stamp = _stamp(started)

    with _exclusive_lock(root_path):
        import server_core
        from mapi_core.memory import hygiene as memory_hygiene
        from mapi_core.memory import self_healing

        structural_before = _structural_diagnostics(server_core)

        queue_conn = server_core.get_db_connection()
        try:
            queue_scan_before = self_healing.scan_self_healing_issues(queue_conn)
            queue_conn.commit()
        finally:
            queue_conn.close()

        self_healing_before_conn = server_core.get_db_connection()
        try:
            self_healing_before = self_healing.get_self_healing_status(self_healing_before_conn)
            repairable_count = int(
                self_healing_before_conn.execute(
                    "SELECT COUNT(*) FROM memory_self_healing_issues "
                    "WHERE status='open' AND repair_class IN ('low','medium')"
                ).fetchone()[0]
            )
        finally:
            self_healing_before_conn.close()

        backup: dict[str, Any] | None = None
        structural_repair: dict[str, Any] = {
            "status": "not_needed", "repaired_count": 0, "blocked_count": 0
        }
        if repairable_count > 0:
            backup = _verified_backup(db_path, backup_dir, stamp=stamp)
            repair_conn = server_core.get_db_connection()
            try:
                repair_conn.execute("BEGIN IMMEDIATE")
                structural_repair = self_healing.repair_deterministic_issues(
                    repair_conn, insert_event=server_core.insert_memory_event
                )
                repair_conn.commit()
            except Exception:
                repair_conn.rollback()
                raise
            finally:
                repair_conn.close()

        queue_conn = server_core.get_db_connection()
        try:
            queue_scan_after = self_healing.scan_self_healing_issues(queue_conn)
            self_healing_after = self_healing.get_self_healing_status(queue_conn)
            queue_conn.commit()
        finally:
            queue_conn.close()

        structural = _structural_diagnostics(server_core)
        projects = _project_keys(server_core)
        previews: list[dict[str, Any]] = []
        for project_key in projects:
            conn = server_core.get_db_connection()
            try:
                preview = memory_hygiene.build_hygiene_preview(
                    conn,
                    project_key=project_key,
                    as_of=started.isoformat().replace("+00:00", "Z"),
                )
            finally:
                conn.close()
            previews.append(
                {
                    "project_key": project_key,
                    "status": preview.get("status"),
                    "apply_allowed": bool(preview.get("apply_allowed")),
                    "candidate_count": int(preview.get("candidate_count") or 0),
                    "preview_hash": preview.get("preview_hash"),
                    "field_change_counts": preview.get("field_change_counts") or {},
                    "sentinel_findings": preview.get("sentinel_findings") or [],
                }
            )

        candidate_total = sum(item["candidate_count"] for item in previews)
        apply_results: list[dict[str, Any]] = []
        auto_apply_blocked = bool(structural["auto_apply_blocked"])

        if apply_safe_metadata and candidate_total > 0 and not auto_apply_blocked:
            if backup is None:
                backup = _verified_backup(db_path, backup_dir, stamp=stamp)
            for item in previews:
                if item["candidate_count"] <= 0 or item["status"] != "preview_ready" or not item["apply_allowed"]:
                    continue
                conn = server_core.get_db_connection()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    result = memory_hygiene.apply_hygiene_preview(
                        conn,
                        project_key=item["project_key"],
                        expected_preview_hash=str(item["preview_hash"]),
                        applied_by="polaris-maintenance",
                        reason="scheduled deterministic metadata hygiene",
                        backup_path=str(backup["path"]),
                        confirm_metadata_repair=True,
                        as_of=started.isoformat().replace("+00:00", "Z"),
                    )
                    conn.commit()
                    apply_results.append({"project_key": item["project_key"], **result})
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()

        review_counts = _pending_review_counts(server_core)
        completed = _utc_now()
        report = {
            "schema": MAINTENANCE_SCHEMA,
            "status": "attention" if structural["critical_issue_count"] or structural["diagnostic_error_count"] else "ok",
            "started_at": started.isoformat().replace("+00:00", "Z"),
            "completed_at": completed.isoformat().replace("+00:00", "Z"),
            "root": str(root_path),
            "database": str(db_path),
            "policy": {
                "auto_apply_safe_metadata": bool(apply_safe_metadata),
                "auto_apply_unambiguous_lineage_repairs": True,
                "auto_apply_content": False,
                "auto_delete_content": False,
                "ambiguous_semantic_repairs": "model_then_user_consent",
                "auto_apply_retention": False,
                "auto_apply_consolidation": False,
            },
            "projects": projects,
            "structural_before": structural_before,
            "structural": structural,
            "self_healing": {
                "scan_before": queue_scan_before,
                "status_before": self_healing_before,
                "deterministic_repair": structural_repair,
                "scan_after": queue_scan_after,
                "status_after": self_healing_after,
            },
            "metadata_hygiene": {
                "preview_count": len(previews),
                "candidate_total": candidate_total,
                "previews": previews,
                "backup": backup,
                "apply_results": apply_results,
                "auto_apply_blocked": auto_apply_blocked,
            },
            "review_queues": review_counts,
        }
        report_path = log_dir / f"maintenance-{stamp}.json"
        _write_json(report_path, report)
        _write_json(log_dir / "latest.json", report)
        report["report_path"] = str(report_path)
        report["cleanup"] = {
            "reports_removed": _cleanup_owned_files(
                log_dir,
                "maintenance-*.json",
                older_than_days=REPORT_RETENTION_DAYS,
                now=completed,
            ),
            "backups_removed": _cleanup_owned_files(
                backup_dir,
                "mapi-maintenance-*.db",
                older_than_days=BACKUP_RETENTION_DAYS,
                now=completed,
            ),
        }
        return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run unattended safe MAPI memory maintenance")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--apply-safe-metadata", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_maintenance(root=args.root, apply_safe_metadata=bool(args.apply_safe_metadata))
    except Exception as exc:
        payload = {
            "schema": MAINTENANCE_SCHEMA,
            "status": "failed",
            "error": f"{type(exc).__name__}:{exc}",
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True) if args.json else payload)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=_json_default) if args.json else result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())