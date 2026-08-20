from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable

from mapi_core.memory.lifecycle_integrity import (
    evaluate_lifecycle_integrity_graph,
    load_lifecycle_graph,
)
from mapi_core.memory.lifecycle_pointer_execution import (
    POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION,
    _base_result,
    _event_ledger,
    _hash,
    _lifecycle,
    _link_ledgers,
    _revalidate,
    _strict_manifest,
    classify_existing_pointer_lifecycle_operation,
    validate_pointer_lifecycle_execution_manifest,
)
from mapi_core.memory.lifecycle_pointer_remediation import (
    LINK_CREATED_EVENT,
    POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
    _load_graph,
    _public_memory,
)
from mapi_core.memory.lifecycle_snapshots import (
    create_applying_lifecycle_snapshot_payload,
    finalize_lifecycle_snapshot_applied_payload,
    get_lifecycle_snapshot_payload,
    lifecycle_snapshot_to_dict,
    mark_lifecycle_snapshot_rolled_back_payload,
)
from mapi_core.sandman.contracts import ContractError, strict_json_loads


APPLY_SCHEMA_VERSION = "memory_v3_pointer_lifecycle_apply.v2"
RUN_SCHEMA_VERSION = "memory_v3_pointer_lifecycle_run.v2"
ROLLBACK_PREVIEW_SCHEMA_VERSION = "memory_v3_pointer_lifecycle_rollback_preview.v2"
ROLLBACK_SCHEMA_VERSION = "memory_v3_pointer_lifecycle_rollback.v2"
BACKUP_SCHEMA_VERSION = "memory_v3_pointer_lifecycle_backup.v1"
SNAPSHOT_SCHEMA_VERSION = "memory_v3_pointer_lifecycle_snapshot.v2"
COMPONENT_INTEGRITY_SCHEMA_VERSION = "memory_v3_pointer_component_integrity.v1"
REQUIRED_MIGRATION = "0027_memory_v3_pointer_lifecycle_execution"
EXPECTED_PRE_APPLY_ISSUES = frozenset({
    "multiple_active_heads", "reverse_pointer_mismatch", "supersedes_link_field_mismatch",
})
ALLOWED_UNSUPPORTED_METRICS = frozenset({
    "legacy state_code='active' is treated as canonical validated for lifecycle projection and integrity checks",
})


class PointerLifecycleApplyBlocked(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rows_for_operation(conn: Any, operation_key: str, row_to_dict: Callable[[Any], dict[str, Any]]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM memory_lifecycle_snapshots WHERE operation_key=? ORDER BY id", (operation_key,)
    ).fetchall()
    result = []
    for row in rows:
        item = lifecycle_snapshot_to_dict(row, row_to_dict=row_to_dict)
        identity = (item.get("before_snapshot") or {}).get("operation_identity") or {}
        result.append({**identity, "status": item.get("status"), "rolled_back_at": item.get("rolled_back_at"), "run": item})
    return result


def _classification(conn: Any, identity: dict[str, Any], row_to_dict: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    return classify_existing_pointer_lifecycle_operation(
        _rows_for_operation(conn, identity["operation_key"], row_to_dict), expected_identity=identity
    )


def _parse_approved_ids(raw: str) -> list[str]:
    value = strict_json_loads(raw, invalid_code="invalid_approved_protected_component_ids_json")
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ContractError("invalid_approved_protected_component_ids_json")
    if value != sorted(set(value)):
        raise ContractError("protected_component_approval_not_canonical")
    return value


def _target_states(conn: Any, target_ids: list[int]) -> list[dict[str, Any]]:
    memories, _ = _load_graph(conn)
    return [
        {"memory_id": memory_id, "lifecycle": _lifecycle(_public_memory(memories[memory_id]))}
        for memory_id in target_ids
    ]


def evaluate_selected_pointer_lifecycle_integrity(
    conn: Any, *, manifest: dict[str, Any], row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]], include_debug: bool = False,
) -> dict[str, Any]:
    del include_debug
    edges_by_component: dict[str, list[dict[str, Any]]] = {}
    for edge in manifest["selected_edges"]:
        edges_by_component.setdefault(edge["component_id"], []).append(edge)
    reports: list[dict[str, Any]] = []
    claimed_graph_ids: set[int] = set()
    aggregate_counts: Counter[str] = Counter()
    def compatible_enrich(row: dict[str, Any]) -> dict[str, Any]:
        try:
            return enrich_memory_dict(row)
        except ValueError:
            # Integrity only consumes lifecycle/lineage fields; legacy fixture and
            # production rows may predate current presentation enum normalization.
            return dict(row)
    for component_id in manifest["selected_component_ids"]:
        edges = edges_by_component.get(component_id) or []
        target_ids = sorted({edge[key] for edge in edges for key in ("new_memory_id", "old_memory_id")})
        if not target_ids:
            raise PointerLifecycleApplyBlocked("selected_component_has_no_targets")
        anchor = min(target_ids)
        graph = load_lifecycle_graph(
            conn, memory_id=anchor, include_archived=True, row_to_dict=row_to_dict,
            enrich_memory_dict=compatible_enrich,
        )
        # Archived supersedes links are historical evidence, not active lifecycle edges.
        graph["links"] = [link for link in graph["links"] if link.get("archived_at") is None]
        evaluated = evaluate_lifecycle_integrity_graph(
            graph, sample_limit=max(100, len(graph["memories_by_id"]) * 20), include_debug=True,
        )
        graph_ids = list(evaluated.get("debug", {}).get("graph_memory_ids") or [])
        live_component_id = _hash(sorted(graph_ids))[:16]
        if live_component_id != component_id:
            raise PointerLifecycleApplyBlocked("component_identity_mismatch")
        if not set(target_ids).issubset(graph_ids):
            raise PointerLifecycleApplyBlocked("selected_component_target_missing")
        if claimed_graph_ids.intersection(graph_ids):
            raise PointerLifecycleApplyBlocked("selected_components_merged")
        claimed_graph_ids.update(graph_ids)
        report = {
            "component_id": component_id,
            "anchor_memory_id": anchor,
            "graph_memory_ids": graph_ids,
            "report_schema_version": evaluated["schema_version"],
            "report_status": evaluated["status"],
            "summary": evaluated["summary"],
            "issue_counts": evaluated["issue_counts"],
            "findings": evaluated["findings"],
            "unsupported_metrics": evaluated["unsupported_metrics"],
            "source_memory_ids": evaluated["source_memory_ids"],
        }
        report["report_fingerprint"] = _hash(report)
        reports.append(report)
        aggregate_counts.update(report["issue_counts"])
    aggregate = {
        "schema_version": COMPONENT_INTEGRITY_SCHEMA_VERSION,
        "component_count": len(reports),
        "reports": reports,
        "critical_issues_total": sum(report["summary"]["critical_issues"] for report in reports),
        "issues_total": sum(report["summary"]["issues_total"] for report in reports),
        "issue_counts": dict(sorted(aggregate_counts.items())),
    }
    aggregate["report_set_fingerprint"] = _hash(reports)
    return aggregate


