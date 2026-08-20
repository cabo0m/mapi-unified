from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from mapi_core.sandman.contracts import canonical_json


SAFE_COLUMNS = (
    "id", "run_key", "request_id", "provider_name", "provider_kind", "model_name",
    "model_role", "api_mode", "status", "project_key", "scope_code", "workspace_id",
    "request_schema_version", "response_schema_version", "validation_schema_version",
    "redaction_policy_version", "external_data_policy", "input_fingerprint",
    "request_manifest_json", "candidate_memory_ids_json", "allowed_actions_json",
    "proposal_budget", "store_requested", "previous_interaction_id_used", "background_used",
    "tools_used", "file_api_used", "grounding_used", "started_at", "completed_at",
    "latency_ms", "input_tokens", "output_tokens", "total_tokens", "estimated_cost_usd",
    "retry_count", "validation_status", "validation_reason_codes_json",
    "proposal_counts_json", "abstain", "response_fingerprint", "error_category",
    "provider_metadata_json", "created_at", "updated_at",
)
JSON_COLUMNS = {
    "request_manifest_json", "candidate_memory_ids_json", "allowed_actions_json",
    "validation_reason_codes_json", "proposal_counts_json", "provider_metadata_json",
}
CREATE_FIELDS = frozenset(
    {
        "run_key", "request_id", "provider_name", "provider_kind", "model_name",
        "model_role", "api_mode", "project_key", "scope_code", "workspace_id",
        "request_schema_version", "response_schema_version", "validation_schema_version",
        "redaction_policy_version", "external_data_policy", "input_fingerprint",
        "request_manifest", "candidate_memory_ids", "allowed_actions", "proposal_budget",
        "store_requested", "previous_interaction_id_used", "background_used", "tools_used",
        "file_api_used", "grounding_used",
    }
)
IMMUTABLE_FIELDS = frozenset(
    {
        "run_key", "request_id", "provider_name", "provider_kind", "model_name",
        "model_role", "api_mode", "project_key", "scope_code", "workspace_id",
        "request_schema_version", "response_schema_version", "validation_schema_version",
        "redaction_policy_version", "external_data_policy", "input_fingerprint",
        "request_manifest_json", "candidate_memory_ids_json", "allowed_actions_json",
        "proposal_budget", "store_requested", "previous_interaction_id_used", "background_used",
        "tools_used", "file_api_used", "grounding_used", "created_at",
    }
)
_COMPLETION_FIELDS = frozenset(
    {
        "completed_at", "latency_ms", "input_tokens", "output_tokens", "total_tokens",
        "estimated_cost_usd", "retry_count", "validation_status",
        "validation_reason_codes_json", "proposal_counts_json", "abstain",
        "response_fingerprint", "provider_metadata_json",
    }
)
MUTABLE_FIELDS_BY_TRANSITION = {
    ("planned", "running"): frozenset({"started_at"}),
    ("planned", "skipped"): frozenset(
        {
            "completed_at", "validation_status", "validation_reason_codes_json",
            "error_category", "provider_metadata_json",
        }
    ),
    ("running", "completed"): _COMPLETION_FIELDS,
    ("running", "rejected"): _COMPLETION_FIELDS,
    ("running", "failed"): frozenset(
        {
            "completed_at", "latency_ms", "input_tokens", "output_tokens", "total_tokens",
            "estimated_cost_usd", "retry_count", "validation_status",
            "validation_reason_codes_json", "error_category", "provider_metadata_json",
        }
    ),
}
TRANSITIONS = {
    "planned": {"running", "skipped"},
    "running": {"completed", "rejected", "failed"},
}


class ShadowRepositoryError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_row(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    result = {key: item.get(key) for key in SAFE_COLUMNS}
    for key in JSON_COLUMNS:
        result[key.removesuffix("_json")] = json.loads(str(result.pop(key)))
    for key in (
        "store_requested", "previous_interaction_id_used", "background_used", "tools_used",
        "file_api_used", "grounding_used",
    ):
        result[key] = bool(result[key])
    if result["abstain"] is not None:
        result["abstain"] = bool(result["abstain"])
    return result


def create_planned(conn: sqlite3.Connection, fields: Mapping[str, Any]) -> dict[str, Any]:
    if set(fields) != CREATE_FIELDS:
        raise ShadowRepositoryError("invalid_create_fields")
    now = _now()
    payload = {
        **fields,
        "status": "planned",
        "request_manifest_json": canonical_json(fields["request_manifest"]),
        "candidate_memory_ids_json": canonical_json(fields["candidate_memory_ids"]),
        "allowed_actions_json": canonical_json(fields["allowed_actions"]),
        "validation_reason_codes_json": "[]",
        "proposal_counts_json": "{}",
        "provider_metadata_json": "{}",
        "created_at": now,
        "updated_at": now,
    }
    payload.pop("request_manifest")
    payload.pop("candidate_memory_ids")
    payload.pop("allowed_actions")
    columns = ", ".join(payload)
    placeholders = ", ".join("?" for _ in payload)
    try:
        cursor = conn.execute(
            f"INSERT INTO sandman_semantic_shadow_runs ({columns}) VALUES ({placeholders})",
            tuple(payload.values()),
        )
    except sqlite3.IntegrityError as exc:
        if "run_key" in str(exc):
            raise ShadowRepositoryError("duplicate_run_key") from None
        raise
    return get_run(conn, int(cursor.lastrowid))


def transition(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    expected_status: str,
    new_status: str,
    fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if new_status not in TRANSITIONS.get(expected_status, set()):
        raise ShadowRepositoryError("invalid_status_transition")
    values = dict(fields or {})
    field_names = set(values)
    if field_names & IMMUTABLE_FIELDS:
        raise ShadowRepositoryError("immutable_field_update_forbidden")
    allowed_fields = MUTABLE_FIELDS_BY_TRANSITION[(expected_status, new_status)]
    if not field_names or not field_names <= allowed_fields:
        raise ShadowRepositoryError("invalid_transition_fields")
    for key in list(values):
        if key in JSON_COLUMNS:
            values[key] = canonical_json(values[key])
    values["updated_at"] = _now()
    assignments = ", ".join(f"{key}=?" for key in values)
    cursor = conn.execute(
        f"UPDATE sandman_semantic_shadow_runs SET status=?, {assignments} WHERE id=? AND status=?",
        (new_status, *values.values(), run_id, expected_status),
    )
    if cursor.rowcount != 1:
        raise ShadowRepositoryError("stale_status_transition")
    return get_run(conn, run_id)


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT {', '.join(SAFE_COLUMNS)} FROM sandman_semantic_shadow_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    result = _safe_row(row)
    if result is None:
        raise ShadowRepositoryError("shadow_run_not_found")
    return result


def get_by_run_key(conn: sqlite3.Connection, run_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {', '.join(SAFE_COLUMNS)} FROM sandman_semantic_shadow_runs WHERE run_key=?",
        (run_key,),
    ).fetchone()
    return _safe_row(row)


def list_runs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    project_key: str | None = None,
    model_name: str | None = None,
    validation_status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 200:
        raise ValueError("limit_out_of_range")
    where: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("status", status),
        ("project_key", project_key),
        ("model_name", model_name),
        ("validation_status", validation_status),
    ):
        if value is not None:
            where.append(f"{column}=?")
            params.append(value)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT {', '.join(SAFE_COLUMNS)} FROM sandman_semantic_shadow_runs"
        f"{clause} ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [_safe_row(row) for row in rows]  # type: ignore[misc]
