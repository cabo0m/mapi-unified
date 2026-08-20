from __future__ import annotations

"""Canonical proposal-only Sandman scheduler.

One provider path: deterministic local core plus optional stateless Gemini shadow.
This module never mutates memories, links, queues or lifecycle state.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from app import db_migrations
from app.sandman import router, shadow
from mapi_core.sandman.contracts import (
    EXTERNAL_DATA_POLICY,
    PROVIDER_REQUEST_SCHEMA_VERSION,
    PROVIDER_RESPONSE_SCHEMA_VERSION,
    PROVIDER_VALIDATION_SCHEMA_VERSION,
    REDACTION_POLICY_VERSION,
)
from mapi_core.sandman.providers.gemini import (
    GeminiConfig,
    GeminiShadowProvider,
    GoogleGenAIInteractionsTransport,
)

SCHEDULER_SCHEMA = "sandman_canonical_scheduler.v1"
RUN_SCHEMA = "sandman_canonical_run.v1"
STATUS_SCHEMA = "sandman_canonical_status.v1"
MORNING_REPORT_SCHEMA = "sandman_morning_report.v1"
PROVIDER_PATH = "deterministic_core+gemini_shadow"
PROMPT_VERSION = "sandman_provider_prompt.v2"
SCHEDULER_NAME = "windows_task_scheduler"
SCHEDULER_TIMEZONE = "Europe/Warsaw"
NIGHTLY_HOUR = 22
MORNING_HOUR = 9
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_CANDIDATE_LIMIT = 12
DEFAULT_PROPOSAL_BUDGET = 3
CANONICAL_FLAG = "sandman_canonical_scheduler_enabled"
PROVIDER_FLAG = "sandman_provider_v3_enabled"
SHADOW_FLAG = "sandman_gemini_shadow_enabled"
ROUTING_FLAG = "sandman_model_queue_routing_enabled"
LEGACY_GEMMA_FLAG = "sandman_gemma_hygiene_enabled"
DEFAULT_ALLOWED_ACTIONS = (
    "contradicts",
    "duplicate_of",
    "reinforces",
    "related_to",
    "supersedes",
)
TERMINAL_STATUSES = frozenset(
    {"completed", "no_op", "partial", "blocked", "failed", "missed", "timed_out"}
)
GOOD_STATUSES = frozenset({"completed", "no_op", "partial"})


@dataclass(frozen=True)
class CanonicalPaths:
    root: Path
    data_dir: Path
    db_path: Path
    base_dir: Path
    reports_dir: Path
    logs_dir: Path
    lock_path: Path

    @classmethod
    def for_root(cls, root_path: str | os.PathLike[str] | Path | None = None) -> "CanonicalPaths":
        root = Path(root_path or Path.cwd()).resolve()
        data_dir = root / "data"
        base_dir = data_dir / "sandman" / "canonical"
        return cls(
            root=root,
            data_dir=data_dir,
            db_path=data_dir / "agent_memory.db",
            base_dir=base_dir,
            reports_dir=base_dir / "reports",
            logs_dir=base_dir / "logs",
            lock_path=base_dir / "sandman-canonical.lock",
        )

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.base_dir, self.reports_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _local_now(value: datetime | None = None) -> datetime:
    source = value or _utc_now()
    if source.tzinfo is None:
        source = source.replace(tzinfo=UTC)
    return source.astimezone(ZoneInfo(SCHEDULER_TIMEZONE))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _connect(db_path: Path, *, migrate: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if migrate:
        db_migrations.apply_all_migrations(conn)
    return conn


def ensure_canonical_schema(root_path: str | os.PathLike[str] | Path | None = None) -> CanonicalPaths:
    paths = CanonicalPaths.for_root(root_path)
    paths.ensure_dirs()
    conn = _connect(paths.db_path, migrate=True)
    conn.commit()
    conn.close()
    return paths


def _csv(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _flag(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM feature_flags WHERE flag_key = ?", (key,)).fetchone()
    if row is None:
        return {
            "flag_key": key,
            "is_enabled": 0,
            "rollout_mode": "off",
            "allowed_project_keys": None,
            "allowed_scope_codes": None,
            "read_only_mode": 0,
            "is_implicit_default": True,
        }
    item = dict(row)
    item["is_implicit_default"] = False
    return item


def _evaluate_flag(flag: Mapping[str, Any], *, project_key: str, scope_code: str) -> dict[str, Any]:
    rollout = str(flag.get("rollout_mode") or "off").strip().lower()
    is_enabled = bool(int(flag.get("is_enabled") or 0))
    projects = _csv(flag.get("allowed_project_keys"))
    scopes = _csv(flag.get("allowed_scope_codes"))
    project_match = rollout in {"all", "off", "scopes"} or project_key in projects
    scope_match = rollout in {"all", "off", "projects"} or scope_code in scopes
    if not is_enabled:
        enabled, reason = False, "flag_disabled"
    elif rollout == "off":
        enabled, reason = False, "rollout_off"
    elif rollout == "all":
        enabled, reason = True, "rollout_all"
    elif rollout == "projects":
        enabled, reason = project_match, "project_allowed" if project_match else "project_not_allowed"
    elif rollout == "scopes":
        enabled, reason = scope_match, "scope_allowed" if scope_match else "scope_not_allowed"
    else:
        enabled = project_match and scope_match
        reason = "project_and_scope_allowed" if enabled else "project_or_scope_not_allowed"
    return {
        "flag_key": str(flag["flag_key"]),
        "enabled": enabled,
        "read_only_mode": bool(int(flag.get("read_only_mode") or 0)),
        "reason": reason,
        "project_key": project_key,
        "scope_code": scope_code,
        "rollout_mode": rollout,
        "allowed_project_keys": projects,
        "allowed_scope_codes": scopes,
        "is_implicit_default": bool(flag.get("is_implicit_default")),
    }


def _process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return False
        output = (completed.stdout or "").lower()
        return str(pid) in output and "no tasks" not in output
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def inspect_lock(paths: CanonicalPaths) -> dict[str, Any]:
    lock = _read_json(paths.lock_path)
    if not lock:
        return {"exists": False, "active": False, "stale": False, "lock": None}
    active = _process_running(int(lock.get("pid") or 0))
    return {"exists": True, "active": active, "stale": not active, "lock": lock}


def acquire_lock(paths: CanonicalPaths, *, run_key: str, run_type: str, timeout_seconds: int) -> dict[str, Any]:
    paths.ensure_dirs()
    state = inspect_lock(paths)
    warnings: list[str] = []
    if state["active"]:
        return {"acquired": False, "reason": "active_lock", "lock": state["lock"], "warnings": []}
    if state["stale"]:
        try:
            paths.lock_path.unlink()
            warnings.append("stale_lock_removed")
        except OSError:
            return {"acquired": False, "reason": "stale_lock_remove_failed", "lock": state["lock"], "warnings": []}
    payload = {
        "schema": SCHEDULER_SCHEMA,
        "run_key": run_key,
        "run_type": run_type,
        "pid": os.getpid(),
        "started_at": _utc_iso(),
        "timeout_seconds": int(timeout_seconds),
    }
    try:
        descriptor = os.open(paths.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return {"acquired": False, "reason": "active_lock", "lock": _read_json(paths.lock_path), "warnings": warnings}
    try:
        os.write(descriptor, (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
    return {"acquired": True, "reason": "lock_acquired", "lock": payload, "warnings": warnings}


def release_lock(paths: CanonicalPaths, *, run_key: str) -> None:
    lock = _read_json(paths.lock_path)
    if lock and lock.get("run_key") == run_key:
        paths.lock_path.unlink(missing_ok=True)


def _select_candidate_ids(conn: sqlite3.Connection, *, project_key: str, scope_code: str, limit: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT id, workspace_id, COALESCE(updated_at, created_at, '') AS freshness,
               COALESCE(importance_score, 0) AS importance
        FROM memories
        WHERE project_key = ? AND scope_code = ? AND archived_at IS NULL
          AND COALESCE(activity_state, 'active') = 'active'
          AND COALESCE(state_code, 'active') NOT IN ('archived','expired','rejected','cancelled','superseded')
          AND COALESCE(memory_v2_status, 'active') NOT IN ('archived','expired','rejected','cancelled','superseded')
        ORDER BY freshness DESC, importance DESC, id DESC
        LIMIT 100
        """,
        (project_key, scope_code),
    ).fetchall()
    groups: dict[int | None, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["workspace_id"], []).append(row)
    if not groups:
        return []
    selected = max(groups.values(), key=lambda group: (len(group), max(int(row["id"]) for row in group)))
    return sorted(int(row["id"]) for row in selected[: max(1, min(int(limit), 20))])


