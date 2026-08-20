from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable

from mapi_core.memory.lifecycle_contracts import MEMORY_V3_HASH_ALGORITHM
from mapi_core.memory.lifecycle_integrity import evaluate_lifecycle_integrity_graph
from mapi_core.memory.lifecycle_snapshots import (
    create_lifecycle_snapshot_payload,
    find_lifecycle_snapshot_by_operation_key,
    get_lifecycle_snapshot_payload,
    mark_lifecycle_snapshot_rolled_back_payload,
)


LIFECYCLE_REMEDIATION_PLAN_VERSION = "legacy_supersession_2026_07_17_v2"
LIFECYCLE_REMEDIATION_INVENTORY_SCHEMA_VERSION = "memory_v3_lifecycle_remediation_inventory.v1"
LIFECYCLE_REMEDIATION_PREVIEW_SCHEMA_VERSION = "memory_v3_lifecycle_remediation_preview.v1"
LIFECYCLE_REMEDIATION_APPLY_SCHEMA_VERSION = "memory_v3_lifecycle_remediation_apply.v1"
LIFECYCLE_REMEDIATION_RUN_SCHEMA_VERSION = "memory_v3_lifecycle_remediation_run.v1"
LIFECYCLE_REMEDIATION_ROLLBACK_PREVIEW_SCHEMA_VERSION = "memory_v3_lifecycle_remediation_rollback_preview.v1"
LIFECYCLE_REMEDIATION_ROLLBACK_SCHEMA_VERSION = "memory_v3_lifecycle_remediation_rollback.v1"
LIFECYCLE_REMEDIATION_SNAPSHOT_SCHEMA_VERSION = "memory_v3_lifecycle_remediation_snapshot.v1"
LIFECYCLE_REMEDIATION_BACKUP_SCHEMA_VERSION = "memory_v3_lifecycle_remediation_backup.v1"
MEMORY_396_VERIFIED_IDENTITY_SCHEMA_VERSION = "memory_v3_legacy_rehome_verified_identity.v1"
TARGET_EVENT_LEDGER_SCHEMA_VERSION = "memory_v3_lifecycle_remediation_event_ledger.v1"

MEMORY_396_VERIFIED_IDENTITY = {
    "content_sha256": "7b28cd574cc6e77f8c65f19e5626d8822f53a110e9fd511f401905153db54ffd",
    "title": "Dodano statystyczny korpus testowy Sandmana",
    "summary_short": "Dodano statystyczny korpus testowy Sandmana",
    "memory_type": "project_context",
    "source": "chatgpt_memory_capture",
    "created_at": "2026-04-27T00:54:46Z",
    "scope_code": "project",
    "workspace_id": 1,
    "schema_version": 1,
    "entry_type": "project",
    "truth_kind": "fact",
    "visibility_scope": "project",
    "sharing_policy": "explicit",
    "owner_role": "review_team",
    "owner_id": "global_review_ops",
    "owner_user_id": 1,
    "created_by_user_id": 1,
    "subject_user_id": None,
    "requires_user_confirmation": 0,
    "layer_code": "projects",
    "area_code": "projects",
    "importance_level": "critical",
}
MEMORY_396_ALLOWED_LIFECYCLE_VARIANTS = (
    {
        "project_key": "demo-project",
        "state_code": "active",
        "memory_v2_status": "active",
        "superseded_by_memory_id": None,
        "supersedes_memory_id": None,
    },
    {
        "project_key": "mapi",
        "state_code": "superseded",
        "memory_v2_status": "superseded",
        "superseded_by_memory_id": 497,
        "supersedes_memory_id": None,
    },
)

MEMORY_IDS = (396, 497, 829, 830, 1159, 1160, 1161, 1163, 1303, 1304)
LINK_IDS = (669, 1009, 1010, 1106, 1107, 1110)
PAIRS = (
    (497, 396, 669),
    (830, 829, 1010),
    (1163, 1161, 1009),
    (1161, 1160, 1107),
    (1160, 1159, 1110),
    (1304, 1303, 1106),
)
HEAD_IDS = (497, 830, 1163, 1304)
OLD_IDS = (396, 829, 1161, 1160, 1159, 1303)
EXPECTED_LINK_ORIGINS = {
    669: "chatgpt_checkpoint_linking",
    1009: "memory_linking_pass_v1",
    1010: "memory_linking_pass_v1",
    1106: "memory_linking_pass_v1",
    1107: "memory_linking_pass_v1",
    1110: "memory_linking_pass_v1",
}
PUBLIC_MEMORY_FIELDS = (
    "id",
    "project_key",
    "scope_code",
    "workspace_id",
    "state_code",
    "memory_v2_status",
    "activity_state",
    "created_at",
    "updated_at",
    "supersedes_memory_id",
    "superseded_by_memory_id",
    "valid_to",
    "expired_due_at",
    "memory_type",
    "source",
)
RESTORABLE_FIELDS = (
    "project_key",
    "supersedes_memory_id",
    "superseded_by_memory_id",
    "state_code",
    "memory_v2_status",
    "valid_to",
    "expired_due_at",
    "updated_at",
    "last_accessed_at",
    "validation_source",
)