def _validate_integrity_evidence(value: Any, *, require_clean: bool) -> list[str]:
    reasons: list[str] = []
    if not isinstance(value, dict) or value.get("schema_version") != COMPONENT_INTEGRITY_SCHEMA_VERSION:
        return ["integrity_evidence_missing_or_schema_mismatch"]
    reports = value.get("reports")
    if not isinstance(reports, list) or value.get("component_count") != len(reports):
        return ["integrity_evidence_invalid"]
    for report in reports:
        if not isinstance(report, dict):
            reasons.append("integrity_report_invalid")
            continue
        stored = report.get("report_fingerprint")
        core = {key: item for key, item in report.items() if key != "report_fingerprint"}
        if stored != _hash(core):
            reasons.append("integrity_report_fingerprint_mismatch")
    if value.get("report_set_fingerprint") != _hash(reports):
        reasons.append("integrity_report_set_fingerprint_mismatch")
    issue_counts = Counter()
    critical_issues_total = 0
    issues_total = 0
    for report in reports:
        if isinstance(report, dict):
            issue_counts.update(report.get("issue_counts") or {})
            summary = report.get("summary")
            if not isinstance(summary, dict):
                reasons.append("integrity_report_summary_invalid")
                continue
            critical = summary.get("critical_issues")
            issues = summary.get("issues_total")
            if not isinstance(critical, int) or isinstance(critical, bool) or not isinstance(issues, int) or isinstance(issues, bool):
                reasons.append("integrity_report_summary_invalid")
                continue
            critical_issues_total += critical
            issues_total += issues
    if value.get("issue_counts") != dict(sorted(issue_counts.items())):
        reasons.append("integrity_issue_counts_mismatch")
    if value.get("critical_issues_total") != critical_issues_total or value.get("issues_total") != issues_total:
        reasons.append("integrity_summary_totals_mismatch")
    if require_clean:
        if value.get("critical_issues_total") != 0 or value.get("issues_total") != 0 or value.get("issue_counts") != {}:
            reasons.append("after_component_integrity_not_clean")
        unsupported = {
            metric for report in reports for metric in (report.get("unsupported_metrics") or [])
        }
        if unsupported - ALLOWED_UNSUPPORTED_METRICS:
            reasons.append("unexpected_unsupported_integrity_metric")
    return sorted(set(reasons))