def _proposal_counts(proposals: list[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for proposal in proposals:
        action = str(proposal.get("action") or "unknown")
        counts[action] = counts.get(action, 0) + 1
    return dict(sorted(counts.items()))


def _safe_shadow_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "shadow_run_id": result.get("shadow_run_id"),
        "model_name": result.get("model_name"),
        "model_role": result.get("model_role"),
        "validation_status": result.get("validation_status"),
        "reason_codes": list(result.get("validation_reason_codes") or result.get("reason_codes") or []),
        "abstain": result.get("abstain"),
        "proposal_counts": dict(result.get("proposal_counts") or {}),
        "latency_ms": result.get("latency_ms"),
        "usage": dict(result.get("usage") or {}),
        "estimated_cost_usd": result.get("estimated_cost_usd"),
        "retry_count": result.get("retry_count"),
        "safety": dict(result.get("safety") or {}),
    }


def _build_preview_internal(
    conn: sqlite3.Connection,
    *,
    project_key: str,
    scope_code: str,
    candidate_limit: int,
    proposal_budget: int,
    allowed_actions: list[str],
    memory_ids: list[int] | None,
    include_debug: bool,
) -> dict[str, Any]:
    canonical_flag = _flag(conn, CANONICAL_FLAG)
    canonical_eval = _evaluate_flag(canonical_flag, project_key=project_key, scope_code=scope_code)
    provider_flag = _flag(conn, PROVIDER_FLAG)
    provider_eval = _evaluate_flag(provider_flag, project_key=project_key, scope_code=scope_code)
    shadow_flag = _flag(conn, SHADOW_FLAG)
    shadow_eval = _evaluate_flag(shadow_flag, project_key=project_key, scope_code=scope_code)
    ids = sorted(set(memory_ids or _select_candidate_ids(conn, project_key=project_key, scope_code=scope_code, limit=candidate_limit)))
    base = {
        "schema_version": RUN_SCHEMA,
        "provider_path": PROVIDER_PATH,
        "project_key": project_key,
        "scope_code": scope_code,
        "candidate_memory_ids": ids,
        "candidate_count": len(ids),
        "allowed_actions": sorted(set(allowed_actions)),
        "proposal_budget": int(proposal_budget),
        "flag_evaluations": {CANONICAL_FLAG: canonical_eval, PROVIDER_FLAG: provider_eval, SHADOW_FLAG: shadow_eval},
        "auto_apply": False,
        "memory_writes": 0,
        "queue_writes": 0,
    }
    if not canonical_eval["enabled"]:
        return {**base, "status": "feature_disabled", "reason_codes": [canonical_eval["reason"]]}
    if not ids:
        return {**base, "status": "no_candidates", "reason_codes": []}
    deterministic = router.preview_deterministic_provider_payload(
        conn,
        project_key=project_key,
        scope_code=scope_code,
        memory_ids_json=ids,
        allowed_actions_json=allowed_actions,
        provider_name="deterministic",
        proposal_budget=int(proposal_budget),
        include_debug=include_debug,
        feature_flag=provider_flag,
        feature_flag_evaluation=provider_eval,
    )
    if deterministic.get("status") != "preview_completed":
        return {**base, "status": "deterministic_blocked", "reason_codes": list(deterministic.get("reason_codes") or [str(deterministic.get("status"))]), "deterministic": deterministic}
    gemini_request = router.preview_provider_request_payload(
        conn,
        project_key=project_key,
        scope_code=scope_code,
        memory_ids_json=ids,
        allowed_actions_json=allowed_actions,
        provider_name="gemini",
        proposal_budget=int(proposal_budget),
        include_debug=include_debug,
        feature_flag=provider_flag,
        feature_flag_evaluation=provider_eval,
    )
    config = GeminiConfig.from_env()
    shadow_preview = shadow.preview_shadow(
        request_preview=gemini_request,
        provider_evaluation=provider_eval,
        shadow_evaluation=shadow_eval,
        config=config,
        model_role="primary",
        include_debug=include_debug,
    )
    proposals = list(deterministic.get("proposals") or [])
    return {
        **base,
        "status": "preview_ready",
        "input_fingerprint": (gemini_request.get("request") or {}).get("input_fingerprint"),
        "redaction_manifest": gemini_request.get("redaction_manifest"),
        "deterministic": {
            "status": deterministic.get("status"),
            "proposal_count": len(proposals),
            "proposal_counts": _proposal_counts(proposals),
            "abstain": bool(deterministic.get("abstain")),
            "validation_status": (deterministic.get("validation") or {}).get("status"),
        },
        "shadow_preview": shadow.public_preview(shadow_preview),
        "_shadow_preview": shadow_preview,
        "_gemini_config": config,
    }


def preview_canonical(
    *,
    root_path: str | os.PathLike[str] | Path | None = None,
    project_key: str = "demo-project",
    scope_code: str = "project",
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    proposal_budget: int = DEFAULT_PROPOSAL_BUDGET,
    allowed_actions: list[str] | None = None,
    memory_ids: list[int] | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    paths = ensure_canonical_schema(root_path)
    conn = _connect(paths.db_path)
    try:
        result = _build_preview_internal(
            conn,
            project_key=project_key,
            scope_code=scope_code,
            candidate_limit=candidate_limit,
            proposal_budget=proposal_budget,
            allowed_actions=list(allowed_actions or DEFAULT_ALLOWED_ACTIONS),
            memory_ids=memory_ids,
            include_debug=include_debug,
        )
    finally:
        conn.close()
    return {key: value for key, value in result.items() if not key.startswith("_")}


# === CANONICAL_RUNTIME_PART_2 ===


def _decode_json_field(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else default
    except json.JSONDecodeError:
        return default


def _row_payload(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    mappings = {
        "candidate_memory_ids_json": ("candidate_memory_ids", []),
        "allowed_actions_json": ("allowed_actions", []),
        "result_summary_json": ("result_summary", {}),
        "reason_codes_json": ("reason_codes", []),
    }
    for source, (target, default) in mappings.items():
        item[target] = _decode_json_field(item.pop(source, None), default)
    item["auto_apply"] = bool(item.get("auto_apply"))
    return item


def list_canonical_runs(
    *,
    root_path: str | os.PathLike[str] | Path | None = None,
    run_type: str | None = None,
    status: str | None = None,
    project_key: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    paths = ensure_canonical_schema(root_path)
    conn = _connect(paths.db_path)
    try:
        where: list[str] = []
        params: list[Any] = []
        for column, value in (("run_type", run_type), ("status", status), ("project_key", project_key)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = conn.execute(
            f"SELECT * FROM sandman_scheduler_runs{clause} ORDER BY id DESC LIMIT ?",
            (*params, max(1, min(int(limit), 200))),
        ).fetchall()
    finally:
        conn.close()
    items = [_row_payload(row) for row in rows]
    return {"status": "ok", "count": len(items), "items": items}


def get_canonical_run(
    run_id: int,
    *,
    root_path: str | os.PathLike[str] | Path | None = None,
) -> dict[str, Any]:
    paths = ensure_canonical_schema(root_path)
    conn = _connect(paths.db_path)
    try:
        row = conn.execute("SELECT * FROM sandman_scheduler_runs WHERE id = ?", (int(run_id),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"status": "not_found", "run_id": int(run_id)}
    return {"status": "ok", "run": _row_payload(row)}


def _existing_by_key(conn: sqlite3.Connection, run_key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sandman_scheduler_runs WHERE run_key = ?", (run_key,)).fetchone()
    return _row_payload(row)


def _create_run(
    conn: sqlite3.Connection,
    *,
    run_key: str,
    run_type: str,
    project_key: str,
    scope_code: str,
    timeout_seconds: int,
    source_run_id: int | None = None,
) -> int:
    now = _utc_iso()
    cursor = conn.execute(
        """
        INSERT INTO sandman_scheduler_runs (
            run_key, run_type, scheduler_name, scheduler_timezone, status,
            project_key, scope_code, provider_path, deterministic_provider,
            shadow_provider, prompt_version, request_schema_version,
            response_schema_version, validation_schema_version,
            redaction_policy_version, external_data_policy, changed_count,
            auto_apply, network_calls, timeout_seconds, source_run_id,
            started_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'planned', ?, ?, ?, 'deterministic', 'gemini', ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?)
        """,
        (
            run_key,
            run_type,
            SCHEDULER_NAME,
            SCHEDULER_TIMEZONE,
            project_key,
            scope_code,
            PROVIDER_PATH,
            PROMPT_VERSION,
            PROVIDER_REQUEST_SCHEMA_VERSION,
            PROVIDER_RESPONSE_SCHEMA_VERSION,
            PROVIDER_VALIDATION_SCHEMA_VERSION,
            REDACTION_POLICY_VERSION,
            EXTERNAL_DATA_POLICY,
            int(timeout_seconds),
            source_run_id,
            now,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _update_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    fields: Mapping[str, Any] | None = None,
) -> None:
    values = dict(fields or {})
    values["status"] = status
    values["updated_at"] = _utc_iso()
    if status in TERMINAL_STATUSES:
        values.setdefault("finished_at", _utc_iso())
    json_fields = {
        "candidate_memory_ids_json",
        "allowed_actions_json",
        "result_summary_json",
        "reason_codes_json",
    }
    assignments: list[str] = []
    params: list[Any] = []
    for key, value in values.items():
        assignments.append(f"{key} = ?")
        params.append(_json(value) if key in json_fields else value)
    conn.execute(
        f"UPDATE sandman_scheduler_runs SET {', '.join(assignments)} WHERE id = ?",
        (*params, int(run_id)),
    )
    conn.commit()


def _build_provider(config: GeminiConfig) -> GeminiShadowProvider:
    return GeminiShadowProvider(
        config=config,
        transport=GoogleGenAIInteractionsTransport(
            api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
            timeout_seconds=config.timeout_seconds,
        ),
    )


def _nightly_report_markdown(run: Mapping[str, Any]) -> str:
    summary = dict(run.get("result_summary") or {})
    return (
        "# Sandman Canonical Nightly Report\n\n"
        f"- Run id: {run.get('id')}\n"
        f"- Run key: {run.get('run_key')}\n"
        f"- Status: {run.get('status')}\n"
        f"- Project: {run.get('project_key')}\n"
        f"- Provider path: {run.get('provider_path')}\n"
        f"- Candidates: {run.get('candidate_count') or 0}\n"
        f"- Deterministic proposals: {run.get('deterministic_proposal_count') or 0}\n"
        f"- Gemini shadow: {run.get('shadow_status') or 'not_run'}\n"
        f"- Gemini validation: {run.get('shadow_validation_status') or 'not_run'}\n"
        f"- Network calls: {run.get('network_calls') or 0}\n"
        f"- Estimated cost USD: {run.get('estimated_cost_usd') or 0}\n"
        "- Memory writes: 0\n"
        "- Queue writes: 0\n"
        "- Auto-apply: false\n\n"
        f"Reason codes: {', '.join(run.get('reason_codes') or []) or 'none'}\n\n"
        f"Summary: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}\n"
    )


def run_canonical(
    *,
    root_path: str | os.PathLike[str] | Path | None = None,
    project_key: str = "demo-project",
    scope_code: str = "project",
    run_type: str = "nightly_preview",
    run_key: str | None = None,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    proposal_budget: int = DEFAULT_PROPOSAL_BUDGET,
    allowed_actions: list[str] | None = None,
    memory_ids: list[int] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    requested_by: str = "scheduler",
    execute_shadow: bool = True,
    provider_factory: Callable[[GeminiConfig], GeminiShadowProvider] | None = None,
    now: datetime | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    paths = ensure_canonical_schema(root_path)
    local = _local_now(now)
    normalized_key = run_key or f"{run_type}:{project_key}:{local.date().isoformat()}"
    conn = _connect(paths.db_path)
    try:
        existing = _existing_by_key(conn, normalized_key)
    finally:
        conn.close()
    if existing is not None:
        return {"status": "existing_result", "run": existing}

    lock = acquire_lock(paths, run_key=normalized_key, run_type=run_type, timeout_seconds=timeout_seconds)
    if not lock["acquired"]:
        return {
            "status": "already_running",
            "run_key": normalized_key,
            "reason_codes": [str(lock["reason"])],
            "active_lock": lock.get("lock"),
            "memory_writes": 0,
            "queue_writes": 0,
            "auto_apply": False,
        }

    run_id: int | None = None
    try:
        conn = _connect(paths.db_path)
        try:
            try:
                run_id = _create_run(
                    conn,
                    run_key=normalized_key,
                    run_type=run_type,
                    project_key=project_key,
                    scope_code=scope_code,
                    timeout_seconds=timeout_seconds,
                )
            except sqlite3.IntegrityError:
                existing = _existing_by_key(conn, normalized_key)
                return {"status": "existing_result", "run": existing}
            _update_run(
                conn,
                run_id,
                status="running",
                fields={"reason_codes_json": list(lock.get("warnings") or [])},
            )
        finally:
            conn.close()

        started = _utc_now()
        conn = _connect(paths.db_path)
        try:
            preview = _build_preview_internal(
                conn,
                project_key=project_key,
                scope_code=scope_code,
                candidate_limit=candidate_limit,
                proposal_budget=proposal_budget,
                allowed_actions=list(allowed_actions or DEFAULT_ALLOWED_ACTIONS),
                memory_ids=memory_ids,
                include_debug=include_debug,
            )
        finally:
            conn.close()

        candidate_ids = list(preview.get("candidate_memory_ids") or [])
        deterministic = dict(preview.get("deterministic") or {})
        shadow_summary: dict[str, Any] = {"status": "not_run"}
        reason_codes = list(lock.get("warnings") or [])
        network_calls = 0
        final_status = "completed"

        if preview.get("status") == "no_candidates":
            final_status = "no_op"
        elif preview.get("status") != "preview_ready":
            final_status = "blocked"
            reason_codes.extend(preview.get("reason_codes") or [str(preview.get("status"))])
        elif execute_shadow:
            shadow_preview = preview["_shadow_preview"]
            if shadow_preview.get("status") == "preview_ready":
                provider = (provider_factory or _build_provider)(preview["_gemini_config"])
                try:
                    shadow_result = shadow.run_shadow(
                        connection_factory=lambda: _connect(paths.db_path),
                        preview=shadow_preview,
                        provider=provider,
                        requested_by=requested_by,
                        notes=f"canonical:{normalized_key}",
                    )
                except TimeoutError:
                    shadow_result = {"status": "timed_out", "reason_codes": ["provider_timeout"]}
                shadow_summary = _safe_shadow_summary(shadow_result)
                network_calls = int((shadow_summary.get("safety") or {}).get("network_calls") or 1)
                if shadow_summary["status"] == "timed_out":
                    final_status = "timed_out"
                    reason_codes.extend(shadow_summary.get("reason_codes") or ["provider_timeout"])
                elif shadow_summary["status"] in {"failed", "provider_failed", "rejected", "response_rejected"}:
                    final_status = "partial"
                    reason_codes.extend(shadow_summary.get("reason_codes") or ["shadow_failed"])
            else:
                final_status = "partial"
                shadow_summary = _safe_shadow_summary(shadow.public_preview(shadow_preview))
                reason_codes.extend(shadow_preview.get("reason_codes") or [str(shadow_preview.get("status"))])
        else:
            final_status = "partial"
            reason_codes.append("shadow_not_requested")

        deterministic_count = int(deterministic.get("proposal_count") or 0)
        shadow_count = sum(int(value) for value in dict(shadow_summary.get("proposal_counts") or {}).values())
        if final_status == "completed" and deterministic_count + shadow_count == 0:
            final_status = "no_op"
        elapsed_ms = max(0, int((_utc_now() - started).total_seconds() * 1000))
        if elapsed_ms > int(timeout_seconds) * 1000:
            final_status = "timed_out"
            reason_codes.append("scheduler_timeout_exceeded")

        result_summary = {
            "deterministic_proposal_counts": dict(deterministic.get("proposal_counts") or {}),
            "shadow_proposal_counts": dict(shadow_summary.get("proposal_counts") or {}),
            "shadow_abstain": shadow_summary.get("abstain"),
            "memory_writes": 0,
            "queue_writes": 0,
            "auto_apply": False,
        }
        fields = {
            "input_fingerprint": preview.get("input_fingerprint"),
            "candidate_memory_ids_json": candidate_ids,
            "allowed_actions_json": sorted(set(allowed_actions or DEFAULT_ALLOWED_ACTIONS)),
            "candidate_count": len(candidate_ids),
            "deterministic_proposal_count": deterministic_count,
            "shadow_run_id": shadow_summary.get("shadow_run_id"),
            "shadow_status": shadow_summary.get("status"),
            "shadow_validation_status": shadow_summary.get("validation_status"),
            "model_name": shadow_summary.get("model_name"),
            "model_role": shadow_summary.get("model_role"),
            "network_calls": network_calls,
            "latency_ms": shadow_summary.get("latency_ms") or elapsed_ms,
            "input_tokens": (shadow_summary.get("usage") or {}).get("input_tokens"),
            "output_tokens": (shadow_summary.get("usage") or {}).get("output_tokens"),
            "total_tokens": (shadow_summary.get("usage") or {}).get("total_tokens"),
            "estimated_cost_usd": shadow_summary.get("estimated_cost_usd"),
            "result_summary_json": result_summary,
            "reason_codes_json": sorted(set(str(code) for code in reason_codes if code)),
            "changed_count": 0,
            "auto_apply": 0,
        }
        conn = _connect(paths.db_path)
        try:
            _update_run(conn, run_id, status=final_status, fields=fields)
            run = _existing_by_key(conn, normalized_key)
            assert run is not None
            report_path = paths.reports_dir / f"{local.strftime('%Y-%m-%dT%H%M%S')}-{run_type}.md"
            _write_text_atomic(report_path, _nightly_report_markdown(run))
            _update_run(conn, run_id, status=final_status, fields={"report_path": str(report_path)})
            run = _existing_by_key(conn, normalized_key)
        finally:
            conn.close()
        return {
            "schema_version": RUN_SCHEMA,
            "status": final_status,
            "run": run,
            "memory_writes": 0,
            "queue_writes": 0,
            "auto_apply": False,
        }
    except BaseException as exc:
        if run_id is None:
            raise
        category = "provider_timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
        conn = _connect(paths.db_path)
        try:
            _update_run(
                conn,
                run_id,
                status="timed_out" if isinstance(exc, TimeoutError) else "failed",
                fields={
                    "error_category": category,
                    "reason_codes_json": [category],
                    "changed_count": 0,
                    "auto_apply": 0,
                    "result_summary_json": {"memory_writes": 0, "queue_writes": 0, "auto_apply": False},
                },
            )
            run = _existing_by_key(conn, normalized_key)
        finally:
            conn.close()
        return {"status": run["status"] if run else "failed", "run": run, "error_category": category}
    finally:
        release_lock(paths, run_key=normalized_key)


def _expected_nightly_date(now: datetime | None = None) -> date:
    local = _local_now(now)
    if local.time() >= dt_time(hour=NIGHTLY_HOUR, minute=30):
        return local.date()
    return local.date() - timedelta(days=1)


def _nightly_key(project_key: str, expected_date: date) -> str:
    return f"nightly_preview:{project_key}:{expected_date.isoformat()}"


def _morning_status(source_status: str | None) -> str:
    if source_status in {"completed", "partial"}:
        return "success"
    if source_status == "no_op":
        return "no-op"
    if source_status == "blocked":
        return "blocked"
    if source_status in {"failed", "timed_out"}:
        return "failed"
    return "missed"


def run_morning_report(
    *,
    root_path: str | os.PathLike[str] | Path | None = None,
    project_key: str = "demo-project",
    now: datetime | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    paths = ensure_canonical_schema(root_path)
    local = _local_now(now)
    source_key = _nightly_key(project_key, _expected_nightly_date(local))
    morning_key = f"morning_report:{project_key}:{local.date().isoformat()}"
    conn = _connect(paths.db_path)
    try:
        existing = _existing_by_key(conn, morning_key)
        source = _existing_by_key(conn, source_key)
    finally:
        conn.close()
    if existing is not None:
        return {"status": "existing_result", "run": existing}
    lock = acquire_lock(paths, run_key=morning_key, run_type="morning_report", timeout_seconds=timeout_seconds)
    if not lock["acquired"]:
        return {"status": "already_running", "reason_codes": [str(lock["reason"])], "active_lock": lock.get("lock")}
    try:
        conn = _connect(paths.db_path)
        try:
            run_id = _create_run(
                conn,
                run_key=morning_key,
                run_type="morning_report",
                project_key=project_key,
                scope_code="project",
                timeout_seconds=timeout_seconds,
                source_run_id=int(source["id"]) if source else None,
            )
            report_status = _morning_status(source.get("status") if source else None)
            ledger_status = "missed" if report_status == "missed" else "completed"
            report_path = paths.reports_dir / f"{local.date().isoformat()}-morning.md"
            markdown = (
                "# Sandman Morning Report\n\n"
                f"- Date: {local.date().isoformat()}\n"
                f"- Expected nightly key: {source_key}\n"
                f"- Morning status: {report_status}\n"
                f"- Source run id: {source.get('id') if source else 'none'}\n"
                f"- Source status: {source.get('status') if source else 'missing'}\n"
                f"- Provider path: {source.get('provider_path') if source else PROVIDER_PATH}\n"
                f"- Candidates: {source.get('candidate_count') if source else 0}\n"
                f"- Deterministic proposals: {source.get('deterministic_proposal_count') if source else 0}\n"
                f"- Gemini shadow: {source.get('shadow_status') if source else 'not_run'}\n"
                f"- Estimated cost USD: {source.get('estimated_cost_usd') if source else 0}\n"
                "- Memory writes: 0\n"
                "- Auto-apply: false\n"
            )
            _write_text_atomic(report_path, markdown)
            _update_run(
                conn,
                run_id,
                status=ledger_status,
                fields={
                    "report_path": str(report_path),
                    "result_summary_json": {
                        "morning_status": report_status,
                        "source_run_id": source.get("id") if source else None,
                        "source_status": source.get("status") if source else None,
                        "memory_writes": 0,
                        "queue_writes": 0,
                        "auto_apply": False,
                    },
                    "reason_codes_json": [] if source else ["expected_nightly_run_missing"],
                    "changed_count": 0,
                    "auto_apply": 0,
                },
            )
            run = _existing_by_key(conn, morning_key)
        finally:
            conn.close()
        return {
            "schema_version": MORNING_REPORT_SCHEMA,
            "status": report_status,
            "run": run,
            "source_run": source,
            "report_path": str(report_path),
        }
    finally:
        release_lock(paths, run_key=morning_key)


def get_canonical_status(
    *,
    root_path: str | os.PathLike[str] | Path | None = None,
    project_key: str = "demo-project",
    now: datetime | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    paths = ensure_canonical_schema(root_path)
    expected_date = _expected_nightly_date(now)
    expected_key = _nightly_key(project_key, expected_date)
    conn = _connect(paths.db_path)
    try:
        expected = _existing_by_key(conn, expected_key)
        latest_nightly = _row_payload(conn.execute(
            "SELECT * FROM sandman_scheduler_runs WHERE run_type IN ('nightly_preview','canary') AND project_key=? ORDER BY id DESC LIMIT 1",
            (project_key,),
        ).fetchone())
        latest_morning = _row_payload(conn.execute(
            "SELECT * FROM sandman_scheduler_runs WHERE run_type='morning_report' AND project_key=? ORDER BY id DESC LIMIT 1",
            (project_key,),
        ).fetchone())
        flags = {
            key: _evaluate_flag(_flag(conn, key), project_key=project_key, scope_code="project")
            for key in (CANONICAL_FLAG, PROVIDER_FLAG, SHADOW_FLAG, ROUTING_FLAG, LEGACY_GEMMA_FLAG)
        }
    finally:
        conn.close()
    lock = inspect_lock(paths)
    reasons: list[str] = []
    if not flags[CANONICAL_FLAG]["enabled"]:
        reasons.append("canonical_scheduler_disabled")
    if not flags[PROVIDER_FLAG]["enabled"]:
        reasons.append("provider_v3_disabled")
    if not flags[SHADOW_FLAG]["enabled"]:
        reasons.append("gemini_shadow_disabled")
    if flags[LEGACY_GEMMA_FLAG]["enabled"]:
        reasons.append("legacy_gemma_still_enabled")
    if flags[ROUTING_FLAG]["enabled"]:
        reasons.append("model_queue_routing_must_remain_disabled")
    if expected is None:
        reasons.append("expected_nightly_run_missing")
    elif expected.get("status") not in GOOD_STATUSES:
        reasons.append(f"expected_nightly_status_{expected.get('status')}")
    if lock["active"]:
        reasons.append("scheduler_lock_active")
    result = {
        "status": "ready" if not reasons else "attention",
        "schema_version": STATUS_SCHEMA,
        "scheduler": {
            "name": SCHEDULER_NAME,
            "timezone": SCHEDULER_TIMEZONE,
            "nightly_time": "22:00",
            "morning_time": "09:00",
            "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "multiple_instances": "ignore_new_plus_file_lock",
        },
        "provider_path": PROVIDER_PATH,
        "model_auto_apply": False,
        "queue_routing_enabled": False,
        "expected_nightly_date": expected_date.isoformat(),
        "expected_nightly_run_key": expected_key,
        "expected_nightly_run": expected,
        "latest_nightly_run": latest_nightly,
        "latest_morning_run": latest_morning,
        "reason_codes": sorted(set(reasons)),
        "flags": flags,
        "lock": lock,
        "legacy_runtime": {
            "math_mara_scheduler_active": False,
            "gemma_hygiene_enabled": flags[LEGACY_GEMMA_FLAG]["enabled"],
            "v1_run_exposed_in_workshop": False,
        },
    }
    if include_debug:
        result["paths"] = {
            "db_path": str(paths.db_path),
            "base_dir": str(paths.base_dir),
            "reports_dir": str(paths.reports_dir),
            "lock_path": str(paths.lock_path),
        }
    return result


def _parse_ids(value: str | None) -> list[int] | None:
    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError("memory_ids_must_be_array")
    return [int(item) for item in parsed]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canonical proposal-only Sandman scheduler")
    parser.add_argument("command", choices=("nightly", "canary", "morning", "status"))
    parser.add_argument("--root", default=None)
    parser.add_argument("--project-key", default="demo-project")
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATE_LIMIT)
    parser.add_argument("--proposal-budget", type=int, default=DEFAULT_PROPOSAL_BUDGET)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--memory-ids-json", default=None)
    parser.add_argument("--no-shadow", action="store_true")
    parser.add_argument("--include-debug", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "morning":
        result = run_morning_report(root_path=args.root, project_key=args.project_key)
    elif args.command == "status":
        result = get_canonical_status(root_path=args.root, project_key=args.project_key, include_debug=args.include_debug)
    else:
        run_type = "canary" if args.command == "canary" else "nightly_preview"
        explicit_key = None
        if run_type == "canary":
            explicit_key = f"canary:{args.project_key}:{_local_now().strftime('%Y%m%dT%H%M%S')}:{uuid.uuid4().hex[:8]}"
        result = run_canonical(
            root_path=args.root,
            project_key=args.project_key,
            run_type=run_type,
            run_key=explicit_key,
            candidate_limit=args.candidate_limit,
            proposal_budget=args.proposal_budget,
            memory_ids=_parse_ids(args.memory_ids_json),
            timeout_seconds=args.timeout_seconds,
            requested_by="scheduler" if run_type == "nightly_preview" else "operator_canary",
            execute_shadow=not args.no_shadow,
            include_debug=args.include_debug,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") not in {"failed", "timed_out"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
