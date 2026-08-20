from __future__ import annotations

import copy
import hashlib
import sqlite3

from app import db_migrations
from mapi_core.sandman.contracts import SAFETY_BLOCK, build_provider_request
from app.sandman.redaction import build_redacted_candidates


def make_conn(*, flag: str = "enabled") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db_migrations.apply_all_migrations(conn)
    if flag == "missing":
        conn.execute("DELETE FROM feature_flags WHERE flag_key='sandman_provider_v3_enabled'")
    else:
        conn.execute(
            "UPDATE feature_flags SET is_enabled=?,rollout_mode=?,read_only_mode=0,notes='fixture' "
            "WHERE flag_key='sandman_provider_v3_enabled'",
            (int(flag == "enabled"), "all" if flag == "enabled" else "off"),
        )
    conn.commit()
    return conn


def insert_memory(conn: sqlite3.Connection, content: str = "safe project fact", **values) -> int:
    base = {
        "memory_type": "project_fact", "entry_type": "fact", "truth_kind": "fact",
        "state_code": "validated", "memory_v2_status": "active", "activity_state": "active",
        "project_key": "p", "scope_code": "project", "workspace_id": 7,
        "visibility_scope": "project", "created_at": "2026-07-16T08:00:00Z",
        "updated_at": "2026-07-16T08:00:00Z",
    }
    base.update(values)
    columns = ["content", *base]
    cursor = conn.execute(
        f"INSERT INTO memories({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        [content, *base.values()],
    )
    return int(cursor.lastrowid)


def add_link(conn: sqlite3.Connection, source: int, target: int, relation: str) -> None:
    conn.execute(
        "INSERT INTO memory_links(from_memory_id,to_memory_id,relation_type,weight,origin) VALUES(?,?,?,?,?)",
        (source, target, relation, 1.0, "fixture"),
    )


def flag_parts(enabled: bool = True, *, missing: bool = False):
    flag = {"flag_key": "sandman_provider_v3_enabled", "is_implicit_default": missing}
    evaluation = {"enabled": enabled and not missing, "reason": "rollout_all" if enabled and not missing else "flag_disabled", "rollout_mode": "all" if enabled else "off"}
    return flag, evaluation


def candidate(memory_id: int, *, content="safe", artifact_kind="fact", content_hash=None, **values):
    redacted_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    original_hash = (
        hashlib.sha256(str(content_hash).encode("utf-8")).hexdigest()
        if content_hash is not None
        else hashlib.sha256(f"original-{memory_id}".encode("utf-8")).hexdigest()
    )
    base = {
        "memory_id": memory_id, "project_key": "p", "scope_code": "project", "workspace_id": 7,
        "memory_type": "project_fact", "truth_kind": "fact", "state_code": "validated",
        "artifact_kind": artifact_kind, "created_at": "2026-07-16T08:00:00Z", "updated_at": "2026-07-16T08:00:00Z",
        "content_redacted": content, "content_sha256": original_hash,
        "redacted_content_sha256": redacted_hash, "sensitivity_class": "internal",
        "redaction_applied": False, "supersedes_memory_id": None, "superseded_by_memory_id": None,
        "allowlisted_links": [],
    }
    base.update(values)
    return base


def request(candidates=None, *, allowed_actions=None, budget=8, request_id="req-1"):
    candidates = candidates or [candidate(1), candidate(2)]
    ids = sorted(item["memory_id"] for item in candidates)
    manifest = {
        "schema_version": "sandman_redaction_manifest.v1", "policy_version": "sandman_redaction_policy.v1",
        "external_data_policy": "redacted_project_only", "candidate_count_requested": len(ids),
        "candidate_count_included": len(ids), "candidate_count_excluded": 0, "included_memory_ids": ids,
        "excluded_candidates": [], "replacement_counts": {}, "truncated_memory_ids": [],
        "raw_secret_exposed": False, "full_project_dump": False,
    }
    return build_provider_request(
        request_id=request_id, provider_name="deterministic", project_key="p", scope_code="project", workspace_id=7,
        candidate_memory_ids=ids, candidates=copy.deepcopy(candidates),
        allowed_actions=allowed_actions or ["duplicate_of", "related_to", "supersedes", "contradicts", "reinforces"],
        proposal_budget=budget, redaction_manifest=manifest, safety=dict(SAFETY_BLOCK),
    )


def redaction_for(conn: sqlite3.Connection, ids: list[int]):
    q = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT * FROM memories WHERE id IN ({q}) ORDER BY id", ids).fetchall()
    return build_redacted_candidates([dict(row) for row in rows], requested_ids=ids)