def _validate_integrity_against_manifest(value: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    reports = value.get("reports") if isinstance(value, dict) else None
    if not isinstance(reports, list):
        return ["integrity_evidence_invalid"]
    if [report.get("component_id") for report in reports] != manifest["selected_component_ids"]:
        return ["component_identity_mismatch"]
    claimed: set[int] = set()
    edges_by_component: dict[str, set[int]] = {}
    for edge in manifest["selected_edges"]:
        edges_by_component.setdefault(edge["component_id"], set()).update(
            (edge["new_memory_id"], edge["old_memory_id"])
        )
    reasons: list[str] = []
    for report in reports:
        graph_ids = report.get("graph_memory_ids")
        component_id = report.get("component_id")
        if not isinstance(graph_ids, list) or not edges_by_component.get(component_id, set()).issubset(graph_ids):
            reasons.append("selected_component_target_missing")
            continue
        if claimed.intersection(graph_ids):
            reasons.append("selected_components_merged")
        claimed.update(graph_ids)
        if _hash(sorted(graph_ids))[:16] != component_id:
            reasons.append("component_identity_mismatch")
    return sorted(set(reasons))


def _require_pre_apply_integrity_allowed(evidence: dict[str, Any], manifest: dict[str, Any]) -> None:
    invalid = _validate_integrity_evidence(evidence, require_clean=False)
    invalid.extend(_validate_integrity_against_manifest(evidence, manifest))
    if invalid:
        raise PointerLifecycleApplyBlocked(invalid[0])
    observed = {finding["issue_code"] for report in evidence["reports"] for finding in report["findings"]}
    if observed - EXPECTED_PRE_APPLY_ISSUES:
        raise PointerLifecycleApplyBlocked("unexpected_pre_apply_integrity_finding")
    unsupported = {metric for report in evidence["reports"] for metric in report["unsupported_metrics"]}
    if unsupported - ALLOWED_UNSUPPORTED_METRICS:
        raise PointerLifecycleApplyBlocked("unexpected_unsupported_integrity_metric")


def _evaluate_selected_integrity(
    evaluator: Callable[..., dict[str, Any]] | None, conn: Any, *, phase: str,
    manifest: dict[str, Any], row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    if evaluator is None:
        return evaluate_selected_pointer_lifecycle_integrity(
            conn, manifest=manifest, row_to_dict=row_to_dict, enrich_memory_dict=enrich_memory_dict,
        )
    return evaluator(
        conn, phase=phase, manifest=manifest, row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
    )


def _expected_after(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    states: dict[int, dict[str, Any]] = {}
    for edge in manifest["selected_edges"]:
        states.setdefault(edge["new_memory_id"], copy.deepcopy(edge["expected_new_lifecycle_fields"]))
        old_id = edge["old_memory_id"]
        states.setdefault(old_id, copy.deepcopy(edge["expected_old_lifecycle_fields"]))
        for changes in (edge["projected_reverse_pointer_update"], edge["projected_state_update"]):
            for field, change in (changes or {}).items():
                states[old_id][field] = change["after"]
    return [{"memory_id": memory_id, "lifecycle": states[memory_id]} for memory_id in sorted(states)]


def _migration_tail(conn: Any) -> list[str]:
    return [str(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]


def _source_path(conn: Any) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not str(row[2] or "").strip():
        raise PointerLifecycleApplyBlocked("source_database_is_not_file_backed")
    return Path(str(row[2])).resolve()


def create_verified_pointer_lifecycle_backup(
    *, conn: Any, backups_root: Path | None, operation_key: str,
    execution_manifest_fingerprint: str, execution_preview_hash: str,
    manifest: dict[str, Any], utc_now_iso: Callable[[], str],
) -> dict[str, Any]:
    source_path = _source_path(conn)
    migrations = _migration_tail(conn)
    if REQUIRED_MIGRATION not in migrations:
        raise PointerLifecycleApplyBlocked("required_migration_0027_not_applied")
    root = Path(backups_root or source_path.parent / "backups").resolve()
    root.mkdir(parents=True, exist_ok=True)
    created_at = utc_now_iso()
    stamp = re.sub(r"[^0-9]", "", created_at)[:14]
    backup_path = root / f"agent_memory-v3-pointer-lifecycle-pre-{stamp}-{_hash(operation_key)[:10]}.db"
    suffix = 1
    while backup_path.exists():
        backup_path = root / f"agent_memory-v3-pointer-lifecycle-pre-{stamp}-{_hash(operation_key)[:10]}-{suffix}.db"
        suffix += 1
    source_size = source_path.stat().st_size
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if str(conn.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
        raise PointerLifecycleApplyBlocked("source_quick_check_failed")
    destination = sqlite3.connect(backup_path)
    try:
        conn.backup(destination)
        destination.commit()
    except Exception:
        destination.close()
        backup_path.unlink(missing_ok=True)
        raise
    destination.close()
    check = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    check.row_factory = sqlite3.Row
    try:
        if str(check.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise PointerLifecycleApplyBlocked("backup_quick_check_failed")
        target_ids = manifest["unique_target_memory_ids"]
        states = _target_states(check, target_ids)
        events = _event_ledger(check, target_ids)
        active, archived = _link_ledgers(check, target_ids)
        if states != _target_states(conn, target_ids) or events != manifest["target_event_ledger"]:
            raise PointerLifecycleApplyBlocked("backup_frozen_target_mismatch")
        if active != manifest["active_target_supersedes_link_ledger"] or archived != manifest["archived_target_supersedes_link_ledger"]:
            raise PointerLifecycleApplyBlocked("backup_frozen_link_mismatch")
        counts = {
            "memory_count": int(check.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
            "link_count": int(check.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]),
            "event_count": int(check.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]),
            "snapshot_count": int(check.execute("SELECT COUNT(*) FROM memory_lifecycle_snapshots").fetchone()[0]),
        }
        max_memory = check.execute("SELECT MAX(id) FROM memories").fetchone()[0]
        max_event = check.execute("SELECT MAX(id) FROM memory_events").fetchone()[0]
    except Exception:
        check.close()
        backup_path.unlink(missing_ok=True)
        raise
    check.close()
    return {
        "schema_version": BACKUP_SCHEMA_VERSION, "operation_key": operation_key,
        "execution_manifest_fingerprint": execution_manifest_fingerprint,
        "execution_preview_hash": execution_preview_hash,
        "source_database_path": str(source_path), "source_database_size": source_size,
        "source_database_sha256": source_sha, "source_quick_check": "ok",
        "backup_path": str(backup_path), "backup_size": backup_path.stat().st_size,
        "backup_sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(), "backup_quick_check": "ok",
        "migration_tail": migrations, "max_memory_id": max_memory, "max_event_id": max_event,
        **counts, "target_memory_lifecycle_fingerprint": _hash(states),
        "target_event_ledger_fingerprint": _hash(events),
        "target_active_supersedes_link_ledger_fingerprint": _hash(active),
        "target_archived_supersedes_link_ledger_fingerprint": _hash(archived), "created_at": created_at,
    }


def _blocked(base: dict[str, Any], reason: str, *, status: str = "blocked", backup: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {**base, "status": status, "blocking_reasons": [reason], "mutations_performed": 0}
    if backup is not None:
        result["backup_manifest"] = backup
    return result


def _exact_existing_result(base: dict[str, Any], rows: list[dict[str, Any]], *, concurrent: bool = False, backup: dict[str, Any] | None = None) -> dict[str, Any]:
    run = rows[0]["run"]
    result = {**base, "status": "already_applied_exact_concurrent" if concurrent else "already_applied_exact",
              "run_id": int(run["id"]), "operation_key": run["operation_key"],
              "stored_evidence": {key: run.get(key) for key in ("before_snapshot", "after_snapshot", "link_snapshot", "event_snapshot")},
              "mutations_performed": 0}
    if backup is not None:
        result["backup_manifest"] = backup
    return result


def apply_memory_pointer_lifecycle_remediation_execution_payload(
    conn: Any, *, plan_version: str, execution_policy_version: str,
    approved_execution_manifest_json: str, expected_execution_manifest_fingerprint: str,
    expected_execution_preview_hash: str, approved_protected_component_ids_json: str,
    applied_by: str | None, reason: str | None, confirm_data_repair: bool, confirm_protected: bool,
    backups_root: Path | None, utc_now_iso: Callable[[], str],
    normalize_required_text: Callable[[Any, str], str], normalize_optional_text: Callable[[Any], str | None],
    row_to_dict: Callable[[Any], dict[str, Any]], enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    insert_memory_event: Callable[..., dict[str, Any]],
    create_backup: Callable[..., dict[str, Any]] | None = None,
    failure_hook: Callable[[str], None] | None = None,
    integrity_evaluator: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base = {"schema_version": APPLY_SCHEMA_VERSION, "plan_version": str(plan_version),
            "execution_policy_version": str(execution_policy_version), "v3_b10_ready": False}
    if plan_version != POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION:
        return _blocked(base, "unsupported_plan_version")
    if execution_policy_version != POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION:
        return _blocked(base, "unsupported_execution_policy_version")
    if confirm_data_repair is not True:
        return _blocked(base, "confirm_data_repair_required")
    try:
        actor = normalize_required_text(applied_by, "applied_by")
        normalized_reason = normalize_required_text(reason, "reason")
        fingerprint = normalize_required_text(expected_execution_manifest_fingerprint, "expected_execution_manifest_fingerprint")
        expected_preview = normalize_required_text(expected_execution_preview_hash, "expected_execution_preview_hash")
        manifest = _strict_manifest(approved_execution_manifest_json)
        approved_ids = _parse_approved_ids(approved_protected_component_ids_json)
    except (ValueError, ContractError) as exc:
        code = exc.reason_codes[0] if isinstance(exc, ContractError) and exc.reason_codes else str(exc)
        return _blocked(base, code)
    actual_fingerprint = _hash(manifest)
    if fingerprint != actual_fingerprint:
        return _blocked(base, "execution_manifest_fingerprint_mismatch")
    preview = _base_result(manifest, actual_fingerprint)
    if expected_preview != preview["execution_preview_hash"]:
        return _blocked(base, "expected_execution_preview_hash_mismatch")
    if manifest["execution_scope"] == "unprotected_only":
        if approved_ids or confirm_protected is not False:
            return _blocked(base, "protected_component_approval_mismatch")
    elif approved_ids != manifest["protected_component_ids"] or confirm_protected is not True:
        return _blocked(base, "protected_component_approval_mismatch")
    identity = preview["future_operation_identity"]
    rows = _rows_for_operation(conn, identity["operation_key"], row_to_dict)
    classification = classify_existing_pointer_lifecycle_operation(rows, expected_identity=identity)
    if classification["decision"] == "already_applied_exact":
        return _exact_existing_result(base, rows)
    if classification["decision"] != "not_applied":
        return _blocked(base, classification["decision"])
    backup_helper = create_backup or create_verified_pointer_lifecycle_backup
    try:
        backup = backup_helper(conn=conn, backups_root=backups_root, operation_key=identity["operation_key"],
                               execution_manifest_fingerprint=actual_fingerprint,
                               execution_preview_hash=expected_preview, manifest=manifest, utc_now_iso=utc_now_iso)
    except Exception as exc:
        return _blocked(base, "trusted_online_backup_failed", status="backup_failed") | {"error": str(exc)}
    hook = failure_hook or (lambda stage: None)
    applied_at = utc_now_iso()
    expected_after = _expected_after(manifest)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = _rows_for_operation(conn, identity["operation_key"], row_to_dict)
        classification = classify_existing_pointer_lifecycle_operation(rows, expected_identity=identity)
        if classification["decision"] == "already_applied_exact":
            conn.rollback()
            return _exact_existing_result(base, rows, concurrent=True, backup=backup)
        if classification["decision"] != "not_applied":
            raise PointerLifecycleApplyBlocked(classification["decision"])
        revalidation = _revalidate(conn, manifest, actual_fingerprint)
        if revalidation["status"] != "execution_revalidation_ready" or revalidation["execution_preview_hash"] != expected_preview:
            raise PointerLifecycleApplyBlocked("stale_execution_manifest")
        before_states = _target_states(conn, manifest["unique_target_memory_ids"])
        before_integrity = _evaluate_selected_integrity(
            integrity_evaluator, conn, phase="pre_apply", manifest=manifest,
            row_to_dict=row_to_dict, enrich_memory_dict=enrich_memory_dict,
        )
        _require_pre_apply_integrity_allowed(before_integrity, manifest)
        first = manifest["selected_edges"][0]
        before_snapshot = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION, "operation_identity": identity,
            "approved_execution_manifest": manifest, "protected_approvals": approved_ids,
            "backup_manifest": backup, "before_target_lifecycle_state": before_states,
            "expected_after_target_lifecycle_state": expected_after,
            "before_component_integrity": before_integrity,
            "before_component_integrity_fingerprint": before_integrity["report_set_fingerprint"],
        }
        snapshot = create_applying_lifecycle_snapshot_payload(
            conn, operation_key=identity["operation_key"],
            new_memory_id=first["new_memory_id"], old_memory_id=first["old_memory_id"],
            relation_kind="pointer_only_chain_repair", reason=normalized_reason,
            input_fingerprint=actual_fingerprint, candidate_set_fingerprint=_hash(manifest["selected_component_ids"]),
            preview_hash=expected_preview, before_snapshot=before_snapshot,
            after_snapshot={"pending": {"expected_after_target_lifecycle_state": expected_after}},
            link_snapshot={"pending": {"before_active_target_supersedes_ledger": manifest["active_target_supersedes_link_ledger"],
                                        "before_archived_target_supersedes_ledger": manifest["archived_target_supersedes_link_ledger"],
                                        "projected_links": [edge["projected_link_create"] for edge in manifest["selected_edges"] if edge["projected_link_create"]],
                                        "created_link_ids": []}},
            event_snapshot={"pending": {"before_target_event_ledger": manifest["target_event_ledger"],
                                         "projected_event_descriptors": [event for edge in manifest["selected_edges"] for event in edge["projected_events"]],
                                         "created_apply_event_ids": []}},
            applied_by=actor, apply_note=normalized_reason, utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text, normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
        )
        hook("applying_snapshot")
        run_id = int(snapshot["id"])
        created_links: list[dict[str, Any]] = []
        updated_ids: list[int] = []
        created_events: list[dict[str, Any]] = []
        for edge in manifest["selected_edges"]:
            link = edge["projected_link_create"]
            if link is not None:
                cursor = conn.execute(
                    "INSERT INTO memory_links(from_memory_id,to_memory_id,relation_type,weight,origin,created_at,archived_at,workspace_id,visibility_scope) VALUES(?,?,?,?,?,?,?,?,?)",
                    (link["from_memory_id"], link["to_memory_id"], link["relation_type"], link["weight"], link["origin"],
                     applied_at, link["archived_at"], link["workspace_id"], link["visibility_scope"]),
                )
                created_links.append(row_to_dict(conn.execute("SELECT * FROM memory_links WHERE id=?", (cursor.lastrowid,)).fetchone()))
                hook(f"link:{cursor.lastrowid}")
            updates: dict[str, dict[str, Any]] = {}
            updates.update(edge["projected_reverse_pointer_update"] or {})
            updates.update(edge["projected_state_update"] or {})
            if updates:
                fields = sorted(updates)
                where = " AND ".join(f"{field} IS ?" for field in fields)
                cursor = conn.execute(
                    f"UPDATE memories SET {', '.join(f'{field}=?' for field in fields)} WHERE id=? AND {where}",
                    tuple(updates[field]["after"] for field in fields) + (edge["old_memory_id"],) + tuple(updates[field]["before"] for field in fields),
                )
                if cursor.rowcount != 1:
                    raise PointerLifecycleApplyBlocked("memory_before_value_mismatch")
                updated_ids.append(edge["old_memory_id"])
                hook(f"memory:{edge['old_memory_id']}")
            for descriptor in edge["projected_events"]:
                payload = {
                    "remediation_run_id": run_id, "operation_key": identity["operation_key"],
                    "plan_version": plan_version, "execution_policy_version": execution_policy_version,
                    "execution_manifest_fingerprint": actual_fingerprint, "execution_preview_hash": expected_preview,
                    "new_memory_id": edge["new_memory_id"], "old_memory_id": edge["old_memory_id"],
                    "changed_fields": descriptor["changed_fields"], "before_field_hash": descriptor["before_field_hash"],
                    "after_field_hash": descriptor["after_field_hash"], "applied_by": actor, "reason": normalized_reason,
                }
                event_memory_id = edge["new_memory_id"] if descriptor["event_type"] == LINK_CREATED_EVENT else edge["old_memory_id"]
                created_events.append(insert_memory_event(conn, memory_id=event_memory_id, event_type=descriptor["event_type"], payload=payload))
                hook(f"event:{descriptor['event_type']}:{event_memory_id}")
        actual_after = _target_states(conn, manifest["unique_target_memory_ids"])
        if actual_after != expected_after:
            raise PointerLifecycleApplyBlocked("after_target_state_mismatch")
        created_link_ids = [int(item["id"]) for item in created_links]
        active, archived = _link_ledgers(conn, manifest["unique_target_memory_ids"])
        expected_active = sorted(manifest["active_target_supersedes_link_ledger"] + [
            {"link_id": int(item["id"]), "from_memory_id": int(item["from_memory_id"]), "to_memory_id": int(item["to_memory_id"]),
             "relation_type": item["relation_type"], "workspace_id": item.get("workspace_id"), "origin": item.get("origin"),
             "weight": item["weight"], "created_at": item["created_at"], "archived_at": item.get("archived_at"),
             "visibility_scope": item.get("visibility_scope")}
            for item in created_links
        ], key=lambda item: item["link_id"])
        if active != expected_active or archived != manifest["archived_target_supersedes_link_ledger"]:
            raise PointerLifecycleApplyBlocked("created_link_ledger_mismatch")
        event_ledger = _event_ledger(conn, manifest["unique_target_memory_ids"])
        created_event_ids = [int(item["id"]) for item in created_events]
        created_event_ledger = [item for item in event_ledger if item["event_id"] in set(created_event_ids)]
        if event_ledger != sorted(manifest["target_event_ledger"] + created_event_ledger, key=lambda item: item["event_id"]):
            raise PointerLifecycleApplyBlocked("created_event_ledger_mismatch")
        post_integrity = _evaluate_selected_integrity(
            integrity_evaluator, conn, phase="post_apply", manifest=manifest,
            row_to_dict=row_to_dict, enrich_memory_dict=enrich_memory_dict,
        )
        integrity_blockers = _validate_integrity_evidence(post_integrity, require_clean=True)
        integrity_blockers.extend(_validate_integrity_against_manifest(post_integrity, manifest))
        if integrity_blockers:
            raise PointerLifecycleApplyBlocked(integrity_blockers[0])
        hook("post_integrity")
        final_snapshot = finalize_lifecycle_snapshot_applied_payload(
            conn, snapshot_id=run_id,
            after_snapshot={"schema_version": SNAPSHOT_SCHEMA_VERSION, "after_target_lifecycle_state": actual_after,
                            "after_component_integrity": post_integrity,
                            "after_component_integrity_fingerprint": post_integrity["report_set_fingerprint"]},
            link_snapshot={"schema_version": SNAPSHOT_SCHEMA_VERSION,
                           "before_active_target_supersedes_ledger": manifest["active_target_supersedes_link_ledger"],
                           "before_archived_target_supersedes_ledger": manifest["archived_target_supersedes_link_ledger"],
                           "created_link_ids": created_link_ids, "created_link_ledger": [item for item in expected_active if item["link_id"] in set(created_link_ids)]},
            event_snapshot={"schema_version": SNAPSHOT_SCHEMA_VERSION, "before_target_event_ledger": manifest["target_event_ledger"],
                            "created_apply_event_ids": created_event_ids, "created_apply_event_ledger": created_event_ledger,
                            "expected_after_target_event_ledger": event_ledger},
            applied_at=applied_at, utc_now_iso=utc_now_iso, normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
        )
        hook("snapshot_finalization")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        status = "stale_execution_manifest" if str(exc) == "stale_execution_manifest" else "apply_failed"
        return _blocked(base, str(exc) if status == "stale_execution_manifest" else "atomic_apply_failed", status=status, backup=backup) | {"error": str(exc)}
    counts = {"memory_updates": len(updated_ids), "links_created": len(created_link_ids),
              "events_created": len(created_event_ids), "snapshots_created": 1,
              "logical_mutations": len(updated_ids) + len(created_link_ids) + len(created_event_ids) + 1}
    return {**base, "status": "applied", "run_id": int(final_snapshot["id"]), "operation_key": identity["operation_key"],
            "execution_scope": manifest["execution_scope"], "execution_manifest_fingerprint": actual_fingerprint,
            "execution_preview_hash": expected_preview, "updated_memory_ids": sorted(set(updated_ids)),
            "created_link_ids": created_link_ids, "created_event_ids": created_event_ids,
            "mutation_counts": counts, "mutations_performed": counts["logical_mutations"], "backup_manifest": backup,
            "before_component_integrity": before_integrity,
            "before_component_integrity_fingerprint": before_integrity["report_set_fingerprint"],
            "post_integrity_summary": {
                key: post_integrity[key] for key in (
                    "component_count", "critical_issues_total", "issues_total", "issue_counts", "report_set_fingerprint"
                )
            }, "remaining_blocked_components": revalidation["remaining_blocked_components"],
            "v3_b10_ready": False}


def _operation_identity_is_exact(run: dict[str, Any]) -> bool:
    before = run.get("before_snapshot") or {}
    manifest = before.get("approved_execution_manifest")
    if not isinstance(manifest, dict):
        return False
    try:
        manifest = validate_pointer_lifecycle_execution_manifest(manifest, allow_legacy=True)
    except ContractError:
        return False
    fingerprint = _hash(manifest)
    expected = _base_result(manifest, fingerprint)["future_operation_identity"]
    return (
        before.get("operation_identity") == expected
        and run.get("operation_key") == expected["operation_key"]
        and run.get("preview_hash") == expected["execution_preview_hash"]
    )


def pointer_lifecycle_operation_identity_is_exact(run: dict[str, Any]) -> bool:
    """Validate a stored run against the operation identity derived from its manifest."""
    return _operation_identity_is_exact(run)


def _stored_apply_integrity_reasons(run: dict[str, Any]) -> list[str]:
    before = run.get("before_snapshot") or {}
    after = run.get("after_snapshot") or {}
    baseline = before.get("before_component_integrity")
    applied = after.get("after_component_integrity")
    reasons = _validate_integrity_evidence(baseline, require_clean=False)
    reasons.extend(_validate_integrity_evidence(applied, require_clean=True))
    if isinstance(baseline, dict) and before.get("before_component_integrity_fingerprint") != baseline.get("report_set_fingerprint"):
        reasons.append("before_component_integrity_fingerprint_mismatch")
    if isinstance(applied, dict) and after.get("after_component_integrity_fingerprint") != applied.get("report_set_fingerprint"):
        reasons.append("after_component_integrity_fingerprint_mismatch")
    manifest = before.get("approved_execution_manifest")
    if isinstance(manifest, dict):
        if isinstance(baseline, dict):
            reasons.extend(_validate_integrity_against_manifest(baseline, manifest))
        if isinstance(applied, dict):
            reasons.extend(_validate_integrity_against_manifest(applied, manifest))
    return sorted(set(reasons))


def validate_pointer_lifecycle_stored_apply_integrity(run: dict[str, Any]) -> list[str]:
    """Return fail-closed reasons for stored pre/post apply integrity evidence."""
    return _stored_apply_integrity_reasons(run)


def get_memory_pointer_lifecycle_remediation_execution_run_payload(
    conn: Any, *, run_id: int, include_debug: bool = False, row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    try:
        run = get_lifecycle_snapshot_payload(conn, snapshot_id=int(run_id), row_to_dict=row_to_dict)
    except FileNotFoundError:
        return {"schema_version": RUN_SCHEMA_VERSION, "status": "not_found", "run_id": int(run_id), "safety": {"read_only": True}}
    if run.get("operation_type") != "pointer_lineage_remediation":
        return {"schema_version": RUN_SCHEMA_VERSION, "status": "blocked", "run_id": int(run_id), "blocking_reasons": ["not_pointer_lifecycle_run"]}
    before = run["before_snapshot"]
    manifest = before["approved_execution_manifest"]
    redacted = copy.deepcopy(manifest)
    before_integrity = before.get("before_component_integrity")
    after_integrity = (run.get("after_snapshot") or {}).get("after_component_integrity")
    rollback_integrity = (run.get("rollback_snapshot") or {}).get("post_rollback_component_integrity")
    integrity_valid: bool | None = None
    rollback_valid: bool | None = None
    if run["status"] in {"applied", "rolled_back"}:
        integrity_valid = not _stored_apply_integrity_reasons(run)
    if run["status"] == "rolled_back":
        rollback_valid = not validate_pointer_lifecycle_rolled_back_evidence(
            conn, run, row_to_dict=row_to_dict, enrich_memory_dict=enrich_memory_dict,
        )
    result = {"schema_version": RUN_SCHEMA_VERSION, "status": "ok", "run_id": int(run["id"]),
              "run_status": run["status"], "operation_identity": before["operation_identity"],
              "execution_scope": manifest["execution_scope"], "approved_execution_manifest": redacted,
              "protected_approvals": before["protected_approvals"], "backup_manifest": before["backup_manifest"],
              "before_target_lifecycle_state": before["before_target_lifecycle_state"],
              "after_target_lifecycle_state": (run.get("after_snapshot") or {}).get("after_target_lifecycle_state"),
              "created_link_ledger": (run.get("link_snapshot") or {}).get("created_link_ledger", []),
              "created_apply_event_ledger": (run.get("event_snapshot") or {}).get("created_apply_event_ledger", []),
              "before_component_integrity_summary": None if not isinstance(before_integrity, dict) else {
                  key: before_integrity.get(key) for key in ("component_count", "critical_issues_total", "issues_total", "issue_counts")
              },
              "before_component_integrity_fingerprint": before.get("before_component_integrity_fingerprint"),
              "after_component_integrity_summary": None if not isinstance(after_integrity, dict) else {
                  key: after_integrity.get(key) for key in ("component_count", "critical_issues_total", "issues_total", "issue_counts")
              },
              "after_component_integrity_fingerprint": (run.get("after_snapshot") or {}).get("after_component_integrity_fingerprint"),
              "post_rollback_integrity_summary": None if not isinstance(rollback_integrity, dict) else {
                  key: rollback_integrity.get(key) for key in ("component_count", "critical_issues_total", "issues_total", "issue_counts")
              },
              "post_rollback_integrity_fingerprint": (run.get("rollback_snapshot") or {}).get("post_rollback_component_integrity_fingerprint"),
              "integrity_evidence_valid": integrity_valid, "rollback_evidence_valid": rollback_valid,
              "rollback_available": run["status"] == "applied", "safety": {"read_only": True, "mutations_performed": 0}}
    if include_debug:
        result["debug"] = {key: run.get(key) for key in ("before_snapshot", "after_snapshot", "link_snapshot", "event_snapshot", "rollback_snapshot")}
    return result


def _rollback_evidence(conn: Any, run: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    before = run["before_snapshot"]
    manifest = before["approved_execution_manifest"]
    blockers: list[str] = []
    if not _operation_identity_is_exact(run):
        blockers.append("operation_identity_mismatch")
    blockers.extend(_stored_apply_integrity_reasons(run))
    current_states = _target_states(conn, manifest["unique_target_memory_ids"])
    expected_states = (run.get("after_snapshot") or {}).get("after_target_lifecycle_state")
    if current_states != expected_states:
        blockers.append("current_target_lifecycle_state_drift")
    links = run.get("link_snapshot") or {}
    created_links = links.get("created_link_ledger")
    if not isinstance(created_links, list):
        blockers.append("snapshot_link_evidence_invalid")
        created_links = []
    else:
        ids = [item["link_id"] for item in created_links]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT id,from_memory_id,to_memory_id,relation_type,workspace_id,origin,weight,created_at,archived_at,visibility_scope FROM memory_links WHERE id IN ({placeholders}) ORDER BY id", tuple(ids)).fetchall()
            live = [{"link_id": int(row["id"]), "from_memory_id": int(row["from_memory_id"]), "to_memory_id": int(row["to_memory_id"]),
                     "relation_type": row["relation_type"], "workspace_id": row["workspace_id"], "origin": row["origin"],
                     "weight": row["weight"], "created_at": row["created_at"], "archived_at": row["archived_at"],
                     "visibility_scope": row["visibility_scope"]} for row in rows]
            if live != created_links:
                blockers.append("created_link_drift")
    events = run.get("event_snapshot") or {}
    expected_event_ledger = events.get("expected_after_target_event_ledger")
    live_events = _event_ledger(conn, manifest["unique_target_memory_ids"])
    if not isinstance(expected_event_ledger, list) or live_events != expected_event_ledger:
        blockers.append("target_event_ledger_drift")
    evidence = {"current_target_lifecycle_state": current_states, "created_link_ledger": created_links,
                "current_target_event_ledger": live_events, "snapshot_status": run["status"]}
    return sorted(set(blockers)), evidence


def _created_link_ledger(conn: Any, ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id,from_memory_id,to_memory_id,relation_type,workspace_id,origin,weight,created_at,archived_at,visibility_scope "
        f"FROM memory_links WHERE id IN ({placeholders}) ORDER BY id", tuple(ids),
    ).fetchall()
    return [{"link_id": int(row["id"]), "from_memory_id": int(row["from_memory_id"]),
             "to_memory_id": int(row["to_memory_id"]), "relation_type": row["relation_type"],
             "workspace_id": row["workspace_id"], "origin": row["origin"], "weight": row["weight"],
             "created_at": row["created_at"], "archived_at": row["archived_at"],
             "visibility_scope": row["visibility_scope"]} for row in rows]


def validate_pointer_lifecycle_rolled_back_evidence(
    conn: Any, run: dict[str, Any], *, row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    integrity_evaluator: Callable[..., dict[str, Any]] | None = None,
) -> list[str]:
    reasons: list[str] = []
    snapshot = run.get("rollback_snapshot")
    if not isinstance(snapshot, dict):
        return ["rolled_back_evidence_missing"]
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        reasons.append("rolled_back_evidence_schema_mismatch")
    before = run.get("before_snapshot") or {}
    manifest = before.get("approved_execution_manifest")
    if not isinstance(manifest, dict) or not _operation_identity_is_exact(run):
        reasons.append("rolled_back_operation_identity_mismatch")
        return sorted(set(reasons))
    required = {
        "run_id", "operation_key", "rollback_preview_hash", "rollback_candidate_fingerprint",
        "restored_memory_ids", "archived_link_ids", "created_rollback_event_ids",
        "created_rollback_event_ledger", "after_rollback_target_lifecycle_state",
        "post_rollback_component_integrity", "post_rollback_component_integrity_fingerprint",
        "baseline_component_integrity_fingerprint", "rolled_back_at", "rolled_back_by", "rollback_note",
    }
    if not required.issubset(snapshot):
        reasons.append("rolled_back_evidence_missing")
    if (
        snapshot.get("run_id") != int(run["id"])
        or snapshot.get("operation_key") != run.get("operation_key")
        or snapshot.get("rollback_preview_hash") != run.get("rollback_preview_hash")
        or not snapshot.get("rollback_candidate_fingerprint")
    ):
        reasons.append("rolled_back_evidence_identity_mismatch")
    if (
        snapshot.get("rolled_back_at") != run.get("rolled_back_at")
        or snapshot.get("rolled_back_by") != run.get("rolled_back_by")
        or snapshot.get("rollback_note") != run.get("rollback_note")
    ):
        reasons.append("rolled_back_actor_or_note_mismatch")
    current_states = _target_states(conn, manifest["unique_target_memory_ids"])
    if current_states != before.get("before_target_lifecycle_state") or current_states != snapshot.get("after_rollback_target_lifecycle_state"):
        reasons.append("rolled_back_target_state_drift")
    created_links = (run.get("link_snapshot") or {}).get("created_link_ledger") or []
    link_ids = (run.get("link_snapshot") or {}).get("created_link_ids") or []
    live_links = _created_link_ledger(conn, link_ids)
    expected_archived = [{**item, "archived_at": run.get("rolled_back_at")} for item in created_links]
    if live_links != expected_archived or snapshot.get("archived_link_ids") != link_ids:
        reasons.append("rolled_back_link_evidence_drift")
    event_snapshot = run.get("event_snapshot") or {}
    before_events = event_snapshot.get("before_target_event_ledger") or []
    apply_events = event_snapshot.get("created_apply_event_ledger") or []
    rollback_events = snapshot.get("created_rollback_event_ledger")
    if not isinstance(rollback_events, list):
        reasons.append("rolled_back_evidence_missing")
        rollback_events = []
    live_events = _event_ledger(conn, manifest["unique_target_memory_ids"])
    expected_events = sorted(before_events + apply_events + rollback_events, key=lambda item: item["event_id"])
    if (
        live_events != expected_events
        or snapshot.get("created_rollback_event_ids") != [item.get("event_id") for item in rollback_events]
    ):
        reasons.append("rolled_back_event_ledger_drift")
    baseline = before.get("before_component_integrity")
    post = snapshot.get("post_rollback_component_integrity")
    baseline_fingerprint = before.get("before_component_integrity_fingerprint")
    if (
        _validate_integrity_evidence(baseline, require_clean=False)
        or _validate_integrity_evidence(post, require_clean=False)
        or snapshot.get("baseline_component_integrity_fingerprint") != baseline_fingerprint
        or snapshot.get("post_rollback_component_integrity_fingerprint") != baseline_fingerprint
        or post != baseline
    ):
        reasons.append("rolled_back_integrity_drift")
    try:
        current_integrity = _evaluate_selected_integrity(
            integrity_evaluator, conn, phase="rolled_back_evidence", manifest=manifest,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
        if current_integrity != post or current_integrity.get("report_set_fingerprint") != baseline_fingerprint:
            reasons.append("rolled_back_integrity_drift")
    except Exception:
        reasons.append("rolled_back_integrity_drift")
    return sorted(set(reasons))


def preview_memory_pointer_lifecycle_remediation_execution_rollback_payload(
    conn: Any, *, run_id: int, include_debug: bool = False, row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    integrity_evaluator: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base = {"schema_version": ROLLBACK_PREVIEW_SCHEMA_VERSION, "run_id": int(run_id), "safety": {"read_only": True, "mutations_performed": 0}}
    try:
        run = get_lifecycle_snapshot_payload(conn, snapshot_id=int(run_id), row_to_dict=row_to_dict)
    except FileNotFoundError:
        return {**base, "status": "blocked", "blocking_reasons": ["run_not_found"]}
    if run.get("operation_type") != "pointer_lineage_remediation":
        return {**base, "status": "blocked", "blocking_reasons": ["not_pointer_lifecycle_run"]}
    if run["status"] == "rolled_back":
        reasons = validate_pointer_lifecycle_rolled_back_evidence(
            conn, run, row_to_dict=row_to_dict, enrich_memory_dict=enrich_memory_dict,
            integrity_evaluator=integrity_evaluator,
        )
        return {**base, "status": "already_rolled_back_exact" if not reasons else "blocked", "blocking_reasons": reasons}
    if run["status"] != "applied":
        return {**base, "status": "blocked", "blocking_reasons": ["run_not_applied"]}
    blockers, evidence = _rollback_evidence(conn, run)
    manifest = run["before_snapshot"]["approved_execution_manifest"]
    changed = [{"old_memory_id": edge["old_memory_id"], "fields": sorted({**(edge["projected_reverse_pointer_update"] or {}), **(edge["projected_state_update"] or {})})}
               for edge in manifest["selected_edges"]]
    candidate = _hash({"run_id": int(run_id), "operation_key": run["operation_key"], "evidence": evidence,
                       "planned_field_restoration": changed, "created_link_ids": (run["link_snapshot"] or {}).get("created_link_ids", [])})
    status = "preview_ready" if not blockers else "blocked"
    preview_hash = _hash({"schema_version": ROLLBACK_PREVIEW_SCHEMA_VERSION, "status": status,
                          "rollback_candidate_fingerprint": candidate, "blocking_reasons": blockers})
    result = {**base, "status": status, "operation_key": run["operation_key"],
              "rollback_candidate_fingerprint": candidate, "rollback_preview_hash": preview_hash,
              "planned_field_restoration": changed, "planned_archive_link_ids": (run["link_snapshot"] or {}).get("created_link_ids", []),
              "preserve_apply_event_ids": (run["event_snapshot"] or {}).get("created_apply_event_ids", []),
              "planned_rollback_event_count": len((run["event_snapshot"] or {}).get("created_apply_event_ids", [])),
              "blocking_reasons": blockers}
    if include_debug:
        result["debug"] = evidence
    return result


def rollback_memory_pointer_lifecycle_remediation_execution_payload(
    conn: Any, *, run_id: int, expected_rollback_preview_hash: str, rolled_back_by: str | None, notes: str | None,
    utc_now_iso: Callable[[], str], normalize_required_text: Callable[[Any, str], str],
    normalize_optional_text: Callable[[Any], str | None], row_to_dict: Callable[[Any], dict[str, Any]],
    enrich_memory_dict: Callable[[dict[str, Any]], dict[str, Any]],
    insert_memory_event: Callable[..., dict[str, Any]], failure_hook: Callable[[str], None] | None = None,
    integrity_evaluator: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base = {"schema_version": ROLLBACK_SCHEMA_VERSION, "run_id": int(run_id)}
    try:
        expected_hash = normalize_required_text(expected_rollback_preview_hash, "expected_rollback_preview_hash")
        actor = normalize_required_text(rolled_back_by, "rolled_back_by")
    except ValueError as exc:
        return _blocked(base, str(exc))
    try:
        run = get_lifecycle_snapshot_payload(conn, snapshot_id=int(run_id), row_to_dict=row_to_dict)
    except FileNotFoundError:
        return _blocked(base, "run_not_found")
    if run.get("operation_type") != "pointer_lineage_remediation":
        return _blocked(base, "not_pointer_lifecycle_run")
    if run["status"] == "rolled_back":
        expected_note = normalize_optional_text(notes)
        reasons = validate_pointer_lifecycle_rolled_back_evidence(
            conn, run, row_to_dict=row_to_dict, enrich_memory_dict=enrich_memory_dict,
            integrity_evaluator=integrity_evaluator,
        )
        if not reasons and (
            run.get("rollback_preview_hash") == expected_hash
            and run.get("rolled_back_by") == actor
            and run.get("rollback_note") == expected_note
        ):
            return {**base, "status": "already_rolled_back_exact", "mutations_performed": 0}
        return _blocked(base, reasons[0] if reasons else "rolled_back_actor_or_note_mismatch")
    preview = preview_memory_pointer_lifecycle_remediation_execution_rollback_payload(
        conn, run_id=run_id, row_to_dict=row_to_dict, enrich_memory_dict=enrich_memory_dict,
        integrity_evaluator=integrity_evaluator,
    )
    if preview["status"] != "preview_ready":
        return _blocked(base, preview["blocking_reasons"][0])
    if preview["rollback_preview_hash"] != expected_hash:
        return _blocked(base, "expected_rollback_preview_hash_mismatch", status="stale_rollback_preview")
    hook = failure_hook or (lambda stage: None)
    rolled_back_at = utc_now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh = preview_memory_pointer_lifecycle_remediation_execution_rollback_payload(
            conn, run_id=run_id, row_to_dict=row_to_dict, enrich_memory_dict=enrich_memory_dict,
            integrity_evaluator=integrity_evaluator,
        )
        if fresh.get("status") != "preview_ready" or fresh.get("rollback_preview_hash") != expected_hash:
            raise PointerLifecycleApplyBlocked("stale_rollback_preview")
        manifest = run["before_snapshot"]["approved_execution_manifest"]
        restored: list[int] = []
        for edge in manifest["selected_edges"]:
            updates = {**(edge["projected_reverse_pointer_update"] or {}), **(edge["projected_state_update"] or {})}
            if not updates:
                continue
            fields = sorted(updates)
            where = " AND ".join(f"{field} IS ?" for field in fields)
            cursor = conn.execute(f"UPDATE memories SET {', '.join(f'{field}=?' for field in fields)} WHERE id=? AND {where}",
                                  tuple(updates[field]["before"] for field in fields) + (edge["old_memory_id"],) + tuple(updates[field]["after"] for field in fields))
            if cursor.rowcount != 1:
                raise PointerLifecycleApplyBlocked("rollback_memory_after_value_mismatch")
            restored.append(edge["old_memory_id"])
            hook(f"restore:{edge['old_memory_id']}")
        created_link_ids = run["link_snapshot"]["created_link_ids"]
        for link_id in created_link_ids:
            cursor = conn.execute("UPDATE memory_links SET archived_at=? WHERE id=? AND archived_at IS NULL", (rolled_back_at, link_id))
            if cursor.rowcount != 1:
                raise PointerLifecycleApplyBlocked("rollback_link_archive_mismatch")
            hook(f"archive_link:{link_id}")
        rollback_events = []
        for edge in manifest["selected_edges"]:
            for descriptor in edge["projected_events"]:
                memory_id = edge["new_memory_id"] if descriptor["event_type"] == LINK_CREATED_EVENT else edge["old_memory_id"]
                rollback_events.append(insert_memory_event(conn, memory_id=memory_id,
                    event_type=f"{descriptor['event_type']}.rolled_back",
                    payload={"remediation_run_id": int(run_id), "operation_key": run["operation_key"],
                             "execution_manifest_fingerprint": run["before_snapshot"]["operation_identity"]["execution_manifest_fingerprint"],
                             "execution_preview_hash": run["preview_hash"],
                             "rollback_preview_hash": expected_hash,
                             "rollback_candidate_fingerprint": fresh["rollback_candidate_fingerprint"],
                             "new_memory_id": edge["new_memory_id"], "old_memory_id": edge["old_memory_id"],
                             "changed_fields": descriptor["changed_fields"], "rolled_back_at": rolled_back_at,
                             "rolled_back_by": actor, "notes": normalize_optional_text(notes)}))
        hook("rollback_events")
        current_states = _target_states(conn, manifest["unique_target_memory_ids"])
        if current_states != run["before_snapshot"]["before_target_lifecycle_state"]:
            raise PointerLifecycleApplyBlocked("rollback_state_restoration_mismatch")
        live_links = _created_link_ledger(conn, created_link_ids)
        expected_archived_links = [
            {**item, "archived_at": rolled_back_at} for item in run["link_snapshot"]["created_link_ledger"]
        ]
        if live_links != expected_archived_links:
            raise PointerLifecycleApplyBlocked("rollback_link_archive_evidence_mismatch")
        target_event_ledger = _event_ledger(conn, manifest["unique_target_memory_ids"])
        rollback_event_ids = [int(item["id"]) for item in rollback_events]
        rollback_event_ledger = [
            item for item in target_event_ledger if item["event_id"] in set(rollback_event_ids)
        ]
        expected_event_ledger = sorted(
            run["event_snapshot"]["before_target_event_ledger"]
            + run["event_snapshot"]["created_apply_event_ledger"]
            + rollback_event_ledger,
            key=lambda item: item["event_id"],
        )
        if target_event_ledger != expected_event_ledger:
            raise PointerLifecycleApplyBlocked("rollback_event_ledger_mismatch")
        post_rollback_integrity = _evaluate_selected_integrity(
            integrity_evaluator, conn, phase="post_rollback", manifest=manifest,
            row_to_dict=row_to_dict, enrich_memory_dict=enrich_memory_dict,
        )
        baseline_integrity = run["before_snapshot"].get("before_component_integrity")
        baseline_fingerprint = run["before_snapshot"].get("before_component_integrity_fingerprint")
        if (
            _validate_integrity_evidence(post_rollback_integrity, require_clean=False)
            or _validate_integrity_against_manifest(post_rollback_integrity, manifest)
            or post_rollback_integrity != baseline_integrity
            or post_rollback_integrity.get("report_set_fingerprint") != baseline_fingerprint
        ):
            raise PointerLifecycleApplyBlocked("post_rollback_integrity_baseline_mismatch")
        rollback_snapshot = {"schema_version": SNAPSHOT_SCHEMA_VERSION,
                             "run_id": int(run_id), "operation_key": run["operation_key"],
                             "rollback_preview_hash": expected_hash,
                             "rollback_candidate_fingerprint": fresh["rollback_candidate_fingerprint"],
                             "restored_memory_ids": sorted(set(restored)), "archived_link_ids": created_link_ids,
                             "created_rollback_event_ids": rollback_event_ids,
                             "created_rollback_event_ledger": rollback_event_ledger,
                             "after_rollback_target_lifecycle_state": current_states,
                             "post_rollback_component_integrity": post_rollback_integrity,
                             "post_rollback_component_integrity_fingerprint": post_rollback_integrity["report_set_fingerprint"],
                             "baseline_component_integrity_fingerprint": baseline_fingerprint,
                             "rolled_back_at": rolled_back_at, "rolled_back_by": actor,
                             "rollback_note": normalize_optional_text(notes)}
        mark_lifecycle_snapshot_rolled_back_payload(
            conn, snapshot_id=int(run_id), rollback_preview_hash=expected_hash, rollback_snapshot=rollback_snapshot,
            rolled_back_at=rolled_back_at, rolled_back_by=actor, rollback_note=normalize_optional_text(notes),
            utc_now_iso=utc_now_iso, normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text, row_to_dict=row_to_dict,
        )
        hook("rollback_snapshot")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return _blocked(base, "atomic_rollback_failed", status="rollback_failed") | {"error": str(exc)}
    return {**base, "status": "rolled_back", "restored_memory_ids": sorted(set(restored)),
            "archived_link_ids": created_link_ids, "created_rollback_event_ids": [int(item["id"]) for item in rollback_events],
            "post_rollback_component_integrity": post_rollback_integrity,
            "post_rollback_component_integrity_fingerprint": post_rollback_integrity["report_set_fingerprint"],
            "baseline_component_integrity_fingerprint": baseline_fingerprint,
            "mutations_performed": len(set(restored)) + len(created_link_ids) + len(rollback_events) + 1}


__all__ = [
    "APPLY_SCHEMA_VERSION", "RUN_SCHEMA_VERSION", "ROLLBACK_PREVIEW_SCHEMA_VERSION", "ROLLBACK_SCHEMA_VERSION",
    "BACKUP_SCHEMA_VERSION", "SNAPSHOT_SCHEMA_VERSION", "create_verified_pointer_lifecycle_backup",
    "evaluate_selected_pointer_lifecycle_integrity", "validate_pointer_lifecycle_rolled_back_evidence",
    "pointer_lifecycle_operation_identity_is_exact",
    "validate_pointer_lifecycle_stored_apply_integrity",
    "apply_memory_pointer_lifecycle_remediation_execution_payload",
    "get_memory_pointer_lifecycle_remediation_execution_run_payload",
    "preview_memory_pointer_lifecycle_remediation_execution_rollback_payload",
    "rollback_memory_pointer_lifecycle_remediation_execution_payload",
]