class LifecycleRemediationBlocked(RuntimeError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _public_memory(row: dict[str, Any]) -> dict[str, Any]:
    result = {field: row.get(field) for field in PUBLIC_MEMORY_FIELDS}
    result["content_sha256"] = hashlib.sha256(str(row.get("content") or "").encode("utf-8")).hexdigest()
    return result


def _parse_iso(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _expected_timestamps(old: dict[str, Any], successor: dict[str, Any]) -> tuple[str, str]:
    old_created = _parse_iso(old.get("created_at"))
    successor_created = _parse_iso(successor.get("created_at"))
    if successor_created <= old_created:
        raise LifecycleRemediationBlocked(f"invalid_chronology:{old['id']}:{successor['id']}")
    return _format_iso(successor_created), _format_iso(successor_created + timedelta(days=2))


def _migration_tail(conn: Any) -> list[str]:
    return [str(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY applied_at, version").fetchall()]


def _load_exact_rows(conn: Any, table: str, ids: tuple[int, ...]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT * FROM {table} WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
    return [_row_dict(row) for row in rows]


def _event_counts(conn: Any) -> dict[str, int]:
    return {
        str(memory_id): int(
            conn.execute("SELECT COUNT(*) FROM memory_events WHERE memory_id = ?", (memory_id,)).fetchone()[0]
        )
        for memory_id in MEMORY_IDS
    }


def _target_event_ledger(conn: Any) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in MEMORY_IDS)
    rows = conn.execute(
        "SELECT id, memory_id, event_type, payload_json, created_at "
        f"FROM memory_events WHERE memory_id IN ({placeholders}) ORDER BY id",
        MEMORY_IDS,
    ).fetchall()
    return [
        {
            "id": int(row[0]),
            "memory_id": int(row[1]),
            "event_type": str(row[2]),
            "created_at": str(row[4]),
            "payload_sha256": hashlib.sha256(
                ("" if row[3] is None else str(row[3])).encode("utf-8")
            ).hexdigest(),
        }
        for row in rows
    ]


def _event_ledger_fingerprint(ledger: list[dict[str, Any]]) -> str:
    return _hash({"schema_version": TARGET_EVENT_LEDGER_SCHEMA_VERSION, "events": ledger})


def _memory_396_identity(memory: dict[str, Any]) -> dict[str, Any]:
    actual_immutable = {
        field: (
            hashlib.sha256(str(memory.get("content") or "").encode("utf-8")).hexdigest()
            if field == "content_sha256"
            else memory.get(field)
        )
        for field in MEMORY_396_VERIFIED_IDENTITY
    }
    mismatched = [
        field
        for field, expected in MEMORY_396_VERIFIED_IDENTITY.items()
        if actual_immutable.get(field) != expected
    ]
    lifecycle_fields = tuple(MEMORY_396_ALLOWED_LIFECYCLE_VARIANTS[0])
    actual_lifecycle = {field: memory.get(field) for field in lifecycle_fields}
    lifecycle_variant_matched = any(
        actual_lifecycle == variant for variant in MEMORY_396_ALLOWED_LIFECYCLE_VARIANTS
    )
    if not lifecycle_variant_matched:
        mismatched.extend(field for field in lifecycle_fields if field not in mismatched)
    return {
        "identity_schema_version": MEMORY_396_VERIFIED_IDENTITY_SCHEMA_VERSION,
        "verified_content_sha256": MEMORY_396_VERIFIED_IDENTITY["content_sha256"],
        "actual_content_sha256": actual_immutable["content_sha256"],
        "matched": not mismatched,
        "mismatched_field_names": mismatched,
        "expected_identity": {
            "immutable": dict(MEMORY_396_VERIFIED_IDENTITY),
            "allowed_lifecycle_variants": [
                dict(variant) for variant in MEMORY_396_ALLOWED_LIFECYCLE_VARIANTS
            ],
        },
        "actual_identity": {
            "immutable": actual_immutable,
            "lifecycle_variant": actual_lifecycle,
        },
    }


def _snapshot_counts(conn: Any) -> dict[str, int]:
    rows = conn.execute(
        "SELECT operation_type, COUNT(*) FROM memory_lifecycle_snapshots GROUP BY operation_type ORDER BY operation_type"
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _integrity(memories: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    prepared: dict[int, dict[str, Any]] = {}
    for row in memories:
        item = dict(row)
        item["_raw"] = dict(row)
        prepared[int(item["id"])] = item
    return evaluate_lifecycle_integrity_graph(
        {
            "base_rows": list(prepared.values()),
            "base_ids": sorted(prepared),
            "memories_by_id": prepared,
            "links": links,
        },
        sample_limit=100,
        include_debug=False,
    )


def _context(conn: Any) -> dict[str, Any]:
    memories = _load_exact_rows(conn, "memories", MEMORY_IDS)
    links = _load_exact_rows(conn, "memory_links", LINK_IDS)
    event_counts = _event_counts(conn)
    snapshot_counts = _snapshot_counts(conn)
    migrations = _migration_tail(conn)
    target_event_ledger = _target_event_ledger(conn)
    identity_396 = _memory_396_identity(
        next((row for row in memories if int(row["id"]) == 396), {})
    )
    candidate_payload = {
        "plan_version": LIFECYCLE_REMEDIATION_PLAN_VERSION,
        "memories": memories,
        "links": links,
        "target_event_ledger_schema_version": TARGET_EVENT_LEDGER_SCHEMA_VERSION,
        "target_event_ledger": target_event_ledger,
        "memory_396_verified_identity_schema_version": MEMORY_396_VERIFIED_IDENTITY_SCHEMA_VERSION,
        "memory_396_expected_identity": identity_396["expected_identity"],
        "memory_396_actual_identity": identity_396["actual_identity"],
        "lifecycle_snapshot_counts": snapshot_counts,
        "schema_migrations": migrations,
    }
    return {
        "memories": memories,
        "memories_by_id": {int(row["id"]): row for row in memories},
        "links": links,
        "links_by_id": {int(row["id"]): row for row in links},
        "event_counts": event_counts,
        "target_event_ledger": target_event_ledger,
        "target_event_ledger_fingerprint": _event_ledger_fingerprint(target_event_ledger),
        "identity_396": identity_396,
        "snapshot_counts": snapshot_counts,
        "migrations": migrations,
        "candidate_set_payload": candidate_payload,
        "candidate_set_fingerprint": _hash(candidate_payload),
    }


def _evidence_396(memory: dict[str, Any]) -> dict[str, Any]:
    haystack = "\n".join(
        str(memory.get(field) or "") for field in ("content", "summary_short", "title")
    ).casefold()
    checks = {
        "memory_396_mentions_sandman": "sandman" in haystack,
        "memory_396_mentions_statistical_corpus": "statistical corpus" in haystack or "statystyczny korpus" in haystack,
        "memory_396_mentions_canonical_test": "tests/test_sandman_statistical_corpus.py" in haystack or "pytest/test_sandman_statistical_corpus.py" in haystack,
        "memory_396_mentions_scope_isolation": any(
            signal in haystack
            for signal in (
                "scope isolation",
                "test_sandman_scope_isolation.py",
                "scope project/workspace",
            )
        ),
    }
    identity = _memory_396_identity(memory)
    return {
        "memory_id": 396,
        "evidence_rule_ids": list(checks),
        "evidence_matches": checks,
        "semantic_diagnostics_passed": all(checks.values()),
        "identity_schema_version": identity["identity_schema_version"],
        "verified_content_sha256": identity["verified_content_sha256"],
        "actual_content_sha256": identity["actual_content_sha256"],
        "matched": identity["matched"],
        "mismatched_field_names": identity["mismatched_field_names"],
        "passed": identity["matched"],
    }


def _dependency_rehome_blockers(conn: Any) -> list[str]:
    blockers: list[str] = []
    table_names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    for table in table_names:
        try:
            columns = [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        except sqlite3.Error:
            continue
        if "memory_id" not in columns or "project_key" not in columns:
            continue
        try:
            rows = conn.execute(
                f'SELECT project_key FROM "{table}" WHERE memory_id = ? AND project_key IS NOT NULL',
                (396,),
            ).fetchall()
        except sqlite3.Error:
            continue
        if any(str(row[0]) not in {"demo-project", "mapi"} for row in rows):
            blockers.append(f"memory_396_dependency_project_drift:{table}")
    return blockers


def _desired_state(context: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    desired = [dict(row) for row in context["memories"]]
    by_id = {int(row["id"]): row for row in desired}
    blockers: list[str] = []
    for new_id, old_id, _ in PAIRS:
        new = by_id.get(new_id)
        old = by_id.get(old_id)
        if new is None or old is None:
            continue
        try:
            valid_to, expired_due_at = _expected_timestamps(old, new)
        except (ValueError, LifecycleRemediationBlocked) as exc:
            blockers.append(str(exc))
            continue
        for field, expected in (("valid_to", valid_to), ("expired_due_at", expired_due_at)):
            current = old.get(field)
            if current not in (None, "", expected):
                blockers.append(f"historical_timestamp_conflict:{old_id}:{field}")
            else:
                old[field] = expected
        new["supersedes_memory_id"] = old_id
        old["superseded_by_memory_id"] = new_id
        old["state_code"] = "superseded"
        old["memory_v2_status"] = "superseded"
    if 396 in by_id:
        by_id[396]["project_key"] = "mapi"
    updates: list[dict[str, Any]] = []
    current_by_id = context["memories_by_id"]
    for memory_id in MEMORY_IDS:
        current = current_by_id.get(memory_id)
        target = by_id.get(memory_id)
        if current is None or target is None:
            continue
        changes = {
            field: {"before": current.get(field), "after": target.get(field)}
            for field in (
                "project_key",
                "supersedes_memory_id",
                "superseded_by_memory_id",
                "state_code",
                "memory_v2_status",
                "valid_to",
                "expired_due_at",
            )
            if current.get(field) != target.get(field)
        }
        if changes:
            changes.update(
                {
                    "updated_at": {"before": current.get("updated_at"), "after": "<remediation_execution_timestamp>"},
                    "last_accessed_at": {"before": current.get("last_accessed_at"), "after": "<remediation_execution_timestamp>"},
                    "validation_source": {
                        "before": current.get("validation_source"),
                        "after": "memory_v3_legacy_lineage_remediation",
                    },
                }
            )
            updates.append({"memory_id": memory_id, "changes": changes})
    return desired, updates, blockers


def _inventory_data(conn: Any) -> dict[str, Any]:
    context = _context(conn)
    blockers: list[str] = []
    warnings: list[str] = []
    if len(context["memories"]) != len(MEMORY_IDS):
        present = set(context["memories_by_id"])
        blockers.extend(f"unexpected_missing_memory:{memory_id}" for memory_id in MEMORY_IDS if memory_id not in present)
    if len(context["links"]) != len(LINK_IDS):
        present = set(context["links_by_id"])
        blockers.extend(f"unexpected_missing_link:{link_id}" for link_id in LINK_IDS if link_id not in present)

    expected_pairs = {link_id: (new_id, old_id) for new_id, old_id, link_id in PAIRS}
    for link_id, (new_id, old_id) in expected_pairs.items():
        link = context["links_by_id"].get(link_id)
        if link is None:
            continue
        if (int(link["from_memory_id"]), int(link["to_memory_id"])) != (new_id, old_id):
            blockers.append(f"link_endpoint_drift:{link_id}")
        if link.get("relation_type") != "supersedes":
            blockers.append(f"link_relation_drift:{link_id}")
        if link.get("archived_at") is not None:
            blockers.append(f"link_archived:{link_id}")
        if int(link.get("workspace_id") or 0) != 1:
            blockers.append(f"link_workspace_drift:{link_id}")
        if link.get("origin") != EXPECTED_LINK_ORIGINS[link_id]:
            blockers.append(f"unexpected_link_origin:{link_id}")

    placeholders = ",".join("?" for _ in MEMORY_IDS)
    active_cluster_links = [
        _row_dict(row)
        for row in conn.execute(
            "SELECT * FROM memory_links WHERE relation_type='supersedes' AND archived_at IS NULL "
            f"AND (from_memory_id IN ({placeholders}) OR to_memory_id IN ({placeholders})) ORDER BY id",
            MEMORY_IDS + MEMORY_IDS,
        ).fetchall()
    ]
    unexpected = [int(row["id"]) for row in active_cluster_links if int(row["id"]) not in LINK_IDS]
    blockers.extend(f"unexpected_active_supersedes_link:{link_id}" for link_id in unexpected)

    by_id = context["memories_by_id"]
    expected_boundaries = {
        396: ({"demo-project", "mapi"}, "project"),
        497: ({"mapi"}, "project"),
        829: ({"demo-project"}, "global"),
        830: ({"demo-project"}, "global"),
        1159: ({"demo-project"}, "project"),
        1160: ({"demo-project"}, "project"),
        1161: ({"demo-project"}, "project"),
        1163: ({"demo-project"}, "project"),
        1303: ({"demo-project"}, "project"),
        1304: ({"demo-project"}, "project"),
    }
    for memory_id, (projects, scope) in expected_boundaries.items():
        memory = by_id.get(memory_id)
        if memory is None:
            continue
        if memory.get("project_key") not in projects:
            blockers.append(f"project_drift:{memory_id}")
        if memory.get("scope_code") != scope:
            blockers.append(f"scope_drift:{memory_id}")
        if int(memory.get("workspace_id") or 0) != 1:
            blockers.append(f"workspace_drift:{memory_id}")
        if memory.get("activity_state") == "archived" or memory.get("archived_at") is not None:
            blockers.append(f"memory_archived:{memory_id}")
        if str(memory.get("state_code") or "").lower() not in {"active", "validated", "superseded"}:
            blockers.append(f"unknown_lifecycle_state:{memory_id}")

    for new_id, old_id, _ in PAIRS:
        new = by_id.get(new_id)
        old = by_id.get(old_id)
        if new is None or old is None:
            continue
        allowed_new_pointer = {old_id}
        if new_id == 497:
            allowed_new_pointer.add(None)
        if new.get("supersedes_memory_id") not in allowed_new_pointer:
            blockers.append(f"forward_pointer_drift:{new_id}")
        if old.get("superseded_by_memory_id") not in {None, new_id}:
            blockers.append(f"reverse_pointer_occupied:{old_id}")

    evidence = _evidence_396(by_id.get(396, {}))
    memory_396 = by_id.get(396)
    if memory_396 is not None and not evidence["matched"]:
        blockers.append("memory_396_verified_identity_mismatch")
    if memory_396 is not None and memory_396.get("project_key") == "demo-project":
        required_396 = {
            "scope_code": "project",
            "workspace_id": 1,
            "memory_type": "project_context",
            "source": "chatgpt_memory_capture",
            "supersedes_memory_id": None,
            "superseded_by_memory_id": None,
        }
        if any(memory_396.get(field) != value for field, value in required_396.items()):
            blockers.append("memory_396_rehome_evidence_insufficient")
        try:
            if _parse_iso(memory_396.get("created_at")) >= _parse_iso(by_id[497].get("created_at")):
                blockers.append("memory_396_rehome_evidence_insufficient")
        except (KeyError, ValueError):
            blockers.append("memory_396_rehome_evidence_insufficient")
        blockers.extend(_dependency_rehome_blockers(conn))

    desired, planned_updates, desired_blockers = _desired_state(context)
    blockers.extend(desired_blockers)
    current_integrity = _integrity(context["memories"], context["links"])
    projected_integrity = _integrity(desired, context["links"]) if len(desired) == len(MEMORY_IDS) else {}
    if projected_integrity.get("summary", {}).get("critical_issues", 1):
        blockers.append("projected_integrity_not_clean")
    if any(row.get("state_code") == "active" for row in context["memories"]):
        warnings.append("legacy_active_state_alias_present")
    blockers = list(dict.fromkeys(blockers))
    already = not planned_updates and not blockers
    status = "already_remediated" if already else ("inventory_blocked" if blockers else "inventory_ready")
    return {
        "status": status,
        "context": context,
        "desired_memories": desired,
        "planned_updates": planned_updates,
        "current_integrity": current_integrity,
        "projected_integrity": projected_integrity,
        "candidate_evidence": [evidence],
        "blocking_reasons": blockers,
        "warnings": warnings,
    }


def get_memory_lifecycle_remediation_inventory_payload(
    conn: Any,
    *,
    plan_version: str = LIFECYCLE_REMEDIATION_PLAN_VERSION,
    include_debug: bool = False,
) -> dict[str, Any]:
    if str(plan_version) != LIFECYCLE_REMEDIATION_PLAN_VERSION:
        return {
            "status": "inventory_blocked",
            "schema_version": LIFECYCLE_REMEDIATION_INVENTORY_SCHEMA_VERSION,
            "plan_version": str(plan_version),
            "blocking_reasons": ["unsupported_plan_version"],
            "warnings": [],
            "safety": {"read_only": True, "mutations_performed": 0},
        }
    data = _inventory_data(conn)
    context = data["context"]
    result = {
        "schema_version": LIFECYCLE_REMEDIATION_INVENTORY_SCHEMA_VERSION,
        "status": data["status"],
        "plan_version": LIFECYCLE_REMEDIATION_PLAN_VERSION,
        "clusters": [
            {"cluster_id": "A", "memory_ids": [396, 497], "link_ids": [669]},
            {"cluster_id": "B", "memory_ids": [829, 830], "link_ids": [1010]},
            {"cluster_id": "C", "memory_ids": [1159, 1160, 1161, 1163], "link_ids": [1009, 1107, 1110]},
            {"cluster_id": "D", "memory_ids": [1303, 1304], "link_ids": [1106]},
        ],
        "memory_rows": [_public_memory(row) for row in context["memories"]],
        "link_rows": context["links"],
        "current_integrity_findings": {
            "status": data["current_integrity"].get("status"),
            "summary": data["current_integrity"].get("summary"),
            "issue_counts": data["current_integrity"].get("issue_counts"),
            "findings": data["current_integrity"].get("findings"),
        },
        "candidate_evidence": data["candidate_evidence"],
        "blocking_reasons": data["blocking_reasons"],
        "warnings": data["warnings"],
        "safety": {"read_only": True, "mutations_performed": 0},
    }
    if include_debug:
        result["debug"] = {
            "candidate_set_fingerprint": context["candidate_set_fingerprint"],
            "event_counts": context["event_counts"],
            "snapshot_counts": context["snapshot_counts"],
            "migration_tail": context["migrations"],
        }
    return result


def _input_payload() -> dict[str, Any]:
    return {
        "schema_version": LIFECYCLE_REMEDIATION_PREVIEW_SCHEMA_VERSION,
        "plan_version": LIFECYCLE_REMEDIATION_PLAN_VERSION,
        "memory_ids": list(MEMORY_IDS),
        "link_ids": list(LINK_IDS),
        "desired_state_policy": "canonical_bidirectional_supersession_v1",
        "timestamp_policy": "successor_created_at_plus_two_days_v1",
        "backup_policy": LIFECYCLE_REMEDIATION_BACKUP_SCHEMA_VERSION,
        "rollback_policy": LIFECYCLE_REMEDIATION_ROLLBACK_SCHEMA_VERSION,
    }


def preview_memory_lifecycle_remediation_payload(
    conn: Any,
    *,
    plan_version: str = LIFECYCLE_REMEDIATION_PLAN_VERSION,
    include_debug: bool = False,
) -> dict[str, Any]:
    inventory = get_memory_lifecycle_remediation_inventory_payload(
        conn, plan_version=plan_version, include_debug=include_debug
    )
    if str(plan_version) != LIFECYCLE_REMEDIATION_PLAN_VERSION:
        return {
            "status": "blocked",
            "schema_version": LIFECYCLE_REMEDIATION_PREVIEW_SCHEMA_VERSION,
            "plan_version": str(plan_version),
            "blocking_reasons": ["unsupported_plan_version"],
            "warnings": [],
            "safety": {"read_only": True, "mutations_performed": 0},
        }
    data = _inventory_data(conn)
    context = data["context"]
    input_fingerprint = _hash(_input_payload())
    operation_key = f"legacy_lineage_remediation:{LIFECYCLE_REMEDIATION_PLAN_VERSION}:{input_fingerprint}"
    before_state = [_public_memory(row) for row in context["memories"]]
    after_state = [_public_memory(row) for row in data["desired_memories"]]
    planned_events = {
        "version.legacy_lineage_remediation_applied": list(HEAD_IDS),
        "version.legacy_superseded_state_repaired": list(OLD_IDS),
        "project_key.legacy_assignment_repaired": [396],
    }
    status = {
        "inventory_ready": "preview_ready",
        "already_remediated": "already_satisfied",
    }.get(data["status"], "blocked")
    core = {
        "plan_version": LIFECYCLE_REMEDIATION_PLAN_VERSION,
        "operation_key": operation_key,
        "input_fingerprint": input_fingerprint,
        "candidate_set_fingerprint": context["candidate_set_fingerprint"],
        "clusters": inventory.get("clusters", []),
        "before_state": before_state,
        "after_state": after_state,
        "planned_memory_updates": data["planned_updates"],
        "reused_links": [
            {"link_id": int(row["id"]), "from_memory_id": int(row["from_memory_id"]), "to_memory_id": int(row["to_memory_id"]), "link_action": "reused"}
            for row in context["links"]
        ],
        "planned_events": planned_events,
        "planned_snapshot": {
            "operation_type": "legacy_lineage_remediation",
            "new_memory_id": 497,
            "old_memory_id": 396,
            "relation_kind": "legacy_chain_repair",
            "snapshot_rows": 1,
        },
        "backup_requirement": {
            "required_before_transaction": True,
            "schema_version": LIFECYCLE_REMEDIATION_BACKUP_SCHEMA_VERSION,
            "automatic_restore": False,
        },
        "rollback_design": {
            "snapshot_based": True,
            "links_unchanged": True,
            "backup_restore_used": False,
            "event_ledger_schema_version": TARGET_EVENT_LEDGER_SCHEMA_VERSION,
            "exact_target_event_ledger_required": True,
        },
        "integrity_projection": {
            "status": data["projected_integrity"].get("status"),
            "summary": data["projected_integrity"].get("summary"),
            "issue_counts": data["projected_integrity"].get("issue_counts"),
            "findings": data["projected_integrity"].get("findings"),
        },
        "blocking_reasons": data["blocking_reasons"],
        "warnings": data["warnings"],
        "status": status,
    }
    preview_hash = _hash(core)
    result = {
        "schema_version": LIFECYCLE_REMEDIATION_PREVIEW_SCHEMA_VERSION,
        **core,
        "preview_hash": preview_hash,
        "hash_algorithm": MEMORY_V3_HASH_ALGORITHM,
        "safety": {"read_only": True, "mutations_performed": 0, "apply_supported": status == "preview_ready"},
    }
    if include_debug:
        result["debug"] = {
            "event_counts": context["event_counts"],
            "target_event_ledger_schema_version": TARGET_EVENT_LEDGER_SCHEMA_VERSION,
            "target_event_ledger_count": len(context["target_event_ledger"]),
            "target_event_ledger_max_id": max(
                (event["id"] for event in context["target_event_ledger"]), default=None
            ),
            "target_event_ledger_fingerprint": context["target_event_ledger_fingerprint"],
            "snapshot_counts": context["snapshot_counts"],
            "migration_tail": context["migrations"],
        }
    return result


def _source_db_path(conn: Any) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not str(row[2] or "").strip():
        raise LifecycleRemediationBlocked("source_database_is_not_file_backed")
    return Path(str(row[2])).resolve()


def create_verified_lifecycle_remediation_backup(
    *,
    conn: Any,
    backups_root: Path | None,
    expected_candidate_set_fingerprint: str,
    operation_key: str,
    utc_now_iso: Callable[[], str],
) -> dict[str, Any]:
    source_path = _source_db_path(conn)
    root = Path(backups_root or source_path.parent / "backups").resolve()
    root.mkdir(parents=True, exist_ok=True)
    created_at = utc_now_iso()
    stamp = re.sub(r"[^0-9]", "", created_at)[:14]
    backup_path = root / f"agent_memory-v3-lifecycle-remediation-pre-{stamp}.db"
    if backup_path.exists():
        backup_path = root / f"agent_memory-v3-lifecycle-remediation-pre-{stamp}-{_hash(operation_key)[:10]}.db"
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_size = source_path.stat().st_size
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    destination: sqlite3.Connection | None = None
    try:
        source.row_factory = sqlite3.Row
        source_quick = str(source.execute("PRAGMA quick_check").fetchone()[0])
        if source_quick != "ok":
            raise LifecycleRemediationBlocked("source_quick_check_failed")
        destination = sqlite3.connect(backup_path)
        source.backup(destination)
        destination.commit()
        destination.close()
        destination = None
    except Exception:
        if destination is not None:
            destination.close()
        if backup_path.exists():
            backup_path.unlink()
        raise
    finally:
        source.close()
    check = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    check.row_factory = sqlite3.Row
    try:
        backup_quick = str(check.execute("PRAGMA quick_check").fetchone()[0])
        backup_context = _context(check)
        max_memory_id = check.execute("SELECT MAX(id) FROM memories").fetchone()[0]
        max_event_id = check.execute("SELECT MAX(id) FROM memory_events").fetchone()[0]
    finally:
        check.close()
    if backup_quick != "ok":
        backup_path.unlink(missing_ok=True)
        raise LifecycleRemediationBlocked("backup_quick_check_failed")
    if backup_context["candidate_set_fingerprint"] != expected_candidate_set_fingerprint:
        backup_path.unlink(missing_ok=True)
        raise LifecycleRemediationBlocked("backup_candidate_set_fingerprint_mismatch")
    return {
        "schema_version": LIFECYCLE_REMEDIATION_BACKUP_SCHEMA_VERSION,
        "source_path": str(source_path),
        "source_size": source_size,
        "source_sha256_before_backup": source_sha,
        "source_quick_check": "ok",
        "backup_path": str(backup_path),
        "backup_size": backup_path.stat().st_size,
        "backup_sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
        "backup_quick_check": "ok",
        "migration_tail": backup_context["migrations"],
        "max_memory_id": max_memory_id,
        "max_event_id": max_event_id,
        "created_at": created_at,
    }


def _snapshot_memories(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": LIFECYCLE_REMEDIATION_SNAPSHOT_SCHEMA_VERSION,
        "plan_version": LIFECYCLE_REMEDIATION_PLAN_VERSION,
        "memories": context["memories"],
        "integrity_fingerprint": _hash(_integrity(context["memories"], context["links"]).get("findings", [])),
    }


def _current_matches_snapshot(context: dict[str, Any], snapshot: dict[str, Any], *, phase: str) -> bool:
    target = snapshot[f"{phase}_snapshot"]["memories"]
    links = snapshot["link_snapshot"]["after" if phase == "after" else "before"]
    return context["memories"] == target and context["links"] == links


def apply_memory_lifecycle_remediation_payload(
    conn: Any,
    *,
    plan_version: str,
    expected_preview_hash: str,
    expected_candidate_set_fingerprint: str,
    applied_by: str | None,
    reason: str | None,
    confirm_data_repair: bool,
    backups_root: Path | None,
    utc_now_iso: Callable[[], str],
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
    insert_memory_event: Callable[..., dict[str, Any]],
    create_backup: Callable[..., dict[str, Any]] | None = None,
    failure_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    base = {
        "schema_version": LIFECYCLE_REMEDIATION_APPLY_SCHEMA_VERSION,
        "plan_version": str(plan_version),
        "safety": {"real_apply_requires_explicit_call": True},
    }
    if str(plan_version) != LIFECYCLE_REMEDIATION_PLAN_VERSION:
        return {**base, "status": "blocked", "blocking_reasons": ["unsupported_plan_version"], "mutations_performed": 0}
    if not confirm_data_repair:
        return {**base, "status": "blocked", "blocking_reasons": ["confirm_data_repair_required"], "mutations_performed": 0}
    try:
        expected_hash = normalize_required_text(expected_preview_hash, "expected_preview_hash")
        expected_candidate = normalize_required_text(expected_candidate_set_fingerprint, "expected_candidate_set_fingerprint")
        actor = normalize_required_text(applied_by, "applied_by")
        normalized_reason = normalize_required_text(reason, "reason")
    except ValueError as exc:
        return {**base, "status": "blocked", "blocking_reasons": [str(exc)], "mutations_performed": 0}

    input_fingerprint = _hash(_input_payload())
    operation_key = f"legacy_lineage_remediation:{LIFECYCLE_REMEDIATION_PLAN_VERSION}:{input_fingerprint}"
    existing = find_lifecycle_snapshot_by_operation_key(
        conn,
        operation_key=operation_key,
        normalize_required_text=normalize_required_text,
        row_to_dict=row_to_dict,
    )
    if existing is not None:
        if existing.get("operation_type") != "legacy_lineage_remediation":
            return {**base, "status": "blocked", "blocking_reasons": ["operation_integrity_error"], "mutations_performed": 0}
        if existing.get("preview_hash") != expected_hash or existing.get("candidate_set_fingerprint") != expected_candidate:
            return {**base, "status": "blocked", "blocking_reasons": ["operation_integrity_error"], "mutations_performed": 0}
        current = _context(conn)
        if existing.get("status") == "applied" and _current_matches_snapshot(current, existing, phase="after"):
            return {**base, "status": "already_applied", "run_id": int(existing["id"]), "operation_key": operation_key, "mutations_performed": 0}
        return {**base, "status": "blocked", "run_id": int(existing["id"]), "blocking_reasons": ["operation_integrity_error"], "mutations_performed": 0}

    preview = preview_memory_lifecycle_remediation_payload(conn, plan_version=plan_version, include_debug=False)
    if preview["status"] != "preview_ready":
        return {**base, "status": "blocked", "blocking_reasons": preview.get("blocking_reasons", []), "mutations_performed": 0}
    if expected_hash != preview["preview_hash"]:
        return {**base, "status": "stale_preview", "blocking_reasons": ["expected_preview_hash_mismatch"], "mutations_performed": 0}
    if expected_candidate != preview["candidate_set_fingerprint"]:
        return {**base, "status": "stale_preview", "blocking_reasons": ["expected_candidate_set_fingerprint_mismatch"], "mutations_performed": 0}

    backup_helper = create_backup or create_verified_lifecycle_remediation_backup
    try:
        backup_manifest = backup_helper(
            conn=conn,
            backups_root=backups_root,
            expected_candidate_set_fingerprint=expected_candidate,
            operation_key=operation_key,
            utc_now_iso=utc_now_iso,
        )
    except Exception as exc:
        return {**base, "status": "backup_failed", "error": str(exc), "blocking_reasons": ["trusted_online_backup_failed"], "mutations_performed": 0}

    applied_at = utc_now_iso()
    hook = failure_hook or (lambda stage: None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh_preview = preview_memory_lifecycle_remediation_payload(conn, plan_version=plan_version, include_debug=False)
        if (
            fresh_preview.get("status") != "preview_ready"
            or fresh_preview.get("preview_hash") != expected_hash
            or fresh_preview.get("candidate_set_fingerprint") != expected_candidate
        ):
            raise LifecycleRemediationBlocked("stale_preview_after_backup")
        before_context = _context(conn)
        predicted_run_id = int(
            conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM memory_lifecycle_snapshots").fetchone()[0]
        )
        desired, _, desired_blockers = _desired_state(before_context)
        if desired_blockers:
            raise LifecycleRemediationBlocked(desired_blockers[0])
        desired_by_id = {int(row["id"]): row for row in desired}
        updated_ids: list[int] = []
        for memory_id in MEMORY_IDS:
            current = before_context["memories_by_id"][memory_id]
            target = desired_by_id[memory_id]
            semantic_fields = RESTORABLE_FIELDS[:7]
            if not any(current.get(field) != target.get(field) for field in semantic_fields):
                continue
            conn.execute(
                """
                UPDATE memories
                SET project_key=?, supersedes_memory_id=?, superseded_by_memory_id=?,
                    state_code=?, memory_v2_status=?, valid_to=?, expired_due_at=?,
                    updated_at=?, last_accessed_at=?, validation_source=?
                WHERE id=?
                """,
                (
                    target.get("project_key"), target.get("supersedes_memory_id"), target.get("superseded_by_memory_id"),
                    target.get("state_code"), target.get("memory_v2_status"), target.get("valid_to"), target.get("expired_due_at"),
                    applied_at, applied_at, "memory_v3_legacy_lineage_remediation", memory_id,
                ),
            )
            updated_ids.append(memory_id)
            hook(f"memory_update:{memory_id}")

        created_events: list[dict[str, Any]] = []
        for head_id in HEAD_IDS:
            old_ids = [old for new, old, _ in PAIRS if new == head_id]
            link_ids = [link for new, _, link in PAIRS if new == head_id]
            created_events.append(
                insert_memory_event(
                    conn,
                    memory_id=head_id,
                    event_type="version.legacy_lineage_remediation_applied",
                    payload={"operation_key": operation_key, "plan_version": plan_version, "remediation_run_id": predicted_run_id, "reused_link_ids": link_ids, "old_memory_ids": old_ids, "applied_at": applied_at, "applied_by": actor},
                )
            )
            hook(f"event:{head_id}")
        successor_by_old = {old: new for new, old, _ in PAIRS}
        for old_id in OLD_IDS:
            created_events.append(
                insert_memory_event(
                    conn,
                    memory_id=old_id,
                    event_type="version.legacy_superseded_state_repaired",
                    payload={"operation_key": operation_key, "plan_version": plan_version, "new_memory_id": successor_by_old[old_id], "effective_superseded_at": desired_by_id[old_id]["valid_to"], "remediated_at": applied_at, "applied_by": actor},
                )
            )
            hook(f"event:{old_id}")
        created_events.append(
            insert_memory_event(
                conn,
                memory_id=396,
                event_type="project_key.legacy_assignment_repaired",
                payload={"operation_key": operation_key, "plan_version": plan_version, "from_project_key": "demo-project", "to_project_key": "mapi", "remediated_at": applied_at, "applied_by": actor},
            )
        )
        hook("event:project_key")

        after_context = _context(conn)
        post_integrity = _integrity(after_context["memories"], after_context["links"])
        if post_integrity.get("summary", {}).get("critical_issues", 1) != 0:
            raise LifecycleRemediationBlocked("post_write_integrity_not_clean")
        created_event_ids = [int(event["id"]) for event in created_events]
        created_event_id_set = set(created_event_ids)
        created_event_ledger = [
            event
            for event in after_context["target_event_ledger"]
            if event["id"] in created_event_id_set
        ]
        expected_after_ledger = sorted(
            before_context["target_event_ledger"] + created_event_ledger,
            key=lambda event: event["id"],
        )
        if expected_after_ledger != after_context["target_event_ledger"]:
            raise LifecycleRemediationBlocked("apply_created_event_ledger_mismatch")
        event_snapshot = {
            "schema_version": LIFECYCLE_REMEDIATION_SNAPSHOT_SCHEMA_VERSION,
            "ledger_schema_version": TARGET_EVENT_LEDGER_SCHEMA_VERSION,
            "before_event_counts": before_context["event_counts"],
            "before_target_event_ledger": before_context["target_event_ledger"],
            "before_target_event_ledger_fingerprint": before_context["target_event_ledger_fingerprint"],
            "before_target_event_ids": [event["id"] for event in before_context["target_event_ledger"]],
            "before_max_target_event_id": max(
                (event["id"] for event in before_context["target_event_ledger"]), default=None
            ),
            "created_event_ids": created_event_ids,
            "created_event_ledger": created_event_ledger,
            "created_event_ledger_fingerprint": _event_ledger_fingerprint(created_event_ledger),
            "expected_after_target_event_ledger_fingerprint": _event_ledger_fingerprint(expected_after_ledger),
            "created_event_types": dict(Counter(str(event["event_type"]) for event in created_events)),
            "backup_manifest": backup_manifest,
        }
        snapshot = create_lifecycle_snapshot_payload(
            conn,
            operation_key=operation_key,
            operation_type="legacy_lineage_remediation",
            new_memory_id=497,
            old_memory_id=396,
            relation_kind="legacy_chain_repair",
            reason=normalized_reason,
            input_fingerprint=preview["input_fingerprint"],
            candidate_set_fingerprint=expected_candidate,
            preview_hash=expected_hash,
            before_snapshot=_snapshot_memories(before_context),
            after_snapshot=_snapshot_memories(after_context),
            link_snapshot={"schema_version": LIFECYCLE_REMEDIATION_SNAPSHOT_SCHEMA_VERSION, "before": before_context["links"], "after": after_context["links"], "link_actions": [{"link_id": link_id, "link_action": "reused"} for link_id in LINK_IDS], "created_link_ids": [], "archived_link_ids": []},
            event_snapshot=event_snapshot,
            applied_at=applied_at,
            applied_by=actor,
            apply_note=normalize_optional_text(reason),
            utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
        )
        if int(snapshot["id"]) != predicted_run_id:
            raise LifecycleRemediationBlocked("remediation_run_id_allocation_mismatch")
        hook("snapshot")
        conn.commit()
    except LifecycleRemediationBlocked as exc:
        conn.rollback()
        return {**base, "status": "stale_preview" if str(exc) == "stale_preview_after_backup" else "apply_failed", "error": str(exc), "blocking_reasons": [str(exc)], "mutations_performed": 0}
    except Exception as exc:
        conn.rollback()
        return {**base, "status": "apply_failed", "error": str(exc), "blocking_reasons": ["atomic_apply_failed"], "mutations_performed": 0}
    return {
        **base,
        "status": "applied",
        "run_id": int(snapshot["id"]),
        "operation_key": operation_key,
        "preview_hash": expected_hash,
        "candidate_set_fingerprint": expected_candidate,
        "updated_memory_ids": updated_ids,
        "reused_link_ids": list(LINK_IDS),
        "created_event_ids": [int(event["id"]) for event in created_events],
        "backup_manifest": backup_manifest,
        "mutations_performed": len(updated_ids) + len(created_events) + 1,
    }


def get_memory_lifecycle_remediation_run_payload(
    conn: Any,
    *,
    run_id: int,
    include_debug: bool = False,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    try:
        snapshot = get_lifecycle_snapshot_payload(conn, snapshot_id=int(run_id), row_to_dict=row_to_dict)
    except FileNotFoundError as exc:
        return {"status": "not_found", "schema_version": LIFECYCLE_REMEDIATION_RUN_SCHEMA_VERSION, "run_id": int(run_id), "error": str(exc)}
    if snapshot.get("operation_type") != "legacy_lineage_remediation":
        return {"status": "blocked", "schema_version": LIFECYCLE_REMEDIATION_RUN_SCHEMA_VERSION, "run_id": int(run_id), "blocking_reasons": ["not_lifecycle_remediation_run"]}
    result = {
        "status": "ok",
        "schema_version": LIFECYCLE_REMEDIATION_RUN_SCHEMA_VERSION,
        "run_id": int(snapshot["id"]),
        "operation_key": snapshot["operation_key"],
        "operation_type": snapshot["operation_type"],
        "run_status": snapshot["status"],
        "plan_version": snapshot["before_snapshot"].get("plan_version"),
        "preview_hash": snapshot["preview_hash"],
        "candidate_set_fingerprint": snapshot["candidate_set_fingerprint"],
        "applied_at": snapshot.get("applied_at"),
        "applied_by": snapshot.get("applied_by"),
        "rolled_back_at": snapshot.get("rolled_back_at"),
        "rolled_back_by": snapshot.get("rolled_back_by"),
        "reused_link_ids": list(LINK_IDS),
        "safety": {"read_only": True, "mutations_performed": 0},
    }
    if include_debug:
        result["debug"] = {key: snapshot.get(key) for key in ("before_snapshot", "after_snapshot", "link_snapshot", "event_snapshot", "rollback_snapshot")}
    return result


def preview_memory_lifecycle_remediation_rollback_payload(
    conn: Any,
    *,
    run_id: int,
    include_debug: bool = False,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    base = {"schema_version": LIFECYCLE_REMEDIATION_ROLLBACK_PREVIEW_SCHEMA_VERSION, "run_id": int(run_id)}
    try:
        snapshot = get_lifecycle_snapshot_payload(conn, snapshot_id=int(run_id), row_to_dict=row_to_dict)
    except FileNotFoundError as exc:
        return {**base, "status": "blocked", "blocking_reasons": ["run_not_found"], "error": str(exc), "safety": {"read_only": True, "mutations_performed": 0}}
    if snapshot.get("operation_type") != "legacy_lineage_remediation":
        return {**base, "status": "blocked", "blocking_reasons": ["not_lifecycle_remediation_run"], "safety": {"read_only": True, "mutations_performed": 0}}
    if snapshot.get("status") == "rolled_back":
        return {**base, "status": "already_rolled_back", "blocking_reasons": [], "safety": {"read_only": True, "mutations_performed": 0}}
    if snapshot.get("status") != "applied":
        return {**base, "status": "blocked", "blocking_reasons": ["run_not_applied"], "safety": {"read_only": True, "mutations_performed": 0}}
    context = _context(conn)
    blockers: list[str] = []
    if context["memories"] != snapshot["after_snapshot"]["memories"]:
        blockers.append("current_memory_state_drift")
    if context["links"] != snapshot["link_snapshot"]["after"]:
        blockers.append("current_link_state_drift")
    event_snapshot = snapshot.get("event_snapshot") or {}
    before_event_ledger = event_snapshot.get("before_target_event_ledger")
    created_event_ledger = event_snapshot.get("created_event_ledger")
    if not isinstance(before_event_ledger, list) or not isinstance(created_event_ledger, list):
        blockers.append("intervening_target_event_detected")
        expected_event_ledger: list[dict[str, Any]] = []
    else:
        expected_event_ledger = sorted(
            before_event_ledger + created_event_ledger,
            key=lambda event: event.get("id", -1),
        )
        if (
            context["target_event_ledger"] != expected_event_ledger
            or _event_ledger_fingerprint(expected_event_ledger)
            != event_snapshot.get("expected_after_target_event_ledger_fingerprint")
        ):
            blockers.append("intervening_target_event_detected")
    planned_restoration = [
        {
            "memory_id": int(before["id"]),
            "fields": {
                field: {"before": after.get(field), "after": before.get(field)}
                for field in RESTORABLE_FIELDS
                if before.get(field) != after.get(field)
            },
        }
        for before, after in zip(snapshot["before_snapshot"]["memories"], snapshot["after_snapshot"]["memories"])
        if any(before.get(field) != after.get(field) for field in RESTORABLE_FIELDS)
    ]
    candidate = {
        "run_id": int(run_id),
        "current_memories": context["memories"],
        "current_links": context["links"],
        "target_event_ledger_schema_version": TARGET_EVENT_LEDGER_SCHEMA_VERSION,
        "target_event_ledger": context["target_event_ledger"],
        "expected_target_event_ledger_fingerprint": _event_ledger_fingerprint(expected_event_ledger),
        "snapshot_status": snapshot["status"],
    }
    fingerprint = _hash(candidate)
    planned_events = {
        "version.legacy_lineage_remediation_rolled_back": list(HEAD_IDS),
        "version.legacy_superseded_state_restored": list(OLD_IDS),
        "project_key.legacy_assignment_restored": [396],
    }
    status = "blocked" if blockers else "preview_ready"
    preview_hash = _hash({"run_id": int(run_id), "rollback_candidate_set_fingerprint": fingerprint, "planned_restoration": planned_restoration, "planned_rollback_events": planned_events, "blocking_reasons": blockers, "status": status})
    result = {
        **base,
        "status": status,
        "operation_key": snapshot["operation_key"],
        "rollback_candidate_set_fingerprint": fingerprint,
        "rollback_preview_hash": preview_hash,
        "hash_algorithm": MEMORY_V3_HASH_ALGORITHM,
        "planned_restoration": planned_restoration,
        "planned_rollback_events": planned_events,
        "blocking_reasons": blockers,
        "safety": {"read_only": True, "mutations_performed": 0},
    }
    if include_debug:
        result["debug"] = {
            "event_counts": context["event_counts"],
            "target_event_ledger_fingerprint": context["target_event_ledger_fingerprint"],
            "expected_target_event_ledger_fingerprint": _event_ledger_fingerprint(expected_event_ledger),
        }
    return result


def rollback_memory_lifecycle_remediation_payload(
    conn: Any,
    *,
    run_id: int,
    expected_rollback_preview_hash: str,
    rolled_back_by: str | None,
    notes: str | None,
    utc_now_iso: Callable[[], str],
    normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]],
    insert_memory_event: Callable[..., dict[str, Any]],
    failure_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    base = {"schema_version": LIFECYCLE_REMEDIATION_ROLLBACK_SCHEMA_VERSION, "run_id": int(run_id)}
    try:
        expected_hash = normalize_required_text(expected_rollback_preview_hash, "expected_rollback_preview_hash")
        actor = normalize_required_text(rolled_back_by, "rolled_back_by")
    except ValueError as exc:
        return {**base, "status": "blocked", "blocking_reasons": [str(exc)], "mutations_performed": 0}
    try:
        snapshot = get_lifecycle_snapshot_payload(conn, snapshot_id=int(run_id), row_to_dict=row_to_dict)
    except FileNotFoundError:
        return {**base, "status": "blocked", "blocking_reasons": ["run_not_found"], "mutations_performed": 0}
    if snapshot.get("operation_type") != "legacy_lineage_remediation":
        return {**base, "status": "blocked", "blocking_reasons": ["not_lifecycle_remediation_run"], "mutations_performed": 0}
    if snapshot.get("status") == "rolled_back":
        return {**base, "status": "already_rolled_back", "blocking_reasons": [], "mutations_performed": 0}
    preview = preview_memory_lifecycle_remediation_rollback_payload(conn, run_id=run_id, row_to_dict=row_to_dict)
    if preview.get("status") != "preview_ready":
        return {**base, "status": "blocked", "blocking_reasons": preview.get("blocking_reasons", []), "mutations_performed": 0}
    if preview["rollback_preview_hash"] != expected_hash:
        return {**base, "status": "stale_rollback_preview", "blocking_reasons": ["expected_rollback_preview_hash_mismatch"], "mutations_performed": 0}
    rolled_back_at = utc_now_iso()
    hook = failure_hook or (lambda stage: None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh = preview_memory_lifecycle_remediation_rollback_payload(conn, run_id=run_id, row_to_dict=row_to_dict)
        if fresh.get("status") != "preview_ready" or fresh.get("rollback_preview_hash") != expected_hash:
            raise LifecycleRemediationBlocked("stale_rollback_preview")
        before_by_id = {int(row["id"]): row for row in snapshot["before_snapshot"]["memories"]}
        current = _context(conn)
        restored_ids: list[int] = []
        for memory_id in MEMORY_IDS:
            row = current["memories_by_id"][memory_id]
            before = before_by_id[memory_id]
            if not any(row.get(field) != before.get(field) for field in RESTORABLE_FIELDS):
                continue
            assignments = ", ".join(f"{field}=?" for field in RESTORABLE_FIELDS)
            conn.execute(
                f"UPDATE memories SET {assignments} WHERE id=?",
                tuple(before.get(field) for field in RESTORABLE_FIELDS) + (memory_id,),
            )
            restored_ids.append(memory_id)
            hook(f"memory_restore:{memory_id}")
        events: list[dict[str, Any]] = []
        for head_id in HEAD_IDS:
            events.append(insert_memory_event(conn, memory_id=head_id, event_type="version.legacy_lineage_remediation_rolled_back", payload={"operation_key": snapshot["operation_key"], "run_id": int(run_id), "rolled_back_at": rolled_back_at, "rolled_back_by": actor}))
        successor_by_old = {old: new for new, old, _ in PAIRS}
        for old_id in OLD_IDS:
            events.append(insert_memory_event(conn, memory_id=old_id, event_type="version.legacy_superseded_state_restored", payload={"operation_key": snapshot["operation_key"], "run_id": int(run_id), "new_memory_id": successor_by_old[old_id], "rolled_back_at": rolled_back_at, "rolled_back_by": actor}))
        events.append(insert_memory_event(conn, memory_id=396, event_type="project_key.legacy_assignment_restored", payload={"operation_key": snapshot["operation_key"], "run_id": int(run_id), "from_project_key": "mapi", "to_project_key": "demo-project", "rolled_back_at": rolled_back_at, "rolled_back_by": actor}))
        hook("rollback_events")
        after = _context(conn)
        if after["memories"] != snapshot["before_snapshot"]["memories"] or after["links"] != snapshot["link_snapshot"]["before"]:
            raise LifecycleRemediationBlocked("rollback_state_restoration_mismatch")
        if _hash(_integrity(after["memories"], after["links"]).get("findings", [])) != snapshot["before_snapshot"]["integrity_fingerprint"]:
            raise LifecycleRemediationBlocked("rollback_integrity_baseline_mismatch")
        rollback_snapshot = {
            "schema_version": LIFECYCLE_REMEDIATION_SNAPSHOT_SCHEMA_VERSION,
            "before_rollback_memories": snapshot["after_snapshot"]["memories"],
            "after_rollback_memories": after["memories"],
            "links": after["links"],
            "created_event_ids": [int(event["id"]) for event in events],
            "created_event_types": dict(Counter(str(event["event_type"]) for event in events)),
        }
        mark_lifecycle_snapshot_rolled_back_payload(
            conn,
            snapshot_id=int(run_id),
            rollback_preview_hash=expected_hash,
            rollback_snapshot=rollback_snapshot,
            rolled_back_at=rolled_back_at,
            rolled_back_by=actor,
            rollback_note=normalize_optional_text(notes),
            utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
        )
        hook("rollback_snapshot")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return {**base, "status": "rollback_failed", "error": str(exc), "blocking_reasons": ["atomic_rollback_failed"], "mutations_performed": 0}
    return {**base, "status": "rolled_back", "restored_memory_ids": restored_ids, "reused_link_ids": list(LINK_IDS), "created_event_ids": [int(event["id"]) for event in events], "mutations_performed": len(restored_ids) + len(events) + 1}
