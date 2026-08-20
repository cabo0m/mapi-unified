from __future__ import annotations

"""Local self-healing contracts for Polaris memory.

The module has two deliberately different paths:
- deterministic structural repairs may run unattended after a verified backup;
- ambiguous semantic repairs are queued for the connected assistant model and
  require user consent before any semantic branch is discarded from current state.

No memory content is deleted by this module.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

from mapi_core.memory.current_state import (
    apply_direct_supersession_transition,
    get_memory_current_state_inventory_payload,
)

SELF_HEALING_SCHEMA = "memory_self_healing.v1"
SELF_HEALING_NOTICE_SCHEMA = "memory_self_healing_notice.v1"
SELF_HEALING_MODEL_MIN_CONFIDENCE = 0.80

ISSUE_STATUSES = {
    "open",
    "awaiting_model",
    "awaiting_user",
    "approved",
    "repaired",
    "dismissed",
    "failed",
}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load_json(value: Any, default: Any) -> Any:
    if value in {None, ""}:
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _fingerprint(issue: Mapping[str, Any]) -> str:
    basis = {
        "issue_code": str(issue.get("issue_code") or "unknown"),
        "new_memory_id": issue.get("new_memory_id"),
        "old_memory_id": issue.get("old_memory_id"),
        "memory_ids": sorted(int(item) for item in (issue.get("memory_ids") or []) if str(item).isdigit()),
        "candidate_memory_ids": sorted(
            int(item) for item in (issue.get("candidate_memory_ids") or []) if str(item).isdigit()
        ),
    }
    return "sha256:" + hashlib.sha256(_json(basis).encode("utf-8")).hexdigest()


def ensure_self_healing_schema(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_self_healing_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            issue_kind TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
            repair_class TEXT NOT NULL CHECK (repair_class IN ('low','medium','high')),
            status TEXT NOT NULL CHECK (
                status IN ('open','awaiting_model','awaiting_user','approved','repaired','dismissed','failed')
            ),
            project_key TEXT,
            memory_ids_json TEXT NOT NULL CHECK (json_valid(memory_ids_json)),
            evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
            proposal_json TEXT CHECK (proposal_json IS NULL OR json_valid(proposal_json)),
            model_confidence REAL,
            model_rationale TEXT,
            requires_user_consent INTEGER NOT NULL DEFAULT 0 CHECK (requires_user_consent IN (0,1)),
            user_decision TEXT,
            repair_result_json TEXT CHECK (repair_result_json IS NULL OR json_valid(repair_result_json)),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            resolved_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_self_healing_status "
        "ON memory_self_healing_issues(status, severity, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_self_healing_project "
        "ON memory_self_healing_issues(project_key, status)"
    )


def _issue_memory_ids(issue: Mapping[str, Any]) -> list[int]:
    values: set[int] = set()
    for key in ("new_memory_id", "old_memory_id"):
        value = issue.get(key)
        if value is not None:
            values.add(int(value))
    for key in ("memory_ids", "candidate_memory_ids"):
        for value in issue.get(key) or []:
            values.add(int(value))
    return sorted(values)


def _project_key_for_ids(conn: Any, memory_ids: list[int]) -> str | None:
    if not memory_ids:
        return None
    placeholders = ",".join("?" for _ in memory_ids)
    rows = conn.execute(
        f"SELECT DISTINCT project_key FROM memories WHERE id IN ({placeholders})",
        tuple(memory_ids),
    ).fetchall()
    values = {str(row[0]).strip() for row in rows if row[0] is not None and str(row[0]).strip()}
    return next(iter(values)) if len(values) == 1 else None


def _classify(issue: Mapping[str, Any]) -> tuple[str, str, bool]:
    code = str(issue.get("issue_code") or "unknown")
    severity = str(issue.get("severity") or "info")
    deterministic = {
        "half_supersession",
        "supersedes_missing_target",
        "superseded_by_missing_target",
        "lineage_link_missing_memory",
        "cross_domain_pointer_ignored",
        "cross_domain_lineage_ignored",
    }
    if code in deterministic:
        return severity, "medium", False
    if code in {"multiple_replacement_heads", "lineage_cycle"}:
        return "critical", "high", True
    if severity == "critical":
        return "critical", "high", True
    if severity == "warning":
        return "warning", "medium", False
    return "info", "low", False

def scan_self_healing_issues(conn: Any, *, limit: int = 1000) -> dict[str, Any]:
    ensure_self_healing_schema(conn)
    inventory = get_memory_current_state_inventory_payload(
        conn,
        project_key=None,
        limit=max(1, min(int(limit), 1000)),
        include_debug=False,
    )
    now = _now()
    seen: set[str] = set()
    created = 0
    refreshed = 0
    for issue in inventory.get("issues") or []:
        fingerprint = _fingerprint(issue)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        memory_ids = _issue_memory_ids(issue)
        severity, repair_class, requires_consent = _classify(issue)
        initial_status = "awaiting_model" if repair_class == "high" else "open"
        project_key = _project_key_for_ids(conn, memory_ids)
        existing = conn.execute(
            "SELECT id,status FROM memory_self_healing_issues WHERE fingerprint=?",
            (fingerprint,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO memory_self_healing_issues (
                    fingerprint,issue_kind,severity,repair_class,status,project_key,
                    memory_ids_json,evidence_json,requires_user_consent,
                    first_seen_at,last_seen_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    fingerprint,
                    str(issue.get("issue_code") or "unknown"),
                    severity,
                    repair_class,
                    initial_status,
                    project_key,
                    _json(memory_ids),
                    _json(dict(issue)),
                    1 if requires_consent else 0,
                    now,
                    now,
                    now,
                ),
            )
            created += 1
        elif str(existing["status"]) not in {"repaired", "dismissed"}:
            conn.execute(
                """
                UPDATE memory_self_healing_issues
                SET severity=?,repair_class=?,project_key=?,memory_ids_json=?,evidence_json=?,
                    requires_user_consent=?,last_seen_at=?,updated_at=?
                WHERE id=?
                """,
                (
                    severity,
                    repair_class,
                    project_key,
                    _json(memory_ids),
                    _json(dict(issue)),
                    1 if requires_consent else 0,
                    now,
                    now,
                    int(existing["id"]),
                ),
            )
            refreshed += 1

    open_rows = conn.execute(
        """
        SELECT id,fingerprint FROM memory_self_healing_issues
        WHERE status IN ('open','awaiting_model','awaiting_user','approved')
        """
    ).fetchall()
    auto_dismissed = 0
    for row in open_rows:
        if str(row["fingerprint"]) not in seen:
            conn.execute(
                """
                UPDATE memory_self_healing_issues
                SET status='dismissed',user_decision='no_longer_present',resolved_at=?,updated_at=?
                WHERE id=?
                """,
                (now, now, int(row["id"])),
            )
            auto_dismissed += 1
    return {
        "schema": SELF_HEALING_SCHEMA,
        "status": "ok",
        "inventory_status": inventory.get("status"),
        "issue_count": len(seen),
        "created_count": created,
        "refreshed_count": refreshed,
        "auto_dismissed_count": auto_dismissed,
    }


def _row_payload(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["memory_ids"] = _load_json(result.pop("memory_ids_json", None), [])
    result["evidence"] = _load_json(result.pop("evidence_json", None), {})
    result["proposal"] = _load_json(result.pop("proposal_json", None), None)
    result["repair_result"] = _load_json(result.pop("repair_result_json", None), None)
    result["requires_user_consent"] = bool(result.get("requires_user_consent"))
    return result


def get_self_healing_status(conn: Any) -> dict[str, Any]:
    ensure_self_healing_schema(conn)
    rows = conn.execute(
        """
        SELECT * FROM memory_self_healing_issues
        WHERE status IN ('open','awaiting_model','awaiting_user','approved','failed')
        ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id
        """
    ).fetchall()
    items = [_row_payload(row) for row in rows]
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    user_required = [item for item in items if item["status"] == "awaiting_user"]
    model_required = [item for item in items if item["status"] == "awaiting_model"]
    return {
        "schema": SELF_HEALING_SCHEMA,
        "status": "attention" if items else "healthy",
        "issue_count": len(items),
        "counts_by_status": counts,
        "awaiting_model_count": len(model_required),
        "awaiting_user_count": len(user_required),
        "next_issue_id": int((user_required or model_required or items)[0]["id"]) if items else None,
        "silent_when_healthy": True,
    }


def get_self_healing_issue(conn: Any, *, issue_id: int, include_content: bool = True) -> dict[str, Any]:
    ensure_self_healing_schema(conn)
    row = conn.execute("SELECT * FROM memory_self_healing_issues WHERE id=?", (int(issue_id),)).fetchone()
    if row is None:
        raise ValueError("self_healing_issue_not_found")
    issue = _row_payload(row)
    memory_ids = [int(value) for value in issue["memory_ids"]]
    memories: list[dict[str, Any]] = []
    if memory_ids:
        placeholders = ",".join("?" for _ in memory_ids)
        fields = (
            "id,summary_short,title,memory_type,project_key,scope_code,state_code,memory_v2_status,"
            "activity_state,supersedes_memory_id,superseded_by_memory_id,source_event_ref,created_at,"
            "importance_level,requires_user_confirmation"
        )
        if include_content:
            fields += ",content"
        rows = conn.execute(
            f"SELECT {fields} FROM memories WHERE id IN ({placeholders}) ORDER BY id",
            tuple(memory_ids),
        ).fetchall()
        memories = [dict(item) for item in rows]
    return {"schema": SELF_HEALING_SCHEMA, "status": "ok", "issue": issue, "memories": memories}


def _protected(memory: Mapping[str, Any]) -> bool:
    return bool(memory.get("requires_user_confirmation")) or str(memory.get("importance_level") or "").casefold() == "critical"


def _same_boundary(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(field) == right.get(field) for field in ("project_key", "scope_code", "workspace_id"))


def _safe_half_supersession(conn: Any, issue: Mapping[str, Any]) -> tuple[bool, list[str]]:
    new_id = int(issue["new_memory_id"])
    old_id = int(issue["old_memory_id"])
    rows = conn.execute("SELECT * FROM memories WHERE id IN (?,?)", (new_id, old_id)).fetchall()
    by_id = {int(row["id"]): dict(row) for row in rows}
    new = by_id.get(new_id)
    old = by_id.get(old_id)
    reasons: list[str] = []
    if new is None or old is None:
        reasons.append("missing_endpoint")
        return False, reasons
    if int(new.get("supersedes_memory_id") or 0) != old_id:
        reasons.append("forward_pointer_drift")
    child_rows = conn.execute("SELECT id FROM memories WHERE supersedes_memory_id=?", (old_id,)).fetchall()
    children = sorted(int(row[0]) for row in child_rows)
    if children != [new_id]:
        reasons.append("not_unique_replacement")
    if old.get("superseded_by_memory_id") not in {None, new_id}:
        reasons.append("reverse_pointer_conflict")
    if not _same_boundary(new, old):
        reasons.append("boundary_mismatch")
    conflicting_links = conn.execute(
        """
        SELECT COUNT(*) FROM memory_links
        WHERE to_memory_id=? AND relation_type='supersedes' AND archived_at IS NULL AND from_memory_id<>?
        """,
        (old_id, new_id),
    ).fetchone()[0]
    if int(conflicting_links or 0) > 0:
        reasons.append("conflicting_active_link")
    try:
        new_created = datetime.fromisoformat(str(new.get("created_at") or "").replace("Z", "+00:00"))
        old_created = datetime.fromisoformat(str(old.get("created_at") or "").replace("Z", "+00:00"))
        if new_created < old_created:
            reasons.append("chronology_violation")
    except ValueError:
        reasons.append("invalid_timestamp")
    return not reasons, reasons


def _restore_orphaned_superseded_memory(conn: Any, *, memory_id: int, now: str) -> None:
    row = conn.execute("SELECT * FROM memories WHERE id=?", (int(memory_id),)).fetchone()
    if row is None:
        return
    memory = dict(row)
    if memory.get("archived_at") is not None:
        raise ValueError("independently_archived_memory")
    children = conn.execute(
        "SELECT id FROM memories WHERE supersedes_memory_id=?", (int(memory_id),)
    ).fetchall()
    if children:
        raise ValueError("live_replacement_still_exists")
    conn.execute(
        """
        UPDATE memories
        SET superseded_by_memory_id=NULL,
            state_code=CASE WHEN state_code='superseded' THEN 'validated' ELSE state_code END,
            memory_v2_status=CASE WHEN memory_v2_status='superseded' THEN 'active' ELSE memory_v2_status END,
            activity_state=CASE WHEN activity_state='superseded' THEN 'active' ELSE activity_state END,
            valid_to=CASE WHEN state_code='superseded' OR memory_v2_status='superseded' THEN NULL ELSE valid_to END,
            updated_at=?
        WHERE id=?
        """,
        (now, int(memory_id)),
    )


def _repair_deterministic_issue(
    conn: Any,
    *,
    item: Mapping[str, Any],
    insert_event: Callable[..., Any] | None,
) -> dict[str, Any]:
    evidence = item["evidence"]
    code = str(item["issue_kind"])
    now = _now()
    touched: list[int] = []

    if code == "half_supersession":
        safe, reasons = _safe_half_supersession(conn, evidence)
        if not safe:
            raise ValueError(";".join(reasons))
        result = apply_direct_supersession_transition(
            conn,
            new_memory_id=int(evidence["new_memory_id"]),
            old_memory_id=int(evidence["old_memory_id"]),
            relation="supersedes",
            now_iso=_now,
            insert_event=insert_event,
            source="polaris_self_healing",
        )
        return {"action": "complete_half_supersession", **result, "content_deleted": False}

    new_id = int(evidence.get("new_memory_id") or 0)
    old_id = int(evidence.get("old_memory_id") or 0)
    new = conn.execute("SELECT * FROM memories WHERE id=?", (new_id,)).fetchone() if new_id else None
    old = conn.execute("SELECT * FROM memories WHERE id=?", (old_id,)).fetchone() if old_id else None

    if code == "supersedes_missing_target":
        if new is None:
            raise ValueError("source_memory_missing")
        if old is not None:
            raise ValueError("target_reappeared")
        if int(new["supersedes_memory_id"] or 0) != old_id:
            return {"action": "no_op_issue_already_resolved", "content_deleted": False}
        conn.execute("UPDATE memories SET supersedes_memory_id=NULL,updated_at=? WHERE id=?", (now, new_id))
        conn.execute(
            "UPDATE memory_links SET archived_at=? WHERE from_memory_id=? AND to_memory_id=? AND archived_at IS NULL",
            (now, new_id, old_id),
        )
        touched.append(new_id)
        action = "clear_missing_supersedes_target"
    elif code == "superseded_by_missing_target":
        if old is None:
            raise ValueError("source_memory_missing")
        if new is not None:
            raise ValueError("target_reappeared")
        if int(old["superseded_by_memory_id"] or 0) != new_id:
            return {"action": "no_op_issue_already_resolved", "content_deleted": False}
        _restore_orphaned_superseded_memory(conn, memory_id=old_id, now=now)
        conn.execute(
            "UPDATE memory_links SET archived_at=? WHERE from_memory_id=? AND to_memory_id=? AND archived_at IS NULL",
            (now, new_id, old_id),
        )
        touched.append(old_id)
        action = "restore_orphaned_superseded_memory"
    elif code == "lineage_link_missing_memory":
        conn.execute(
            """
            UPDATE memory_links SET archived_at=?
            WHERE from_memory_id=? AND to_memory_id=? AND archived_at IS NULL
            """,
            (now, new_id, old_id),
        )
        touched.extend(value for value in (new_id, old_id) if conn.execute("SELECT 1 FROM memories WHERE id=?", (value,)).fetchone())
        action = "archive_dangling_lineage_link"
    elif code == "cross_domain_lineage_ignored":
        if new is None or old is None:
            raise ValueError("cross_domain_endpoint_missing")
        conn.execute(
            """
            UPDATE memory_links SET archived_at=?
            WHERE from_memory_id=? AND to_memory_id=? AND archived_at IS NULL
            """,
            (now, new_id, old_id),
        )
        touched.extend([new_id, old_id])
        action = "archive_cross_domain_lineage_link"
    elif code == "cross_domain_pointer_ignored":
        if new is None or old is None:
            raise ValueError("cross_domain_endpoint_missing")
        if _same_boundary(dict(new), dict(old)):
            raise ValueError("boundary_is_no_longer_cross_domain")
        if int(new["supersedes_memory_id"] or 0) == old_id:
            conn.execute("UPDATE memories SET supersedes_memory_id=NULL,updated_at=? WHERE id=?", (now, new_id))
        if int(old["superseded_by_memory_id"] or 0) == new_id:
            conn.execute("UPDATE memories SET superseded_by_memory_id=NULL,updated_at=? WHERE id=?", (now, old_id))
            valid_children = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE supersedes_memory_id=? AND id<>?", (old_id, new_id)
            ).fetchone()[0]
            if int(valid_children or 0) == 0 and str(old["state_code"] or "") == "superseded":
                _restore_orphaned_superseded_memory(conn, memory_id=old_id, now=now)
        conn.execute(
            """
            UPDATE memory_links SET archived_at=?
            WHERE from_memory_id=? AND to_memory_id=? AND archived_at IS NULL
            """,
            (now, new_id, old_id),
        )
        touched.extend([new_id, old_id])
        action = "sever_cross_domain_lineage"
    else:
        raise ValueError("unsupported_deterministic_issue")

    if insert_event is not None:
        for memory_id in sorted(set(touched)):
            insert_event(
                conn,
                memory_id=memory_id,
                event_type="memory.self_healing.technical_repair",
                payload={"issue_id": int(item["id"]), "issue_kind": code, "action": action},
            )
    return {
        "action": action,
        "issue_kind": code,
        "touched_memory_ids": sorted(set(touched)),
        "content_deleted": False,
    }


def repair_deterministic_issues(
    conn: Any,
    *,
    insert_event: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    ensure_self_healing_schema(conn)
    rows = conn.execute(
        """
        SELECT * FROM memory_self_healing_issues
        WHERE status='open' AND repair_class IN ('low','medium')
        ORDER BY id
        """
    ).fetchall()
    repaired: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    now = _now()
    high_rows = conn.execute(
        "SELECT id,memory_ids_json FROM memory_self_healing_issues WHERE status='awaiting_model' AND repair_class='high'"
    ).fetchall()
    high_sets = [
        (int(row["id"]), set(int(value) for value in _load_json(row["memory_ids_json"], [])))
        for row in high_rows
    ]
    for row in rows:
        item = _row_payload(row)
        item_ids = set(int(value) for value in item.get("memory_ids") or [])
        overlapping_high = [issue_id for issue_id, ids in high_sets if item_ids & ids]
        if overlapping_high:
            blocked.append(
                {
                    "issue_id": int(item["id"]),
                    "reasons": ["overlapping_high_risk_semantic_issue"],
                    "related_issue_ids": overlapping_high,
                }
            )
            continue
        try:
            result = _repair_deterministic_issue(conn, item=item, insert_event=insert_event)
        except (ValueError, TypeError, KeyError) as exc:
            reasons = [str(exc) or type(exc).__name__]
            conn.execute(
                """
                UPDATE memory_self_healing_issues
                SET status='awaiting_model',repair_class='high',requires_user_consent=1,
                    repair_result_json=?,updated_at=? WHERE id=?
                """,
                (_json({"blocked_reasons": reasons}), now, int(item["id"])),
            )
            blocked.append({"issue_id": int(item["id"]), "reasons": reasons})
            continue
        conn.execute(
            """
            UPDATE memory_self_healing_issues
            SET status='repaired',repair_result_json=?,resolved_at=?,updated_at=? WHERE id=?
            """,
            (_json(result), now, now, int(item["id"])),
        )
        repaired.append({"issue_id": int(item["id"]), **result})
    return {
        "schema": SELF_HEALING_SCHEMA,
        "status": "ok",
        "repaired_count": len(repaired),
        "blocked_count": len(blocked),
        "repaired": repaired,
        "blocked": blocked,
    }

def propose_self_healing_resolution(
    conn: Any,
    *,
    issue_id: int,
    selected_memory_id: int,
    confidence: float,
    rationale: str,
) -> dict[str, Any]:
    ensure_self_healing_schema(conn)
    row = conn.execute("SELECT * FROM memory_self_healing_issues WHERE id=?", (int(issue_id),)).fetchone()
    if row is None:
        raise ValueError("self_healing_issue_not_found")
    issue = _row_payload(row)
    if issue["status"] not in {"awaiting_model", "awaiting_user"}:
        raise ValueError("self_healing_issue_not_model_resolvable")
    issue_kind = str(issue["issue_kind"])
    evidence = issue["evidence"]
    selected = int(selected_memory_id)
    if issue_kind == "multiple_replacement_heads":
        candidates = sorted(int(value) for value in (evidence.get("candidate_memory_ids") or []))
        if selected not in candidates:
            raise ValueError("selected_memory_is_not_candidate_head")
        proposal = {
            "action": "select_canonical_head",
            "old_memory_id": int(evidence["old_memory_id"]),
            "selected_memory_id": selected,
            "archive_noncanonical_candidate_ids": [value for value in candidates if value != selected],
            "preserve_content": True,
        }
    elif issue_kind == "lineage_cycle":
        candidates = sorted(set(int(value) for value in (evidence.get("memory_ids") or [])))
        if selected not in candidates:
            raise ValueError("selected_memory_is_not_cycle_member")
        proposal = {
            "action": "break_lineage_cycle",
            "selected_memory_id": selected,
            "archive_noncanonical_candidate_ids": [value for value in candidates if value != selected],
            "preserve_content": True,
        }
    else:
        raise ValueError("self_healing_issue_requires_specialized_resolution")
    normalized_confidence = max(0.0, min(1.0, float(confidence)))
    if normalized_confidence < SELF_HEALING_MODEL_MIN_CONFIDENCE:
        raise ValueError("model_confidence_too_low_for_proposal")
    now = _now()
    conn.execute(
        """
        UPDATE memory_self_healing_issues
        SET status='awaiting_user',proposal_json=?,model_confidence=?,model_rationale=?,
            requires_user_consent=1,updated_at=? WHERE id=?
        """,
        (_json(proposal), normalized_confidence, str(rationale).strip()[:2000], now, int(issue_id)),
    )
    return {
        "schema": SELF_HEALING_SCHEMA,
        "status": "awaiting_user",
        "issue_id": int(issue_id),
        "proposal": proposal,
        "requires_user_confirmation": True,
        "user_prompt": (
            "Moja pamięć ma niespójność między wersjami tej samej informacji. "
            "Mam przygotowaną bezpieczną naprawę, która zachowa historię. Zgadzasz się, żebym ją zastosował/a?"
        ),
    }

def _apply_canonical_head_resolution(
    conn: Any,
    *,
    issue: Mapping[str, Any],
    insert_event: Callable[..., Any] | None,
) -> dict[str, Any]:
    proposal = issue.get("proposal") or {}
    old_id = int(proposal["old_memory_id"])
    selected_id = int(proposal["selected_memory_id"])
    losers = sorted(int(value) for value in proposal.get("archive_noncanonical_candidate_ids") or [])
    evidence_candidates = sorted(int(value) for value in issue["evidence"].get("candidate_memory_ids") or [])
    if sorted([selected_id, *losers]) != evidence_candidates:
        raise ValueError("self_healing_proposal_candidate_set_drift")
    live_children = sorted(
        int(row[0]) for row in conn.execute("SELECT id FROM memories WHERE supersedes_memory_id=?", (old_id,)).fetchall()
    )
    if live_children != evidence_candidates:
        raise ValueError("self_healing_candidate_set_drift")
    rows = conn.execute(
        f"SELECT * FROM memories WHERE id IN ({','.join('?' for _ in [old_id, *evidence_candidates])})",
        tuple([old_id, *evidence_candidates]),
    ).fetchall()
    by_id = {int(row["id"]): dict(row) for row in rows}
    selected = by_id.get(selected_id)
    old = by_id.get(old_id)
    if selected is None or old is None or any(value not in by_id for value in losers):
        raise ValueError("self_healing_resolution_memory_missing")
    if any(not _same_boundary(selected, by_id[value]) for value in [old_id, *losers]):
        raise ValueError("self_healing_resolution_boundary_drift")
    now = _now()
    for loser_id in losers:
        loser = by_id[loser_id]
        conn.execute(
            """
            UPDATE memories
            SET supersedes_memory_id=NULL,state_code='archived',memory_v2_status='archived',
                activity_state='archived',archived_at=COALESCE(archived_at,?),updated_at=?
            WHERE id=?
            """,
            (now, now, loser_id),
        )
        conn.execute(
            """
            UPDATE memory_links SET archived_at=?
            WHERE from_memory_id=? AND to_memory_id=? AND relation_type='supersedes' AND archived_at IS NULL
            """,
            (now, loser_id, old_id),
        )
        if insert_event is not None:
            insert_event(
                conn,
                memory_id=loser_id,
                event_type="memory.self_healing.noncanonical_branch_archived",
                payload={"issue_id": int(issue["id"]), "canonical_memory_id": selected_id, "old_memory_id": old_id},
            )
    transition = apply_direct_supersession_transition(
        conn,
        new_memory_id=selected_id,
        old_memory_id=old_id,
        relation="supersedes",
        now_iso=_now,
        insert_event=insert_event,
        source="polaris_self_healing_user_approved",
    )
    return {
        "action": "select_canonical_head",
        "canonical_memory_id": selected_id,
        "archived_noncanonical_memory_ids": losers,
        "transition": transition,
        "content_deleted": False,
    }


def _apply_cycle_resolution(
    conn: Any,
    *,
    issue: Mapping[str, Any],
    insert_event: Callable[..., Any] | None,
) -> dict[str, Any]:
    proposal = issue.get("proposal") or {}
    selected_id = int(proposal["selected_memory_id"])
    losers = sorted(int(value) for value in proposal.get("archive_noncanonical_candidate_ids") or [])
    evidence_ids = sorted(set(int(value) for value in issue["evidence"].get("memory_ids") or []))
    if sorted([selected_id, *losers]) != evidence_ids:
        raise ValueError("self_healing_cycle_candidate_set_drift")
    placeholders = ",".join("?" for _ in evidence_ids)
    rows = conn.execute(f"SELECT * FROM memories WHERE id IN ({placeholders})", tuple(evidence_ids)).fetchall()
    by_id = {int(row["id"]): dict(row) for row in rows}
    if len(by_id) != len(evidence_ids):
        raise ValueError("self_healing_cycle_memory_missing")
    selected = by_id[selected_id]
    if any(not _same_boundary(selected, by_id[value]) for value in losers):
        raise ValueError("self_healing_cycle_boundary_drift")
    inventory = get_memory_current_state_inventory_payload(conn, project_key=None, limit=1000, include_debug=False)
    live_cycles = {
        tuple(sorted(set(int(value) for value in candidate.get("memory_ids") or [])))
        for candidate in inventory.get("issues") or []
        if candidate.get("issue_code") == "lineage_cycle"
    }
    if tuple(evidence_ids) not in live_cycles:
        raise ValueError("self_healing_cycle_no_longer_present")
    now = _now()
    conn.execute(
        """
        UPDATE memories
        SET supersedes_memory_id=NULL,superseded_by_memory_id=NULL,
            state_code=CASE WHEN state_code='superseded' THEN 'validated' ELSE state_code END,
            memory_v2_status=CASE WHEN memory_v2_status='superseded' THEN 'active' ELSE memory_v2_status END,
            activity_state=CASE WHEN activity_state='superseded' THEN 'active' ELSE activity_state END,
            valid_to=CASE WHEN state_code='superseded' OR memory_v2_status='superseded' THEN NULL ELSE valid_to END,
            updated_at=?
        WHERE id=?
        """,
        (now, selected_id),
    )
    for loser_id in losers:
        conn.execute(
            """
            UPDATE memories
            SET supersedes_memory_id=NULL,superseded_by_memory_id=NULL,state_code='archived',
                memory_v2_status='archived',activity_state='archived',archived_at=COALESCE(archived_at,?),updated_at=?
            WHERE id=?
            """,
            (now, now, loser_id),
        )
    conn.execute(
        f"""
        UPDATE memory_links SET archived_at=?
        WHERE relation_type='supersedes' AND archived_at IS NULL
          AND from_memory_id IN ({placeholders}) AND to_memory_id IN ({placeholders})
        """,
        (now, *evidence_ids, *evidence_ids),
    )
    if insert_event is not None:
        for memory_id in evidence_ids:
            insert_event(
                conn,
                memory_id=memory_id,
                event_type="memory.self_healing.lineage_cycle_resolved",
                payload={
                    "issue_id": int(issue["id"]),
                    "canonical_memory_id": selected_id,
                    "archived_memory_ids": losers,
                },
            )
    return {
        "action": "break_lineage_cycle",
        "canonical_memory_id": selected_id,
        "archived_noncanonical_memory_ids": losers,
        "content_deleted": False,
    }


def confirm_self_healing_resolution(
    conn: Any,
    *,
    issue_id: int,
    approve: bool,
    insert_event: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    ensure_self_healing_schema(conn)
    row = conn.execute("SELECT * FROM memory_self_healing_issues WHERE id=?", (int(issue_id),)).fetchone()
    if row is None:
        raise ValueError("self_healing_issue_not_found")
    issue = _row_payload(row)
    if issue["status"] != "awaiting_user" or not issue.get("proposal"):
        raise ValueError("self_healing_issue_not_awaiting_user")
    now = _now()
    if not approve:
        conn.execute(
            """
            UPDATE memory_self_healing_issues
            SET status='dismissed',user_decision='rejected',resolved_at=?,updated_at=? WHERE id=?
            """,
            (now, now, int(issue_id)),
        )
        return {"schema": SELF_HEALING_SCHEMA, "status": "dismissed", "issue_id": int(issue_id)}
    action = str((issue.get("proposal") or {}).get("action") or "")
    if action == "select_canonical_head":
        result = _apply_canonical_head_resolution(conn, issue=issue, insert_event=insert_event)
    elif action == "break_lineage_cycle":
        result = _apply_cycle_resolution(conn, issue=issue, insert_event=insert_event)
    else:
        raise ValueError("self_healing_unknown_proposal_action")
    conn.execute(
        """
        UPDATE memory_self_healing_issues
        SET status='repaired',user_decision='approved',repair_result_json=?,resolved_at=?,updated_at=? WHERE id=?
        """,
        (_json(result), now, now, int(issue_id)),
    )
    return {"schema": SELF_HEALING_SCHEMA, "status": "repaired", "issue_id": int(issue_id), "result": result}


def build_self_healing_notice(conn: Any) -> dict[str, Any]:
    status = get_self_healing_status(conn)
    if status["status"] == "healthy":
        return {
            "schema": SELF_HEALING_NOTICE_SCHEMA,
            "status": "healthy",
            "visible_to_user": False,
            "user_prompt": None,
            "assistant_instruction": None,
        }
    issue_id = int(status["next_issue_id"])
    detail = get_self_healing_issue(conn, issue_id=issue_id, include_content=False)
    issue = detail["issue"]
    user_prompt: str | None = None
    if issue["status"] == "awaiting_model":
        instruction = (
            "A memory self-healing issue is waiting for model analysis. Before mentioning it to the user, "
            "call run_workshop_action area=memory action=self_healing_issue with this issue_id. "
            "If it is multiple_replacement_heads or lineage_cycle and you can identify the intended canonical memory with "
            "confidence >= 0.80, call area=memory action=self_healing_propose. Do not expose database IDs or implementation "
            "details unless asked."
        )
        visible = False
    elif issue["status"] == "awaiting_user":
        count = int(status["issue_count"])
        noun = "niespójność" if count == 1 else "niespójności"
        user_prompt = (
            f"Moja pamięć ma {count} {noun}. Mam bezpieczną propozycję naprawy, która zachowa historię. "
            "Zgadzasz się, żebym ją zastosował/a?"
        )
        instruction = (
            "A memory self-healing repair is waiting for user consent. Ask concise consent using the supplied user_prompt "
            "or a natural equivalent. If the user agrees, BEFORE replying call run_workshop_action area=memory "
            "action=self_healing_confirm with approve=true and this issue_id. If they decline, call the same action "
            "with approve=false. Do not expose database IDs or implementation details unless asked."
        )
        visible = True
    else:
        instruction = None
        visible = False
    return {
        "schema": SELF_HEALING_NOTICE_SCHEMA,
        "status": issue["status"],
        "visible_to_user": visible,
        "issue_count": status["issue_count"],
        "issue_id": issue_id,
        "issue_kind": issue["issue_kind"],
        "user_prompt": user_prompt,
        "assistant_instruction": instruction,
    }
