from __future__ import annotations

"""Admin/dev MCP core for MAPI.

This module owns the full local MAPI implementation: database access, memory
operations, Sandman/governance tools, file helpers, and the compact MCP surface
router. Public MPbM tools should stay in server_mpbm_core.py.
"""

import hashlib
import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from app import (
    actor_context as actor_ctx,
    conflict_explainer,
    conflict_logic,
    consolidation_logic,
    conversation_archive as conv_archive,
    db_migrations,
    memory_config as config,
    memory_store as store,
    sandman_ai,
    sandman_dreams,
    sandman_gemma_client,
    sandman_gemma_hygiene,
    sandman_gemma_runtime,
    sandman_logic,
    schemas,
    timeline,
    mapi_gemma_agent,
    gemma_worker,
    gemma_worker_jobs,
    gemma_worker_runner,
)
from app.workshops.payload_contracts import MEMORY_FIND_SORT_VALUES, PROJECT_KEY_MODE_VALUES, invalid_choice_payload
from app.workshops.capabilities import build_mapi_capabilities_payload
from app.actor_context import (
    ActorContext,
    build_memory_visibility_filter,
    infer_visibility_scope,
    resolve_actor_context,
    resolve_system_actor,
)
from app.workshops.idempotency import idempotent_direct_mutation
from app.workshops.runner import run_workshop_action_payload
from app.runtime.freshness import get_runtime_readiness
from app.runtime.onboarding import (
    AUTONOMY_LEVELS,
    MEMORY_POLICIES,
    ONBOARDING_STEPS,
    advance_onboarding_state,
    build_onboarding_payload,
    revise_onboarding_answer_state,
    skip_onboarding_state,
)
from app.runtime.backpressure import transport_status_payload
from app.runtime.private_mode import effective_multiuser_flag_enabled, private_owner_key, runtime_mode
from app.runtime.context import (
    configure_runtime_context,
    runtime_data_dir,
    runtime_db_path,
    runtime_root,
)
from app.workshops.runtime_registry import bind_workshop_handlers
from app.workspaces.info import get_workspace_info_payload
from app.features.flag_helpers import (
    evaluate_feature_flag_config,
    feature_flag_to_dict,
    get_feature_flag_config,
    is_simple_feature_active,
    normalize_csv_tokens,
    normalize_feature_flag_key,
    normalize_rollout_mode,
    require_feature_flag_write_access,
    serialize_csv_tokens,
)
from app.core.time_score import compute_days_overdue as core_compute_days_overdue, normalize_score as core_normalize_score, safe_event_timestamp as core_safe_event_timestamp, shift_iso_days as core_shift_iso_days, utc_now_iso as core_utc_now_iso, utc_offset_days_iso as core_utc_offset_days_iso
from app.admin.file_tools import (
    append_file_text_payload,
    delete_path_payload,
    get_root_payload,
    list_dir_payload,
    make_dir_payload,
    move_path_payload,
    read_file_base64_payload,
    read_file_text_payload,
    search_text_payload,
    stat_path_payload,
    write_file_base64_payload,
    write_file_text_payload,
)
from app.admin.sql_tools import get_db_info_payload, query_sql_payload
from app.admin.patch_tools import insert_after_marker_payload, insert_before_marker_payload, replace_once_payload
from app.admin.migration_validation import validate_migration_0010_payload
from app.admin.operator_tools import (
    git_commit_command,
    git_push_command,
    git_status_command,
    run_powershell_command,
    run_shell_command,
    run_pytest_command,
)
from app.bootstrap.agent_core import (
    build_bootstrap_agent_context_payload,
    agent_bootstrap_protocol,
    agent_recommended_next_calls,
    agent_workshop_index,
    known_systems_for_project,
    project_purpose_for,
)
from app.conversations.archive_tools import archive_conversation_payload, get_conversation_payload, list_conversations_payload, search_verbatim_payload
from app.conversations.day_reconstruction import reconstruct_day_payload
from app.features.tool_payloads import (
    compatibility_feature_flag,
    evaluate_feature_flag_payload,
    get_feature_flag_payload,
    list_feature_flags_payload,
    set_feature_flag_payload,
    upsert_feature_flag_payload,
)
from app.ingest.items import (
    INGEST_SOURCE_TYPES,
    INGEST_STATUSES,
    archive_ingest_item_payload,
    create_ingest_item_payload,
    ensure_ingest_source,
    get_ingest_item_payload,
    list_ingest_queue_payload,
    normalize_claims_json,
    normalize_ingest_status,
    normalize_source_type,
    preview_research_ingest_review_payload,
    promote_ingest_item_payload,
    reject_ingest_item_payload,
    require_ingest_item,
    row_to_ingest_item,
)
from app.memory.activation import get_memory_recall_telemetry_payload, recall_memory_payload
from app.memory.conflicts import get_conflict_pairs_payload, list_conflicted_memories_payload
from app.memory.capture_queue import (
    CAPTURE_REVIEW_ITEM_SCHEMA_VERSION,
    create_capture_review_item,
    expire_capture_item,
    get_capture_review_item,
    list_capture_review_items,
    review_capture_item,
)
from app.memory.escalation import (
    escalation_dashboard_payload,
    escalation_history_payload,
    escalation_stage,
    highest_escalation_summary,
)
from app.memory.events import (
    add_review_note_payload,
    add_validation_event_payload,
    insert_memory_event_payload,
    memory_event_to_dict,
    list_review_events_payload,
    list_validation_events_payload,
)
from app.memory.filters import memory_matches_operational_filters
from app.memory.insights import layer_stats_payload, version_lineage_payload
from app.memory.context_engine import build_agent_context_payload
from app.memory.hybrid_retrieval import fuse_hybrid_results
from app.memory.steward import (
    after_action_content,
    before_action_payload,
    capture_phase_payload,
    nightly_payload,
    session_close_content,
)
from app.memory.current_state import (
    apply_direct_supersession_transition,
    get_memory_current_state_inventory_payload,
    get_memory_current_state_payload,
    resolve_current_memory_state,
)
from app.memory import hygiene as memory_hygiene
from app.memory import self_healing as memory_self_healing
from app.memory.write_routing import (
    WRITE_RESULT_SCHEMA,
    memory_write_preflight,
    normalize_memory_content,
)
from app.memory.layers import (
    demote_memory_payload,
    layer_move_payload,
    promote_memory_payload,
    promotion_candidates_payload,
    validate_layer_transition,
)
from app.memory.lifecycle import deprecate_memory_payload, reject_memory_payload, return_memory_to_review_payload
from app.memory.lifecycle_integrity import get_memory_lifecycle_integrity_report_payload
from app.memory.lifecycle_pointer_remediation import (
    POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
    get_memory_pointer_lifecycle_remediation_inventory_payload,
    preview_memory_pointer_lifecycle_remediation_payload,
)
from app.memory.lifecycle_pointer_execution import (
    POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION,
    preview_memory_pointer_lifecycle_remediation_execution_payload,
)
from app.memory.lifecycle_pointer_apply import (
    apply_memory_pointer_lifecycle_remediation_execution_payload,
    get_memory_pointer_lifecycle_remediation_execution_run_payload,
    preview_memory_pointer_lifecycle_remediation_execution_rollback_payload,
    rollback_memory_pointer_lifecycle_remediation_execution_payload,
)
from app.memory.lifecycle_remediation import (
    LIFECYCLE_REMEDIATION_PLAN_VERSION,
    apply_memory_lifecycle_remediation_payload,
    get_memory_lifecycle_remediation_inventory_payload,
    get_memory_lifecycle_remediation_run_payload,
    preview_memory_lifecycle_remediation_payload,
    preview_memory_lifecycle_remediation_rollback_payload,
    rollback_memory_lifecycle_remediation_payload,
)
from app.memory.link_views import attach_links_to_memory_items, link_memories_payload, memory_links_response
from app.memory.linking import (
    preview_memory_linking_pass_payload,
    run_memory_linking_pass_payload,
)
from app.memory.observability import queue_observability_metrics_payload
from app.memory.retrieval_baseline import (
    evaluate_golden_cases,
    load_retrieval_golden_corpus,
    materialize_golden_cases,
)
from app.memory.agent_gravity import (
    build_agent_gravity_preview,
    build_gravity_context_block,
    build_gravity_shadow_comparison,
    gravity_policy,
)
from app.memory.agent_self_delta import build_agent_self_delta_payload
from app.memory.agent_self_narrative import build_agent_self_narrative_payload
from app.memory.agent_self_model import (
    build_agent_autobiographical_timeline_payload,
    build_agent_commitment_ledger_payload,
    build_agent_self_capsule_payload,
    build_agent_self_snapshot_payload,
)
from app.memory.pagination import (
    COMPACT_FIELDS,
    DEFAULT_FIELDS,
    MEMORY_LIST_ORDER,
    PROJECTION_FIELDS,
    list_memory_page,
    normalize_projection,
)
from app.memory.ownership import bulk_set_memory_owner_payload, set_memory_owner_payload
from app.memory.provenance_backfill import apply_provenance_backfill, build_provenance_backfill_preview
from app.memory.provenance_context import resolve_write_provenance
from app.memory.provenance import get_memory_provenance_payload, list_memory_audit_payload
from app.memory.project_keys import (
    list_project_key_aliases_payload,
    project_key_filter_values,
    upsert_project_key_alias_payload,
)
from app.memory.project_keys import resolve_canonical_project_key
from app.memory.relation_contracts import (
    CANONICAL_MEMORY_RELATIONS,
    get_relation_contracts_payload,
    normalize_relation,
    preview_relation_payload,
)
from app.memory.evidence_relations import (
    EVIDENCE_BOUND_RELATIONS,
    apply_evidence_relation_payload,
    preview_evidence_relation_payload,
    preview_evidence_relation_rollback_payload,
    rollback_evidence_relation_payload,
)
from app.memory.canonical_truth_review import build_canonical_truth_review_payload
from app.memory.legacy_graph_audit import build_legacy_graph_audit_payload
from app.operations_observability import operations_observability_payload
from app.runtime.doctor import collect_doctor_report
from app.runtime.recovery import build_recovery_plan
from app.memory.reconciliation import (
    preview_memory_capture_reconciliation_payload,
)
from app.memory.reconciliation_apply import apply_memory_capture_reconciliation_payload
from app.memory.retention import (
    preview_memory_retention_policy_payload,
    preview_project_memory_retention_payload,
)
from app.memory.retention_review import (
    decide_retention_review_item,
    get_retention_review_item,
    list_retention_review_items,
    save_retention_review_item,
)
from app.memory.retention_apply import (
    apply_memory_retention_batch_payload,
    apply_memory_retention_review_payload,
    preview_memory_retention_rollback_payload,
    rollback_memory_retention_review_payload,
)
from app.memory.sensitivity import capture_sensitivity_gate
from app.sandman import canonical_runtime as sandman_canonical_runtime
from app.sandman import router as sandman_v3_router
from app.sandman import evaluation as sandman_v3_evaluation
from app.sandman import observability as sandman_v3_observability
from app.sandman import routing as sandman_v3_routing
from app.sandman import shadow as sandman_gemini_shadow
from app.sandman import shadow_repository as sandman_shadow_repository
from app.sandman.contracts import canonical_fingerprint
from app.sandman.providers.gemini import (
    GeminiConfig,
    GeminiShadowProvider,
    GoogleGenAIInteractionsTransport,
    get_shared_model_circuit_breaker,
)
from app.memory.quality import (
    count_project_scope_mismatches,
    list_project_scope_mismatches_payload,
    project_scope_mismatch_rows,
    quality_gate_issues_for_memory,
    tag_count,
)
from app.memory.query_builders import memory_order_clause, memory_query_parts, text_search_terms
from app.memory.queues import (
    list_duplicate_candidates_admin_payload,
    list_expired_memories_payload,
    list_overdue_memory_queue_payload,
    list_overdue_duplicate_queue_payload,
    list_revalidation_queue_payload,
    list_review_queue_payload,
)
from app.memory.review_flow import approve_memory_payload, create_memory_draft_payload, preview_memory_quality_gate_payload
from app.memory.semantic import backfill_semantic_embeddings_payload, search_semantic_payload, semantic_embedding_stats_payload
from app.memory.sleep_runs import get_sleep_run_actions_payload, get_sleep_run_payload, list_sleep_runs_payload
from app.memory.sla import bulk_set_memory_sla_payload, compute_sla_days, list_sla_policies_payload, set_memory_priority_payload, set_memory_sla_payload, sla_policy_observability_payload, upsert_sla_policy_payload
from app.memory.supersession import (
    apply_memory_supersession_payload,
    get_memory_supersession_run_payload,
    list_memory_supersession_runs_payload,
    preview_memory_supersession_payload,
    preview_memory_supersession_rollback_payload,
    rollback_memory_supersession_run_payload,
)
from app.memory.versions import collect_version_lineage
from app.owners.governance_warnings import (
    owner_deactivation_guardrail_warnings,
    owner_directory_governance_warnings,
    owner_key_governance_warnings,
    owner_mapping_governance_warnings,
    owner_metadata_governance_warnings,
)
from app.owners.catalog_health import (
    get_owner_catalog_health_data,
    get_owner_catalog_health_payload,
    get_owner_catalog_repair_summary_payload,
    get_owner_catalog_governance_history_payload,
    get_owner_catalog_governance_checklist_payload,
    get_owner_governance_history_payload,
    get_owner_mapping_batch_candidates_payload,
    get_owner_mapping_repair_audit_payload,
    get_owner_rollout_summary_payload,
    get_problematic_owner_mappings_payload,
    list_owner_directory_items_payload,
    list_owner_role_mappings_payload,
    suggest_owner_mapping_repairs,
)
from app.owners.catalog_mutations import (
    bulk_repair_owner_mappings_payload,
    preview_bulk_repair_owner_mappings_payload,
    repair_owner_mapping_issue_payload,
    rollout_owner_catalog_to_project_payload,
    set_owner_target_active_payload,
    upsert_owner_directory_item_payload,
    upsert_owner_role_mapping_payload,
)
from app.owners.serializers import owner_directory_item_to_dict, owner_mapping_rank, owner_role_mapping_to_dict
from app.owners.summaries import (
    accumulate_effective_owner_workload,
    effective_owner_summary_from_items,
    effective_owner_workload_payload,
    filter_items_by_effective_owner,
    operational_queue_dashboard_payload,
    owner_rebalance_candidates_payload,
    owner_summary_from_items,
    rebalance_candidate_items,
    recommended_bulk_actions,
)
from app.owners.workload_tools import effective_owner_workload_tool_payload, operational_queue_dashboard_tool_payload
from app.owners.validation import (
    ALLOWED_OWNER_TYPES,
    owner_catalog_audit_project_key,
    validate_new_owner_target_payload,
    validate_owner_key_format,
    validate_project_override_payload,
)
from app.memory.duplicate_review import bulk_set_duplicate_candidate_sla_payload, duplicate_review_item_to_dict, get_or_create_duplicate_review_item, set_duplicate_candidate_sla_payload
from app.schemas import (
    LAYER_ORDER,
    SANDMAN_PROTECTED_LAYERS,
    SANDMAN_PROTECTED_STATES,
    derive_state_code,
    derive_truth_kind,
    enrich_memory_dict,
    normalize_area_code,
    normalize_memory_entry_type,
    normalize_memory_v2_status,
    normalize_layer_code,
    normalize_optional_text,
    normalize_required_text,
    normalize_scope_code,
    normalize_state_code,
    normalize_truth_kind,
)
from memory_bootstrap_policy import (
    BootstrapPolicy,
    build_core_identity_sql,
    build_project_anchors_sql,
    build_recent_project_sql,
    project_anchor_tags_for,
    project_anchor_exclude_tags_for,
)
from mcp_surface import (
    current_surface_profile,
    install_mcp_surface_filter,
    open_workshop_payload,
    profile_allows,
    resolve_workshop_action,
    surface_manifest,
    workshop_index,
)

mcp = FastMCP("MAPI")
install_mcp_surface_filter(mcp)

ROOT = config.ROOT
DATA_DIR = config.DATA_DIR
DB_PATH = config.DB_PATH

SAFE_ROLLBACK_ACTION_TYPES = {
    "archived",
    "downgraded",
    "duplicate_link_created",
    "support_link_created",
    "summary_link_created",
    "summary_memory_created",
    "summary_memory_updated",
    "summary_link_deleted",
    "conflict_link_created",
    "dream_link_created",
    "conflict_flagged",
    "canonical_evidence_boosted",
    "valid_to_set",
}

CROSS_PROJECT_FLAG_KEY = "cross_project_knowledge_layer"
CONFLICT_EXPLAINER_FLAG_KEY = "conflict_explainer"
CONFLICT_PREVIEW_RESOLUTION_FLAG_KEY = "conflict_preview_resolution"
CONFLICT_AUTO_RESOLUTION_FLAG_KEY = "conflict_auto_resolution"
MEMORY_V2_FLAG_KEY = "memory_v2_enabled"
MEMORY_V3_CAPTURE_RECONCILIATION_FLAG_KEY = "memory_v3_capture_reconciliation_enabled"
MEMORY_V3_RETENTION_FLAG_KEY = "memory_v3_retention_enabled"
FEATURE_FLAG_ROLLOUT_MODES = {"off", "all", "projects", "scopes", "projects_and_scopes"}
SANDMAN_GEMMA_HYGIENE_FLAG_KEY = "sandman_gemma_hygiene_enabled"
SANDMAN_PROVIDER_V3_FLAG_KEY = "sandman_provider_v3_enabled"


def _sync_config() -> None:
    configure_runtime_context(root=ROOT, data_dir=DATA_DIR, db_path=DB_PATH)


def safe_path(user_path: str | None):
    _sync_config()
    return store.safe_path(user_path)


def rel_path(path):
    _sync_config()
    return store.rel_path(path)


def guess_mime(path):
    return store.guess_mime(path)


def normalize_score(value: float) -> float:
    return core_normalize_score(value, normalizer=store.normalize_score)


def utc_now_iso() -> str:
    return core_utc_now_iso()


def utc_offset_days_iso(days: int) -> str:
    return core_utc_offset_days_iso(days)


def shift_iso_days(value: str | None, days: int) -> str | None:
    return core_shift_iso_days(value, days, normalize_optional_text=normalize_optional_text)


def get_db_connection():
    _sync_config()
    conn = store.get_db_connection()
    db_migrations.apply_all_migrations(conn)
    return conn


def parse_params_json(params_json: str):
    return store.parse_params_json(params_json)


def is_read_only_sql(query: str) -> bool:
    return store.is_read_only_sql(query)


def row_to_dict(row):
    return store.row_to_dict(row)


def insert_memory_event(
    conn,
    *,
    memory_id: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
):
    return insert_memory_event_payload(
        conn,
        memory_id=memory_id,
        event_type=event_type,
        payload=payload,
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        row_to_dict=row_to_dict,
    )


def require_memory_row(conn, memory_id: int):
    return store.require_memory_row(conn, memory_id)


def require_sleep_run_row(conn, run_id: int):
    return store.require_sleep_run_row(conn, run_id)


def create_sleep_run(conn, mode: str, freedom_level: int, notes: str | None = None, rollback_of_run_id: int | None = None, workspace_id: int | None = None, project_key: str | None = None) -> int:
    return store.create_sleep_run(conn, mode, freedom_level, notes, rollback_of_run_id, workspace_id=workspace_id, project_key=project_key)


def add_sleep_action(conn, run_id: int, action_type: str, memory_id: int | None, old_value: Any, new_value: Any, reason: str) -> None:
    store.add_sleep_action(conn, run_id, action_type, memory_id, old_value, new_value, reason)


def finalize_sleep_run(conn, run_id: int, **kwargs: Any) -> None:
    store.finalize_sleep_run(conn, run_id, **kwargs)


def _decode_action_value(value: Any) -> Any:
    return store.decode_action_value(value)


def _existing_rollback_run_id(conn, target_run_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM sleep_runs WHERE rollback_of_run_id = ? ORDER BY id DESC LIMIT 1",
        (target_run_id,),
    ).fetchone()
    return None if row is None else int(row["id"])


def _get_rollbackable_actions(conn, target_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM sleep_run_actions WHERE run_id = ? ORDER BY id DESC",
        (target_run_id,),
    ).fetchall()
    actions = [row_to_dict(row) for row in rows]
    return [item for item in actions if item["action_type"] in SAFE_ROLLBACK_ACTION_TYPES]




def _ensure_memory_embedding_best_effort(conn, memory: dict[str, Any]) -> dict[str, Any]:
    """Best-effort semantic embedding for a memory, with observable status."""
    memory_id = memory.get("id")
    try:
        memory_id_int = int(memory_id)
    except (TypeError, ValueError):
        return {"status": "error", "memory_id": memory_id, "error_type": "ValueError", "error": "memory.id is missing or invalid"}
    try:
        from vector_store import ensure_embeddings_table, embed_memory
        ensure_embeddings_table(conn)
        existing = conn.execute(
            "SELECT memory_id, model_name, created_at, updated_at FROM memory_embeddings_meta WHERE memory_id = ?",
            (memory_id_int,),
        ).fetchone()
        if existing is not None:
            keys = existing.keys()
            return {
                "status": "already_present",
                "memory_id": memory_id_int,
                "model_name": existing["model_name"] if "model_name" in keys else None,
                "created_at": existing["created_at"] if "created_at" in keys else None,
                "updated_at": existing["updated_at"] if "updated_at" in keys else None,
            }
        embed_memory(conn, {"id": memory_id_int, "content": memory.get("content"), "summary_short": memory.get("summary_short"), "tags": memory.get("tags")})
        embedded = conn.execute(
            "SELECT memory_id, model_name, created_at, updated_at FROM memory_embeddings_meta WHERE memory_id = ?",
            (memory_id_int,),
        ).fetchone()
        if embedded is None:
            return {"status": "missing_after_embed", "memory_id": memory_id_int}
        keys = embedded.keys()
        return {
            "status": "embedded",
            "memory_id": memory_id_int,
            "model_name": embedded["model_name"] if "model_name" in keys else None,
            "created_at": embedded["created_at"] if "created_at" in keys else None,
            "updated_at": embedded["updated_at"] if "updated_at" in keys else None,
        }
    except Exception as exc:
        return {"status": "error", "memory_id": memory_id_int, "error_type": type(exc).__name__, "error": str(exc)}



def _tags_allow_global_project_scope(tags: str | None) -> bool:
    normalized_tags = normalize_optional_text(tags) or ""
    return "allow-global-project-scope" in {
        tag.strip().lower()
        for tag in normalized_tags.split(",")
        if tag.strip()
    }


def _coerce_project_scope_on_create(
    *,
    project_key: str | None,
    scope_code: str | None,
    tags: str | None,
    conn=None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Prevent accidental project memories from entering as global scope.

    Explicit exceptions are allowed with the allow-global-project-scope tag or when the
    cross_project_knowledge_layer feature flag is enabled for this project/scope combination.
    Otherwise project_key plus global or omitted scope is normalized to project scope at write time.
    """
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)
    if normalized_project_key is None:
        return normalized_scope_code, None
    raw_scope_code = normalize_optional_text(scope_code)
    if normalized_scope_code != "global" and raw_scope_code is not None:
        return normalized_scope_code, None
    if _tags_allow_global_project_scope(tags):
        return normalized_scope_code, {
            "status": "allowed_global_project_scope",
            "reason": "allow-global-project-scope tag present",
            "project_key": normalized_project_key,
            "scope_code": normalized_scope_code,
        }
    if conn is not None:
        flag = _get_feature_flag_config(conn, CROSS_PROJECT_FLAG_KEY)
        evaluation = _evaluate_feature_flag_config(flag, project_key=project_key, scope_code=normalized_scope_code)
        if evaluation["enabled"]:
            return normalized_scope_code, {
                "status": "allowed_by_feature_flag",
                "reason": "cross_project_knowledge_layer feature flag enabled",
                "project_key": normalized_project_key,
                "scope_code": normalized_scope_code,
            }
    return "project", {
        "status": "auto_normalized_project_scope",
        "reason": "project_key memories default to project scope unless explicitly allowed as global",
        "project_key": normalized_project_key,
        "from_scope_code": raw_scope_code or "<default-global>",
        "to_scope_code": "project",
    }


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
    layer_code: str | None = None,
    area_code: str | None = None,
    state_code: str | None = None,
    scope_code: str | None = None,
    parent_memory_id: int | None = None,
    version: int = 1,
    promoted_from_id: int | None = None,
    demoted_from_id: int | None = None,
    supersedes_memory_id: int | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    decay_score: float = 0.0,
    emotional_weight: float = 0.0,
    identity_weight: float = 0.0,
    project_key: str | None = None,
    conversation_key: str | None = None,
    last_validated_at: str | None = None,
    validation_source: str | None = None,
    schema_version: int = 2,
    entry_type: str | None = None,
    truth_kind: str | None = None,
    title: str | None = None,
    source_context: str | None = None,
    source_event_ref: str | None = None,
    updated_at: str | None = None,
    last_confirmed_at: str | None = None,
    memory_v2_status: str | None = None,
    importance_level: str | None = None,
    superseded_by_memory_id: int | None = None,
    requires_user_confirmation: bool = False,
    should_resurface_when: list[str] | tuple[str, ...] | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    review_due_at: str | None = None,
    revalidation_due_at: str | None = None,
    expired_due_at: str | None = None,
    priority: str | None = None,
    # --- Multi-user fields (Stage 1) ---
    visibility_scope: str | None = None,
    workspace_id: int | None = None,
    owner_user_id: int | None = None,
    created_by_user_id: int | None = None,
    last_modified_by_user_id: int | None = None,
    sharing_policy: str | None = None,
    ensure_embedding: bool = True,
) -> dict[str, Any]:
    now_iso = utc_now_iso()
    cursor = conn.cursor()
    normalized_state_code = schemas.derive_state_code(state_code)
    normalized_entry_type = normalize_memory_entry_type(entry_type) or schemas.derive_entry_type(
        entry_type=entry_type,
        memory_type=memory_type,
        layer_code=layer_code,
        area_code=area_code,
        project_key=project_key,
    )
    normalized_truth_kind = normalize_truth_kind(truth_kind) or derive_truth_kind(
        truth_kind=truth_kind,
        entry_type=normalized_entry_type,
        memory_type=memory_type,
        area_code=area_code,
    )
    normalized_memory_v2_status = normalize_memory_v2_status(memory_v2_status) or schemas.derive_memory_v2_status(
        memory_v2_status=memory_v2_status,
        state_code=normalized_state_code,
        activity_state=None,
        contradiction_flag=None,
    )
    normalized_title = normalize_optional_text(title) or normalize_optional_text(summary_short) or normalize_required_text(memory_type, "title")
    normalized_updated_at = normalize_optional_text(updated_at) or now_iso
    normalized_last_confirmed_at = normalize_optional_text(last_confirmed_at) or normalize_optional_text(last_validated_at)
    normalized_importance_level = schemas.normalize_importance_level(importance_level) or schemas.derive_importance_level(importance_score)
    normalized_should_resurface_when = [
        value
        for value in (normalize_optional_text(item) for item in (should_resurface_when or ()))
        if value
    ]
    activity_state = "archived" if normalized_state_code == "archived" else "active"
    normalized_scope_code, scope_gate = _coerce_project_scope_on_create(
        project_key=project_key,
        scope_code=scope_code,
        tags=tags,
        conn=conn,
    )
    normalized_priority = normalize_optional_text(priority) or "normal"
    normalized_owner_role = normalize_optional_text(owner_role) or _default_owner_role(
        state_code=normalized_state_code,
        scope_code=normalized_scope_code,
        project_key=project_key,
    )
    normalized_review_due_at, normalized_revalidation_due_at = _default_due_at(
        conn=conn,
        state_code=normalized_state_code,
        review_due_at=review_due_at,
        revalidation_due_at=revalidation_due_at,
        priority=normalized_priority,
        memory_type=memory_type,
        scope_code=normalized_scope_code,
        project_key=project_key,
    )
    # Ustal workspace_id Ă˘â‚¬â€ť fallback do default workspace
    resolved_workspace_id = workspace_id
    if resolved_workspace_id is None:
        ws_row = conn.execute(
            "SELECT id FROM workspaces WHERE workspace_key = 'default' LIMIT 1"
        ).fetchone()
        if ws_row:
            resolved_workspace_id = int(ws_row["id"])

    # Ustal visibility_scope jeÄąâ€şli nie podany jawnie
    resolved_visibility_scope = normalize_optional_text(visibility_scope) or infer_visibility_scope(
        memory_type=memory_type,
        project_key=project_key,
        workspace_id=resolved_workspace_id,
        owner_user_id=owner_user_id,
    )
    resolved_sharing_policy = normalize_optional_text(sharing_policy) or "explicit"

    # BUG2: prywatny rekord zawsze musi mieĂ„â€ˇ owner_user_id (DoD Stage 1)
    resolved_owner_user_id = owner_user_id
    if resolved_visibility_scope == "private" and resolved_owner_user_id is None:
        legacy_row = conn.execute(
            "SELECT id FROM users WHERE external_user_key = 'system:legacy' LIMIT 1"
        ).fetchone()
        if legacy_row:
            resolved_owner_user_id = int(legacy_row["id"])

    insert_columns = [
        "content",
        "summary_short",
        "memory_type",
        "source",
        "importance_score",
        "confidence_score",
        "tags",
        "created_at",
        "last_accessed_at",
        "activity_state",
        "evidence_count",
        "contradiction_flag",
        "layer_code",
        "area_code",
        "state_code",
        "scope_code",
        "parent_memory_id",
        "version",
        "promoted_from_id",
        "demoted_from_id",
        "supersedes_memory_id",
        "valid_from",
        "valid_to",
        "decay_score",
        "emotional_weight",
        "identity_weight",
        "project_key",
        "conversation_key",
        "last_validated_at",
        "validation_source",
        "schema_version",
        "entry_type",
        "truth_kind",
        "title",
        "source_context",
        "source_event_ref",
        "updated_at",
        "last_confirmed_at",
        "memory_v2_status",
        "importance_level",
        "superseded_by_memory_id",
        "requires_user_confirmation",
        "should_resurface_when_json",
        "owner_role",
        "owner_id",
        "review_due_at",
        "revalidation_due_at",
        "expired_due_at",
        "priority",
        "visibility_scope",
        "workspace_id",
        "owner_user_id",
        "created_by_user_id",
        "last_modified_by_user_id",
        "sharing_policy",
    ]
    insert_values = [
        normalize_required_text(content, "content"),
        normalize_optional_text(summary_short),
        normalize_required_text(memory_type, "memory_type"),
        normalize_optional_text(source),
        normalize_score(importance_score),
        normalize_score(confidence_score),
        normalize_optional_text(tags),
        now_iso,
        now_iso,
        activity_state,
        1,
        0,
        normalize_layer_code(layer_code),
        normalize_area_code(area_code),
        normalized_state_code,
        normalized_scope_code,
        parent_memory_id,
        max(int(version or 1), 1),
        promoted_from_id,
        demoted_from_id,
        supersedes_memory_id,
        normalize_optional_text(valid_from),
        normalize_optional_text(valid_to),
        normalize_score(decay_score),
        normalize_score(emotional_weight),
        normalize_score(identity_weight),
        normalize_optional_text(project_key),
        normalize_optional_text(conversation_key),
        normalize_optional_text(last_validated_at),
        normalize_optional_text(validation_source),
        max(int(schema_version or 1), 1),
        normalized_entry_type,
        normalized_truth_kind,
        normalized_title,
        normalize_optional_text(source_context),
        normalize_optional_text(source_event_ref),
        normalized_updated_at,
        normalized_last_confirmed_at,
        normalized_memory_v2_status,
        normalized_importance_level,
        superseded_by_memory_id,
        1 if requires_user_confirmation else 0,
        json.dumps(normalized_should_resurface_when, ensure_ascii=False),
        normalized_owner_role,
        normalize_optional_text(owner_id),
        normalized_review_due_at,
        normalized_revalidation_due_at,
        normalize_optional_text(expired_due_at),
        normalized_priority,
        resolved_visibility_scope,
        resolved_workspace_id,
        resolved_owner_user_id,
        created_by_user_id,
        last_modified_by_user_id,
        resolved_sharing_policy,
    ]
    placeholders = ", ".join(["?" for _ in insert_columns])
    cursor.execute(
        f"INSERT INTO memories ({', '.join(insert_columns)}) VALUES ({placeholders})",
        insert_values,
    )
    memory_id = int(cursor.lastrowid)
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    result = enrich_memory_dict(row_to_dict(row))
    if scope_gate is not None:
        result["scope_gate"] = scope_gate
    result["embedding_hook"] = (
        _ensure_memory_embedding_best_effort(conn, result)
        if ensure_embedding
        else {"status": "deferred", "memory_id": memory_id, "reason": "owner_managed_transaction"}
    )
    return result



def _create_link(
    conn,
    from_memory_id: int,
    to_memory_id: int,
    relation_type: str,
    weight: float,
    origin: str | None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    created_at = utc_now_iso()
    cursor = conn.cursor()

    # Dziedzicz workspace_id ze wspomnienia ÄąĹźrÄ‚Ĺ‚dÄąâ€šowego (Stage 1 Ă˘â‚¬â€ť multi-user)
    src_row = conn.execute(
        "SELECT workspace_id FROM memories WHERE id = ? LIMIT 1",
        (from_memory_id,),
    ).fetchone()
    link_workspace_id = int(src_row["workspace_id"]) if src_row and src_row["workspace_id"] is not None else None

    cursor.execute(
        """
        INSERT INTO memory_links
            (from_memory_id, to_memory_id, relation_type, weight, origin, created_at,
             workspace_id, visibility_scope)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'inherited')
        """,
        (from_memory_id, to_memory_id, relation_type, float(weight), origin, created_at, link_workspace_id),
    )
    link_id = int(cursor.lastrowid)
    row = conn.execute("SELECT * FROM memory_links WHERE id = ?", (link_id,)).fetchone()
    return row_to_dict(row)


def _rollback_single_action(conn, rollback_run_id: int, action: dict[str, Any]) -> dict[str, Any]:
    action_type = str(action["action_type"])
    memory_id = action.get("memory_id")
    old_value = _decode_action_value(action.get("old_value"))
    new_value = _decode_action_value(action.get("new_value"))

    if action_type == "archived":
        previous_state = "active"
        previous_archived_at = None
        if isinstance(old_value, dict):
            previous_state = old_value.get("activity_state", "active") or "active"
            previous_archived_at = old_value.get("archived_at")
        conn.execute(
            "UPDATE memories SET activity_state = ?, archived_at = ?, sandman_note = NULL WHERE id = ?",
            (previous_state, previous_archived_at, int(memory_id)),
        )
        result = {"restored_memory_id": int(memory_id), "activity_state": previous_state, "archived_at": previous_archived_at}
    elif action_type == "downgraded":
        previous_importance = float(old_value.get("importance_score")) if isinstance(old_value, dict) else None
        if previous_importance is None:
            raise ValueError("Brak old importance_score dla akcji downgraded")
        conn.execute(
            "UPDATE memories SET importance_score = ?, sandman_note = NULL WHERE id = ?",
            (previous_importance, int(memory_id)),
        )
        result = {"restored_memory_id": int(memory_id), "importance_score": previous_importance}
    elif action_type in {"duplicate_link_created", "support_link_created", "summary_link_created", "conflict_link_created", "dream_link_created"}:
        link_id = None
        if isinstance(new_value, dict):
            link_id = new_value.get("link_id", new_value.get("id"))
        if link_id is None:
            raise ValueError(f"Brak link_id dla akcji {action_type}")
        conn.execute("DELETE FROM memory_links WHERE id = ?", (int(link_id),))
        result = {"deleted_link_id": int(link_id)}
    elif action_type == "summary_memory_created":
        created_memory_id = None
        if isinstance(new_value, dict):
            created_memory_id = new_value.get("memory_id", new_value.get("id"))
        if created_memory_id is None:
            raise ValueError("Brak memory_id dla akcji summary_memory_created")
        conn.execute(
            """
            DELETE FROM timeline_events
            WHERE memory_id = ?
               OR related_memory_id = ?
               OR (source_table = 'memories' AND source_row_id = ?)
            """,
            (int(created_memory_id), int(created_memory_id), int(created_memory_id)),
        )
        conn.execute("DELETE FROM memories WHERE id = ?", (int(created_memory_id),))
        result = {"deleted_memory_id": int(created_memory_id)}
        # memory was deleted Ă˘â‚¬â€ť don't reference its id in the rollback action record
        memory_id = None
    elif action_type == "summary_memory_updated":
        if memory_id is None or not isinstance(old_value, dict):
            raise ValueError("Brak danych do rollback summary_memory_updated")
        conn.execute(
            """
            UPDATE memories
            SET summary_short = ?, content = ?, source = ?, importance_score = ?, confidence_score = ?, tags = ?
            WHERE id = ?
            """,
            (
                old_value.get("summary_short"),
                old_value.get("content"),
                old_value.get("source"),
                float(old_value.get("importance_score") or 0.5),
                float(old_value.get("confidence_score") or 0.5),
                old_value.get("tags"),
                int(memory_id),
            ),
        )
        result = {"restored_memory_id": int(memory_id), **old_value}
    elif action_type == "summary_link_deleted":
        if not isinstance(old_value, dict):
            raise ValueError("Brak danych linku do rollback summary_link_deleted")
        recreated = _create_link(
            conn,
            int(old_value["from_memory_id"]),
            int(old_value["to_memory_id"]),
            str(old_value["relation_type"]),
            float(old_value.get("weight") or 1.0),
            old_value.get("origin"),
        )
        result = {"recreated_link_id": int(recreated["id"]), "relation_type": recreated["relation_type"]}
    elif action_type == "conflict_flagged":
        previous_flag = int(old_value.get("contradiction_flag", 0) or 0) if isinstance(old_value, dict) else 0
        conn.execute("UPDATE memories SET contradiction_flag = ? WHERE id = ?", (previous_flag, int(memory_id)))
        result = {"restored_memory_id": int(memory_id), "contradiction_flag": previous_flag}
    elif action_type == "canonical_evidence_boosted":
        previous_evidence = int(old_value.get("evidence_count", 1) or 1) if isinstance(old_value, dict) else 1
        conn.execute(
            "UPDATE memories SET evidence_count = ?, sandman_note = NULL WHERE id = ?",
            (previous_evidence, int(memory_id)),
        )
        result = {"restored_memory_id": int(memory_id), "evidence_count": previous_evidence}
    elif action_type == "valid_to_set":
        previous_valid_to = old_value.get("valid_to") if isinstance(old_value, dict) else None
        conn.execute("UPDATE memories SET valid_to = ? WHERE id = ?", (previous_valid_to, int(memory_id)))
        result = {"restored_memory_id": int(memory_id), "valid_to": previous_valid_to}
    else:
        raise ValueError(f"NieobsÄąâ€šugiwany action_type do rollback: {action_type}")

    add_sleep_action(
        conn,
        rollback_run_id,
        f"rollback_{action_type}",
        None if memory_id is None else int(memory_id),
        new_value,
        result,
        f"rollback_of_action_{action.get('id')}",
    )
    return {"action_type": action_type, **result}


def _default_owner_role(*, state_code: str | None = None, scope_code: str | None = None, project_key: str | None = None) -> str | None:
    normalized_state = normalize_state_code(state_code)
    normalized_scope = normalize_scope_code(scope_code)
    normalized_project_key = normalize_optional_text(project_key)

    if normalized_state == "candidate":
        if normalized_scope == "global":
            return "maintainer"
        if normalized_project_key:
            return "project_maintainer"
        return "review_team"
    if normalized_state == "validated":
        if normalized_scope == "global":
            return "knowledge_curator"
        if normalized_project_key:
            return "project_maintainer"
        return "review_team"
    if normalized_state == "superseded":
        if normalized_scope == "global":
            return "knowledge_curator"
        if normalized_project_key:
            return "project_maintainer"
        return "review_team"
    return None


def _default_due_at(
    *,
    conn=None,
    state_code: str | None = None,
    review_due_at: str | None = None,
    revalidation_due_at: str | None = None,
    priority: str | None = "normal",
    memory_type: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
) -> tuple[str | None, str | None]:
    normalized_state = normalize_state_code(state_code)
    normalized_review_due_at = normalize_optional_text(review_due_at)
    normalized_revalidation_due_at = normalize_optional_text(revalidation_due_at)
    if normalized_state == "candidate" and normalized_review_due_at is None:
        days = _compute_sla_days(conn, "review", priority, memory_type, scope_code, project_key) if conn is not None else 2
        normalized_review_due_at = utc_offset_days_iso(days)
    if (
        normalized_state == "validated"
        and normalized_revalidation_due_at is None
        and memory_hygiene.should_schedule_revalidation(memory_type=memory_type)
    ):
        days = _compute_sla_days(conn, "revalidation", priority, memory_type, scope_code, project_key) if conn is not None else 5
        normalized_revalidation_due_at = utc_offset_days_iso(days)
    return normalized_review_due_at, normalized_revalidation_due_at


def _compute_sla_days(
    conn,
    queue_type: str,
    priority: str | None = "normal",
    memory_type: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
) -> int:
    return compute_sla_days(
        conn,
        queue_type,
        priority=priority,
        memory_type=memory_type,
        scope_code=scope_code,
        project_key=project_key,
    )


def _apply_ownership_defaults(memory: dict[str, Any]) -> dict[str, Any]:
    item = dict(memory)
    owner_recommendation = memory_hygiene.recommend_owner_role(item)
    current_owner_role = normalize_optional_text(item.get("owner_role"))
    if (
        current_owner_role is None
        or str(current_owner_role).casefold() in memory_hygiene.LEGACY_OWNER_ROLE_ALIASES
    ):
        item["owner_role"] = owner_recommendation["value"]
    review_due_at, revalidation_due_at = _default_due_at(
        state_code=item.get("state_code"),
        review_due_at=item.get("review_due_at"),
        revalidation_due_at=item.get("revalidation_due_at"),
    )
    item["review_due_at"] = review_due_at
    item["revalidation_due_at"] = revalidation_due_at
    if normalize_optional_text(item.get("expired_due_at")) is None and normalize_optional_text(item.get("valid_to")) is not None and normalize_state_code(item.get("state_code")) == "superseded":
        item["expired_due_at"] = shift_iso_days(item.get("valid_to"), 2)
    return item


def _owner_directory_item_to_dict(row) -> dict[str, Any]:
    return owner_directory_item_to_dict(row, row_to_dict=row_to_dict)


def _owner_role_mapping_to_dict(row) -> dict[str, Any]:
    return owner_role_mapping_to_dict(row, row_to_dict=row_to_dict)


def _owner_mapping_rank(mapping: dict[str, Any], *, project_key: str | None, scope_code: str | None) -> tuple[int, int, int]:
    return owner_mapping_rank(
        mapping,
        project_key=project_key,
        scope_code=scope_code,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
    )


def _resolve_effective_owner(conn, *, owner_role: str | None, project_key: str | None = None, scope_code: str | None = None) -> dict[str, Any]:
    normalized_owner_role = normalize_optional_text(owner_role)
    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)
    base = {
        "owner_role": normalized_owner_role,
        "effective_owner_key": None,
        "effective_owner_type": None,
        "effective_display_name": None,
        "effective_owner_active": False,
        "owner_resolution_reason": None,
        "owner_mapping": None,
    }
    if normalized_owner_role is None:
        base["owner_resolution_reason"] = "no_owner_role"
        return base

    rows = conn.execute(
        "SELECT * FROM owner_role_mappings WHERE owner_role = ? AND is_active = 1 ORDER BY id ASC",
        (normalized_owner_role,),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        mapping = _owner_role_mapping_to_dict(row)
        mapping_project_key = normalize_optional_text(mapping.get("project_key"))
        mapping_scope_code = normalize_scope_code(mapping.get("scope_code"))
        if mapping_project_key is not None and mapping_project_key != normalized_project_key:
            continue
        if mapping_scope_code is not None and mapping_scope_code != normalized_scope_code:
            continue
        candidates.append(mapping)
    if not candidates:
        base["owner_resolution_reason"] = "no_mapping"
        return base

    selected = sorted(candidates, key=lambda item: _owner_mapping_rank(item, project_key=normalized_project_key, scope_code=normalized_scope_code), reverse=True)[0]
    owner_row = conn.execute("SELECT * FROM owner_directory_items WHERE owner_key = ?", (selected["owner_key"],)).fetchone()
    if owner_row is None:
        base["owner_resolution_reason"] = "owner_missing_in_directory"
        base["owner_mapping"] = selected
        return base
    owner_item = _owner_directory_item_to_dict(owner_row)
    base.update(
        {
            "effective_owner_key": owner_item["owner_key"],
            "effective_owner_type": owner_item["owner_type"],
            "effective_display_name": owner_item["display_name"],
            "effective_owner_active": bool(owner_item["is_active"]),
            "owner_mapping": selected,
            "owner_resolution_reason": "resolved" if bool(owner_item["is_active"]) else "owner_inactive",
        }
    )
    return base


def _apply_effective_owner(conn, item: dict[str, Any], *, owner_field: str | None = None) -> dict[str, Any]:
    result = dict(item)
    target = result if owner_field is None else result.get(owner_field)
    if not isinstance(target, dict):
        return result
    resolution = _resolve_effective_owner(
        conn,
        owner_role=target.get("owner_role"),
        project_key=target.get("project_key"),
        scope_code=target.get("scope_code"),
    )
    target.update(resolution)
    return result


def _duplicate_review_item_to_dict(row) -> dict[str, Any]:
    return duplicate_review_item_to_dict(row, row_to_dict=row_to_dict)


def _get_or_create_duplicate_review_item(conn, canonical_memory_id: int, duplicate_memory_id: int) -> dict[str, Any]:
    return get_or_create_duplicate_review_item(
        conn,
        canonical_memory_id,
        duplicate_memory_id,
        row_to_dict=row_to_dict,
        utc_now_iso=utc_now_iso,
        utc_offset_days_iso=utc_offset_days_iso,
        compute_sla_days=_compute_sla_days,
    )


def _normalize_feature_flag_key(flag_key: str) -> str:
    return normalize_feature_flag_key(flag_key, normalize_required_text=normalize_required_text)


def _normalize_rollout_mode(rollout_mode: str | None) -> str:
    return normalize_rollout_mode(rollout_mode, normalize_optional_text=normalize_optional_text)


def _normalize_csv_tokens(value: str | None, *, normalizer=None) -> list[str]:
    return normalize_csv_tokens(
        value,
        normalize_optional_text=normalize_optional_text,
        normalize_required_text=normalize_required_text,
        normalizer=normalizer,
    )


def _serialize_csv_tokens(tokens: list[str]) -> str | None:
    return serialize_csv_tokens(tokens)


def _feature_flag_to_dict(row) -> dict[str, Any]:
    return feature_flag_to_dict(
        row,
        row_to_dict=row_to_dict,
        cross_project_flag_key=CROSS_PROJECT_FLAG_KEY,
        normalize_rollout_mode_func=_normalize_rollout_mode,
        normalize_feature_flag_key_func=_normalize_feature_flag_key,
    )


def _get_feature_flag_config(conn, flag_key: str) -> dict[str, Any]:
    return get_feature_flag_config(
        conn,
        flag_key,
        normalize_feature_flag_key_func=_normalize_feature_flag_key,
        feature_flag_to_dict_func=_feature_flag_to_dict,
    )


def _evaluate_feature_flag_config(flag: dict[str, Any], *, project_key: str | None = None, scope_code: str | None = None) -> dict[str, Any]:
    return evaluate_feature_flag_config(
        flag,
        project_key=project_key,
        scope_code=scope_code,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
        normalize_rollout_mode_func=_normalize_rollout_mode,
        normalize_csv_tokens_func=_normalize_csv_tokens,
    )


def _require_feature_flag_write_access(conn, *, flag_key: str, project_key: str | None, scope_code: str | None, operation_name: str) -> dict[str, Any]:
    return require_feature_flag_write_access(
        conn,
        flag_key=flag_key,
        project_key=project_key,
        scope_code=scope_code,
        operation_name=operation_name,
        get_feature_flag_config_func=_get_feature_flag_config,
        evaluate_feature_flag_config_func=_evaluate_feature_flag_config,
    )


def _is_conflict_feature_active(conn, flag_key: str) -> bool:
    return is_simple_feature_active(
        conn,
        flag_key,
        get_feature_flag_config_func=_get_feature_flag_config,
        evaluate_feature_flag_config_func=_evaluate_feature_flag_config,
    )


def _is_multiuser_feature_active(conn, flag_key: str) -> bool:
    database_enabled = is_simple_feature_active(
        conn,
        flag_key,
        get_feature_flag_config_func=_get_feature_flag_config,
        evaluate_feature_flag_config_func=_evaluate_feature_flag_config,
    )
    return effective_multiuser_flag_enabled(flag_key, database_enabled)


def _is_memory_v2_feature_active(conn) -> bool:
    return is_simple_feature_active(
        conn,
        MEMORY_V2_FLAG_KEY,
        get_feature_flag_config_func=_get_feature_flag_config,
        evaluate_feature_flag_config_func=_evaluate_feature_flag_config,
    )


def _capture_reconciliation_flag_evaluation(
    conn,
    *,
    project_key: str | None,
    scope_code: str | None,
) -> dict[str, Any]:
    flag = _get_feature_flag_config(conn, MEMORY_V3_CAPTURE_RECONCILIATION_FLAG_KEY)
    return _evaluate_feature_flag_config(
        flag,
        project_key=project_key,
        scope_code=scope_code,
    )


def _retention_flag_evaluation(
    conn,
    *,
    project_key: str | None,
    scope_code: str | None,
) -> dict[str, Any]:
    flag = _get_feature_flag_config(conn, MEMORY_V3_RETENTION_FLAG_KEY)
    return _evaluate_feature_flag_config(flag, project_key=project_key, scope_code=scope_code)


def _sandman_provider_v3_feature_status(
    conn,
    *,
    project_key: str | None,
    scope_code: str | None,
) -> dict[str, Any]:
    flag = _get_feature_flag_config(conn, SANDMAN_PROVIDER_V3_FLAG_KEY)
    evaluation = _evaluate_feature_flag_config(flag, project_key=project_key, scope_code=scope_code)
    return {"feature_flag": flag, "evaluation": evaluation}


def _sandman_gemini_shadow_feature_status(
    conn,
    *,
    project_key: str | None,
    scope_code: str | None,
) -> dict[str, Any]:
    flag = _get_feature_flag_config(conn, sandman_gemini_shadow.SHADOW_FLAG_KEY)
    evaluation = _evaluate_feature_flag_config(flag, project_key=project_key, scope_code=scope_code)
    return {"feature_flag": flag, "evaluation": evaluation}


def _sandman_model_queue_routing_feature_status(
    conn,
    *,
    project_key: str | None,
    scope_code: str | None,
) -> dict[str, Any]:
    flag = _get_feature_flag_config(
        conn, sandman_v3_routing.MODEL_QUEUE_ROUTING_FLAG_KEY
    )
    evaluation = _evaluate_feature_flag_config(
        flag, project_key=project_key, scope_code=scope_code
    )
    return {"feature_flag": flag, "evaluation": evaluation}


def _capture_retention_gate(
    conn,
    *,
    content: str | None,
    project_key: str | None,
    scope_code: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    evaluation = _retention_flag_evaluation(
        conn,
        project_key=normalize_optional_text(project_key),
        scope_code=normalize_scope_code(scope_code),
    )
    if not evaluation["enabled"]:
        return None
    gate = capture_sensitivity_gate(content, metadata=metadata)
    if gate["status"] == "allowed":
        return None
    return {
        **gate,
        "flag_key": MEMORY_V3_RETENTION_FLAG_KEY,
        "flag_reason": evaluation["reason"],
    }


def _queued_capture_retention_gate(conn, *, item_id: int) -> dict[str, Any] | None:
    item = get_capture_review_item(conn, item_id=int(item_id), row_to_dict=row_to_dict)
    proposal = dict(item.get("proposal") or {})
    return _capture_retention_gate(
        conn,
        content=proposal.get("content"),
        project_key=item.get("project_key") or proposal.get("project_key"),
        scope_code=item.get("scope_code") or proposal.get("scope_code"),
        metadata={"tags": proposal.get("tags"), "visibility_scope": proposal.get("visibility_scope")},
    )


def _normalize_capture_content_for_fingerprint(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    normalized = re.sub(r" {2,}", " ", normalized)
    return normalized.strip()


def _capture_input_fingerprint(
    *,
    content: str,
    project_key: str | None,
    scope_code: str | None,
    conversation_key: str | None,
    source_context: str | None,
    source_event_ref: str | None,
    hint: str | None,
) -> str:
    return _canonical_json_hash(
        {
            "schema_version": "memory_v3_capture_input.v1",
            "content": _normalize_capture_content_for_fingerprint(normalize_required_text(content, "content")),
            "project_key": normalize_optional_text(project_key),
            "scope_code": normalize_scope_code(scope_code),
            "conversation_key": normalize_optional_text(conversation_key),
            "source_context": normalize_optional_text(source_context),
            "source_event_ref": normalize_optional_text(source_event_ref),
            "hint": normalize_optional_text(hint),
        }
    )


def _capture_proposal_key(*, input_fingerprint: str) -> str:
    return f"capture:{normalize_required_text(input_fingerprint, 'input_fingerprint')}"


MULTIUSER_IDENTITY_FLAG = "multiuser_identity_enabled"
MULTIUSER_SCOPE_RETRIEVAL_FLAG = "multiuser_scope_retrieval_enabled"
MULTIUSER_TIMELINE_ACTOR_FLAG = "multiuser_timeline_actor_enabled"
MULTIUSER_SCOPE_MAINTENANCE_FLAG = "multiuser_scope_maintenance_enabled"
MULTIUSER_SCOPE_PROMOTION_FLAG = "multiuser_scope_promotion_enabled"


def _owner_key_governance_warnings(owner_key: str) -> list[dict[str, Any]]:
    return owner_key_governance_warnings(owner_key)


def _owner_metadata_governance_warnings(owner_key: str, owner_type: str, routing_metadata_json: str | None) -> list[dict[str, Any]]:
    return owner_metadata_governance_warnings(
        owner_key,
        owner_type,
        routing_metadata_json,
        normalize_optional_text=normalize_optional_text,
    )


def _owner_directory_governance_warnings(owner_key: str, owner_type: str, routing_metadata_json: str | None, *, is_active: bool) -> list[dict[str, Any]]:
    return owner_directory_governance_warnings(
        owner_key,
        owner_type,
        routing_metadata_json,
        is_active=is_active,
        normalize_optional_text=normalize_optional_text,
    )



def _owner_deactivation_guardrail_warnings(conn, owner_key: str, *, requested_is_active: bool) -> list[dict[str, Any]]:
    return owner_deactivation_guardrail_warnings(
        conn,
        owner_key,
        requested_is_active=requested_is_active,
        normalize_optional_text=normalize_optional_text,
        owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
    )



def _owner_mapping_governance_warnings(
    conn,
    *,
    owner_role: str,
    owner_key: str,
    project_key: str | None,
    scope_code: str | None,
    is_active: bool,
    current_mapping_id: int | None = None,
) -> list[dict[str, Any]]:
    return owner_mapping_governance_warnings(
        conn,
        owner_role=owner_role,
        owner_key=owner_key,
        project_key=project_key,
        scope_code=scope_code,
        is_active=is_active,
        current_mapping_id=current_mapping_id,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
        owner_directory_item_to_dict=_owner_directory_item_to_dict,
        owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
    )



def _resolve_workspace_id(conn, workspace_key: str) -> int:
    """Resolves workspace_key Ă˘â€ â€™ workspace.id. Raises ValueError if not found."""
    row = conn.execute(
        "SELECT id FROM workspaces WHERE workspace_key = ?",
        (workspace_key.strip(),),
    ).fetchone()
    if row is None:
        raise ValueError(f"Workspace '{workspace_key}' nie istnieje")
    return int(row["id"])


# Ordered scope hierarchy for promotion validation (most restricted Ă˘â€ â€™ least restricted)
_SCOPE_ORDER = ["private", "project", "workspace"]


@mcp.tool
def list_owner_directory_items(owner_type: str | None = None, active_only: bool = False) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_owner_directory_items_payload(
            conn,
            owner_type=owner_type,
            active_only=active_only,
            normalize_optional_text=normalize_optional_text,
            owner_directory_item_to_dict=_owner_directory_item_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def upsert_owner_directory_item(
    owner_key: str,
    owner_type: str,
    display_name: str,
    is_active: bool = True,
    routing_metadata_json: str | None = None,
    allow_unsafe_deactivation: bool = False,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return upsert_owner_directory_item_payload(
            conn,
            owner_key=owner_key,
            owner_type=owner_type,
            display_name=display_name,
            is_active=is_active,
            routing_metadata_json=routing_metadata_json,
            allow_unsafe_deactivation=allow_unsafe_deactivation,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            owner_deactivation_guardrail_warnings=_owner_deactivation_guardrail_warnings,
            owner_directory_item_to_dict=_owner_directory_item_to_dict,
            owner_directory_governance_warnings=_owner_directory_governance_warnings,
            record_project_event=timeline.record_project_event,
        )
    finally:
        conn.close()


@mcp.tool
def list_owner_role_mappings(
    owner_role: str | None = None,
    project_key: str | None = None,
    scope_code: str | None = None,
    active_only: bool = False,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_owner_role_mappings_payload(
            conn,
            owner_role=owner_role,
            project_key=project_key,
            scope_code=scope_code,
            active_only=active_only,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def upsert_owner_role_mapping(
    owner_role: str,
    owner_key: str,
    project_key: str | None = None,
    scope_code: str | None = None,
    is_active: bool = True,
    notes: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return upsert_owner_role_mapping_payload(
            conn,
            owner_role=owner_role,
            owner_key=owner_key,
            project_key=project_key,
            scope_code=scope_code,
            is_active=is_active,
            notes=notes,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            utc_now_iso=utc_now_iso,
            owner_catalog_audit_project_key=_owner_catalog_audit_project_key,
            owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
            owner_mapping_governance_warnings=_owner_mapping_governance_warnings,
            record_project_event=timeline.record_project_event,
        )
    finally:
        conn.close()


@mcp.tool
def list_feature_flags() -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_feature_flags_payload(conn, feature_flag_to_dict=_feature_flag_to_dict)
    finally:
        conn.close()


@mcp.tool
def get_feature_flag(flag_key: str) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_feature_flag_payload(conn, flag_key=flag_key, get_feature_flag_config=_get_feature_flag_config)
    finally:
        conn.close()


@mcp.tool
def upsert_feature_flag(
    flag_key: str,
    is_enabled: bool = True,
    rollout_mode: str = "all",
    allowed_project_keys: str | None = None,
    allowed_scope_codes: str | None = None,
    read_only_mode: bool = False,
    notes: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return upsert_feature_flag_payload(
            conn,
            flag_key=flag_key,
            is_enabled=is_enabled,
            rollout_mode=rollout_mode,
            allowed_project_keys=allowed_project_keys,
            allowed_scope_codes=allowed_scope_codes,
            read_only_mode=read_only_mode,
            notes=notes,
            normalize_feature_flag_key=_normalize_feature_flag_key,
            normalize_rollout_mode=_normalize_rollout_mode,
            normalize_csv_tokens=_normalize_csv_tokens,
            serialize_csv_tokens=_serialize_csv_tokens,
            normalize_scope_code=normalize_scope_code,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            get_feature_flag_config=_get_feature_flag_config,
        )
    finally:
        conn.close()


@mcp.tool
def evaluate_feature_flag(flag_key: str, project_key: str | None = None, scope_code: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return evaluate_feature_flag_payload(
            conn,
            flag_key=flag_key,
            project_key=project_key,
            scope_code=scope_code,
            get_feature_flag_config=_get_feature_flag_config,
            evaluate_feature_flag_config=_evaluate_feature_flag_config,
        )
    finally:
        conn.close()


@mcp.tool
def set_feature_flag(
    key: str,
    enabled: bool,
    rollout_mode: str = "all",
    rollout_scope: str | None = None,
    rollout_project_key: str | None = None,
    description: str | None = None,
    read_only_mode: bool = False,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return set_feature_flag_payload(
            conn,
            key=key,
            enabled=enabled,
            rollout_mode=rollout_mode,
            rollout_scope=rollout_scope,
            rollout_project_key=rollout_project_key,
            description=description,
            read_only_mode=read_only_mode,
            normalize_required_text=normalize_required_text,
            normalize_feature_flag_key=_normalize_feature_flag_key,
            normalize_rollout_mode=_normalize_rollout_mode,
            normalize_csv_tokens=_normalize_csv_tokens,
            serialize_csv_tokens=_serialize_csv_tokens,
            normalize_scope_code=normalize_scope_code,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            get_feature_flag_config=_get_feature_flag_config,
        )
    finally:
        conn.close()


@mcp.tool
def get_root() -> dict[str, Any]:
    return get_root_payload(root=runtime_root(), sync_config=_sync_config)


@mcp.tool
def list_dir(path: str = ".") -> dict[str, Any]:
    return list_dir_payload(root=runtime_root(), safe_path=safe_path, rel_path=rel_path, guess_mime=guess_mime, path=path)


@mcp.tool
def read_file_text(path: str, encoding: str = "utf-8", errors: str = "strict") -> dict[str, Any]:
    return read_file_text_payload(safe_path=safe_path, rel_path=rel_path, guess_mime=guess_mime, path=path, encoding=encoding, errors=errors)


@mcp.tool
def read_file_base64(path: str) -> dict[str, Any]:
    return read_file_base64_payload(safe_path=safe_path, rel_path=rel_path, guess_mime=guess_mime, path=path)


@mcp.tool
def write_file_text(path: str, content: str, encoding: str = "utf-8", create_parents: bool = True) -> dict[str, Any]:
    return write_file_text_payload(safe_path=safe_path, rel_path=rel_path, path=path, content=content, encoding=encoding, create_parents=create_parents)


@mcp.tool
def write_file_base64(path: str, base64_content: str, create_parents: bool = True) -> dict[str, Any]:
    return write_file_base64_payload(safe_path=safe_path, rel_path=rel_path, path=path, base64_content=base64_content, create_parents=create_parents)


@mcp.tool
def append_file_text(path: str, content: str, encoding: str = "utf-8", create_parents: bool = True) -> dict[str, Any]:
    return append_file_text_payload(safe_path=safe_path, rel_path=rel_path, path=path, content=content, encoding=encoding, create_parents=create_parents)


@mcp.tool
def make_dir(path: str, parents: bool = True, exist_ok: bool = True) -> dict[str, Any]:
    return make_dir_payload(safe_path=safe_path, rel_path=rel_path, path=path, parents=parents, exist_ok=exist_ok)


@mcp.tool
def move_path(src: str, dst: str, create_parents: bool = True) -> dict[str, Any]:
    return move_path_payload(safe_path=safe_path, rel_path=rel_path, src=src, dst=dst, create_parents=create_parents)


@mcp.tool
def delete_path(path: str, recursive: bool = True) -> dict[str, Any]:
    return delete_path_payload(root=runtime_root(), safe_path=safe_path, rel_path=rel_path, path=path, recursive=recursive)


@mcp.tool
def stat_path(path: str = ".") -> dict[str, Any]:
    return stat_path_payload(safe_path=safe_path, rel_path=rel_path, guess_mime=guess_mime, path=path)


@mcp.tool
def search_text(query: str, path: str = ".", case_sensitive: bool = False, max_results: int = 100) -> dict[str, Any]:
    return search_text_payload(safe_path=safe_path, rel_path=rel_path, query=query, path=path, case_sensitive=case_sensitive, max_results=max_results)


@mcp.tool
def get_db_info() -> dict[str, Any]:
    return get_db_info_payload(db_path=runtime_db_path(), get_db_connection=get_db_connection)


@mcp.tool
def query_sql(query: str, params_json: str = "[]", allow_write: bool = False, max_rows: int = 100) -> dict[str, Any]:
    return query_sql_payload(
        get_db_connection=get_db_connection,
        parse_params_json=parse_params_json,
        is_read_only_sql=is_read_only_sql,
        row_to_dict=row_to_dict,
        query=query,
        params_json=params_json,
        allow_write=allow_write,
        max_rows=max_rows,
    )


def _memory_order_clause(sort_by: str) -> str:
    return memory_order_clause(sort_by)


def _memory_query_parts(
    *,
    limit: int,
    min_importance: float,
    sort_by: str,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    state_code: str | None = None,
    truth_kind: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    project_key_values: list[str] | tuple[str, ...] | None = None,
    project_key_mode: str = "exact",
    conversation_key: str | None = None,
    parent_memory_id: int | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
    # --- Multi-user filters (Stage 1) ---
    visibility_scope: str | None = None,
    workspace_id: int | None = None,
    actor: ActorContext | None = None,
    text_match_mode: str = "phrase",
    exclude_tags: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    return memory_query_parts(
        limit=limit,
        min_importance=min_importance,
        sort_by=sort_by,
        memory_type=memory_type,
        tag=tag,
        text_query=text_query,
        layer_code=layer_code,
        area_code=area_code,
        state_code=state_code,
        truth_kind=truth_kind,
        scope_code=scope_code,
        project_key=project_key,
        project_key_values=project_key_values,
        project_key_mode=project_key_mode,
        conversation_key=conversation_key,
        parent_memory_id=parent_memory_id,
        effective_owner_key=effective_owner_key,
        effective_owner_type=effective_owner_type,
        visibility_scope=visibility_scope,
        workspace_id=workspace_id,
        actor=actor,
        text_match_mode=text_match_mode,
        exclude_tags=exclude_tags,
        normalize_optional_text=normalize_optional_text,
        normalize_layer_code=normalize_layer_code,
        normalize_area_code=normalize_area_code,
        normalize_state_code=normalize_state_code,
        normalize_truth_kind=normalize_truth_kind,
        normalize_scope_code=normalize_scope_code,
        build_memory_visibility_filter=build_memory_visibility_filter,
    )


def _resolve_project_key_filter(
    conn,
    *,
    project_key: str | None,
    project_key_mode: str = "exact",
) -> tuple[list[str] | None, str, str | None]:
    return project_key_filter_values(
        conn,
        project_key,
        project_key_mode=project_key_mode,
        normalize_optional_text=normalize_optional_text,
    )


def _retrieval_exclude_tags_for(canonical_project_key: str | None) -> tuple[str, ...]:
    return project_anchor_exclude_tags_for(canonical_project_key)


def _retrieval_candidate_limit(limit: int) -> int:
    return max(limit, min(max(limit * 5, 50), 200))


def _normalize_match_text(value: Any) -> str:
    normalized = normalize_optional_text(value) or ""
    return normalized.lower()


def _slugify_match_text(value: Any) -> str:
    normalized = _normalize_match_text(value)
    return re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE).strip("-")


def _tag_tokens(value: str | None) -> set[str]:
    normalized = normalize_optional_text(value) or ""
    return {
        token.strip().lower()
        for token in normalized.split(",")
        if token and token.strip()
    }


def _query_memory_id_signal(text_query: str) -> int | None:
    normalized = _normalize_match_text(text_query)
    direct_match = re.fullmatch(r"#?(\d+)", normalized)
    if direct_match is not None:
        return int(direct_match.group(1))
    embedded_match = re.search(r"\b(?:memory|mem|id)\s*#?(\d+)\b", normalized)
    if embedded_match is not None:
        return int(embedded_match.group(1))
    return None


def _score_retrieval_item(
    item: dict[str, Any],
    *,
    text_query: str | None,
    requested_project_key: str | None,
    canonical_project_key: str | None,
    project_key_values: list[str] | tuple[str, ...] | None,
    sort_by: str,
    fallback_used: bool,
) -> dict[str, Any]:
    normalized_query = _normalize_match_text(text_query)
    normalized_summary = _normalize_match_text(item.get("summary_short"))
    normalized_content = _normalize_match_text(item.get("content"))
    normalized_tags_raw = _normalize_match_text(item.get("tags"))
    normalized_project_key = _normalize_match_text(item.get("project_key"))
    normalized_requested_project_key = _normalize_match_text(requested_project_key)
    normalized_canonical_project_key = _normalize_match_text(canonical_project_key)
    normalized_project_key_values = [_normalize_match_text(value) for value in (project_key_values or [])]
    normalized_truth_kind = _normalize_match_text(item.get("truth_kind"))
    normalized_memory_v2_status = _normalize_match_text(item.get("memory_v2_status") or item.get("status"))
    slug_query = _slugify_match_text(text_query)
    tag_values = _tag_tokens(item.get("tags"))
    query_terms = [term.lower() for term in text_search_terms(text_query)]
    memory_id_signal = _query_memory_id_signal(text_query or "")

    matched_by: list[str] = []
    score = 0.0
    token_hits = 0

    if normalized_requested_project_key and normalized_project_key == normalized_requested_project_key:
        matched_by.append("project_key")
        score += 140.0
    elif normalized_canonical_project_key and normalized_project_key == normalized_canonical_project_key:
        matched_by.append("canonical_project_key")
        score += 135.0
    elif normalized_project_key in normalized_project_key_values:
        matched_by.append("alias")
        score += 130.0

    if memory_id_signal is not None and int(item.get("id") or 0) == memory_id_signal:
        matched_by.append("memory_id")
        score += 400.0

    if normalized_query:
        if normalized_query in normalized_summary or normalized_query in normalized_content:
            matched_by.append("text")
            score += 150.0
        if normalized_query in normalized_tags_raw:
            matched_by.append("tag")
            score += 170.0

    if slug_query:
        if slug_query in tag_values:
            matched_by.append("exact_slug")
            score += 180.0
        elif slug_query == _slugify_match_text(item.get("summary_short")):
            matched_by.append("exact_slug")
            score += 170.0

    query_project_terms = {
        _normalize_match_text(value)
        for value in [requested_project_key, canonical_project_key] + list(project_key_values or [])
        if _normalize_match_text(value)
    }
    if normalized_query and normalized_query in query_project_terms:
        matched_by.append("query_project_key")
        score += 120.0
    elif slug_query and slug_query in {_slugify_match_text(value) for value in query_project_terms}:
        matched_by.append("query_project_key")
        score += 115.0

    for term in query_terms:
        if term in tag_values:
            token_hits += 1
            score += 42.0
            continue
        if term in normalized_summary:
            token_hits += 1
            score += 28.0
        elif term in normalized_content:
            token_hits += 1
            score += 14.0

    if token_hits > 0:
        matched_by.append("token")

    importance_score = float(item.get("importance_score") or 0.0)
    score += importance_score * 20.0

    query_mentions_dreams = any(term in {"dream", "dreams", "sandman", "mara", "sen", "sny", "inspiracja"} for term in query_terms)
    if normalized_truth_kind in {"fact", "decision", "preference"}:
        score += 18.0
    elif normalized_truth_kind in {"dream", "interpretation", "proposal"} and not query_mentions_dreams:
        score -= 35.0

    if normalized_memory_v2_status == "active":
        score += 10.0
    elif normalized_memory_v2_status == "proposed":
        score -= 6.0
    elif normalized_memory_v2_status in {"archived", "superseded"}:
        score -= 14.0
    elif normalized_memory_v2_status == "contradicted":
        score -= 20.0

    if fallback_used and any(reason in matched_by for reason in ("token", "exact_slug", "tag", "memory_id", "query_project_key")):
        matched_by.append("fallback")

    if sort_by in {"created_at_desc", "recent"}:
        score += float(item.get("id") or 0) * 0.001

    signal_count = len({
        reason
        for reason in matched_by
        if reason not in {"project_key", "canonical_project_key", "alias", "fallback"}
    })
    return {
        "score": round(score, 3),
        "matched_by": matched_by,
        "signal_count": signal_count,
    }


def _retrieval_sort_key(
    item: dict[str, Any],
    *,
    score: float,
    sort_by: str,
) -> tuple[Any, ...]:
    importance_score = float(item.get("importance_score") or 0.0)
    recall_count = int(item.get("recall_count") or 0)
    memory_id = int(item.get("id") or 0)

    if sort_by == "created_at_asc":
        return (-score, memory_id)
    if sort_by in {"created_at_desc", "recent"}:
        return (-score, -memory_id)
    if sort_by == "recalled":
        return (-score, -recall_count, -importance_score, -memory_id)
    if sort_by == "validated":
        return (-score, -importance_score, -memory_id)
    return (-score, -importance_score, -memory_id)


def _attach_retrieval_debug(
    items: list[dict[str, Any]],
    *,
    text_query: str | None,
    requested_project_key: str | None,
    canonical_project_key: str | None,
    project_key_values: list[str] | tuple[str, ...] | None,
    sort_by: str,
    fallback_used: bool,
    include_debug: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scored: list[tuple[tuple[Any, ...], dict[str, Any], dict[str, Any]]] = []
    debug_rows: list[dict[str, Any]] = []
    for item in items:
        match_debug = _score_retrieval_item(
            item,
            text_query=text_query,
            requested_project_key=requested_project_key,
            canonical_project_key=canonical_project_key,
            project_key_values=project_key_values,
            sort_by=sort_by,
            fallback_used=fallback_used,
        )
        sort_key = _retrieval_sort_key(item, score=match_debug["score"], sort_by=sort_by)
        debug_rows.append({
            "memory_id": int(item.get("id") or 0),
            **match_debug,
        })
        item_out = dict(item)
        if include_debug:
            item_out["match_debug"] = {
                "score": match_debug["score"],
                "matched_by": match_debug["matched_by"],
            }
        scored.append((sort_key, item_out, match_debug))
    scored.sort(key=lambda entry: entry[0])
    return [entry[1] for entry in scored], debug_rows


def _fallback_candidate_items(
    conn,
    *,
    text_query: str,
    limit: int,
    memory_type: str | None,
    tag: str | None,
    min_importance: float,
    sort_by: str,
    layer_code: str | None,
    area_code: str | None,
    state_code: str | None,
    scope_code: str | None,
    project_key: str | None,
    project_key_values: list[str] | tuple[str, ...] | None,
    project_key_mode: str,
    conversation_key: str | None,
    parent_memory_id: int | None,
    actor: ActorContext | None,
    exclude_tags: list[str] | tuple[str, ...] | None,
    existing_ids: set[int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sql, params, filters = _memory_query_parts(
        limit=_retrieval_candidate_limit(limit),
        memory_type=memory_type,
        tag=tag,
        min_importance=min_importance,
        sort_by=sort_by,
        text_query=None,
        layer_code=layer_code,
        area_code=area_code,
        state_code=state_code,
        scope_code=scope_code,
        project_key=project_key,
        project_key_values=project_key_values,
        project_key_mode=project_key_mode,
        conversation_key=conversation_key,
        parent_memory_id=parent_memory_id,
        actor=actor,
        exclude_tags=exclude_tags,
    )
    rows = conn.execute(sql, params).fetchall()
    fallback_items = []
    for row in rows:
        item = _apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        if int(item.get("id") or 0) in existing_ids:
            continue
        fallback_items.append(item)
    filters["text_match_mode"] = "fallback"
    filters["fallback_query"] = text_query
    return fallback_items, filters


@mcp.tool
def list_memories(
    limit: int = 20,
    memory_type: str | None = None,
    tag: str | None = None,
    min_importance: float = 0.0,
    sort_by: str = "active",
    text_query: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    state_code: str | None = None,
    truth_kind: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    project_key_mode: str = "exact",
    conversation_key: str | None = None,
    parent_memory_id: int | None = None,
    include_links: bool = False,
    include_history: bool = False,
    # --- Task 2.2: opcjonalny aktor do scope-aware retrieval ---
    user_key: str | None = None,
    workspace_key: str | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    """Lista wspomnieÄąâ€ž z opcjonalnym filtrem scope-aware (user_key).

    Bez user_key: tryb globalny (legacy, dostĂ„â„˘p do wszystkich wspomnieÄąâ€ž).
    Z user_key: tryb scope-aware Ă˘â‚¬â€ť zwraca tylko wspomnienia widoczne dla tego uÄąÄ˝ytkownika.
    Tryb scope-aware wymaga aktywnej flagi multiuser_scope_retrieval_enabled.
    project_key_mode='exact' trzyma ścisły namespace; 'aliases' używa jawnego rejestru aliasów.
    """
    conn = get_db_connection()
    try:
        actor: ActorContext | None = None
        scope_active = False
        project_key_values, resolved_project_key_mode, canonical_project_key = _resolve_project_key_filter(
            conn,
            project_key=project_key,
            project_key_mode=project_key_mode,
        )
        exclude_tags = list(_retrieval_exclude_tags_for(canonical_project_key))
        if user_key and _is_multiuser_feature_active(conn, MULTIUSER_SCOPE_RETRIEVAL_FLAG):
            actor = resolve_actor_context(
                conn,
                user_key=user_key,
                workspace_key=workspace_key,
                project_key=project_key,
            )
            scope_active = True

        sql, params, filters = _memory_query_parts(
            limit=limit,
            memory_type=memory_type,
            tag=tag,
            min_importance=min_importance,
            sort_by=sort_by,
            text_query=text_query,
            layer_code=layer_code,
            area_code=area_code,
            state_code=state_code,
            truth_kind=truth_kind,
            scope_code=scope_code,
            project_key=project_key,
            project_key_values=project_key_values,
            project_key_mode=resolved_project_key_mode,
            conversation_key=conversation_key,
            parent_memory_id=parent_memory_id,
            actor=actor,
            exclude_tags=exclude_tags,
        )
        filters["canonical_project_key"] = canonical_project_key
        rows = conn.execute(sql, params).fetchall()
        items = [_apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(row)))) for row in rows]
        items, ranking_debug = _attach_retrieval_debug(
            items,
            text_query=text_query,
            requested_project_key=project_key,
            canonical_project_key=canonical_project_key,
            project_key_values=project_key_values,
            sort_by=sort_by,
            fallback_used=False,
            include_debug=debug,
        )
        current_state_projection = resolve_current_memory_state(conn, items, include_history=include_history)
        items = current_state_projection["items"]
        filters["include_history"] = bool(include_history)
        items = _attach_links_to_memory_items(conn, items, include_links=include_links)
    finally:
        conn.close()
    result: dict[str, Any] = {"count": len(items), "items": items, "filters": filters, "include_links": include_links, "include_history": bool(include_history)}
    if user_key:
        result["scope_retrieval_active"] = scope_active
        result["actor_user_key"] = user_key
    if debug:
        result["debug"] = {
            "resolved_project_key": canonical_project_key,
            "applied_aliases": project_key_values or ([project_key] if project_key else []),
            "sort_mode": sort_by,
            "exclude_tags": exclude_tags,
            "retrieval_strategy": ["list"],
            "ranking": ranking_debug,
            "current_state": {
                "schema": current_state_projection["schema"],
                "counts": current_state_projection["counts"],
                "issues": current_state_projection["issues"],
                "resolved_question_ids": current_state_projection["resolved_question_ids"],
            },
        }
    return result


@mcp.tool
def find_memories(
    text_query: str,
    limit: int = 20,
    memory_type: str | None = None,
    tag: str | None = None,
    min_importance: float = 0.0,
    sort_by: str = "active",
    layer_code: str | None = None,
    area_code: str | None = None,
    state_code: str | None = None,
    truth_kind: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    project_key_mode: str = "exact",
    conversation_key: str | None = None,
    parent_memory_id: int | None = None,
    include_links: bool = False,
    include_history: bool = False,
    # --- Task 2.2: opcjonalny aktor do scope-aware retrieval ---
    user_key: str | None = None,
    workspace_key: str | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    """Wyszukuje wspomnienia po tekÄąâ€şcie z opcjonalnym filtrem scope-aware (user_key).

    Bez user_key: tryb globalny (legacy, przeszukuje wszystkie wspomnienia).
    Z user_key: tryb scope-aware Ă˘â‚¬â€ť zwraca tylko wspomnienia widoczne dla tego uÄąÄ˝ytkownika.
    Tryb scope-aware wymaga aktywnej flagi multiuser_scope_retrieval_enabled.
    project_key_mode='exact' trzyma ścisły namespace; 'aliases' używa jawnego rejestru aliasów.
    """
    normalized_text_query = normalize_optional_text(text_query)
    if not normalized_text_query:
        return {"status": "error", "error": 'text_query nie moÄąÄ˝e byĂ„â€ˇ puste'}
    conn = get_db_connection()
    try:
        actor: ActorContext | None = None
        scope_active = False
        project_key_values, resolved_project_key_mode, canonical_project_key = _resolve_project_key_filter(
            conn,
            project_key=project_key,
            project_key_mode=project_key_mode,
        )
        exclude_tags = list(_retrieval_exclude_tags_for(canonical_project_key))
        retrieval_strategy: list[str] = ["phrase"]
        if user_key and _is_multiuser_feature_active(conn, MULTIUSER_SCOPE_RETRIEVAL_FLAG):
            actor = resolve_actor_context(
                conn,
                user_key=user_key,
                workspace_key=workspace_key,
                project_key=project_key,
            )
            scope_active = True

        sql, params, filters = _memory_query_parts(
            limit=limit,
            memory_type=memory_type,
            tag=tag,
            min_importance=min_importance,
            sort_by=sort_by,
            text_query=normalized_text_query,
            layer_code=layer_code,
            area_code=area_code,
            state_code=state_code,
            truth_kind=truth_kind,
            scope_code=scope_code,
            project_key=project_key,
            project_key_values=project_key_values,
            project_key_mode=resolved_project_key_mode,
            conversation_key=conversation_key,
            parent_memory_id=parent_memory_id,
            actor=actor,
            exclude_tags=exclude_tags,
        )
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            retrieval_strategy.append("relaxed")
            sql, params, filters = _memory_query_parts(
                limit=limit,
                memory_type=memory_type,
                tag=tag,
                min_importance=min_importance,
                sort_by=sort_by,
                text_query=normalized_text_query,
                layer_code=layer_code,
                area_code=area_code,
                state_code=state_code,
                truth_kind=truth_kind,
                scope_code=scope_code,
                project_key=project_key,
                project_key_values=project_key_values,
                project_key_mode=resolved_project_key_mode,
                conversation_key=conversation_key,
                parent_memory_id=parent_memory_id,
                actor=actor,
                text_match_mode="relaxed",
                exclude_tags=exclude_tags,
            )
            rows = conn.execute(sql, params).fetchall()
        filters["canonical_project_key"] = canonical_project_key
        primary_items = [_apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(row)))) for row in rows]
        had_primary_matches = bool(primary_items)
        fallback_used = False
        if not primary_items:
            existing_ids = {int(item.get("id") or 0) for item in primary_items}
            fallback_items, fallback_filters = _fallback_candidate_items(
                conn,
                text_query=normalized_text_query,
                limit=limit,
                memory_type=memory_type,
                tag=tag,
                min_importance=min_importance,
                sort_by=sort_by,
                layer_code=layer_code,
                area_code=area_code,
                state_code=state_code,
                scope_code=scope_code,
                project_key=project_key,
                project_key_values=project_key_values,
                project_key_mode=resolved_project_key_mode,
                conversation_key=conversation_key,
                parent_memory_id=parent_memory_id,
                actor=actor,
                exclude_tags=exclude_tags,
                existing_ids=existing_ids,
            )
            if fallback_items:
                retrieval_strategy.append("fallback")
                fallback_used = True
                filters["fallback_filters"] = fallback_filters
                primary_items.extend(fallback_items)
        items, ranking_debug = _attach_retrieval_debug(
            primary_items,
            text_query=normalized_text_query,
            requested_project_key=project_key,
            canonical_project_key=canonical_project_key,
            project_key_values=project_key_values,
            sort_by=sort_by,
            fallback_used=fallback_used,
            include_debug=debug,
        )
        if fallback_used:
            scope_fallback_only = False
            allowed_ids = {
                int(entry["memory_id"])
                for entry in ranking_debug
                if int(entry["signal_count"]) > 0 or "text" in entry["matched_by"]
            }
            if not had_primary_matches and not allowed_ids and canonical_project_key:
                scope_fallback_only = True
                allowed_ids = {
                    int(entry["memory_id"])
                    for entry in ranking_debug
                    if any(reason in entry["matched_by"] for reason in ("project_key", "canonical_project_key", "alias"))
                }
            items = [item for item in items if int(item.get("id") or 0) in allowed_ids]
            ranking_debug = [entry for entry in ranking_debug if int(entry["memory_id"]) in allowed_ids]
            if scope_fallback_only:
                for item in items:
                    if debug and "fallback" not in item.get("match_debug", {}).get("matched_by", []):
                        item["match_debug"]["matched_by"].append("fallback")
                for entry in ranking_debug:
                    if "fallback" not in entry["matched_by"]:
                        entry["matched_by"].append("fallback")
        current_state_projection = resolve_current_memory_state(conn, items, include_history=include_history)
        items = current_state_projection["items"][:limit]
        returned_ids = {int(item.get("id") or 0) for item in items}
        ranking_debug = [entry for entry in ranking_debug if int(entry.get("memory_id") or 0) in returned_ids]
        items = _attach_links_to_memory_items(conn, items, include_links=include_links)
    finally:
        conn.close()
    result: dict[str, Any] = {
        "count": len(items),
        "items": items,
        "filters": filters,
        "query": normalized_text_query,
        "include_links": include_links,
        "include_history": bool(include_history),
    }
    if user_key:
        result["scope_retrieval_active"] = scope_active
        result["actor_user_key"] = user_key
    if debug:
        result["debug"] = {
            "resolved_project_key": canonical_project_key,
            "applied_aliases": project_key_values or ([project_key] if project_key else []),
            "sort_mode": sort_by,
            "exclude_tags": exclude_tags,
            "retrieval_strategy": retrieval_strategy,
            "ranking": ranking_debug[:limit],
            "current_state": {
                "schema": current_state_projection["schema"],
                "counts": current_state_projection["counts"],
                "issues": current_state_projection["issues"],
                "resolved_question_ids": current_state_projection["resolved_question_ids"],
            },
        }
    return result


def recent_memories(
    project_key: str | None = None,
    limit: int = 8,
    memory_type: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    state_code: str | None = None,
    truth_kind: str | None = None,
    scope_code: str | None = None,
    include_links: bool = False,
    include_history: bool = False,
    project_key_mode: str = "exact",
    debug: bool = False,
) -> dict[str, Any]:
    """Convenience wrapper for model-friendly chronological retrieval."""
    return list_memories(
        project_key=project_key,
        limit=limit,
        memory_type=memory_type,
        layer_code=layer_code,
        area_code=area_code,
        state_code=state_code,
        scope_code=scope_code,
        sort_by="created_at_desc",
        project_key_mode=project_key_mode,
        include_links=include_links,
        include_history=include_history,
        debug=debug,
    )


_RETRIEVAL_QA_CASES = (
    {
        "case_id": "demo-project-lifecycle",
        "project_key": "demo-project",
        "query": "lifecycle preview",
        "expected_resolved_project_key": "demo-project",
    },
    {
        "case_id": "demo-project-provenance",
        "project_key": "demo-project",
        "query": "durable memory provenance",
        "expected_resolved_project_key": "demo-project",
    },
    {
        "case_id": "sample-research-storage",
        "project_key": "sample-research",
        "query": "SQLite local storage",
        "expected_resolved_project_key": "sample-research",
    },
    {
        "case_id": "sample-research-decision",
        "project_key": "research-demo",
        "query": "prototype storage decision",
        "expected_resolved_project_key": "sample-research",
    },
)


def _retrieval_result_stub(item: dict[str, Any]) -> dict[str, Any]:
    payload = _section_memory_stub(item)
    payload["memory_id"] = payload.pop("id")
    if "match_debug" in item:
        payload["matched_by"] = list(item["match_debug"].get("matched_by") or [])
        payload["match_debug"] = dict(item["match_debug"])
    return payload


def _rejected_stub(item: dict[str, Any], *, reason: str) -> dict[str, Any]:
    payload = _retrieval_result_stub(item)
    payload["reason"] = reason
    return payload


def _ranking_rows_by_id(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {
        int(row.get("memory_id") or 0): row
        for row in rows
        if int(row.get("memory_id") or 0) > 0
    }


def _explain_strategy_items(
    conn,
    *,
    text_query: str,
    limit: int,
    memory_type: str | None,
    tag: str | None,
    min_importance: float,
    sort_by: str,
    layer_code: str | None,
    area_code: str | None,
    state_code: str | None,
    scope_code: str | None,
    project_key: str | None,
    project_key_values: list[str] | tuple[str, ...] | None,
    project_key_mode: str,
    conversation_key: str | None,
    parent_memory_id: int | None,
    actor: ActorContext | None,
    exclude_tags: list[str] | tuple[str, ...] | None,
    requested_project_key: str | None,
    canonical_project_key: str | None,
    text_match_mode: str,
    fallback_used: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    sql, params, filters = _memory_query_parts(
        limit=limit,
        memory_type=memory_type,
        tag=tag,
        min_importance=min_importance,
        sort_by=sort_by,
        text_query=text_query,
        layer_code=layer_code,
        area_code=area_code,
        state_code=state_code,
        scope_code=scope_code,
        project_key=project_key,
        project_key_values=project_key_values,
        project_key_mode=project_key_mode,
        conversation_key=conversation_key,
        parent_memory_id=parent_memory_id,
        actor=actor,
        text_match_mode=text_match_mode,
        exclude_tags=exclude_tags,
    )
    rows = conn.execute(sql, params).fetchall()
    items = [_apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(row)))) for row in rows]
    items, ranking = _attach_retrieval_debug(
        items,
        text_query=text_query,
        requested_project_key=requested_project_key,
        canonical_project_key=canonical_project_key,
        project_key_values=project_key_values,
        sort_by=sort_by,
        fallback_used=fallback_used,
        include_debug=True,
    )
    filters["canonical_project_key"] = canonical_project_key
    return items, ranking, filters


def _retrieval_recommendations(
    *,
    query: str,
    requested_project_key: str | None,
    resolved_project_key: str | None,
    applied_aliases: list[str],
    selected_strategy: str,
    results_count: int,
    recent_probe_count: int,
    top_result_has_text_signal: bool,
) -> list[str]:
    recommendations: list[str] = []
    if requested_project_key and resolved_project_key and requested_project_key != resolved_project_key:
        recommendations.append(f"Use canonical project key '{resolved_project_key}' when you need an explicit project family.")
    if results_count == 0 and recent_probe_count > 0:
        recommendations.append("Try get_project_brief or recent_project_changes for the same project before broadening the text query.")
    if selected_strategy == "fallback":
        recommendations.append("Try a narrower query with an exact tag, slug, or memory id to reduce fallback-only ranking.")
    if results_count == 0 and applied_aliases:
        recommendations.append(f"Retry with one of the known project aliases: {', '.join(applied_aliases[:4])}.")
    if results_count > 0 and not top_result_has_text_signal:
        recommendations.append("Top result matched mostly by project routing; add a stronger domain term to make ranking more reliable.")
    if not recommendations and query:
        recommendations.append("If the result still feels weak, rerun with debug=true and compare against recent_project_changes for the same project.")
    return recommendations


def explain_retrieval(
    query: str,
    project_key: str | None = None,
    project_key_mode: str = "aliases",
    limit: int = 10,
    include_candidates: bool = True,
    include_rejected: bool = True,
    include_recent_probe: bool = True,
) -> dict[str, Any]:
    normalized_query = normalize_optional_text(query)
    if not normalized_query:
        return {"status": "error", "error": "query cannot be empty"}

    safe_limit = max(1, min(int(limit or 10), 20))
    candidate_limit = _retrieval_candidate_limit(safe_limit)
    conn = get_db_connection()
    try:
        project_key_values, resolved_project_key_mode, canonical_project_key = _resolve_project_key_filter(
            conn,
            project_key=project_key,
            project_key_mode=project_key_mode,
        )
        exclude_tags = list(_retrieval_exclude_tags_for(canonical_project_key))
        requested_project_key = normalize_optional_text(project_key)
        applied_aliases = project_key_values or ([requested_project_key] if requested_project_key else [])

        phrase_items, phrase_ranking, phrase_filters = _explain_strategy_items(
            conn,
            text_query=normalized_query,
            limit=candidate_limit,
            memory_type=None,
            tag=None,
            min_importance=0.0,
            sort_by="active",
            layer_code=None,
            area_code=None,
            state_code=None,
            scope_code=None,
            project_key=requested_project_key,
            project_key_values=project_key_values,
            project_key_mode=resolved_project_key_mode,
            conversation_key=None,
            parent_memory_id=None,
            actor=None,
            exclude_tags=exclude_tags,
            requested_project_key=requested_project_key,
            canonical_project_key=canonical_project_key,
            text_match_mode="phrase",
            fallback_used=False,
        )

        relaxed_items: list[dict[str, Any]] = []
        relaxed_ranking: list[dict[str, Any]] = []
        relaxed_filters: dict[str, Any] | None = None
        if not phrase_items:
            relaxed_items, relaxed_ranking, relaxed_filters = _explain_strategy_items(
                conn,
                text_query=normalized_query,
                limit=candidate_limit,
                memory_type=None,
                tag=None,
                min_importance=0.0,
                sort_by="active",
                layer_code=None,
                area_code=None,
                state_code=None,
                scope_code=None,
                project_key=requested_project_key,
                project_key_values=project_key_values,
                project_key_mode=resolved_project_key_mode,
                conversation_key=None,
                parent_memory_id=None,
                actor=None,
                exclude_tags=exclude_tags,
                requested_project_key=requested_project_key,
                canonical_project_key=canonical_project_key,
                text_match_mode="relaxed",
                fallback_used=False,
            )

        fallback_items: list[dict[str, Any]] = []
        fallback_ranking: list[dict[str, Any]] = []
        fallback_filters: dict[str, Any] | None = None
        if not phrase_items and not relaxed_items:
            fallback_pool, fallback_filters = _fallback_candidate_items(
                conn,
                text_query=normalized_query,
                limit=safe_limit,
                memory_type=None,
                tag=None,
                min_importance=0.0,
                sort_by="active",
                layer_code=None,
                area_code=None,
                state_code=None,
                scope_code=None,
                project_key=requested_project_key,
                project_key_values=project_key_values,
                project_key_mode=resolved_project_key_mode,
                conversation_key=None,
                parent_memory_id=None,
                actor=None,
                exclude_tags=exclude_tags,
                existing_ids=set(),
            )
            fallback_items, fallback_ranking = _attach_retrieval_debug(
                fallback_pool,
                text_query=normalized_query,
                requested_project_key=requested_project_key,
                canonical_project_key=canonical_project_key,
                project_key_values=project_key_values,
                sort_by="active",
                fallback_used=True,
                include_debug=True,
            )

        selected_strategy = "none"
        selected_items: list[dict[str, Any]] = []
        selected_ranking: list[dict[str, Any]] = []
        rejected_or_filtered: list[dict[str, Any]] = []

        if phrase_items:
            selected_strategy = "phrase"
            selected_items = phrase_items
            selected_ranking = phrase_ranking
            if include_rejected:
                for item in phrase_items[safe_limit:]:
                    rejected_or_filtered.append(_rejected_stub(item, reason="trimmed_by_limit"))
        elif relaxed_items:
            selected_strategy = "relaxed"
            selected_items = relaxed_items
            selected_ranking = relaxed_ranking
            if include_rejected:
                for item in relaxed_items[safe_limit:]:
                    rejected_or_filtered.append(_rejected_stub(item, reason="trimmed_by_limit"))
        elif fallback_items:
            selected_strategy = "fallback"
            selected_items = list(fallback_items)
            selected_ranking = list(fallback_ranking)
            allowed_ids = {
                int(entry["memory_id"])
                for entry in fallback_ranking
                if int(entry["signal_count"]) > 0 or "text" in entry["matched_by"]
            }
            scope_fallback_only = False
            if not allowed_ids and canonical_project_key:
                scope_fallback_only = True
                allowed_ids = {
                    int(entry["memory_id"])
                    for entry in fallback_ranking
                    if any(reason in entry["matched_by"] for reason in ("project_key", "canonical_project_key", "alias"))
                }
            if include_rejected:
                for item in fallback_items:
                    memory_id = int(item.get("id") or 0)
                    if memory_id not in allowed_ids:
                        reason = "project_scope_only_fallback" if scope_fallback_only else "no_match_signal_after_fallback"
                        rejected_or_filtered.append(_rejected_stub(item, reason=reason))
            selected_items = [item for item in fallback_items if int(item.get("id") or 0) in allowed_ids]
            selected_ranking = [entry for entry in fallback_ranking if int(entry["memory_id"]) in allowed_ids]
            if scope_fallback_only:
                for item in selected_items:
                    match_debug = item.setdefault("match_debug", {"score": 0.0, "matched_by": []})
                    if "fallback" not in match_debug.get("matched_by", []):
                        match_debug["matched_by"].append("fallback")
                for entry in selected_ranking:
                    if "fallback" not in entry.get("matched_by", []):
                        entry["matched_by"].append("fallback")
            if include_rejected:
                for item in selected_items[safe_limit:]:
                    rejected_or_filtered.append(_rejected_stub(item, reason="trimmed_by_limit"))

        recent_probe_payload: dict[str, Any] | None = None
        if include_recent_probe:
            recent_probe_payload = recent_memories(
                project_key=requested_project_key,
                project_key_mode=resolved_project_key_mode,
                limit=candidate_limit,
                debug=True,
            )
            if not selected_items and recent_probe_payload.get("items"):
                selected_strategy = "recent_probe"

        final_items = selected_items[:safe_limit]
        final_ids = {int(item.get("id") or 0) for item in final_items}
        if include_rejected:
            rejected_or_filtered = _brief_unique_items(rejected_or_filtered)
            seen_rejected: set[int] = set()
            deduped_rejected: list[dict[str, Any]] = []
            for item in rejected_or_filtered:
                memory_id = int(item.get("memory_id") or 0)
                if memory_id <= 0:
                    deduped_rejected.append(item)
                    continue
                if memory_id in final_ids or memory_id in seen_rejected:
                    continue
                seen_rejected.add(memory_id)
                deduped_rejected.append(item)
            rejected_or_filtered = deduped_rejected

        candidate_counts = {
            "phrase": len(phrase_items),
            "relaxed": len(relaxed_items),
            "fallback_pool": len(fallback_items),
            "selected_before_limit": len(selected_items),
            "final_results": len(final_items),
            "recent_probe": len((recent_probe_payload or {}).get("items", [])),
        }

        strategies_attempted = ["phrase"]
        if not phrase_items:
            strategies_attempted.append("relaxed")
        if not phrase_items and not relaxed_items:
            strategies_attempted.append("fallback")
        if include_recent_probe:
            strategies_attempted.append("recent_probe")

        warnings: list[str] = []
        if requested_project_key and canonical_project_key and requested_project_key != canonical_project_key:
            warnings.append(f"Alias routing resolved '{requested_project_key}' to canonical '{canonical_project_key}'.")
        if exclude_tags:
            warnings.append(f"Exclude tags active for this project family: {', '.join(exclude_tags)}.")
        if selected_strategy == "fallback":
            warnings.append("Final results rely on fallback heuristics rather than phrase/relaxed text hits.")
        if not final_items and candidate_counts["recent_probe"] > 0:
            warnings.append("Text retrieval returned no final matches, but recent project memories exist.")
        if include_rejected and any(item.get("reason") == "no_match_signal_after_fallback" for item in rejected_or_filtered):
            warnings.append("Some fallback candidates were dropped because they only matched project scope, not query signal.")

        top_result_has_text_signal = any(
            reason in {"text", "tag", "exact_slug", "memory_id", "token"}
            for reason in (final_items[0].get("match_debug", {}).get("matched_by") or [])
        ) if final_items else False
        recommendations = _retrieval_recommendations(
            query=normalized_query,
            requested_project_key=requested_project_key,
            resolved_project_key=canonical_project_key or requested_project_key,
            applied_aliases=applied_aliases,
            selected_strategy=selected_strategy,
            results_count=len(final_items),
            recent_probe_count=candidate_counts["recent_probe"],
            top_result_has_text_signal=top_result_has_text_signal,
        )

        ranking_sources = {
            "phrase": phrase_ranking,
            "relaxed": relaxed_ranking,
            "fallback": fallback_ranking,
        }
        results = [_retrieval_result_stub(item) for item in final_items]
        debug: dict[str, Any] = {
            "filters": phrase_filters if selected_strategy == "phrase" else relaxed_filters or fallback_filters or phrase_filters,
            "resolved_project_key_mode": resolved_project_key_mode,
            "exclude_tags": exclude_tags,
            "candidate_rankings": {
                name: rows[:safe_limit] if include_candidates else []
                for name, rows in ranking_sources.items()
            },
            "recent_probe": {
                "count": candidate_counts["recent_probe"],
                "sort_mode": (recent_probe_payload or {}).get("debug", {}).get("sort_mode"),
                "items": [_retrieval_result_stub(item) for item in (recent_probe_payload or {}).get("items", [])[:safe_limit]] if include_candidates else [],
            } if include_recent_probe else {},
        }
    finally:
        conn.close()

    return {
        "status": "ok",
        "query": normalized_query,
        "requested_project_key": requested_project_key,
        "resolved_project_key": canonical_project_key or requested_project_key,
        "applied_aliases": applied_aliases,
        "strategies_attempted": strategies_attempted,
        "selected_strategy": selected_strategy,
        "candidate_counts": candidate_counts,
        "results": results,
        "rejected_or_filtered": rejected_or_filtered if include_rejected else [],
        "warnings": warnings,
        "recommendations": recommendations,
        "debug": debug,
    }


def search_qa_report(
    project_keys: list[str] | None = None,
    limit_per_case: int = 5,
) -> dict[str, Any]:
    requested_keys = {normalize_optional_text(key) for key in (project_keys or []) if normalize_optional_text(key)}
    canonical_requested_keys: set[str] = set()
    if requested_keys:
        conn = get_db_connection()
        try:
            for key in requested_keys:
                _, _, canonical_project_key = _resolve_project_key_filter(conn, project_key=key, project_key_mode="aliases")
                canonical_requested_keys.add(canonical_project_key or key)
        finally:
            conn.close()
    cases = [
        case for case in _RETRIEVAL_QA_CASES
        if not requested_keys or str(case.get("expected_resolved_project_key")) in canonical_requested_keys
    ]
    case_results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    warnings: list[str] = []
    passed = 0

    for case in cases:
        explain = explain_retrieval(
            query=str(case["query"]),
            project_key=case.get("project_key"),
            project_key_mode="aliases",
            limit=limit_per_case,
            include_candidates=False,
            include_rejected=True,
            include_recent_probe=True,
        )
        resolved_project_key = explain.get("resolved_project_key")
        top_ids = [int(item.get("memory_id") or 0) for item in explain.get("results", [])[:limit_per_case]]
        forbid_tags = {str(tag).lower() for tag in case.get("forbid_tags", ())}
        top_tag_sets = [_tag_tokens(item.get("tags")) for item in explain.get("results", [])[:3]]
        forbid_hit = any(forbid_tags.intersection(tag_set) for tag_set in top_tag_sets)
        status = "passed"
        reasons: list[str] = []
        if resolved_project_key != case.get("expected_resolved_project_key"):
            status = "failed"
            reasons.append("resolved_project_key_mismatch")
        if not explain.get("results"):
            status = "failed"
            reasons.append("no_results")
        if forbid_hit:
            status = "failed"
            reasons.append("forbidden_infra_tag_in_top_results")

        if status == "passed":
            passed += 1
        else:
            failures.append({
                "case_id": case["case_id"],
                "query": case["query"],
                "project_key": case.get("project_key"),
                "reasons": reasons,
            })
        if explain.get("warnings"):
            warnings.extend(str(item) for item in explain["warnings"])
        case_results.append({
            "case_id": case["case_id"],
            "project_key": case.get("project_key"),
            "query": case["query"],
            "resolved_project_key": resolved_project_key,
            "selected_strategy": explain.get("selected_strategy"),
            "top_memory_ids": top_ids,
            "status": status,
            "warnings": explain.get("warnings", []),
        })

    recent_payload = recent_memories(project_key="demo-project", project_key_mode="aliases", limit=max(2, min(limit_per_case, 5)), debug=True)
    recent_ids = [int(item.get("id") or 0) for item in recent_payload.get("items", [])]
    recent_order_ok = recent_ids == sorted(recent_ids, reverse=True)
    recent_case = {
        "case_id": "recent-search-ordering",
        "project_key": "demo-project",
        "query": "recent",
        "resolved_project_key": recent_payload.get("debug", {}).get("resolved_project_key"),
        "selected_strategy": "created_at_desc",
        "top_memory_ids": recent_ids,
        "status": "passed" if recent_order_ok else "failed",
        "warnings": [] if recent_order_ok else ["Recent ordering was not descending by id."],
    }
    case_results.append(recent_case)
    if recent_order_ok:
        passed += 1
    else:
        failures.append({"case_id": "recent-search-ordering", "query": "recent", "project_key": "demo-project", "reasons": ["recent_ordering_failed"]})

    conn = get_db_connection()
    try:
        golden_materialized = materialize_golden_cases(conn, load_retrieval_golden_corpus())
    finally:
        conn.close()
    golden_cases = list(golden_materialized.get("cases") or [])
    if canonical_requested_keys:
        golden_cases = [
            case for case in golden_cases
            if case.get("expected_project_key") in canonical_requested_keys
            or case.get("project_key") in requested_keys
        ]
    golden = evaluate_golden_cases(
        golden_cases,
        lexical_search=find_memories,
        semantic_search=lambda **_: {"status": "disabled", "results": [], "results_count": 0},
        latency_runs=1,
    )
    golden.update({
        "schema": golden_materialized.get("schema"),
        "corpus_id": golden_materialized.get("corpus_id"),
        "corpus_fingerprint": golden_materialized.get("corpus_fingerprint"),
        "declared_case_count": golden_materialized.get("case_count"),
        "skipped_count": golden_materialized.get("skipped_count"),
        "skipped": golden_materialized.get("skipped") or [],
    })
    for item in golden.get("cases") or []:
        case_results.append({
            "case_id": "golden:" + str(item.get("case_id")),
            "project_key": item.get("project_key"),
            "query": item.get("query"),
            "resolved_project_key": item.get("expected_project_key") or item.get("project_key"),
            "selected_strategy": "golden_corpus",
            "top_memory_ids": list(((item.get("channels") or {}).get("lexical") or {}).get("returned_ids") or []),
            "status": "passed" if item.get("passed") else "failed",
            "warnings": [],
        })
        if not item.get("passed"):
            channel_failures: list[str] = []
            for channel_name, channel in (item.get("channels") or {}).items():
                if channel.get("passed"):
                    continue
                for key in ("missing_expected_ids", "forbidden_returned_ids", "unexpected_ids", "wrong_project_keys"):
                    if channel.get(key):
                        channel_failures.append(f"{channel_name}:{key}")
            failures.append({
                "case_id": "golden:" + str(item.get("case_id")),
                "query": item.get("query"),
                "project_key": item.get("project_key"),
                "reasons": channel_failures or ["golden_case_failed"],
            })
    cases_run = len(case_results)
    passed_total = sum(1 for item in case_results if item.get("status") == "passed")
    return {
        "status": "ok",
        "cases_run": cases_run,
        "passed": passed_total,
        "warnings": sorted(set(warnings)),
        "failures": failures,
        "case_results": case_results,
        "golden_corpus": golden,
    }


_PROJECT_BRIEF_STATE_TYPES = {
    "project_state",
    "continuity",
    "project_decision",
    "operational_protocol",
    "project_note",
    "project_context",
}
_PROJECT_BRIEF_STATE_TAGS = {"next-step", "continuity", "project-state", "decision"}
_PROJECT_BRIEF_DECISION_TYPES = {"project_decision", "operational_protocol"}
_PROJECT_BRIEF_DECISION_TAGS = {"decision", "accepted", "workflow", "operator-rule"}
_PROJECT_BRIEF_NEXT_STEP_TYPES = {"next_step"}
_PROJECT_BRIEF_NEXT_STEP_TAGS = {"next-step"}
_PROJECT_BRIEF_NEXT_STEP_PATTERNS = ("next step", "następny krok", "nastepny krok", "todo", "do zrobienia")
_PROJECT_BRIEF_RISK_TYPES = {"implementation_risk", "bug_report", "security-incident", "blocked", "rejection", "warning"}
_PROJECT_BRIEF_RISK_TAGS = {"failed", "warning", "blocked", "rejection", "known-issue", "known issue", "security-incident"}
_PROJECT_BRIEF_OPEN_QUESTION_TAGS = {"review-required", "unresolved", "needs-review", "missing-owner", "stale", "revalidation"}
_PROJECT_BRIEF_OPEN_QUESTION_PATTERNS = ("needs review", "review required", "missing owner", "stale", "revalidation", "unresolved")
_PROJECT_BRIEF_IMPORTANT_PATH_PATTERN = re.compile(
    r"([A-Za-z]:\\[^\s\"'<>]+|(?:docs|app|tests|workers|scripts|data|artifacts|backups)/[^\s\"'<>]+)"
)


def _brief_normalized_text(value: Any) -> str:
    return (normalize_optional_text(value) or "").lower()


def _brief_tag_set(item: dict[str, Any]) -> set[str]:
    return _tag_tokens(item.get("tags"))


def _brief_text_blob(item: dict[str, Any]) -> str:
    return " ".join([
        _brief_normalized_text(item.get("summary_short")),
        _brief_normalized_text(item.get("content")),
        _brief_normalized_text(item.get("tags")),
    ]).strip()


def _brief_recent_sort(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            normalize_optional_text(item.get("created_at")) or "",
            int(item.get("id") or 0),
        ),
        reverse=True,
    )


def _brief_unique_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in items:
        memory_id = int(item.get("id") or 0)
        if memory_id in seen:
            continue
        seen.add(memory_id)
        result.append(item)
    return result


def _brief_select_items(
    items: list[dict[str, Any]],
    *,
    limit: int,
    predicate,
    score_fn,
) -> list[dict[str, Any]]:
    scored: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for item in items:
        if not predicate(item):
            continue
        score = score_fn(item)
        scored.append(((-score, -float(item.get("importance_score") or 0.0), -int(item.get("id") or 0)), item))
    scored.sort(key=lambda entry: entry[0])
    return [entry[1] for entry in scored[:limit]]


def _section_memory_stub(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "summary_short": item.get("summary_short"),
        "memory_type": item.get("memory_type"),
        "entry_type": item.get("entry_type") or item.get("type"),
        "truth_kind": item.get("truth_kind"),
        "status": item.get("memory_v2_status") or item.get("status"),
        "project_key": item.get("project_key"),
        "tags": item.get("tags"),
        "importance_score": item.get("importance_score"),
        "importance_level": item.get("importance_level"),
        "confidence_score": item.get("confidence_score"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "requires_user_confirmation": bool(item.get("requires_user_confirmation")),
        "supersedes_memory_id": item.get("supersedes_memory_id"),
        "superseded_by_memory_id": item.get("superseded_by_memory_id"),
        "current_state": item.get("current_state") or {},
        "linked_memories": item.get("linked_memories") or [],
    }


def _resolve_project_brief_context(project_key: str) -> dict[str, Any]:
    normalized_project_key = normalize_optional_text(project_key)
    if normalized_project_key is None:
        raise ValueError("project_key cannot be empty")
    conn = get_db_connection()
    try:
        project_key_values, resolved_project_key_mode, canonical_project_key = _resolve_project_key_filter(
            conn,
            project_key=normalized_project_key,
            project_key_mode="aliases",
        )
    finally:
        conn.close()
    return {
        "requested_project_key": normalized_project_key,
        "resolved_project_key": canonical_project_key or normalized_project_key,
        "canonical_project_key": canonical_project_key or normalized_project_key,
        "project_key_mode": resolved_project_key_mode,
        "project_key_values": project_key_values or [normalized_project_key],
        "known_systems": known_systems_for_project(canonical_project_key or normalized_project_key),
        "project_profile": {
            "purpose": project_purpose_for(canonical_project_key or normalized_project_key),
            "anchor_tags": list(project_anchor_tags_for(canonical_project_key or normalized_project_key)),
            "exclude_tags": list(_retrieval_exclude_tags_for(canonical_project_key or normalized_project_key)),
        },
    }


_MEMORY_CAPTURE_EXPLICIT_PATTERNS = (
    "zapamiętaj",
    "zapamietaj",
    "zapisz pamięć",
    "zapisz pamiec",
    "save this",
    "remember this",
    "od teraz",
    "ustalmy",
)
_MEMORY_CAPTURE_DECISION_PATTERNS = ("decyzja", "ustalone", "accepted", "zaakceptowane", "accepted workflow", "postanowione", "ustalmy")
_MEMORY_CAPTURE_PREFERENCE_PATTERNS = ("prefer", "woli", "wolę", "preferenc", "zawsze", "nigdy", "ma robić", "ma robic")
_MEMORY_CAPTURE_INCIDENT_PATTERNS = ("bug", "błąd", "blad", "awaria", "incident", "regres", "failure", "fix", "napraw")
_MEMORY_CAPTURE_PROJECT_PATTERNS = ("project", "projekt", "repo", "wdroż", "wdroz", "deploy", "workflow", "plan")
_MEMORY_CAPTURE_OPEN_QUESTION_PATTERNS = ("?", "open question", "pytanie", "trzeba ustalić", "trzeba ustalic", "review required")
_MEMORY_CAPTURE_TRIVIA_PATTERNS = ("dzięki", "dzieki", "ok", "oki", "super", "thanks")


def _capture_text_blob(*values: Any) -> str:
    return " ".join(filter(None, (normalize_optional_text(str(value)) if value is not None else None for value in values))).strip()


def _capture_summary_candidate(content: str, limit: int = 72) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    if not compact:
        return "Memory proposal"
    sentence = re.split(r"(?<=[\.\!\?])\s+", compact, maxsplit=1)[0].strip()
    base = sentence or compact
    if len(base) <= limit:
        return base
    return base[: max(16, limit - 1)].rstrip() + "…"


def _capture_tags(signals: list[str], project_key: str | None) -> str | None:
    tags: list[str] = []
    if project_key:
        tags.append(project_key)
    tags.extend(signal.replace("_", "-") for signal in signals if signal not in {"explicit_request"})
    unique: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        normalized = normalize_optional_text(tag)
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        unique.append(normalized)
    return ",".join(unique) if unique else None


def _score_capture_signals(signals: list[str]) -> tuple[float, float]:
    if "decision" in signals:
        return 0.86, 0.84
    if "preference" in signals:
        return 0.82, 0.8
    if "incident" in signals:
        return 0.8, 0.8
    if "project" in signals:
        return 0.74, 0.72
    if "open_question" in signals:
        return 0.68, 0.66
    if "explicit_request" in signals:
        return 0.62, 0.68
    return 0.34, 0.45


def _classify_memory_capture(
    *,
    content: str,
    project_key: str | None = None,
    source_context: str | None = None,
    hint: str | None = None,
) -> dict[str, Any]:
    normalized_content = normalize_required_text(content, "content")
    blob = _capture_text_blob(normalized_content, source_context, hint).lower()
    signals: list[str] = []
    reasons: list[str] = []

    def add_signal(signal: str, reason: str) -> None:
        if signal not in signals:
            signals.append(signal)
        reasons.append(reason)

    if any(pattern in blob for pattern in _MEMORY_CAPTURE_EXPLICIT_PATTERNS):
        add_signal("explicit_request", "Detected explicit memory/save phrasing.")
    if any(pattern in blob for pattern in _MEMORY_CAPTURE_DECISION_PATTERNS):
        add_signal("decision", "Detected decision-like wording.")
    if any(pattern in blob for pattern in _MEMORY_CAPTURE_PREFERENCE_PATTERNS):
        add_signal("preference", "Detected stable preference or rule wording.")
    if any(pattern in blob for pattern in _MEMORY_CAPTURE_INCIDENT_PATTERNS):
        add_signal("incident", "Detected incident/bug/fix wording.")
    if any(pattern in blob for pattern in _MEMORY_CAPTURE_PROJECT_PATTERNS) or normalize_optional_text(project_key):
        add_signal("project", "Detected project/repo/workflow context.")
    if any(pattern in blob for pattern in _MEMORY_CAPTURE_OPEN_QUESTION_PATTERNS):
        add_signal("open_question", "Detected unresolved question or review wording.")

    compact = re.sub(r"\s+", " ", normalized_content).strip()
    low_signal_trivia = len(compact) <= 24 and any(pattern == compact.lower() for pattern in _MEMORY_CAPTURE_TRIVIA_PATTERNS)
    if low_signal_trivia and not signals:
        return {
            "status": "skipped",
            "message": "Content looks transient and not worth storing as memory.",
            "skip_reason": "transient_trivia",
            "signals": [],
            "reasons": ["Short acknowledgment without durable project value."],
        }

    if not signals and len(compact.split()) < 5:
        return {
            "status": "skipped",
            "message": "Content is too thin for a durable memory proposal.",
            "skip_reason": "too_little_signal",
            "signals": [],
            "reasons": ["No explicit save/decision/preference/project/incident signal detected."],
        }

    if "decision" in signals:
        memory_type = "project_decision" if "project" in signals else "decision"
        entry_type = "decision"
        truth_kind = "decision"
    elif "preference" in signals:
        memory_type = "profile_note"
        entry_type = "user_profile"
        truth_kind = "preference"
    elif "incident" in signals:
        memory_type = "bug_report"
        entry_type = "incident"
        truth_kind = "fact"
    elif "open_question" in signals:
        memory_type = "open_question"
        entry_type = "open_question"
        truth_kind = "proposal"
    elif "project" in signals:
        memory_type = "project_note"
        entry_type = "project"
        truth_kind = "fact"
    else:
        memory_type = "raw_note"
        entry_type = "raw_note"
        truth_kind = "proposal"

    importance_score, confidence_score = _score_capture_signals(signals)
    summary_short = _capture_summary_candidate(normalized_content)
    proposal = {
        "content": normalized_content,
        "memory_type": memory_type,
        "summary_short": summary_short,
        "title": summary_short,
        "project_key": normalize_optional_text(project_key),
        "source_context": normalize_optional_text(source_context),
        "source_event_ref": None,
        "entry_type": entry_type,
        "truth_kind": truth_kind,
        "importance_score": importance_score,
        "confidence_score": confidence_score,
        "memory_v2_status": "proposed",
        "requires_user_confirmation": True,
        "tags": _capture_tags(signals, project_key),
        "should_resurface_when": [
            "użytkownik zatwierdza propozycję zapisu",
            "wraca temat tego projektu lub decyzji",
        ],
    }
    return {
        "status": "proposed",
        "message": "Memory proposal prepared. It should be approved, edited, or rejected before durable save.",
        "signals": signals,
        "reasons": reasons,
        "proposal": proposal,
        "edit_hints": [
            "content",
            "summary_short",
            "title",
            "memory_type",
            "entry_type",
            "truth_kind",
            "project_key",
            "tags",
        ],
    }


def get_recent_project_changes(
    project_key: str,
    since_id: int | None = None,
    since_date: str | None = None,
    limit: int = 20,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Return recent project memories chronologically for one resolved project family."""
    context = _resolve_project_brief_context(project_key)
    candidate_limit = _retrieval_candidate_limit(int(limit or 20))
    payload = list_memories(
        project_key=context["requested_project_key"],
        project_key_mode="aliases",
        limit=candidate_limit,
        sort_by="created_at_desc",
        include_history=True,
        debug=include_debug,
    )
    items = _brief_recent_sort(payload["items"])
    if since_id is not None:
        items = [item for item in items if int(item.get("id") or 0) > int(since_id)]
    normalized_since_date = normalize_optional_text(since_date)
    if normalized_since_date:
        items = [
            item
            for item in items
            if (normalize_optional_text(item.get("created_at")) or "") >= normalized_since_date
        ]
    items = items[: max(1, min(int(limit or 20), 50))]
    result = {
        "status": "ok",
        "requested_project_key": context["requested_project_key"],
        "resolved_project_key": context["resolved_project_key"],
        "canonical_project_key": context["canonical_project_key"],
        "since_id": None if since_id is None else int(since_id),
        "since_date": normalized_since_date,
        "items": [_section_memory_stub(item) for item in items],
        "source_memory_ids": [int(item["id"]) for item in items if item.get("id") is not None],
        "debug": {},
    }
    if include_debug:
        result["debug"] = {
            "sort_mode": "created_at_desc",
            "project_key_values": context["project_key_values"],
            "exclude_tags": context["project_profile"]["exclude_tags"],
            "counts": {
                "candidate_items": int(payload["count"]),
                "returned_items": len(items),
            },
        }
    return result


def get_project_brief(
    project_key: str,
    limit: int = 12,
    include_recent: bool = True,
    include_next_steps: bool = True,
    include_risks: bool = True,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Build a compact project briefing for one resolved project family."""
    safe_limit = max(4, min(int(limit or 12), 24))
    context = _resolve_project_brief_context(project_key)
    active_payload = list_memories(
        project_key=context["requested_project_key"],
        project_key_mode="aliases",
        limit=_retrieval_candidate_limit(safe_limit),
        sort_by="active",
        debug=include_debug,
    )
    recent_payload = list_memories(
        project_key=context["requested_project_key"],
        project_key_mode="aliases",
        limit=_retrieval_candidate_limit(safe_limit),
        sort_by="created_at_desc",
        debug=include_debug,
    )
    active_items = active_payload["items"]
    recent_items = _brief_recent_sort(recent_payload["items"])
    pool_items = _brief_unique_items(active_items + recent_items)

    def state_predicate(item: dict[str, Any]) -> bool:
        memory_type = _brief_normalized_text(item.get("memory_type"))
        tags = _brief_tag_set(item)
        return (
            memory_type in _PROJECT_BRIEF_STATE_TYPES
            or bool(tags & _PROJECT_BRIEF_STATE_TAGS)
            or float(item.get("importance_score") or 0.0) >= 0.8
        )

    def state_score(item: dict[str, Any]) -> float:
        memory_type = _brief_normalized_text(item.get("memory_type"))
        tags = _brief_tag_set(item)
        score = float(item.get("importance_score") or 0.0) * 100.0
        if memory_type in {"project_state", "continuity"}:
            score += 80.0
        if memory_type in {"project_decision", "operational_protocol"}:
            score += 55.0
        score += 18.0 * len(tags & _PROJECT_BRIEF_STATE_TAGS)
        return score

    def decision_predicate(item: dict[str, Any]) -> bool:
        memory_type = _brief_normalized_text(item.get("memory_type"))
        truth_kind = _brief_normalized_text(item.get("truth_kind"))
        tags = _brief_tag_set(item)
        return truth_kind == "decision" or memory_type in _PROJECT_BRIEF_DECISION_TYPES or bool(tags & _PROJECT_BRIEF_DECISION_TAGS)

    def decision_score(item: dict[str, Any]) -> float:
        tags = _brief_tag_set(item)
        return float(item.get("importance_score") or 0.0) * 100.0 + 24.0 * len(tags & _PROJECT_BRIEF_DECISION_TAGS)

    def next_step_predicate(item: dict[str, Any]) -> bool:
        memory_type = _brief_normalized_text(item.get("memory_type"))
        tags = _brief_tag_set(item)
        blob = _brief_text_blob(item)
        return (
            memory_type in _PROJECT_BRIEF_NEXT_STEP_TYPES
            or bool(tags & _PROJECT_BRIEF_NEXT_STEP_TAGS)
            or any(pattern in blob for pattern in _PROJECT_BRIEF_NEXT_STEP_PATTERNS)
        )

    def next_step_score(item: dict[str, Any]) -> float:
        blob = _brief_text_blob(item)
        tags = _brief_tag_set(item)
        score = float(item.get("importance_score") or 0.0) * 100.0
        score += 24.0 * len(tags & _PROJECT_BRIEF_NEXT_STEP_TAGS)
        score += 12.0 * sum(1 for pattern in _PROJECT_BRIEF_NEXT_STEP_PATTERNS if pattern in blob)
        return score

    def risk_predicate(item: dict[str, Any]) -> bool:
        memory_type = _brief_normalized_text(item.get("memory_type"))
        tags = _brief_tag_set(item)
        blob = _brief_text_blob(item)
        return (
            memory_type in _PROJECT_BRIEF_RISK_TYPES
            or bool(tags & _PROJECT_BRIEF_RISK_TAGS)
            or any(pattern in blob for pattern in ("failed", "warning", "blocked", "known issue", "rejection"))
        )

    def risk_score(item: dict[str, Any]) -> float:
        tags = _brief_tag_set(item)
        blob = _brief_text_blob(item)
        return float(item.get("importance_score") or 0.0) * 100.0 + 18.0 * len(tags & _PROJECT_BRIEF_RISK_TAGS) + 8.0 * sum(
            1 for pattern in ("failed", "warning", "blocked", "known issue", "rejection") if pattern in blob
        )

    def open_question_predicate(item: dict[str, Any]) -> bool:
        if (item.get("current_state") or {}).get("resolved_by_memory_id"):
            return False
        tags = _brief_tag_set(item)
        memory_type = _brief_normalized_text(item.get("memory_type"))
        truth_kind = _brief_normalized_text(item.get("truth_kind"))
        return (
            memory_type in {"open_question", "question", "project_question"}
            or truth_kind in {"question", "proposal"}
            or bool(tags & _PROJECT_BRIEF_OPEN_QUESTION_TAGS)
            or bool(item.get("requires_user_confirmation"))
        )

    def open_question_score(item: dict[str, Any]) -> float:
        tags = _brief_tag_set(item)
        blob = _brief_text_blob(item)
        return float(item.get("importance_score") or 0.0) * 100.0 + 16.0 * len(tags & _PROJECT_BRIEF_OPEN_QUESTION_TAGS) + 8.0 * sum(
            1 for pattern in _PROJECT_BRIEF_OPEN_QUESTION_PATTERNS if pattern in blob
        )

    current_state_items = _brief_select_items(pool_items, limit=safe_limit, predicate=state_predicate, score_fn=state_score)
    if not current_state_items:
        current_state_items = active_items[:safe_limit]
    recent_changes_payload = get_recent_project_changes(
        context["requested_project_key"],
        limit=safe_limit,
        include_debug=include_debug,
    )
    recent_changes_items = recent_changes_payload["items"] if include_recent else []
    decision_candidates = _brief_select_items(pool_items, limit=safe_limit, predicate=decision_predicate, score_fn=decision_score)
    decisions_items = _brief_recent_sort(decision_candidates)[: max(3, safe_limit // 2)]
    next_step_items = _brief_select_items(pool_items, limit=max(3, safe_limit // 2), predicate=next_step_predicate, score_fn=next_step_score) if include_next_steps else []
    risk_items = _brief_select_items(pool_items, limit=max(3, safe_limit // 2), predicate=risk_predicate, score_fn=risk_score) if include_risks else []
    open_question_items = _brief_select_items(pool_items, limit=max(3, safe_limit // 2), predicate=open_question_predicate, score_fn=open_question_score)

    important_paths: list[str] = []
    seen_paths: set[str] = set()
    for item in _brief_unique_items(current_state_items + decisions_items + next_step_items + risk_items + open_question_items):
        blob = " ".join(filter(None, [
            normalize_optional_text(item.get("summary_short")),
            normalize_optional_text(item.get("content")),
        ]))
        for match in _PROJECT_BRIEF_IMPORTANT_PATH_PATTERN.findall(blob):
            if match not in seen_paths:
                seen_paths.add(match)
                important_paths.append(match)

    source_memory_ids = sorted({
        int(item["id"])
        for item in _brief_unique_items(current_state_items + recent_changes_items + decisions_items + next_step_items + risk_items + open_question_items)
        if item.get("id") is not None
    })
    section_presence = sum(
        1
        for bucket in (current_state_items, recent_changes_items, decisions_items, next_step_items, risk_items, open_question_items)
        if bucket
    )
    confidence_score = min(0.95, 0.5 + (0.06 * section_presence) + (0.02 * min(len(source_memory_ids), 10)))
    confidence_notes = [
        "Brief is deterministic and assembled from resolved project-family retrieval, not freeform synthesis.",
    ]
    if not current_state_items:
        confidence_notes.append("Current state section is sparse.")
    if include_recent and not recent_changes_items:
        confidence_notes.append("Recent changes section is sparse or absent.")

    result = {
        "status": "ok",
        "requested_project_key": context["requested_project_key"],
        "resolved_project_key": context["resolved_project_key"],
        "canonical_project_key": context["canonical_project_key"],
        "known_systems": context["known_systems"],
        "project_profile": context["project_profile"],
        "current_state": [_section_memory_stub(item) for item in current_state_items],
        "recent_changes": recent_changes_items,
        "decisions": [_section_memory_stub(item) for item in decisions_items],
        "next_steps": [_section_memory_stub(item) for item in next_step_items],
        "risks_or_warnings": [_section_memory_stub(item) for item in risk_items],
        "open_questions": [_section_memory_stub(item) for item in open_question_items],
        "important_paths": important_paths,
        "source_memory_ids": source_memory_ids,
        "confidence": {
            "score": round(confidence_score, 3),
            "notes": confidence_notes,
        },
        "debug": {},
    }
    if include_debug:
        result["debug"] = {
            "strategy": [
                "resolve_project_key_aliases",
                "collect_active_candidates",
                "collect_recent_candidates",
                "section_selectors",
            ],
            "project_key_values": context["project_key_values"],
            "counts_per_section": {
                "current_state": len(current_state_items),
                "recent_changes": len(recent_changes_items),
                "decisions": len(decisions_items),
                "next_steps": len(next_step_items),
                "risks_or_warnings": len(risk_items),
                "open_questions": len(open_question_items),
            },
            "recent_sort_mode": recent_changes_payload.get("debug", {}).get("sort_mode", "created_at_desc"),
            "active_candidate_count": int(active_payload["count"]),
            "recent_candidate_count": int(recent_payload["count"]),
            "source_memory_ids": source_memory_ids,
        }
    return result


_PROJECT_CARD_COMPLETED_PATTERNS = ("completed", "done", "zamkniete", "zamknięte", "finished")
_PROJECT_CARD_PAUSED_PATTERNS = ("paused", "on hold", "hold", "wstrzymane", "wstrzymany")


def _project_card_primary_text(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    return (
        normalize_optional_text(item.get("title"))
        or normalize_optional_text(item.get("summary_short"))
        or normalize_optional_text(item.get("content"))
    )


def _project_card_updated_at(items: list[dict[str, Any]]) -> str | None:
    timestamps = [
        normalize_optional_text(item.get("updated_at")) or normalize_optional_text(item.get("created_at"))
        for item in items
    ]
    timestamps = [value for value in timestamps if value]
    if not timestamps:
        return None
    return max(timestamps)


def _project_card_status(
    pool_items: list[dict[str, Any]],
    *,
    open_question_items: list[dict[str, Any]],
    next_step_items: list[dict[str, Any]],
) -> str:
    normalized_statuses = {
        _brief_normalized_text(item.get("memory_v2_status") or item.get("status"))
        for item in pool_items
        if _brief_normalized_text(item.get("memory_v2_status") or item.get("status"))
    }
    if normalized_statuses and normalized_statuses <= {"archived", "superseded"}:
        return "archived"

    for item in _brief_recent_sort(pool_items)[:6]:
        blob = _brief_text_blob(item)
        if any(pattern in blob for pattern in _PROJECT_CARD_COMPLETED_PATTERNS):
            return "completed"
        if any(pattern in blob for pattern in _PROJECT_CARD_PAUSED_PATTERNS):
            return "paused"

    if next_step_items or open_question_items:
        return "active"
    return "active"


def _project_card_proposed_updates(section_items: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    for field_name, items in section_items.items():
        for item in items:
            status = _brief_normalized_text(item.get("memory_v2_status") or item.get("status"))
            requires_confirmation = bool(item.get("requires_user_confirmation"))
            if not requires_confirmation and status != "proposed":
                continue
            proposals.append(
                {
                    "field": field_name,
                    "memory_id": item.get("id"),
                    "summary_short": item.get("summary_short"),
                    "title": item.get("title"),
                    "status": status or "proposed",
                    "requires_user_confirmation": requires_confirmation or status == "proposed",
                }
            )
    return proposals


def get_project_card(
    project_key: str,
    limit: int = 12,
    include_recent: bool = True,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Build a compact ProjectCard derived from the resolved project brief."""
    brief = get_project_brief(
        project_key=project_key,
        limit=limit,
        include_recent=include_recent,
        include_next_steps=True,
        include_risks=True,
        include_debug=include_debug,
    )
    current_state_items = list(brief.get("current_state") or [])
    recent_changes_items = list(brief.get("recent_changes") or [])
    decisions_items = list(brief.get("decisions") or [])
    next_step_items = list(brief.get("next_steps") or [])
    incident_items = list(brief.get("risks_or_warnings") or [])
    open_question_items = list(brief.get("open_questions") or [])
    pool_items = _brief_unique_items(
        current_state_items + recent_changes_items + decisions_items + next_step_items + incident_items + open_question_items
    )

    title = brief.get("canonical_project_key") or brief.get("resolved_project_key") or normalize_optional_text(project_key)
    goal = (
        normalize_optional_text(brief.get("project_profile", {}).get("purpose"))
        or _project_card_primary_text(current_state_items[0] if current_state_items else None)
        or title
    )
    last_decision = _project_card_primary_text(decisions_items[0] if decisions_items else None)
    next_step = _project_card_primary_text(next_step_items[0] if next_step_items else None)
    systems_or_files = list(dict.fromkeys(list(brief.get("known_systems") or []) + list(brief.get("important_paths") or [])))[:12]
    linked_memories = [int(memory_id) for memory_id in brief.get("source_memory_ids", [])]
    proposed_updates = _project_card_proposed_updates(
        {
            "last_decision": decisions_items[:1],
            "next_step": next_step_items[:1],
            "open_questions": open_question_items,
            "incidents": incident_items,
        }
    )

    result = {
        "status": "ok",
        "requested_project_key": brief.get("requested_project_key"),
        "resolved_project_key": brief.get("resolved_project_key"),
        "canonical_project_key": brief.get("canonical_project_key"),
        "project_card": {
            "project_id": brief.get("canonical_project_key"),
            "title": title,
            "goal": goal,
            "status": _project_card_status(
                pool_items,
                open_question_items=open_question_items,
                next_step_items=next_step_items,
            ),
            "last_decision": last_decision,
            "open_questions": [text for text in (_project_card_primary_text(item) for item in open_question_items[:6]) if text],
            "systems_or_files": systems_or_files,
            "next_step": next_step,
            "linked_memories": linked_memories,
            "updated_at": _project_card_updated_at(pool_items),
            "owner_key": private_owner_key(),
            "runtime_mode": runtime_mode(),
            "private_single_user": runtime_mode() == "private_single_user",
        },
        "decision_links": decisions_items,
        "incident_links": incident_items,
        "open_question_links": open_question_items,
        "recent_changes": recent_changes_items if include_recent else [],
        "proposed_updates": proposed_updates,
        "source_memory_ids": linked_memories,
        "confidence": brief.get("confidence"),
        "debug": {},
    }
    if include_debug:
        result["debug"] = {
            "derived_from": "get_project_brief",
            "counts": {
                "current_state": len(current_state_items),
                "recent_changes": len(recent_changes_items),
                "decisions": len(decisions_items),
                "incidents": len(incident_items),
                "open_questions": len(open_question_items),
                "proposed_updates": len(proposed_updates),
            },
            "project_brief_debug": brief.get("debug", {}),
        }
    return result


def get_memory_restore_ritual(
    project_key: str | None = None,
    full: bool = False,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Return a structured restore ritual: remembered, uncertain, next steps."""
    conn = get_db_connection()
    try:
        if not _is_memory_v2_feature_active(conn):
            return {"status": "disabled", "reason": "feature_flag_off", "flag_key": MEMORY_V2_FLAG_KEY}
    finally:
        conn.close()
    limit = 16 if full else 8
    normalized_project_key = normalize_optional_text(project_key)
    active_payload = list_memories(
        project_key=normalized_project_key,
        project_key_mode="aliases" if normalized_project_key else "exact",
        limit=limit,
        sort_by="active",
        debug=include_debug,
    )
    recent_payload = recent_memories(
        project_key=normalized_project_key,
        project_key_mode="aliases" if normalized_project_key else "exact",
        limit=limit,
        debug=include_debug,
    )
    pool_items = _brief_unique_items(list(active_payload.get("items") or []) + list(recent_payload.get("items") or []))
    pool_items = _brief_recent_sort(pool_items)

    remembered: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    next_steps: list[dict[str, Any]] = []

    for item in pool_items:
        status = _brief_normalized_text(item.get("memory_v2_status") or item.get("status"))
        truth_kind = _brief_normalized_text(item.get("truth_kind"))
        importance_score = float(item.get("importance_score") or 0.0)
        if (
            len(remembered) < max(3, limit // 2)
            and status == "active"
            and truth_kind in {"fact", "decision", "preference"}
            and importance_score >= 0.6
        ):
            remembered.append(_section_memory_stub(item))
            continue
        if (
            len(uncertain) < max(3, limit // 2)
            and (
                status in {"stale", "proposed", "contradicted", "archived", "superseded"}
                or truth_kind in {"dream", "interpretation", "proposal"}
                or bool(item.get("requires_user_confirmation"))
            )
        ):
            uncertain.append(_section_memory_stub(item))

    if normalized_project_key:
        project_card = get_project_card(normalized_project_key, limit=limit, include_recent=full, include_debug=include_debug)
        for item in project_card.get("open_question_links") or []:
            if len(uncertain) >= max(3, limit // 2):
                break
            if not any(existing.get("id") == item.get("id") for existing in uncertain):
                uncertain.append(item)
        for item in project_card.get("recent_changes") or []:
            if len(next_steps) >= max(3, limit // 2):
                break
            if _brief_normalized_text(item.get("memory_type")) == "next_step" or "next-step" in _brief_tag_set(item):
                next_steps.append(item)
        if not next_steps and project_card.get("project_card", {}).get("next_step"):
            next_steps.append({
                "summary_short": project_card["project_card"]["next_step"],
                "memory_type": "next_step",
                "project_key": project_card.get("canonical_project_key"),
            })
    else:
        for item in pool_items:
            if len(next_steps) >= max(3, limit // 2):
                break
            if _brief_normalized_text(item.get("memory_type")) == "next_step" or "next-step" in _brief_tag_set(item):
                next_steps.append(_section_memory_stub(item))

    if not next_steps:
        if normalized_project_key:
            next_steps.append(
                {
                    "summary_short": f"Review project card and recent changes for {normalized_project_key}.",
                    "memory_type": "next_step",
                    "project_key": normalized_project_key,
                }
            )
        else:
            next_steps.append(
                {
                    "summary_short": "Review recent proposed or stale memories and confirm the ones that still matter.",
                    "memory_type": "next_step",
                }
            )

    source_memory_ids = sorted(
        {
            int(item["id"])
            for bucket in (remembered, uncertain, next_steps)
            for item in bucket
            if item.get("id") is not None
        }
    )
    result = {
        "status": "ok",
        "project_key": normalized_project_key,
        "mode": "full" if full else "short",
        "remembered": remembered,
        "uncertain": uncertain,
        "next_steps": next_steps,
        "source_memory_ids": source_memory_ids,
        "notes": {
            "remembered": "Active facts, decisions or preferences with higher importance.",
            "uncertain": "Dreams, proposals, stale or contradicted memories that require caution.",
            "next_steps": "Practical actions inferred from next-step memories or project card.",
        },
        "debug": {},
    }
    if include_debug:
        result["debug"] = {
            "active_count": int(active_payload.get("count") or 0),
            "recent_count": int(recent_payload.get("count") or 0),
            "pool_count": len(pool_items),
        }
    return result


def _v2_review_stub(item: dict[str, Any], *, review_reason: str | None = None) -> dict[str, Any]:
    payload = _section_memory_stub(item)
    payload["review_reason"] = review_reason
    payload["review_due_at"] = normalize_optional_text(item.get("review_due_at"))
    payload["revalidation_due_at"] = normalize_optional_text(item.get("revalidation_due_at"))
    payload["expired_due_at"] = normalize_optional_text(item.get("expired_due_at"))
    payload["superseded_by_memory_id"] = item.get("superseded_by_memory_id")
    payload["requires_user_confirmation"] = bool(item.get("requires_user_confirmation"))
    return payload


def _fetch_memory_v2_review_rows(
    conn,
    *,
    where_sql: str,
    params: list[Any],
    limit: int,
    review_reason: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT *
        FROM memories
        WHERE {where_sql}
        ORDER BY
            COALESCE(expired_due_at, review_due_at, revalidation_due_at, updated_at, created_at) ASC,
            COALESCE(importance_score, 0) DESC,
            id DESC
        LIMIT ?
        """,
        [*params, int(limit)],
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        items.append(_v2_review_stub(item, review_reason=review_reason))
    return items


def _health_report_stub(item: dict[str, Any], *, review_reason: str | None = None) -> dict[str, Any]:
    payload = _v2_review_stub(item, review_reason=review_reason)
    payload["source_context"] = normalize_optional_text(item.get("source_context"))
    payload["source"] = normalize_optional_text(item.get("source"))
    return payload


def _fetch_memory_health_rows(
    conn,
    *,
    where_sql: str,
    params: list[Any],
    limit: int,
    review_reason: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT *
        FROM memories
        WHERE {where_sql}
        ORDER BY
            COALESCE(review_due_at, revalidation_due_at, expired_due_at, updated_at, created_at) ASC,
            COALESCE(importance_score, 0) DESC,
            id DESC
        LIMIT ?
        """,
        [*params, int(limit)],
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        items.append(_health_report_stub(item, review_reason=review_reason))
    return items


_CONSOLIDATION_REVIEW_STATUSES = {"pending", "approved", "rejected", "superseded"}
_CONSOLIDATION_APPLY_SUPPORTED_TYPES = {"merge_duplicates"}


def _normalize_consolidation_proposal_status(value: Any) -> str:
    normalized = normalize_optional_text(value)
    if normalized in _CONSOLIDATION_REVIEW_STATUSES:
        return normalized
    return "pending"


def _normalize_consolidation_proposal_id(proposal_id: str) -> int | None:
    normalized = normalize_optional_text(proposal_id)
    if normalized is None:
        return None
    candidate = normalized
    if normalized.startswith("memory:"):
        candidate = normalized.split(":", 1)[1].strip()
    if not candidate.isdigit():
        return None
    parsed = int(candidate)
    return parsed if parsed > 0 else None


def _consolidation_proposal_public_id(memory_id: int) -> str:
    return f"memory:{int(memory_id)}"


def _consolidation_review_item_to_dict(row) -> dict[str, Any]:
    item = row_to_dict(row)
    item["proposal_memory_id"] = int(item["proposal_memory_id"])
    item["status"] = _normalize_consolidation_proposal_status(item.get("status"))
    return item


def _get_or_create_consolidation_review_item(conn, proposal_memory_id: int) -> dict[str, Any]:
    normalized_id = int(proposal_memory_id)
    row = conn.execute(
        "SELECT * FROM memory_consolidation_review_items WHERE proposal_memory_id = ?",
        (normalized_id,),
    ).fetchone()
    if row is None:
        now_iso = utc_now_iso()
        conn.execute(
            """
            INSERT INTO memory_consolidation_review_items (
                proposal_memory_id, status, created_at, updated_at
            )
            VALUES (?, 'pending', ?, ?)
            """,
            (normalized_id, now_iso, now_iso),
        )
        row = conn.execute(
            "SELECT * FROM memory_consolidation_review_items WHERE proposal_memory_id = ?",
            (normalized_id,),
        ).fetchone()
    return _consolidation_review_item_to_dict(row)


def _find_consolidation_review_item(conn, proposal_memory_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM memory_consolidation_review_items WHERE proposal_memory_id = ?",
        (int(proposal_memory_id),),
    ).fetchone()
    if row is None:
        return None
    return _consolidation_review_item_to_dict(row)


def _consolidation_proposal_source_ids(conn, proposal_memory_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT to_memory_id
        FROM memory_links
        WHERE from_memory_id = ?
        ORDER BY to_memory_id ASC
        """,
        (int(proposal_memory_id),),
    ).fetchall()
    return [int(row["to_memory_id"]) for row in rows]


def _extract_consolidation_proposal_field(content: str | None, label: str) -> str | None:
    text = normalize_optional_text(content)
    if text is None:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        prefix = f"{label}:"
        if stripped.startswith(prefix):
            value = stripped[len(prefix):].strip()
            return value or None
    return None


def _consolidation_proposal_type(item: dict[str, Any]) -> str:
    action = _extract_consolidation_proposal_field(item.get("content"), "Akcja")
    if action:
        return action
    source = normalize_optional_text(item.get("source")) or ""
    match = re.search(r":consolidation:([^:]+):", source)
    if match:
        return match.group(1).strip() or "review_candidate"
    return "review_candidate"


def _consolidation_proposal_rationale(item: dict[str, Any]) -> str:
    rationale = _extract_consolidation_proposal_field(item.get("content"), "Uzasadnienie")
    if rationale:
        return rationale
    content = normalize_optional_text(item.get("content"))
    return content or ""


def _consolidation_proposal_risk_level(item: dict[str, Any]) -> str:
    confidence = item.get("confidence_score")
    if confidence is None:
        return "unknown"
    score = float(confidence or 0.0)
    if score >= 0.8:
        return "low"
    if score >= 0.6:
        return "medium"
    return "high"


def _consolidation_proposal_tags(tags: Any) -> list[str]:
    normalized = normalize_optional_text(tags)
    if normalized is None:
        return []
    return sorted({part.strip() for part in normalized.split(",") if part.strip()})


def _consolidation_proposal_status(item: dict[str, Any], review_item: dict[str, Any] | None) -> str:
    if review_item is not None:
        return _normalize_consolidation_proposal_status(review_item.get("status"))
    if _brief_normalized_text(item.get("memory_v2_status") or item.get("status")) == "superseded":
        return "superseded"
    return "pending"


def _consolidation_apply_source_memories(conn, source_memory_ids: list[int]) -> list[dict[str, Any]]:
    memories: list[dict[str, Any]] = []
    for source_memory_id in source_memory_ids:
        source_row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND archived_at IS NULL",
            (int(source_memory_id),),
        ).fetchone()
        if source_row is None:
            continue
        memories.append(
            _apply_effective_owner(
                conn,
                _apply_ownership_defaults(enrich_memory_dict(row_to_dict(source_row))),
            )
        )
    return memories


def _build_consolidation_apply_preview(
    conn,
    item: dict[str, Any],
    *,
    review_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    proposal = _build_consolidation_proposal_payload(conn, item, review_item=review_item)
    source_memory_ids = list(proposal.get("source_memory_ids") or [])
    source_memories = _consolidation_apply_source_memories(conn, source_memory_ids)
    blocking_reasons: list[str] = []
    unsupported_metrics = list(proposal.get("unsupported_metrics") or [])
    proposal_type = str(proposal.get("proposal_type") or "review_candidate")

    if proposal.get("status") != "approved":
        blocking_reasons.append("proposal_must_be_approved_before_apply")
    if proposal_type not in _CONSOLIDATION_APPLY_SUPPORTED_TYPES:
        blocking_reasons.append("unsupported_proposal_type")
        unsupported_metrics.append(
            f"proposal_type={proposal_type} is not yet supported by apply_approved_memory_consolidation_proposal"
        )
    if len(source_memory_ids) < 2:
        blocking_reasons.append("at_least_two_source_memories_are_required")

    candidate = None
    if not blocking_reasons:
        candidate = consolidation_logic.build_candidate_from_memory_ids(conn, source_memory_ids)
        if candidate is None:
            blocking_reasons.append("source_memories_do_not_form_a_safe_explicit_consolidation_candidate")

    diff = {
        "support_links_to_create": [],
        "summary_memory": {"action": "none"},
        "summary_links_to_create": [],
        "canonical_evidence_boost": None,
    }
    summary: dict[str, Any] = {
        "changed_count": 0,
        "support_links_created_count": 0,
        "summary_memory_created_count": 0,
        "summary_links_created_count": 0,
        "central_evidence_boost_count": 0,
    }

    if candidate is not None:
        central_id = int(candidate["central_memory_id"])
        support_weight = float(candidate.get("average_gravity") or 0.5) or 0.5
        for member_id in candidate["supporting_memory_ids"]:
            if consolidation_logic.support_link_exists(conn, int(member_id), central_id):
                continue
            diff["support_links_to_create"].append(
                {
                    "from_memory_id": int(member_id),
                    "to_memory_id": central_id,
                    "relation_type": "supports",
                    "weight": round(float(support_weight), 4),
                }
            )

        summary_memory_id = candidate.get("existing_summary_memory_id") if bool(candidate.get("reusable_summary_exact_match")) else None
        if summary_memory_id is not None:
            diff["summary_memory"] = {
                "action": "reuse",
                "memory_id": int(summary_memory_id),
            }
        else:
            diff["summary_memory"] = {
                "action": "create",
                "memory_payload": candidate["proposed_summary_memory"],
            }

        summary_ref = int(summary_memory_id) if summary_memory_id is not None else None
        if summary_ref is not None:
            if not consolidation_logic.summary_link_exists(conn, summary_ref, central_id, "summarizes"):
                diff["summary_links_to_create"].append(
                    {
                        "from_memory_id": summary_ref,
                        "to_memory_id": central_id,
                        "relation_type": "summarizes",
                        "weight": 1.0,
                    }
                )
            for member_id in candidate["member_ids"]:
                if consolidation_logic.summary_link_exists(conn, summary_ref, int(member_id), "consolidated_from"):
                    continue
                diff["summary_links_to_create"].append(
                    {
                        "from_memory_id": summary_ref,
                        "to_memory_id": int(member_id),
                        "relation_type": "consolidated_from",
                        "weight": 1.0,
                    }
                )
        else:
            diff["summary_links_to_create"].append(
                {
                    "from_memory_id": "new_summary_memory",
                    "to_memory_id": central_id,
                    "relation_type": "summarizes",
                    "weight": 1.0,
                }
            )
            for member_id in candidate["member_ids"]:
                diff["summary_links_to_create"].append(
                    {
                        "from_memory_id": "new_summary_memory",
                        "to_memory_id": int(member_id),
                        "relation_type": "consolidated_from",
                        "weight": 1.0,
                    }
                )

        support_count = len(diff["support_links_to_create"])
        if support_count:
            central_row = require_memory_row(conn, central_id)
            old_evidence_count = int(central_row["evidence_count"] or 1)
            diff["canonical_evidence_boost"] = {
                "memory_id": central_id,
                "old_evidence_count": old_evidence_count,
                "new_evidence_count": old_evidence_count + support_count,
            }

        summary = {
            "changed_count": int(support_count + len(diff["summary_links_to_create"]) + (1 if diff["summary_memory"]["action"] == "create" else 0) + (1 if diff["canonical_evidence_boost"] else 0)),
            "support_links_created_count": support_count,
            "summary_memory_created_count": 1 if diff["summary_memory"]["action"] == "create" else 0,
            "summary_links_created_count": len(diff["summary_links_to_create"]),
            "central_evidence_boost_count": 1 if diff["canonical_evidence_boost"] else 0,
        }

    apply_eligible = not blocking_reasons
    return {
        "proposal": proposal,
        "source_memories": [_section_memory_stub(item) for item in source_memories],
        "apply_eligible": apply_eligible,
        "blocking_reasons": blocking_reasons,
        "supported_proposal_types": sorted(_CONSOLIDATION_APPLY_SUPPORTED_TYPES),
        "candidate": candidate,
        "diff": diff,
        "summary": summary,
        "rollback": {
            "supported": True,
            "run_mode": "consolidation_apply_run",
            "preview_tool": "preview_undo_run",
            "apply_tool": "undo_run",
        },
        "unsupported_metrics": unsupported_metrics,
    }


def _canonical_json_hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_CONSOLIDATION_PREVIEW_HASH_ALGORITHM = "sha256:canonical-json:v1"


def _consolidation_apply_preview_result_payload(
    preview: dict[str, Any],
    *,
    proposal_memory_id: int,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "schema_version": "memory_consolidation_apply_preview.v1",
        "proposal_id": _consolidation_proposal_public_id(proposal_memory_id),
        "proposal": preview["proposal"],
        "source_memories": preview["source_memories"],
        "apply_eligible": preview["apply_eligible"],
        "blocking_reasons": preview["blocking_reasons"],
        "supported_proposal_types": preview["supported_proposal_types"],
        "diff": preview["diff"],
        "summary": preview["summary"],
        "rollback": preview["rollback"],
        "unsupported_metrics": preview["unsupported_metrics"],
    }


def _consolidation_apply_preview_hash(preview: dict[str, Any], *, proposal_memory_id: int) -> str:
    return _canonical_json_hash(
        _consolidation_apply_preview_result_payload(preview, proposal_memory_id=proposal_memory_id)
    )


def _consolidation_apply_rollback_preview_hash(payload: dict[str, Any]) -> str:
    return _canonical_json_hash(payload)


def _consolidation_apply_rollback_preview_hash_source(preview: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": normalize_optional_text(preview.get("schema_version")) or "memory_consolidation_apply_rollback_preview.v1",
        "run_id": int(preview.get("run_id") or 0),
        "status": normalize_optional_text(preview.get("status")) or "blocked",
        "rollback_available": bool(preview.get("rollback_available")),
        "blocking_reasons": list(preview.get("blocking_reasons") or []),
        "affected_memory_ids": list(preview.get("affected_memory_ids") or []),
        "actions": list(preview.get("actions") or []),
        "action_summary": dict(preview.get("action_summary") or {}),
        "rollbackable_action_count": int(preview.get("rollbackable_action_count") or 0),
        "existing_rollback_run_id": preview.get("existing_rollback_run_id"),
        "unsupported_metrics": list(preview.get("unsupported_metrics") or []),
    }


def _consolidation_apply_rollback_preview_hash_from_response(preview: dict[str, Any]) -> str:
    return _consolidation_apply_rollback_preview_hash(
        _consolidation_apply_rollback_preview_hash_source(preview)
    )


def _build_consolidation_apply_rollback_preview_payload(
    *,
    run: dict[str, Any],
    run_record: dict[str, Any],
    undo_preview: dict[str, Any],
) -> dict[str, Any]:
    blocking_reasons: list[str] = []
    unsupported_metrics: list[str] = []
    rollback_available = bool(run_record.get("rollback_available"))

    if str(run.get("mode") or "") != "consolidation_apply_run":
        blocking_reasons.append("run_id_is_not_consolidation_apply_run")
    if undo_preview.get("status") != "preview_completed":
        blocking_reasons.append("undo_preview_unavailable")
        unsupported_metrics.append("preview_undo_run did not return preview_completed")
    if bool(undo_preview.get("already_rolled_back")):
        blocking_reasons.append("run_already_rolled_back")
    if int(undo_preview.get("rollbackable_action_count") or 0) < 1:
        blocking_reasons.append("no_rollbackable_actions")
    if not rollback_available and "run_already_rolled_back" not in blocking_reasons:
        blocking_reasons.append("rollback_not_available")

    return {
        "schema_version": "memory_consolidation_apply_rollback_preview.v1",
        "run_id": int(run["id"]),
        "status": "ready" if not blocking_reasons else "blocked",
        "rollback_available": rollback_available,
        "blocking_reasons": blocking_reasons,
        "affected_memory_ids": list(run_record.get("affected_memory_ids") or []),
        "actions": list(undo_preview.get("rollbackable_actions") or []),
        "action_summary": dict(undo_preview.get("rollbackable_action_summary") or {}),
        "rollbackable_action_count": int(undo_preview.get("rollbackable_action_count") or 0),
        "existing_rollback_run_id": undo_preview.get("existing_rollback_run_id"),
        "unsupported_metrics": unsupported_metrics,
    }


def _consolidation_apply_preview_affected_memory_ids(preview: dict[str, Any]) -> list[int]:
    ids: set[int] = set()
    proposal = preview.get("proposal") or {}
    for item in proposal.get("source_memory_ids") or []:
        ids.add(int(item))
    diff = preview.get("diff") or {}
    for link in diff.get("support_links_to_create") or []:
        from_memory_id = link.get("from_memory_id")
        to_memory_id = link.get("to_memory_id")
        if isinstance(from_memory_id, int):
            ids.add(int(from_memory_id))
        if isinstance(to_memory_id, int):
            ids.add(int(to_memory_id))
    for link in diff.get("summary_links_to_create") or []:
        from_memory_id = link.get("from_memory_id")
        to_memory_id = link.get("to_memory_id")
        if isinstance(from_memory_id, int):
            ids.add(int(from_memory_id))
        if isinstance(to_memory_id, int):
            ids.add(int(to_memory_id))
    boost = diff.get("canonical_evidence_boost") or {}
    if isinstance(boost.get("memory_id"), int):
        ids.add(int(boost["memory_id"]))
    return sorted(ids)


def _consolidation_apply_preview_planned_actions(preview: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    diff = preview.get("diff") or {}
    for link in diff.get("support_links_to_create") or []:
        actions.append(
            {
                "action": "support_link_create",
                "from_memory_id": link.get("from_memory_id"),
                "to_memory_id": link.get("to_memory_id"),
                "relation_type": link.get("relation_type"),
                "weight": link.get("weight"),
            }
        )
    summary_memory = diff.get("summary_memory") or {}
    summary_action = normalize_optional_text(summary_memory.get("action"))
    if summary_action and summary_action != "none":
        actions.append(
            {
                "action": f"summary_memory_{summary_action}",
                "memory_id": summary_memory.get("memory_id"),
                "memory_payload": summary_memory.get("memory_payload"),
            }
        )
    for link in diff.get("summary_links_to_create") or []:
        actions.append(
            {
                "action": "summary_link_create",
                "from_memory_id": link.get("from_memory_id"),
                "to_memory_id": link.get("to_memory_id"),
                "relation_type": link.get("relation_type"),
                "weight": link.get("weight"),
            }
        )
    boost = diff.get("canonical_evidence_boost")
    if isinstance(boost, dict) and boost:
        actions.append(
            {
                "action": "canonical_evidence_boost",
                "memory_id": boost.get("memory_id"),
                "old_evidence_count": boost.get("old_evidence_count"),
                "new_evidence_count": boost.get("new_evidence_count"),
            }
        )
    return actions


def _build_consolidation_apply_preview_snapshot(
    preview: dict[str, Any],
    *,
    run_id: int,
    proposal_memory_id: int,
    stored_at: str,
    preview_hash_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preview_result = _consolidation_apply_preview_result_payload(preview, proposal_memory_id=proposal_memory_id)
    snapshot = {
        "schema_version": "consolidation_apply_preview_snapshot.v1",
        "run_id": int(run_id),
        "proposal_id": _consolidation_proposal_public_id(proposal_memory_id),
        "proposal_type": preview_result["proposal"].get("proposal_type"),
        "project_key": preview_result["proposal"].get("project_key"),
        "stored_at": stored_at,
        "preview_source": "stored_at_apply",
        "preview_hash_algorithm": _CONSOLIDATION_PREVIEW_HASH_ALGORITHM,
        "affected_memory_ids": _consolidation_apply_preview_affected_memory_ids(preview),
        "planned_actions": _consolidation_apply_preview_planned_actions(preview),
        "diff": preview_result["diff"],
        "blocking_reasons": preview_result["blocking_reasons"],
        "rollback_instruction": preview_result["rollback"],
        "preview_result": preview_result,
        "preview_hash_guard": preview_hash_guard,
    }
    snapshot["preview_hash"] = _consolidation_apply_preview_hash(preview, proposal_memory_id=proposal_memory_id)
    return snapshot


def _get_consolidation_apply_preview_snapshot_row(conn, run_id: int):
    return conn.execute(
        """
        SELECT *
        FROM memory_consolidation_apply_snapshots
        WHERE run_id = ?
        LIMIT 1
        """,
        (int(run_id),),
    ).fetchone()


def _store_consolidation_apply_preview_snapshot(
    conn,
    *,
    run_id: int,
    proposal_memory_id: int,
    snapshot: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO memory_consolidation_apply_snapshots (
            run_id,
            proposal_memory_id,
            schema_version,
            preview_source,
            preview_hash,
            snapshot_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(run_id),
            int(proposal_memory_id),
            str(snapshot["schema_version"]),
            str(snapshot["preview_source"]),
            str(snapshot["preview_hash"]),
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            str(snapshot["stored_at"]),
        ),
    )


def _build_consolidation_apply_rollback_preview_snapshot(
    preview: dict[str, Any],
    *,
    original_apply_run_id: int,
    rollback_run_id: int,
    stored_at: str,
    expected_rollback_preview_hash: str,
) -> dict[str, Any]:
    snapshot = {
        "schema_version": "consolidation_apply_rollback_preview_snapshot.v1",
        "original_apply_run_id": int(original_apply_run_id),
        "rollback_run_id": int(rollback_run_id),
        "stored_at": stored_at,
        "preview_source": "stored_at_rollback",
        "rollback_preview_hash_algorithm": _CONSOLIDATION_PREVIEW_HASH_ALGORITHM,
        "affected_memory_ids": list(preview.get("affected_memory_ids") or []),
        "actions": list(preview.get("actions") or []),
        "action_summary": dict(preview.get("action_summary") or {}),
        "rollbackable_action_count": int(preview.get("rollbackable_action_count") or 0),
        "blocking_reasons": list(preview.get("blocking_reasons") or []),
        "rollback_instruction": dict(preview.get("rollback_instruction") or {}),
        "rollback_guard": {
            "expected_rollback_preview_hash": expected_rollback_preview_hash,
            "actual_rollback_preview_hash": str(preview["rollback_preview_hash"]),
            "matched": expected_rollback_preview_hash == str(preview["rollback_preview_hash"]),
            "algorithm": _CONSOLIDATION_PREVIEW_HASH_ALGORITHM,
        },
        "rollback_preview": preview,
    }
    snapshot["rollback_preview_hash"] = str(preview["rollback_preview_hash"])
    return snapshot


def _get_consolidation_rollback_preview_snapshot_row(conn, original_apply_run_id: int):
    return conn.execute(
        """
        SELECT *
        FROM memory_consolidation_rollback_snapshots
        WHERE original_apply_run_id = ?
        LIMIT 1
        """,
        (int(original_apply_run_id),),
    ).fetchone()


def _store_consolidation_rollback_preview_snapshot(
    conn,
    *,
    original_apply_run_id: int,
    rollback_run_id: int,
    snapshot: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO memory_consolidation_rollback_snapshots (
            original_apply_run_id,
            rollback_run_id,
            schema_version,
            preview_source,
            rollback_preview_hash,
            snapshot_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(original_apply_run_id),
            int(rollback_run_id),
            str(snapshot["schema_version"]),
            str(snapshot["preview_source"]),
            str(snapshot["rollback_preview_hash"]),
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            str(snapshot["stored_at"]),
        ),
    )


def _snapshot_integrity_status_from_findings(
    findings: list[dict[str, Any]],
    unsupported_metrics: list[str],
) -> str:
    if any(str(item.get("severity")) == "error" for item in findings):
        return "error"
    if findings:
        return "warning"
    return "ok"


def _extract_consolidation_apply_proposal_id_from_value(value: Any) -> int | None:
    normalized = normalize_optional_text(value)
    if normalized is None:
        return None
    match = re.fullmatch(r"consolidation_proposal_apply:(\d+)", normalized)
    if match is None:
        return None
    return int(match.group(1))


def _collect_consolidation_apply_ints_from_payload(value: Any, interesting_keys: set[str]) -> set[int]:
    decoded = _decode_action_value(value)
    result: set[int] = set()
    if isinstance(decoded, dict):
        for key, nested in decoded.items():
            if key in interesting_keys and nested is not None:
                try:
                    result.add(int(nested))
                except (TypeError, ValueError):
                    pass
            if isinstance(nested, (dict, list)):
                result.update(_collect_consolidation_apply_ints_from_payload(nested, interesting_keys))
    elif isinstance(decoded, list):
        for item in decoded:
            result.update(_collect_consolidation_apply_ints_from_payload(item, interesting_keys))
    return result


def _consolidation_apply_action_memory_ids(action: dict[str, Any]) -> set[int]:
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
    ids.update(_collect_consolidation_apply_ints_from_payload(action.get("old_value"), interesting_keys))
    ids.update(_collect_consolidation_apply_ints_from_payload(action.get("new_value"), interesting_keys))
    return ids


def _consolidation_apply_action_link_ids(action: dict[str, Any]) -> set[int]:
    interesting_keys = {"link_id", "deleted_link_id"}
    ids: set[int] = set()
    ids.update(_collect_consolidation_apply_ints_from_payload(action.get("old_value"), interesting_keys))
    ids.update(_collect_consolidation_apply_ints_from_payload(action.get("new_value"), interesting_keys))
    return ids


def _infer_consolidation_apply_proposal_id(conn, actions: list[dict[str, Any]]) -> int | None:
    proposal_ids: list[int] = []
    seen: set[int] = set()
    for action in actions:
        for link_id in sorted(_consolidation_apply_action_link_ids(action)):
            link_row = conn.execute("SELECT origin FROM memory_links WHERE id = ?", (int(link_id),)).fetchone()
            if link_row is None:
                continue
            proposal_id = _extract_consolidation_apply_proposal_id_from_value(link_row["origin"])
            if proposal_id is None or proposal_id in seen:
                continue
            seen.add(proposal_id)
            proposal_ids.append(proposal_id)
        for memory_id in sorted(_consolidation_apply_action_memory_ids(action)):
            memory_row = conn.execute("SELECT source FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
            if memory_row is None:
                continue
            proposal_id = _extract_consolidation_apply_proposal_id_from_value(memory_row["source"])
            if proposal_id is None or proposal_id in seen:
                continue
            seen.add(proposal_id)
            proposal_ids.append(proposal_id)
    if len(proposal_ids) == 1:
        return proposal_ids[0]
    return None


def _consolidation_apply_run_record(
    conn,
    run: dict[str, Any],
    *,
    include_details: bool,
) -> dict[str, Any]:
    run_id = int(run["id"])
    action_rows = conn.execute(
        "SELECT * FROM sleep_run_actions WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    ).fetchall()
    actions = [row_to_dict(row) for row in action_rows]
    snapshot_row = _get_consolidation_apply_preview_snapshot_row(conn, run_id)
    snapshot_payload = _decode_action_value(snapshot_row["snapshot_json"]) if snapshot_row is not None else None
    rollback_snapshot_row = _get_consolidation_rollback_preview_snapshot_row(conn, run_id)
    rollback_snapshot_payload = _decode_action_value(rollback_snapshot_row["snapshot_json"]) if rollback_snapshot_row is not None else None
    proposal_memory_id = _infer_consolidation_apply_proposal_id(conn, actions)
    proposal = None
    proposal_payload = None
    unsupported_metrics: list[str] = []

    if proposal_memory_id is None:
        unsupported_metrics.append("proposal_id could not be inferred from persisted apply run artifacts")
    else:
        proposal_row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND memory_type = 'consolidation_proposal'",
            (proposal_memory_id,),
        ).fetchone()
        if proposal_row is None:
            unsupported_metrics.append(f"proposal memory:{proposal_memory_id} is not available anymore")
        else:
            proposal = _apply_effective_owner(
                conn,
                _apply_ownership_defaults(enrich_memory_dict(row_to_dict(proposal_row))),
            )
            proposal_payload = _build_consolidation_proposal_payload(
                conn,
                proposal,
                review_item=_find_consolidation_review_item(conn, proposal_memory_id),
            )

    rollback_run_id = _existing_rollback_run_id(conn, run_id)
    rollback_available = rollback_run_id is None and str(run.get("status") or "").startswith("completed")
    affected_memory_ids = sorted({memory_id for action in actions for memory_id in _consolidation_apply_action_memory_ids(action)})

    summary = (
        proposal_payload.get("summary")
        if proposal_payload is not None
        else normalize_optional_text(run.get("notes"))
        or f"Consolidation apply run {run_id}"
    )

    record = {
        "run_id": run_id,
        "proposal_id": _consolidation_proposal_public_id(proposal_memory_id) if proposal_memory_id is not None else None,
        "proposal_type": proposal_payload.get("proposal_type") if proposal_payload is not None else None,
        "project_key": normalize_optional_text(run.get("project_key")) or normalize_optional_text((proposal_payload or {}).get("project_key")),
        "status": normalize_optional_text(run.get("status")),
        "created_at": normalize_optional_text(run.get("started_at")),
        "applied_at": normalize_optional_text(run.get("finished_at")),
        "rollback_available": rollback_available,
        "rollback_run_id": rollback_run_id,
        "summary": summary,
        "preview_snapshot_status": "stored" if snapshot_payload is not None else "reconstructed",
        "preview_hash_guard_status": normalize_optional_text((snapshot_payload or {}).get("preview_hash_guard", {}).get("status")) if isinstance(snapshot_payload, dict) else None,
        "rollback_preview_snapshot_status": (
            "stored"
            if rollback_snapshot_payload is not None
            else ("missing" if rollback_run_id is not None else "not_applicable")
        ),
    }

    if not include_details:
        return record

    if snapshot_payload is not None:
        diff_payload = snapshot_payload.get("diff") or {}
        preview_saved = True
        blocking_reasons = list(snapshot_payload.get("blocking_reasons") or [])
        preview_snapshot_hash = normalize_optional_text(snapshot_payload.get("preview_hash"))
        preview_snapshot_hash_algorithm = normalize_optional_text(snapshot_payload.get("preview_hash_algorithm")) or _CONSOLIDATION_PREVIEW_HASH_ALGORITHM
        preview_hash_guard = snapshot_payload.get("preview_hash_guard") if isinstance(snapshot_payload.get("preview_hash_guard"), dict) else None
        preview_snapshot = snapshot_payload
    else:
        diff_payload = {
            "support_links_to_create": [],
            "summary_memory": {"action": "none"},
            "summary_links_to_create": [],
            "canonical_evidence_boost": None,
        }
        preview_saved = False
        blocking_reasons = []
        preview_snapshot_hash = None
        preview_snapshot_hash_algorithm = None
        preview_hash_guard = None
        preview_snapshot = None
        for action in actions:
            action_type = str(action.get("action_type") or "")
            new_value = _decode_action_value(action.get("new_value"))
            old_value = _decode_action_value(action.get("old_value"))
            if action_type == "support_link_created" and isinstance(new_value, dict):
                diff_payload["support_links_to_create"].append(
                    {
                        "from_memory_id": new_value.get("from_memory_id"),
                        "to_memory_id": new_value.get("to_memory_id"),
                        "relation_type": new_value.get("relation_type"),
                        "link_id": new_value.get("link_id"),
                    }
                )
            elif action_type == "summary_memory_created" and isinstance(new_value, dict):
                diff_payload["summary_memory"] = {
                    "action": "create",
                    "memory_id": new_value.get("memory_id"),
                    "memory_type": new_value.get("memory_type"),
                    "summary_short": new_value.get("summary_short"),
                }
            elif action_type == "summary_link_created" and isinstance(new_value, dict):
                diff_payload["summary_links_to_create"].append(
                    {
                        "from_memory_id": new_value.get("from_memory_id"),
                        "to_memory_id": new_value.get("to_memory_id"),
                        "relation_type": new_value.get("relation_type"),
                        "link_id": new_value.get("link_id"),
                    }
                )
            elif action_type == "canonical_evidence_boosted" and isinstance(new_value, dict):
                diff_payload["canonical_evidence_boost"] = {
                    "memory_id": action.get("memory_id"),
                    "old_evidence_count": old_value.get("evidence_count") if isinstance(old_value, dict) else None,
                    "new_evidence_count": new_value.get("evidence_count"),
                }

        unsupported_metrics.append("missing_stored_preview_snapshot")
        if proposal is not None:
            preview = _build_consolidation_apply_preview(
                conn,
                proposal,
                review_item=_find_consolidation_review_item(conn, proposal_memory_id),
            )
            unsupported_metrics.extend(
                item for item in (preview.get("unsupported_metrics") or []) if item not in unsupported_metrics
            )
        else:
            unsupported_metrics.append("proposal preview cannot be reconstructed without the source proposal memory")
        unsupported_metrics.append("blocking_reasons are not persisted on successful apply runs without a stored preview snapshot")

    if rollback_snapshot_payload is not None:
        rollback_preview_hash = normalize_optional_text(rollback_snapshot_payload.get("rollback_preview_hash"))
        rollback_preview_hash_algorithm = normalize_optional_text(rollback_snapshot_payload.get("rollback_preview_hash_algorithm")) or _CONSOLIDATION_PREVIEW_HASH_ALGORITHM
        rollback_guard = rollback_snapshot_payload.get("rollback_guard") if isinstance(rollback_snapshot_payload.get("rollback_guard"), dict) else None
    else:
        rollback_preview_hash = None
        rollback_preview_hash_algorithm = None
        rollback_guard = None
        if rollback_run_id is not None:
            unsupported_metrics.append("missing_stored_rollback_preview_snapshot")

    detailed = {
        **record,
        "schema_version": "memory_consolidation_apply_run.v1",
        "affected_memory_ids": affected_memory_ids,
        "diff": diff_payload,
        "preview_saved": preview_saved,
        "blocking_reasons": blocking_reasons,
        "preview_snapshot": preview_snapshot,
        "preview_snapshot_hash": preview_snapshot_hash,
        "preview_snapshot_hash_algorithm": preview_snapshot_hash_algorithm,
        "preview_hash_guard": preview_hash_guard,
        "rollback": {
            "status": "rolled_back" if rollback_run_id is not None else "not_rolled_back",
            "rollback_run_id": rollback_run_id,
            "rollback_preview_snapshot_status": record["rollback_preview_snapshot_status"],
            "rollback_preview_hash": rollback_preview_hash,
            "rollback_preview_hash_algorithm": rollback_preview_hash_algorithm,
            "guard": rollback_guard,
            "preview_snapshot": rollback_snapshot_payload,
        },
        "rollback_instruction": {
            "preview_tool": "preview_undo_run",
            "apply_tool": "undo_run",
            "run_id": run_id,
        },
        "safety": {
            "mutates_memory_entries": False,
            "apply_or_rollback_executed": False,
            "read_only": True,
        },
        "action_summary": {
            action_type: sum(1 for action in actions if action.get("action_type") == action_type)
            for action_type in sorted({str(action.get("action_type")) for action in actions})
        },
        "action_count": len(actions),
        "unsupported_metrics": unsupported_metrics,
    }
    return detailed


def _build_consolidation_proposal_payload(
    conn,
    item: dict[str, Any],
    *,
    review_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_memory_ids = _consolidation_proposal_source_ids(conn, int(item["id"]))
    status = _consolidation_proposal_status(item, review_item)
    review_note = normalize_optional_text((review_item or {}).get("review_note"))
    reviewed_by = normalize_optional_text((review_item or {}).get("reviewed_by"))
    reviewed_at = normalize_optional_text((review_item or {}).get("reviewed_at"))
    unsupported_metrics = ["suggested_patch is not represented in the current repository model for consolidation proposals"]
    return {
        "proposal_id": _consolidation_proposal_public_id(int(item["id"])),
        "proposal_type": _consolidation_proposal_type(item),
        "status": status,
        "created_at": normalize_optional_text(item.get("created_at")),
        "updated_at": normalize_optional_text((review_item or {}).get("updated_at")) or normalize_optional_text(item.get("updated_at")),
        "source_kind": "memory_entry",
        "source_ids": source_memory_ids,
        "source_memory_ids": source_memory_ids,
        "summary": normalize_optional_text(item.get("title")) or normalize_optional_text(item.get("summary_short")) or "Consolidation proposal",
        "title": normalize_optional_text(item.get("title")) or normalize_optional_text(item.get("summary_short")) or "Consolidation proposal",
        "rationale": _consolidation_proposal_rationale(item),
        "suggested_action": _consolidation_proposal_type(item),
        "suggested_patch": {},
        "suggested_patch_json": None,
        "confidence_score": item.get("confidence_score"),
        "created_by": "mara" if "mara" in (normalize_optional_text(item.get("tags")) or "") else (normalize_optional_text(item.get("source")) or "system"),
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
        "review_note": review_note,
        "review_reason": review_note,
        "project_key": normalize_optional_text(item.get("project_key")),
        "tags": _consolidation_proposal_tags(item.get("tags")),
        "target_memory_ids": [],
        "risk_level": _consolidation_proposal_risk_level(item),
        "unsupported_metrics": unsupported_metrics,
    }


def _list_consolidation_proposal_rows(
    conn,
    *,
    project_key: str | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    project_key_values, _resolved_project_key_mode, canonical_project_key = _resolve_project_key_filter(
        conn,
        project_key=normalize_optional_text(project_key),
        project_key_mode="aliases" if normalize_optional_text(project_key) else "exact",
    )
    sql = (
        "SELECT m.*, r.status AS review_status, r.reviewed_at AS review_reviewed_at, "
        "r.reviewed_by AS review_reviewed_by, r.review_note AS review_review_note, "
        "r.created_at AS review_created_at, r.updated_at AS review_updated_at "
        "FROM memories m "
        "LEFT JOIN memory_consolidation_review_items r ON r.proposal_memory_id = m.id "
        "WHERE m.archived_at IS NULL AND m.memory_type = 'consolidation_proposal'"
    )
    params: list[Any] = []
    if project_key_values:
        placeholders = ", ".join("?" for _ in project_key_values)
        sql += f" AND m.project_key IN ({placeholders})"
        params.extend(project_key_values)
    sql += " ORDER BY m.created_at DESC, m.id DESC"
    rows = conn.execute(sql, params).fetchall()
    items: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for row in rows:
        item = _apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        review_item = None
        if row["review_status"] is not None:
            review_item = {
                "proposal_memory_id": int(row["id"]),
                "status": row["review_status"],
                "reviewed_at": row["review_reviewed_at"],
                "reviewed_by": row["review_reviewed_by"],
                "review_note": row["review_review_note"],
                "created_at": row["review_created_at"],
                "updated_at": row["review_updated_at"],
            }
        item["_resolved_project_key"] = canonical_project_key or normalize_optional_text(project_key)
        items.append((item, review_item))
    return items


def _consolidation_queue_counts(conn, *, project_key_values: list[str] | None = None) -> dict[str, int]:
    params: list[Any] = []
    where_sql = "m.archived_at IS NULL AND m.memory_type = 'consolidation_proposal'"
    if project_key_values:
        placeholders = ", ".join("?" for _ in project_key_values)
        where_sql += f" AND m.project_key IN ({placeholders})"
        params.extend(project_key_values)
    rows = conn.execute(
        f"""
        SELECT
            COALESCE(
                r.status,
                CASE
                    WHEN m.memory_v2_status = 'superseded' THEN 'superseded'
                    ELSE 'pending'
                END
            ) AS proposal_status,
            COUNT(*) AS count
        FROM memories m
        LEFT JOIN memory_consolidation_review_items r ON r.proposal_memory_id = m.id
        WHERE {where_sql}
        GROUP BY proposal_status
        """,
        params,
    ).fetchall()
    counts = {"pending": 0, "approved": 0, "rejected": 0, "superseded": 0}
    for row in rows:
        counts[_normalize_consolidation_proposal_status(row["proposal_status"])] = int(row["count"] or 0)
    return counts


@mcp.tool
def get_memory_forgetting_review(
    project_key: str | None = None,
    limit: int = 8,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Return operator-focused review buckets for stale/archive/superseded hygiene."""
    conn = get_db_connection()
    try:
        if not _is_memory_v2_feature_active(conn):
            return {"status": "disabled", "reason": "feature_flag_off", "flag_key": MEMORY_V2_FLAG_KEY}
        normalized_project_key = normalize_optional_text(project_key)
        if limit < 1 or limit > 50:
            return {"status": "error", "error": "limit musi byc w zakresie 1..50"}

        project_key_values, _resolved_project_key_mode, canonical_project_key = _resolve_project_key_filter(
            conn,
            project_key=normalized_project_key,
            project_key_mode="aliases" if normalized_project_key else "exact",
        )
        base_conditions = ["archived_at IS NULL"]
        base_params: list[Any] = []
        if project_key_values:
            placeholders = ", ".join("?" for _ in project_key_values)
            base_conditions.append(f"project_key IN ({placeholders})")
            base_params.extend(project_key_values)
        base_sql = " AND ".join(base_conditions)
        now_iso = utc_now_iso()

        needs_confirmation = _fetch_memory_v2_review_rows(
            conn,
            where_sql=f"{base_sql} AND (requires_user_confirmation = 1 OR memory_v2_status = 'proposed')",
            params=base_params,
            limit=limit,
            review_reason="confirm_or_reject",
        )
        stale_candidates = _fetch_memory_v2_review_rows(
            conn,
            where_sql=f"{base_sql} AND memory_v2_status IN ('stale', 'contradicted')",
            params=base_params,
            limit=limit,
            review_reason="stale_or_contradicted",
        )
        promotion_candidates = _fetch_memory_v2_review_rows(
            conn,
            where_sql=(
                f"{base_sql} "
                "AND memory_v2_status = 'proposed' "
                "AND truth_kind IN ('fact', 'decision', 'preference') "
                "AND COALESCE(confidence_score, 0) >= 0.8 "
                "AND COALESCE(importance_score, 0) >= 0.7"
            ),
            params=base_params,
            limit=limit,
            review_reason="promote_if_confirmed",
        )
        archive_candidates = _fetch_memory_v2_review_rows(
            conn,
            where_sql=(
                f"{base_sql} "
                "AND entry_type != 'core' "
                "AND ("
                "memory_v2_status = 'superseded' "
                "OR state_code = 'superseded' "
                "OR (expired_due_at IS NOT NULL AND expired_due_at <= ?) "
                "OR (memory_v2_status = 'stale' AND review_due_at IS NOT NULL AND review_due_at <= ?)"
                ")"
            ),
            params=[*base_params, now_iso, now_iso],
            limit=limit,
            review_reason="archive_or_keep_hidden",
        )
        protected_core_count = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM memories
                WHERE {base_sql}
                  AND entry_type = 'core'
                  AND (
                    memory_v2_status = 'superseded'
                    OR (expired_due_at IS NOT NULL AND expired_due_at <= ?)
                    OR (memory_v2_status = 'stale' AND review_due_at IS NOT NULL AND review_due_at <= ?)
                  )
                """,
                [*base_params, now_iso, now_iso],
            ).fetchone()[0]
        )

        overdue_revalidation = list_overdue_revalidation_queue(
            limit=limit,
            as_of=now_iso,
            project_key=normalized_project_key,
        )
        overdue_expired = list_overdue_expired_queue(
            limit=limit,
            as_of=now_iso,
            project_key=normalized_project_key,
        )
        source_memory_ids = sorted(
            {
                int(item["id"])
                for bucket in (
                    needs_confirmation,
                    stale_candidates,
                    promotion_candidates,
                    archive_candidates,
                    overdue_revalidation.get("items") or [],
                    overdue_expired.get("items") or [],
                )
                for item in bucket
                if item.get("id") is not None
            }
        )
        result = {
            "status": "ok",
            "project_key": canonical_project_key or normalized_project_key,
            "needs_confirmation": needs_confirmation,
            "stale_candidates": stale_candidates,
            "promotion_candidates": promotion_candidates,
            "archive_candidates": archive_candidates,
            "revalidation_overdue": [_v2_review_stub(item, review_reason="revalidation_overdue") for item in (overdue_revalidation.get("items") or [])],
            "expired_overdue": [_v2_review_stub(item, review_reason="expired_overdue") for item in (overdue_expired.get("items") or [])],
            "source_memory_ids": source_memory_ids,
            "summary": {
                "needs_confirmation_count": len(needs_confirmation),
                "stale_candidates_count": len(stale_candidates),
                "promotion_candidates_count": len(promotion_candidates),
                "archive_candidates_count": len(archive_candidates),
                "revalidation_overdue_count": int(overdue_revalidation.get("count") or 0),
                "expired_overdue_count": int(overdue_expired.get("count") or 0),
                "protected_core_count": protected_core_count,
            },
            "notes": {
                "needs_confirmation": "Proposed or confirmation-required memories that need explicit operator review.",
                "stale_candidates": "Items already marked stale or contradicted; review whether they should stay visible.",
                "promotion_candidates": "High-signal proposed items that may be confirmed into durable fact/decision/preference.",
                "archive_candidates": "Reversible forgetting targets. Core memories are excluded and counted separately.",
            },
            "debug": {},
        }
        if include_debug:
            result["debug"] = {
                "as_of": now_iso,
                "feature_flag_active": True,
                "base_sql": base_sql,
                "base_params": base_params,
                "project_key_values": project_key_values,
                "canonical_project_key": canonical_project_key,
            }
        return result
    finally:
        conn.close()


@mcp.tool
def get_memory_health_report(
    project_key: str | None = None,
    limit: int = 8,
    include_debug: bool = False,
    include_consolidation_snapshot_integrity: bool = True,
    snapshot_integrity_sample_limit: int = 5,
) -> dict[str, Any]:
    """Return a read-only health report for memory v2 operator review."""
    conn = get_db_connection()
    try:
        if not _is_memory_v2_feature_active(conn):
            return {"status": "disabled", "reason": "feature_flag_off", "flag_key": MEMORY_V2_FLAG_KEY}
        normalized_project_key = normalize_optional_text(project_key)
        if limit < 1 or limit > 50:
            return {"status": "error", "error": "limit musi byc w zakresie 1..50"}
        if snapshot_integrity_sample_limit < 1 or snapshot_integrity_sample_limit > 20:
            return {"status": "error", "error": "snapshot_integrity_sample_limit musi byc w zakresie 1..20"}

        project_key_values, _resolved_project_key_mode, canonical_project_key = _resolve_project_key_filter(
            conn,
            project_key=normalized_project_key,
            project_key_mode="aliases" if normalized_project_key else "exact",
        )
        base_conditions = ["archived_at IS NULL"]
        base_params: list[Any] = []
        if project_key_values:
            placeholders = ", ".join("?" for _ in project_key_values)
            base_conditions.append(f"project_key IN ({placeholders})")
            base_params.extend(project_key_values)
        base_sql = " AND ".join(base_conditions)
        now_iso = utc_now_iso()

        total_entries = int(
            conn.execute(f"SELECT COUNT(*) FROM memories WHERE {base_sql}", base_params).fetchone()[0]
        )
        by_type_rows = conn.execute(
            f"""
            SELECT COALESCE(entry_type, 'unknown') AS label, COUNT(*) AS count
            FROM memories
            WHERE {base_sql}
            GROUP BY COALESCE(entry_type, 'unknown')
            ORDER BY count DESC, label ASC
            """,
            base_params,
        ).fetchall()
        by_status_rows = conn.execute(
            f"""
            SELECT COALESCE(memory_v2_status, 'unknown') AS label, COUNT(*) AS count
            FROM memories
            WHERE {base_sql}
            GROUP BY COALESCE(memory_v2_status, 'unknown')
            ORDER BY count DESC, label ASC
            """,
            base_params,
        ).fetchall()
        by_importance_rows = conn.execute(
            f"""
            SELECT COALESCE(importance_level, 'unknown') AS label, COUNT(*) AS count
            FROM memories
            WHERE {base_sql}
            GROUP BY COALESCE(importance_level, 'unknown')
            ORDER BY count DESC, label ASC
            """,
            base_params,
        ).fetchall()
        confidence_row = conn.execute(
            f"""
            SELECT
                SUM(CASE WHEN confidence_score IS NULL THEN 1 ELSE 0 END) AS missing_count,
                SUM(CASE WHEN confidence_score IS NOT NULL AND confidence_score < 0.7 THEN 1 ELSE 0 END) AS low_count,
                SUM(CASE WHEN confidence_score IS NOT NULL AND confidence_score >= 0.7 THEN 1 ELSE 0 END) AS ok_count
            FROM memories
            WHERE {base_sql}
            """,
            base_params,
        ).fetchone()

        conflicts_payload = get_conflict_registry(include_resolved=False)
        conflict_items = (conflicts_payload.get("items") or [])[:limit] if conflicts_payload.get("status") == "ok" else []
        consolidation_queue_counts = _consolidation_queue_counts(conn, project_key_values=project_key_values)
        stale_entries = _fetch_memory_health_rows(
            conn,
            where_sql=f"{base_sql} AND memory_v2_status IN ('stale', 'contradicted')",
            params=base_params,
            limit=limit,
            review_reason="stale_or_contradicted",
        )
        low_confidence_entries = _fetch_memory_health_rows(
            conn,
            where_sql=f"{base_sql} AND confidence_score IS NOT NULL AND confidence_score < 0.7",
            params=base_params,
            limit=limit,
            review_reason="confidence_below_0_7",
        )
        entries_missing_source_context = _fetch_memory_health_rows(
            conn,
            where_sql=f"{base_sql} AND (source_context IS NULL OR trim(source_context) = '')",
            params=base_params,
            limit=limit,
            review_reason="missing_source_context",
        )
        dreams_requiring_consolidation_review = _fetch_memory_health_rows(
            conn,
            where_sql=(
                f"{base_sql} AND ("
                "memory_type = 'consolidation_proposal' "
                "OR COALESCE(tags, '') LIKE '%consolidation-proposal%'"
                ")"
            ),
            params=base_params,
            limit=limit,
            review_reason="consolidation_review_required",
        )
        entries_requiring_user_confirmation = _fetch_memory_health_rows(
            conn,
            where_sql=f"{base_sql} AND (requires_user_confirmation = 1 OR memory_v2_status = 'proposed')",
            params=base_params,
            limit=limit,
            review_reason="confirm_or_reject",
        )

        recommended_actions: list[str] = []
        if stale_entries:
            recommended_actions.append("Review stale or contradicted entries and decide whether to refresh, archive, or keep hidden.")
        if low_confidence_entries:
            recommended_actions.append("Review low-confidence entries before using them as durable facts or decisions.")
        if entries_missing_source_context:
            recommended_actions.append("Backfill source_context for entries that would be hard to audit later.")
        if dreams_requiring_consolidation_review:
            recommended_actions.append("Treat Mara consolidation proposals as review-only until an operator confirms a follow-up action.")
        if entries_requiring_user_confirmation:
            recommended_actions.append("Resolve confirmation-required or proposed entries explicitly instead of letting them linger.")
        if conflict_items:
            recommended_actions.append("Inspect open conflict clusters before promoting or merging related memories.")
        if int(consolidation_queue_counts.get("pending") or 0) > 0:
            recommended_actions.append("Review pending consolidation proposals in the consolidation queue before any manual merge follow-up.")

        unsupported_metrics = [
            "confidence has no native enum in schema; by_confidence uses score buckets derived from confidence_score and the 0.7 quality threshold",
        ]

        consolidation_snapshot_integrity = {
            "available": False,
            "status": "unsupported",
            "summary": {},
            "issue_counts": {},
            "sample_issues": [],
            "unsupported_metrics": [],
            "recommended_actions": [],
        }
        if include_consolidation_snapshot_integrity:
            integrity_result = get_memory_consolidation_snapshot_integrity_report(
                project_key=canonical_project_key or normalized_project_key,
                include_debug=False,
            )
            if integrity_result.get("status") in {"ok", "warning", "error"}:
                integrity_summary = dict(integrity_result.get("summary") or {})
                issue_counts = {
                    "orphan_snapshots": int(integrity_summary.get("orphan_snapshots") or 0),
                    "duplicate_snapshot_runs": int(integrity_summary.get("duplicate_snapshot_runs") or 0),
                    "malformed_snapshots": int(integrity_summary.get("malformed_snapshots") or 0),
                    "proposal_id_mismatches": int(integrity_summary.get("proposal_id_mismatches") or 0),
                    "proposal_type_mismatches": int(integrity_summary.get("proposal_type_mismatches") or 0),
                    "preview_hash_mismatches": int(integrity_summary.get("preview_hash_mismatches") or 0),
                    "runs_missing_stored_snapshot": int(integrity_summary.get("runs_missing_stored_snapshot") or 0),
                    "legacy_reconstructed_only_runs": int(integrity_summary.get("legacy_reconstructed_only_runs") or 0),
                    "rollback_orphan_snapshots": int(integrity_summary.get("rollback_orphan_snapshots") or 0),
                    "rollback_duplicate_snapshot_runs": int(integrity_summary.get("rollback_duplicate_snapshot_runs") or 0),
                    "rollback_duplicate_snapshot_rollback_runs": int(integrity_summary.get("rollback_duplicate_snapshot_rollback_runs") or 0),
                    "rollback_malformed_snapshots": int(integrity_summary.get("rollback_malformed_snapshots") or 0),
                    "rollback_preview_hash_mismatches": int(integrity_summary.get("rollback_preview_hash_mismatches") or 0),
                    "rollback_guard_mismatches": int(integrity_summary.get("rollback_guard_mismatches") or 0),
                    "rollback_wrong_run_type_links": int(integrity_summary.get("rollback_wrong_run_type_links") or 0),
                    "rollback_runs_missing_stored_snapshot": int(integrity_summary.get("rollback_runs_missing_stored_snapshot") or 0),
                    "legacy_rollback_runs_without_snapshot": int(integrity_summary.get("legacy_rollback_runs_without_snapshot") or 0),
                }
                integrity_unsupported_metrics = list(integrity_result.get("unsupported_metrics") or [])
                integrity_status = str(integrity_result.get("status") or "warning")
                if integrity_status == "ok" and integrity_unsupported_metrics:
                    integrity_status = "warning"
                consolidation_snapshot_integrity = {
                    "available": True,
                    "status": integrity_status,
                    "summary": integrity_summary,
                    "issue_counts": {
                        **issue_counts,
                        "total_findings": len(integrity_result.get("findings") or []),
                        "sampled_findings": min(
                            len(integrity_result.get("findings") or []),
                            int(snapshot_integrity_sample_limit),
                        ),
                    },
                    "sample_issues": list(integrity_result.get("findings") or [])[: int(snapshot_integrity_sample_limit)],
                    "unsupported_metrics": integrity_unsupported_metrics,
                    "recommended_actions": list(integrity_result.get("recommended_actions") or []),
                }
                if integrity_status in {"warning", "error"}:
                    recommended_actions.append(
                        "Review consolidation snapshot integrity findings from the health report before trusting apply or rollback audit coverage."
                    )
            else:
                consolidation_snapshot_integrity = {
                    "available": False,
                    "status": "unsupported",
                    "summary": {},
                    "issue_counts": {},
                    "sample_issues": [],
                    "unsupported_metrics": [normalize_optional_text(integrity_result.get("error")) or "snapshot integrity report could not be computed safely"],
                    "recommended_actions": [],
                }
                unsupported_metrics.append("consolidation snapshot integrity report is unavailable from the current health report context")

        source_memory_ids = sorted(
            {
                int(item["id"])
                for bucket in (
                    stale_entries,
                    low_confidence_entries,
                    entries_missing_source_context,
                    dreams_requiring_consolidation_review,
                    entries_requiring_user_confirmation,
                )
                for item in bucket
                if item.get("id") is not None
            }
        )
        result = {
            "status": "ok",
            "project_key": canonical_project_key or normalized_project_key,
            "schema_version": "memory_health_report.v1",
            "summary": {
                "total_entries": total_entries,
                "by_type": {str(row["label"]): int(row["count"]) for row in by_type_rows},
                "by_status": {str(row["label"]): int(row["count"]) for row in by_status_rows},
                "by_confidence": {
                    "low_below_0_7": int(confidence_row["low_count"] or 0),
                    "at_or_above_0_7": int(confidence_row["ok_count"] or 0),
                    "missing": int(confidence_row["missing_count"] or 0),
                },
                "by_importance": {str(row["label"]): int(row["count"]) for row in by_importance_rows},
                "consolidation_queue": consolidation_queue_counts,
            },
            "operator_review": {
                "conflicts": conflict_items,
                "stale_entries": stale_entries,
                "low_confidence_entries": low_confidence_entries,
                "entries_missing_source_context": entries_missing_source_context,
                "dreams_requiring_consolidation_review": dreams_requiring_consolidation_review,
                "entries_requiring_user_confirmation": entries_requiring_user_confirmation,
            },
            "recommended_actions": recommended_actions,
            "unsupported_metrics": unsupported_metrics,
            "source_memory_ids": source_memory_ids,
            "consolidation_snapshot_integrity": consolidation_snapshot_integrity,
            "debug": {},
        }
        if include_debug:
            result["debug"] = {
                "as_of": now_iso,
                "feature_flag_active": True,
                "base_sql": base_sql,
                "base_params": base_params,
                "project_key_values": project_key_values,
                "canonical_project_key": canonical_project_key,
                "confidence_thresholds": {
                    "low_confidence": 0.7,
                    "source": "app/memory/quality.py",
                },
                "conflict_registry_count": int(conflicts_payload.get("count") or 0) if isinstance(conflicts_payload, dict) else 0,
                "consolidation_snapshot_integrity": {
                    "included": bool(include_consolidation_snapshot_integrity),
                    "sample_limit": int(snapshot_integrity_sample_limit),
                },
            }
        return result
    finally:
        conn.close()


@mcp.tool
def get_memory_current_state(
    memory_id: int,
    include_history: bool = True,
    include_debug: bool = False,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_memory_current_state_payload(
            conn,
            memory_id=int(memory_id),
            include_history=bool(include_history),
            include_debug=bool(include_debug),
        )
    finally:
        conn.close()


def get_memory_current_state_inventory(
    project_key: str | None = None,
    limit: int = 200,
    include_debug: bool = False,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_memory_current_state_inventory_payload(
            conn,
            project_key=normalize_optional_text(project_key),
            limit=int(limit),
            include_debug=bool(include_debug),
        )
    finally:
        conn.close()


def get_memory_self_healing_status() -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return memory_self_healing.get_self_healing_status(conn)
    finally:
        conn.close()


def get_memory_self_healing_issue(issue_id: int, include_content: bool = True) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return memory_self_healing.get_self_healing_issue(
            conn, issue_id=int(issue_id), include_content=bool(include_content)
        )
    finally:
        conn.close()


def propose_memory_self_healing_resolution(
    issue_id: int,
    selected_memory_id: int,
    confidence: float,
    rationale: str,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = memory_self_healing.propose_self_healing_resolution(
            conn,
            issue_id=int(issue_id),
            selected_memory_id=int(selected_memory_id),
            confidence=float(confidence),
            rationale=normalize_required_text(rationale, "rationale"),
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def confirm_memory_self_healing_resolution(issue_id: int, approve: bool) -> dict[str, Any]:
    if not bool(approve):
        conn = get_db_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = memory_self_healing.confirm_self_healing_resolution(
                conn, issue_id=int(issue_id), approve=False, insert_event=insert_memory_event
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    from mapi.maintenance import _stamp, _verified_backup

    probe_conn = get_db_connection()
    try:
        database_row = probe_conn.execute("PRAGMA database_list").fetchone()
        db_path = Path(str(database_row[2])).resolve()
    finally:
        probe_conn.close()
    backup_dir = Path(os.environ.get("MAPI_BACKUP_DIR") or (db_path.parent.parent / "backups")).resolve()
    backup = _verified_backup(db_path, backup_dir, stamp="user-self-healing-" + _stamp())
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = memory_self_healing.confirm_self_healing_resolution(
            conn, issue_id=int(issue_id), approve=True, insert_event=insert_memory_event
        )
        queue_refresh = memory_self_healing.scan_self_healing_issues(conn)
        current_inventory = get_memory_current_state_inventory_payload(
            conn, project_key=None, limit=1000, include_debug=False
        )
        if int((current_inventory.get("summary") or {}).get("critical_issue_count") or 0) > 0:
            raise ValueError("self_healing_post_repair_current_state_failed")
        integrity = get_memory_lifecycle_integrity_report_payload(
            conn,
            memory_id=None,
            project_key=None,
            scope_code=None,
            include_archived=True,
            limit=500,
            sample_limit=100,
            include_debug=False,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
        if int((integrity.get("summary") or {}).get("critical_issues") or 0) > 0:
            raise ValueError("self_healing_post_repair_integrity_failed")
        conn.commit()
        return {
            **result,
            "backup": backup,
            "queue_refresh": queue_refresh,
            "post_repair_current_state": current_inventory,
            "post_repair_integrity": integrity,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_memory_lifecycle_integrity_report(
    memory_id: int | None = None,
    project_key: str | None = None,
    scope_code: str | None = None,
    include_archived: bool = True,
    sample_limit: int = 10,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Return a read-only lifecycle integrity report for Memory v3 lineage and supersession contracts."""
    conn = get_db_connection()
    try:
        return get_memory_lifecycle_integrity_report_payload(
            conn,
            memory_id=memory_id,
            project_key=project_key,
            scope_code=scope_code,
            include_archived=include_archived,
            sample_limit=sample_limit,
            include_debug=include_debug,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_memory_lifecycle_remediation_inventory(
    plan_version: str = LIFECYCLE_REMEDIATION_PLAN_VERSION,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Inventory the allowlisted legacy lineage remediation plan without writes."""
    conn = get_db_connection()
    try:
        return get_memory_lifecycle_remediation_inventory_payload(
            conn,
            plan_version=plan_version,
            include_debug=include_debug,
        )
    finally:
        conn.close()


@mcp.tool
def get_memory_pointer_lifecycle_remediation_inventory(
    plan_version: str = POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Inventory global pointer-only lifecycle lineages without writes."""
    conn = get_db_connection()
    try:
        return get_memory_pointer_lifecycle_remediation_inventory_payload(
            conn,
            plan_version=plan_version,
            include_debug=include_debug,
        )
    finally:
        conn.close()


@mcp.tool
def preview_memory_pointer_lifecycle_remediation(
    plan_version: str = POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Preview guarded pointer-only lifecycle canonicalization without writes."""
    conn = get_db_connection()
    try:
        return preview_memory_pointer_lifecycle_remediation_payload(
            conn,
            plan_version=plan_version,
            include_debug=include_debug,
        )
    finally:
        conn.close()


@mcp.tool
def preview_memory_pointer_lifecycle_remediation_execution(
    plan_version: str = POINTER_LIFECYCLE_REMEDIATION_PLAN_VERSION,
    execution_policy_version: str = POINTER_LIFECYCLE_EXECUTION_POLICY_VERSION,
    execution_scope: str = "all_safe",
    approved_execution_manifest_json: str | None = None,
    expected_execution_manifest_fingerprint: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Build or revalidate a frozen pointer-lifecycle execution design without writes."""
    conn = get_db_connection()
    try:
        return preview_memory_pointer_lifecycle_remediation_execution_payload(
            conn,
            plan_version=plan_version,
            execution_policy_version=execution_policy_version,
            execution_scope=execution_scope,
            approved_execution_manifest_json=approved_execution_manifest_json,
            expected_execution_manifest_fingerprint=expected_execution_manifest_fingerprint,
            include_debug=include_debug,
        )
    finally:
        conn.close()


@mcp.tool
def apply_memory_pointer_lifecycle_remediation_execution(
    plan_version: str,
    execution_policy_version: str,
    approved_execution_manifest_json: str,
    expected_execution_manifest_fingerprint: str,
    expected_execution_preview_hash: str,
    approved_protected_component_ids_json: str,
    applied_by: str,
    reason: str,
    confirm_data_repair: bool = False,
    confirm_protected: bool = False,
) -> dict[str, Any]:
    """Execute an explicitly approved frozen pointer-lifecycle remediation manifest."""
    conn = get_db_connection()
    try:
        return apply_memory_pointer_lifecycle_remediation_execution_payload(
            conn, plan_version=plan_version, execution_policy_version=execution_policy_version,
            approved_execution_manifest_json=approved_execution_manifest_json,
            expected_execution_manifest_fingerprint=expected_execution_manifest_fingerprint,
            expected_execution_preview_hash=expected_execution_preview_hash,
            approved_protected_component_ids_json=approved_protected_component_ids_json,
            applied_by=applied_by, reason=reason, confirm_data_repair=confirm_data_repair,
            confirm_protected=confirm_protected, backups_root=runtime_data_dir() / "backups",
            utc_now_iso=utc_now_iso, normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text, row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict, insert_memory_event=insert_memory_event,
        )
    finally:
        conn.close()


@mcp.tool
def get_memory_pointer_lifecycle_remediation_execution_run(
    run_id: int, include_debug: bool = False,
) -> dict[str, Any]:
    """Inspect one pointer-lifecycle remediation run without writes."""
    conn = get_db_connection()
    try:
        return get_memory_pointer_lifecycle_remediation_execution_run_payload(
            conn, run_id=run_id, include_debug=include_debug, row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
    finally:
        conn.close()


@mcp.tool
def preview_memory_pointer_lifecycle_remediation_execution_rollback(
    run_id: int, include_debug: bool = False,
) -> dict[str, Any]:
    """Preview exact snapshot-based rollback of a pointer-lifecycle remediation run."""
    conn = get_db_connection()
    try:
        return preview_memory_pointer_lifecycle_remediation_execution_rollback_payload(
            conn, run_id=run_id, include_debug=include_debug, row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
    finally:
        conn.close()


@mcp.tool
def rollback_memory_pointer_lifecycle_remediation_execution(
    run_id: int,
    expected_rollback_preview_hash: str,
    rolled_back_by: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Execute an exact guarded rollback of a pointer-lifecycle remediation run."""
    conn = get_db_connection()
    try:
        return rollback_memory_pointer_lifecycle_remediation_execution_payload(
            conn, run_id=run_id, expected_rollback_preview_hash=expected_rollback_preview_hash,
            rolled_back_by=rolled_back_by, notes=notes, utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text, row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            insert_memory_event=insert_memory_event,
        )
    finally:
        conn.close()


@mcp.tool
def preview_memory_lifecycle_remediation(
    plan_version: str = LIFECYCLE_REMEDIATION_PLAN_VERSION,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Preview the allowlisted legacy lineage remediation plan without writes."""
    conn = get_db_connection()
    try:
        return preview_memory_lifecycle_remediation_payload(
            conn,
            plan_version=plan_version,
            include_debug=include_debug,
        )
    finally:
        conn.close()


@mcp.tool
def apply_memory_lifecycle_remediation(
    plan_version: str,
    expected_preview_hash: str,
    expected_candidate_set_fingerprint: str,
    applied_by: str,
    reason: str,
    confirm_data_repair: bool = False,
) -> dict[str, Any]:
    """Execute the guarded legacy lineage remediation after explicit confirmation."""
    conn = get_db_connection()
    try:
        return apply_memory_lifecycle_remediation_payload(
            conn,
            plan_version=plan_version,
            expected_preview_hash=expected_preview_hash,
            expected_candidate_set_fingerprint=expected_candidate_set_fingerprint,
            applied_by=applied_by,
            reason=reason,
            confirm_data_repair=confirm_data_repair,
            backups_root=runtime_data_dir() / "backups",
            utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
            insert_memory_event=insert_memory_event,
        )
    finally:
        conn.close()


@mcp.tool
def get_memory_lifecycle_remediation_run(run_id: int, include_debug: bool = False) -> dict[str, Any]:
    """Inspect one legacy lifecycle remediation run without writes."""
    conn = get_db_connection()
    try:
        return get_memory_lifecycle_remediation_run_payload(
            conn,
            run_id=run_id,
            include_debug=include_debug,
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def preview_memory_lifecycle_remediation_rollback(
    run_id: int,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Preview snapshot-based rollback for a legacy remediation run."""
    conn = get_db_connection()
    try:
        return preview_memory_lifecycle_remediation_rollback_payload(
            conn,
            run_id=run_id,
            include_debug=include_debug,
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def rollback_memory_lifecycle_remediation(
    run_id: int,
    expected_rollback_preview_hash: str,
    rolled_back_by: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Execute guarded snapshot-based rollback for a legacy remediation run."""
    conn = get_db_connection()
    try:
        return rollback_memory_lifecycle_remediation_payload(
            conn,
            run_id=run_id,
            expected_rollback_preview_hash=expected_rollback_preview_hash,
            rolled_back_by=rolled_back_by,
            notes=notes,
            utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
            insert_memory_event=insert_memory_event,
        )
    finally:
        conn.close()


@mcp.tool
def list_memory_consolidation_proposals(
    status: str | None = "pending",
    proposal_type: str | None = None,
    project_key: str | None = None,
    limit: int = 50,
    include_rejected: bool = False,
) -> dict[str, Any]:
    """List consolidation proposals stored as proposal memories with durable review-state."""
    if limit < 1 or limit > 200:
        return {"status": "error", "error": "limit musi byc w zakresie 1..200"}

    normalized_status = _normalize_consolidation_proposal_status(status)
    normalized_type = normalize_optional_text(proposal_type)
    normalized_project_key = normalize_optional_text(project_key)
    conn = get_db_connection()
    try:
        rows = _list_consolidation_proposal_rows(conn, project_key=normalized_project_key)
        proposals = [
            _build_consolidation_proposal_payload(conn, item, review_item=review_item)
            for item, review_item in rows
        ]
        if normalized_type is not None:
            proposals = [item for item in proposals if item.get("proposal_type") == normalized_type]
        if not include_rejected and normalize_optional_text(status) is None:
            proposals = [item for item in proposals if item.get("status") != "rejected"]
        if normalize_optional_text(status) is not None:
            proposals = [item for item in proposals if item.get("status") == normalized_status]

        proposals.sort(
            key=lambda item: (
                0 if item.get("status") == "pending" else 1,
                -float(item.get("confidence_score") or 0.0),
                -int(_normalize_consolidation_proposal_id(str(item.get("proposal_id") or "")) or 0),
            ),
            reverse=False,
        )
        proposals = proposals[: int(limit)]

        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for item in proposals:
            by_status[str(item.get("status"))] = by_status.get(str(item.get("status")), 0) + 1
            by_type[str(item.get("proposal_type"))] = by_type.get(str(item.get("proposal_type")), 0) + 1

        return {
            "status": "ok",
            "schema_version": "memory_consolidation_queue.v1",
            "filters": {
                "status": normalized_status if normalize_optional_text(status) is not None else None,
                "proposal_type": normalized_type,
                "project_key": normalized_project_key,
                "limit": int(limit),
                "include_rejected": bool(include_rejected),
            },
            "summary": {
                "total_returned": len(proposals),
                "by_status": by_status,
                "by_type": by_type,
            },
            "proposals": proposals,
            "unsupported_metrics": [],
        }
    finally:
        conn.close()


@mcp.tool
def get_memory_consolidation_proposal(proposal_id: str) -> dict[str, Any]:
    """Return one consolidation proposal with linked source memory previews."""
    normalized_id = _normalize_consolidation_proposal_id(proposal_id)
    if normalized_id is None:
        return {
            "status": "error",
            "schema_version": "memory_consolidation_proposal.v1",
            "error": "proposal_id musi wskazywac istniejace proposal memory, np. memory:123",
        }

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND archived_at IS NULL AND memory_type = 'consolidation_proposal'",
            (normalized_id,),
        ).fetchone()
        if row is None:
            return {
                "status": "not_found",
                "schema_version": "memory_consolidation_proposal.v1",
                "proposal_id": _consolidation_proposal_public_id(normalized_id),
                "error": "proposal_not_found",
            }
        item = _apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        review_item = _find_consolidation_review_item(conn, normalized_id)
        proposal = _build_consolidation_proposal_payload(conn, item, review_item=review_item)
        source_memories = []
        for source_memory_id in proposal["source_memory_ids"]:
            source_row = conn.execute("SELECT * FROM memories WHERE id = ? AND archived_at IS NULL", (int(source_memory_id),)).fetchone()
            if source_row is None:
                continue
            source_memories.append(
                _section_memory_stub(
                    _apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(source_row))))
                )
            )
        return {
            "status": "ok",
            "schema_version": "memory_consolidation_proposal.v1",
            "proposal": proposal,
            "source_memories": source_memories,
            "target_memories": [],
            "safety": {
                "mutates_memory_entries": False,
                "approval_applies_patch": False,
            },
            "unsupported_metrics": proposal.get("unsupported_metrics") or [],
        }
    finally:
        conn.close()


@mcp.tool
def preview_apply_memory_consolidation_proposal(proposal_id: str) -> dict[str, Any]:
    """Preview a rollback-safe apply plan for an approved consolidation proposal."""
    normalized_id = _normalize_consolidation_proposal_id(proposal_id)
    if normalized_id is None:
        return {
            "status": "error",
            "schema_version": "memory_consolidation_apply_preview.v1",
            "error": "proposal_id is invalid",
        }

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND archived_at IS NULL AND memory_type = 'consolidation_proposal'",
            (normalized_id,),
        ).fetchone()
        if row is None:
            return {
                "status": "not_found",
                "schema_version": "memory_consolidation_apply_preview.v1",
                "proposal_id": _consolidation_proposal_public_id(normalized_id),
                "error": "proposal_not_found",
            }
        item = _apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        review_item = _find_consolidation_review_item(conn, normalized_id)
        preview = _build_consolidation_apply_preview(conn, item, review_item=review_item)
        preview_hash = _consolidation_apply_preview_hash(preview, proposal_memory_id=normalized_id)
        return {
            "status": "ok",
            "schema_version": "memory_consolidation_apply_preview.v1",
            "proposal_id": _consolidation_proposal_public_id(normalized_id),
            "proposal": preview["proposal"],
            "source_memories": preview["source_memories"],
            "apply_eligible": preview["apply_eligible"],
            "blocking_reasons": preview["blocking_reasons"],
            "supported_proposal_types": preview["supported_proposal_types"],
            "diff": preview["diff"],
            "summary": preview["summary"],
            "rollback": preview["rollback"],
            "preview_hash": preview_hash,
            "preview_hash_algorithm": _CONSOLIDATION_PREVIEW_HASH_ALGORITHM,
            "apply_guard": {
                "required": True,
                "expected_parameter": "expected_preview_hash",
                "preview_hash": preview_hash,
                "preview_hash_algorithm": _CONSOLIDATION_PREVIEW_HASH_ALGORITHM,
                "expected_preview_hash_field": "expected_preview_hash",
                "instruction": "Pass preview_hash as expected_preview_hash when applying this preview.",
            },
            "unsupported_metrics": preview["unsupported_metrics"],
        }
    finally:
        conn.close()


@mcp.tool
def approve_memory_consolidation_proposal(
    proposal_id: str,
    review_note: str | None = None,
    reviewed_by: str | None = "operator",
) -> dict[str, Any]:
    """Approve a consolidation proposal by changing review-state only."""
    normalized_id = _normalize_consolidation_proposal_id(proposal_id)
    if normalized_id is None:
        return {"status": "error", "schema_version": "memory_consolidation_review.v1", "error": "proposal_id is invalid"}
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND archived_at IS NULL AND memory_type = 'consolidation_proposal'",
            (normalized_id,),
        ).fetchone()
        if row is None:
            return {
                "status": "not_found",
                "schema_version": "memory_consolidation_review.v1",
                "proposal_id": _consolidation_proposal_public_id(normalized_id),
                "error": "proposal_not_found",
            }
        review_item = _get_or_create_consolidation_review_item(conn, normalized_id)
        old_status = _normalize_consolidation_proposal_status(review_item.get("status"))
        if old_status == "approved":
            return {
                "status": "ok",
                "schema_version": "memory_consolidation_review.v1",
                "proposal_id": _consolidation_proposal_public_id(normalized_id),
                "old_status": "approved",
                "new_status": "approved",
                "already_in_target_status": True,
                "reviewed_at": normalize_optional_text(review_item.get("reviewed_at")),
                "reviewed_by": normalize_optional_text(review_item.get("reviewed_by")),
                "review_note": normalize_optional_text(review_item.get("review_note")),
                "mutated_memory_entries": False,
                "applied_patch": False,
            }
        if old_status == "rejected":
            return {
                "status": "error",
                "schema_version": "memory_consolidation_review.v1",
                "proposal_id": _consolidation_proposal_public_id(normalized_id),
                "error": "rejected_proposal_cannot_be_approved_without_reopen_semantics",
            }

        now_iso = utc_now_iso()
        normalized_reviewed_by = normalize_optional_text(reviewed_by) or "operator"
        normalized_review_note = normalize_optional_text(review_note)
        conn.execute(
            """
            UPDATE memory_consolidation_review_items
            SET status = 'approved',
                reviewed_at = ?,
                reviewed_by = ?,
                review_note = ?,
                updated_at = ?
            WHERE proposal_memory_id = ?
            """,
            (now_iso, normalized_reviewed_by, normalized_review_note, now_iso, normalized_id),
        )
        conn.commit()
        return {
            "status": "ok",
            "schema_version": "memory_consolidation_review.v1",
            "proposal_id": _consolidation_proposal_public_id(normalized_id),
            "old_status": old_status,
            "new_status": "approved",
            "already_in_target_status": False,
            "reviewed_at": now_iso,
            "reviewed_by": normalized_reviewed_by,
            "review_note": normalized_review_note,
            "mutated_memory_entries": False,
            "applied_patch": False,
        }
    finally:
        conn.close()


@mcp.tool
def apply_approved_memory_consolidation_proposal(
    proposal_id: str,
    notes: str | None = None,
    applied_by: str | None = "operator",
    expected_preview_hash: str | None = None,
) -> dict[str, Any]:
    """Apply an approved merge_duplicates proposal as a rollback-safe consolidation run."""
    normalized_id = _normalize_consolidation_proposal_id(proposal_id)
    if normalized_id is None:
        return {"status": "error", "schema_version": "memory_consolidation_apply.v1", "error": "proposal_id is invalid"}

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND archived_at IS NULL AND memory_type = 'consolidation_proposal'",
            (normalized_id,),
        ).fetchone()
        if row is None:
            return {
                "status": "not_found",
                "schema_version": "memory_consolidation_apply.v1",
                "proposal_id": _consolidation_proposal_public_id(normalized_id),
                "error": "proposal_not_found",
            }
        item = _apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
        review_item = _find_consolidation_review_item(conn, normalized_id)
        preview = _build_consolidation_apply_preview(conn, item, review_item=review_item)
        current_preview_hash = _consolidation_apply_preview_hash(preview, proposal_memory_id=normalized_id)
        normalized_expected_preview_hash = normalize_optional_text(expected_preview_hash)
        if not preview["apply_eligible"]:
            return {
                "status": "blocked",
                "schema_version": "memory_consolidation_apply.v1",
                "proposal_id": _consolidation_proposal_public_id(normalized_id),
                "blocking_reasons": preview["blocking_reasons"],
                "unsupported_metrics": preview["unsupported_metrics"],
                "apply_eligible": False,
            }
        if normalized_expected_preview_hash is None:
            return {
                "status": "blocked",
                "schema_version": "memory_consolidation_apply.v1",
                "proposal_id": _consolidation_proposal_public_id(normalized_id),
                "blocking_reasons": ["missing_expected_preview_hash"],
                "unsupported_metrics": preview["unsupported_metrics"],
                "apply_eligible": False,
                "guard_status": "missing_expected_hash_blocked",
                "mutated": False,
                "rollback_created": False,
                "apply_run_created": False,
                "rollback_available": False,
                "preview_hash_algorithm": _CONSOLIDATION_PREVIEW_HASH_ALGORITHM,
                "current_preview_hash": current_preview_hash,
                "recommended_actions": [
                    "Call preview_apply_memory_consolidation_proposal(...) and pass preview_hash as expected_preview_hash."
                ],
            }
        if normalized_expected_preview_hash is not None and normalized_expected_preview_hash != current_preview_hash:
            return {
                "status": "blocked",
                "schema_version": "memory_consolidation_apply.v1",
                "proposal_id": _consolidation_proposal_public_id(normalized_id),
                "blocking_reasons": ["expected_preview_hash_mismatch"],
                "unsupported_metrics": preview["unsupported_metrics"],
                "apply_eligible": False,
                "applied": False,
                "guard_status": "preview_hash_mismatch_blocked",
                "mutated": False,
                "rollback_created": False,
                "apply_run_created": False,
                "expected_preview_hash": normalized_expected_preview_hash,
                "current_preview_hash": current_preview_hash,
                "preview_hash_algorithm": _CONSOLIDATION_PREVIEW_HASH_ALGORITHM,
                "rollback_available": False,
                "rollback_instruction": "No rollback is needed because apply was blocked before mutation.",
            }

        candidate = preview["candidate"]
        proposal = preview["proposal"]
        unsupported_metrics = list(preview["unsupported_metrics"] or [])
        preview_hash_guard = {
            "status": "matched",
            "expected_preview_hash": normalized_expected_preview_hash,
            "actual_preview_hash": current_preview_hash,
            "algorithm": _CONSOLIDATION_PREVIEW_HASH_ALGORITHM,
        }
        central_id = int(candidate["central_memory_id"])
        normalized_notes = normalize_optional_text(notes) or f"apply_consolidation_proposal_{normalized_id}"
        run_id = create_sleep_run(
            conn,
            mode="consolidation_apply_run",
            freedom_level=0,
            notes=normalized_notes,
            project_key=normalize_optional_text(proposal.get("project_key")),
        )
        stored_at = utc_now_iso()
        snapshot_payload = _build_consolidation_apply_preview_snapshot(
            preview,
            run_id=run_id,
            proposal_memory_id=normalized_id,
            stored_at=stored_at,
            preview_hash_guard=preview_hash_guard,
        )
        _store_consolidation_apply_preview_snapshot(
            conn,
            run_id=run_id,
            proposal_memory_id=normalized_id,
            snapshot=snapshot_payload,
        )

        support_links_created: list[dict[str, Any]] = []
        summary_links_created: list[dict[str, Any]] = []
        created_summary_memories: list[dict[str, Any]] = []
        central_evidence_boosted: list[dict[str, Any]] = []

        support_weight = float(candidate.get("average_gravity") or 0.5) or 0.5
        for member_id in candidate["supporting_memory_ids"]:
            if consolidation_logic.support_link_exists(conn, int(member_id), central_id):
                continue
            link = _create_link(
                conn,
                int(member_id),
                central_id,
                "supports",
                float(support_weight),
                f"consolidation_proposal_apply:{normalized_id}",
            )
            support_links_created.append(link)
            add_sleep_action(
                conn,
                run_id,
                "support_link_created",
                int(member_id),
                None,
                {
                    "link_id": int(link["id"]),
                    "from_memory_id": int(member_id),
                    "to_memory_id": central_id,
                    "relation_type": "supports",
                },
                "proposal_apply_support_link",
            )

        summary_memory_id = candidate.get("existing_summary_memory_id") if bool(candidate.get("reusable_summary_exact_match")) else None
        if summary_memory_id is None:
            proposed_summary = candidate["proposed_summary_memory"]
            created_summary = _insert_memory(
                conn,
                content=str(proposed_summary["content"]),
                memory_type=str(proposed_summary["memory_type"]),
                summary_short=proposed_summary.get("summary_short"),
                source=f"consolidation_proposal_apply:{normalized_id}",
                importance_score=float(proposed_summary.get("importance_score") or 0.5),
                confidence_score=float(proposed_summary.get("confidence_score") or 0.5),
                tags=proposed_summary.get("tags"),
                project_key=normalize_optional_text(proposal.get("project_key")),
                title=normalize_optional_text(proposal.get("title")) or "Applied consolidation summary",
            )
            summary_memory_id = int(created_summary["id"])
            created_summary_memories.append(created_summary)
            add_sleep_action(
                conn,
                run_id,
                "summary_memory_created",
                summary_memory_id,
                None,
                {
                    "memory_id": summary_memory_id,
                    "memory_type": created_summary["memory_type"],
                    "summary_short": created_summary.get("summary_short"),
                },
                "proposal_apply_summary_memory_created",
            )

        if not consolidation_logic.summary_link_exists(conn, int(summary_memory_id), central_id, "summarizes"):
            link = _create_link(
                conn,
                int(summary_memory_id),
                central_id,
                "summarizes",
                1.0,
                f"consolidation_proposal_apply:{normalized_id}",
            )
            summary_links_created.append(link)
            add_sleep_action(
                conn,
                run_id,
                "summary_link_created",
                int(summary_memory_id),
                None,
                {
                    "link_id": int(link["id"]),
                    "from_memory_id": int(summary_memory_id),
                    "to_memory_id": central_id,
                    "relation_type": "summarizes",
                },
                "proposal_apply_summary_link",
            )

        for member_id in candidate["member_ids"]:
            if consolidation_logic.summary_link_exists(conn, int(summary_memory_id), int(member_id), "consolidated_from"):
                continue
            link = _create_link(
                conn,
                int(summary_memory_id),
                int(member_id),
                "consolidated_from",
                1.0,
                f"consolidation_proposal_apply:{normalized_id}",
            )
            summary_links_created.append(link)
            add_sleep_action(
                conn,
                run_id,
                "summary_link_created",
                int(summary_memory_id),
                None,
                {
                    "link_id": int(link["id"]),
                    "from_memory_id": int(summary_memory_id),
                    "to_memory_id": int(member_id),
                    "relation_type": "consolidated_from",
                },
                "proposal_apply_summary_link",
            )

        if support_links_created:
            central_memory = require_memory_row(conn, central_id)
            old_evidence_count = int(central_memory["evidence_count"] or 1)
            new_evidence_count = old_evidence_count + len(support_links_created)
            conn.execute(
                "UPDATE memories SET evidence_count = ?, sandman_note = ? WHERE id = ?",
                (new_evidence_count, f"Consolidation proposal apply: {normalized_id}", central_id),
            )
            boosted = {
                "memory_id": central_id,
                "old_evidence_count": old_evidence_count,
                "new_evidence_count": new_evidence_count,
            }
            central_evidence_boosted.append(boosted)
            add_sleep_action(
                conn,
                run_id,
                "canonical_evidence_boosted",
                central_id,
                {"evidence_count": old_evidence_count},
                {"evidence_count": new_evidence_count},
                "proposal_apply_support_bonus",
            )

        conn.commit()
        changed_count = len(support_links_created) + len(summary_links_created) + len(created_summary_memories) + len(central_evidence_boosted)
        finalize_sleep_run(
            conn,
            run_id,
            status="completed",
            scanned_count=len(candidate["member_ids"]),
            changed_count=changed_count,
            archived_count=0,
            downgraded_count=0,
            duplicate_count=0,
            conflict_count=0,
            created_summary_count=len(created_summary_memories),
        )
        return {
            "status": "ok",
            "schema_version": "memory_consolidation_apply.v1",
            "proposal_id": _consolidation_proposal_public_id(normalized_id),
            "proposal_review_status": proposal.get("status"),
            "proposal_type": proposal.get("proposal_type"),
            "run_id": run_id,
            "run_mode": "consolidation_apply_run",
            "applied_by": normalize_optional_text(applied_by) or "operator",
            "preview_snapshot_status": "stored",
            "preview_snapshot_hash": snapshot_payload["preview_hash"],
            "preview_hash_algorithm": snapshot_payload["preview_hash_algorithm"],
            "expected_preview_hash": normalized_expected_preview_hash,
            "preview_hash_guard": preview_hash_guard,
            "changed_count": changed_count,
            "support_links_created": support_links_created,
            "summary_links_created": summary_links_created,
            "created_summary_memories": created_summary_memories,
            "central_evidence_boosted": central_evidence_boosted,
            "rollback": {
                "supported": True,
                "preview_tool": "preview_undo_run",
                "apply_tool": "undo_run",
                "run_id": run_id,
            },
            "unsupported_metrics": unsupported_metrics,
        }
    finally:
        conn.close()


@mcp.tool
def reject_memory_consolidation_proposal(
    proposal_id: str,
    reason: str,
    reviewed_by: str | None = "operator",
) -> dict[str, Any]:
    """Reject a consolidation proposal by changing review-state only."""
    normalized_id = _normalize_consolidation_proposal_id(proposal_id)
    if normalized_id is None:
        return {"status": "error", "schema_version": "memory_consolidation_review.v1", "error": "proposal_id is invalid"}
    normalized_reason = normalize_optional_text(reason)
    if normalized_reason is None:
        return {"status": "error", "schema_version": "memory_consolidation_review.v1", "error": "reason cannot be empty"}
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND archived_at IS NULL AND memory_type = 'consolidation_proposal'",
            (normalized_id,),
        ).fetchone()
        if row is None:
            return {
                "status": "not_found",
                "schema_version": "memory_consolidation_review.v1",
                "proposal_id": _consolidation_proposal_public_id(normalized_id),
                "error": "proposal_not_found",
            }
        review_item = _get_or_create_consolidation_review_item(conn, normalized_id)
        old_status = _normalize_consolidation_proposal_status(review_item.get("status"))
        if old_status == "rejected":
            return {
                "status": "ok",
                "schema_version": "memory_consolidation_review.v1",
                "proposal_id": _consolidation_proposal_public_id(normalized_id),
                "old_status": "rejected",
                "new_status": "rejected",
                "already_in_target_status": True,
                "reviewed_at": normalize_optional_text(review_item.get("reviewed_at")),
                "reviewed_by": normalize_optional_text(review_item.get("reviewed_by")),
                "review_note": normalize_optional_text(review_item.get("review_note")),
                "mutated_memory_entries": False,
                "applied_patch": False,
            }

        now_iso = utc_now_iso()
        normalized_reviewed_by = normalize_optional_text(reviewed_by) or "operator"
        conn.execute(
            """
            UPDATE memory_consolidation_review_items
            SET status = 'rejected',
                reviewed_at = ?,
                reviewed_by = ?,
                review_note = ?,
                updated_at = ?
            WHERE proposal_memory_id = ?
            """,
            (now_iso, normalized_reviewed_by, normalized_reason, now_iso, normalized_id),
        )
        conn.commit()
        return {
            "status": "ok",
            "schema_version": "memory_consolidation_review.v1",
            "proposal_id": _consolidation_proposal_public_id(normalized_id),
            "old_status": old_status,
            "new_status": "rejected",
            "already_in_target_status": False,
            "reviewed_at": now_iso,
            "reviewed_by": normalized_reviewed_by,
            "review_note": normalized_reason,
            "mutated_memory_entries": False,
            "applied_patch": False,
        }
    finally:
        conn.close()


@mcp.tool
def compare_memory_modes(
    query: str,
    project_key: str | None = None,
    limit: int = 6,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Compare classic retrieval output with v2 restore/project-card output on the same scenario."""
    normalized_query = normalize_optional_text(query)
    if not normalized_query:
        return {"status": "error", "error": "query nie moze byc pusty"}
    normalized_project_key = normalize_optional_text(project_key)
    v1_result = find_memories(
        text_query=normalized_query,
        project_key=normalized_project_key,
        project_key_mode="aliases" if normalized_project_key else "exact",
        limit=limit,
        debug=include_debug,
    )
    conn = get_db_connection()
    try:
        memory_v2_enabled = _is_memory_v2_feature_active(conn)
    finally:
        conn.close()

    v1_items = list(v1_result.get("items") or [])
    v1_stubs = [_retrieval_result_stub(item) for item in v1_items[:limit]]
    v1_ids = [int(item["memory_id"]) for item in v1_stubs if item.get("memory_id") is not None]

    project_card: dict[str, Any] | None = None
    restore_result: dict[str, Any]
    if memory_v2_enabled:
        restore_result = get_memory_restore_ritual(
            project_key=normalized_project_key,
            full=False,
            include_debug=include_debug,
        )
        if normalized_project_key:
            project_card = get_project_card(
                project_key=normalized_project_key,
                limit=max(limit, 8),
                include_recent=True,
                include_debug=include_debug,
            )
    else:
        restore_result = {"status": "disabled", "reason": "feature_flag_off", "flag_key": MEMORY_V2_FLAG_KEY}

    v2_ids = sorted(
        {
            int(memory_id)
            for memory_id in (
                list(restore_result.get("source_memory_ids") or [])
                + list(project_card.get("source_memory_ids") or [])
                if project_card
                else list(restore_result.get("source_memory_ids") or [])
            )
            if memory_id is not None
        }
    )
    overlap_ids = sorted(set(v1_ids) & set(v2_ids))
    only_v1_ids = [memory_id for memory_id in v1_ids if memory_id not in overlap_ids]
    only_v2_ids = [memory_id for memory_id in v2_ids if memory_id not in overlap_ids]

    notes: list[str] = []
    if memory_v2_enabled:
        notes.append("v1 shows ranked retrieval hits, while v2 groups memory into remembered, uncertain, and next steps.")
        if normalized_project_key:
            notes.append("ProjectCard adds project-centric framing on top of the same underlying memory pool.")
        if only_v1_ids:
            notes.append("Some v1 hits do not surface in the v2 ritual because they were neither high-confidence remembered items nor uncertainty/next-step signals.")
        if only_v2_ids:
            notes.append("Some v2 items appear only through project-card or restore heuristics, not through the raw text-ranking top hits.")
    else:
        notes.append("memory_v2_enabled is off, so only the classic retrieval path is active.")

    result = {
        "status": "ok",
        "query": normalized_query,
        "project_key": normalized_project_key,
        "memory_v2_enabled": memory_v2_enabled,
        "v1": {
            "count": int(v1_result.get("count") or 0),
            "top_results": v1_stubs,
            "source_memory_ids": v1_ids,
        },
        "v2": {
            "restore_ritual": restore_result,
            "project_card": project_card,
            "source_memory_ids": v2_ids,
        },
        "comparison": {
            "overlap_memory_ids": overlap_ids,
            "only_v1_memory_ids": only_v1_ids,
            "only_v2_memory_ids": only_v2_ids,
            "overlap_count": len(overlap_ids),
        },
        "notes": notes,
        "debug": {},
    }
    if include_debug:
        result["debug"] = {
            "resolved_project_key_v1": ((v1_result.get("debug") or {}).get("resolved_project_key")),
            "v1_strategy": ((v1_result.get("debug") or {}).get("retrieval_strategy")),
            "restore_debug": restore_result.get("debug") if isinstance(restore_result, dict) else None,
            "project_card_debug": project_card.get("debug") if isinstance(project_card, dict) else None,
        }
    return result


@mcp.tool
def list_project_key_aliases(canonical_project_key: str | None = None, include_inactive: bool = False) -> dict[str, Any]:
    """List configured project_key aliases. Aliases are only used when project_key_mode='aliases'."""
    conn = get_db_connection()
    try:
        return list_project_key_aliases_payload(
            conn,
            canonical_project_key=canonical_project_key,
            include_inactive=include_inactive,
            normalize_optional_text=normalize_optional_text,
        )
    finally:
        conn.close()


@mcp.tool
def upsert_project_key_alias(
    canonical_project_key: str,
    alias_project_key: str,
    alias_kind: str | None = "alias",
    status: str | None = "active",
    notes: str | None = None,
) -> dict[str, Any]:
    """Create or update one project_key alias for explicit alias-mode retrieval."""
    conn = get_db_connection()
    try:
        return upsert_project_key_alias_payload(
            conn,
            canonical_project_key=canonical_project_key,
            alias_project_key=alias_project_key,
            alias_kind=alias_kind,
            status=status,
            notes=notes,
            normalize_optional_text=normalize_optional_text,
        )
    finally:
        conn.close()




def _agent_workshop_index() -> list[dict[str, Any]]:
    return agent_workshop_index()


def _agent_recommended_next_calls() -> dict[str, Any]:
    return agent_recommended_next_calls()


def _agent_bootstrap_protocol() -> dict[str, Any]:
    return agent_bootstrap_protocol()



def _persist_onboarding_answer(conn: Any, *, step: str, value: Any) -> list[int]:
    if value is None:
        return []
    subject = normalize_optional_text(os.getenv("MAPI_AGENT_SUBJECT_KEY")) or "agent"
    self_project = normalize_optional_text(os.getenv("MAPI_AGENT_PROJECT_KEY")) or "agent-self"
    created_ids: list[int] = []

    if step == "agent_name":
        name = normalize_required_text(str(value), "agent_name")
        old = conn.execute(
            """
            SELECT * FROM memories
            WHERE project_key=? AND memory_type='identity'
              AND activity_state='active'
              AND (tags LIKE '%agent-self%' OR source_event_ref LIKE 'mapi-init:%:identity')
            ORDER BY CASE WHEN source_event_ref=? THEN 0 ELSE 1 END, id DESC
            LIMIT 1
            """,
            (self_project, f"mapi-init:{subject}:identity"),
        ).fetchone()
        old_id = int(old["id"]) if old is not None else None
        created = _insert_memory(
            conn,
            content=f"{name} is the name chosen by the user for this assistant instance.",
            summary_short=f"Assistant name: {name}",
            memory_type="identity",
            source="polaris-onboarding",
            importance_score=1.0,
            confidence_score=1.0,
            tags=f"agent-self,self-model,self-evidence,identity,onboarding,chosen-by:user,subject:{subject},agent:{subject}",
            layer_code="identity",
            area_code="identity",
            state_code="validated",
            scope_code="project",
            identity_weight=1.0,
            project_key=self_project,
            entry_type="user_profile",
            truth_kind="fact",
            title=f"Assistant name: {name}",
            source_context="Chosen explicitly by the user during Polaris onboarding.",
            source_event_ref="polaris-onboarding:v2:agent_name",
            supersedes_memory_id=old_id,
            importance_level="high",
            priority="high",
            ensure_embedding=False,
        )
        new_id = int(created["id"])
        created_ids.append(new_id)
        if old_id is not None and old_id != new_id:
            apply_direct_supersession_transition(
                conn,
                new_memory_id=new_id,
                old_memory_id=old_id,
                relation="supersedes",
                insert_event=_insert_memory_event,
                source="polaris-onboarding",
            )
        return created_ids

    if step == "user_name":
        name = normalize_required_text(str(value), "user_name")
        created = _insert_memory(
            conn,
            content=f"The user prefers to be addressed as {name}.",
            summary_short=f"User name: {name}",
            memory_type="identity",
            source="polaris-onboarding",
            importance_score=0.95,
            confidence_score=1.0,
            tags="user-profile,identity,onboarding,user-name",
            layer_code="identity",
            area_code="identity",
            state_code="validated",
            scope_code="global",
            identity_weight=0.9,
            project_key=None,
            entry_type="user_profile",
            truth_kind="fact",
            title=f"User name: {name}",
            source_context="Provided explicitly by the user during Polaris onboarding.",
            source_event_ref="polaris-onboarding:v2:user_name",
            importance_level="high",
            visibility_scope="private",
            priority="high",
            ensure_embedding=False,
        )
        created_ids.append(int(created["id"]))
        return created_ids

    if step == "work_context":
        text = normalize_required_text(str(value), "work_context")
        created = _insert_memory(
            conn,
            content=f"User work context and primary assistance needs: {text}",
            summary_short="User work context",
            memory_type="user_profile",
            source="polaris-onboarding",
            importance_score=0.8,
            confidence_score=1.0,
            tags="user-profile,onboarding,work-context",
            layer_code="core",
            area_code="preferences",
            state_code="validated",
            scope_code="global",
            identity_weight=0.5,
            project_key=None,
            entry_type="user_profile",
            truth_kind="fact",
            title="User work context",
            source_context="Provided explicitly by the user during Polaris onboarding.",
            source_event_ref="polaris-onboarding:v2:work_context",
            visibility_scope="private",
            ensure_embedding=False,
        )
        created_ids.append(int(created["id"]))
        return created_ids

    if step == "autonomy_level":
        level = normalize_required_text(str(value), "autonomy_level")
        descriptions = {
            "reactive": "The assistant should mainly respond to explicit user requests and avoid unsolicited next-step proposals.",
            "collaborative": "The assistant should propose useful next steps and flag problems while leaving consequential choices to the user.",
            "proactive": "The assistant should actively surface problems, propose actions and revisit important commitments within its authorized boundaries.",
        }
        created = _insert_memory(
            conn,
            content=descriptions[level],
            summary_short=f"Assistant autonomy level: {level}",
            memory_type="preference",
            source="polaris-onboarding",
            importance_score=0.95,
            confidence_score=1.0,
            tags="user-profile,onboarding,assistant-autonomy,interaction-policy",
            layer_code="core",
            area_code="preferences",
            state_code="validated",
            scope_code="global",
            identity_weight=0.7,
            project_key=None,
            entry_type="user_profile",
            truth_kind="decision",
            title=f"Assistant autonomy level: {level}",
            source_context="Chosen explicitly by the user during Polaris onboarding review.",
            source_event_ref="polaris-onboarding:v2:autonomy_level",
            visibility_scope="private",
            importance_level="high",
            priority="high",
            ensure_embedding=False,
        )
        created_ids.append(int(created["id"]))
        return created_ids

    if step == "memory_policy":
        policy = normalize_required_text(str(value), "memory_policy")
        descriptions = {
            "automatic_important": "The assistant may proactively store durable information it judges important.",
            "ask_when_unsure": "The assistant should ask the user when it is uncertain whether information belongs in durable memory.",
            "explicit_only": "The assistant should write durable memory only when the user explicitly asks it to do so.",
        }
        created = _insert_memory(
            conn,
            content=descriptions[policy],
            summary_short=f"Memory policy: {policy}",
            memory_type="preference",
            source="polaris-onboarding",
            importance_score=1.0,
            confidence_score=1.0,
            tags="user-profile,onboarding,memory-policy,guardrail",
            layer_code="core",
            area_code="preferences",
            state_code="validated",
            scope_code="global",
            identity_weight=0.7,
            project_key=None,
            entry_type="user_profile",
            truth_kind="decision",
            title=f"Memory policy: {policy}",
            source_context="Chosen explicitly by the user during Polaris onboarding.",
            source_event_ref="polaris-onboarding:v2:memory_policy",
            visibility_scope="private",
            importance_level="high",
            priority="high",
            ensure_embedding=False,
        )
        created_ids.append(int(created["id"]))
        return created_ids

    if step == "memory_exclusions":
        text = normalize_required_text(str(value), "memory_exclusions")
        created = _insert_memory(
            conn,
            content=f"Durable-memory exclusion requested by the user: {text}",
            summary_short="User memory exclusion",
            memory_type="guardrail",
            source="polaris-onboarding",
            importance_score=1.0,
            confidence_score=1.0,
            tags="user-profile,onboarding,memory-exclusion,guardrail",
            layer_code="core",
            area_code="preferences",
            state_code="validated",
            scope_code="global",
            identity_weight=0.8,
            project_key=None,
            entry_type="user_profile",
            truth_kind="decision",
            title="User memory exclusion",
            source_context="Provided explicitly by the user during Polaris onboarding.",
            source_event_ref="polaris-onboarding:v2:memory_exclusions",
            visibility_scope="private",
            importance_level="high",
            priority="high",
            ensure_embedding=False,
        )
        created_ids.append(int(created["id"]))
        return created_ids

    if step == "first_project":
        project = normalize_required_text(str(value), "first_project")
        created = _insert_memory(
            conn,
            content=f"The user's first Polaris project is {project}.",
            summary_short=f"Initial project: {project}",
            memory_type="project_checkpoint",
            source="polaris-onboarding",
            importance_score=0.8,
            confidence_score=1.0,
            tags="onboarding,project,initial-project",
            layer_code="core",
            area_code="projects",
            state_code="validated",
            scope_code="project",
            identity_weight=0.0,
            project_key=project,
            entry_type="project",
            truth_kind="fact",
            title=f"Initial project: {project}",
            source_context="Created from the user's explicit first-project choice during onboarding.",
            source_event_ref="polaris-onboarding:v2:first_project",
            importance_level="high",
            priority="normal",
            ensure_embedding=False,
        )
        created_ids.append(int(created["id"]))
        return created_ids

    return []


@mcp.tool
def get_polaris_onboarding() -> dict[str, Any]:
    """Return first-run onboarding state and the single next question ChatGPT should ask the user."""
    conn = get_db_connection()
    try:
        payload = build_onboarding_payload(conn)
        conn.commit()
        return payload
    finally:
        conn.close()


@mcp.tool
def advance_polaris_onboarding(
    step: str,
    value: str | None = None,
    skip: bool = False,
) -> dict[str, Any]:
    """Save one onboarding draft answer. Final summary confirmation atomically commits the reviewed profile."""
    conn = get_db_connection()
    try:
        normalized_step = str(step).strip().casefold()
        state = advance_onboarding_state(conn, step=normalized_step, value=value, skip=bool(skip))
        created_ids: list[int] = []
        if normalized_step == "summary_confirmation" and state.get("status") == "completed":
            answers = dict(state.get("answers") or {})
            for answer_step in ONBOARDING_STEPS[:-1]:
                created_ids.extend(
                    _persist_onboarding_answer(
                        conn,
                        step=answer_step,
                        value=answers.get(answer_step),
                    )
                )
        conn.commit()
        payload = build_onboarding_payload(conn)
        payload["created_memory_ids"] = created_ids
        payload["durable_profile_committed"] = bool(created_ids) and normalized_step == "summary_confirmation"
        return payload
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@mcp.tool
def revise_polaris_onboarding(
    step: str,
    value: str | None = None,
    skip: bool = False,
) -> dict[str, Any]:
    """Revise one draft answer while the onboarding summary is awaiting confirmation."""
    conn = get_db_connection()
    try:
        revise_onboarding_answer_state(conn, step=step, value=value, skip=bool(skip))
        conn.commit()
        payload = build_onboarding_payload(conn)
        payload["created_memory_ids"] = []
        payload["durable_profile_committed"] = False
        return payload
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@mcp.tool
def skip_polaris_onboarding(reason: str | None = None) -> dict[str, Any]:
    """Skip the remaining first-run questions without disabling Polaris or its memory tools."""
    conn = get_db_connection()
    try:
        skip_onboarding_state(conn, reason=reason)
        conn.commit()
        return build_onboarding_payload(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@mcp.tool
def bootstrap_agent_context(project_key: str | None = None, limit: int = 24) -> dict[str, Any]:
    """Restore continuity. On first run, also tell ChatGPT to guide the user through Polaris onboarding one question at a time."""
    payload = build_bootstrap_agent_context_payload(
        project_key=project_key,
        limit=limit,
        get_db_connection=get_db_connection,
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
        normalize_optional_text=normalize_optional_text,
    )
    conn = get_db_connection()
    try:
        onboarding = build_onboarding_payload(conn)
        conn.commit()
    finally:
        conn.close()
    payload["onboarding"] = onboarding
    if onboarding.get("onboarding_required"):
        payload["assistant_instruction"] = onboarding.get("assistant_instruction")
        payload["memory_self_healing"] = {
            "schema": memory_self_healing.SELF_HEALING_NOTICE_SCHEMA,
            "status": "deferred_until_onboarding_complete",
            "visible_to_user": False,
        }
        return payload

    conn = get_db_connection()
    try:
        healing_notice = memory_self_healing.build_self_healing_notice(conn)
    finally:
        conn.close()
    payload["memory_self_healing"] = healing_notice
    if healing_notice.get("assistant_instruction"):
        payload["assistant_instruction"] = healing_notice["assistant_instruction"]
    return payload


@mcp.tool
def gemma_lms_status() -> dict[str, Any]:
    """Return LM Studio/LMS diagnostics for the MAPI Gemma coding agent."""
    return mapi_gemma_agent.gemma_lms_status_payload()


@mcp.tool
def gemma_lms_load() -> dict[str, Any]:
    """Load configured Gemma model through the LM Studio CLI."""
    return mapi_gemma_agent.gemma_lms_load_payload()


@mcp.tool
def gemma_lms_unload() -> dict[str, Any]:
    """Unload configured Gemma model through the LM Studio CLI."""
    return mapi_gemma_agent.gemma_lms_unload_payload()


@mcp.tool
def gemma_ask(
    prompt: str,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
    timeout_seconds: int | None = None,
    ensure_loaded: bool = True,
    unload_after_call: bool | None = None,
) -> dict[str, Any]:
    """Ask Gemma a read-only question through LM Studio."""
    return mapi_gemma_agent.gemma_ask_payload(
        prompt=prompt,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        ensure_loaded=ensure_loaded,
        unload_after_call=unload_after_call,
    )


@mcp.tool
def gemma_coding_task(
    task: str,
    context: str | None = None,
    repository_hint: str | None = None,
    max_tokens: int | None = None,
    timeout_seconds: int | None = None,
    ensure_loaded: bool = True,
    unload_after_call: bool | None = None,
) -> dict[str, Any]:
    """Ask Gemma for structured read-only coding advice."""
    return mapi_gemma_agent.gemma_coding_task_payload(
        task=task,
        context=context,
        repository_hint=repository_hint,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        ensure_loaded=ensure_loaded,
        unload_after_call=unload_after_call,
    )


@mcp.tool
def gemma_worker_status() -> dict[str, Any]:
    return gemma_worker.gemma_worker_status_payload()


def _decode_gemma_worker_json_list(
    value: str | None,
    *,
    field_name: str,
) -> tuple[list[Any] | None, dict[str, Any] | None]:
    if value is None:
        return [], None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return None, {"status": "error", "error": f"invalid_{field_name}_json", "details": str(exc)}
    if not isinstance(parsed, list):
        return None, {"status": "error", "error": f"{field_name}_json_must_decode_to_array"}
    return parsed, None


@mcp.tool
def gemma_worker_create_job(
    task: str,
    repo: str,
    project_key: str | None = None,
    context: str | None = None,
    allowed_actions_json: str | None = None,
    acceptance_criteria_json: str | None = None,
) -> dict[str, Any]:
    allowed_actions, allowed_error = _decode_gemma_worker_json_list(
        allowed_actions_json,
        field_name="allowed_actions",
    )
    if allowed_error is not None:
        return allowed_error
    acceptance_criteria, acceptance_error = _decode_gemma_worker_json_list(
        acceptance_criteria_json,
        field_name="acceptance_criteria",
    )
    if acceptance_error is not None:
        return acceptance_error

    conn = get_db_connection()
    try:
        job = gemma_worker_jobs.create_gemma_worker_job(
            conn,
            task=task,
            repo=repo,
            project_key=project_key,
            context=context,
            allowed_actions=list(allowed_actions or []),
            acceptance_criteria=list(acceptance_criteria or []),
        )
        return {"status": "created", "job": job, "next_action": "prepare_plan"}
    finally:
        conn.close()


@mcp.tool
def gemma_worker_get_job(job_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return {"status": "ok", "job": gemma_worker_jobs.require_gemma_worker_job(conn, int(job_id))}
    finally:
        conn.close()


@mcp.tool
def gemma_worker_prepare_plan(
    job_id: int,
    max_tokens: int | None = None,
    timeout_seconds: int | None = None,
    ensure_loaded: bool = True,
    unload_after_call: bool | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return gemma_worker_runner.prepare_gemma_worker_plan(
            conn,
            job_id=job_id,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            ensure_loaded=ensure_loaded,
            unload_after_call=unload_after_call,
        )
    finally:
        conn.close()


@mcp.tool
def gemma_worker_approve_job(job_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return gemma_worker_runner.approve_gemma_worker_job(conn, job_id=job_id)
    finally:
        conn.close()


@mcp.tool
def gemma_worker_reject_job(job_id: int, reason: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return gemma_worker_runner.reject_gemma_worker_job(
            conn,
            job_id=job_id,
            reason=reason,
        )
    finally:
        conn.close()


@mcp.tool
def gemma_worker_cancel_job(job_id: int, reason: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return gemma_worker_runner.cancel_gemma_worker_job(
            conn,
            job_id=job_id,
            reason=reason,
        )
    finally:
        conn.close()


@mcp.tool
def gemma_worker_run_job(
    job_id: int,
    max_tokens: int | None = None,
    timeout_seconds: int | None = None,
    ensure_loaded: bool = True,
    unload_after_call: bool | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return gemma_worker_runner.run_gemma_worker_job(
            conn,
            job_id=job_id,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            ensure_loaded=ensure_loaded,
            unload_after_call=unload_after_call,
        )
    finally:
        conn.close()


@mcp.tool
def gemma_worker_get_report(job_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        job = gemma_worker_jobs.require_gemma_worker_job(conn, int(job_id))
        if job.get("result") is None:
            return {
                "status": "error",
                "error": "report_not_available",
                "job": job,
            }
        return {
            "status": "ok",
            "job_id": int(job["id"]),
            "job_status": job["status"],
            "summary": job["result"].get("summary"),
            "needs_agent_review": bool(job["result"].get("needs_agent_review")),
            "report": job["result"],
        }
    finally:
        conn.close()


@mcp.tool
def gemma_worker_prepare_task(
    task: str,
    repo: str,
    allowed_actions_json: str | None = None,
    acceptance_criteria_json: str | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    return gemma_worker.gemma_worker_prepare_task_payload(
        task=task,
        repo=repo,
        allowed_actions_json=allowed_actions_json,
        acceptance_criteria_json=acceptance_criteria_json,
        context=context,
    )


@mcp.tool
def gemma_worker_report(
    status: str,
    summary: str,
    changed_files_json: str | None = None,
    actions_used_json: str | None = None,
    tests_run_json: str | None = None,
    needs_agent_review: bool = True,
) -> dict[str, Any]:
    return gemma_worker.gemma_worker_report_payload(
        status=status,
        summary=summary,
        changed_files_json=changed_files_json,
        actions_used_json=actions_used_json,
        tests_run_json=tests_run_json,
        needs_agent_review=needs_agent_review,
    )



@mcp.tool
def gemma_worker_run_task(task: str, repo: str, allowed_actions_json: str | None = None, acceptance_criteria_json: str | None = None, context: str | None = None, max_tokens: int | None = None, timeout_seconds: int | None = None, ensure_loaded: bool = True, unload_after_call: bool | None = None) -> dict[str, Any]:
    return gemma_worker.gemma_worker_run_task_payload(task=task, repo=repo, allowed_actions_json=allowed_actions_json, acceptance_criteria_json=acceptance_criteria_json, context=context, max_tokens=max_tokens, timeout_seconds=timeout_seconds, ensure_loaded=ensure_loaded, unload_after_call=unload_after_call)


@mcp.tool
def open_workshop(area: str) -> dict[str, Any]:
    """Open a compact workshop index for one operational area."""
    return open_workshop_payload(area)

@mcp.tool
def insert_before_marker(
    path: str,
    marker: str,
    content: str,
    encoding: str = "utf-8",
    dry_run: bool = False,
    backup: bool = True,
    require_marker_once: bool = True,
) -> dict[str, Any]:
    """Safely insert text before an exact marker in a text file."""
    return insert_before_marker_payload(
        safe_path=safe_path,
        rel_path=rel_path,
        path=path,
        marker=marker,
        content=content,
        encoding=encoding,
        dry_run=dry_run,
        backup=backup,
        require_marker_once=require_marker_once,
    )


@mcp.tool
def insert_after_marker(
    path: str,
    marker: str,
    content: str,
    encoding: str = "utf-8",
    dry_run: bool = False,
    backup: bool = True,
    require_marker_once: bool = True,
) -> dict[str, Any]:
    """Safely insert text after an exact marker in a text file."""
    return insert_after_marker_payload(
        safe_path=safe_path,
        rel_path=rel_path,
        path=path,
        marker=marker,
        content=content,
        encoding=encoding,
        dry_run=dry_run,
        backup=backup,
        require_marker_once=require_marker_once,
    )


@mcp.tool
def replace_once(
    path: str,
    find: str,
    replace: str,
    encoding: str = "utf-8",
    dry_run: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    """Safely replace one exact text occurrence in a text file."""
    return replace_once_payload(
        safe_path=safe_path,
        rel_path=rel_path,
        path=path,
        find=find,
        replace=replace,
        encoding=encoding,
        dry_run=dry_run,
        backup=backup,
    )

@mcp.tool
def run_shell(script: str, workdir: str | None = None, timeout_seconds: int = 60) -> dict[str, Any]:
    """Run an arbitrary admin shell script using the platform-native shell."""
    return run_shell_command(root=runtime_root(), script=script, workdir=workdir, timeout_seconds=timeout_seconds)


@mcp.tool
def run_powershell(script: str, workdir: str | None = None, timeout_seconds: int = 60) -> dict[str, Any]:
    """Run a PowerShell script from the dev MCP server."""
    return run_powershell_command(root=runtime_root(), script=script, workdir=workdir, timeout_seconds=timeout_seconds)


@mcp.tool
def run_pytest(test_path: str | None = None, timeout_seconds: int = 120, extra_args: list[str] | None = None) -> dict[str, Any]:
    """Run pytest from the repository root."""
    return run_pytest_command(root=runtime_root(), test_path=test_path, timeout_seconds=timeout_seconds, extra_args=extra_args)


@mcp.tool
def git_status(workdir: str | None = None) -> dict[str, Any]:
    """Run git status --short."""
    return git_status_command(root=runtime_root(), workdir=workdir)


@mcp.tool
def git_commit(message: str, workdir: str | None = None, stage_all: bool = True) -> dict[str, Any]:
    """Create a git commit. Optionally stages all changes first."""
    return git_commit_command(root=runtime_root(), message=message, workdir=workdir, stage_all=stage_all)


@mcp.tool
def git_push(remote: str = "origin", branch: str | None = None, workdir: str | None = None) -> dict[str, Any]:
    """Push the current branch or an explicit branch."""
    return git_push_command(root=runtime_root(), remote=remote, branch=branch, workdir=workdir)


@mcp.tool
def run_workshop_action(
    area: str,
    action: str,
    payload: dict[str, Any] | None = None,
    payload_json: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Run an action from a workshop through the compact MCP surface."""
    return run_workshop_action_payload(
        area=area,
        action=action,
        payload=payload,
        payload_json=payload_json,
        idempotency_key=idempotency_key,
        get_db_connection=get_db_connection,
        normalize_optional_text=normalize_optional_text,
    )


def _memory_links_response(memory_id: int, outgoing_rows: list[Any], incoming_rows: list[Any]) -> dict[str, Any]:
    return memory_links_response(memory_id, outgoing_rows, incoming_rows, row_to_dict=row_to_dict)


def _attach_links_to_memory_items(conn, items: list[dict[str, Any]], *, include_links: bool = False) -> list[dict[str, Any]]:
    return attach_links_to_memory_items(conn, items, row_to_dict=row_to_dict, include_links=include_links)


@mcp.tool
def get_memory(memory_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        memory = require_memory_row(conn, memory_id)
        outgoing = conn.execute("SELECT * FROM memory_links WHERE archived_at IS NULL AND from_memory_id = ? ORDER BY id ASC", (memory_id,)).fetchall()
        incoming = conn.execute("SELECT * FROM memory_links WHERE archived_at IS NULL AND to_memory_id = ? ORDER BY id ASC", (memory_id,)).fetchall()
        memory_item = _apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(memory))))
        link_payload = _memory_links_response(memory_id, outgoing, incoming)
        memory_item["link_count"] = link_payload["link_count"]
        memory_item["outgoing_link_count"] = link_payload["outgoing_link_count"]
        memory_item["incoming_link_count"] = link_payload["incoming_link_count"]
        memory_item["links"] = link_payload["links"]
        memory_item["outgoing_links"] = link_payload["outgoing_links"]
        memory_item["incoming_links"] = link_payload["incoming_links"]
        memory_item["linked_memories"] = sorted(
            {
                int(link["other_memory_id"])
                for link in link_payload["links"]
                if link.get("other_memory_id") is not None
            }
        )
    finally:
        conn.close()
    return {"memory": memory_item, **link_payload}


@mcp.tool
def get_memory_links(memory_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        require_memory_row(conn, memory_id)
        outgoing = conn.execute("SELECT * FROM memory_links WHERE archived_at IS NULL AND from_memory_id = ? ORDER BY id ASC", (memory_id,)).fetchall()
        incoming = conn.execute("SELECT * FROM memory_links WHERE archived_at IS NULL AND to_memory_id = ? ORDER BY id ASC", (memory_id,)).fetchall()
        return _memory_links_response(memory_id, outgoing, incoming)
    finally:
        conn.close()



@mcp.tool
def propose_memory_capture(
    content: str,
    project_key: str | None = None,
    source_context: str | None = None,
    conversation_key: str | None = None,
    hint: str | None = None,
) -> dict[str, Any]:
    """Preview a structured memory proposal without writing it to the database."""
    conn = get_db_connection()
    try:
        if not _is_memory_v2_feature_active(conn):
            return {"status": "disabled", "reason": "feature_flag_off", "flag_key": MEMORY_V2_FLAG_KEY}
        blocked = _capture_retention_gate(
            conn,
            content=content,
            project_key=project_key,
            scope_code=None,
        )
        if blocked is not None:
            return blocked
    finally:
        conn.close()
    result = _classify_memory_capture(
        content=content,
        project_key=project_key,
        source_context=source_context or conversation_key,
        hint=hint,
    )
    if result.get("status") != "proposed":
        return result
    proposal = dict(result["proposal"])
    if normalize_optional_text(conversation_key):
        proposal["conversation_key"] = normalize_optional_text(conversation_key)
    return {
        "status": "proposed",
        "message": result["message"],
        "proposal": proposal,
        "signals": result["signals"],
        "reasons": result["reasons"],
        "edit_hints": result["edit_hints"],
    }


@mcp.tool
def create_memory_from_proposal(
    proposal_json: str,
    content: str | None = None,
    summary_short: str | None = None,
    title: str | None = None,
    project_key: str | None = None,
    tags: str | None = None,
    source_context: str | None = None,
) -> dict[str, Any]:
    """Approve a memory proposal and persist it as a normal memory entry."""
    try:
        proposal = json.loads(normalize_required_text(proposal_json, "proposal_json"))
    except json.JSONDecodeError as exc:
        return {"status": "error", "error": f"invalid proposal_json: {exc}"}
    if not isinstance(proposal, dict):
        return {"status": "error", "error": "proposal_json must decode to an object"}

    merged = dict(proposal)
    if content is not None:
        merged["content"] = content
    if summary_short is not None:
        merged["summary_short"] = summary_short
    if title is not None:
        merged["title"] = title
    if project_key is not None:
        merged["project_key"] = project_key
    if tags is not None:
        merged["tags"] = tags
    if source_context is not None:
        merged["source_context"] = source_context

    normalized_scope_code = normalize_scope_code(merged.get("scope_code"))
    normalized_project_key = normalize_optional_text(merged.get("project_key"))
    conn = get_db_connection()
    try:
        if not _is_memory_v2_feature_active(conn):
            return {"status": "disabled", "reason": "feature_flag_off", "flag_key": MEMORY_V2_FLAG_KEY}
        blocked = _capture_retention_gate(
            conn,
            content=merged.get("content"),
            project_key=normalized_project_key,
            scope_code=normalized_scope_code,
            metadata={"tags": merged.get("tags"), "visibility_scope": merged.get("visibility_scope")},
        )
        if blocked is not None:
            return blocked
        evaluation = _capture_reconciliation_flag_evaluation(
            conn,
            project_key=normalized_project_key,
            scope_code=normalized_scope_code,
        )
        if evaluation["enabled"]:
            return {
                "status": "reconciliation_required",
                "reason": evaluation["reason"],
                "flag_key": MEMORY_V3_CAPTURE_RECONCILIATION_FLAG_KEY,
                "operator_next_action": "use_capture_save_then_reconciliation_preview",
                "recommended_actions": [
                    "capture_save",
                    "capture_review_decide",
                    "capture_reconciliation_preview",
                ],
                "safety": {
                    "review_queue_only": True,
                    "memory_mutations_performed": 0,
                },
            }
    finally:
        conn.close()

    created = _create_memory_direct(
        content=str(merged.get("content") or ""),
        memory_type=str(merged.get("memory_type") or ""),
        summary_short=merged.get("summary_short"),
        source=merged.get("source"),
        importance_score=float(merged.get("importance_score") or 0.5),
        confidence_score=float(merged.get("confidence_score") or 0.5),
        tags=merged.get("tags"),
        layer_code=merged.get("layer_code"),
        area_code=merged.get("area_code"),
        state_code=merged.get("state_code"),
        scope_code=merged.get("scope_code"),
        parent_memory_id=merged.get("parent_memory_id"),
        version=int(merged.get("version") or 1),
        promoted_from_id=merged.get("promoted_from_id"),
        demoted_from_id=merged.get("demoted_from_id"),
        supersedes_memory_id=merged.get("supersedes_memory_id"),
        valid_from=merged.get("valid_from"),
        valid_to=merged.get("valid_to"),
        decay_score=float(merged.get("decay_score") or 0.0),
        emotional_weight=float(merged.get("emotional_weight") or 0.0),
        identity_weight=float(merged.get("identity_weight") or 0.0),
        project_key=merged.get("project_key"),
        conversation_key=merged.get("conversation_key"),
        last_validated_at=merged.get("last_validated_at"),
        validation_source=merged.get("validation_source"),
        schema_version=int(merged.get("schema_version") or 2),
        entry_type=merged.get("entry_type"),
        truth_kind=merged.get("truth_kind"),
        title=merged.get("title"),
        source_context=merged.get("source_context"),
        source_event_ref=merged.get("source_event_ref"),
        updated_at=merged.get("updated_at"),
        last_confirmed_at=merged.get("last_confirmed_at"),
        memory_v2_status=merged.get("memory_v2_status") if merged.get("memory_v2_status") not in {None, "proposed"} else "active",
        importance_level=merged.get("importance_level"),
        superseded_by_memory_id=merged.get("superseded_by_memory_id"),
        requires_user_confirmation=bool(merged.get("requires_user_confirmation")) if merged.get("memory_v2_status") not in {None, "proposed"} else False,
        should_resurface_when=merged.get("should_resurface_when"),
        owner_role=merged.get("owner_role"),
        owner_id=merged.get("owner_id"),
        review_due_at=merged.get("review_due_at"),
        revalidation_due_at=merged.get("revalidation_due_at"),
        expired_due_at=merged.get("expired_due_at"),
        priority=merged.get("priority"),
    )
    if created.get("status") == "created" and created.get("memory", {}).get("id") is not None:
        conn = get_db_connection()
        try:
            _insert_memory_event(
                conn,
                memory_id=int(created["memory"]["id"]),
                event_type="memory_v2.proposal_approved",
                payload={
                    "source": "create_memory_from_proposal",
                    "old_status": merged.get("memory_v2_status") or "proposed",
                    "new_status": created["memory"].get("memory_v2_status"),
                    "entry_type": created["memory"].get("entry_type"),
                    "truth_kind": created["memory"].get("truth_kind"),
                },
            )
            conn.commit()
        finally:
            conn.close()
    return created


@mcp.tool
def save_memory_capture_proposal(
    content: str,
    project_key: str | None = None,
    scope_code: str | None = None,
    source_context: str | None = None,
    conversation_key: str | None = None,
    source_event_ref: str | None = None,
    hint: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Persist a durable capture review item without creating a memory."""
    conn = get_db_connection()
    try:
        if not _is_memory_v2_feature_active(conn):
            return {"status": "disabled", "reason": "feature_flag_off", "flag_key": MEMORY_V2_FLAG_KEY}
        blocked = _capture_retention_gate(
            conn,
            content=content,
            project_key=project_key,
            scope_code=scope_code,
        )
        if blocked is not None:
            return blocked
        result = _classify_memory_capture(
            content=content,
            project_key=project_key,
            source_context=source_context or conversation_key,
            hint=hint,
        )
        if result.get("status") != "proposed":
            return {
                "status": result.get("status") or "skipped",
                "schema_version": CAPTURE_REVIEW_ITEM_SCHEMA_VERSION,
                "classifier_signals": list(result.get("signals") or []),
                "classifier_reasons": list(result.get("reasons") or []),
                "skip_reason": result.get("skip_reason"),
                "safety": {
                    "review_queue_only": True,
                    "memory_mutations_performed": 0,
                },
            }
        proposal = dict(result["proposal"])
        normalized_scope_code = normalize_scope_code(scope_code) or normalize_scope_code(proposal.get("scope_code"))
        proposal["scope_code"] = normalized_scope_code
        proposal["project_key"] = normalize_optional_text(project_key) or normalize_optional_text(proposal.get("project_key"))
        proposal["source_context"] = normalize_optional_text(source_context) or normalize_optional_text(proposal.get("source_context"))
        proposal["conversation_key"] = normalize_optional_text(conversation_key)
        proposal["source_event_ref"] = normalize_optional_text(source_event_ref)
        input_fingerprint = _capture_input_fingerprint(
            content=content,
            project_key=proposal.get("project_key"),
            scope_code=proposal.get("scope_code"),
            conversation_key=proposal.get("conversation_key"),
            source_context=proposal.get("source_context"),
            source_event_ref=proposal.get("source_event_ref"),
            hint=hint,
        )
        created = create_capture_review_item(
            conn,
            proposal_key=_capture_proposal_key(input_fingerprint=input_fingerprint),
            proposal=proposal,
            input_fingerprint=input_fingerprint,
            project_key=proposal.get("project_key"),
            scope_code=proposal.get("scope_code"),
            conversation_key=proposal.get("conversation_key"),
            source_context=proposal.get("source_context"),
            source_event_ref=proposal.get("source_event_ref"),
            recommended_action="capture_review",
            expires_at=normalize_optional_text(expires_at),
            utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
        )
        conn.commit()
        return {
            "status": "queued" if created["created"] else "already_queued",
            "schema_version": CAPTURE_REVIEW_ITEM_SCHEMA_VERSION,
            "item": created["item"],
            "classifier_signals": list(result.get("signals") or []),
            "classifier_reasons": list(result.get("reasons") or []),
            "edit_hints": list(result.get("edit_hints") or []),
            "safety": {
                "review_queue_only": True,
                "memory_mutations_performed": 0,
            },
        }
    finally:
        conn.close()


@mcp.tool
def list_memory_capture_review_items(
    status: str | None = None,
    project_key: str | None = None,
    scope_code: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    limit: int = 50,
    include_expired: bool = False,
) -> dict[str, Any]:
    """List durable capture review items."""
    conn = get_db_connection()
    try:
        return list_capture_review_items(
            conn,
            status=status,
            project_key=project_key,
            scope_code=scope_code,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
            include_expired=include_expired,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_memory_capture_review_item(item_id: int) -> dict[str, Any]:
    """Get one durable capture review item."""
    conn = get_db_connection()
    try:
        return {
            "status": "ok",
            "schema_version": CAPTURE_REVIEW_ITEM_SCHEMA_VERSION,
            "item": get_capture_review_item(
                conn,
                item_id=int(item_id),
                row_to_dict=row_to_dict,
            ),
            "safety": {
                "read_only": True,
                "memory_mutations_performed": 0,
            },
        }
    finally:
        conn.close()


@mcp.tool
def review_memory_capture_item(
    item_id: int,
    decision: str,
    reviewed_by: str | None = None,
    review_note: str | None = None,
) -> dict[str, Any]:
    """Approve or reject a durable capture review item without creating a memory."""
    conn = get_db_connection()
    try:
        result = review_capture_item(
            conn,
            item_id=int(item_id),
            decision=decision,
            reviewed_by=reviewed_by,
            review_note=review_note,
            utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
        )
        if result.get("status") == "updated":
            conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool
def expire_memory_capture_item(
    item_id: int,
    reason: str,
    expired_by: str | None = None,
) -> dict[str, Any]:
    """Expire a durable capture review item without creating a memory."""
    conn = get_db_connection()
    try:
        result = expire_capture_item(
            conn,
            item_id=int(item_id),
            reason=reason,
            expired_by=expired_by,
            utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
        )
        if result.get("status") == "expired":
            conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool
def preview_memory_capture_reconciliation(
    item_id: int,
    candidate_limit: int = 20,
    semantic_limit: int = 10,
    include_semantic: bool = True,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Build and persist a deterministic capture reconciliation preview without applying it."""
    conn = get_db_connection()
    try:
        blocked = _queued_capture_retention_gate(conn, item_id=int(item_id))
        if blocked is not None:
            return blocked
        result = preview_memory_capture_reconciliation_payload(
            conn,
            item_id=int(item_id),
            candidate_limit=int(candidate_limit),
            semantic_limit=int(semantic_limit),
            include_semantic=bool(include_semantic),
            include_debug=bool(include_debug),
            normalize_required_text=normalize_required_text,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            canonical_json_hash=_canonical_json_hash,
            utc_now_iso=utc_now_iso,
            search_semantic_func=search_semantic,
        )
        if result.get("status") == "preview_ready":
            conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool
def apply_memory_capture_reconciliation(
    item_id: int,
    expected_preview_hash: str,
    applied_by: str,
    notes: str | None = None,
    confirm_protected: bool = False,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Atomically apply one approved Memory v3 capture reconciliation preview."""
    conn = get_db_connection()
    try:
        blocked = _queued_capture_retention_gate(conn, item_id=int(item_id))
        if blocked is not None:
            return blocked
        return apply_memory_capture_reconciliation_payload(
            conn,
            item_id=int(item_id),
            expected_preview_hash=expected_preview_hash,
            applied_by=applied_by,
            notes=notes,
            confirm_protected=bool(confirm_protected),
            include_debug=bool(include_debug),
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            canonical_json_hash=_canonical_json_hash,
            utc_now_iso=utc_now_iso,
            shift_iso_days=shift_iso_days,
            memory_v2_enabled=_is_memory_v2_feature_active,
            reconciliation_flag_evaluation=_capture_reconciliation_flag_evaluation,
            capture_proposal_key=_capture_proposal_key,
            preview_reconciliation=preview_memory_capture_reconciliation_payload,
            search_semantic_func=search_semantic,
            insert_memory=_insert_memory,
            insert_memory_event=insert_memory_event,
            create_link=_create_link,
            record_timeline_event=timeline.record_timeline_event,
            new_operation_id=timeline.new_operation_id,
        )
    finally:
        conn.close()


@mcp.tool
def preview_memory_retention_policy(
    memory_id: int,
    as_of: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Build a deterministic redacted retention preview for one memory."""
    conn = get_db_connection()
    try:
        return preview_memory_retention_policy_payload(
            conn,
            memory_id=int(memory_id),
            as_of=as_of,
            include_debug=bool(include_debug),
            row_to_dict=row_to_dict,
            canonical_json_hash=_canonical_json_hash,
            utc_now_iso=utc_now_iso,
        )
    finally:
        conn.close()


@mcp.tool
def preview_project_memory_retention(
    project_key: str,
    as_of: str | None = None,
    limit: int = 50,
    include_retain: bool = False,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Build a deterministic read-only retention preview for one project namespace."""
    conn = get_db_connection()
    try:
        return preview_project_memory_retention_payload(
            conn,
            project_key=project_key,
            as_of=as_of,
            limit=int(limit),
            include_retain=bool(include_retain),
            include_debug=bool(include_debug),
            row_to_dict=row_to_dict,
            canonical_json_hash=_canonical_json_hash,
            utc_now_iso=utc_now_iso,
        )
    finally:
        conn.close()


@mcp.tool
def save_memory_retention_review(
    memory_id: int,
    expected_preview_hash: str,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Persist an idempotent pending retention review item from a fresh preview."""
    conn = get_db_connection()
    try:
        result = save_retention_review_item(
            conn,
            memory_id=int(memory_id),
            expected_preview_hash=expected_preview_hash,
            as_of=as_of,
            preview_func=lambda active_conn, **kwargs: preview_memory_retention_policy_payload(
                active_conn,
                row_to_dict=row_to_dict,
                canonical_json_hash=_canonical_json_hash,
                utc_now_iso=utc_now_iso,
                **kwargs,
            ),
            canonical_json_hash=_canonical_json_hash,
            utc_now_iso=utc_now_iso,
            row_to_dict=row_to_dict,
        )
        if result.get("status") == "created":
            conn.commit()
        return result
    finally:
        conn.close()


@mcp.tool
def list_memory_retention_reviews(
    status: str | None = None,
    project_key: str | None = None,
    memory_id: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List durable retention review items."""
    conn = get_db_connection()
    try:
        return list_retention_review_items(
            conn,
            status=status,
            project_key=project_key,
            memory_id=memory_id,
            limit=int(limit),
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_memory_retention_review(review_item_id: int) -> dict[str, Any]:
    """Get one durable retention review item."""
    conn = get_db_connection()
    try:
        return get_retention_review_item(conn, review_item_id=int(review_item_id), row_to_dict=row_to_dict)
    finally:
        conn.close()


@mcp.tool
def decide_memory_retention_review(
    review_item_id: int,
    decision: str,
    reviewed_by: str,
    review_note: str | None = None,
) -> dict[str, Any]:
    """Approve or reject one pending retention review item."""
    conn = get_db_connection()
    try:
        result = decide_retention_review_item(
            conn,
            review_item_id=int(review_item_id),
            decision=decision,
            reviewed_by=reviewed_by,
            review_note=review_note,
            utc_now_iso=utc_now_iso,
            row_to_dict=row_to_dict,
        )
        if result.get("status") == "updated":
            conn.commit()
        return result
    finally:
        conn.close()


def _retention_preview_for_apply(conn, **kwargs) -> dict[str, Any]:
    return preview_memory_retention_policy_payload(
        conn,
        row_to_dict=row_to_dict,
        canonical_json_hash=_canonical_json_hash,
        utc_now_iso=utc_now_iso,
        **kwargs,
    )


@mcp.tool
def apply_memory_retention_review(
    review_item_id: int,
    expected_preview_hash: str,
    applied_by: str,
    notes: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Atomically apply one approved and fresh retention review item."""
    conn = get_db_connection()
    try:
        return apply_memory_retention_review_payload(
            conn,
            review_item_id=int(review_item_id),
            expected_preview_hash=expected_preview_hash,
            applied_by=applied_by,
            notes=notes,
            include_debug=bool(include_debug),
            row_to_dict=row_to_dict,
            preview_func=_retention_preview_for_apply,
            memory_v2_enabled=_is_memory_v2_feature_active,
            retention_flag_evaluation=_retention_flag_evaluation,
            insert_memory_event=insert_memory_event,
            canonical_json_hash=_canonical_json_hash,
            utc_now_iso=utc_now_iso,
            compute_sla_days=compute_sla_days,
            shift_iso_days=shift_iso_days,
        )
    finally:
        conn.close()


@mcp.tool
def preview_memory_retention_rollback(review_item_id: int) -> dict[str, Any]:
    """Preview an exact rollback for an applied archive or expire action."""
    conn = get_db_connection()
    try:
        return preview_memory_retention_rollback_payload(
            conn,
            review_item_id=int(review_item_id),
            row_to_dict=row_to_dict,
            canonical_json_hash=_canonical_json_hash,
        )
    finally:
        conn.close()


@mcp.tool
def rollback_memory_retention_review(
    review_item_id: int,
    expected_rollback_preview_hash: str,
    rolled_back_by: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Atomically restore the exact pre-apply lifecycle snapshot."""
    conn = get_db_connection()
    try:
        return rollback_memory_retention_review_payload(
            conn,
            review_item_id=int(review_item_id),
            expected_rollback_preview_hash=expected_rollback_preview_hash,
            rolled_back_by=rolled_back_by,
            notes=notes,
            row_to_dict=row_to_dict,
            canonical_json_hash=_canonical_json_hash,
            utc_now_iso=utc_now_iso,
            insert_memory_event=insert_memory_event,
        )
    finally:
        conn.close()


@mcp.tool
def apply_memory_retention_batch(
    review_item_ids_json: str,
    expected_preview_hashes_json: str,
    applied_by: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Atomically apply 1..10 approved items after a trusted internal online backup."""
    try:
        item_ids = json.loads(normalize_required_text(review_item_ids_json, "review_item_ids_json"))
        expected_hashes = json.loads(normalize_required_text(expected_preview_hashes_json, "expected_preview_hashes_json"))
    except (json.JSONDecodeError, ValueError) as exc:
        return {"status": "error", "error": f"invalid_batch_json:{type(exc).__name__}"}
    if not isinstance(item_ids, list) or not isinstance(expected_hashes, dict):
        return {"status": "error", "error": "batch_json_contract_invalid"}
    conn = get_db_connection()
    try:
        return apply_memory_retention_batch_payload(
            conn,
            review_item_ids=item_ids,
            expected_preview_hashes=expected_hashes,
            applied_by=applied_by,
            notes=notes,
            source_db_path=runtime_db_path(),
            backups_root=runtime_data_dir() / "backups",
            row_to_dict=row_to_dict,
            preview_func=_retention_preview_for_apply,
            memory_v2_enabled=_is_memory_v2_feature_active,
            retention_flag_evaluation=_retention_flag_evaluation,
            insert_memory_event=insert_memory_event,
            canonical_json_hash=_canonical_json_hash,
            utc_now_iso=utc_now_iso,
            compute_sla_days=compute_sla_days,
            shift_iso_days=shift_iso_days,
        )
    finally:
        conn.close()


def _create_memory_direct(
    content: str,
    memory_type: str,
    summary_short: str | None = None,
    source: str | None = None,
    importance_score: float = 0.5,
    confidence_score: float = 0.5,
    tags: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    state_code: str | None = None,
    scope_code: str | None = None,
    parent_memory_id: int | None = None,
    version: int = 1,
    promoted_from_id: int | None = None,
    demoted_from_id: int | None = None,
    supersedes_memory_id: int | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    decay_score: float = 0.0,
    emotional_weight: float = 0.0,
    identity_weight: float = 0.0,
    project_key: str | None = None,
    conversation_key: str | None = None,
    last_validated_at: str | None = None,
    validation_source: str | None = None,
    schema_version: int = 2,
    entry_type: str | None = None,
    truth_kind: str | None = None,
    title: str | None = None,
    source_context: str | None = None,
    source_event_ref: str | None = None,
    updated_at: str | None = None,
    last_confirmed_at: str | None = None,
    memory_v2_status: str | None = None,
    importance_level: str | None = None,
    superseded_by_memory_id: int | None = None,
    requires_user_confirmation: bool = False,
    should_resurface_when: list[str] | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    review_due_at: str | None = None,
    revalidation_due_at: str | None = None,
    expired_due_at: str | None = None,
    priority: str | None = None,
    supersession_relation: str | None = None,
    supersession_scope: str | None = None,
) -> dict[str, Any]:
    if not content or not content.strip():
        return {"status": "error", "error": 'content cannot be empty'}
    if not memory_type or not memory_type.strip():
        return {"status": "error", "error": 'memory_type cannot be empty'}
    conn = get_db_connection()
    try:
        normalized_scope_code = normalize_scope_code(scope_code)
        if normalized_scope_code == "global":
            _require_feature_flag_write_access(
                conn,
                flag_key=CROSS_PROJECT_FLAG_KEY,
                project_key=project_key,
                scope_code=normalized_scope_code,
                operation_name="create_memory",
            )
        memory = _insert_memory(
            conn,
            content=content,
            memory_type=memory_type,
            summary_short=summary_short,
            source=source,
            importance_score=importance_score,
            confidence_score=confidence_score,
            tags=tags,
            layer_code=layer_code,
            area_code=area_code,
            state_code=state_code,
            scope_code=scope_code,
            parent_memory_id=parent_memory_id,
            version=version,
            promoted_from_id=promoted_from_id,
            demoted_from_id=demoted_from_id,
            supersedes_memory_id=supersedes_memory_id,
            valid_from=valid_from,
            valid_to=valid_to,
            decay_score=decay_score,
            emotional_weight=emotional_weight,
            identity_weight=identity_weight,
            project_key=project_key,
            conversation_key=conversation_key,
            last_validated_at=last_validated_at,
            validation_source=validation_source,
            schema_version=schema_version,
            entry_type=entry_type,
            truth_kind=truth_kind,
            title=title,
            source_context=source_context,
            source_event_ref=source_event_ref,
            updated_at=updated_at,
            last_confirmed_at=last_confirmed_at,
            memory_v2_status=memory_v2_status,
            importance_level=importance_level,
            superseded_by_memory_id=superseded_by_memory_id,
            requires_user_confirmation=requires_user_confirmation,
            should_resurface_when=should_resurface_when,
            owner_role=owner_role,
            owner_id=owner_id,
            review_due_at=review_due_at,
            revalidation_due_at=revalidation_due_at,
            expired_due_at=expired_due_at,
            priority=priority,
        )
        creation_event = _insert_memory_event(
            conn,
            memory_id=int(memory["id"]),
            event_type="memory_v2.created",
            payload={
                "source": normalize_optional_text(source) or "create_memory",
                "schema_version": int(memory.get("schema_version") or 1),
                "entry_type": memory.get("entry_type"),
                "truth_kind": memory.get("truth_kind"),
                "memory_v2_status": memory.get("memory_v2_status"),
                "requires_user_confirmation": bool(memory.get("requires_user_confirmation")),
                "project_key": memory.get("project_key"),
            },
        )
        if not normalize_optional_text(memory.get("source_event_ref")):
            generated_source_event_ref = f"memory-event:{int(creation_event['id'])}"
            conn.execute(
                "UPDATE memories SET source_event_ref=?, updated_at=? WHERE id=? AND (source_event_ref IS NULL OR trim(source_event_ref)='')",
                (generated_source_event_ref, utc_now_iso(), int(memory["id"])),
            )
            refreshed = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory["id"]),)).fetchone()
            if refreshed is not None:
                memory = enrich_memory_dict(row_to_dict(refreshed))
        if supersedes_memory_id is not None:
            transition = apply_direct_supersession_transition(
                conn,
                new_memory_id=int(memory["id"]),
                old_memory_id=int(supersedes_memory_id),
                relation=normalize_optional_text(supersession_relation) or "supersedes",
                scope_note=normalize_optional_text(supersession_scope),
                now_iso=utc_now_iso,
                insert_event=_insert_memory_event,
                source=normalize_optional_text(source) or "operator_confirmed_direct",
            )
            refreshed = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory["id"]),)).fetchone()
            if refreshed is not None:
                memory = enrich_memory_dict(row_to_dict(refreshed))
            memory["supersession_transition"] = transition
        conn.commit()

        # post-commit auto-embedding retry: vector writes are verified after durable memory insert.
        memory["embedding_hook"] = _ensure_memory_embedding_best_effort(conn, memory)
        conn.commit()
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()
    return {"status": "created", "memory": memory}


def _memory_create_payload(**values: Any) -> dict[str, Any]:
    """Return the lossless payload persisted in a capture proposal."""
    return dict(values)


def _queue_public_memory_create(payload: dict[str, Any]) -> dict[str, Any] | None:
    normalized_project_key = normalize_optional_text(payload.get("project_key"))
    normalized_scope_code = normalize_scope_code(payload.get("scope_code"))
    conn = get_db_connection()
    try:
        evaluation = _capture_reconciliation_flag_evaluation(
            conn, project_key=normalized_project_key, scope_code=normalized_scope_code
        )
        if not evaluation["enabled"]:
            return None
        proposal = dict(payload)
        proposal["project_key"] = normalized_project_key
        proposal["scope_code"] = normalized_scope_code
        input_fingerprint = _capture_input_fingerprint(
            content=str(proposal.get("content") or ""),
            project_key=normalized_project_key,
            scope_code=normalized_scope_code,
            conversation_key=proposal.get("conversation_key"),
            source_context=proposal.get("source_context"),
            source_event_ref=proposal.get("source_event_ref"),
            hint=None,
        )
        proposal_key = _capture_proposal_key(input_fingerprint=input_fingerprint)
        queued = create_capture_review_item(
            conn,
            proposal_key=proposal_key,
            proposal=proposal,
            input_fingerprint=input_fingerprint,
            project_key=normalized_project_key,
            scope_code=normalized_scope_code,
            conversation_key=proposal.get("conversation_key"),
            source_context=proposal.get("source_context"),
            source_event_ref=proposal.get("source_event_ref"),
            recommended_action="capture_review",
            utc_now_iso=utc_now_iso,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
        )
        conn.commit()
        item = queued["item"]
        return {
            "status": "capture_queued",
            "memory_created": False,
            "capture_review_item_id": int(item["id"]),
            "proposal_key": proposal_key,
            "reconciliation_required": True,
            "read_only_mode": bool(evaluation.get("read_only_mode")),
        }
    finally:
        conn.close()


@mcp.tool
@idempotent_direct_mutation("direct:save_memory", get_db_connection_resolver=lambda: get_db_connection)
def save_memory(
    content: str,
    memory_type: str = "project_note",
    summary_short: str | None = None,
    project_key: str | None = None,
    scope_code: str | None = None,
    tags: str | None = None,
    title: str | None = None,
    entry_type: str | None = None,
    truth_kind: str | None = None,
    source_context: str | None = None,
    conversation_key: str | None = None,
    source_event_ref: str | None = None,
    importance_score: float = 0.75,
    confidence_score: float = 0.9,
    write_intent: str = "user_explicit",
    supersedes_memory_id: int | None = None,
    supersession_relation: str | None = None,
    supersession_scope: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Persist an explicit or deliberately autonomous private-MAPI memory."""
    if not profile_allows(current_surface_profile(), "agent"):
        return {"status": "denied", "error": "save_memory_requires_agent"}
    normalized_content = normalize_memory_content(content)
    if not normalized_content:
        return {"status": "error", "error": "content cannot be empty"}
    provenance = resolve_write_provenance(
        conversation_key=conversation_key,
        source_event_ref=source_event_ref,
        normalize_optional_text=normalize_optional_text,
    )
    conversation_key = provenance["conversation_key"]
    source_event_ref = provenance["source_event_ref"]
    normalized_project = normalize_optional_text(project_key)
    normalized_scope = normalize_scope_code(scope_code) or ("project" if normalized_project else None)
    conn = get_db_connection()
    try:
        preflight = memory_write_preflight(
            conn,
            content=normalized_content,
            project_key=normalized_project,
            scope_code=normalized_scope,
            source_event_ref=normalize_optional_text(source_event_ref),
            write_intent=write_intent,
            tags=tags,
            supersedes_memory_id=supersedes_memory_id,
        )
    finally:
        conn.close()
    if preflight.get("status") != "allowed":
        existing = dict(preflight.get("existing_memory") or {})
        result = {key: value for key, value in preflight.items() if key != "existing_memory"}
        result.update({
            "schema_version": WRITE_RESULT_SCHEMA,
            "memory_created": False,
            "memory_id": int(existing["id"]) if existing.get("id") is not None else None,
            "existing_memory": _section_memory_stub(existing) if existing else None,
        })
        return result

    normalized_intent = str(preflight["write_intent"])
    normalized_memory_type = normalize_optional_text(memory_type) or "project_note"
    normalized_summary = normalize_optional_text(summary_short) or _capture_summary_candidate(normalized_content, limit=140)
    importance_policy = memory_hygiene.apply_new_write_importance_policy(
        memory_type=normalized_memory_type,
        requested_score=float(importance_score),
        project_key=normalized_project,
        scope_code=normalized_scope,
        tags=tags,
        title=title or summary_short,
        summary_short=normalized_summary,
        entry_type=entry_type or ("project" if normalized_project else "memory"),
        truth_kind=truth_kind or "fact",
        source_context=source_context,
        source_event_ref=source_event_ref,
    )
    effective_priority = (
        "critical" if importance_policy["effective_level"] == "critical"
        else "high" if importance_policy["effective_level"] == "high"
        else "low" if importance_policy["effective_level"] == "low"
        else "normal"
    )
    created = _create_memory_direct(
        content=normalized_content,
        memory_type=normalized_memory_type,
        summary_short=normalized_summary,
        source=f"memory_write:{normalized_intent}",
        importance_score=float(importance_policy["effective_score"]),
        confidence_score=float(confidence_score),
        tags=tags,
        state_code="validated",
        scope_code=normalized_scope,
        supersedes_memory_id=supersedes_memory_id,
        project_key=normalized_project,
        conversation_key=conversation_key,
        entry_type=entry_type or ("project" if normalized_project else "memory"),
        truth_kind=truth_kind or "fact",
        title=title or summary_short,
        source_context=source_context,
        source_event_ref=source_event_ref,
        memory_v2_status="active",
        importance_level=importance_policy["effective_level"],
        priority=effective_priority,
        requires_user_confirmation=False,
        supersession_relation=supersession_relation,
        supersession_scope=supersession_scope,
    )
    if created.get("status") != "created":
        return created
    memory_id = int(created["memory"]["id"])
    conn = get_db_connection()
    try:
        event = _insert_memory_event(
            conn,
            memory_id=memory_id,
            event_type=f"memory.write.{normalized_intent}",
            payload={
                "write_intent": normalized_intent,
                "input_fingerprint": preflight["input_fingerprint"],
                "sensitivity_class": preflight["sensitivity"]["sensitivity_class"],
                "source_event_ref": normalize_optional_text(source_event_ref),
                "conversation_key": normalize_optional_text(conversation_key),
                "provenance_origins": list(provenance.get("origins") or []),
                "project_key": normalized_project,
                "scope_code": normalized_scope,
                "importance_policy": importance_policy,
            },
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "created",
        "schema_version": WRITE_RESULT_SCHEMA,
        "memory_created": True,
        "memory_id": memory_id,
        "write_mode": normalized_intent,
        "input_fingerprint": preflight["input_fingerprint"],
        "sensitivity_class": preflight["sensitivity"]["sensitivity_class"],
        "event_id": int(event["id"]),
        "provenance": {
            "conversation_key": normalize_optional_text(conversation_key),
            "source_event_ref": normalize_optional_text(source_event_ref),
            "origins": list(provenance.get("origins") or []),
        },
        "importance_policy": importance_policy,
        "memory": created["memory"],
    }


@mcp.tool
@idempotent_direct_mutation("direct:propose_memory", get_db_connection_resolver=lambda: get_db_connection)
def propose_memory(
    content: str,
    project_key: str | None = None,
    scope_code: str | None = None,
    source_context: str | None = None,
    conversation_key: str | None = None,
    source_event_ref: str | None = None,
    hint: str | None = None,
    expires_at: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Queue an uncertain or agent-generated memory proposal for review."""
    if not profile_allows(current_surface_profile(), "agent"):
        return {"status": "denied", "error": "propose_memory_requires_agent"}
    provenance = resolve_write_provenance(
        conversation_key=conversation_key,
        source_event_ref=source_event_ref,
        normalize_optional_text=normalize_optional_text,
    )
    result = save_memory_capture_proposal(
        content=content,
        project_key=project_key,
        scope_code=scope_code,
        source_context=source_context,
        conversation_key=provenance["conversation_key"],
        source_event_ref=provenance["source_event_ref"],
        hint=hint,
        expires_at=expires_at,
    )
    if result.get("status") in {"queued", "already_queued"}:
        item = dict(result.get("item") or {})
        return {
            **result,
            "status": "proposed" if result["status"] == "queued" else "already_proposed",
            "write_mode": "agent_proposed",
            "capture_review_item_id": int(item["id"]) if item.get("id") is not None else None,
            "memory_created": False,
        }
    return {**result, "write_mode": "agent_proposed", "memory_created": False}


def admin_memory_write(
    *, payload: dict[str, Any], operator_id: str, reason: str, source_event_ref: str
) -> dict[str, Any]:
    """Audited repair/migration write; never the normal MAPI save path."""
    result = create_memory_direct_confirmed(
        payload=payload,
        operator_id=operator_id,
        reason=reason,
        source_event_ref=source_event_ref,
    )
    if result.get("status") != "created":
        return result
    memory_id = int(result["memory"]["id"])
    conn = get_db_connection()
    try:
        event = _insert_memory_event(
            conn,
            memory_id=memory_id,
            event_type="memory.write.operator_override",
            payload={
                "operator_id": normalize_required_text(operator_id, "operator_id"),
                "reason": normalize_required_text(reason, "reason"),
                "source_event_ref": normalize_required_text(source_event_ref, "source_event_ref"),
                "write_mode": "operator_override",
            },
        )
        conn.commit()
    finally:
        conn.close()
    return {**result, "write_mode": "operator_override", "operator_override_event_id": int(event["id"])}


@mcp.tool
def create_memory(
    content: str, memory_type: str, summary_short: str | None = None, source: str | None = None,
    importance_score: float = 0.5, confidence_score: float = 0.5, tags: str | None = None,
    layer_code: str | None = None, area_code: str | None = None, state_code: str | None = None,
    scope_code: str | None = None, parent_memory_id: int | None = None, version: int = 1,
    promoted_from_id: int | None = None, demoted_from_id: int | None = None,
    supersedes_memory_id: int | None = None, valid_from: str | None = None, valid_to: str | None = None,
    decay_score: float = 0.0, emotional_weight: float = 0.0, identity_weight: float = 0.0,
    project_key: str | None = None, conversation_key: str | None = None,
    last_validated_at: str | None = None, validation_source: str | None = None, schema_version: int = 2,
    entry_type: str | None = None, truth_kind: str | None = None, title: str | None = None,
    source_context: str | None = None, source_event_ref: str | None = None, updated_at: str | None = None,
    last_confirmed_at: str | None = None, memory_v2_status: str | None = None,
    importance_level: str | None = None, superseded_by_memory_id: int | None = None,
    requires_user_confirmation: bool = False, should_resurface_when: list[str] | None = None,
    owner_role: str | None = None, owner_id: str | None = None, review_due_at: str | None = None,
    revalidation_due_at: str | None = None, expired_due_at: str | None = None, priority: str | None = None,
) -> dict[str, Any]:
    """Legacy compatibility alias. May still route through capture reconciliation."""
    if not content or not content.strip():
        return {"status": "error", "error": "content cannot be empty"}
    if not memory_type or not memory_type.strip():
        return {"status": "error", "error": "memory_type cannot be empty"}
    payload = _memory_create_payload(**locals())
    queued = _queue_public_memory_create(payload)
    if queued is not None:
        return queued
    return _create_memory_direct(**payload)


def create_memory_direct_confirmed(
    *, payload: dict[str, Any], operator_id: str, reason: str, source_event_ref: str
) -> dict[str, Any]:
    if not profile_allows(current_surface_profile(), "admin"):
        return {"status": "denied", "error": "operator_direct_write_requires_admin"}
    normalized_operator = normalize_required_text(operator_id, "operator_id")
    normalized_reason = normalize_required_text(reason, "reason")
    normalized_event_ref = normalize_required_text(source_event_ref, "source_event_ref")
    direct_payload = dict(payload)
    direct_payload["source_event_ref"] = normalized_event_ref
    created = _create_memory_direct(**direct_payload)
    if created.get("status") != "created":
        return created
    conn = get_db_connection()
    try:
        _insert_memory_event(
            conn, memory_id=int(created["memory"]["id"]), event_type="memory_v3.operator_confirmed_direct",
            payload={"operator_id": normalized_operator, "reason": normalized_reason,
                     "source_event_ref": normalized_event_ref, "write_mode": "operator_confirmed_direct"},
        )
        conn.commit()
    finally:
        conn.close()
    return {**created, "write_mode": "operator_confirmed_direct"}


# ---------------------------------------------------------------------------
# Multi-user helpers (Stage 1)
# ---------------------------------------------------------------------------

def _resolve_default_workspace_id(conn) -> int | None:
    """Zwraca ID domyÄąâ€şlnego workspace, lub None jeÄąâ€şli migracja nie zostaÄąâ€ša uruchomiona."""
    row = conn.execute(
        "SELECT id FROM workspaces WHERE workspace_key = 'default' LIMIT 1"
    ).fetchone()
    return int(row["id"]) if row else None


def _resolve_user_id(conn, user_key: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM users WHERE external_user_key = ? AND status = 'active' LIMIT 1",
        (user_key,),
    ).fetchone()
    return int(row["id"]) if row else None


@mcp.tool
def create_private_memory(
    content: str,
    memory_type: str,
    owner_user_key: str,
    summary_short: str | None = None,
    source: str | None = None,
    importance_score: float = 0.5,
    confidence_score: float = 0.5,
    tags: str | None = None,
    project_key: str | None = None,
    conversation_key: str | None = None,
    workspace_key: str | None = None,
) -> dict[str, Any]:
    """Tworzy prywatne wspomnienie przypisane do konkretnego uÄąÄ˝ytkownika."""
    if not content or not content.strip():
        return {"status": "error", "error": 'content cannot be empty'}
    conn = get_db_connection()
    try:
        if not _is_multiuser_feature_active(conn, MULTIUSER_IDENTITY_FLAG):
            return {
                "status": "disabled",
                "message": f"Feature flag '{MULTIUSER_IDENTITY_FLAG}' is off. "
                           "Enable it to use multi-user memory tools.",
            }
        actor = resolve_actor_context(
            conn,
            user_key=owner_user_key,
            workspace_key=workspace_key,
            project_key=project_key,
            conversation_key=conversation_key,
        )
        memory = _insert_memory(
            conn,
            content=content,
            memory_type=memory_type,
            summary_short=summary_short,
            source=source,
            importance_score=importance_score,
            confidence_score=confidence_score,
            tags=tags,
            project_key=project_key,
            conversation_key=conversation_key,
            visibility_scope="private",
            workspace_id=actor.workspace_id,
            owner_user_id=actor.user_id,
            created_by_user_id=actor.user_id,
            last_modified_by_user_id=actor.user_id,
            sharing_policy="explicit",
        )
        timeline.record_timeline_event(
            conn,
            event_type="memory.scope_assigned",
            memory_id=int(memory["id"]),
            origin="multiuser_auto",
            actor_user_id=actor.user_id,
            workspace_id=actor.workspace_id,
            actor_type=actor.actor_type,
            payload={"visibility_scope": "private", "owner_user_key": owner_user_key},
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "created", "memory": memory}


@mcp.tool
def create_project_memory(
    content: str,
    memory_type: str,
    project_key: str,
    summary_short: str | None = None,
    source: str | None = None,
    importance_score: float = 0.5,
    confidence_score: float = 0.5,
    tags: str | None = None,
    owner_user_key: str | None = None,
    workspace_key: str | None = None,
    conversation_key: str | None = None,
) -> dict[str, Any]:
    """Tworzy wspomnienie projektowe widoczne dla wszystkich czÄąâ€šonkÄ‚Ĺ‚w workspace w danym projekcie."""
    if not content or not content.strip():
        return {"status": "error", "error": 'content cannot be empty'}
    if not project_key or not project_key.strip():
        return {"status": "error", "error": 'project_key cannot be empty'}
    conn = get_db_connection()
    try:
        if not _is_multiuser_feature_active(conn, MULTIUSER_IDENTITY_FLAG):
            return {
                "status": "disabled",
                "message": f"Feature flag '{MULTIUSER_IDENTITY_FLAG}' is off. "
                           "Enable it to use multi-user memory tools.",
            }
        actor = resolve_actor_context(
            conn,
            user_key=owner_user_key,
            workspace_key=workspace_key,
            project_key=project_key,
            conversation_key=conversation_key,
        )
        memory = _insert_memory(
            conn,
            content=content,
            memory_type=memory_type,
            summary_short=summary_short,
            source=source,
            importance_score=importance_score,
            confidence_score=confidence_score,
            tags=tags,
            project_key=project_key,
            conversation_key=conversation_key,
            visibility_scope="project",
            workspace_id=actor.workspace_id,
            owner_user_id=actor.user_id if owner_user_key else None,
            created_by_user_id=actor.user_id,
            last_modified_by_user_id=actor.user_id,
            sharing_policy="explicit",
        )
        timeline.record_timeline_event(
            conn,
            event_type="memory.scope_assigned",
            memory_id=int(memory["id"]),
            origin="multiuser_auto",
            actor_user_id=actor.user_id,
            workspace_id=actor.workspace_id,
            actor_type=actor.actor_type,
            payload={"visibility_scope": "project", "project_key": project_key},
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "created", "memory": memory}


@mcp.tool
def create_workspace_memory(
    content: str,
    memory_type: str,
    summary_short: str | None = None,
    source: str | None = None,
    importance_score: float = 0.5,
    confidence_score: float = 0.5,
    tags: str | None = None,
    owner_user_key: str | None = None,
    workspace_key: str | None = None,
    project_key: str | None = None,
    conversation_key: str | None = None,
) -> dict[str, Any]:
    """Tworzy wspomnienie workspace-level widoczne dla wszystkich czÄąâ€šonkÄ‚Ĺ‚w workspace."""
    if not content or not content.strip():
        return {"status": "error", "error": 'content cannot be empty'}
    conn = get_db_connection()
    try:
        if not _is_multiuser_feature_active(conn, MULTIUSER_IDENTITY_FLAG):
            return {
                "status": "disabled",
                "message": f"Feature flag '{MULTIUSER_IDENTITY_FLAG}' is off. "
                           "Enable it to use multi-user memory tools.",
            }
        actor = resolve_actor_context(
            conn,
            user_key=owner_user_key,
            workspace_key=workspace_key,
            project_key=project_key,
            conversation_key=conversation_key,
        )
        memory = _insert_memory(
            conn,
            content=content,
            memory_type=memory_type,
            summary_short=summary_short,
            source=source,
            importance_score=importance_score,
            confidence_score=confidence_score,
            tags=tags,
            project_key=project_key,
            conversation_key=conversation_key,
            visibility_scope="workspace",
            workspace_id=actor.workspace_id,
            owner_user_id=actor.user_id if owner_user_key else None,
            created_by_user_id=actor.user_id,
            last_modified_by_user_id=actor.user_id,
            sharing_policy="explicit",
        )
        timeline.record_timeline_event(
            conn,
            event_type="memory.scope_assigned",
            memory_id=int(memory["id"]),
            origin="multiuser_auto",
            actor_user_id=actor.user_id,
            workspace_id=actor.workspace_id,
            actor_type=actor.actor_type,
            payload={"visibility_scope": "workspace", "workspace_key": actor.workspace_key},
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "created", "memory": memory}


@mcp.tool
def get_workspace_info(workspace_key: str = "default") -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_workspace_info_payload(conn, workspace_key=workspace_key)
    finally:
        conn.close()


def get_memory_if_visible(memory_id: int, user_key: str, workspace_key: str = "default") -> dict[str, Any] | None:
    conn = get_db_connection()
    try:
        if _is_multiuser_feature_active(conn, MULTIUSER_SCOPE_RETRIEVAL_FLAG):
            actor = resolve_actor_context(conn, user_key=user_key, workspace_key=workspace_key)
            visibility_sql, visibility_params = build_memory_visibility_filter(actor)
            row = conn.execute(
                f"SELECT * FROM memories WHERE id = ? AND {visibility_sql} AND archived_at IS NULL",
                [int(memory_id), *visibility_params],
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ? AND COALESCE(activity_state, 'active') != 'archived'",
                (int(memory_id),),
            ).fetchone()
    finally:
        conn.close()
    return enrich_memory_dict(row_to_dict(row)) if row else None


@mcp.tool
def list_memories_for_user(
    user_key: str,
    workspace_key: str = "default",
    project_key: str | None = None,
    limit: int = 20,
    visibility_scope: str | None = None,
) -> dict[str, Any]:
    """
    Listuje wspomnienia widoczne dla danego uÄąÄ˝ytkownika (scope-aware retrieval).

    Filtruje wedÄąâ€šug reguÄąâ€š widocznoÄąâ€şci: private (wÄąâ€šasne) + workspace + project w workspace aktora.
    Wyniki sĂ„â€¦ rankowane: private > project > workspace > inne, a w ramach zakresu
    malejĂ„â€¦co po importance_score.
    """
    conn = get_db_connection()
    try:
        if not _is_multiuser_feature_active(conn, MULTIUSER_SCOPE_RETRIEVAL_FLAG):
            # Fallback do globalnego listowania bez filtra scope
            sql_fb, params_fb, filters_fb = _memory_query_parts(
                limit=limit,
                min_importance=0.0,
                sort_by="active",
                project_key=project_key,
                visibility_scope=visibility_scope,
            )
            rows_fb = conn.execute(sql_fb, params_fb).fetchall()
            items_fb = [enrich_memory_dict(row_to_dict(r)) for r in rows_fb]
            return {
                "count": len(items_fb),
                "items": items_fb,
                "filters": filters_fb,
                "actor": {"user_key": user_key, "workspace_key": workspace_key, "role_codes": []},
                "scope_retrieval_active": False,
            }

        actor = resolve_actor_context(
            conn,
            user_key=user_key,
            workspace_key=workspace_key,
            project_key=project_key,
        )
        sql, params, filters = _memory_query_parts(
            limit=limit,
            min_importance=0.0,
            sort_by="active",
            project_key=project_key,
            visibility_scope=visibility_scope,
            actor=actor,
        )
        rows = conn.execute(sql, params).fetchall()
        raw_items = [enrich_memory_dict(row_to_dict(row)) for row in rows]

        # Task 4.3: ranking zgodny ze scope Ă˘â‚¬â€ť private > project > workspace > inne
        _SCOPE_RANK = {"private": 0, "project": 1, "workspace": 2}

        def _scope_key(item: dict) -> tuple:
            scope = item.get("visibility_scope") or "other"
            rank = _SCOPE_RANK.get(scope, 3)
            importance = float(item.get("importance_score") or 0.0)
            return (rank, -importance)

        items = sorted(raw_items, key=_scope_key)
    finally:
        conn.close()
    return {
        "count": len(items),
        "items": items,
        "filters": filters,
        "actor": {
            "user_key": actor.user_key,
            "workspace_key": actor.workspace_key,
            "role_codes": actor.role_codes,
        },
        "scope_retrieval_active": True,
    }


@mcp.tool
def validate_migration_0010() -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return validate_migration_0010_payload(conn)
    finally:
        conn.close()


def _collect_version_lineage(conn, memory_id: int) -> list[dict[str, Any]]:
    return collect_version_lineage(
        conn,
        memory_id,
        require_memory_row=require_memory_row,
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
    )


@mcp.tool
def create_memory_draft(
    content: str,
    memory_type: str,
    summary_short: str | None = None,
    source: str | None = None,
    importance_score: float = 0.5,
    confidence_score: float = 0.5,
    tags: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    scope_code: str | None = None,
    parent_memory_id: int | None = None,
    project_key: str | None = None,
    conversation_key: str | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    review_due_at: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return create_memory_draft_payload(
            conn,
            content=content,
            memory_type=memory_type,
            summary_short=summary_short,
            source=source,
            importance_score=importance_score,
            confidence_score=confidence_score,
            tags=tags,
            layer_code=layer_code,
            area_code=area_code,
            scope_code=scope_code,
            parent_memory_id=parent_memory_id,
            project_key=project_key,
            conversation_key=conversation_key,
            owner_role=owner_role,
            owner_id=owner_id,
            review_due_at=review_due_at,
            cross_project_flag_key=CROSS_PROJECT_FLAG_KEY,
            normalize_scope_code=normalize_scope_code,
            normalize_layer_code=normalize_layer_code,
            normalize_area_code=normalize_area_code,
            normalize_optional_text=normalize_optional_text,
            require_feature_flag_write_access=_require_feature_flag_write_access,
            insert_memory=_insert_memory,
            insert_memory_event=_insert_memory_event,
        )
    finally:
        conn.close()


@mcp.tool
def list_review_queue(
    limit: int = 20,
    memory_type: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    parent_memory_id: int | None = None,
    sort_by: str = "recent",
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_review_queue_payload(
            conn,
            limit=limit,
            memory_type=memory_type,
            scope_code=scope_code,
            project_key=project_key,
            layer_code=layer_code,
            area_code=area_code,
            tag=tag,
            text_query=text_query,
            parent_memory_id=parent_memory_id,
            sort_by=sort_by,
            effective_owner_key=effective_owner_key,
            effective_owner_type=effective_owner_type,
            memory_query_parts=_memory_query_parts,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
            apply_effective_owner=_apply_effective_owner,
            filter_items_by_effective_owner=_filter_items_by_effective_owner,
        )
    finally:
        conn.close()


@mcp.tool
def approve_memory(
    memory_id: int,
    validation_source: str | None = "manual_review",
    scope_code: str | None = None,
    importance_score: float | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    revalidation_due_at: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return approve_memory_payload(
            conn,
            memory_id=memory_id,
            validation_source=validation_source,
            scope_code=scope_code,
            importance_score=importance_score,
            owner_role=owner_role,
            owner_id=owner_id,
            revalidation_due_at=revalidation_due_at,
            require_memory_row=require_memory_row,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            normalize_scope_code=normalize_scope_code,
            normalize_optional_text=normalize_optional_text,
            normalize_score=normalize_score,
            utc_now_iso=utc_now_iso,
            utc_offset_days_iso=utc_offset_days_iso,
            shift_iso_days=shift_iso_days,
            compute_sla_days=_compute_sla_days,
            default_owner_role=_default_owner_role,
            quality_gate_issues_for_memory=_quality_gate_issues_for_memory,
            insert_memory_event=_insert_memory_event,
            apply_ownership_defaults=_apply_ownership_defaults,
        )
    finally:
        conn.close()


def _tag_count(tags: str | None) -> int:
    return tag_count(tags, normalize_optional_text=normalize_optional_text)


def _quality_gate_issues_for_memory(memory: dict[str, Any], *, target_scope_code: str | None = None) -> list[str]:
    return quality_gate_issues_for_memory(
        memory,
        target_scope_code=target_scope_code,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
    )


@mcp.tool
def preview_memory_quality_gate(memory_id: int, target_scope_code: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return preview_memory_quality_gate_payload(
            conn,
            memory_id=memory_id,
            target_scope_code=target_scope_code,
            require_memory_row=require_memory_row,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            normalize_scope_code=normalize_scope_code,
            quality_gate_issues_for_memory=_quality_gate_issues_for_memory,
        )
    finally:
        conn.close()


def _insert_memory_event(conn, *, memory_id: int, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return insert_memory_event_payload(
        conn,
        memory_id=memory_id,
        event_type=event_type,
        payload=payload,
        utc_now_iso=utc_now_iso,
        normalize_required_text=normalize_required_text,
        row_to_dict=row_to_dict,
    )


def _memory_v2_transition_result(conn, memory_id: int, *, status: str, event: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    memory = _apply_effective_owner(conn, _apply_ownership_defaults(enrich_memory_dict(row_to_dict(row))))
    return {
        "status": status,
        "memory_id": int(memory_id),
        "memory": memory,
        "event": event,
    }


@mcp.tool
def confirm_memory_v2(memory_id: int, source: str | None = "manual_confirmation", notes: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        if not _is_memory_v2_feature_active(conn):
            return {"status": "disabled", "reason": "feature_flag_off", "flag_key": MEMORY_V2_FLAG_KEY}
        memory = require_memory_row(conn, int(memory_id))
        previous = enrich_memory_dict(row_to_dict(memory))
        now_iso = utc_now_iso()
        conn.execute(
            """
            UPDATE memories
            SET updated_at = ?,
                last_confirmed_at = ?,
                last_validated_at = COALESCE(last_validated_at, ?),
                validation_source = ?,
                memory_v2_status = 'active',
                requires_user_confirmation = 0
            WHERE id = ?
            """,
            (now_iso, now_iso, now_iso, normalize_optional_text(source) or "manual_confirmation", int(memory_id)),
        )
        event = _insert_memory_event(
            conn,
            memory_id=int(memory_id),
            event_type="memory_v2.confirmed",
            payload={
                "old_status": previous.get("memory_v2_status"),
                "new_status": "active",
                "notes": normalize_optional_text(notes),
                "source": normalize_optional_text(source) or "manual_confirmation",
            },
        )
        conn.commit()
        return _memory_v2_transition_result(conn, int(memory_id), status="confirmed", event=event)
    finally:
        conn.close()


@mcp.tool
def mark_memory_stale(memory_id: int, source: str | None = "manual_review", notes: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        if not _is_memory_v2_feature_active(conn):
            return {"status": "disabled", "reason": "feature_flag_off", "flag_key": MEMORY_V2_FLAG_KEY}
        memory = require_memory_row(conn, int(memory_id))
        previous = enrich_memory_dict(row_to_dict(memory))
        now_iso = utc_now_iso()
        conn.execute(
            """
            UPDATE memories
            SET updated_at = ?,
                validation_source = ?,
                memory_v2_status = 'stale',
                requires_user_confirmation = 1
            WHERE id = ?
            """,
            (now_iso, normalize_optional_text(source) or "manual_review", int(memory_id)),
        )
        event = _insert_memory_event(
            conn,
            memory_id=int(memory_id),
            event_type="memory_v2.marked_stale",
            payload={
                "old_status": previous.get("memory_v2_status"),
                "new_status": "stale",
                "notes": normalize_optional_text(notes),
                "source": normalize_optional_text(source) or "manual_review",
            },
        )
        conn.commit()
        return _memory_v2_transition_result(conn, int(memory_id), status="stale", event=event)
    finally:
        conn.close()


@mcp.tool
def archive_memory_v2(memory_id: int, source: str | None = "manual_archive", notes: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        if not _is_memory_v2_feature_active(conn):
            return {"status": "disabled", "reason": "feature_flag_off", "flag_key": MEMORY_V2_FLAG_KEY}
        memory = require_memory_row(conn, int(memory_id))
        previous = enrich_memory_dict(row_to_dict(memory))
        now_iso = utc_now_iso()
        conn.execute(
            """
            UPDATE memories
            SET updated_at = ?,
                validation_source = ?,
                activity_state = 'archived',
                archived_at = COALESCE(archived_at, ?),
                state_code = 'archived',
                memory_v2_status = 'archived'
            WHERE id = ?
            """,
            (now_iso, normalize_optional_text(source) or "manual_archive", now_iso, int(memory_id)),
        )
        event = _insert_memory_event(
            conn,
            memory_id=int(memory_id),
            event_type="memory_v2.archived",
            payload={
                "old_status": previous.get("memory_v2_status"),
                "new_status": "archived",
                "notes": normalize_optional_text(notes),
                "source": normalize_optional_text(source) or "manual_archive",
            },
        )
        conn.commit()
        return _memory_v2_transition_result(conn, int(memory_id), status="archived", event=event)
    finally:
        conn.close()


@mcp.tool
def supersede_memory_v2(
    memory_id: int,
    new_memory_id: int,
    source: str | None = "manual_supersede",
    notes: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        if not _is_memory_v2_feature_active(conn):
            return {"status": "disabled", "reason": "feature_flag_off", "flag_key": MEMORY_V2_FLAG_KEY}
        require_memory_row(conn, int(memory_id))
        require_memory_row(conn, int(new_memory_id))
        now_iso = utc_now_iso()
        conn.execute(
            """
            UPDATE memories
            SET updated_at = ?,
                validation_source = ?,
                state_code = 'superseded',
                memory_v2_status = 'superseded',
                superseded_by_memory_id = ?,
                valid_to = COALESCE(valid_to, ?)
            WHERE id = ?
            """,
            (now_iso, normalize_optional_text(source) or "manual_supersede", int(new_memory_id), now_iso, int(memory_id)),
        )
        event = _insert_memory_event(
            conn,
            memory_id=int(memory_id),
            event_type="memory_v2.superseded",
            payload={
                "new_memory_id": int(new_memory_id),
                "notes": normalize_optional_text(notes),
                "source": normalize_optional_text(source) or "manual_supersede",
            },
        )
        conn.commit()
        return _memory_v2_transition_result(conn, int(memory_id), status="superseded", event=event)
    finally:
        conn.close()


@mcp.tool
def add_validation_event(
    memory_id: int,
    verdict: str,
    notes: str | None = None,
    source: str | None = "manual_review",
    confidence_score: float | None = None,
    importance_score: float | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    review_due_at: str | None = None,
    revalidation_due_at: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return add_validation_event_payload(
            conn,
            memory_id=memory_id,
            verdict=verdict,
            notes=notes,
            source=source,
            confidence_score=confidence_score,
            importance_score=importance_score,
            owner_role=owner_role,
            owner_id=owner_id,
            review_due_at=review_due_at,
            revalidation_due_at=revalidation_due_at,
            normalize_optional_text=normalize_optional_text,
            normalize_score=normalize_score,
            utc_now_iso=utc_now_iso,
            utc_offset_days_iso=utc_offset_days_iso,
            compute_sla_days=_compute_sla_days,
            default_owner_role=_default_owner_role,
            require_memory_row=require_memory_row,
            insert_memory_event=_insert_memory_event,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
        )
    finally:
        conn.close()


@mcp.tool
def list_validation_events(memory_id: int, limit: int = 20, verdict: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_validation_events_payload(
            conn,
            memory_id=memory_id,
            limit=limit,
            verdict=verdict,
            normalize_optional_text=normalize_optional_text,
            require_memory_row=require_memory_row,
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def list_revalidation_queue(
    limit: int = 20,
    validated_before: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_revalidation_queue_payload(
            conn,
            limit=limit,
            validated_before=validated_before,
            scope_code=scope_code,
            project_key=project_key,
            layer_code=layer_code,
            area_code=area_code,
            memory_type=memory_type,
            tag=tag,
            text_query=text_query,
            effective_owner_key=effective_owner_key,
            effective_owner_type=effective_owner_type,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            normalize_layer_code=normalize_layer_code,
            normalize_area_code=normalize_area_code,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
            apply_effective_owner=_apply_effective_owner,
            filter_items_by_effective_owner=_filter_items_by_effective_owner,
        )
    finally:
        conn.close()


@mcp.tool
def add_review_note(memory_id: int, notes: str, source: str | None = "manual_review") -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return add_review_note_payload(
            conn,
            memory_id=memory_id,
            notes=notes,
            source=source,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            require_memory_row=require_memory_row,
            insert_memory_event=_insert_memory_event,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
    finally:
        conn.close()


@mcp.tool
def list_review_events(memory_id: int, limit: int = 20, event_type: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_review_events_payload(
            conn,
            memory_id=memory_id,
            limit=limit,
            event_type=event_type,
            normalize_optional_text=normalize_optional_text,
            require_memory_row=require_memory_row,
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def reject_memory(memory_id: int, notes: str, source: str | None = "manual_review") -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return reject_memory_payload(
            conn,
            memory_id=memory_id,
            notes=notes,
            source=source,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            require_memory_row=require_memory_row,
            insert_memory_event=_insert_memory_event,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
        )
    finally:
        conn.close()


@mcp.tool
def return_memory_to_review(memory_id: int, notes: str | None = None, source: str | None = "manual_review") -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return return_memory_to_review_payload(
            conn,
            memory_id=memory_id,
            notes=notes,
            source=source,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            require_memory_row=require_memory_row,
            insert_memory_event=_insert_memory_event,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
        )
    finally:
        conn.close()


@mcp.tool
def list_memory_audit(memory_id: int, limit: int = 50, event_type_prefix: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_memory_audit_payload(
            conn,
            memory_id=memory_id,
            limit=limit,
            event_type_prefix=event_type_prefix,
            require_memory_row=require_memory_row,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
            normalize_optional_text=normalize_optional_text,
        )
    finally:
        conn.close()


@mcp.tool
def get_memory_provenance(memory_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_memory_provenance_payload(
            conn,
            memory_id=memory_id,
            require_memory_row=require_memory_row,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
            normalize_optional_text=normalize_optional_text,
        )
    finally:
        conn.close()


@mcp.tool
def preview_memory_provenance_backfill(
    project_key: str | None = None,
    sample_limit: int = 50,
) -> dict[str, Any]:
    """Preview evidence-bound repair of legacy provenance gaps without mutation."""
    conn = get_db_connection()
    try:
        result = build_provenance_backfill_preview(
            conn,
            project_key=project_key,
            sample_limit=max(1, min(int(sample_limit), 200)),
        )
        result.pop("_candidates", None)
        return result
    finally:
        conn.close()


@mcp.tool
def apply_memory_provenance_backfill(
    expected_preview_hash: str,
    project_key: str | None = None,
    applied_by: str = "operator",
    confirm_provenance_repair: bool = False,
) -> dict[str, Any]:
    """Apply a fresh provenance preview after creating a trusted SQLite backup."""
    if not confirm_provenance_repair:
        return {
            "status": "blocked",
            "reason": "confirm_provenance_repair_required",
            "mutations_performed": 0,
        }
    conn = get_db_connection()
    try:
        preview = build_provenance_backfill_preview(conn, project_key=project_key)
        normalized_expected = normalize_required_text(expected_preview_hash, "expected_preview_hash")
        if preview["preview_hash"] != normalized_expected:
            return {
                "status": "blocked",
                "reason": "preview_hash_mismatch",
                "expected_preview_hash": normalized_expected,
                "current_preview_hash": preview["preview_hash"],
                "candidate_count": preview["candidate_count"],
                "mutations_performed": 0,
            }

        backup_dir = Path(DATA_DIR) / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_now_iso().replace("-", "").replace(":", "").replace("T", "-").replace("Z", "")
        backup_path = backup_dir / f"mapi-memory-provenance-pre-{stamp}.db"
        backup_conn = sqlite3.connect(str(backup_path))
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()
        backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        backup_ref = f"{backup_path}|sha256:{backup_sha256}"

        result = apply_provenance_backfill(
            conn,
            expected_preview_hash=normalized_expected,
            project_key=project_key,
            applied_by=applied_by,
            backup_ref=backup_ref,
            insert_memory_event=_insert_memory_event,
            utc_now_iso=utc_now_iso,
        )
        updated_ids = list(result.pop("updated_memory_ids", []) or [])
        result["updated_memory_id_sample"] = updated_ids[:100]
        result["backup_path"] = str(backup_path)
        result["backup_sha256"] = backup_sha256
        return result
    finally:
        conn.close()


@mcp.tool
def create_memory_version(
    memory_id: int,
    content: str | None = None,
    summary_short: str | None = None,
    source: str | None = None,
    importance_score: float | None = None,
    confidence_score: float | None = None,
    tags: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        current_row = require_memory_row(conn, int(memory_id))
        current_memory = _apply_ownership_defaults(enrich_memory_dict(row_to_dict(current_row)))
        created_source = normalize_optional_text(source) or current_memory.get("source") or "manual_version"
        version_memory = _insert_memory(
            conn,
            content=content or current_memory["content"],
            memory_type=current_memory["memory_type"],
            summary_short=summary_short if summary_short is not None else current_memory.get("summary_short"),
            source=created_source,
            importance_score=current_memory["importance_score"] if importance_score is None else float(importance_score),
            confidence_score=current_memory["confidence_score"] if confidence_score is None else float(confidence_score),
            tags=tags if tags is not None else current_memory.get("tags"),
            layer_code=current_memory.get("layer_code"),
            area_code=current_memory.get("area_code"),
            state_code="candidate",
            scope_code=current_memory.get("scope_code"),
            parent_memory_id=current_memory.get("parent_memory_id"),
            version=int(current_memory.get("version") or 1) + 1,
            supersedes_memory_id=int(memory_id),
            valid_from=current_memory.get("valid_from"),
            valid_to=None,
            decay_score=current_memory.get("decay_score") or 0.0,
            emotional_weight=current_memory.get("emotional_weight") or 0.0,
            identity_weight=current_memory.get("identity_weight") or 0.0,
            project_key=current_memory.get("project_key"),
            conversation_key=current_memory.get("conversation_key"),
            last_validated_at=None,
            validation_source=None,
            owner_role=current_memory.get("owner_role") or _default_owner_role(state_code="candidate", scope_code=current_memory.get("scope_code"), project_key=current_memory.get("project_key")),
            owner_id=current_memory.get("owner_id"),
            review_due_at=utc_offset_days_iso(_compute_sla_days(conn, "review", current_memory.get("priority") or "normal", current_memory.get("memory_type"), current_memory.get("scope_code"), current_memory.get("project_key"))),
        )
        event = _insert_memory_event(
            conn,
            memory_id=int(version_memory["id"]),
            event_type="version.created",
            payload={
                "source": created_source,
                "base_memory_id": int(memory_id),
                "base_version": int(current_memory.get("version") or 1),
                "new_version": int(version_memory.get("version") or 1),
            },
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "version_created",
        "base_memory_id": int(memory_id),
        "memory": version_memory,
        "event": event,
    }


@mcp.tool
def list_memory_versions(memory_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        versions = [_apply_ownership_defaults(item) for item in _collect_version_lineage(conn, int(memory_id))]
    finally:
        conn.close()
    return {
        "memory_id": int(memory_id),
        "count": len(versions),
        "items": versions,
    }


@mcp.tool
def deprecate_memory(
    memory_id: int,
    reason: str,
    source: str | None = "manual_review",
    replacement_memory_id: int | None = None,
    valid_to: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return deprecate_memory_payload(
            conn,
            memory_id=memory_id,
            reason=reason,
            source=source,
            replacement_memory_id=replacement_memory_id,
            valid_to=valid_to,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            shift_iso_days=shift_iso_days,
            require_memory_row=require_memory_row,
            insert_memory_event=_insert_memory_event,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
        )
    finally:
        conn.close()


def _memory_matches_operational_filters(
    memory: dict[str, Any],
    *,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
) -> bool:
    return memory_matches_operational_filters(
        memory,
        scope_code=scope_code,
        project_key=project_key,
        layer_code=layer_code,
        area_code=area_code,
        memory_type=memory_type,
        tag=tag,
        text_query=text_query,
        normalize_scope_code=normalize_scope_code,
        normalize_optional_text=normalize_optional_text,
        normalize_layer_code=normalize_layer_code,
        normalize_area_code=normalize_area_code,
    )


@mcp.tool
def list_expired_memories(
    limit: int = 20,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_expired_memories_payload(
            conn,
            limit=limit,
            as_of=as_of,
            scope_code=scope_code,
            project_key=project_key,
            layer_code=layer_code,
            area_code=area_code,
            memory_type=memory_type,
            tag=tag,
            text_query=text_query,
            effective_owner_key=effective_owner_key,
            effective_owner_type=effective_owner_type,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            normalize_layer_code=normalize_layer_code,
            normalize_area_code=normalize_area_code,
            utc_now_iso=utc_now_iso,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
            apply_effective_owner=_apply_effective_owner,
            filter_items_by_effective_owner=_filter_items_by_effective_owner,
        )
    finally:
        conn.close()


@mcp.tool
def list_duplicate_candidates_admin(
    limit: int = 20,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_duplicate_candidates_admin_payload(
            conn,
            limit=limit,
            scope_code=scope_code,
            project_key=project_key,
            layer_code=layer_code,
            area_code=area_code,
            memory_type=memory_type,
            tag=tag,
            text_query=text_query,
            effective_owner_key=effective_owner_key,
            effective_owner_type=effective_owner_type,
            get_duplicate_candidates=sandman_logic.get_duplicate_candidates,
            require_memory_row=require_memory_row,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
            apply_effective_owner=_apply_effective_owner,
            memory_matches_operational_filters=_memory_matches_operational_filters,
            get_or_create_duplicate_review_item=_get_or_create_duplicate_review_item,
            filter_items_by_effective_owner=_filter_items_by_effective_owner,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            normalize_layer_code=normalize_layer_code,
            normalize_area_code=normalize_area_code,
        )
    finally:
        conn.close()


def _owner_summary_from_items(items: list[dict[str, Any]], *, memory_field: str | None = None) -> dict[str, Any]:
    return owner_summary_from_items(items, memory_field=memory_field, normalize_optional_text=normalize_optional_text)


def _effective_owner_summary_from_items(items: list[dict[str, Any]], *, memory_field: str | None = None) -> dict[str, Any]:
    return effective_owner_summary_from_items(items, memory_field=memory_field, normalize_optional_text=normalize_optional_text)


def _filter_items_by_effective_owner(
    items: list[dict[str, Any]],
    *,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
    memory_field: str | None = None,
) -> list[dict[str, Any]]:
    return filter_items_by_effective_owner(
        items,
        effective_owner_key=effective_owner_key,
        effective_owner_type=effective_owner_type,
        memory_field=memory_field,
        normalize_optional_text=normalize_optional_text,
    )


def _recommended_bulk_actions(
    *,
    owner_summary: dict[str, Any],
    overdue_review_queue: dict[str, Any],
    overdue_revalidation_queue: dict[str, Any],
    overdue_expired_queue: dict[str, Any],
    overdue_duplicate_queue: dict[str, Any],
) -> list[dict[str, Any]]:
    return recommended_bulk_actions(
        owner_summary=owner_summary,
        overdue_review_queue=overdue_review_queue,
        overdue_revalidation_queue=overdue_revalidation_queue,
        overdue_expired_queue=overdue_expired_queue,
        overdue_duplicate_queue=overdue_duplicate_queue,
        normalize_optional_text=normalize_optional_text,
    )


@mcp.tool
def get_operational_queue_dashboard(
    limit_per_queue: int = 5,
    validated_before: str | None = None,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return operational_queue_dashboard_tool_payload(
            conn,
            limit_per_queue=limit_per_queue,
            validated_before=validated_before,
            as_of=as_of,
            scope_code=scope_code,
            project_key=project_key,
            layer_code=layer_code,
            area_code=area_code,
            memory_type=memory_type,
            tag=tag,
            text_query=text_query,
            effective_owner_key=effective_owner_key,
            effective_owner_type=effective_owner_type,
            cross_project_flag_key=CROSS_PROJECT_FLAG_KEY,
            list_review_queue=list_review_queue,
            list_revalidation_queue=list_revalidation_queue,
            list_expired_memories=list_expired_memories,
            list_duplicate_candidates_admin=list_duplicate_candidates_admin,
            list_overdue_review_queue=list_overdue_review_queue,
            list_overdue_revalidation_queue=list_overdue_revalidation_queue,
            list_overdue_expired_queue=list_overdue_expired_queue,
            list_overdue_duplicate_queue=list_overdue_duplicate_queue,
            get_owner_catalog_repair_summary=get_owner_catalog_repair_summary,
            operational_queue_dashboard_payload=operational_queue_dashboard_payload,
            get_feature_flag_config=_get_feature_flag_config,
            evaluate_feature_flag_config=_evaluate_feature_flag_config,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            normalize_layer_code=normalize_layer_code,
            normalize_area_code=normalize_area_code,
        )
    finally:
        conn.close()


def _accumulate_effective_owner_workload(
    buckets: dict[str, dict[str, Any]],
    items: list[dict[str, Any]],
    *,
    bucket_name: str,
    memory_field: str | None = None,
) -> None:
    accumulate_effective_owner_workload(
        buckets,
        items,
        bucket_name=bucket_name,
        memory_field=memory_field,
        normalize_optional_text=normalize_optional_text,
    )


@mcp.tool
def get_effective_owner_workload(
    limit: int = 50,
    validated_before: str | None = None,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
) -> dict[str, Any]:
    return effective_owner_workload_tool_payload(
        limit=limit,
        validated_before=validated_before,
        as_of=as_of,
        scope_code=scope_code,
        project_key=project_key,
        layer_code=layer_code,
        area_code=area_code,
        memory_type=memory_type,
        tag=tag,
        text_query=text_query,
        effective_owner_key=effective_owner_key,
        effective_owner_type=effective_owner_type,
        list_review_queue=list_review_queue,
        list_revalidation_queue=list_revalidation_queue,
        list_expired_memories=list_expired_memories,
        list_duplicate_candidates_admin=list_duplicate_candidates_admin,
        list_overdue_review_queue=list_overdue_review_queue,
        list_overdue_revalidation_queue=list_overdue_revalidation_queue,
        list_overdue_expired_queue=list_overdue_expired_queue,
        list_overdue_duplicate_queue=list_overdue_duplicate_queue,
        effective_owner_workload_payload=effective_owner_workload_payload,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
        normalize_layer_code=normalize_layer_code,
        normalize_area_code=normalize_area_code,
    )


def _rebalance_candidate_items(items: list[dict[str, Any]], *, memory_field: str | None = None) -> list[dict[str, Any]]:
    return rebalance_candidate_items(items, memory_field=memory_field)


@mcp.tool
def get_owner_rebalance_candidates(
    limit: int = 10,
    candidate_limit_per_queue: int = 10,
    overloaded_owner_key: str | None = None,
    validated_before: str | None = None,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
) -> dict[str, Any]:
    return owner_rebalance_candidates_payload(
        limit=limit,
        candidate_limit_per_queue=candidate_limit_per_queue,
        overloaded_owner_key=overloaded_owner_key,
        validated_before=validated_before,
        as_of=as_of,
        scope_code=scope_code,
        project_key=project_key,
        layer_code=layer_code,
        area_code=area_code,
        memory_type=memory_type,
        tag=tag,
        text_query=text_query,
        get_effective_owner_workload=get_effective_owner_workload,
        list_overdue_review_queue=list_overdue_review_queue,
        list_overdue_revalidation_queue=list_overdue_revalidation_queue,
        list_overdue_expired_queue=list_overdue_expired_queue,
        list_overdue_duplicate_queue=list_overdue_duplicate_queue,
        list_owner_role_mappings=list_owner_role_mappings,
        list_owner_directory_items=list_owner_directory_items,
        normalize_optional_text=normalize_optional_text,
    )


def _get_owner_catalog_health_data(
    conn,
    *,
    project_key: str | None = None,
    scope_code: str | None = None,
) -> dict[str, Any]:
    return get_owner_catalog_health_data(
        conn,
        project_key=project_key,
        scope_code=scope_code,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
        owner_directory_item_to_dict=_owner_directory_item_to_dict,
        owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
        owner_directory_governance_warnings=_owner_directory_governance_warnings,
        owner_mapping_governance_warnings=_owner_mapping_governance_warnings,
    )


@mcp.tool
def get_owner_catalog_health(
    project_key: str | None = None,
    scope_code: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_owner_catalog_health_payload(
            conn,
            project_key=project_key,
            scope_code=scope_code,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            owner_directory_item_to_dict=_owner_directory_item_to_dict,
            owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
            owner_directory_governance_warnings=_owner_directory_governance_warnings,
            owner_mapping_governance_warnings=_owner_mapping_governance_warnings,
        )
    finally:
        conn.close()


def _suggest_owner_mapping_repairs(
    conn,
    *,
    owner_role: str | None,
    owner_key: str | None,
    project_key: str | None,
    scope_code: str | None,
    reason: str | None,
) -> list[dict[str, Any]]:
    return suggest_owner_mapping_repairs(
        conn,
        owner_role=owner_role,
        owner_key=owner_key,
        project_key=project_key,
        scope_code=scope_code,
        reason=reason,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
        owner_directory_item_to_dict=_owner_directory_item_to_dict,
        owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
    )


@mcp.tool
def get_problematic_owner_mappings(
    limit: int = 50,
    project_key: str | None = None,
    scope_code: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_problematic_owner_mappings_payload(
            conn,
            limit=limit,
            project_key=project_key,
            scope_code=scope_code,
            kind=kind,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            owner_directory_item_to_dict=_owner_directory_item_to_dict,
            owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
            owner_directory_governance_warnings=_owner_directory_governance_warnings,
            owner_mapping_governance_warnings=_owner_mapping_governance_warnings,
        )
    finally:
        conn.close()


@mcp.tool
def repair_owner_mapping_issue(
    mapping_id: int,
    repair_kind: str,
    target_owner_key: str | None = None,
    owner_type: str | None = None,
    display_name: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return repair_owner_mapping_issue_payload(
            conn,
            mapping_id=mapping_id,
            repair_kind=repair_kind,
            target_owner_key=target_owner_key,
            owner_type=owner_type,
            display_name=display_name,
            notes=notes,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            owner_catalog_audit_project_key=_owner_catalog_audit_project_key,
            owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
            owner_directory_item_to_dict=_owner_directory_item_to_dict,
            record_project_event=timeline.record_project_event,
            timeline_rows_to_dicts=timeline.timeline_rows_to_dicts,
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def preview_bulk_repair_owner_mappings(
    mapping_ids: list[int],
    repair_kind: str,
    target_owner_key: str | None = None,
    owner_type: str | None = None,
    display_name: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return preview_bulk_repair_owner_mappings_payload(
            conn,
            mapping_ids=mapping_ids,
            repair_kind=repair_kind,
            target_owner_key=target_owner_key,
            owner_type=owner_type,
            display_name=display_name,
            notes=notes,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
            owner_directory_item_to_dict=_owner_directory_item_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def bulk_repair_owner_mappings(
    mapping_ids: list[int],
    repair_kind: str,
    target_owner_key: str | None = None,
    owner_type: str | None = None,
    display_name: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    return bulk_repair_owner_mappings_payload(
        mapping_ids=mapping_ids,
        repair_kind=repair_kind,
        target_owner_key=target_owner_key,
        owner_type=owner_type,
        display_name=display_name,
        notes=notes,
        normalize_required_text=normalize_required_text,
        utc_now_iso=utc_now_iso,
        owner_catalog_audit_project_key=_owner_catalog_audit_project_key,
        repair_owner_mapping_issue=repair_owner_mapping_issue,
        get_db_connection=get_db_connection,
        record_project_event=timeline.record_project_event,
        timeline_rows_to_dicts=timeline.timeline_rows_to_dicts,
        row_to_dict=row_to_dict,
    )


@mcp.tool
def get_owner_mapping_batch_candidates(
    limit: int = 20,
    max_groups: int = 10,
    project_key: str | None = None,
    scope_code: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_owner_mapping_batch_candidates_payload(
            conn,
            limit=limit,
            max_groups=max_groups,
            project_key=project_key,
            scope_code=scope_code,
            kind=kind,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            owner_directory_item_to_dict=_owner_directory_item_to_dict,
            owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
            owner_directory_governance_warnings=_owner_directory_governance_warnings,
            owner_mapping_governance_warnings=_owner_mapping_governance_warnings,
        )
    finally:
        conn.close()


@mcp.tool
def get_owner_catalog_repair_summary(
    project_key: str | None = None,
    scope_code: str | None = None,
    limit_recent_audits: int = 10,
    max_groups: int = 10,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_owner_catalog_repair_summary_payload(
            conn,
            project_key=project_key,
            scope_code=scope_code,
            limit_recent_audits=limit_recent_audits,
            max_groups=max_groups,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            owner_catalog_audit_project_key=_owner_catalog_audit_project_key,
            timeline_rows_to_dicts=timeline.timeline_rows_to_dicts,
            row_to_dict=row_to_dict,
            owner_directory_item_to_dict=_owner_directory_item_to_dict,
            owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
            owner_directory_governance_warnings=_owner_directory_governance_warnings,
            owner_mapping_governance_warnings=_owner_mapping_governance_warnings,
        )
    finally:
        conn.close()


def _owner_catalog_audit_project_key(project_key: str | None) -> str:
    return owner_catalog_audit_project_key(project_key, normalize_optional_text=normalize_optional_text)



@mcp.tool
def get_owner_catalog_governance_history(
    limit: int = 50,
    offset: int = 0,
    project_key: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_owner_catalog_governance_history_payload(
            conn,
            limit=limit,
            offset=offset,
            project_key=project_key,
            owner_catalog_audit_project_key=_owner_catalog_audit_project_key,
            timeline_rows_to_dicts=timeline.timeline_rows_to_dicts,
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()

@mcp.tool
def get_owner_mapping_repair_audit(
    limit: int = 50,
    offset: int = 0,
    project_key: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_owner_mapping_repair_audit_payload(
            conn,
            limit=limit,
            offset=offset,
            project_key=project_key,
            owner_catalog_audit_project_key=_owner_catalog_audit_project_key,
            timeline_rows_to_dicts=timeline.timeline_rows_to_dicts,
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_owner_governance_history(
    owner_key: str | None = None,
    owner_role: str | None = None,
    project_key: str | None = None,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Full audit trail for owner catalog: directory changes, mapping changes, repairs, target status changes.

    Parameters
    ----------
    owner_key:
        Filter events by owner_key (substring match in description).
    owner_role:
        Filter events by owner_role (substring match in description).
    project_key:
        Filter by timeline project_key.  None returns all projects.
    category:
        Narrow to a specific event category, e.g. "owner_directory_change",
        "owner_role_mapping_change", "owner_mapping_repair",
        "owner_mapping_bulk_repair", "owner_target_status_change".
        None returns all owner catalog categories.
    limit:
        Max items to return (default 50).
    offset:
        Pagination offset (default 0).
    """
    conn = get_db_connection()
    try:
        return get_owner_governance_history_payload(
            conn,
            owner_key=owner_key,
            owner_role=owner_role,
            project_key=project_key,
            category=category,
            limit=limit,
            offset=offset,
            normalize_optional_text=normalize_optional_text,
            timeline_rows_to_dicts=timeline.timeline_rows_to_dicts,
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Epic 3 (gap) Ă˘â‚¬â€ť Task 3.2: dedicated owner target activation / deactivation
# ---------------------------------------------------------------------------

_ALLOWED_OWNER_TYPES = ALLOWED_OWNER_TYPES


@mcp.tool
def set_owner_target_active(
    owner_key: str,
    is_active: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return set_owner_target_active_payload(
            conn,
            owner_key=owner_key,
            is_active=is_active,
            reason=reason,
            normalize_required_text=normalize_required_text,
            utc_now_iso=utc_now_iso,
            owner_catalog_audit_project_key=_owner_catalog_audit_project_key,
            owner_directory_item_to_dict=_owner_directory_item_to_dict,
            record_project_event=timeline.record_project_event,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Epic 4 Ă˘â‚¬â€ť Task 4.1-4.4: governance, rollout, validation, checklists
# ---------------------------------------------------------------------------

def _validate_owner_key_format(owner_key: str) -> list[str]:
    return validate_owner_key_format(owner_key)


@mcp.tool
def validate_new_owner_target(
    owner_key: str,
    owner_type: str,
    display_name: str,
    routing_metadata_json: str | None = None,
) -> dict[str, Any]:
    """
    Waliduje dane nowego targetu wÄąâ€šaÄąâ€şciciela przed dodaniem do katalogu.
    Sprawdza: format owner_key, dozwolony owner_type, wypeÄąâ€šnienie pÄ‚Ĺ‚l wymaganych,
    brak duplikatÄ‚Ĺ‚w w katalogu.
    Nie modyfikuje bazy Ă˘â‚¬â€ť wyÄąâ€šĂ„â€¦cznie operacja odczytu.
    """
    conn = get_db_connection()
    try:
        return validate_new_owner_target_payload(
            conn,
            owner_key=owner_key,
            owner_type=owner_type,
            display_name=display_name,
            routing_metadata_json=routing_metadata_json,
            owner_key_validator=_validate_owner_key_format,
            allowed_owner_types=_ALLOWED_OWNER_TYPES,
        )
    finally:
        conn.close()


@mcp.tool
def validate_project_override(
    project_key: str,
    owner_role: str,
    target_owner_key: str,
) -> dict[str, Any]:
    """
    Waliduje zamierzony project-level override mapowania wÄąâ€šaÄąâ€şciciela.
    Sprawdza: czy target istnieje i jest aktywny, czy istnieje globalne mapowanie dla tej roli,
    czy override nie jest redundantny (ten sam target co globalny).
    Nie modyfikuje bazy Ă˘â‚¬â€ť wyÄąâ€šĂ„â€¦cznie operacja odczytu.
    """
    conn = get_db_connection()
    try:
        return validate_project_override_payload(
            conn,
            project_key=project_key,
            owner_role=owner_role,
            target_owner_key=target_owner_key,
            normalize_required_text=normalize_required_text,
            owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def rollout_owner_catalog_to_project(
    project_key: str,
    mappings: list[dict[str, Any]],
    dry_run: bool = False,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return rollout_owner_catalog_to_project_payload(
            conn,
            project_key=project_key,
            mappings=mappings,
            dry_run=dry_run,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            owner_catalog_audit_project_key=_owner_catalog_audit_project_key,
            owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
            record_project_event=timeline.record_project_event,
        )
    finally:
        conn.close()


@mcp.tool
def get_owner_catalog_governance_checklist(
    operation: str,
    project_key: str | None = None,
) -> dict[str, Any]:
    """
    Zwraca listĂ„â„˘ kontrolnĂ„â€¦ (checklist) dla operacji na katalogu wÄąâ€šaÄąâ€şcicieli.
    operation: 'new_owner_target' | 'deactivate_target' | 'migrate_mappings' | 'rollout_project'
    KaÄąÄ˝dy element zawiera: id, description, required, tool_hint.
    """
    return get_owner_catalog_governance_checklist_payload(
        operation,
        project_key=project_key,
        normalize_optional_text=normalize_optional_text,
    )


@mcp.tool
def get_owner_rollout_summary(
    scope_code: str | None = None,
    include_health_check: bool = True,
) -> dict[str, Any]:
    """Summary of owner catalog rollout state across all projects.

    Shows which projects have their own override mappings vs. rely on the
    global fallback, and optionally runs a health check per project.

    Parameters
    ----------
    scope_code:
        Optional filter Ă˘â‚¬â€ť only include mappings that match this scope_code
        (or have no scope_code set).
    include_health_check:
        When True (default), runs get_owner_catalog_health per project and
        surfaces projects with attention-level problems.
    """
    conn = get_db_connection()
    try:
        return get_owner_rollout_summary_payload(
            conn,
            scope_code=scope_code,
            include_health_check=include_health_check,
            normalize_scope_code=normalize_scope_code,
            owner_role_mapping_to_dict=_owner_role_mapping_to_dict,
            get_owner_catalog_health_data=_get_owner_catalog_health_data,
        )
    finally:
        conn.close()


def _compute_days_overdue(due_at_iso: str, as_of_iso: str) -> int:
    return core_compute_days_overdue(due_at_iso, as_of_iso)


@mcp.tool
def run_escalation_check(
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    level2_threshold_days: int = 3,
    level3_threshold_days: int = 7,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Sprawdza wszystkie kolejki overdue i tworzy/aktualizuje wpisy w escalation_history.

    Poziomy eskalacji:
    - Level 1: item jest overdue (days_overdue < level2_threshold_days)
    - Level 2: powaÄąÄ˝nie overdue (days_overdue >= level2_threshold_days)
    - Level 3: krytycznie overdue (days_overdue >= level3_threshold_days) LUB brak ownera
    """
    normalized_as_of = normalize_optional_text(as_of) or utc_now_iso()
    normalized_scope = normalize_scope_code(scope_code)
    normalized_project_key = normalize_optional_text(project_key)
    if level2_threshold_days < 1:
        return {"status": "error", "error": 'level2_threshold_days musi byĂ„â€ˇ >= 1'}
    if level3_threshold_days <= level2_threshold_days:
        return {"status": "error", "error": 'level3_threshold_days musi byĂ„â€ˇ > level2_threshold_days'}

    overdue_review = list_overdue_review_queue(limit=1000, as_of=normalized_as_of, scope_code=normalized_scope, project_key=normalized_project_key)
    overdue_revalidation = list_overdue_revalidation_queue(limit=1000, as_of=normalized_as_of, scope_code=normalized_scope, project_key=normalized_project_key)
    overdue_expired = list_overdue_expired_queue(limit=1000, as_of=normalized_as_of, scope_code=normalized_scope, project_key=normalized_project_key)
    overdue_duplicate = list_overdue_duplicate_queue(limit=1000, as_of=normalized_as_of, scope_code=normalized_scope, project_key=normalized_project_key)

    queue_configs = [
        (overdue_review["items"], "memory", "review_due_at", "review_overdue"),
        (overdue_revalidation["items"], "memory", "revalidation_due_at", "revalidation_overdue"),
        (overdue_expired["items"], "memory", "expired_due_at", "expired_overdue"),
        (overdue_duplicate["items"], "duplicate_review_item", "duplicate_due_at", "duplicate_overdue"),
    ]

    escalations: list[dict[str, Any]] = []
    level1_count = level2_count = level3_count = 0

    conn = get_db_connection()
    try:
        now_iso = utc_now_iso()
        for items, entity_type, due_field, base_reason in queue_configs:
            for item in items:
                entity_id = int(item.get("id", 0))
                due_at = normalize_optional_text(item.get(due_field))
                if due_at is None:
                    continue
                days_overdue = _compute_days_overdue(due_at, normalized_as_of)
                owner_role = normalize_optional_text(item.get("owner_role"))
                priority = normalize_optional_text(item.get("priority")) or "normal"
                item_project_key = normalize_optional_text(item.get("project_key"))
                item_scope_code = normalize_optional_text(item.get("scope_code"))

                # Ustal poziom i reason
                reason = base_reason
                if owner_role is None:
                    level = max(2, 3 if days_overdue >= level3_threshold_days else 2)
                    reason = "owner_missing"
                elif days_overdue >= level3_threshold_days:
                    level = 3
                elif days_overdue >= level2_threshold_days:
                    level = 2
                else:
                    level = 1

                entry = {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "escalation_level": level,
                    "owner_role": owner_role,
                    "project_key": item_project_key,
                    "scope_code": item_scope_code,
                    "reason": reason,
                    "days_overdue": days_overdue,
                    "priority": priority,
                    "escalated_at": now_iso,
                }
                escalations.append(entry)

                if level == 1:
                    level1_count += 1
                elif level == 2:
                    level2_count += 1
                else:
                    level3_count += 1

                if not dry_run:
                    conn.execute(
                        """
                        INSERT INTO escalation_history
                            (escalation_level, entity_type, entity_id, owner_role, project_key,
                             scope_code, reason, days_overdue, priority, escalated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(entity_type, entity_id, escalation_level, reason)
                        DO UPDATE SET
                            days_overdue = excluded.days_overdue,
                            priority = excluded.priority,
                            escalated_at = excluded.escalated_at,
                            owner_role = excluded.owner_role
                        """,
                        (
                            level, entity_type, entity_id, owner_role, item_project_key,
                            item_scope_code, reason, days_overdue, priority, now_iso,
                        ),
                    )
                    timeline.record_project_event(
                        conn,
                        project_key=_owner_catalog_audit_project_key(item_project_key),
                        event_type="project.note_recorded",
                        title=f"Escalation level {level}: {entity_type} {entity_id}",
                        description=(
                            f"entity_type={entity_type}; entity_id={entity_id}; "
                            f"escalation_level={level}; reason={reason}; "
                            f"days_overdue={days_overdue}; priority={priority}"
                        ),
                        origin="system",
                        tags=["escalation", f"escalation.level_{level}"],
                        status="completed",
                        canonical=True,
                        category="escalation",
                        now_fn=utc_now_iso,
                    )

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    return {
        "status": "ok",
        "summary": {
            "level1_count": level1_count,
            "level2_count": level2_count,
            "level3_count": level3_count,
            "total": len(escalations),
            "dry_run": dry_run,
        },
        "escalations": escalations,
        "filters": {
            "as_of": normalized_as_of,
            "scope_code": normalized_scope,
            "project_key": normalized_project_key,
            "level2_threshold_days": level2_threshold_days,
            "level3_threshold_days": level3_threshold_days,
        },
    }


@mcp.tool
def get_escalation_history(
    entity_type: str | None = None,
    entity_id: int | None = None,
    escalation_level: int | None = None,
    project_key: str | None = None,
    include_resolved: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Zwraca historiĂ„â„˘ eskalacji z opcjonalnym filtrowaniem."""
    conn = get_db_connection()
    try:
        return escalation_history_payload(
            conn,
            entity_type=entity_type,
            entity_id=entity_id,
            escalation_level=escalation_level,
            project_key=project_key,
            include_resolved=include_resolved,
            limit=limit,
            offset=offset,
            normalize_optional_text=normalize_optional_text,
        )
    finally:
        conn.close()


@mcp.tool
def get_escalation_dashboard(
    project_key: str | None = None,
    scope_code: str | None = None,
) -> dict[str, Any]:
    """Dashboard eskalacji: podsumowanie pending escalations, najczĂ„â„˘stsze przyczyny, avg czas reakcji."""
    conn = get_db_connection()
    try:
        return escalation_dashboard_payload(
            conn,
            project_key=project_key,
            scope_code=scope_code,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
        )
    finally:
        conn.close()


@mcp.tool
def apply_escalation_reactions(
    project_key: str | None = None,
    scope_code: str | None = None,
    min_level: int = 2,
    owner_overload_threshold: int = 3,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Stosuje pÄ‚Ĺ‚Äąâ€šautomatyczne reakcje na aktywne eskalacje.

    Reakcje per poziom eskalacji:
    - Level 2: jeÄąâ€şli priority memory to 'low' lub 'normal' Ă˘â€ â€™ ustaw na 'high'
    - Level 3: ustaw priority na 'critical'; jeÄąâ€şli owner ma >= owner_overload_threshold
      aktywnych eskalacji level 3 Ă˘â€ â€™ emituj event 'owner_overloaded' w timeline

    DomyÄąâ€şlnie dry_run=True Ă˘â‚¬â€ť zwraca listĂ„â„˘ planowanych akcji bez ich wykonywania.
    Ustaw dry_run=False ÄąÄ˝eby faktycznie zastosowaĂ„â€ˇ reakcje.
    """
    _PRIORITY_ORDER = {"low": 0, "normal": 1, "high": 2, "critical": 3}
    _BOOST_MAP = {2: "high", 3: "critical"}

    if min_level not in (1, 2, 3):
        return {"status": "error", "error": 'min_level musi byĂ„â€ˇ 1, 2 lub 3'}

    normalized_project_key = normalize_optional_text(project_key)
    normalized_scope_code = normalize_scope_code(scope_code)

    conn = get_db_connection()
    try:
        # Pobierz aktywne eskalacje na odpowiednim poziomie
        sql = (
            "SELECT * FROM escalation_history "
            "WHERE resolved_at IS NULL AND escalation_level >= ? AND entity_type = 'memory'"
        )
        params: list[Any] = [min_level]
        if normalized_project_key is not None:
            sql += " AND project_key = ?"
            params.append(normalized_project_key)
        if normalized_scope_code is not None:
            sql += " AND scope_code = ?"
            params.append(normalized_scope_code)
        sql += " ORDER BY escalation_level DESC, days_overdue DESC"

        escalation_rows = conn.execute(sql, params).fetchall()

        # Zlicz level-3 per owner_role (do detekcji overload)
        overload_sql = (
            "SELECT owner_role, COUNT(*) as cnt FROM escalation_history "
            "WHERE resolved_at IS NULL AND escalation_level = 3 AND owner_role IS NOT NULL"
        )
        overload_params: list[Any] = []
        if normalized_project_key is not None:
            overload_sql += " AND project_key = ?"
            overload_params.append(normalized_project_key)
        overload_sql += " GROUP BY owner_role"
        overload_rows = conn.execute(overload_sql, overload_params).fetchall()
        overloaded_owners = {
            r["owner_role"]: int(r["cnt"])
            for r in overload_rows
            if int(r["cnt"]) >= owner_overload_threshold
        }

        planned_actions: list[dict[str, Any]] = []
        now_iso = utc_now_iso()

        for esc in escalation_rows:
            e = dict(esc)
            entity_id = int(e["entity_id"])
            level = int(e["escalation_level"])
            target_priority = _BOOST_MAP.get(level)
            if target_priority is None:
                continue

            # SprawdÄąĹź obecny priorytet memory
            mem_row = conn.execute(
                "SELECT id, priority, state_code FROM memories WHERE id = ? AND activity_state = 'active'",
                (entity_id,),
            ).fetchone()
            if mem_row is None:
                continue

            current_priority = str(mem_row["priority"] or "normal")
            current_order = _PRIORITY_ORDER.get(current_priority, 1)
            target_order = _PRIORITY_ORDER.get(target_priority, 2)

            if target_order > current_order:
                action = {
                    "action": "boost_priority",
                    "entity_type": "memory",
                    "entity_id": entity_id,
                    "current_priority": current_priority,
                    "target_priority": target_priority,
                    "escalation_level": level,
                    "reason": e.get("reason"),
                    "applied": False,
                }
                planned_actions.append(action)

                if not dry_run:
                    conn.execute(
                        "UPDATE memories SET priority = ?, last_accessed_at = ? WHERE id = ?",
                        (target_priority, now_iso, entity_id),
                    )
                    _insert_memory_event(
                        conn,
                        memory_id=entity_id,
                        event_type="priority.updated",
                        payload={
                            "priority": target_priority,
                            "reason": "escalation_reaction",
                            "escalation_level": level,
                        },
                    )
                    action["applied"] = True

        # Reakcje na overloaded owners
        owner_actions: list[dict[str, Any]] = []
        emitted_owners: set[str] = set()
        for owner_role_key, count in overloaded_owners.items():
            if owner_role_key in emitted_owners:
                continue
            emitted_owners.add(owner_role_key)
            owner_action = {
                "action": "flag_owner_overloaded",
                "owner_role": owner_role_key,
                "level3_escalation_count": count,
                "applied": False,
            }
            owner_actions.append(owner_action)

            if not dry_run:
                timeline.record_project_event(
                    conn,
                    project_key=_owner_catalog_audit_project_key(normalized_project_key),
                    event_type="project.note_recorded",
                    title=f"Owner overloaded: {owner_role_key}",
                    description=(
                        f"owner_role={owner_role_key}; level3_escalation_count={count}; "
                        f"threshold={owner_overload_threshold}"
                    ),
                    origin="system",
                    tags=["owner_overloaded", "escalation_reaction"],
                    status="completed",
                    canonical=True,
                    category="owner_overloaded",
                    now_fn=utc_now_iso,
                )
                owner_action["applied"] = True

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    all_actions = planned_actions + owner_actions
    applied_count = sum(1 for a in all_actions if a.get("applied"))
    return {
        "status": "ok",
        "summary": {
            "total_actions": len(all_actions),
            "priority_boosts": len(planned_actions),
            "owner_overload_flags": len(owner_actions),
            "applied": applied_count if not dry_run else 0,
            "dry_run": dry_run,
        },
        "actions": all_actions,
        "filters": {
            "project_key": normalized_project_key,
            "scope_code": normalized_scope_code,
            "min_level": min_level,
            "owner_overload_threshold": owner_overload_threshold,
        },
    }


@mcp.tool
def get_sla_policy_observability(
    queue_type: str | None = None,
    project_key: str | None = None,
    scope_code: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Raport odchyleÄąâ€ž polityk SLA: porÄ‚Ĺ‚wnuje skonfigurowane polityki z rzeczywistym stanem kolejek.

    Dla kaÄąÄ˝dej kombinacji (queue_type Ä‚â€” priority) zwraca:
    - policy_days: ile dni SLA wynika z polityki
    - total_items: liczba elementÄ‚Ĺ‚w z due_date w tej kombinacji
    - overdue_count: ile jest juÄąÄ˝ przeterminowanych
    - overdue_rate: procent przeterminowanych
    - assessment: 'too_aggressive' (>50% overdue) | 'too_loose' (<5% overdue, duÄąÄ˝o czasu) | 'ok'

    Wyniki pogrupowane per queue_type i priority.
    """
    conn = get_db_connection()
    try:
        return sla_policy_observability_payload(
            conn,
            queue_type=queue_type,
            project_key=project_key,
            scope_code=scope_code,
            as_of=as_of,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            utc_now_iso=utc_now_iso,
        )
    finally:
        conn.close()


def _safe_event_timestamp(value: str | None) -> float | None:
    return core_safe_event_timestamp(value, normalize_optional_text=normalize_optional_text)


@mcp.tool
def get_queue_observability_metrics(
    validated_before: str | None = None,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
    text_query: str | None = None,
) -> dict[str, Any]:
    return queue_observability_metrics_payload(
        validated_before=validated_before,
        as_of=as_of,
        scope_code=scope_code,
        project_key=project_key,
        layer_code=layer_code,
        area_code=area_code,
        memory_type=memory_type,
        tag=tag,
        effective_owner_key=effective_owner_key,
        effective_owner_type=effective_owner_type,
        text_query=text_query,
        cross_project_flag_key=CROSS_PROJECT_FLAG_KEY,
        get_db_connection=get_db_connection,
        list_review_queue=list_review_queue,
        list_revalidation_queue=list_revalidation_queue,
        list_expired_memories=list_expired_memories,
        list_duplicate_candidates_admin=list_duplicate_candidates_admin,
        list_overdue_review_queue=list_overdue_review_queue,
        list_overdue_revalidation_queue=list_overdue_revalidation_queue,
        list_overdue_expired_queue=list_overdue_expired_queue,
        list_overdue_duplicate_queue=list_overdue_duplicate_queue,
        get_owner_catalog_health_data=_get_owner_catalog_health_data,
        get_feature_flag_config=_get_feature_flag_config,
        evaluate_feature_flag_config=_evaluate_feature_flag_config,
        compatibility_feature_flag=compatibility_feature_flag,
        count_project_scope_mismatches=_count_project_scope_mismatches,
        safe_event_timestamp=_safe_event_timestamp,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
        normalize_layer_code=normalize_layer_code,
        normalize_area_code=normalize_area_code,
        row_to_dict=row_to_dict,
        enrich_memory_dict=enrich_memory_dict,
        apply_ownership_defaults=_apply_ownership_defaults,
    )


def _count_project_scope_mismatches(conn, *, project_key=None, scope_code=None, layer_code=None, area_code=None, memory_type=None, tag=None, text_query=None) -> int:
    return count_project_scope_mismatches(
        conn,
        project_key=project_key,
        scope_code=scope_code,
        layer_code=layer_code,
        area_code=area_code,
        memory_type=memory_type,
        tag=tag,
        text_query=text_query,
    )


def _project_scope_mismatch_rows(conn, *, project_key=None, limit=50):
    return project_scope_mismatch_rows(
        conn,
        project_key=project_key,
        limit=limit,
        normalize_optional_text=normalize_optional_text,
        row_to_dict=row_to_dict,
    )


def _escalation_stage(*, value: int, level1_threshold: int, level2_threshold: int, level3_threshold: int) -> dict[str, Any]:
    return escalation_stage(
        value=value,
        level1_threshold=level1_threshold,
        level2_threshold=level2_threshold,
        level3_threshold=level3_threshold,
    )


def _highest_escalation_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    return highest_escalation_summary(items)


@mcp.tool
def get_quality_alerts(
    validated_before: str | None = None,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    layer_code: str | None = None,
    area_code: str | None = None,
    memory_type: str | None = None,
    tag: str | None = None,
    text_query: str | None = None,
    max_review_queue: int = 10,
    max_revalidation_queue: int = 10,
    max_expired_queue: int = 5,
    max_duplicate_queue: int = 5,
    max_avg_approval_lead_seconds: float = 86400.0,
    max_overdue_review_count: int = 0,
    max_overdue_revalidation_count: int = 0,
    max_missing_owner_count: int = 0,
    max_overdue_review_count_level2: int = 3,
    max_overdue_review_count_level3: int = 7,
    max_overdue_revalidation_count_level2: int = 3,
    max_overdue_revalidation_count_level3: int = 7,
    max_missing_owner_count_level2: int = 2,
    max_missing_owner_count_level3: int = 5,
    max_overdue_expired_count: int = 0,
    max_overdue_expired_count_level2: int = 2,
    max_overdue_expired_count_level3: int = 5,
    max_overdue_duplicate_count: int = 0,
    max_overdue_duplicate_count_level2: int = 2,
    max_overdue_duplicate_count_level3: int = 5,
    max_owner_overdue_total: int = 2,
    max_owner_overdue_total_level2: int = 4,
    max_owner_overdue_total_level3: int = 7,
    max_broken_owner_mapping_count: int = 0,
    max_broken_owner_mapping_count_level2: int = 1,
    max_broken_owner_mapping_count_level3: int = 3,
    max_owner_catalog_governance_warning_count: int = 0,
    max_owner_catalog_governance_warning_count_level2: int = 3,
    max_owner_catalog_governance_warning_count_level3: int = 7,
    max_project_scope_mismatch_count: int = 0,
    max_project_scope_mismatch_count_level2: int = 2,
    max_project_scope_mismatch_count_level3: int = 5,
) -> dict[str, Any]:
    metrics = get_queue_observability_metrics(
        validated_before=validated_before,
        as_of=as_of,
        scope_code=scope_code,
        project_key=project_key,
        layer_code=layer_code,
        area_code=area_code,
        memory_type=memory_type,
        tag=tag,
        text_query=text_query,
    )
    owner_workload = get_effective_owner_workload(
        limit=200,
        validated_before=validated_before,
        as_of=as_of,
        scope_code=scope_code,
        project_key=project_key,
        layer_code=layer_code,
        area_code=area_code,
        memory_type=memory_type,
        tag=tag,
        text_query=text_query,
    )

    alerts: list[dict[str, Any]] = []
    backlogs = metrics["backlogs"]
    approval_metrics = metrics["approval_metrics"]

    if backlogs["review_queue_count"] > int(max_review_queue):
        alerts.append({"severity": "warning", "kind": "review_backlog", "value": backlogs["review_queue_count"], "threshold": int(max_review_queue)})
    if backlogs["revalidation_queue_count"] > int(max_revalidation_queue):
        alerts.append({"severity": "warning", "kind": "revalidation_backlog", "value": backlogs["revalidation_queue_count"], "threshold": int(max_revalidation_queue)})
    if backlogs["expired_queue_count"] > int(max_expired_queue):
        alerts.append({"severity": "warning", "kind": "expired_backlog", "value": backlogs["expired_queue_count"], "threshold": int(max_expired_queue)})
    if backlogs["duplicate_queue_count"] > int(max_duplicate_queue):
        alerts.append({"severity": "warning", "kind": "duplicate_backlog", "value": backlogs["duplicate_queue_count"], "threshold": int(max_duplicate_queue)})
    if approval_metrics["approval_lead_time_avg_seconds"] > float(max_avg_approval_lead_seconds):
        alerts.append({"severity": "warning", "kind": "approval_lead_time", "value": approval_metrics["approval_lead_time_avg_seconds"], "threshold": float(max_avg_approval_lead_seconds)})
    review_escalation = _escalation_stage(
        value=backlogs.get("overdue_review_count", 0),
        level1_threshold=max_overdue_review_count,
        level2_threshold=max_overdue_review_count_level2,
        level3_threshold=max_overdue_review_count_level3,
    )
    revalidation_escalation = _escalation_stage(
        value=backlogs.get("overdue_revalidation_count", 0),
        level1_threshold=max_overdue_revalidation_count,
        level2_threshold=max_overdue_revalidation_count_level2,
        level3_threshold=max_overdue_revalidation_count_level3,
    )
    owner_missing_escalation = _escalation_stage(
        value=metrics.get("inventory", {}).get("missing_owner_count", 0),
        level1_threshold=max_missing_owner_count,
        level2_threshold=max_missing_owner_count_level2,
        level3_threshold=max_missing_owner_count_level3,
    )
    expired_overdue_escalation = _escalation_stage(
        value=backlogs.get("overdue_expired_count", 0),
        level1_threshold=max_overdue_expired_count,
        level2_threshold=max_overdue_expired_count_level2,
        level3_threshold=max_overdue_expired_count_level3,
    )
    duplicate_overdue_escalation = _escalation_stage(
        value=backlogs.get("overdue_duplicate_count", 0),
        level1_threshold=max_overdue_duplicate_count,
        level2_threshold=max_overdue_duplicate_count_level2,
        level3_threshold=max_overdue_duplicate_count_level3,
    )
    top_owner_workload = owner_workload["items"][0] if owner_workload.get("items") else None
    owner_overdue_total = int((top_owner_workload or {}).get("overdue_total") or 0)
    owner_overloaded_escalation = _escalation_stage(
        value=owner_overdue_total,
        level1_threshold=max_owner_overdue_total,
        level2_threshold=max_owner_overdue_total_level2,
        level3_threshold=max_owner_overdue_total_level3,
    )
    broken_owner_mapping_escalation = _escalation_stage(
        value=metrics.get("inventory", {}).get("broken_owner_mapping_count", 0),
        level1_threshold=max_broken_owner_mapping_count,
        level2_threshold=max_broken_owner_mapping_count_level2,
        level3_threshold=max_broken_owner_mapping_count_level3,
    )
    owner_catalog_governance_escalation = _escalation_stage(
        value=metrics.get("inventory", {}).get("owner_catalog_governance_warning_count", 0),
        level1_threshold=max_owner_catalog_governance_warning_count,
        level2_threshold=max_owner_catalog_governance_warning_count_level2,
        level3_threshold=max_owner_catalog_governance_warning_count_level3,
    )
    project_scope_mismatch_escalation = _escalation_stage(
        value=metrics.get("inventory", {}).get("project_scope_mismatch_count", 0),
        level1_threshold=max_project_scope_mismatch_count,
        level2_threshold=max_project_scope_mismatch_count_level2,
        level3_threshold=max_project_scope_mismatch_count_level3,
    )

    if review_escalation["level"] > 0:
        alerts.append({"severity": review_escalation["severity"], "kind": "review_overdue", "value": backlogs.get("overdue_review_count", 0), "threshold": int(max_overdue_review_count), "escalation_level": review_escalation["level"], "escalation_stage": review_escalation["stage"], "runbook": "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OVERDUE_ESCALATION_RUNBOOK.md"})
    if revalidation_escalation["level"] > 0:
        alerts.append({"severity": revalidation_escalation["severity"], "kind": "revalidation_overdue", "value": backlogs.get("overdue_revalidation_count", 0), "threshold": int(max_overdue_revalidation_count), "escalation_level": revalidation_escalation["level"], "escalation_stage": revalidation_escalation["stage"], "runbook": "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OVERDUE_ESCALATION_RUNBOOK.md"})
    if owner_missing_escalation["level"] > 0:
        alerts.append({"severity": owner_missing_escalation["severity"], "kind": "owner_missing", "value": metrics.get("inventory", {}).get("missing_owner_count", 0), "threshold": int(max_missing_owner_count), "escalation_level": owner_missing_escalation["level"], "escalation_stage": owner_missing_escalation["stage"], "runbook": "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OVERDUE_ESCALATION_RUNBOOK.md"})
    if expired_overdue_escalation["level"] > 0:
        alerts.append({"severity": expired_overdue_escalation["severity"], "kind": "expired_overdue", "value": backlogs.get("overdue_expired_count", 0), "threshold": int(max_overdue_expired_count), "escalation_level": expired_overdue_escalation["level"], "escalation_stage": expired_overdue_escalation["stage"], "runbook": "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OVERDUE_ESCALATION_RUNBOOK.md"})
    if duplicate_overdue_escalation["level"] > 0:
        alerts.append({"severity": duplicate_overdue_escalation["severity"], "kind": "duplicate_overdue", "value": backlogs.get("overdue_duplicate_count", 0), "threshold": int(max_overdue_duplicate_count), "escalation_level": duplicate_overdue_escalation["level"], "escalation_stage": duplicate_overdue_escalation["stage"], "runbook": "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OVERDUE_ESCALATION_RUNBOOK.md"})
    if owner_overloaded_escalation["level"] > 0 and top_owner_workload is not None:
        alerts.append({"severity": owner_overloaded_escalation["severity"], "kind": "owner_overloaded", "value": owner_overdue_total, "threshold": int(max_owner_overdue_total), "escalation_level": owner_overloaded_escalation["level"], "escalation_stage": owner_overloaded_escalation["stage"], "effective_owner_key": top_owner_workload.get("effective_owner_key"), "effective_owner_type": top_owner_workload.get("effective_owner_type"), "effective_display_name": top_owner_workload.get("effective_display_name"), "total_count": int(top_owner_workload.get("total_count") or 0), "overdue_total": owner_overdue_total, "runbook": "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OVERDUE_ESCALATION_RUNBOOK.md"})
    if broken_owner_mapping_escalation["level"] > 0:
        alerts.append({"severity": broken_owner_mapping_escalation["severity"], "kind": "broken_owner_mapping", "value": metrics.get("inventory", {}).get("broken_owner_mapping_count", 0), "threshold": int(max_broken_owner_mapping_count), "escalation_level": broken_owner_mapping_escalation["level"], "escalation_stage": broken_owner_mapping_escalation["stage"], "runbook": "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OWNER_REBALANCE_RUNBOOK.md"})
    if owner_catalog_governance_escalation["level"] > 0:
        alerts.append({"severity": owner_catalog_governance_escalation["severity"], "kind": "owner_catalog_governance_warning", "value": metrics.get("inventory", {}).get("owner_catalog_governance_warning_count", 0), "threshold": int(max_owner_catalog_governance_warning_count), "escalation_level": owner_catalog_governance_escalation["level"], "escalation_stage": owner_catalog_governance_escalation["stage"], "runbook": "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OWNER_CATALOG_GOVERNANCE.md"})
    if project_scope_mismatch_escalation["level"] > 0:
        alerts.append({"severity": project_scope_mismatch_escalation["severity"], "kind": "project_scope_mismatch", "value": metrics.get("inventory", {}).get("project_scope_mismatch_count", 0), "threshold": int(max_project_scope_mismatch_count), "escalation_level": project_scope_mismatch_escalation["level"], "escalation_stage": project_scope_mismatch_escalation["stage"], "runbook": "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OWNER_REBALANCE_RUNBOOK.md"})

    escalation_areas = {
        "review_overdue": {**review_escalation, "value": backlogs.get("overdue_review_count", 0), "thresholds": {"level1": int(max_overdue_review_count), "level2": int(max_overdue_review_count_level2), "level3": int(max_overdue_review_count_level3)}},
        "revalidation_overdue": {**revalidation_escalation, "value": backlogs.get("overdue_revalidation_count", 0), "thresholds": {"level1": int(max_overdue_revalidation_count), "level2": int(max_overdue_revalidation_count_level2), "level3": int(max_overdue_revalidation_count_level3)}},
        "owner_missing": {**owner_missing_escalation, "value": metrics.get("inventory", {}).get("missing_owner_count", 0), "thresholds": {"level1": int(max_missing_owner_count), "level2": int(max_missing_owner_count_level2), "level3": int(max_missing_owner_count_level3)}},
        "expired_overdue": {**expired_overdue_escalation, "value": backlogs.get("overdue_expired_count", 0), "thresholds": {"level1": int(max_overdue_expired_count), "level2": int(max_overdue_expired_count_level2), "level3": int(max_overdue_expired_count_level3)}},
        "duplicate_overdue": {**duplicate_overdue_escalation, "value": backlogs.get("overdue_duplicate_count", 0), "thresholds": {"level1": int(max_overdue_duplicate_count), "level2": int(max_overdue_duplicate_count_level2), "level3": int(max_overdue_duplicate_count_level3)}},
        "owner_overloaded": {**owner_overloaded_escalation, "value": owner_overdue_total, "effective_owner_key": None if top_owner_workload is None else top_owner_workload.get("effective_owner_key"), "thresholds": {"level1": int(max_owner_overdue_total), "level2": int(max_owner_overdue_total_level2), "level3": int(max_owner_overdue_total_level3)}},
        "broken_owner_mapping": {**broken_owner_mapping_escalation, "value": metrics.get("inventory", {}).get("broken_owner_mapping_count", 0), "thresholds": {"level1": int(max_broken_owner_mapping_count), "level2": int(max_broken_owner_mapping_count_level2), "level3": int(max_broken_owner_mapping_count_level3)}},
        "owner_catalog_governance_warning": {**owner_catalog_governance_escalation, "value": metrics.get("inventory", {}).get("owner_catalog_governance_warning_count", 0), "thresholds": {"level1": int(max_owner_catalog_governance_warning_count), "level2": int(max_owner_catalog_governance_warning_count_level2), "level3": int(max_owner_catalog_governance_warning_count_level3)}},
        "project_scope_mismatch": {**project_scope_mismatch_escalation, "value": metrics.get("inventory", {}).get("project_scope_mismatch_count", 0), "thresholds": {"level1": int(max_project_scope_mismatch_count), "level2": int(max_project_scope_mismatch_count_level2), "level3": int(max_project_scope_mismatch_count_level3)}},
    }

    feature_flag_evaluation = metrics.get("feature_flag_evaluation") or {}
    feature_flag = metrics.get("feature_flag") or {}
    if not bool(feature_flag_evaluation.get("enabled", False)):
        alerts.append({"severity": "info", "kind": "feature_flag_disabled", "value": feature_flag_evaluation.get("reason"), "threshold": None})
    if bool(feature_flag_evaluation.get("read_only_mode", False)):
        alerts.append({"severity": "info", "kind": "feature_flag_read_only", "value": True, "threshold": None})

    escalation_summary = {"highest": _highest_escalation_summary(list(escalation_areas.values())), "areas": escalation_areas, "runbook": "docs/CROSS_PROJECT_KNOWLEDGE_LAYER_OVERDUE_ESCALATION_RUNBOOK.md"}

    return {
        "status": "ok" if not alerts else "attention",
        "alert_count": len(alerts),
        "alerts": alerts,
        "feature_flag": feature_flag,
        "feature_flag_evaluation": feature_flag_evaluation,
        "metrics": metrics,
        "owner_workload": owner_workload,
        "escalation_summary": escalation_summary,
        "thresholds": {
            "max_review_queue": int(max_review_queue),
            "max_revalidation_queue": int(max_revalidation_queue),
            "max_expired_queue": int(max_expired_queue),
            "max_duplicate_queue": int(max_duplicate_queue),
            "max_avg_approval_lead_seconds": float(max_avg_approval_lead_seconds),
            "max_overdue_review_count": int(max_overdue_review_count),
            "max_overdue_review_count_level2": int(max_overdue_review_count_level2),
            "max_overdue_review_count_level3": int(max_overdue_review_count_level3),
            "max_overdue_revalidation_count": int(max_overdue_revalidation_count),
            "max_overdue_revalidation_count_level2": int(max_overdue_revalidation_count_level2),
            "max_overdue_revalidation_count_level3": int(max_overdue_revalidation_count_level3),
            "max_missing_owner_count": int(max_missing_owner_count),
            "max_missing_owner_count_level2": int(max_missing_owner_count_level2),
            "max_missing_owner_count_level3": int(max_missing_owner_count_level3),
            "max_overdue_expired_count": int(max_overdue_expired_count),
            "max_overdue_expired_count_level2": int(max_overdue_expired_count_level2),
            "max_overdue_expired_count_level3": int(max_overdue_expired_count_level3),
            "max_overdue_duplicate_count": int(max_overdue_duplicate_count),
            "max_overdue_duplicate_count_level2": int(max_overdue_duplicate_count_level2),
            "max_overdue_duplicate_count_level3": int(max_overdue_duplicate_count_level3),
            "max_owner_overdue_total": int(max_owner_overdue_total),
            "max_owner_overdue_total_level2": int(max_owner_overdue_total_level2),
            "max_owner_overdue_total_level3": int(max_owner_overdue_total_level3),
            "max_broken_owner_mapping_count": int(max_broken_owner_mapping_count),
            "max_broken_owner_mapping_count_level2": int(max_broken_owner_mapping_count_level2),
            "max_broken_owner_mapping_count_level3": int(max_broken_owner_mapping_count_level3),
            "max_owner_catalog_governance_warning_count": int(max_owner_catalog_governance_warning_count),
            "max_owner_catalog_governance_warning_count_level2": int(max_owner_catalog_governance_warning_count_level2),
            "max_owner_catalog_governance_warning_count_level3": int(max_owner_catalog_governance_warning_count_level3),
            "max_project_scope_mismatch_count": int(max_project_scope_mismatch_count),
            "max_project_scope_mismatch_count_level2": int(max_project_scope_mismatch_count_level2),
            "max_project_scope_mismatch_count_level3": int(max_project_scope_mismatch_count_level3),
        },
    }


@mcp.tool
def list_project_scope_mismatches(project_key: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List active project memories that still have global or omitted scope without an explicit exception tag."""
    conn = get_db_connection()
    try:
        return list_project_scope_mismatches_payload(
            conn,
            project_key=project_key,
            limit=limit,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_memory_hygiene_inventory(
    project_key: str = "mapi",
    as_of: str | None = None,
    include_candidates: bool = False,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return memory_hygiene.hygiene_inventory(
            conn,
            project_key=project_key,
            as_of=as_of,
            include_candidates=include_candidates,
        )
    finally:
        conn.close()


@mcp.tool
def preview_memory_hygiene(
    project_key: str = "mapi",
    as_of: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return memory_hygiene.build_hygiene_preview(conn, project_key=project_key, as_of=as_of)
    finally:
        conn.close()


@mcp.tool
def apply_memory_hygiene(
    project_key: str,
    expected_preview_hash: str,
    applied_by: str,
    reason: str,
    backup_path: str,
    confirm_metadata_repair: bool,
    as_of: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = memory_hygiene.apply_hygiene_preview(
            conn,
            project_key=project_key,
            expected_preview_hash=expected_preview_hash,
            applied_by=applied_by,
            reason=reason,
            backup_path=backup_path,
            confirm_metadata_repair=confirm_metadata_repair,
            as_of=as_of,
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@mcp.tool
def get_memory_hygiene_run(run_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return memory_hygiene.get_hygiene_run(conn, run_id=run_id)
    finally:
        conn.close()


@mcp.tool
def preview_memory_hygiene_rollback(run_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return memory_hygiene.preview_hygiene_rollback(conn, run_id=run_id)
    finally:
        conn.close()


@mcp.tool
def rollback_memory_hygiene_run(
    run_id: int,
    expected_rollback_preview_hash: str,
    rolled_back_by: str,
    notes: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = memory_hygiene.rollback_hygiene_run(
            conn,
            run_id=run_id,
            expected_rollback_preview_hash=expected_rollback_preview_hash,
            rolled_back_by=rolled_back_by,
            notes=notes,
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@mcp.tool
def normalize_project_scope(memory_id: int, reviewed_by: str | None = None, reason: str | None = None) -> dict[str, Any]:
    """Normalize one active project memory from global/default scope to project scope and record an audit event."""
    conn = get_db_connection()
    try:
        row = require_memory_row(conn, int(memory_id))
        memory = row_to_dict(row)
        if normalize_optional_text(memory.get("project_key")) is None:
            return {"status": "skipped", "reason": "memory has no project_key", "memory": enrich_memory_dict(memory)}
        raw_scope_code = normalize_optional_text(memory.get("scope_code"))
        if normalize_scope_code(memory.get("scope_code")) != "global" and raw_scope_code is not None:
            return {"status": "skipped", "reason": "memory scope is neither global nor default-null", "memory": enrich_memory_dict(memory)}
        if _tags_allow_global_project_scope(memory.get("tags")):
            return {"status": "skipped", "reason": "allow-global-project-scope tag present", "memory": enrich_memory_dict(memory)}
        now = utc_now_iso()
        conn.execute(
            """
            UPDATE memories
            SET scope_code = 'project',
                layer_code = CASE WHEN layer_code IS NULL OR trim(layer_code) = '' OR layer_code = 'buffer' THEN 'projects' ELSE layer_code END,
                area_code = CASE WHEN area_code IS NULL OR trim(area_code) = '' THEN 'projects' ELSE area_code END,
                validation_source = COALESCE(validation_source, 'project_scope_quality_gate'),
                last_accessed_at = ?
            WHERE id = ? AND archived_at IS NULL
            """,
            (now, int(memory_id)),
        )
        try:
            timeline.record_timeline_event(
                conn,
                event_type="memory.scope_normalized",
                memory_id=int(memory_id),
                origin="project_scope_quality_gate",
                payload={
                    "from_scope_code": raw_scope_code or "<default-global>",
                    "to_scope_code": "project",
                    "reviewed_by": normalize_optional_text(reviewed_by),
                    "reason": normalize_optional_text(reason) or "project_key memory must not use global/default scope by default",
                },
            )
        except Exception:
            pass
        conn.commit()
        updated = require_memory_row(conn, int(memory_id))
        return {"status": "normalized", "memory": enrich_memory_dict(row_to_dict(updated))}
    finally:
        conn.close()


@mcp.tool
def set_memory_owner(memory_id: int, owner_role: str, owner_id: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return set_memory_owner_payload(
            conn,
            memory_id=memory_id,
            owner_role=owner_role,
            owner_id=owner_id,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            require_memory_row=require_memory_row,
            insert_memory_event=_insert_memory_event,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
        )
    finally:
        conn.close()


@mcp.tool
def set_memory_sla(memory_id: int, review_due_at: str | None = None, revalidation_due_at: str | None = None, expired_due_at: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return set_memory_sla_payload(
            conn,
            memory_id=memory_id,
            review_due_at=review_due_at,
            revalidation_due_at=revalidation_due_at,
            expired_due_at=expired_due_at,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            require_memory_row=require_memory_row,
            insert_memory_event=_insert_memory_event,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
        )
    finally:
        conn.close()


@mcp.tool
def bulk_set_memory_owner(memory_ids: list[int], owner_role: str, owner_id: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return bulk_set_memory_owner_payload(
            conn,
            memory_ids=memory_ids,
            owner_role=owner_role,
            owner_id=owner_id,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            require_memory_row=require_memory_row,
            insert_memory_event=_insert_memory_event,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
        )
    finally:
        conn.close()


@mcp.tool
def bulk_set_memory_sla(
    memory_ids: list[int],
    review_due_at: str | None = None,
    revalidation_due_at: str | None = None,
    expired_due_at: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return bulk_set_memory_sla_payload(
            conn,
            memory_ids=memory_ids,
            review_due_at=review_due_at,
            revalidation_due_at=revalidation_due_at,
            expired_due_at=expired_due_at,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            require_memory_row=require_memory_row,
            insert_memory_event=_insert_memory_event,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
        )
    finally:
        conn.close()


@mcp.tool
def set_memory_priority(memory_id: int, priority: str) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return set_memory_priority_payload(
            conn,
            memory_id=memory_id,
            priority=priority,
            normalize_required_text=normalize_required_text,
            utc_now_iso=utc_now_iso,
            require_memory_row=require_memory_row,
            insert_memory_event=_insert_memory_event,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
        )
    finally:
        conn.close()


@mcp.tool
def list_sla_policies(
    queue_type: str | None = None,
    priority: str | None = None,
    active_only: bool = True,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_sla_policies_payload(
            conn,
            queue_type=queue_type,
            priority=priority,
            active_only=active_only,
            normalize_optional_text=normalize_optional_text,
        )
    finally:
        conn.close()


@mcp.tool
def upsert_sla_policy(
    queue_type: str,
    sla_days: int,
    priority: str | None = None,
    memory_type: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    is_active: bool = True,
    notes: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return upsert_sla_policy_payload(
            conn,
            queue_type=queue_type,
            sla_days=sla_days,
            priority=priority,
            memory_type=memory_type,
            scope_code=scope_code,
            project_key=project_key,
            is_active=is_active,
            notes=notes,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            utc_now_iso=utc_now_iso,
            owner_catalog_audit_project_key=_owner_catalog_audit_project_key,
            record_project_event=timeline.record_project_event,
        )
    finally:
        conn.close()


@mcp.tool
def list_overdue_review_queue(
    limit: int = 20,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_overdue_memory_queue_payload(
            conn,
            limit=limit,
            as_of=as_of,
            scope_code=scope_code,
            project_key=project_key,
            owner_role=owner_role,
            owner_id=owner_id,
            effective_owner_key=effective_owner_key,
            effective_owner_type=effective_owner_type,
            state_code="candidate",
            due_column="review_due_at",
            queue_state="review_overdue",
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            utc_now_iso=utc_now_iso,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
            apply_effective_owner=_apply_effective_owner,
            filter_items_by_effective_owner=_filter_items_by_effective_owner,
        )
    finally:
        conn.close()


@mcp.tool
def list_overdue_revalidation_queue(
    limit: int = 20,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_overdue_memory_queue_payload(
            conn,
            limit=limit,
            as_of=as_of,
            scope_code=scope_code,
            project_key=project_key,
            owner_role=owner_role,
            owner_id=owner_id,
            effective_owner_key=effective_owner_key,
            effective_owner_type=effective_owner_type,
            state_code="validated",
            due_column="revalidation_due_at",
            queue_state="revalidation_overdue",
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            utc_now_iso=utc_now_iso,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
            apply_effective_owner=_apply_effective_owner,
            filter_items_by_effective_owner=_filter_items_by_effective_owner,
        )
    finally:
        conn.close()


@mcp.tool
def set_duplicate_candidate_sla(
    canonical_memory_id: int,
    duplicate_memory_id: int,
    duplicate_due_at: str | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    status: str = "open",
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return set_duplicate_candidate_sla_payload(
            conn,
            canonical_memory_id=canonical_memory_id,
            duplicate_memory_id=duplicate_memory_id,
            duplicate_due_at=duplicate_due_at,
            owner_role=owner_role,
            owner_id=owner_id,
            status=status,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            require_memory_row=require_memory_row,
            get_or_create_duplicate_review_item=_get_or_create_duplicate_review_item,
            insert_memory_event=_insert_memory_event,
            duplicate_review_item_to_dict=_duplicate_review_item_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def bulk_set_duplicate_candidate_sla(
    pairs: list[dict[str, int]],
    duplicate_due_at: str | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    status: str = "open",
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return bulk_set_duplicate_candidate_sla_payload(
            conn,
            pairs=pairs,
            duplicate_due_at=duplicate_due_at,
            owner_role=owner_role,
            owner_id=owner_id,
            status=status,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            require_memory_row=require_memory_row,
            get_or_create_duplicate_review_item=_get_or_create_duplicate_review_item,
            insert_memory_event=_insert_memory_event,
            duplicate_review_item_to_dict=_duplicate_review_item_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def list_overdue_expired_queue(
    limit: int = 20,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_overdue_memory_queue_payload(
            conn,
            limit=limit,
            as_of=as_of,
            scope_code=scope_code,
            project_key=project_key,
            owner_role=owner_role,
            owner_id=owner_id,
            effective_owner_key=effective_owner_key,
            effective_owner_type=effective_owner_type,
            state_code=None,
            due_column="expired_due_at",
            queue_state="expired_overdue",
            normalize_optional_text=normalize_optional_text,
            normalize_scope_code=normalize_scope_code,
            utc_now_iso=utc_now_iso,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            apply_ownership_defaults=_apply_ownership_defaults,
            apply_effective_owner=_apply_effective_owner,
            filter_items_by_effective_owner=_filter_items_by_effective_owner,
        )
    finally:
        conn.close()


@mcp.tool
def list_overdue_duplicate_queue(
    limit: int = 20,
    as_of: str | None = None,
    scope_code: str | None = None,
    project_key: str | None = None,
    owner_role: str | None = None,
    owner_id: str | None = None,
    effective_owner_key: str | None = None,
    effective_owner_type: str | None = None,
) -> dict[str, Any]:
    return list_overdue_duplicate_queue_payload(
        limit=limit,
        as_of=as_of,
        scope_code=scope_code,
        project_key=project_key,
        owner_role=owner_role,
        owner_id=owner_id,
        effective_owner_key=effective_owner_key,
        effective_owner_type=effective_owner_type,
        list_duplicate_candidates_admin=list_duplicate_candidates_admin,
        normalize_optional_text=normalize_optional_text,
        normalize_scope_code=normalize_scope_code,
        utc_now_iso=utc_now_iso,
    )


@mcp.tool
def link_memories(
    from_memory_id: int,
    to_memory_id: int,
    relation_type: str,
    weight: float = 0.5,
    origin: str | None = None,
    allow_legacy_unsafe: bool = False,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return link_memories_payload(
            conn,
            from_memory_id=from_memory_id,
            to_memory_id=to_memory_id,
            relation_type=relation_type,
            weight=weight,
            origin=origin,
            allow_legacy_unsafe=bool(allow_legacy_unsafe),
            new_operation_id=timeline.new_operation_id,
            create_link=_create_link,
        )
    finally:
        conn.close()


@mcp.tool
@idempotent_direct_mutation("direct:recall_memory", get_db_connection_resolver=lambda: get_db_connection)
def recall_memory(
    memory_id: int,
    strength: float = 0.1,
    recall_type: str = "manual",
    source: str | None = "mcp",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Record behavioral recall telemetry without changing durable importance."""
    conn = get_db_connection()
    try:
        return recall_memory_payload(
            conn,
            memory_id=memory_id,
            strength=strength,
            recall_type=recall_type,
            source=source,
            require_memory_row=require_memory_row,
            normalize_score=normalize_score,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            insert_memory_event=insert_memory_event,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_memory_recall_telemetry(
    memory_id: int,
    limit: int = 50,
    recall_type: str | None = None,
) -> dict[str, Any]:
    """Read append-only recall events and legacy unattributed recall count."""
    conn = get_db_connection()
    try:
        return get_memory_recall_telemetry_payload(
            conn,
            memory_id=memory_id,
            limit=limit,
            recall_type=recall_type,
            require_memory_row=require_memory_row,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
            memory_event_to_dict=memory_event_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def list_sleep_runs(limit: int = 20, status: str | None = None, mode: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_sleep_runs_payload(conn, limit=limit, status=status, mode=mode, row_to_dict=row_to_dict)
    finally:
        conn.close()


@mcp.tool
def get_sleep_run(run_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_sleep_run_payload(conn, run_id=run_id, require_sleep_run_row=require_sleep_run_row, row_to_dict=row_to_dict)
    finally:
        conn.close()


@mcp.tool
def get_sleep_run_actions(run_id: int, limit: int = 200) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_sleep_run_actions_payload(conn, run_id=run_id, limit=limit, require_sleep_run_row=require_sleep_run_row, row_to_dict=row_to_dict)
    finally:
        conn.close()


@mcp.tool
def list_memory_consolidation_apply_runs(
    project_key: str | None = None,
    proposal_id: str | None = None,
    status: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List read-only audit records for consolidation apply runs."""
    if limit < 1 or limit > 200:
        return {"status": "error", "schema_version": "memory_consolidation_apply_runs.v1", "error": "limit musi byc w zakresie 1..200"}

    normalized_project_key = normalize_optional_text(project_key)
    normalized_status = normalize_optional_text(status)
    normalized_created_after = normalize_optional_text(created_after)
    normalized_created_before = normalize_optional_text(created_before)
    normalized_proposal_id = _normalize_consolidation_proposal_id(proposal_id) if normalize_optional_text(proposal_id) is not None else None
    if normalize_optional_text(proposal_id) is not None and normalized_proposal_id is None:
        return {"status": "error", "schema_version": "memory_consolidation_apply_runs.v1", "error": "proposal_id is invalid"}

    conn = get_db_connection()
    try:
        sql = """
            SELECT *
            FROM sleep_runs
            WHERE mode = 'consolidation_apply_run'
        """
        params: list[Any] = []
        if normalized_project_key is not None:
            sql += " AND project_key = ?"
            params.append(normalized_project_key)
        if normalized_status is not None:
            sql += " AND status = ?"
            params.append(normalized_status)
        if normalized_created_after is not None:
            sql += " AND started_at >= ?"
            params.append(normalized_created_after)
        if normalized_created_before is not None:
            sql += " AND started_at <= ?"
            params.append(normalized_created_before)
        sql += " ORDER BY id DESC"
        rows = conn.execute(sql, params).fetchall()

        items: list[dict[str, Any]] = []
        unsupported_metrics: list[str] = []
        for row in rows:
            item = _consolidation_apply_run_record(conn, row_to_dict(row), include_details=False)
            if normalized_proposal_id is not None and item.get("proposal_id") != _consolidation_proposal_public_id(normalized_proposal_id):
                continue
            items.append(item)
            if len(items) >= int(limit):
                break

        if normalized_proposal_id is not None and not items:
            unsupported_metrics.append("requested proposal_id has no persisted consolidation_apply_run records")

        return {
            "status": "ok",
            "schema_version": "memory_consolidation_apply_runs.v1",
            "filters": {
                "project_key": normalized_project_key,
                "proposal_id": _consolidation_proposal_public_id(normalized_proposal_id) if normalized_proposal_id is not None else None,
                "status": normalized_status,
                "created_after": normalized_created_after,
                "created_before": normalized_created_before,
                "limit": int(limit),
            },
            "summary": {
                "total_returned": len(items),
            },
            "runs": items,
            "safety": {
                "read_only": True,
                "mutates_memory_entries": False,
            },
            "unsupported_metrics": unsupported_metrics,
        }
    finally:
        conn.close()


@mcp.tool
def get_memory_consolidation_apply_run(run_id: int) -> dict[str, Any]:
    """Return one read-only audit record for a consolidation apply run."""
    conn = get_db_connection()
    try:
        run_row = require_sleep_run_row(conn, int(run_id))
        run = row_to_dict(run_row)
        if str(run.get("mode")) != "consolidation_apply_run":
            return {
                "status": "error",
                "schema_version": "memory_consolidation_apply_run.v1",
                "error": "run_id does not point to consolidation_apply_run",
            }
        payload = _consolidation_apply_run_record(conn, run, include_details=True)
        run_status = payload.pop("status", None)
        return {"status": "ok", "run_status": run_status, **payload}
    finally:
        conn.close()


@mcp.tool
def get_memory_consolidation_lifecycle_report(
    project_key: str | None = None,
    proposal_id: str | None = None,
    proposal_status: str | None = None,
    include_completed: bool = True,
    include_rejected: bool = True,
    include_apply_runs: bool = True,
    include_rollback_details: bool = True,
    include_snapshot_integrity: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """Return a read-only lifecycle report for consolidation proposals and their apply/rollback audit chain."""
    if limit < 1 or limit > 200:
        return {"status": "error", "schema_version": "memory_consolidation_lifecycle_report.v1", "error": "limit musi byc w zakresie 1..200"}

    normalized_project_key = normalize_optional_text(project_key)
    normalized_proposal_id = _normalize_consolidation_proposal_id(proposal_id) if normalize_optional_text(proposal_id) is not None else None
    if normalize_optional_text(proposal_id) is not None and normalized_proposal_id is None:
        return {"status": "error", "schema_version": "memory_consolidation_lifecycle_report.v1", "error": "proposal_id is invalid"}

    normalized_proposal_status = (
        _normalize_consolidation_proposal_status(proposal_status)
        if normalize_optional_text(proposal_status) is not None
        else None
    )

    conn = get_db_connection()
    try:
        proposal_rows = _list_consolidation_proposal_rows(conn, project_key=normalized_project_key)
        proposals: list[dict[str, Any]] = []
        for item, review_item in proposal_rows:
            proposal = _build_consolidation_proposal_payload(conn, item, review_item=review_item)
            if normalized_proposal_id is not None and proposal["proposal_id"] != _consolidation_proposal_public_id(normalized_proposal_id):
                continue
            if normalized_proposal_status is not None and proposal.get("status") != normalized_proposal_status:
                continue
            if not include_rejected and proposal.get("status") == "rejected":
                continue
            proposals.append(proposal)

        run_rows = conn.execute(
            """
            SELECT *
            FROM sleep_runs
            WHERE mode = 'consolidation_apply_run'
            ORDER BY id DESC
            """
        ).fetchall()
        runs_by_proposal_id: dict[str, list[dict[str, Any]]] = {}
        unmatched_run_ids: list[int] = []
        for row in run_rows:
            record = _consolidation_apply_run_record(conn, row_to_dict(row), include_details=True)
            if normalized_project_key is not None and record.get("project_key") != normalized_project_key:
                continue
            record_proposal_id = normalize_optional_text(record.get("proposal_id"))
            if record_proposal_id is None:
                snapshot_row = conn.execute(
                    "SELECT proposal_memory_id FROM memory_consolidation_apply_snapshots WHERE run_id = ?",
                    (int(record["run_id"]),),
                ).fetchone()
                if snapshot_row is not None and snapshot_row["proposal_memory_id"] is not None:
                    record_proposal_id = _consolidation_proposal_public_id(int(snapshot_row["proposal_memory_id"]))
                    record["proposal_id"] = record_proposal_id
                    run_unsupported = list(record.get("unsupported_metrics") or [])
                    if "proposal_id was reconstructed from the stored apply snapshot for lifecycle report" not in run_unsupported:
                        run_unsupported.append("proposal_id was reconstructed from the stored apply snapshot for lifecycle report")
                    record["unsupported_metrics"] = run_unsupported
            if record_proposal_id is None:
                unmatched_run_ids.append(int(record["run_id"]))
                continue
            runs_by_proposal_id.setdefault(record_proposal_id, []).append(record)

        integrity_result: dict[str, Any] | None = None
        findings_by_apply_run_id: dict[int, list[dict[str, Any]]] = {}
        rollback_findings_by_apply_run_id: dict[int, list[dict[str, Any]]] = {}
        global_integrity_issue_counts: dict[str, int] = {}
        if include_snapshot_integrity:
            integrity_result = get_memory_consolidation_snapshot_integrity_report(
                project_key=normalized_project_key,
                include_debug=False,
            )
            for finding in integrity_result.get("findings") or []:
                run_id = finding.get("run_id")
                if run_id is None:
                    kind = str(finding.get("kind") or "unknown")
                    global_integrity_issue_counts[kind] = global_integrity_issue_counts.get(kind, 0) + 1
                    continue
                kind = str(finding.get("kind") or "")
                target = rollback_findings_by_apply_run_id if kind.startswith("rollback_") or kind in {
                    "legacy_rollback_without_snapshot",
                    "orphan_rollback_snapshot",
                    "duplicate_snapshots_for_rollback_run",
                    "duplicate_rollback_snapshots_for_apply_run",
                } else findings_by_apply_run_id
                target.setdefault(int(run_id), []).append(finding)

        items: list[dict[str, Any]] = []
        issue_counts: dict[str, int] = dict(global_integrity_issue_counts)
        report_unsupported_metrics = list((integrity_result or {}).get("unsupported_metrics") or [])
        if unmatched_run_ids:
            report_unsupported_metrics.append(
                "some consolidation_apply_run rows could not be linked to a unique proposal_id in the lifecycle report"
            )

        for proposal in proposals:
            proposal_public_id = str(proposal["proposal_id"])
            proposal_runs = runs_by_proposal_id.get(proposal_public_id, [])
            latest_run = proposal_runs[0] if proposal_runs else None
            item_issue_codes: list[str] = []
            item_unsupported_metrics = list(proposal.get("unsupported_metrics") or [])
            if latest_run is not None:
                item_unsupported_metrics.extend(
                    item
                    for item in (latest_run.get("unsupported_metrics") or [])
                    if item not in item_unsupported_metrics
                )
            apply_integrity_findings = list(findings_by_apply_run_id.get(int(latest_run["run_id"]), [])) if latest_run is not None else []
            rollback_integrity_findings = list(rollback_findings_by_apply_run_id.get(int(latest_run["run_id"]), [])) if latest_run is not None else []

            if proposal.get("status") == "approved" and latest_run is None:
                item_issue_codes.append("approved_without_apply_run")
            if latest_run is not None and latest_run.get("preview_snapshot_status") != "stored":
                item_issue_codes.append("apply_run_without_stored_apply_snapshot")
            if latest_run is not None and not normalize_optional_text((latest_run.get("preview_hash_guard") or {}).get("status")):
                item_issue_codes.append("missing_apply_guard_metadata")
            if apply_integrity_findings:
                item_issue_codes.append("apply_snapshot_integrity_issue")
            if latest_run is not None and bool(latest_run.get("rollback_available")):
                item_issue_codes.append("rollback_available_not_inspected")
            if (
                latest_run is not None
                and latest_run.get("rollback_run_id") is not None
                and latest_run.get("rollback", {}).get("rollback_preview_snapshot_status") != "stored"
            ):
                item_issue_codes.append("completed_rollback_without_stored_snapshot")
            if rollback_integrity_findings:
                item_issue_codes.append("rollback_snapshot_integrity_issue")
            if latest_run is not None and normalize_optional_text(latest_run.get("proposal_id")) != proposal_public_id:
                item_issue_codes.append("proposal_run_mismatch")

            integrity_status = "ok"
            combined_integrity_findings = apply_integrity_findings + rollback_integrity_findings
            if combined_integrity_findings:
                integrity_status = _snapshot_integrity_status_from_findings(combined_integrity_findings, [])

            if "apply_snapshot_integrity_issue" in item_issue_codes or "rollback_snapshot_integrity_issue" in item_issue_codes:
                operator_next_action = "resolve_integrity_issue"
            elif proposal.get("status") == "pending":
                operator_next_action = "review"
            elif proposal.get("status") == "approved" and latest_run is None:
                operator_next_action = "preview_apply"
            elif latest_run is not None and bool(latest_run.get("rollback_available")) and include_rollback_details:
                operator_next_action = "preview_rollback"
            elif latest_run is not None and latest_run.get("rollback_run_id") is not None and latest_run.get("rollback", {}).get("rollback_preview_snapshot_status") != "stored":
                operator_next_action = "resolve_integrity_issue"
            elif latest_run is not None:
                operator_next_action = "inspect_run"
            elif proposal.get("status") == "rejected":
                operator_next_action = "none"
            else:
                operator_next_action = "none"

            item = {
                "proposal_id": proposal_public_id,
                "proposal_type": proposal.get("proposal_type"),
                "proposal_status": proposal.get("status"),
                "project_key": proposal.get("project_key"),
                "summary": proposal.get("summary"),
                "source_memory_ids": list(proposal.get("source_memory_ids") or []),
                "target_memory_ids": list(proposal.get("target_memory_ids") or []),
                "review": {
                    "reviewed_by": proposal.get("reviewed_by"),
                    "reviewed_at": proposal.get("reviewed_at"),
                    "review_note": proposal.get("review_note"),
                },
                "apply": None,
                "rollback": None,
                "integrity": {
                    "status": integrity_status,
                    "issue_codes": item_issue_codes,
                    "apply_findings": apply_integrity_findings,
                    "rollback_findings": rollback_integrity_findings,
                },
                "operator_next_action": operator_next_action,
                "unsupported_metrics": list(dict.fromkeys(item_unsupported_metrics)),
            }

            if include_apply_runs and latest_run is not None:
                item["apply"] = {
                    "run_id": int(latest_run["run_id"]),
                    "run_status": latest_run.get("status"),
                    "applied_at": latest_run.get("applied_at"),
                    "apply_snapshot_status": latest_run.get("preview_snapshot_status"),
                    "apply_snapshot_hash": latest_run.get("preview_snapshot_hash"),
                    "apply_snapshot_hash_algorithm": latest_run.get("preview_snapshot_hash_algorithm"),
                    "preview_hash_guard": latest_run.get("preview_hash_guard"),
                    "rollback_available": bool(latest_run.get("rollback_available")),
                    "action_count": latest_run.get("action_count"),
                    "action_summary": dict(latest_run.get("action_summary") or {}),
                }
            elif not include_apply_runs:
                item["unsupported_metrics"].append("apply run details were disabled by include_apply_runs=false")

            rollback_payload = latest_run.get("rollback") if latest_run is not None else None
            if include_rollback_details and latest_run is not None:
                item["rollback"] = {
                    "status": rollback_payload.get("status") if isinstance(rollback_payload, dict) else None,
                    "rollback_run_id": latest_run.get("rollback_run_id"),
                    "rollback_preview_snapshot_status": rollback_payload.get("rollback_preview_snapshot_status") if isinstance(rollback_payload, dict) else None,
                    "rollback_preview_hash": rollback_payload.get("rollback_preview_hash") if isinstance(rollback_payload, dict) else None,
                    "rollback_preview_hash_algorithm": rollback_payload.get("rollback_preview_hash_algorithm") if isinstance(rollback_payload, dict) else None,
                    "guard": rollback_payload.get("guard") if isinstance(rollback_payload, dict) else None,
                }
            elif not include_rollback_details and latest_run is not None:
                item["unsupported_metrics"].append("rollback details were disabled by include_rollback_details=false")

            if not include_snapshot_integrity:
                item["integrity"]["status"] = "unsupported"
                item["integrity"]["apply_findings"] = []
                item["integrity"]["rollback_findings"] = []
                item["unsupported_metrics"].append("snapshot integrity details were disabled by include_snapshot_integrity=false")

            item["unsupported_metrics"] = list(dict.fromkeys(item["unsupported_metrics"]))
            if not include_completed and operator_next_action == "none":
                continue

            if len(items) >= int(limit):
                break
            items.append(item)
            for code in item_issue_codes:
                issue_counts[code] = issue_counts.get(code, 0) + 1

        summary = {
            "proposals_total": len(items),
            "pending_review": sum(1 for item in items if item.get("proposal_status") == "pending"),
            "approved_not_applied": sum(1 for item in items if item.get("proposal_status") == "approved" and item.get("apply") is None),
            "rejected": sum(1 for item in items if item.get("proposal_status") == "rejected"),
            "applied_runs": sum(1 for item in items if item.get("apply") is not None),
            "rollback_available": sum(1 for item in items if bool((item.get("apply") or {}).get("rollback_available"))),
            "rollback_completed": sum(1 for item in items if (item.get("rollback") or {}).get("rollback_run_id") is not None),
            "items_with_issues": sum(1 for item in items if item.get("integrity", {}).get("issue_codes")),
            "issues_total": sum(len(item.get("integrity", {}).get("issue_codes") or []) for item in items),
        }

        recommended_actions: list[str] = []
        if summary["pending_review"]:
            recommended_actions.append("Review pending consolidation proposals before they accumulate.")
        if summary["approved_not_applied"]:
            recommended_actions.append("Preview or apply approved proposals that have not produced an apply run yet.")
        if issue_counts.get("rollback_available_not_inspected"):
            recommended_actions.append("Inspect rollback-ready apply runs before deciding whether rollback is needed.")
        if summary["items_with_issues"]:
            recommended_actions.append("Resolve lifecycle integrity issues before trusting proposal/apply/rollback audit coverage.")

        return {
            "status": "ok",
            "schema_version": "memory_consolidation_lifecycle_report.v1",
            "filters": {
                "project_key": normalized_project_key,
                "proposal_id": _consolidation_proposal_public_id(normalized_proposal_id) if normalized_proposal_id is not None else None,
                "proposal_status": normalized_proposal_status,
                "include_completed": bool(include_completed),
                "include_rejected": bool(include_rejected),
                "include_apply_runs": bool(include_apply_runs),
                "include_rollback_details": bool(include_rollback_details),
                "include_snapshot_integrity": bool(include_snapshot_integrity),
                "limit": int(limit),
            },
            "summary": summary,
            "items": items,
            "issue_counts": issue_counts,
            "recommended_actions": recommended_actions,
            "safety": {
                "read_only": True,
                "mutates_memory_entries": False,
                "creates_runs": False,
            },
            "unsupported_metrics": list(dict.fromkeys(report_unsupported_metrics)),
        }
    finally:
        conn.close()


@mcp.tool
def preview_memory_consolidation_apply_rollback(run_id: int) -> dict[str, Any]:
    """Preview guarded rollback for one consolidation apply run without mutating data."""
    conn = get_db_connection()
    try:
        run_row = require_sleep_run_row(conn, int(run_id))
        run = row_to_dict(run_row)
        if str(run.get("mode")) != "consolidation_apply_run":
            return {
                "status": "unsupported",
                "schema_version": "memory_consolidation_apply_rollback_preview.v1",
                "run_id": int(run_id),
                "rollback_available": False,
                "blocking_reasons": ["run_id_is_not_consolidation_apply_run"],
                "rollback_preview_hash": None,
                "rollback_preview_hash_algorithm": _CONSOLIDATION_PREVIEW_HASH_ALGORITHM,
                "rollback_guard": {
                    "required_for_execute": True,
                    "field": "expected_rollback_preview_hash",
                },
                "affected_memory_ids": [],
                "actions": [],
                "unsupported_metrics": [],
                "safety": {
                    "read_only": True,
                    "mutates_memory_entries": False,
                    "creates_rollback_run": False,
                },
            }
        run_record = _consolidation_apply_run_record(conn, run, include_details=True)
        undo_preview = preview_undo_run(int(run_id))
        preview_payload = _build_consolidation_apply_rollback_preview_payload(
            run=run,
            run_record=run_record,
            undo_preview=undo_preview,
        )
        preview_hash = _consolidation_apply_rollback_preview_hash(preview_payload)
        return {
            "schema_version": "memory_consolidation_apply_rollback_preview.v1",
            "run_id": int(run_id),
            "status": preview_payload["status"],
            "rollback_available": preview_payload["rollback_available"],
            "blocking_reasons": preview_payload["blocking_reasons"],
            "rollback_preview_hash": preview_hash,
            "rollback_preview_hash_algorithm": _CONSOLIDATION_PREVIEW_HASH_ALGORITHM,
            "rollback_guard": {
                "required_for_execute": True,
                "field": "expected_rollback_preview_hash",
                "rollback_preview_hash": preview_hash,
                "rollback_preview_hash_algorithm": _CONSOLIDATION_PREVIEW_HASH_ALGORITHM,
            },
            "affected_memory_ids": preview_payload["affected_memory_ids"],
            "actions": preview_payload["actions"],
            "action_summary": preview_payload["action_summary"],
            "rollbackable_action_count": preview_payload["rollbackable_action_count"],
            "existing_rollback_run_id": preview_payload["existing_rollback_run_id"],
            "unsupported_metrics": preview_payload["unsupported_metrics"],
            "rollback_instruction": {
                "preview_tool": "preview_memory_consolidation_apply_rollback",
                "apply_tool": "rollback_memory_consolidation_apply_run",
                "run_id": int(run_id),
            },
            "safety": {
                "read_only": True,
                "mutates_memory_entries": False,
                "creates_rollback_run": False,
            },
        }
    finally:
        conn.close()


@mcp.tool
def rollback_memory_consolidation_apply_run(
    run_id: int,
    expected_rollback_preview_hash: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Execute guarded rollback for a consolidation apply run via the existing undo mechanism."""
    normalized_expected_hash = normalize_optional_text(expected_rollback_preview_hash)
    preview = preview_memory_consolidation_apply_rollback(int(run_id))

    if preview.get("status") == "unsupported":
        return {
            "status": "unsupported",
            "schema_version": "memory_consolidation_apply_rollback.v1",
            "run_id": int(run_id),
            "rollback_run_id": None,
            "expected_rollback_preview_hash": normalized_expected_hash,
            "actual_rollback_preview_hash": preview.get("rollback_preview_hash"),
            "blocking_reasons": list(preview.get("blocking_reasons") or []),
            "affected_memory_ids": list(preview.get("affected_memory_ids") or []),
            "unsupported_metrics": list(preview.get("unsupported_metrics") or []),
        }
    if normalized_expected_hash is None:
        return {
            "status": "blocked",
            "schema_version": "memory_consolidation_apply_rollback.v1",
            "run_id": int(run_id),
            "rollback_run_id": None,
            "expected_rollback_preview_hash": None,
            "actual_rollback_preview_hash": preview.get("rollback_preview_hash"),
            "blocking_reasons": ["missing_expected_rollback_preview_hash"],
            "affected_memory_ids": list(preview.get("affected_memory_ids") or []),
            "unsupported_metrics": list(preview.get("unsupported_metrics") or []),
        }

    current_preview_hash = normalize_optional_text(preview.get("rollback_preview_hash"))
    if current_preview_hash is None:
        unsupported_metrics = list(preview.get("unsupported_metrics") or [])
        if "rollback_preview_hash_unavailable" not in unsupported_metrics:
            unsupported_metrics.append("rollback_preview_hash_unavailable")
        return {
            "status": "unsupported",
            "schema_version": "memory_consolidation_apply_rollback.v1",
            "run_id": int(run_id),
            "rollback_run_id": None,
            "expected_rollback_preview_hash": normalized_expected_hash,
            "actual_rollback_preview_hash": None,
            "blocking_reasons": list(preview.get("blocking_reasons") or []),
            "affected_memory_ids": list(preview.get("affected_memory_ids") or []),
            "unsupported_metrics": unsupported_metrics,
        }
    if normalized_expected_hash != current_preview_hash:
        return {
            "status": "blocked",
            "schema_version": "memory_consolidation_apply_rollback.v1",
            "run_id": int(run_id),
            "rollback_run_id": None,
            "expected_rollback_preview_hash": normalized_expected_hash,
            "actual_rollback_preview_hash": current_preview_hash,
            "blocking_reasons": ["expected_rollback_preview_hash_mismatch"],
            "affected_memory_ids": list(preview.get("affected_memory_ids") or []),
            "unsupported_metrics": list(preview.get("unsupported_metrics") or []),
        }
    if preview.get("status") != "ready":
        return {
            "status": "blocked",
            "schema_version": "memory_consolidation_apply_rollback.v1",
            "run_id": int(run_id),
            "rollback_run_id": None,
            "expected_rollback_preview_hash": normalized_expected_hash,
            "actual_rollback_preview_hash": current_preview_hash,
            "blocking_reasons": list(preview.get("blocking_reasons") or ["rollback_not_ready"]),
            "affected_memory_ids": list(preview.get("affected_memory_ids") or []),
            "unsupported_metrics": list(preview.get("unsupported_metrics") or []),
        }

    undo_result = undo_run(int(run_id), notes=notes)
    if undo_result.get("status") != "completed":
        return {
            "status": "failed",
            "schema_version": "memory_consolidation_apply_rollback.v1",
            "run_id": int(run_id),
            "rollback_run_id": undo_result.get("rollback_run_id"),
            "expected_rollback_preview_hash": normalized_expected_hash,
            "actual_rollback_preview_hash": current_preview_hash,
            "blocking_reasons": [],
            "affected_memory_ids": list(preview.get("affected_memory_ids") or []),
            "unsupported_metrics": list(preview.get("unsupported_metrics") or []),
            "undo_error": undo_result.get("error"),
        }
    rollback_run_id = int(undo_result["rollback_run_id"])
    stored_at = utc_now_iso()
    conn = get_db_connection()
    try:
        _store_consolidation_rollback_preview_snapshot(
            conn,
            original_apply_run_id=int(run_id),
            rollback_run_id=rollback_run_id,
            snapshot=_build_consolidation_apply_rollback_preview_snapshot(
                preview,
                original_apply_run_id=int(run_id),
                rollback_run_id=rollback_run_id,
                stored_at=stored_at,
                expected_rollback_preview_hash=normalized_expected_hash,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "rolled_back",
        "schema_version": "memory_consolidation_apply_rollback.v1",
        "run_id": int(run_id),
        "rollback_run_id": rollback_run_id,
        "expected_rollback_preview_hash": normalized_expected_hash,
        "actual_rollback_preview_hash": current_preview_hash,
        "blocking_reasons": [],
        "affected_memory_ids": list(preview.get("affected_memory_ids") or []),
        "unsupported_metrics": list(preview.get("unsupported_metrics") or []),
        "restored_count": undo_result.get("restored_count"),
        "restored_items": list(undo_result.get("restored_items") or []),
        "rollback_preview_snapshot_status": "stored",
        "rollback_preview_hash": current_preview_hash,
        "rollback_preview_hash_algorithm": _CONSOLIDATION_PREVIEW_HASH_ALGORITHM,
    }


@mcp.tool
def get_memory_consolidation_snapshot_integrity_report(
    project_key: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Report whether stored consolidation apply and rollback preview snapshots are complete and auditable."""
    normalized_project_key = normalize_optional_text(project_key)
    conn = get_db_connection()
    try:
        run_sql = """
            SELECT *
            FROM sleep_runs
            WHERE mode = 'consolidation_apply_run'
        """
        run_params: list[Any] = []
        if normalized_project_key is not None:
            run_sql += " AND project_key = ?"
            run_params.append(normalized_project_key)
        run_sql += " ORDER BY id DESC"
        run_rows = conn.execute(run_sql, run_params).fetchall()

        snapshot_sql = """
            SELECT s.*
            FROM memory_consolidation_apply_snapshots s
        """
        snapshot_params: list[Any] = []
        if normalized_project_key is not None:
            snapshot_sql += """
                JOIN sleep_runs r ON r.id = s.run_id
                WHERE r.mode = 'consolidation_apply_run' AND r.project_key = ?
            """
            snapshot_params.append(normalized_project_key)
        snapshot_sql += " ORDER BY s.id DESC"
        snapshot_rows = conn.execute(snapshot_sql, snapshot_params).fetchall()

        rollback_snapshot_sql = """
            SELECT s.*
            FROM memory_consolidation_rollback_snapshots s
        """
        rollback_snapshot_params: list[Any] = []
        if normalized_project_key is not None:
            rollback_snapshot_sql += """
                JOIN sleep_runs r ON r.id = s.original_apply_run_id
                WHERE r.mode = 'consolidation_apply_run' AND r.project_key = ?
            """
            rollback_snapshot_params.append(normalized_project_key)
        rollback_snapshot_sql += " ORDER BY s.id DESC"
        rollback_snapshot_rows = conn.execute(rollback_snapshot_sql, rollback_snapshot_params).fetchall()

        orphan_apply_snapshot_rows: list[Any] = []
        orphan_apply_snapshot_count_excluded = 0
        if normalized_project_key is None:
            orphan_apply_snapshot_rows = conn.execute(
                """
                SELECT s.*
                FROM memory_consolidation_apply_snapshots s
                LEFT JOIN sleep_runs r ON r.id = s.run_id
                WHERE r.id IS NULL OR r.mode != 'consolidation_apply_run'
                ORDER BY s.id DESC
                """
            ).fetchall()
        else:
            orphan_apply_snapshot_count_excluded = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM memory_consolidation_apply_snapshots s
                    LEFT JOIN sleep_runs r ON r.id = s.run_id
                    WHERE r.id IS NULL OR r.mode != 'consolidation_apply_run'
                    """
                ).fetchone()[0]
            )

        orphan_rollback_snapshot_rows: list[Any] = []
        orphan_rollback_snapshot_count_excluded = 0
        if normalized_project_key is None:
            orphan_rollback_snapshot_rows = conn.execute(
                """
                SELECT
                    s.*,
                    apply_run.mode AS original_apply_run_mode,
                    rollback_run.mode AS rollback_run_mode
                FROM memory_consolidation_rollback_snapshots s
                LEFT JOIN sleep_runs apply_run ON apply_run.id = s.original_apply_run_id
                LEFT JOIN sleep_runs rollback_run ON rollback_run.id = s.rollback_run_id
                WHERE apply_run.id IS NULL
                   OR apply_run.mode != 'consolidation_apply_run'
                   OR rollback_run.id IS NULL
                   OR rollback_run.mode != 'rollback'
                ORDER BY s.id DESC
                """
            ).fetchall()
        else:
            orphan_rollback_snapshot_count_excluded = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM memory_consolidation_rollback_snapshots s
                    LEFT JOIN sleep_runs apply_run ON apply_run.id = s.original_apply_run_id
                    LEFT JOIN sleep_runs rollback_run ON rollback_run.id = s.rollback_run_id
                    WHERE apply_run.id IS NULL
                       OR apply_run.mode != 'consolidation_apply_run'
                       OR rollback_run.id IS NULL
                       OR rollback_run.mode != 'rollback'
                    """
                ).fetchone()[0]
            )

        snapshots_by_run_id: dict[int, list[dict[str, Any]]] = {}
        for row in snapshot_rows:
            item = row_to_dict(row)
            snapshots_by_run_id.setdefault(int(item["run_id"]), []).append(item)

        rollback_snapshots_by_apply_run_id: dict[int, list[dict[str, Any]]] = {}
        rollback_snapshots_by_rollback_run_id: dict[int, list[dict[str, Any]]] = {}
        for row in rollback_snapshot_rows:
            item = row_to_dict(row)
            rollback_snapshots_by_apply_run_id.setdefault(int(item["original_apply_run_id"]), []).append(item)
            rollback_snapshots_by_rollback_run_id.setdefault(int(item["rollback_run_id"]), []).append(item)

        apply_findings: list[dict[str, Any]] = []
        rollback_findings: list[dict[str, Any]] = []
        apply_unsupported_metrics: list[str] = []
        rollback_unsupported_metrics: list[str] = []
        summary = {
            "total_apply_runs": len(run_rows),
            "apply_snapshots_checked": len(snapshot_rows),
            "runs_with_stored_snapshot": 0,
            "runs_missing_stored_snapshot": 0,
            "legacy_reconstructed_only_runs": 0,
            "orphan_snapshots": len(orphan_apply_snapshot_rows),
            "duplicate_snapshot_runs": 0,
            "malformed_snapshots": 0,
            "proposal_id_mismatches": 0,
            "proposal_type_mismatches": 0,
            "preview_hash_mismatches": 0,
            "healthy_runs": 0,
            "rollback_snapshots_checked": len(rollback_snapshot_rows),
            "rollback_runs_total": 0,
            "rollback_runs_with_stored_snapshot": 0,
            "rollback_runs_missing_stored_snapshot": 0,
            "legacy_rollback_runs_without_snapshot": 0,
            "rollback_orphan_snapshots": len(orphan_rollback_snapshot_rows),
            "rollback_duplicate_snapshot_runs": 0,
            "rollback_duplicate_snapshot_rollback_runs": 0,
            "rollback_malformed_snapshots": 0,
            "rollback_preview_hash_mismatches": 0,
            "rollback_guard_mismatches": 0,
            "rollback_wrong_run_type_links": 0,
            "healthy_rollback_runs": 0,
        }

        proposal_id_inference_gaps = 0
        proposal_type_cross_check_gaps = 0

        for orphan_row in orphan_apply_snapshot_rows:
            apply_findings.append(
                {
                    "severity": "warning",
                    "kind": "orphan_snapshot",
                    "snapshot_id": int(orphan_row["id"]),
                    "run_id": int(orphan_row["run_id"]),
                    "message": "Stored preview snapshot has no matching consolidation_apply_run.",
                }
            )

        for orphan_row in orphan_rollback_snapshot_rows:
            reason_parts: list[str] = []
            if normalize_optional_text(orphan_row["original_apply_run_mode"]) != "consolidation_apply_run":
                reason_parts.append("original_apply_run_missing_or_wrong_type")
                summary["rollback_wrong_run_type_links"] += 1
            if normalize_optional_text(orphan_row["rollback_run_mode"]) != "rollback":
                reason_parts.append("rollback_run_missing_or_wrong_type")
                summary["rollback_wrong_run_type_links"] += 1
            rollback_findings.append(
                {
                    "severity": "warning",
                    "kind": "orphan_rollback_snapshot",
                    "snapshot_id": int(orphan_row["id"]),
                    "original_apply_run_id": int(orphan_row["original_apply_run_id"]),
                    "rollback_run_id": int(orphan_row["rollback_run_id"]),
                    "reasons": reason_parts,
                    "message": "Stored rollback preview snapshot is not linked to the expected consolidation_apply_run -> rollback chain.",
                }
            )

        duplicate_rollback_run_ids = sorted(
            run_id for run_id, items in rollback_snapshots_by_rollback_run_id.items() if len(items) > 1
        )
        summary["rollback_duplicate_snapshot_rollback_runs"] = len(duplicate_rollback_run_ids)
        for rollback_run_id in duplicate_rollback_run_ids:
            rollback_findings.append(
                {
                    "severity": "error",
                    "kind": "duplicate_snapshots_for_rollback_run",
                    "rollback_run_id": int(rollback_run_id),
                    "snapshot_ids": [int(item["id"]) for item in rollback_snapshots_by_rollback_run_id[rollback_run_id]],
                    "message": "More than one rollback snapshot row points to the same rollback run.",
                }
            )

        for run_row in run_rows:
            run = row_to_dict(run_row)
            run_id = int(run["id"])
            run_record = _consolidation_apply_run_record(conn, run, include_details=False)
            run_apply_findings_before = len(apply_findings)
            run_snapshots = snapshots_by_run_id.get(run_id, [])

            if run_snapshots:
                summary["runs_with_stored_snapshot"] += 1
            else:
                summary["runs_missing_stored_snapshot"] += 1
                summary["legacy_reconstructed_only_runs"] += 1
                apply_findings.append(
                    {
                        "severity": "warning",
                        "kind": "legacy_reconstructed_only_run",
                        "run_id": run_id,
                        "proposal_id": run_record.get("proposal_id"),
                        "message": "Apply run has no stored immutable preview snapshot and relies on reconstructed audit only.",
                    }
                )
                if run_record.get("proposal_id") is None:
                    proposal_id_inference_gaps += 1
                proposal_type_cross_check_gaps += 1
            if run_snapshots:
                if len(run_snapshots) > 1:
                    summary["duplicate_snapshot_runs"] += 1
                    apply_findings.append(
                        {
                            "severity": "error",
                            "kind": "duplicate_snapshots_for_run",
                            "run_id": run_id,
                            "snapshot_ids": [int(item["id"]) for item in run_snapshots],
                            "message": "More than one snapshot row points to the same consolidation_apply_run.",
                        }
                    )

                proposal_public_id = normalize_optional_text(run_record.get("proposal_id"))
                proposal_memory_id = (
                    _normalize_consolidation_proposal_id(proposal_public_id)
                    if proposal_public_id is not None
                    else None
                )
                run_proposal_type = normalize_optional_text(run_record.get("proposal_type"))
                if proposal_memory_id is None:
                    proposal_id_inference_gaps += 1

                for snapshot_row in run_snapshots:
                    snapshot_id = int(snapshot_row["id"])
                    payload = _decode_action_value(snapshot_row.get("snapshot_json"))
                    if not isinstance(payload, dict):
                        summary["malformed_snapshots"] += 1
                        apply_findings.append(
                            {
                                "severity": "error",
                                "kind": "malformed_snapshot_payload",
                                "run_id": run_id,
                                "snapshot_id": snapshot_id,
                                "message": "snapshot_json is not valid JSON object payload.",
                            }
                        )
                        proposal_type_cross_check_gaps += 1
                        continue

                    required_keys = {
                        "schema_version",
                        "run_id",
                        "proposal_id",
                        "proposal_type",
                        "preview_source",
                        "preview_hash",
                        "preview_result",
                    }
                    missing_keys = sorted(key for key in required_keys if payload.get(key) in (None, ""))
                    preview_result = payload.get("preview_result")
                    if missing_keys or not isinstance(preview_result, dict):
                        summary["malformed_snapshots"] += 1
                        apply_findings.append(
                            {
                                "severity": "error",
                                "kind": "malformed_snapshot_payload",
                                "run_id": run_id,
                                "snapshot_id": snapshot_id,
                                "missing_keys": missing_keys,
                                "message": "Stored snapshot payload is incomplete or preview_result is not an object.",
                            }
                        )
                        proposal_type_cross_check_gaps += 1
                        continue

                    try:
                        normalized_payload_run_id = int(payload.get("run_id"))
                    except (TypeError, ValueError):
                        normalized_payload_run_id = None
                    if normalized_payload_run_id != run_id:
                        summary["malformed_snapshots"] += 1
                        apply_findings.append(
                            {
                                "severity": "error",
                                "kind": "snapshot_run_id_mismatch",
                                "run_id": run_id,
                                "snapshot_id": snapshot_id,
                                "payload_run_id": payload.get("run_id"),
                                "message": "Stored snapshot payload run_id does not match the snapshot row target run.",
                            }
                        )

                    payload_public_id = normalize_optional_text(payload.get("proposal_id"))
                    payload_memory_id = (
                        _normalize_consolidation_proposal_id(payload_public_id)
                        if payload_public_id is not None
                        else None
                    )
                    proposal_id_mismatch_reasons: list[str] = []
                    row_proposal_memory_id = snapshot_row.get("proposal_memory_id")
                    if row_proposal_memory_id is not None and payload_memory_id is not None and int(row_proposal_memory_id) != payload_memory_id:
                        proposal_id_mismatch_reasons.append("row proposal_memory_id differs from payload proposal_id")
                    if proposal_memory_id is not None and payload_memory_id is not None and proposal_memory_id != payload_memory_id:
                        proposal_id_mismatch_reasons.append("run proposal_id differs from payload proposal_id")
                    if proposal_id_mismatch_reasons:
                        summary["proposal_id_mismatches"] += 1
                        apply_findings.append(
                            {
                                "severity": "error",
                                "kind": "proposal_id_mismatch",
                                "run_id": run_id,
                                "snapshot_id": snapshot_id,
                                "run_proposal_id": proposal_public_id,
                                "payload_proposal_id": payload_public_id,
                                "row_proposal_memory_id": row_proposal_memory_id,
                                "reasons": proposal_id_mismatch_reasons,
                                "message": "Stored snapshot proposal_id is inconsistent with persisted apply run metadata.",
                            }
                        )

                    payload_proposal_type = normalize_optional_text(payload.get("proposal_type"))
                    if run_proposal_type is not None and payload_proposal_type is not None:
                        if run_proposal_type != payload_proposal_type:
                            summary["proposal_type_mismatches"] += 1
                            apply_findings.append(
                                {
                                    "severity": "error",
                                    "kind": "proposal_type_mismatch",
                                    "run_id": run_id,
                                    "snapshot_id": snapshot_id,
                                    "run_proposal_type": run_proposal_type,
                                    "payload_proposal_type": payload_proposal_type,
                                    "message": "Stored snapshot proposal_type differs from the current proposal metadata.",
                                }
                            )
                    else:
                        proposal_type_cross_check_gaps += 1

                    expected_preview_hash = _canonical_json_hash(preview_result)
                    payload_preview_hash = normalize_optional_text(payload.get("preview_hash"))
                    row_preview_hash = normalize_optional_text(snapshot_row.get("preview_hash"))
                    if (
                        payload_preview_hash != expected_preview_hash
                        or row_preview_hash != expected_preview_hash
                        or payload_preview_hash != row_preview_hash
                    ):
                        summary["preview_hash_mismatches"] += 1
                        apply_findings.append(
                            {
                                "severity": "error",
                                "kind": "preview_hash_mismatch",
                                "run_id": run_id,
                                "snapshot_id": snapshot_id,
                                "row_preview_hash": row_preview_hash,
                                "payload_preview_hash": payload_preview_hash,
                                "expected_preview_hash": expected_preview_hash,
                                "message": "Stored snapshot hash does not match the canonical preview_result payload.",
                            }
                        )

            if len(apply_findings) == run_apply_findings_before:
                summary["healthy_runs"] += 1

            rollback_run_id = run_record.get("rollback_run_id")
            if rollback_run_id is None:
                continue

            summary["rollback_runs_total"] += 1
            run_rollback_findings_before = len(rollback_findings)
            run_rollback_snapshots = rollback_snapshots_by_apply_run_id.get(run_id, [])
            rollback_run_row = conn.execute("SELECT * FROM sleep_runs WHERE id = ?", (int(rollback_run_id),)).fetchone()
            rollback_run_mode = normalize_optional_text((row_to_dict(rollback_run_row) if rollback_run_row is not None else {}).get("mode"))

            if run_rollback_snapshots:
                summary["rollback_runs_with_stored_snapshot"] += 1
            else:
                summary["rollback_runs_missing_stored_snapshot"] += 1
                summary["legacy_rollback_runs_without_snapshot"] += 1
                rollback_findings.append(
                    {
                        "severity": "warning",
                        "kind": "legacy_rollback_without_snapshot",
                        "run_id": run_id,
                        "rollback_run_id": int(rollback_run_id),
                        "message": "Rollback exists but no stored immutable rollback preview snapshot is available; this may be a legacy/raw undo path and cannot be proven as a guarded rollback.",
                    }
                )
                continue

            if len(run_rollback_snapshots) > 1:
                summary["rollback_duplicate_snapshot_runs"] += 1
                rollback_findings.append(
                    {
                        "severity": "error",
                        "kind": "duplicate_rollback_snapshots_for_apply_run",
                        "run_id": run_id,
                        "rollback_run_id": int(rollback_run_id),
                        "snapshot_ids": [int(item["id"]) for item in run_rollback_snapshots],
                        "message": "More than one rollback snapshot row points to the same consolidation apply run.",
                    }
                )

            if rollback_run_row is None or rollback_run_mode != "rollback":
                summary["rollback_wrong_run_type_links"] += 1
                rollback_findings.append(
                    {
                        "severity": "error",
                        "kind": "rollback_snapshot_wrong_run_type",
                        "run_id": run_id,
                        "rollback_run_id": int(rollback_run_id),
                        "rollback_run_mode": rollback_run_mode,
                        "message": "Rollback snapshot points to a run that is missing or not of type rollback.",
                    }
                )

            for snapshot_row in run_rollback_snapshots:
                snapshot_id = int(snapshot_row["id"])
                payload = _decode_action_value(snapshot_row.get("snapshot_json"))
                if not isinstance(payload, dict):
                    summary["rollback_malformed_snapshots"] += 1
                    rollback_findings.append(
                        {
                            "severity": "error",
                            "kind": "malformed_rollback_snapshot_payload",
                            "run_id": run_id,
                            "rollback_run_id": int(rollback_run_id),
                            "snapshot_id": snapshot_id,
                            "message": "rollback snapshot_json is not valid JSON object payload.",
                        }
                    )
                    continue

                required_keys = {
                    "schema_version",
                    "original_apply_run_id",
                    "rollback_run_id",
                    "preview_source",
                    "rollback_preview_hash",
                    "rollback_preview",
                    "rollback_guard",
                }
                missing_keys = sorted(key for key in required_keys if payload.get(key) in (None, ""))
                rollback_preview = payload.get("rollback_preview")
                rollback_guard = payload.get("rollback_guard")
                if missing_keys or not isinstance(rollback_preview, dict) or not isinstance(rollback_guard, dict):
                    summary["rollback_malformed_snapshots"] += 1
                    rollback_findings.append(
                        {
                            "severity": "error",
                            "kind": "malformed_rollback_snapshot_payload",
                            "run_id": run_id,
                            "rollback_run_id": int(rollback_run_id),
                            "snapshot_id": snapshot_id,
                            "missing_keys": missing_keys,
                            "message": "Stored rollback snapshot payload is incomplete or rollback_preview/rollback_guard is not an object.",
                        }
                    )
                    continue

                try:
                    normalized_payload_apply_run_id = int(payload.get("original_apply_run_id"))
                except (TypeError, ValueError):
                    normalized_payload_apply_run_id = None
                try:
                    normalized_payload_rollback_run_id = int(payload.get("rollback_run_id"))
                except (TypeError, ValueError):
                    normalized_payload_rollback_run_id = None
                if normalized_payload_apply_run_id != run_id or normalized_payload_rollback_run_id != int(rollback_run_id):
                    summary["rollback_malformed_snapshots"] += 1
                    rollback_findings.append(
                        {
                            "severity": "error",
                            "kind": "rollback_snapshot_run_id_mismatch",
                            "run_id": run_id,
                            "rollback_run_id": int(rollback_run_id),
                            "snapshot_id": snapshot_id,
                            "payload_original_apply_run_id": payload.get("original_apply_run_id"),
                            "payload_rollback_run_id": payload.get("rollback_run_id"),
                            "message": "Stored rollback snapshot payload run ids do not match the persisted apply/rollback chain.",
                        }
                    )

                row_rollback_run_id = snapshot_row.get("rollback_run_id")
                if row_rollback_run_id is None or int(row_rollback_run_id) != int(rollback_run_id):
                    summary["rollback_wrong_run_type_links"] += 1
                    rollback_findings.append(
                        {
                            "severity": "error",
                            "kind": "rollback_snapshot_row_link_mismatch",
                            "run_id": run_id,
                            "rollback_run_id": int(rollback_run_id),
                            "snapshot_id": snapshot_id,
                            "row_rollback_run_id": row_rollback_run_id,
                            "message": "Rollback snapshot row is linked to a different rollback run than the apply audit chain.",
                        }
                    )

                expected_rollback_preview_hash = _consolidation_apply_rollback_preview_hash_from_response(rollback_preview)
                payload_rollback_preview_hash = normalize_optional_text(payload.get("rollback_preview_hash"))
                row_rollback_preview_hash = normalize_optional_text(snapshot_row.get("rollback_preview_hash"))
                nested_rollback_preview_hash = normalize_optional_text(rollback_preview.get("rollback_preview_hash"))
                if (
                    payload_rollback_preview_hash != expected_rollback_preview_hash
                    or row_rollback_preview_hash != expected_rollback_preview_hash
                    or nested_rollback_preview_hash != expected_rollback_preview_hash
                    or payload_rollback_preview_hash != row_rollback_preview_hash
                ):
                    summary["rollback_preview_hash_mismatches"] += 1
                    rollback_findings.append(
                        {
                            "severity": "error",
                            "kind": "rollback_preview_hash_mismatch",
                            "run_id": run_id,
                            "rollback_run_id": int(rollback_run_id),
                            "snapshot_id": snapshot_id,
                            "row_rollback_preview_hash": row_rollback_preview_hash,
                            "payload_rollback_preview_hash": payload_rollback_preview_hash,
                            "nested_rollback_preview_hash": nested_rollback_preview_hash,
                            "expected_rollback_preview_hash": expected_rollback_preview_hash,
                            "message": "Stored rollback snapshot hash does not match the canonical rollback_preview payload.",
                        }
                    )

                guard_expected = normalize_optional_text(rollback_guard.get("expected_rollback_preview_hash"))
                guard_actual = normalize_optional_text(rollback_guard.get("actual_rollback_preview_hash"))
                guard_algorithm = normalize_optional_text(rollback_guard.get("algorithm"))
                guard_matched = rollback_guard.get("matched")
                guard_mismatch_reasons: list[str] = []
                if guard_expected is None:
                    guard_mismatch_reasons.append("expected_rollback_preview_hash missing")
                if guard_actual != expected_rollback_preview_hash:
                    guard_mismatch_reasons.append("actual_rollback_preview_hash differs from canonical rollback preview hash")
                if guard_algorithm != _CONSOLIDATION_PREVIEW_HASH_ALGORITHM:
                    guard_mismatch_reasons.append("guard algorithm differs from the canonical rollback preview hash algorithm")
                expected_matched_value = bool(guard_expected is not None and guard_expected == guard_actual)
                if not isinstance(guard_matched, bool) or guard_matched != expected_matched_value:
                    guard_mismatch_reasons.append("guard matched flag does not reflect expected vs actual rollback preview hash equality")
                if guard_mismatch_reasons:
                    summary["rollback_guard_mismatches"] += 1
                    rollback_findings.append(
                        {
                            "severity": "error",
                            "kind": "rollback_guard_mismatch",
                            "run_id": run_id,
                            "rollback_run_id": int(rollback_run_id),
                            "snapshot_id": snapshot_id,
                            "reasons": guard_mismatch_reasons,
                            "expected_rollback_preview_hash": expected_rollback_preview_hash,
                            "guard_expected_rollback_preview_hash": guard_expected,
                            "guard_actual_rollback_preview_hash": guard_actual,
                            "message": "Stored rollback guard metadata is inconsistent with the canonical rollback preview payload.",
                        }
                    )

            if len(rollback_findings) == run_rollback_findings_before:
                summary["healthy_rollback_runs"] += 1

        if proposal_id_inference_gaps:
            apply_unsupported_metrics.append(
                "proposal_id cross-check is unavailable for runs where persisted apply artifacts no longer identify a unique proposal"
            )
        if proposal_type_cross_check_gaps:
            apply_unsupported_metrics.append(
                "proposal_type cross-check requires both a valid stored snapshot payload and current proposal metadata"
            )
        if summary["legacy_reconstructed_only_runs"]:
            apply_unsupported_metrics.append(
                "legacy runs without stored snapshots only support reconstructed audit and cannot prove full preview snapshot integrity"
            )
        if orphan_apply_snapshot_count_excluded:
            apply_unsupported_metrics.append(
                "orphan snapshots without matching runs cannot be attributed to a filtered project_key report"
            )
        if summary["legacy_rollback_runs_without_snapshot"]:
            rollback_unsupported_metrics.append(
                "rollback runs without stored snapshots may come from legacy/raw undo paths and cannot prove guarded rollback snapshot integrity"
            )
        if orphan_rollback_snapshot_count_excluded:
            rollback_unsupported_metrics.append(
                "orphan rollback snapshots without a valid apply/rollback chain cannot be attributed to a filtered project_key report"
            )

        apply_recommended_actions: list[str] = []
        if summary["runs_missing_stored_snapshot"]:
            apply_recommended_actions.append(
                "Treat legacy apply runs without stored snapshots as best-effort audit only during operator review."
            )
        if summary["orphan_snapshots"] or orphan_apply_snapshot_count_excluded:
            apply_recommended_actions.append(
                "Inspect orphan snapshot rows before trusting aggregate snapshot coverage metrics."
            )
        if summary["malformed_snapshots"] or summary["preview_hash_mismatches"]:
            apply_recommended_actions.append(
                "Review malformed or hash-mismatched snapshot rows for manual tampering, partial writes, or fixture drift."
            )
        if summary["proposal_id_mismatches"] or summary["proposal_type_mismatches"]:
            apply_recommended_actions.append(
                "Investigate proposal metadata mismatches before using a snapshot as audit evidence."
            )

        rollback_recommended_actions: list[str] = []
        if summary["legacy_rollback_runs_without_snapshot"]:
            rollback_recommended_actions.append(
                "Treat rollback runs without stored snapshots as legacy/raw undo evidence only unless newer guarded rollback metadata exists elsewhere."
            )
        if summary["rollback_orphan_snapshots"] or orphan_rollback_snapshot_count_excluded:
            rollback_recommended_actions.append(
                "Inspect orphan rollback snapshot rows before trusting rollback coverage or rollback replay metadata."
            )
        if summary["rollback_malformed_snapshots"] or summary["rollback_preview_hash_mismatches"]:
            rollback_recommended_actions.append(
                "Review malformed or hash-mismatched rollback snapshots for tampering, partial writes, or fixture drift."
            )
        if summary["rollback_guard_mismatches"] or summary["rollback_wrong_run_type_links"]:
            rollback_recommended_actions.append(
                "Investigate rollback guard metadata or run-type link mismatches before trusting rollback audit evidence."
            )

        findings = apply_findings + rollback_findings
        top_level_unsupported_metrics = [
            item
            for item in apply_unsupported_metrics + rollback_unsupported_metrics
            if item
            not in {
                "proposal_id cross-check is unavailable for runs where persisted apply artifacts no longer identify a unique proposal",
                "proposal_type cross-check requires both a valid stored snapshot payload and current proposal metadata",
            }
        ]
        unsupported_metrics = list(dict.fromkeys(top_level_unsupported_metrics))
        recommended_actions = list(dict.fromkeys(apply_recommended_actions + rollback_recommended_actions))
        summary["issues_total"] = len(findings)

        result: dict[str, Any] = {
            "status": _snapshot_integrity_status_from_findings(findings, unsupported_metrics),
            "schema_version": "memory_consolidation_snapshot_integrity_report.v2",
            "filters": {
                "project_key": normalized_project_key,
            },
            "summary": summary,
            "apply_snapshot_integrity": {
                "status": _snapshot_integrity_status_from_findings(apply_findings, apply_unsupported_metrics),
                "summary": {
                    "runs_checked": len(run_rows),
                    "snapshot_rows_checked": len(snapshot_rows),
                    "healthy_runs": summary["healthy_runs"],
                    "runs_with_stored_snapshot": summary["runs_with_stored_snapshot"],
                    "runs_missing_stored_snapshot": summary["runs_missing_stored_snapshot"],
                    "legacy_reconstructed_only_runs": summary["legacy_reconstructed_only_runs"],
                    "orphan_snapshots": summary["orphan_snapshots"],
                    "duplicate_snapshot_runs": summary["duplicate_snapshot_runs"],
                    "malformed_snapshots": summary["malformed_snapshots"],
                    "proposal_id_mismatches": summary["proposal_id_mismatches"],
                    "proposal_type_mismatches": summary["proposal_type_mismatches"],
                    "preview_hash_mismatches": summary["preview_hash_mismatches"],
                    "issues_total": len(apply_findings),
                },
                "findings": apply_findings,
                "unsupported_metrics": apply_unsupported_metrics,
                "recommended_actions": apply_recommended_actions,
            },
            "rollback_snapshot_integrity": {
                "status": _snapshot_integrity_status_from_findings(rollback_findings, rollback_unsupported_metrics),
                "summary": {
                    "rollback_runs_checked": summary["rollback_runs_total"],
                    "snapshot_rows_checked": len(rollback_snapshot_rows),
                    "healthy_rollback_runs": summary["healthy_rollback_runs"],
                    "rollback_runs_with_stored_snapshot": summary["rollback_runs_with_stored_snapshot"],
                    "rollback_runs_missing_stored_snapshot": summary["rollback_runs_missing_stored_snapshot"],
                    "legacy_rollback_runs_without_snapshot": summary["legacy_rollback_runs_without_snapshot"],
                    "rollback_orphan_snapshots": summary["rollback_orphan_snapshots"],
                    "rollback_duplicate_snapshot_runs": summary["rollback_duplicate_snapshot_runs"],
                    "rollback_duplicate_snapshot_rollback_runs": summary["rollback_duplicate_snapshot_rollback_runs"],
                    "rollback_malformed_snapshots": summary["rollback_malformed_snapshots"],
                    "rollback_preview_hash_mismatches": summary["rollback_preview_hash_mismatches"],
                    "rollback_guard_mismatches": summary["rollback_guard_mismatches"],
                    "rollback_wrong_run_type_links": summary["rollback_wrong_run_type_links"],
                    "issues_total": len(rollback_findings),
                },
                "findings": rollback_findings,
                "unsupported_metrics": rollback_unsupported_metrics,
                "recommended_actions": rollback_recommended_actions,
            },
            "findings": findings,
            "recommended_actions": recommended_actions,
            "safety": {
                "read_only": True,
                "mutates_memory_entries": False,
                "writes_snapshot_rows": False,
            },
            "unsupported_metrics": unsupported_metrics,
        }
        if include_debug:
            result["debug"] = {
                "run_ids": [int(row["id"]) for row in run_rows],
                "snapshot_run_ids": sorted(snapshots_by_run_id.keys()),
                "orphan_snapshot_run_ids": [int(row["run_id"]) for row in orphan_apply_snapshot_rows],
                "rollback_snapshot_original_apply_run_ids": sorted(rollback_snapshots_by_apply_run_id.keys()),
                "rollback_snapshot_run_ids": sorted(rollback_snapshots_by_rollback_run_id.keys()),
                "orphan_rollback_snapshot_original_apply_run_ids": [int(row["original_apply_run_id"]) for row in orphan_rollback_snapshot_rows],
            }
        return result
    finally:
        conn.close()


@mcp.tool
def preview_memory_supersession(
    new_memory_id: int,
    old_memory_id: int,
    relation_kind: str,
    reason: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Preview guarded Memory v3 supersession without mutating data."""
    conn = get_db_connection()
    try:
        return preview_memory_supersession_payload(
            conn,
            new_memory_id=new_memory_id,
            old_memory_id=old_memory_id,
            relation_kind=relation_kind,
            reason=reason,
            include_debug=include_debug,
            normalize_required_text=normalize_required_text,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            canonical_json_hash=_canonical_json_hash,
        )
    finally:
        conn.close()


@mcp.tool
def apply_memory_supersession(
    new_memory_id: int,
    old_memory_id: int,
    relation_kind: str,
    reason: str,
    expected_preview_hash: str,
    applied_by: str | None = None,
    notes: str | None = None,
    confirm_protected: bool = False,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Execute guarded Memory v3 supersession apply."""
    conn = get_db_connection()
    try:
        return apply_memory_supersession_payload(
            conn,
            new_memory_id=new_memory_id,
            old_memory_id=old_memory_id,
            relation_kind=relation_kind,
            reason=reason,
            expected_preview_hash=expected_preview_hash,
            applied_by=applied_by,
            notes=notes,
            confirm_protected=confirm_protected,
            include_debug=include_debug,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            canonical_json_hash=_canonical_json_hash,
            utc_now_iso=utc_now_iso,
            shift_iso_days=shift_iso_days,
            insert_memory_event=insert_memory_event,
        )
    finally:
        conn.close()


@mcp.tool
def list_memory_supersession_runs(
    project_key: str | None = None,
    new_memory_id: int | None = None,
    old_memory_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List read-only Memory v3 supersession apply runs."""
    conn = get_db_connection()
    try:
        return list_memory_supersession_runs_payload(
            conn,
            project_key=project_key,
            new_memory_id=new_memory_id,
            old_memory_id=old_memory_id,
            status=status,
            limit=limit,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_memory_supersession_run(run_id: int, include_debug: bool = False) -> dict[str, Any]:
    """Get one read-only Memory v3 supersession apply run."""
    conn = get_db_connection()
    try:
        return get_memory_supersession_run_payload(
            conn,
            run_id=run_id,
            include_debug=include_debug,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
        )
    finally:
        conn.close()


@mcp.tool
def preview_memory_supersession_rollback(run_id: int, include_debug: bool = False) -> dict[str, Any]:
    """Preview guarded rollback for a Memory v3 supersession apply run."""
    conn = get_db_connection()
    try:
        return preview_memory_supersession_rollback_payload(
            conn,
            run_id=run_id,
            include_debug=include_debug,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            canonical_json_hash=_canonical_json_hash,
        )
    finally:
        conn.close()


@mcp.tool
def rollback_memory_supersession_run(
    run_id: int,
    expected_rollback_preview_hash: str,
    rolled_back_by: str | None = None,
    notes: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Execute guarded rollback for a Memory v3 supersession apply run."""
    conn = get_db_connection()
    try:
        return rollback_memory_supersession_run_payload(
            conn,
            run_id=run_id,
            expected_rollback_preview_hash=expected_rollback_preview_hash,
            rolled_back_by=rolled_back_by,
            notes=notes,
            include_debug=include_debug,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            row_to_dict=row_to_dict,
            enrich_memory_dict=enrich_memory_dict,
            canonical_json_hash=_canonical_json_hash,
            utc_now_iso=utc_now_iso,
            insert_memory_event=insert_memory_event,
        )
    finally:
        conn.close()


@mcp.tool
def preview_undo_run(run_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        run = require_sleep_run_row(conn, run_id)
        existing_rollback_run_id = _existing_rollback_run_id(conn, run_id)
        rollbackable_actions = _get_rollbackable_actions(conn, run_id)
        summary: dict[str, int] = {}
        for action in rollbackable_actions:
            summary[action["action_type"]] = summary.get(action["action_type"], 0) + 1
        return {"status": "preview_completed", "target_run": row_to_dict(run), "already_rolled_back": existing_rollback_run_id is not None, "existing_rollback_run_id": existing_rollback_run_id, "rollbackable_action_count": len(rollbackable_actions), "rollbackable_action_summary": summary, "rollbackable_actions": rollbackable_actions}
    finally:
        conn.close()


@mcp.tool
def undo_run(run_id: int, notes: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        run = require_sleep_run_row(conn, run_id)
        mode = str(run["mode"])
        status = str(run["status"])
        if mode not in {"run", "conflict_run", "consolidation_run", "conflict_resolution_run", "consolidation_apply_run"}:
            return {"status": "error", "error": 'Undo obsluguje tylko przebiegi wykonawcze: run, conflict_run, consolidation_run, conflict_resolution_run albo consolidation_apply_run'}
        if not status.startswith("completed"):
            return {"status": "error", "error": 'Undo moÄąÄ˝na wykonaĂ„â€ˇ tylko dla zakoÄąâ€žczonego przebiegu completed'}
        existing_rollback_run_id = _existing_rollback_run_id(conn, run_id)
        if existing_rollback_run_id is not None:
            return {"status": "error", "error": f'Ten run zostaÄąâ€š juÄąÄ˝ cofniĂ„â„˘ty przez rollback run_id={existing_rollback_run_id}'}
        rollbackable_actions = _get_rollbackable_actions(conn, run_id)
        rollback_run_id = create_sleep_run(conn, mode="rollback", freedom_level=0, notes=notes or f"rollback_of_run_{run_id}", rollback_of_run_id=run_id)
        restored_items = [_rollback_single_action(conn, rollback_run_id, action) for action in rollbackable_actions]
        conn.commit()
        finalize_sleep_run(conn, rollback_run_id, status="completed", scanned_count=len(rollbackable_actions), changed_count=len(restored_items), archived_count=0, downgraded_count=0, duplicate_count=0, conflict_count=0, created_summary_count=0)
        return {"status": "completed", "rollback_run_id": rollback_run_id, "target_run_id": run_id, "restored_count": len(restored_items), "restored_items": restored_items}
    finally:
        conn.close()


@mcp.tool
def list_conflicted_memories(limit: int = 20) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return list_conflicted_memories_payload(conn, limit=limit, row_to_dict=row_to_dict)
    finally:
        conn.close()


@mcp.tool
def get_conflict_pairs(memory_id: int | None = None, limit: int = 100) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        return get_conflict_pairs_payload(conn, memory_id=memory_id, limit=limit, row_to_dict=row_to_dict)
    finally:
        conn.close()


@mcp.tool
def explain_conflict(memory_a_id: int, memory_b_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        if not _is_conflict_feature_active(conn, CONFLICT_EXPLAINER_FLAG_KEY):
            return {"status": "disabled", "reason": "feature_flag_off", "flag_key": CONFLICT_EXPLAINER_FLAG_KEY}
        result = conflict_explainer.explain_conflict_pair(conn, int(memory_a_id), int(memory_b_id))
        try:
            base_ids = sorted([int(memory_a_id), int(memory_b_id)])
            operation_id = timeline.new_operation_id("conflict")
            timeline.record_timeline_event(
                conn,
                event_type="conflict.classified",
                memory_id=base_ids[0],
                related_memory_id=base_ids[1],
                operation_id=operation_id,
                origin="conflict_explainer_auto",
                timeline_scope="memory",
                semantic_kind="decision",
                title=f"Conflict classified: {result['conflict_kind']} (confidence {result['confidence']})",
                payload={
                    "conflict_kind": result["conflict_kind"],
                    "confidence": result["confidence"],
                    "conflict_reason": result["conflict_reason"],
                    "base_memory_ids": result["base_memory_ids"],
                    "context_memory_ids": result["context_memory_ids"],
                    "signal_scores": result["debug"].get("signal_scores", {}),
                    "signals": result["debug"].get("signals", []),
                },
            )
            if bool(result.get("needs_human_review")):
                timeline.record_timeline_event(
                    conn,
                    event_type="conflict.review_requested",
                    memory_id=base_ids[0],
                    related_memory_id=base_ids[1],
                    operation_id=operation_id,
                    origin="conflict_explainer_auto",
                    timeline_scope="memory",
                    semantic_kind="decision",
                    title=f"Conflict review requested: {result['conflict_kind']}",
                    payload={
                        "conflict_kind": result["conflict_kind"],
                        "confidence": result["confidence"],
                        "base_memory_ids": result["base_memory_ids"],
                    },
                )
            timeline.record_timeline_event(
                conn,
                event_type="conflict.explained",
                memory_id=base_ids[0],
                related_memory_id=base_ids[1],
                operation_id=operation_id,
                origin="conflict_explainer_auto",
                timeline_scope="memory",
                semantic_kind="decision",
                title=f"Conflict explained: {result['conflict_kind']} (confidence {result['confidence']})",
                payload={
                    "conflict_kind": result["conflict_kind"],
                    "confidence": result["confidence"],
                    "suggested_relation": result["suggested_relation"],
                    "suggested_action": result["suggested_action"],
                    "needs_human_review": result["needs_human_review"],
                    "base_memory_ids": result["base_memory_ids"],
                },
            )
            conn.commit()
        except Exception:
            pass
        return result
    finally:
        conn.close()


@mcp.tool
def preview_conflict_resolution(memory_a_id: int, memory_b_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        if not _is_conflict_feature_active(conn, CONFLICT_PREVIEW_RESOLUTION_FLAG_KEY):
            return {"status": "disabled", "reason": "feature_flag_off", "flag_key": CONFLICT_PREVIEW_RESOLUTION_FLAG_KEY}
        return conflict_explainer.preview_resolution(conn, int(memory_a_id), int(memory_b_id))
    finally:
        conn.close()


def _apply_conflict_resolution_impl(
    memory_a_id: int,
    memory_b_id: int,
    notes: str | None = None,
    *,
    allow_legacy_unsafe_canonical_links: bool,
) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        if not _is_conflict_feature_active(conn, CONFLICT_AUTO_RESOLUTION_FLAG_KEY):
            return {
                "status": "skipped",
                "skip_reason": "feature_flag_off",
                "flag_key": CONFLICT_AUTO_RESOLUTION_FLAG_KEY,
                "memory_a_id": int(memory_a_id),
                "memory_b_id": int(memory_b_id),
                "conflict_kind": None,
                "applied_changes": [],
                "run_id": None,
            }
        run_id = create_sleep_run(conn, mode="conflict_resolution_run", freedom_level=0, notes=notes)
        result = conflict_explainer.apply_resolution(
            conn,
            int(memory_a_id),
            int(memory_b_id),
            allow_legacy_unsafe_canonical_links=bool(allow_legacy_unsafe_canonical_links),
        )

        if result["status"] == "skipped":
            finalize_sleep_run(conn, run_id, status="skipped", scanned_count=2, changed_count=0, archived_count=0, downgraded_count=0, duplicate_count=0, conflict_count=1, created_summary_count=0)
            return {**result, "run_id": run_id}

        for change in result["applied_changes"]:
            if change["action"] == "create_link":
                add_sleep_action(
                    conn, run_id, "conflict_link_created",
                    change["from_memory_id"],
                    None,
                    {"link_id": change["link_id"], "from_memory_id": change["from_memory_id"], "to_memory_id": change["to_memory_id"], "relation_type": change["relation_type"]},
                    f"conflict_resolution_{result['conflict_kind']}",
                )
            elif change["action"] == "set_valid_to":
                add_sleep_action(
                    conn, run_id, "valid_to_set",
                    change["memory_id"],
                    {"valid_to": change["old_valid_to"]},
                    {"valid_to": change["new_valid_to"]},
                    f"conflict_resolution_{result['conflict_kind']}",
                )

        try:
            base_ids = sorted([int(memory_a_id), int(memory_b_id)])
            operation_id = timeline.new_operation_id("conflict")
            timeline.record_timeline_event(
                conn,
                event_type="conflict.resolution_applied",
                memory_id=base_ids[0],
                related_memory_id=base_ids[1],
                operation_id=operation_id,
                origin="conflict_explainer_auto",
                timeline_scope="memory",
                semantic_kind="decision",
                title=f"Conflict resolution applied: {result['conflict_kind']} (confidence {result['confidence']})",
                payload={
                    "conflict_kind": result["conflict_kind"],
                    "confidence": result["confidence"],
                    "applied_changes_count": len(result["applied_changes"]),
                    "run_id": run_id,
                    "base_memory_ids": base_ids,
                },
            )
        except Exception:
            pass

        conn.commit()
        finalize_sleep_run(conn, run_id, status="completed", scanned_count=2, changed_count=len(result["applied_changes"]), archived_count=0, downgraded_count=0, duplicate_count=0, conflict_count=1, created_summary_count=0)
        return {**result, "run_id": run_id}
    finally:
        conn.close()


@mcp.tool
def apply_conflict_resolution(memory_a_id: int, memory_b_id: int, notes: str | None = None) -> dict[str, Any]:
    """Apply only non-canonical auto resolutions; canonical truth links require guarded routes."""
    return _apply_conflict_resolution_impl(
        int(memory_a_id),
        int(memory_b_id),
        notes=notes,
        allow_legacy_unsafe_canonical_links=False,
    )


def apply_conflict_resolution_legacy_unsafe(
    memory_a_id: int,
    memory_b_id: int,
    notes: str | None = None,
) -> dict[str, Any]:
    """Compatibility/forensics helper for historical tests; not registered as an MCP tool."""
    return _apply_conflict_resolution_impl(
        int(memory_a_id),
        int(memory_b_id),
        notes=notes,
        allow_legacy_unsafe_canonical_links=True,
    )




_CONFLICT_LINK_TYPES = {"contradicts", "supersedes", "relates_to"}


def _conflict_registry_cluster_status(
    conn,
    *,
    cluster: dict[str, Any],
    member_ids: list[int],
) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in member_ids)
    decision_rows = conn.execute(
        f"""
        SELECT *
        FROM timeline_events
        WHERE event_type = 'conflict.decision_recorded'
          AND memory_id IN ({placeholders})
          AND related_memory_id IN ({placeholders})
        ORDER BY created_at DESC, id DESC
        """,
        member_ids + member_ids,
    ).fetchall() if member_ids else []
    resolution_rows = conn.execute(
        f"""
        SELECT *
        FROM timeline_events
        WHERE event_type = 'conflict.resolution_applied'
          AND memory_id IN ({placeholders})
        ORDER BY created_at DESC, id DESC
        """,
        member_ids,
    ).fetchall() if member_ids else []

    latest_decision = row_to_dict(decision_rows[0]) if decision_rows else None
    latest_resolution = row_to_dict(resolution_rows[0]) if resolution_rows else None
    latest_decision_payload = _decode_action_value(latest_decision.get("payload_json")) if latest_decision else {}
    latest_resolution_payload = _decode_action_value(latest_resolution.get("payload_json")) if latest_resolution else {}
    decision_value = normalize_optional_text((latest_decision_payload or {}).get("decision") if isinstance(latest_decision_payload, dict) else None)

    if decision_value in {"false_positive", "rejected"}:
        status = "ignored"
    elif latest_resolution is not None or decision_value == "approved":
        status = "resolved"
    elif not bool(cluster.get("has_unresolved")) and int(cluster.get("conflict_link_count") or 0) > 0:
        status = "resolved"
    else:
        status = "open"

    return {
        "status": status,
        "latest_decision": {
            "decision": decision_value,
            "notes": latest_decision_payload.get("notes") if isinstance(latest_decision_payload, dict) else None,
            "event_id": int(latest_decision["id"]) if latest_decision else None,
            "created_at": latest_decision.get("created_at") if latest_decision else None,
        } if latest_decision else None,
        "latest_resolution": {
            "event_id": int(latest_resolution["id"]) if latest_resolution else None,
            "created_at": latest_resolution.get("created_at") if latest_resolution else None,
            "conflict_kind": latest_resolution_payload.get("conflict_kind") if isinstance(latest_resolution_payload, dict) else None,
            "run_id": latest_resolution_payload.get("run_id") if isinstance(latest_resolution_payload, dict) else None,
        } if latest_resolution else None,
    }


def _conflict_registry_severity(cluster: dict[str, Any]) -> str:
    size = int(cluster.get("size") or 0)
    unresolved = bool(cluster.get("has_unresolved"))
    link_count = int(cluster.get("conflict_link_count") or 0)
    if unresolved and size >= 3:
        return "high"
    if unresolved or link_count >= 2:
        return "medium"
    return "low"


def _conflict_registry_entry(conn, cluster: dict[str, Any]) -> dict[str, Any]:
    member_ids = [int(item) for item in cluster.get("member_ids") or []]
    central_id = int(cluster.get("central_memory_id") or member_ids[0])
    divergence_id = int(cluster.get("divergence_source_id") or central_id)
    explanation = conflict_explainer.explain_conflict_pair(conn, central_id, divergence_id) if len(member_ids) >= 2 else None
    preview = conflict_explainer.preview_resolution(conn, central_id, divergence_id) if len(member_ids) >= 2 else {}
    lifecycle = _conflict_registry_cluster_status(conn, cluster=cluster, member_ids=member_ids)
    summary = (
        explanation["explanation"]
        if explanation is not None
        else f"Conflict cluster with {len(member_ids)} memory items."
    )
    proposed_resolution = None
    if explanation is not None:
        proposed_resolution = explanation.get("suggested_action")
    if preview.get("proposed_changes"):
        proposed_resolution = proposed_resolution or preview["proposed_changes"][0].get("action")

    return {
        "conflict_id": "conflict:" + "-".join(str(item) for item in member_ids),
        "type": "conflict",
        "conflicting_memories": member_ids,
        "summary": summary,
        "severity": _conflict_registry_severity(cluster),
        "proposed_resolution": proposed_resolution,
        "status": lifecycle["status"],
        "linked_memories": member_ids,
        "cluster": cluster,
        "classification": {
            "conflict_kind": explanation.get("conflict_kind") if explanation else None,
            "confidence": explanation.get("confidence") if explanation else None,
            "needs_human_review": explanation.get("needs_human_review") if explanation else None,
            "suggested_relation": explanation.get("suggested_relation") if explanation else None,
        },
        "resolution_preview": {
            "can_auto_apply": bool(preview.get("can_auto_apply")),
            "skip_reason": preview.get("skip_reason"),
            "proposed_changes_count": len(preview.get("proposed_changes") or []),
        },
        "latest_decision": lifecycle["latest_decision"],
        "latest_resolution": lifecycle["latest_resolution"],
    }


@mcp.tool
def get_conflict_history(memory_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        memory_row = require_memory_row(conn, memory_id)
        memory = enrich_memory_dict(row_to_dict(memory_row))

        # 1. Conflict timeline events
        all_events = timeline.timeline_query(conn, limit=500, memory_id=memory_id, row_to_dict=row_to_dict)
        conflict_events = [e for e in all_events if str(e.get("event_type", "")).startswith("conflict.")]

        # 2. Conflict-related links
        link_rows = conn.execute(
            "SELECT * FROM memory_links WHERE from_memory_id = ? OR to_memory_id = ?",
            (memory_id, memory_id),
        ).fetchall()
        conflict_links: list[dict[str, Any]] = []
        for row in link_rows:
            link = row_to_dict(row)
            if link.get("relation_type") not in _CONFLICT_LINK_TYPES:
                continue
            direction = "outgoing" if int(link["from_memory_id"]) == memory_id else "incoming"
            other_id = int(link["to_memory_id"]) if direction == "outgoing" else int(link["from_memory_id"])
            conflict_links.append({
                "link_id": int(link["id"]),
                "relation_type": link["relation_type"],
                "direction": direction,
                "other_memory_id": other_id,
                "weight": link.get("weight"),
                "created_at": link.get("created_at"),
            })

        # 3. Resolution runs Ă˘â‚¬â€ť wyciĂ„â€¦gam run_id z timeline events conflict.resolution_applied
        resolution_run_ids: list[int] = []
        for event in conflict_events:
            if event.get("event_type") == "conflict.resolution_applied":
                payload = event.get("payload") or {}
                rid = payload.get("run_id")
                if rid is not None and int(rid) not in resolution_run_ids:
                    resolution_run_ids.append(int(rid))

        # valid_to_set Ă˘â‚¬â€ť bezpoÄąâ€şredni match przez memory_id
        vt_rows = conn.execute(
            """
            SELECT sra.*, sr.started_at AS run_started_at
            FROM sleep_run_actions sra
            JOIN sleep_runs sr ON sr.id = sra.run_id
            WHERE sr.mode = 'conflict_resolution_run'
              AND sra.action_type = 'valid_to_set'
              AND sra.memory_id = ?
            ORDER BY sra.id ASC
            """,
            (memory_id,),
        ).fetchall()

        valid_to_history: list[dict[str, Any]] = []
        for row in vt_rows:
            item = row_to_dict(row)
            run_id_item = int(item["run_id"])
            if run_id_item not in resolution_run_ids:
                resolution_run_ids.append(run_id_item)
            old_val = _decode_action_value(item.get("old_value"))
            new_val = _decode_action_value(item.get("new_value"))
            valid_to_history.append({
                "run_id": run_id_item,
                "memory_id": int(item["memory_id"]),
                "previous_valid_to": old_val.get("valid_to") if isinstance(old_val, dict) else old_val,
                "new_valid_to": new_val.get("valid_to") if isinstance(new_val, dict) else new_val,
                "run_started_at": item.get("run_started_at"),
            })

        resolution_runs: list[dict[str, Any]] = []
        for rid in resolution_run_ids:
            run_row = conn.execute("SELECT * FROM sleep_runs WHERE id = ?", (rid,)).fetchone()
            if run_row:
                run = row_to_dict(run_row)
                rollback_row = conn.execute(
                    "SELECT id FROM sleep_runs WHERE rollback_of_run_id = ? AND status = 'completed' LIMIT 1",
                    (rid,),
                ).fetchone()
                resolution_runs.append({
                    "run_id": int(run["id"]),
                    "status": run.get("status"),
                    "rolled_back": rollback_row is not None,
                    "started_at": run.get("started_at"),
                })

        rolled_back_run_ids = {r["run_id"] for r in resolution_runs if r["rolled_back"]}
        for vt in valid_to_history:
            vt["rolled_back"] = vt["run_id"] in rolled_back_run_ids

    finally:
        conn.close()

    return {
        "memory_id": memory_id,
        "memory_summary": {
            "summary_short": memory.get("summary_short"),
            "contradiction_flag": memory.get("contradiction_flag"),
            "valid_from": memory.get("valid_from"),
            "valid_to": memory.get("valid_to"),
            "activity_state": memory.get("activity_state"),
        },
        "conflict_event_count": len(conflict_events),
        "conflict_events": conflict_events,
        "conflict_link_count": len(conflict_links),
        "conflict_links": conflict_links,
        "resolution_run_count": len(resolution_runs),
        "resolution_runs": resolution_runs,
        "valid_to_history": valid_to_history,
    }


@mcp.tool
def get_conflict_reasoning(memory_a_id: int, memory_b_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        result = conflict_explainer.explain_conflict_pair(conn, int(memory_a_id), int(memory_b_id))
    finally:
        conn.close()

    debug = result.get("debug", {})
    signal_scores: dict[str, float] = debug.get("signal_scores", {})
    signals_fired: list[str] = debug.get("signals", [])

    # Sortuj sygnaÄąâ€šy malejĂ„â€¦co po score
    ranked_signals = sorted(signal_scores.items(), key=lambda kv: kv[1], reverse=True)

    # Limity bundle
    context_memory_count = int(debug.get("context_memory_count", 0))
    related_limit = 5  # wartoÄąâ€şĂ„â€ˇ domyÄąâ€şlna w explain_conflict_pair
    bundle_limit_hit = context_memory_count >= related_limit

    return {
        "memory_a_id": int(memory_a_id),
        "memory_b_id": int(memory_b_id),
        "classification": {
            "conflict_kind": result["conflict_kind"],
            "confidence": result["confidence"],
            "conflict_reason": result["conflict_reason"],
            "needs_human_review": result["needs_human_review"],
            "suggested_relation": result["suggested_relation"],
            "suggested_action": result["suggested_action"],
        },
        "signals": {
            "fired": signals_fired,
            "ranked": [{"kind": kind, "score": score} for kind, score in ranked_signals],
            "winner": ranked_signals[0][0] if ranked_signals else None,
            "runner_up": ranked_signals[1][0] if len(ranked_signals) > 1 else None,
        },
        "context": {
            "bundle_summary_shared": debug.get("bundle_summary_shared"),
            "bundle_type_shared": debug.get("bundle_type_shared"),
            "context_memory_count": context_memory_count,
            "context_memory_ids": result["context_memory_ids"],
            "timeline_event_count": debug.get("timeline_event_count", 0),
            "supporting_link_ids": result["supporting_link_ids"],
        },
        "bundle_limits": {
            "related_limit": related_limit,
            "limit_hit": bundle_limit_hit,
            "omitted_note": (
                "Liczba pamiĂ„â„˘ci kontekstowych osiĂ„â€¦gnĂ„â„˘Äąâ€ša limit Ă˘â‚¬â€ť czĂ„â„˘Äąâ€şĂ„â€ˇ powiĂ„â€¦zanych rekordÄ‚Ĺ‚w mogÄąâ€ša zostaĂ„â€ˇ pominiĂ„â„˘ta."
                if bundle_limit_hit else None
            ),
        },
        "explanation": result["explanation"],
    }


@mcp.tool
def get_source_quality(memory_a_id: int, memory_b_id: int) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        row_a = require_memory_row(conn, int(memory_a_id))
        row_b = require_memory_row(conn, int(memory_b_id))
        mem_a = enrich_memory_dict(row_to_dict(row_a))
        mem_b = enrich_memory_dict(row_to_dict(row_b))

        supports_a = conn.execute(
            "SELECT COUNT(*) FROM memory_links WHERE to_memory_id = ? AND relation_type = 'supports'",
            (int(memory_a_id),),
        ).fetchone()[0]
        supports_b = conn.execute(
            "SELECT COUNT(*) FROM memory_links WHERE to_memory_id = ? AND relation_type = 'supports'",
            (int(memory_b_id),),
        ).fetchone()[0]
    finally:
        conn.close()

    breakdown_a = conflict_explainer.source_quality_breakdown(mem_a, supports_count=int(supports_a))
    breakdown_b = conflict_explainer.source_quality_breakdown(mem_b, supports_count=int(supports_b))
    gap = abs(breakdown_a["total_score"] - breakdown_b["total_score"])
    higher_quality_id = (
        int(memory_a_id) if breakdown_a["total_score"] >= breakdown_b["total_score"] else int(memory_b_id)
    )

    return {
        "memory_a_id": int(memory_a_id),
        "memory_b_id": int(memory_b_id),
        "quality_a": breakdown_a,
        "quality_b": breakdown_b,
        "quality_gap": round(gap, 3),
        "higher_quality_memory_id": higher_quality_id,
        "gap_interpretation": (
            "significant" if gap >= 0.35
            else "moderate" if gap >= 0.20
            else "minimal"
        ),
    }


@mcp.tool
def get_conflict_quality_metrics(since: str | None = None, until: str | None = None) -> dict[str, Any]:
    """Returns quality metrics for the conflict explainer subsystem.

    Covers: explained conflicts, review requests, resolutions applied, conflict kinds breakdown,
    review rate, and resolution rate. Optionally filtered by time window (ISO timestamps).
    """
    conn = get_db_connection()
    try:
        params_base: list[Any] = []
        time_filter = ""
        if since:
            time_filter += " AND created_at >= ?"
            params_base.append(since)
        if until:
            time_filter += " AND created_at <= ?"
            params_base.append(until)

        def _count(event_type: str) -> int:
            row = conn.execute(
                f"SELECT COUNT(*) FROM timeline_events WHERE event_type = ?{time_filter}",
                [event_type] + params_base,
            ).fetchone()
            return int(row[0]) if row else 0

        explained = _count("conflict.explained")
        review_requested = _count("conflict.review_requested")
        resolution_applied = _count("conflict.resolution_applied")

        # Count per conflict kind from conflict.explained events
        kind_rows = conn.execute(
            f"SELECT json_extract(payload_json, '$.conflict_kind') AS kind, COUNT(*) AS cnt "
            f"FROM timeline_events WHERE event_type = 'conflict.explained'{time_filter} "
            f"GROUP BY kind ORDER BY cnt DESC",
            params_base,
        ).fetchall()
        by_kind = {str(r[0]): int(r[1]) for r in kind_rows if r[0]}

        # Count needs_human_review=true from conflict.explained
        human_review_count = conn.execute(
            f"SELECT COUNT(*) FROM timeline_events "
            f"WHERE event_type = 'conflict.explained' "
            f"AND json_extract(payload_json, '$.needs_human_review') = 1{time_filter}",
            params_base,
        ).fetchone()
        human_review_total = int(human_review_count[0]) if human_review_count else 0

        review_rate = round(review_requested / explained, 3) if explained > 0 else None
        resolution_rate = round(resolution_applied / explained, 3) if explained > 0 else None

        # Feature flags status
        flag_keys = [CONFLICT_EXPLAINER_FLAG_KEY, CONFLICT_PREVIEW_RESOLUTION_FLAG_KEY, CONFLICT_AUTO_RESOLUTION_FLAG_KEY]
        flags_status = {}
        for fk in flag_keys:
            flags_status[fk] = _is_conflict_feature_active(conn, fk)

    finally:
        conn.close()

    return {
        "period": {"since": since, "until": until},
        "explained_count": explained,
        "review_requested_count": review_requested,
        "resolution_applied_count": resolution_applied,
        "needs_human_review_count": human_review_total,
        "by_conflict_kind": by_kind,
        "review_rate": review_rate,
        "resolution_rate": resolution_rate,
        "feature_flags": flags_status,
    }


@mcp.tool
def get_conflict_system_status() -> dict[str, Any]:
    """Returns operational status of the conflict explainer subsystem.

    Use for health checks, operator dashboards, and pre-flight verification.
    Returns feature flag states, DB table counts, and a human-readable readiness verdict.
    """
    conn = get_db_connection()
    try:
        flag_keys = [CONFLICT_EXPLAINER_FLAG_KEY, CONFLICT_PREVIEW_RESOLUTION_FLAG_KEY, CONFLICT_AUTO_RESOLUTION_FLAG_KEY]
        flags: dict[str, bool] = {fk: _is_conflict_feature_active(conn, fk) for fk in flag_keys}

        conflicted_count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE contradiction_flag = 1 AND activity_state = 'active'"
        ).fetchone()[0]
        conflict_links_count = conn.execute(
            "SELECT COUNT(*) FROM memory_links WHERE relation_type IN ('contradicts', 'supersedes')"
        ).fetchone()[0]
        open_reviews = conn.execute(
            "SELECT COUNT(DISTINCT json_extract(payload_json, '$.base_memory_ids[0]')) "
            "FROM timeline_events WHERE event_type = 'conflict.review_requested'"
        ).fetchone()[0]
        last_explained = conn.execute(
            "SELECT created_at FROM timeline_events WHERE event_type = 'conflict.explained' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        last_resolved = conn.execute(
            "SELECT created_at FROM timeline_events WHERE event_type = 'conflict.resolution_applied' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        resolution_runs = conn.execute(
            "SELECT COUNT(*) FROM sleep_runs WHERE mode = 'conflict_resolution_run' AND status = 'completed'"
        ).fetchone()[0]
    finally:
        conn.close()

    all_flags_active = all(flags.values())
    explainer_active = flags[CONFLICT_EXPLAINER_FLAG_KEY]

    if not explainer_active:
        readiness = "disabled"
    elif all_flags_active:
        readiness = "fully_operational"
    else:
        readiness = "partially_enabled"

    return {
        "readiness": readiness,
        "feature_flags": flags,
        "db_stats": {
            "active_conflicted_memories": int(conflicted_count),
            "conflict_links_count": int(conflict_links_count),
            "open_reviews_estimate": int(open_reviews),
            "completed_resolution_runs": int(resolution_runs),
        },
        "last_activity": {
            "last_explained_at": last_explained[0] if last_explained else None,
            "last_resolved_at": last_resolved[0] if last_resolved else None,
        },
    }


@mcp.tool
def get_conflict_clusters(include_members: bool = True) -> dict[str, Any]:
    """Returns conflict clusters Ă˘â‚¬â€ť connected components in the conflict graph.

    Each cluster groups memories linked by 'contradicts' or 'supersedes' relations.
    Identifies the central memory (highest degree) and the divergence source
    (memory causing the most direct contradictions).

    Set include_members=False to get a compact summary without full member lists.
    """
    conn = get_db_connection()
    try:
        clusters = conflict_logic.build_conflict_clusters(conn)
    finally:
        conn.close()

    if not include_members:
        clusters = [
            {k: v for k, v in c.items() if k != "member_ids"}
            for c in clusters
        ]

    unresolved_count = sum(1 for c in clusters if c.get("has_unresolved"))
    large_clusters = [c for c in clusters if c["size"] >= 3]

    return {
        "cluster_count": len(clusters),
        "total_clustered_memories": sum(c["size"] for c in clusters),
        "unresolved_cluster_count": unresolved_count,
        "large_cluster_count": len(large_clusters),
        "clusters": clusters,
    }


@mcp.tool
def get_conflict_registry(include_resolved: bool = True) -> dict[str, Any]:
    """Return a registry-style view of conflict clusters with lifecycle status."""
    conn = get_db_connection()
    try:
        clusters = conflict_logic.build_conflict_clusters(conn)
        registry = [_conflict_registry_entry(conn, cluster) for cluster in clusters]
    finally:
        conn.close()

    if not include_resolved:
        registry = [item for item in registry if item.get("status") == "open"]

    counts = {
        "open": sum(1 for item in registry if item.get("status") == "open"),
        "resolved": sum(1 for item in registry if item.get("status") == "resolved"),
        "ignored": sum(1 for item in registry if item.get("status") == "ignored"),
    }
    return {
        "status": "ok",
        "count": len(registry),
        "counts_by_status": counts,
        "items": registry,
    }


@mcp.tool
def get_conflict_report(memory_a_id: int, memory_b_id: int) -> dict[str, Any]:
    """Returns a comprehensive operator report for a conflict pair.

    Combines: classification + explanation, preview of proposed resolution,
    conflict history for both memories, and a decision summary.
    Designed as a single-call operator view Ă˘â‚¬â€ť no need to call multiple tools separately.
    """
    conn = get_db_connection()
    try:
        # Explanation (classify + explain)
        explanation = conflict_explainer.explain_conflict_pair(conn, int(memory_a_id), int(memory_b_id))

        # Preview resolution
        preview = conflict_explainer.preview_resolution(conn, int(memory_a_id), int(memory_b_id))

        # History for both memories
        def _history_summary(mid: int) -> dict[str, Any]:
            all_events = timeline.timeline_query(conn, limit=200, memory_id=mid, row_to_dict=row_to_dict)
            conflict_events = [e for e in all_events if str(e.get("event_type", "")).startswith("conflict.")]
            link_rows = conn.execute(
                "SELECT relation_type, COUNT(*) AS cnt FROM memory_links "
                "WHERE (from_memory_id = ? OR to_memory_id = ?) "
                "AND relation_type IN ('contradicts', 'supersedes', 'relates_to') "
                "GROUP BY relation_type",
                (mid, mid),
            ).fetchall()
            return {
                "memory_id": mid,
                "conflict_event_count": len(conflict_events),
                "recent_events": [
                    {"event_type": e.get("event_type"), "created_at": e.get("created_at")}
                    for e in conflict_events[:5]
                ],
                "link_summary": {str(r[0]): int(r[1]) for r in link_rows},
            }

        history_a = _history_summary(int(memory_a_id))
        history_b = _history_summary(int(memory_b_id))

    finally:
        conn.close()

    # Decision summary
    auto_applicable = bool(preview.get("can_auto_apply"))
    needs_review = bool(explanation.get("needs_human_review"))
    if preview.get("skip_reason") == "canonical_relation_requires_guarded_route":
        recommended_action = (
            "guarded_supersession_required"
            if explanation.get("suggested_relation") == "supersedes"
            else "guarded_relation_review_required"
        )
    elif auto_applicable:
        recommended_action = "apply_conflict_resolution"
    elif needs_review:
        recommended_action = "manual_review_required"
    else:
        recommended_action = "no_action"

    return {
        "memory_a_id": int(memory_a_id),
        "memory_b_id": int(memory_b_id),
        "conflict_kind": explanation["conflict_kind"],
        "confidence": explanation["confidence"],
        "explanation": explanation["explanation"],
        "needs_human_review": needs_review,
        "suggested_relation": explanation["suggested_relation"],
        "suggested_action": explanation["suggested_action"],
        "resolution_preview": {
            "can_auto_apply": auto_applicable,
            "skip_reason": preview.get("skip_reason"),
            "proposed_changes": preview.get("proposed_changes", []),
            "canonical_guarded_routes": preview.get("canonical_guarded_routes", {}),
            "proposed_changes_count": len(preview.get("proposed_changes", [])),
        },
        "history": {
            "memory_a": history_a,
            "memory_b": history_b,
        },
        "decision_summary": {
            "recommended_action": recommended_action,
            "auto_applicable": auto_applicable,
            "needs_review": needs_review,
            "conflict_kind": explanation["conflict_kind"],
        },
        "registry_status": (
            "resolved" if auto_applicable and not needs_review
            else "open" if needs_review
            else "open"
        ),
    }


@mcp.tool
def record_conflict_decision(
    memory_a_id: int,
    memory_b_id: int,
    decision: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Records an operator's manual decision for a conflict pair.

    decision must be one of: 'approved', 'rejected', 'deferred', 'false_positive'.

    Records a timeline event 'conflict.decision_recorded' and optionally triggers
    auto-resolution if decision='approved' and the pair supports it.
    """
    valid_decisions = {"approved", "rejected", "deferred", "false_positive"}
    normalized = str(decision).strip().lower()
    if normalized not in valid_decisions:
        return {"status": "error", "error": f"decision musi byĂ„â€ˇ jednym z: {', '.join(sorted(valid_decisions))}"}

    conn = get_db_connection()
    try:
        base_ids = sorted([int(memory_a_id), int(memory_b_id)])
        operation_id = timeline.new_operation_id("conflict")

        timeline.record_timeline_event(
            conn,
            event_type="conflict.decision_recorded",
            memory_id=base_ids[0],
            related_memory_id=base_ids[1],
            operation_id=operation_id,
            origin="operator",
            timeline_scope="memory",
            semantic_kind="decision",
            title=f"Conflict decision: {normalized}",
            payload={
                "decision": normalized,
                "notes": notes,
                "base_memory_ids": base_ids,
            },
        )

        apply_result: dict[str, Any] | None = None
        if normalized == "approved":
            preview = conflict_explainer.preview_resolution(conn, int(memory_a_id), int(memory_b_id))
            if preview.get("can_auto_apply"):
                conn.commit()
                conn.close()
                apply_result = apply_conflict_resolution(int(memory_a_id), int(memory_b_id), notes=notes)
                return {
                    "status": "approved_and_applied",
                    "decision": normalized,
                    "memory_a_id": int(memory_a_id),
                    "memory_b_id": int(memory_b_id),
                    "apply_result": apply_result,
                    "operation_id": operation_id,
                }

        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "status": "recorded",
        "decision": normalized,
        "memory_a_id": int(memory_a_id),
        "memory_b_id": int(memory_b_id),
        "notes": notes,
        "operation_id": operation_id,
        "apply_result": apply_result,
    }


@mcp.tool
def preview_conflicts_v1(notes: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        run_id = create_sleep_run(conn, mode="conflict_preview", freedom_level=0, notes=notes)
        conflict_candidates = conflict_logic.get_conflict_candidates(conn)
        flagged_ids: set[int] = set()
        links_to_create_count = 0
        for pair in conflict_candidates:
            flagged_ids.add(int(pair["memory_a_id"]))
            flagged_ids.add(int(pair["memory_b_id"]))
            if not bool(pair["contradiction_link_exists"]):
                links_to_create_count += 1
            add_sleep_action(conn, run_id, "conflict_candidate", int(pair["memory_a_id"]), {"memory_a_id": pair["memory_a_id"], "memory_b_id": pair["memory_b_id"], "contradiction_link_exists": pair["contradiction_link_exists"]}, {"relation_type": "contradicts", "memory_a_id": pair["memory_a_id"], "memory_b_id": pair["memory_b_id"]}, "same_summary_conflicting_signal")
        scanned_count = conn.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
        finalize_sleep_run(conn, run_id, status="preview_completed", scanned_count=int(scanned_count), changed_count=0, archived_count=0, downgraded_count=0, duplicate_count=0, conflict_count=len(conflict_candidates), created_summary_count=0)
        return {"status": "preview_completed", "run_id": run_id, "scanned_count": int(scanned_count), "conflict_candidates": conflict_candidates, "summary": {"conflict_count": len(conflict_candidates), "flagged_memory_count": len(flagged_ids), "links_to_create_count": links_to_create_count}}
    finally:
        conn.close()


@mcp.tool
def run_conflicts_v1(notes: str | None = None) -> dict[str, Any]:
    """Retired legacy heuristic conflict writer; canonical contradictions require review/evidence."""
    return {
        "status": "blocked",
        "schema": "legacy_conflicts_v1_retirement.v1",
        "reason": "legacy_conflicts_v1_retired",
        "links_created": [],
        "flagged_changes": [],
        "run_id": None,
        "canonical_route": "memory.capture_reconciliation -> conflict_review -> open_unresolved_conflict_review",
        "legacy_forensics_helper": "run_conflicts_v1_legacy_unsafe",
        "safety": {"mutations_performed": 0, "semantic_heuristic_truth_write_allowed": False},
    }


def run_conflicts_v1_legacy_unsafe(notes: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        run_id = create_sleep_run(conn, mode="conflict_run", freedom_level=0, notes=notes)
        conflict_candidates = conflict_logic.get_conflict_candidates(conn)
        links_created: list[dict[str, Any]] = []
        flagged_changes: list[dict[str, Any]] = []
        already_flagged: set[int] = set()
        for pair in conflict_candidates:
            memory_a_id = int(pair["memory_a_id"])
            memory_b_id = int(pair["memory_b_id"])
            source_id = min(memory_a_id, memory_b_id)
            target_id = max(memory_a_id, memory_b_id)
            if not conflict_logic.contradiction_link_exists(conn, source_id, target_id):
                item = _create_link(conn, source_id, target_id, "contradicts", 0.9, "conflicts_v1_auto")
                links_created.append(item)
                add_sleep_action(conn, run_id, "conflict_link_created", source_id, None, item, "same_summary_conflicting_signal")
            for memory_id in (memory_a_id, memory_b_id):
                if memory_id in already_flagged:
                    continue
                memory = require_memory_row(conn, memory_id)
                old_flag = int(memory["contradiction_flag"] or 0)
                if old_flag != 1:
                    conn.execute("UPDATE memories SET contradiction_flag = 1 WHERE id = ?", (memory_id,))
                    flagged_changes.append({"memory_id": memory_id, "old_contradiction_flag": old_flag, "new_contradiction_flag": 1})
                    add_sleep_action(conn, run_id, "conflict_flagged", memory_id, {"contradiction_flag": old_flag}, {"contradiction_flag": 1}, "same_summary_conflicting_signal")
                already_flagged.add(memory_id)
        conn.commit()
        scanned_count = conn.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
        finalize_sleep_run(conn, run_id, status="completed", scanned_count=int(scanned_count), changed_count=len(links_created) + len(flagged_changes), archived_count=0, downgraded_count=0, duplicate_count=0, conflict_count=len(conflict_candidates), created_summary_count=0)
        return {"status": "completed", "run_id": run_id, "scanned_count": int(scanned_count), "conflict_candidates": conflict_candidates, "links_created": links_created, "flagged_changes": flagged_changes, "summary": {"conflict_count": len(conflict_candidates), "links_created_count": len(links_created), "flagged_memory_count": len(flagged_changes), "changed_count": len(links_created) + len(flagged_changes)}}
    finally:
        conn.close()


# --- Sandman dream-linking helpers -------------------------------------------------
_DREAM_STOPWORDS = {
    "oraz", "jest", "jako", "jego", "jej", "dla", "przez", "ktore", "ktÄ‚Ĺ‚re",
    "taki", "taka", "takie", "tego", "tym", "ten", "czy", "nie", "sie", "siĂ„â„˘",
    "the", "and", "with", "from", "that", "this", "into", "memory", "wspomnienie",
}


_DREAM_BROAD_LINK_TERMS = {
    "demo-project", "mpbm", "mapi", "agent", "mapi", "project",
    "project-context", "memory", "memories", "wspomnienie", "wspomnienia",
    "user", "uÄąÄ˝ytkownik", "assistant", "current", "conversation", "context",
    "asystenta", "firmowego", "firma", "firmy", "work", "not", "for",
}


def _sandman_existing_link_keys(conn) -> set[tuple[int, int, str]]:
    rows = conn.execute("SELECT from_memory_id, to_memory_id, relation_type FROM memory_links").fetchall()
    return {(int(row["from_memory_id"]), int(row["to_memory_id"]), str(row["relation_type"])) for row in rows}


def _sandman_tokenize(value: object) -> set[str]:
    import re

    text = str(value or "").lower()
    words = re.findall(r"[a-zĂ„â€¦Ă„â€ˇĂ„â„˘Äąâ€šÄąâ€žÄ‚Ĺ‚Äąâ€şÄąĹźÄąÄ˝0-9_\-]{3,}", text, flags=re.IGNORECASE)
    return {word.strip("-_") for word in words if word.strip("-_") and word not in _DREAM_STOPWORDS}


def _sandman_tags(value: object) -> set[str]:
    return {item.strip().lower() for item in str(value or "").split(",") if item.strip()}


def _sandman_inferred_terms(memory: dict[str, object]) -> set[str]:
    raw_text = " ".join(
        str(memory.get(key) or "")
        for key in ("content", "summary_short", "tags", "memory_type")
    ).lower()
    tokens = _sandman_tokenize(raw_text) | _sandman_tags(memory.get("tags"))
    inferred: set[str] = set()

    if tokens & {"blog", "blogposts", "routemeta", "metatitle", "metadescription", "react", "frontend", "build-success", "technical-section"}:
        inferred.update({"website", "websites", "site", "frontend", "content", "build", "react", "implementation"})
    if tokens & {"website", "websites", "strona", "strony", "stronach", "internetowych", "domain", "domena"}:
        inferred.update({"website", "websites", "site", "web", "frontend"})
    if tokens & {"facebook", "bio", "copywriting", "marketing", "pozycjonowanie", "positioning"}:
        inferred.update({"content", "marketing", "copywriting", "positioning"})
    if tokens & {"docs", "document", "documentation", "dokument", "dokumentacja"}:
        inferred.update({"documents", "documentation", "content"})
    if tokens & {"build", "build-success", "npm", "test", "validation", "py_compile"}:
        inferred.update({"validates", "build", "test"})

    return inferred - _DREAM_BROAD_LINK_TERMS


def _sandman_scope_clause(workspace_id: int | None, project_key: str | None) -> tuple[str, list[object]]:
    clauses: list[str] = ["activity_state = 'active'"]
    params: list[object] = []
    if workspace_id is not None:
        clauses.append("workspace_id = ?")
        params.append(int(workspace_id))
    if project_key:
        clauses.append("project_key = ?")
        params.append(project_key)
    return " AND ".join(clauses), params


def _sandman_extract_mention_candidates(conn, memories: list[dict[str, object]], existing: set[tuple[int, int, str]], max_links: int) -> list[dict[str, object]]:
    import re

    existing_ids = {int(memory["id"]) for memory in memories}
    candidates: list[dict[str, object]] = []
    seen: set[tuple[int, int, str]] = set()
    for memory in memories:
        source_id = int(memory["id"])
        text = f"{memory.get('content') or ''} {memory.get('summary_short') or ''}"
        for raw_id in re.findall(r"\[(\d+)\]", text):
            target_id = int(raw_id)
            if target_id == source_id or target_id not in existing_ids:
                continue
            key = (source_id, target_id, "mentions")
            if key in existing or key in seen:
                continue
            seen.add(key)
            candidates.append({
                "from_memory_id": source_id,
                "to_memory_id": target_id,
                "relation_type": "mentions",
                "weight": 0.88,
                "reason": "memory_text_contains_bracket_id_reference",
            })
            if len(candidates) >= max_links:
                return candidates
    return candidates


def _sandman_prepare_memories(memories: list[dict[str, object]]) -> list[dict[str, object]]:
    prepared: list[dict[str, object]] = []
    for memory in memories:
        content_tokens = _sandman_tokenize(memory.get("content"))
        summary_tokens = _sandman_tokenize(memory.get("summary_short"))
        raw_tags = _sandman_tags(memory.get("tags"))
        inferred_terms = _sandman_inferred_terms(memory)
        semantic_tags = raw_tags | inferred_terms
        prepared.append({
            **memory,
            "_tokens": content_tokens | summary_tokens | semantic_tags,
            "_tags": semantic_tags,
            "_raw_tags": raw_tags,
            "_inferred_terms": inferred_terms,
        })
    return prepared

def _sandman_relation_type(left: dict[str, object], right: dict[str, object], score: float, common_tags: set[str], common_tokens: set[str]) -> str:
    common_words = set(common_tags) | set(common_tokens)
    common_text = " ".join(sorted(common_words))
    left_tags = _sandman_tags(left.get("tags"))
    right_tags = _sandman_tags(right.get("tags"))
    both_tags = left_tags & right_tags

    if any(word in common_text for word in ("credential", "credentials", "auth-risk", "security", "rotate-key", "oauth", "bearer-token", "basic-auth")):
        return "risk_for"
    if any(word in common_text for word in ("metric", "metrics", "coverage", "graph")) or "metrics" in both_tags:
        return "metric_for"
    if any(word in common_text for word in ("react", "routemeta", "blogposts", "frontend", "build", "implementation")):
        return "implements"
    if any(word in common_text for word in ("docs", "document", "documentation", "documents", "dokument", "copywriting", "bio", "content")):
        return "documents"
    if any(word in common_text for word in ("error", "problem", "troubleshooting", "bug", "fix", "napraw", "lifespan")):
        return "fixes"
    if any(word in common_text for word in ("installation", "installer", "setup", "systemd", "caddy", "ssh", "vps", "linux", "ubuntu", "config", "uvicorn")):
        return "configures"
    if any(word in common_text for word in ("validation", "validate", "test", "success", "healthcheck", "health", "py_compile")):
        return "validates"
    if bool(left.get("project_key") and left.get("project_key") == right.get("project_key")) and score < 0.62:
        return "same_project"
    return "related_to"

def _sandman_optics_verdict(
    relation_type: str,
    score: float,
    common_tags: set[str],
    common_tokens: set[str],
    same_project: bool,
    same_type: bool,
) -> dict[str, object] | None:
    strong_tags = set(common_tags) - _DREAM_BROAD_LINK_TERMS
    strong_tokens = set(common_tokens) - _DREAM_BROAD_LINK_TERMS
    strong_signal = (2 * len(strong_tags)) + len(strong_tokens)

    relation_thresholds = {
        "risk_for": 0.58,
        "metric_for": 0.62,
        "documents": 0.58,
        "implements": 0.58,
        "fixes": 0.58,
        "configures": 0.58,
        "validates": 0.58,
        "same_project": 0.62,
        "related_to": 0.58,
    }
    min_required = relation_thresholds.get(relation_type, 0.58)

    if score < min_required:
        return None

    if relation_type == "same_project" and (len(strong_tags) < 1 and len(strong_tokens) < 3):
        return None
    if relation_type in {"related_to", "documents", "implements", "fixes", "configures", "validates"} and strong_signal < 3:
        return None
    if relation_type == "risk_for" and not (set(common_tags) | set(common_tokens)) & {"credential", "credentials", "secret", "security", "rotate-secret", "oauth", "bearer-token", "basic-auth"}:
        return None
    if relation_type == "metric_for" and not (set(common_tags) | set(common_tokens)) & {"metric", "metrics", "coverage", "graph"}:
        return None

    if score >= 0.84 and strong_signal >= 7:
        quality = "trusted"
    elif score >= 0.68 and strong_signal >= 4:
        quality = "probable"
    else:
        quality = "weak"

    # The optician does not allow weak links into the dream graph yet. They can
    # return later as explicit review candidates when a link-review queue exists.
    if quality == "weak":
        return None

    return {
        "quality_class": quality,
        "optics_score": round(score + min(0.08, strong_signal / 100), 3),
        "strong_shared_tag_count": len(strong_tags),
        "strong_shared_term_count": len(strong_tokens),
    }

def _sandman_build_similarity_candidate(
    left: dict[str, object],
    right: dict[str, object],
    existing: set[tuple[int, int, str]],
    seen: set[tuple[int, int, str]],
    *,
    min_score: float = 0.42,
    reason_prefix: str | None = None,
) -> dict[str, object] | None:
    left_id = int(left["id"])
    right_id = int(right["id"])
    if left_id == right_id:
        return None
    common_tags = set(left.get("_tags", set())) & set(right.get("_tags", set()))
    common_tokens = set(left.get("_tokens", set())) & set(right.get("_tokens", set()))
    same_project = bool(left.get("project_key") and left.get("project_key") == right.get("project_key"))
    same_type = bool(left.get("memory_type") and left.get("memory_type") == right.get("memory_type"))
    score = 0.0
    reasons: list[str] = []
    if common_tags:
        score += min(0.35, 0.12 * len(common_tags))
        reasons.append("shared_tags:" + ",".join(sorted(common_tags)[:5]))
    if same_project:
        score += 0.20
        reasons.append("same_project")
    if same_type:
        score += 0.10
        reasons.append("same_memory_type")
    if common_tokens:
        score += min(0.35, 0.04 * len(common_tokens))
        reasons.append("shared_terms:" + ",".join(sorted(common_tokens)[:6]))
    if score < min_score:
        return None
    source_id, target_id = (left_id, right_id) if left_id < right_id else (right_id, left_id)
    relation_type = _sandman_relation_type(left, right, score, common_tags, common_tokens)
    optics = _sandman_optics_verdict(relation_type, score, common_tags, common_tokens, same_project, same_type)
    if optics is None:
        return None
    key = (source_id, target_id, relation_type)
    reverse_key = (target_id, source_id, relation_type)
    if key in existing or reverse_key in existing or key in seen or reverse_key in seen:
        return None
    seen.add(key)
    reason_text = ";".join(reasons)
    if reason_prefix:
        reason_text = f"{reason_prefix};{reason_text}"
    return {
        "from_memory_id": source_id,
        "to_memory_id": target_id,
        "relation_type": relation_type,
        "weight": round(min(0.92, score), 3),
        "reason": reason_text,
        "shared_tag_count": len(common_tags),
        "shared_term_count": len(common_tokens),
        **optics,
    }

def _sandman_linked_ids_in_scope(conn, memory_ids: set[int]) -> set[int]:
    if not memory_ids:
        return set()
    rows = conn.execute("SELECT from_memory_id, to_memory_id FROM memory_links WHERE archived_at IS NULL").fetchall()
    linked: set[int] = set()
    for row in rows:
        left_id = int(row["from_memory_id"])
        right_id = int(row["to_memory_id"])
        if left_id in memory_ids:
            linked.add(left_id)
        if right_id in memory_ids:
            linked.add(right_id)
    return linked


def _sandman_extract_orphan_rescue_candidates(conn, prepared: list[dict[str, object]], existing: set[tuple[int, int, str]], max_links: int) -> list[dict[str, object]]:
    memory_ids = {int(memory["id"]) for memory in prepared}
    linked_ids = _sandman_linked_ids_in_scope(conn, memory_ids)
    orphan_memories = [memory for memory in prepared if int(memory["id"]) not in linked_ids]
    candidates: list[dict[str, object]] = []
    seen: set[tuple[int, int, str]] = set()
    for orphan in orphan_memories:
        possible: list[dict[str, object]] = []
        for other in prepared:
            if int(other["id"]) == int(orphan["id"]):
                continue
            candidate = _sandman_build_similarity_candidate(
                orphan,
                other,
                existing,
                seen,
                min_score=0.34,
                reason_prefix="orphan_rescue",
            )
            if candidate is not None:
                possible.append(candidate)
        possible.sort(key=lambda item: (float(item["weight"]), int(item.get("shared_tag_count", 0)), int(item.get("shared_term_count", 0))), reverse=True)
        for item in possible[:3]:
            candidates.append(item)
            if len(candidates) >= max_links:
                return candidates
    return candidates


def _sandman_extract_random_walk_candidates(prepared: list[dict[str, object]], existing: set[tuple[int, int, str]], max_links: int) -> list[dict[str, object]]:
    import random

    pairs: list[tuple[int, int]] = []
    for index in range(len(prepared)):
        for other_index in range(index + 1, len(prepared)):
            pairs.append((index, other_index))
    random.SystemRandom().shuffle(pairs)

    candidates: list[dict[str, object]] = []
    seen: set[tuple[int, int, str]] = set()
    for left_index, right_index in pairs:
        candidate = _sandman_build_similarity_candidate(
            prepared[left_index],
            prepared[right_index],
            existing,
            seen,
            min_score=0.42,
            reason_prefix="random_walk",
        )
        if candidate is None:
            continue
        candidates.append(candidate)
        if len(candidates) >= max_links:
            break
    return candidates


def _sandman_extract_similarity_candidates(conn, memories: list[dict[str, object]], existing: set[tuple[int, int, str]], max_links: int) -> list[dict[str, object]]:
    prepared = _sandman_prepare_memories(memories)
    candidates: list[dict[str, object]] = []

    orphan_candidates = _sandman_extract_orphan_rescue_candidates(conn, prepared, existing, max_links)
    candidates.extend(orphan_candidates)

    augmented_existing = set(existing)
    for item in candidates:
        augmented_existing.add((int(item["from_memory_id"]), int(item["to_memory_id"]), str(item["relation_type"])))

    remaining = max(0, max_links - len(candidates))
    if remaining > 0:
        random_candidates = _sandman_extract_random_walk_candidates(prepared, augmented_existing, remaining)
        candidates.extend(random_candidates)

    deduped: list[dict[str, object]] = []
    seen: set[tuple[int, int, str]] = set()
    for item in candidates:
        key = (int(item["from_memory_id"]), int(item["to_memory_id"]), str(item["relation_type"]))
        reverse_key = (key[1], key[0], key[2])
        if key in seen or reverse_key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:max_links]

def _sandman_graph_density_stats(conn, workspace_id: int | None = None, project_key: str | None = None) -> dict[str, object]:
    where_sql, params = _sandman_scope_clause(workspace_id, project_key)
    memory_rows = conn.execute(
        f"SELECT id FROM memories WHERE {where_sql}",
        params,
    ).fetchall()
    memory_ids = {int(row["id"]) for row in memory_rows}
    memory_count = len(memory_ids)
    if memory_count == 0:
        return {
            "memory_count": 0,
            "link_count": 0,
            "linked_memory_count": 0,
            "unlinked_memory_count": 0,
            "links_per_memory": 0.0,
            "avg_degree": 0.0,
        }

    link_rows = conn.execute(
        """
        SELECT from_memory_id, to_memory_id
        FROM memory_links
        WHERE archived_at IS NULL
        """
    ).fetchall()
    internal_links = []
    linked_ids: set[int] = set()
    for row in link_rows:
        left_id = int(row["from_memory_id"])
        right_id = int(row["to_memory_id"])
        if left_id in memory_ids and right_id in memory_ids:
            internal_links.append((left_id, right_id))
            linked_ids.add(left_id)
            linked_ids.add(right_id)

    link_count = len(internal_links)
    linked_memory_count = len(linked_ids)
    unlinked_memory_count = max(0, memory_count - linked_memory_count)
    links_per_memory = link_count / memory_count
    avg_degree = (2 * link_count) / memory_count
    return {
        "memory_count": memory_count,
        "link_count": link_count,
        "linked_memory_count": linked_memory_count,
        "unlinked_memory_count": unlinked_memory_count,
        "links_per_memory": round(links_per_memory, 3),
        "avg_degree": round(avg_degree, 3),
    }


def _sandman_adaptive_dream_link_limit(
    conn,
    workspace_id: int | None = None,
    project_key: str | None = None,
    requested_max_links: int = 80,
) -> dict[str, object]:
    stats = _sandman_graph_density_stats(conn, workspace_id=workspace_id, project_key=project_key)
    memory_count = int(stats["memory_count"])
    link_count = int(stats["link_count"])
    unlinked_memory_count = int(stats["unlinked_memory_count"])
    links_per_memory = float(stats["links_per_memory"])

    if memory_count <= 0:
        return {"limit": 0, "reason": "empty_scope", "stats": stats}

    # Sandman should still rescue isolated memories, but once the graph is dense
    # he must walk slower. This is a soft brake, not a handbrake.
    if links_per_memory >= 5.0:
        density_cap = 4
        density_band = "very_dense"
    elif links_per_memory >= 4.0:
        density_cap = 8
        density_band = "dense"
    elif links_per_memory >= 3.25:
        density_cap = 12
        density_band = "warming_up"
    elif links_per_memory >= 2.25:
        density_cap = 24
        density_band = "medium"
    else:
        density_cap = requested_max_links
        density_band = "sparse"

    orphan_bonus = min(6, unlinked_memory_count * 2)
    target_links_per_memory = 4.5
    target_budget = max(0, int((memory_count * target_links_per_memory) - link_count))
    if unlinked_memory_count > 0:
        target_budget = max(target_budget, orphan_bonus)

    limit = min(int(requested_max_links), int(density_cap) + int(orphan_bonus), int(target_budget))
    if unlinked_memory_count > 0 and limit <= 0:
        limit = min(int(requested_max_links), int(orphan_bonus) or 2)
    limit = max(0, limit)

    return {
        "limit": limit,
        "reason": "adaptive_density_brake",
        "density_band": density_band,
        "density_cap": density_cap,
        "orphan_bonus": orphan_bonus,
        "target_budget": target_budget,
        "requested_max_links": requested_max_links,
        "stats": stats,
    }


def _sandman_get_dream_link_candidates(conn, workspace_id: int | None = None, project_key: str | None = None, max_links: int = 80) -> list[dict[str, object]]:
    brake = _sandman_adaptive_dream_link_limit(conn, workspace_id=workspace_id, project_key=project_key, requested_max_links=max_links)
    effective_max_links = int(brake.get("limit", 0))
    if effective_max_links <= 0:
        return []

    # Prefer the deterministic memory_linking_pass engine for Sandman's dream links.
    # The older similarity/mention dream linker remains as a fallback, but Sandman
    # should not depend on hand-curated chat passes to keep the graph connected.
    try:
        deterministic_candidates = _get_memory_linking_candidates(
            conn,
            project_key=project_key,
            limit=effective_max_links,
            max_links_per_memory=8,
            min_score=0.47,
        )
    except NameError:
        deterministic_candidates = []

    if deterministic_candidates:
        candidates = deterministic_candidates[:effective_max_links]
        for item in candidates:
            item["adaptive_brake"] = brake
            item["sandman_linker"] = "memory_linking_pass_v1"
            item["reason"] = "sandman_forced_memory_linking_pass"
        return candidates

    where_sql, params = _sandman_scope_clause(workspace_id, project_key)
    rows = conn.execute(
        f"""
        SELECT id, content, summary_short, memory_type, tags, project_key, layer_code, area_code, state_code, scope_code, activity_state
        FROM memories
        WHERE {where_sql}
        ORDER BY COALESCE(last_recalled_at, last_accessed_at, created_at, '') DESC, id DESC
        LIMIT 500
        """,
        params,
    ).fetchall()
    memories = [row_to_dict(row) for row in rows]
    existing = _sandman_existing_link_keys(conn)

    similarity_candidates = _sandman_extract_similarity_candidates(conn, memories, existing, effective_max_links)
    remaining = max(0, effective_max_links - len(similarity_candidates))
    if remaining <= 0:
        candidates = similarity_candidates[:effective_max_links]
    else:
        mention_candidates = _sandman_extract_mention_candidates(conn, memories, existing, remaining)
        candidates = (similarity_candidates + mention_candidates)[:effective_max_links]

    for item in candidates:
        item["adaptive_brake"] = brake
        item["sandman_linker"] = "legacy_similarity_mention"
    return candidates

def _sandman_make_dream_story(candidates: list[dict[str, object]], links_created: list[dict[str, object]], run_id: int, project_key: str | None = None) -> str:
    source_items = links_created or candidates[:12]
    if not source_items:
        return "Sandman wrÄ‚Ĺ‚ciÄąâ€š z pustymi kieszeniami. W korytarzu pamiĂ„â„˘ci staÄąâ€ša tylko szafa, ktÄ‚Ĺ‚ra udawaÄąâ€ša drzwi."

    relation_images = {
        "mentions": "numer zapisany na wewnĂ„â„˘trznej stronie powieki wskazaÄąâ€š innĂ„â€¦ kartkĂ„â„˘",
        "related_to": "dwie kartki rozpoznaÄąâ€šy ten sam kurz i przysunĂ„â„˘Äąâ€šy siĂ„â„˘ do siebie",
        "same_project": "pokÄ‚Ĺ‚j przesunĂ„â€¦Äąâ€š Äąâ€şciany, ÄąÄ˝eby obce notatki staÄąâ€šy siĂ„â„˘ sĂ„â€¦siadami",
        "documents": "papier poÄąâ€šoÄąÄ˝yÄąâ€š cieÄąâ€ž na mechanizmie, ktÄ‚Ĺ‚ry wczeÄąâ€şniej nie miaÄąâ€š imienia",
        "implements": "maÄąâ€šy mechanizm wyrÄ‚Ĺ‚sÄąâ€š z notatki i zaczĂ„â€¦Äąâ€š udawaĂ„â€ˇ architekturĂ„â„˘",
        "fixes": "rdza znalazÄąâ€ša Äąâ€şrubkĂ„â„˘, a Äąâ€şrubka przypomniaÄąâ€ša sobie gwint",
        "configures": "klucz obrÄ‚Ĺ‚ciÄąâ€š siĂ„â„˘ w zamku, ktÄ‚Ĺ‚rego jeszcze nie narysowano",
        "validates": "lampka kontrolna mrugnĂ„â„˘Äąâ€ša, chociaÄąÄ˝ nikt jej nie pytaÄąâ€š o zgodĂ„â„˘",
        "risk_for": "czerwony sznurek zawiĂ„â€¦zaÄąâ€š supeÄąâ€š na kieszeni z sekretami",
        "metric_for": "liczby przeszÄąâ€šy przez lustro i wrÄ‚Ĺ‚ciÄąâ€šy jako drobny deszcz",
        "next_step_for": "schodek wyrÄ‚Ĺ‚sÄąâ€š pod stopĂ„â€¦ dopiero po zrobieniu kroku",
        "depends_on": "jedna szuflada Äąâ€şniÄąâ€ša zawias drugiej",
    }
    seen_relations: list[str] = []
    for item in source_items:
        relation = str(item.get("relation_type") or "related_to")
        if relation not in seen_relations:
            seen_relations.append(relation)
    fragments = [relation_images.get(relation, "niĂ„â€ˇ przeszÄąâ€ša przez miejsce, gdzie brakowaÄąâ€šo nazwy") for relation in seen_relations[:5]]
    project_part = f" nad stoÄąâ€šem {project_key}" if project_key else ""
    return (
        f"Sandman Äąâ€şniÄąâ€š{project_part}. "
        + " ".join(fragment.capitalize() + "." for fragment in fragments)
        + " Rano zostaÄąâ€šy po tym tylko drobne wÄąâ€šÄ‚Ĺ‚kna miĂ„â„˘dzy kartkami i wraÄąÄ˝enie, ÄąÄ˝e biblioteka przez chwilĂ„â„˘ oddychaÄąâ€ša odwrotnie."
    )

def _sandman_quality_owner_target(memory: dict[str, Any]) -> tuple[str, str]:
    """Deterministic owner routing for Sandman quality hygiene repairs."""
    existing_role = normalize_optional_text(memory.get("owner_role"))
    project_key = normalize_optional_text(memory.get("project_key"))
    if existing_role == "review_team":
        return "review_team", "global_review_ops"
    if existing_role == "project_maintainer":
        return "project_maintainer", "global_project_ops"
    if existing_role == "maintainer":
        return "maintainer", "global_memory_ops"
    if project_key is not None:
        return "project_maintainer", "global_project_ops"
    return existing_role or "maintainer", "global_memory_ops"


def _sandman_quality_owner_gap_rows(conn, *, project_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    filters = [
        "archived_at IS NULL",
        "(owner_role IS NULL OR trim(owner_role) = '' OR owner_id IS NULL OR trim(owner_id) = '')",
    ]
    params: list[Any] = []
    normalized_project_key = normalize_optional_text(project_key)
    if normalized_project_key is not None:
        filters.append("LOWER(project_key) = LOWER(?)")
        params.append(normalized_project_key)
    rows = conn.execute(
        f"""
        SELECT id, summary_short, memory_type, layer_code, area_code, scope_code,
               project_key, owner_role, owner_id, tags, created_at
        FROM memories
        WHERE {' AND '.join(filters)}
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, max(1, min(int(limit or 100), 1000))),
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def _sandman_quality_scope_mismatch_rows(conn, *, project_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return _project_scope_mismatch_rows(conn, project_key=project_key, limit=limit)


@mcp.tool
def preview_sandman_quality_hygiene(project_key: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Preview deterministic Sandman quality hygiene repairs: owner gaps and project scope mismatches."""
    conn = get_db_connection()
    try:
        owner_gap_items = _sandman_quality_owner_gap_rows(conn, project_key=project_key, limit=limit)
        scope_items = _sandman_quality_scope_mismatch_rows(conn, project_key=project_key, limit=limit)
        owner_actions = []
        for item in owner_gap_items:
            owner_role, owner_id = _sandman_quality_owner_target(item)
            owner_actions.append({
                "memory_id": int(item["id"]),
                "action": "set_owner",
                "owner_role": owner_role,
                "owner_id": owner_id,
                "reason": "missing owner_role or owner_id",
                "summary_short": item.get("summary_short"),
                "project_key": item.get("project_key"),
            })
        scope_actions = []
        for item in scope_items:
            scope_actions.append({
                "memory_id": int(item["id"]),
                "action": "normalize_project_scope",
                "scope_code": "project",
                "layer_code_if_missing_or_buffer": "projects",
                "area_code_if_missing": "projects",
                "reason": "project_key memory has global, empty, or default-null scope without allow-global-project-scope tag",
                "summary_short": item.get("summary_short"),
                "project_key": item.get("project_key"),
            })
        return {
            "status": "preview_completed",
            "project_key": normalize_optional_text(project_key),
            "owner_gap_count": len(owner_actions),
            "project_scope_mismatch_count": len(scope_actions),
            "total_action_count": len(owner_actions) + len(scope_actions),
            "owner_actions": owner_actions,
            "scope_actions": scope_actions,
        }
    finally:
        conn.close()


@mcp.tool
def run_sandman_quality_hygiene(project_key: str | None = None, limit: int = 100, notes: str | None = None) -> dict[str, Any]:
    """Run deterministic Sandman quality hygiene repairs with audit events."""
    conn = get_db_connection()
    try:
        owner_gap_items = _sandman_quality_owner_gap_rows(conn, project_key=project_key, limit=limit)
        scope_items = _sandman_quality_scope_mismatch_rows(conn, project_key=project_key, limit=limit)
        now = utc_now_iso()
        owner_updates = []
        scope_updates = []
        for item in owner_gap_items:
            memory_id = int(item["id"])
            owner_role, owner_id = _sandman_quality_owner_target(item)
            conn.execute(
                """
                UPDATE memories
                SET owner_role = ?, owner_id = ?, last_accessed_at = ?
                WHERE id = ? AND archived_at IS NULL
                """,
                (owner_role, owner_id, now, memory_id),
            )
            payload = {
                "owner_role": owner_role,
                "owner_id": owner_id,
                "reason": "Sandman quality hygiene: missing owner_role or owner_id",
                "notes": normalize_optional_text(notes),
            }
            try:
                timeline.record_timeline_event(
                    conn,
                    event_type="sandman.quality.owner_repaired",
                    memory_id=memory_id,
                    origin="sandman_quality_hygiene",
                    payload=payload,
                )
            except Exception:
                pass
            owner_updates.append({"memory_id": memory_id, **payload})

        for item in scope_items:
            memory_id = int(item["id"])
            from_scope = normalize_optional_text(item.get("scope_code")) or "<default-null>"
            conn.execute(
                """
                UPDATE memories
                SET scope_code = 'project',
                    layer_code = CASE WHEN layer_code IS NULL OR trim(layer_code) = '' OR layer_code = 'buffer' THEN 'projects' ELSE layer_code END,
                    area_code = CASE WHEN area_code IS NULL OR trim(area_code) = '' THEN 'projects' ELSE area_code END,
                    validation_source = COALESCE(validation_source, 'sandman_quality_hygiene'),
                    last_accessed_at = ?
                WHERE id = ? AND archived_at IS NULL
                """,
                (now, memory_id),
            )
            payload = {
                "from_scope_code": from_scope,
                "to_scope_code": "project",
                "reason": "Sandman quality hygiene: project_key memory must not use global/default scope by default",
                "notes": normalize_optional_text(notes),
            }
            try:
                timeline.record_timeline_event(
                    conn,
                    event_type="sandman.quality.scope_repaired",
                    memory_id=memory_id,
                    origin="sandman_quality_hygiene",
                    payload=payload,
                )
            except Exception:
                pass
            scope_updates.append({"memory_id": memory_id, **payload})

        conn.commit()
        return {
            "status": "completed",
            "project_key": normalize_optional_text(project_key),
            "owner_repaired_count": len(owner_updates),
            "scope_repaired_count": len(scope_updates),
            "total_repaired_count": len(owner_updates) + len(scope_updates),
            "owner_updates": owner_updates,
            "scope_updates": scope_updates,
        }
    finally:
        conn.close()


def _sandman_gemma_owner_gap_candidates(conn, *, project_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    filters = [
        "archived_at IS NULL",
        "(owner_role IS NULL OR trim(owner_role) = '' OR owner_id IS NULL OR trim(owner_id) = '')",
    ]
    params: list[Any] = []
    normalized_project_key = normalize_optional_text(project_key)
    if normalized_project_key is not None:
        filters.append("LOWER(project_key) = LOWER(?)")
        params.append(normalized_project_key)
    rows = conn.execute(
        f"""
        SELECT id, summary_short, memory_type, layer_code, area_code, scope_code,
               project_key, owner_role, owner_id, tags, source, archived_at, created_at
        FROM memories
        WHERE {' AND '.join(filters)}
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, max(1, min(int(limit or 100), 1000))),
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def _sandman_gemma_scope_mismatch_candidates(conn, *, project_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return _project_scope_mismatch_rows(conn, project_key=project_key, limit=limit)


def _sandman_gemma_noise_candidates(conn, *, project_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    filters = [
        "archived_at IS NULL",
        "(LOWER(COALESCE(tags, '')) LIKE '%test%' OR LOWER(COALESCE(tags, '')) LIKE '%smoke%' OR LOWER(COALESCE(tags, '')) LIKE '%probe%' OR LOWER(COALESCE(summary_short, '')) LIKE '%test%' OR LOWER(COALESCE(summary_short, '')) LIKE '%smoke%' OR LOWER(COALESCE(summary_short, '')) LIKE '%probe%' OR LOWER(COALESCE(source, '')) LIKE '%runtime_smoke%' OR LOWER(COALESCE(source, '')) LIKE '%probe%')",
    ]
    params: list[Any] = []
    normalized_project_key = normalize_optional_text(project_key)
    if normalized_project_key is not None:
        filters.append("LOWER(project_key) = LOWER(?)")
        params.append(normalized_project_key)
    rows = conn.execute(
        f"""
        SELECT id, summary_short, memory_type, layer_code, area_code, scope_code,
               project_key, owner_role, owner_id, tags, source, archived_at, created_at
        FROM memories
        WHERE {' AND '.join(filters)}
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, max(1, min(int(limit or 100), 1000))),
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def _sandman_gemma_collect_candidates(conn, *, project_key: str | None = None, issue_kinds: list[str] | None = None, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
    requested = set(issue_kinds or ["owner_gap", "project_scope_mismatch", "test_probe_noise"])
    candidates: dict[str, list[dict[str, Any]]] = {}
    if "owner_gap" in requested:
        candidates["owner_gap"] = _sandman_gemma_owner_gap_candidates(conn, project_key=project_key, limit=limit)
    if "project_scope_mismatch" in requested:
        candidates["project_scope_mismatch"] = _sandman_gemma_scope_mismatch_candidates(conn, project_key=project_key, limit=limit)
    if "test_probe_noise" in requested:
        candidates["test_probe_noise"] = _sandman_gemma_noise_candidates(conn, project_key=project_key, limit=limit)
    return candidates


def _parse_issue_kinds(issue_kinds_json: str | None) -> list[str] | None:
    normalized = normalize_optional_text(issue_kinds_json)
    if normalized is None:
        return None
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        parsed = [part.strip() for part in normalized.split(',') if part.strip()]
    if not isinstance(parsed, list):
        raise ValueError("issue_kinds_json must be a JSON list or comma-separated string")
    return [str(item) for item in parsed if str(item).strip()]


def _current_memory_map(conn, memory_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not memory_ids:
        return {}
    placeholders = ','.join('?' for _ in memory_ids)
    rows = conn.execute(f"SELECT * FROM memories WHERE id IN ({placeholders})", tuple(memory_ids)).fetchall()
    return {int(row['id']): row_to_dict(row) for row in rows}


def _sandman_gemma_preview_client(model_client: str | None, fake_mode: str | None):
    normalized_client = normalize_optional_text(model_client) or "fake"
    if normalized_client in {"fake", "test", "stub"}:
        return normalized_client, sandman_gemma_hygiene.FakeGemmaClient(fake_mode or "valid_low_risk_json")
    if normalized_client in {"local", "local_gemma", "lm_studio", "gemma"}:
        return "local_gemma", sandman_gemma_client.LocalGemmaClient()
    if normalized_client in {"managed_lms", "lms", "lms_managed"}:
        return "managed_lms", sandman_gemma_client.ManagedLmsGemmaClient()
    raise ValueError("model_client must be one of: fake, local_gemma, lm_studio, managed_lms")


@mcp.tool
def check_sandman_gemma_lms_status() -> dict[str, Any]:
    """Report LM Studio/LMS residency diagnostics for Sandman Gemma without loading or unloading models."""
    return sandman_gemma_client.check_lms_status()


@mcp.tool
def check_gemma_runtime() -> dict[str, Any]:
    """Pelna diagnostyka Gemma runtime: serwer, model, test chat completion.

    Sprawdza:
    - czy LM Studio server odpowiada na /v1/models
    - czy skonfigurowany model jest zaladowany
    - czy /chat/completions odpowiada prawidlowo

    Nie laduje ani nie rozladowuje modeli. Tylko odczyt.
    """
    result = sandman_gemma_runtime.ensure_gemma_ready(fail_closed=False)
    result["runtime_config"] = sandman_gemma_runtime.gemma_runtime_info()
    return result


def _sandman_gemma_feature_flag_status(conn, *, project_key: str | None = None) -> dict[str, Any]:
    flag = _get_feature_flag_config(conn, SANDMAN_GEMMA_HYGIENE_FLAG_KEY)
    evaluation = _evaluate_feature_flag_config(flag, project_key=project_key, scope_code="project")
    flag_view = dict(flag)
    flag_view["key"] = flag_view.get("flag_key")
    flag_view["enabled"] = bool(int(flag_view.get("is_enabled") or 0))
    return {"feature_flag": flag_view, "evaluation": evaluation}


def get_sandman_canonical_status(
    project_key: str = "demo-project",
    include_debug: bool = False,
) -> dict[str, Any]:
    """Return canonical scheduler/provider freshness and last-run status."""
    return sandman_canonical_runtime.get_canonical_status(
        root_path=runtime_root(),
        project_key=normalize_required_text(project_key, "project_key"),
        include_debug=include_debug,
    )


def preview_sandman_canonical(
    project_key: str = "demo-project",
    scope_code: str = "project",
    candidate_limit: int = 12,
    proposal_budget: int = 3,
    memory_ids_json: str | None = None,
    allowed_actions_json: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Build the canonical deterministic + Gemini-shadow preview without writes."""
    memory_ids = None
    if normalize_optional_text(memory_ids_json):
        parsed_ids = json.loads(str(memory_ids_json))
        if not isinstance(parsed_ids, list):
            raise ValueError("memory_ids_json must be a JSON array")
        memory_ids = [int(item) for item in parsed_ids]
    allowed_actions = None
    if normalize_optional_text(allowed_actions_json):
        parsed_actions = json.loads(str(allowed_actions_json))
        if not isinstance(parsed_actions, list) or any(not isinstance(item, str) for item in parsed_actions):
            raise ValueError("allowed_actions_json must be a JSON string array")
        allowed_actions = parsed_actions
    return sandman_canonical_runtime.preview_canonical(
        root_path=runtime_root(),
        project_key=normalize_required_text(project_key, "project_key"),
        scope_code=normalize_scope_code(scope_code) or "project",
        candidate_limit=int(candidate_limit),
        proposal_budget=int(proposal_budget),
        memory_ids=memory_ids,
        allowed_actions=allowed_actions,
        include_debug=include_debug,
    )


def list_sandman_canonical_runs(
    run_type: str | None = None,
    status: str | None = None,
    project_key: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List the single canonical scheduler ledger."""
    return sandman_canonical_runtime.list_canonical_runs(
        root_path=runtime_root(),
        run_type=normalize_optional_text(run_type),
        status=normalize_optional_text(status),
        project_key=normalize_optional_text(project_key),
        limit=int(limit),
    )


def get_sandman_canonical_run(run_id: int) -> dict[str, Any]:
    """Return one canonical scheduler run."""
    return sandman_canonical_runtime.get_canonical_run(
        int(run_id),
        root_path=runtime_root(),
    )


def get_sandman_provider_v3_status(
    project_key: str | None = None,
    scope_code: str | None = "project",
) -> dict[str, Any]:
    """Return the read-only Sandman v3 registry and flag state without health checks."""
    conn = get_db_connection()
    try:
        normalized_project = normalize_optional_text(project_key)
        normalized_scope = normalize_scope_code(scope_code)
        flag_status = _sandman_provider_v3_feature_status(
            conn,
            project_key=normalized_project,
            scope_code=normalized_scope,
        )
        shadow_flag_status = _sandman_gemini_shadow_feature_status(
            conn,
            project_key=normalized_project,
            scope_code=normalized_scope,
        )
        routing_flag_status = _sandman_model_queue_routing_feature_status(
            conn,
            project_key=normalized_project,
            scope_code=normalized_scope,
        )
        canary = sandman_v3_routing.canary_state(
            conn, project_key=normalized_project or "mapi"
        )
        config = GeminiConfig.from_env()
        return sandman_v3_router.provider_status_payload(
            feature_flag=flag_status["feature_flag"],
            feature_flag_evaluation=flag_status["evaluation"],
            gemini_shadow_flag_evaluation=shadow_flag_status["evaluation"],
            routing_flag=routing_flag_status["feature_flag"],
            routing_flag_evaluation=routing_flag_status["evaluation"],
            routing_canary=canary,
            gemini_config={
                "api_key_configured": config.api_key_configured,
                "primary_model": config.primary_model,
                "escalation_model": config.escalation_model,
                "escalation_enabled": config.escalation_enabled,
            },
        )
    finally:
        conn.close()


def preview_sandman_provider_request(
    project_key: str,
    scope_code: str,
    memory_ids_json: str,
    allowed_actions_json: str,
    provider_name: str = "deterministic",
    proposal_budget: int = 8,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Build a redacted proposal-only provider request from explicit local IDs."""
    normalized_project = normalize_required_text(project_key, "project_key")
    normalized_scope = normalize_scope_code(scope_code)
    conn = get_db_connection()
    try:
        flag_status = _sandman_provider_v3_feature_status(
            conn,
            project_key=normalized_project,
            scope_code=normalized_scope,
        )
        return sandman_v3_router.preview_provider_request_payload(
            conn,
            project_key=normalized_project,
            scope_code=normalized_scope or "",
            memory_ids_json=memory_ids_json,
            allowed_actions_json=allowed_actions_json,
            provider_name=provider_name,
            proposal_budget=proposal_budget,
            include_debug=include_debug,
            feature_flag=flag_status["feature_flag"],
            feature_flag_evaluation=flag_status["evaluation"],
        )
    finally:
        conn.close()


def preview_sandman_provider_deterministic(
    project_key: str,
    scope_code: str,
    memory_ids_json: str,
    allowed_actions_json: str,
    provider_name: str = "deterministic",
    proposal_budget: int = 8,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Run the local deterministic provider and whole-response validator, read-only."""
    normalized_project = normalize_required_text(project_key, "project_key")
    normalized_scope = normalize_scope_code(scope_code)
    conn = get_db_connection()
    try:
        flag_status = _sandman_provider_v3_feature_status(
            conn,
            project_key=normalized_project,
            scope_code=normalized_scope,
        )
        return sandman_v3_router.preview_deterministic_provider_payload(
            conn,
            project_key=normalized_project,
            scope_code=normalized_scope or "",
            memory_ids_json=memory_ids_json,
            allowed_actions_json=allowed_actions_json,
            provider_name=provider_name,
            proposal_budget=proposal_budget,
            include_debug=include_debug,
            feature_flag=flag_status["feature_flag"],
            feature_flag_evaluation=flag_status["evaluation"],
        )
    finally:
        conn.close()


def get_sandman_semantic_evaluation_corpus(
    include_case_ids: bool = False,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Return the synthetic semantic evaluation corpus manifest without case content."""
    return sandman_v3_evaluation.semantic_evaluation_corpus_manifest(
        include_case_ids=include_case_ids,
        include_debug=include_debug,
    )


def evaluate_sandman_semantic_provider(
    evaluation_kind: str,
    prediction_bundle_json: str | None = None,
    include_case_results: bool = False,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Evaluate a local baseline or supplied synthetic replay without persistence."""
    return sandman_v3_evaluation.evaluate_semantic_provider_bundle(
        evaluation_kind=normalize_required_text(evaluation_kind, "evaluation_kind"),
        prediction_bundle_json=prediction_bundle_json,
        include_case_results=include_case_results,
        include_debug=include_debug,
    )


def _stable_shadow_request_id(
    *,
    project_key: str,
    scope_code: str,
    memory_ids_json: str,
    allowed_actions_json: str,
    proposal_budget: int,
) -> str:
    try:
        memory_ids = sorted(set(int(item) for item in json.loads(memory_ids_json)))
        allowed_actions = sorted(set(str(item) for item in json.loads(allowed_actions_json)))
    except (TypeError, ValueError, json.JSONDecodeError):
        memory_ids = memory_ids_json
        allowed_actions = allowed_actions_json
    fingerprint = canonical_fingerprint(
        {
            "project_key": project_key,
            "scope_code": scope_code,
            "memory_ids": memory_ids,
            "allowed_actions": allowed_actions,
            "proposal_budget": proposal_budget,
        }
    )
    return f"shadow-{fingerprint.removeprefix('sha256:')}"


def _stable_route_request_id(
    *,
    project_key: str,
    scope_code: str,
    memory_ids_json: str,
    allowed_actions_json: str,
    proposal_budget: int,
    model_role: str,
) -> str:
    try:
        memory_ids = sorted(set(int(item) for item in json.loads(memory_ids_json)))
        allowed_actions = sorted(
            set(str(item) for item in json.loads(allowed_actions_json))
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        memory_ids = memory_ids_json
        allowed_actions = allowed_actions_json
    fingerprint = canonical_fingerprint(
        {
            "schema_version": sandman_v3_routing.ROUTE_PREVIEW_SCHEMA_VERSION,
            "project_key": project_key,
            "scope_code": scope_code,
            "memory_ids": memory_ids,
            "allowed_actions": allowed_actions,
            "proposal_budget": int(proposal_budget),
            "model_role": model_role,
        }
    )
    return f"route-{fingerprint.removeprefix('sha256:')}"


def _preview_sandman_model_queue_route_internal(
    *,
    stage: str,
    project_key: str,
    scope_code: str,
    memory_ids_json: str,
    allowed_actions_json: str,
    proposal_budget: int,
    model_role: str,
    operator_prediction_bundle_json: str,
    evaluation_report_json: str | None,
    include_debug: bool,
    conn=None,
) -> dict[str, Any]:
    normalized_project = normalize_required_text(project_key, "project_key")
    normalized_scope = normalize_scope_code(scope_code) or ""
    owns_connection = conn is None
    conn = conn or get_db_connection()
    try:
        provider_flag = _sandman_provider_v3_feature_status(
            conn, project_key=normalized_project, scope_code=normalized_scope
        )
        shadow_flag = _sandman_gemini_shadow_feature_status(
            conn, project_key=normalized_project, scope_code=normalized_scope
        )
        routing_flag = _sandman_model_queue_routing_feature_status(
            conn, project_key=normalized_project, scope_code=normalized_scope
        )
        return sandman_v3_routing.preview_model_queue_route(
            conn,
            stage=stage,
            project_key=normalized_project,
            scope_code=normalized_scope,
            memory_ids_json=memory_ids_json,
            allowed_actions_json=allowed_actions_json,
            proposal_budget=int(proposal_budget),
            model_role=model_role,
            operator_prediction_bundle_json=operator_prediction_bundle_json,
            evaluation_report_json=evaluation_report_json,
            include_debug=include_debug,
            request_builder=sandman_v3_router.preview_provider_request_payload,
            provider_flag=provider_flag["feature_flag"],
            provider_evaluation=provider_flag["evaluation"],
            shadow_flag=shadow_flag["feature_flag"],
            shadow_evaluation=shadow_flag["evaluation"],
            routing_flag=routing_flag["feature_flag"],
            routing_evaluation=routing_flag["evaluation"],
            config=GeminiConfig.from_env(),
            request_id_factory=lambda: _stable_route_request_id(
                project_key=normalized_project,
                scope_code=normalized_scope,
                memory_ids_json=memory_ids_json,
                allowed_actions_json=allowed_actions_json,
                proposal_budget=int(proposal_budget),
                model_role=model_role,
            ),
        )
    finally:
        if owns_connection:
            conn.close()


def preview_sandman_model_queue_route(
    stage: str = "existing_memory",
    project_key: str = "mapi",
    scope_code: str = "project",
    memory_ids_json: str = "[]",
    allowed_actions_json: str = "[]",
    proposal_budget: int = 3,
    model_role: str = "primary",
    operator_prediction_bundle_json: str = "",
    evaluation_report_json: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Preview guarded model proposal routing without network or queue writes."""
    return sandman_v3_routing.public_preview(
        _preview_sandman_model_queue_route_internal(
            stage=stage,
            project_key=project_key,
            scope_code=scope_code,
            memory_ids_json=memory_ids_json,
            allowed_actions_json=allowed_actions_json,
            proposal_budget=proposal_budget,
            model_role=model_role,
            operator_prediction_bundle_json=operator_prediction_bundle_json,
            evaluation_report_json=evaluation_report_json,
            include_debug=include_debug,
        )
    )


def run_sandman_model_queue_canary(
    stage: str = "existing_memory",
    project_key: str = "mapi",
    scope_code: str = "project",
    memory_ids_json: str = "[]",
    allowed_actions_json: str = "[]",
    proposal_budget: int = 3,
    model_role: str = "primary",
    operator_prediction_bundle_json: str = "",
    evaluation_report_json: str | None = None,
    expected_route_preview_hash: str = "",
    requested_by: str = "",
    route_reason: str = "",
    confirm_queue_write: bool = False,
    include_debug: bool = False,
    notes: str | None = None,
) -> dict[str, Any]:
    """Run a guarded proposal-only canary into the existing consolidation review queue."""
    preview = _preview_sandman_model_queue_route_internal(
        stage=stage,
        project_key=project_key,
        scope_code=scope_code,
        memory_ids_json=memory_ids_json,
        allowed_actions_json=allowed_actions_json,
        proposal_budget=proposal_budget,
        model_role=model_role,
        operator_prediction_bundle_json=operator_prediction_bundle_json,
        evaluation_report_json=evaluation_report_json,
        include_debug=include_debug,
    )
    if preview.get("status") != "preview_ready":
        if expected_route_preview_hash:
            conn = get_db_connection()
            try:
                existing = sandman_v3_routing.existing_route_result_for_preview(
                    conn, preview=preview
                )
            finally:
                conn.close()
            if existing is not None:
                return existing
        return sandman_v3_routing.public_preview(preview)
    config = GeminiConfig.from_env()
    return sandman_v3_routing.run_model_queue_canary(
        connection_factory=get_db_connection,
        preview=preview,
        rebuild_preview=lambda conn: _preview_sandman_model_queue_route_internal(
            stage=stage,
            project_key=project_key,
            scope_code=scope_code,
            memory_ids_json=memory_ids_json,
            allowed_actions_json=allowed_actions_json,
            proposal_budget=proposal_budget,
            model_role=model_role,
            operator_prediction_bundle_json=operator_prediction_bundle_json,
            evaluation_report_json=evaluation_report_json,
            include_debug=include_debug,
            conn=conn,
        ),
        provider=_build_gemini_shadow_provider(config),
        expected_route_preview_hash=expected_route_preview_hash,
        requested_by=requested_by,
        route_reason=route_reason,
        confirm_queue_write=confirm_queue_write,
        insert_memory=_insert_memory,
        create_link=_create_link,
        utc_now_iso=utc_now_iso,
        notes=notes,
    )


def get_sandman_provider_observability(
    project_key: str | None = "mapi",
    limit: int = 50,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Return content-free provider and model queue observability."""
    normalized_project = normalize_optional_text(project_key) or "mapi"
    conn = get_db_connection()
    try:
        provider_flag = _sandman_provider_v3_feature_status(
            conn, project_key=normalized_project, scope_code="project"
        )
        shadow_flag = _sandman_gemini_shadow_feature_status(
            conn, project_key=normalized_project, scope_code="project"
        )
        routing_flag = _sandman_model_queue_routing_feature_status(
            conn, project_key=normalized_project, scope_code="project"
        )
        flag_evaluations = sandman_v3_routing.build_flag_evaluations(
            provider_flag=provider_flag["feature_flag"],
            provider_evaluation=provider_flag["evaluation"],
            shadow_flag=shadow_flag["feature_flag"],
            shadow_evaluation=shadow_flag["evaluation"],
            routing_flag=routing_flag["feature_flag"],
            routing_evaluation=routing_flag["evaluation"],
        )
        return sandman_v3_observability.provider_observability_payload(
            conn,
            project_key=normalized_project,
            limit=int(limit),
            include_debug=include_debug,
            flag_evaluations=flag_evaluations,
        )
    finally:
        conn.close()


def _preview_sandman_gemini_shadow_internal(
    *,
    project_key: str,
    scope_code: str,
    memory_ids_json: str,
    allowed_actions_json: str,
    proposal_budget: int,
    model_role: str,
    include_debug: bool,
) -> dict[str, Any]:
    normalized_project = normalize_required_text(project_key, "project_key")
    normalized_scope = normalize_scope_code(scope_code) or ""
    config = GeminiConfig.from_env()
    conn = get_db_connection()
    try:
        provider_flag = _sandman_provider_v3_feature_status(
            conn, project_key=normalized_project, scope_code=normalized_scope
        )
        shadow_flag = _sandman_gemini_shadow_feature_status(
            conn, project_key=normalized_project, scope_code=normalized_scope
        )
        request_preview = sandman_v3_router.preview_provider_request_payload(
            conn,
            project_key=normalized_project,
            scope_code=normalized_scope,
            memory_ids_json=memory_ids_json,
            allowed_actions_json=allowed_actions_json,
            provider_name="gemini",
            proposal_budget=proposal_budget,
            include_debug=include_debug,
            feature_flag=provider_flag["feature_flag"],
            feature_flag_evaluation=provider_flag["evaluation"],
            request_id_factory=lambda: _stable_shadow_request_id(
                project_key=normalized_project,
                scope_code=normalized_scope,
                memory_ids_json=memory_ids_json,
                allowed_actions_json=allowed_actions_json,
                proposal_budget=proposal_budget,
            ),
        )
        return sandman_gemini_shadow.preview_shadow(
            request_preview=request_preview,
            provider_evaluation=provider_flag["evaluation"],
            shadow_evaluation=shadow_flag["evaluation"],
            config=config,
            model_role=model_role,
            include_debug=include_debug,
        )
    finally:
        conn.close()


def preview_sandman_gemini_shadow(
    project_key: str,
    scope_code: str,
    memory_ids_json: str,
    allowed_actions_json: str,
    proposal_budget: int = 8,
    model_role: str = "primary",
    include_debug: bool = False,
) -> dict[str, Any]:
    """Preview a stateless Gemini shadow call without network or ledger writes."""
    return sandman_gemini_shadow.public_preview(
        _preview_sandman_gemini_shadow_internal(
            project_key=project_key,
            scope_code=scope_code,
            memory_ids_json=memory_ids_json,
            allowed_actions_json=allowed_actions_json,
            proposal_budget=proposal_budget,
            model_role=model_role,
            include_debug=include_debug,
        )
    )


def _build_gemini_shadow_provider(config: GeminiConfig) -> GeminiShadowProvider:
    return GeminiShadowProvider(
        config=config,
        circuit_breaker=get_shared_model_circuit_breaker(config),
        transport=GoogleGenAIInteractionsTransport(
            api_key=__import__("os").environ.get("GEMINI_API_KEY", "").strip(),
            timeout_seconds=config.timeout_seconds,
        ),
    )


def run_sandman_gemini_shadow(
    project_key: str,
    scope_code: str,
    memory_ids_json: str,
    allowed_actions_json: str,
    requested_by: str,
    proposal_budget: int = 8,
    model_role: str = "primary",
    include_debug: bool = False,
    notes: str | None = None,
) -> dict[str, Any]:
    """Run one audited, stateless Gemini shadow analysis with no memory or queue writes."""
    preview = _preview_sandman_gemini_shadow_internal(
        project_key=project_key,
        scope_code=scope_code,
        memory_ids_json=memory_ids_json,
        allowed_actions_json=allowed_actions_json,
        proposal_budget=proposal_budget,
        model_role=model_role,
        include_debug=include_debug,
    )
    if preview.get("status") != "preview_ready":
        return sandman_gemini_shadow.public_preview(preview)
    config = GeminiConfig.from_env()
    return sandman_gemini_shadow.run_shadow(
        connection_factory=get_db_connection,
        preview=preview,
        provider=_build_gemini_shadow_provider(config),
        requested_by=requested_by,
        notes=notes,
    )


def list_sandman_gemini_shadow_runs(
    status: str | None = None,
    project_key: str | None = None,
    model_name: str | None = None,
    validation_status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List safe Gemini shadow audit metadata without prompts or responses."""
    conn = get_db_connection()
    try:
        items = sandman_shadow_repository.list_runs(
            conn,
            status=normalize_optional_text(status),
            project_key=normalize_optional_text(project_key),
            model_name=normalize_optional_text(model_name),
            validation_status=normalize_optional_text(validation_status),
            limit=int(limit),
        )
        return {"status": "ok", "items": items, "count": len(items)}
    finally:
        conn.close()


def get_sandman_gemini_shadow_run(run_id: int) -> dict[str, Any]:
    """Get one safe Gemini shadow audit record."""
    conn = get_db_connection()
    try:
        return {"status": "ok", "run": sandman_shadow_repository.get_run(conn, int(run_id))}
    finally:
        conn.close()


def _require_sandman_gemma_preview_access(conn, *, project_key: str | None = None) -> dict[str, Any]:
    status = _sandman_gemma_feature_flag_status(conn, project_key=project_key)
    evaluation = status["evaluation"]
    if not evaluation["enabled"]:
        raise ValueError(f"Feature flag {SANDMAN_GEMMA_HYGIENE_FLAG_KEY} blokuje preview Sandman Gemma: {evaluation['reason']}")
    return status


def _require_sandman_gemma_run_access(conn, *, project_key: str | None = None) -> dict[str, Any]:
    status = _require_sandman_gemma_preview_access(conn, project_key=project_key)
    if status["evaluation"].get("read_only_mode"):
        raise ValueError(f"Feature flag {SANDMAN_GEMMA_HYGIENE_FLAG_KEY} jest w trybie read-only/shadow. Run auto-apply zablokowany")
    return status


def _record_sandman_gemma_decision_actions(conn, *, run_id: int, validation: dict[str, list[dict[str, Any]]]) -> None:
    for bucket, action_type in (
        ("accepted_auto_apply", "gemma_decision_auto_apply_candidate"),
        ("needs_human_review", "gemma_decision_needs_human_review"),
        ("rejected_by_validator", "gemma_decision_rejected"),
    ):
        for decision in validation.get(bucket, []):
            add_sleep_action(conn, run_id, action_type, int(decision.get("memory_id")) if decision.get("memory_id") is not None else None, None, decision, str(decision.get("reason_code") or bucket))


def _record_sandman_gemma_timeline_event(conn, *, event_type: str, run_id: int, project_key: str | None, title: str, payload: dict[str, Any], memory_id: int | None = None) -> int:
    return timeline.record_timeline_event(
        conn,
        event_type=event_type,
        memory_id=memory_id,
        run_id=run_id,
        operation_id=timeline.run_operation_id(run_id),
        timeline_scope="run" if memory_id is None else "memory",
        semantic_kind="runtime_event",
        title=title,
        project_key=normalize_optional_text(project_key),
        source_table="sleep_runs",
        source_row_id=run_id,
        origin="sandman_agent_auto",
        payload=payload,
    )


@mcp.tool
def preview_sandman_gemma_candidates(project_key: str | None = None, issue_kinds_json: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Collect candidate memories for Gemma-based Sandman review. Does not call a model and does not write."""
    issue_kinds = _parse_issue_kinds(issue_kinds_json)
    conn = get_db_connection()
    try:
        candidates = _sandman_gemma_collect_candidates(conn, project_key=project_key, issue_kinds=issue_kinds, limit=limit)
        return {
            "status": "ok",
            "project_key": normalize_optional_text(project_key),
            "issue_kinds": sorted(candidates.keys()),
            "total_candidate_count": sum(len(items) for items in candidates.values()),
            "candidates": candidates,
        }
    finally:
        conn.close()


@mcp.tool
def preview_sandman_gemma_hygiene(project_key: str | None = None, issue_kinds_json: str | None = None, limit: int = 100, fake_mode: str | None = None, model_client: str | None = "lms", debug: bool = False) -> dict[str, Any]:
    """Preview Gemma-style Sandman hygiene review through parser and host validator. No memory writes, but records an auditable preview run."""
    issue_kinds = _parse_issue_kinds(issue_kinds_json) or [
        "owner_gap",
        "project_scope_mismatch",
        "test_probe_noise",
    ]
    # Preflight: upewnij sie ze Gemma dziala zanim zaczniemy run
    _resolved_client = (normalize_optional_text(model_client) or "lms")
    if _resolved_client not in {"fake", "test", "stub"}:
        sandman_gemma_runtime.ensure_gemma_ready()
    conn = get_db_connection()
    try:
        flag_status = _require_sandman_gemma_preview_access(conn, project_key=project_key)
        run_id = create_sleep_run(conn, mode="sandman_gemma_preview", freedom_level=0, notes="Gemma hygiene preview", project_key=project_key)
        _record_sandman_gemma_timeline_event(
            conn,
            event_type="sandman_agent.gemma_preview_started",
            run_id=run_id,
            project_key=project_key,
            title="Sandman Gemma preview started",
            payload={"issue_kinds": sorted(issue_kinds), "limit": int(limit), "model_client": model_client},
        )
        candidates = _sandman_gemma_collect_candidates(conn, project_key=project_key, issue_kinds=issue_kinds, limit=limit)
        prompt = sandman_gemma_hygiene.build_sandman_gemma_hygiene_prompt(candidates, project_key=project_key)
        resolved_model_client, client = _sandman_gemma_preview_client(model_client, fake_mode)
        raw_response = client.complete_json(prompt, timeout_seconds=sandman_gemma_client.SANDMAN_GEMMA_TIMEOUT_SECONDS)
        parsed = sandman_gemma_hygiene.parse_gemma_decisions(raw_response)
        memory_ids = sorted({int(decision["memory_id"]) for decision in parsed.decisions})
        current_memories = _current_memory_map(conn, memory_ids)
        validation = sandman_gemma_hygiene.validate_sandman_decisions(parsed.decisions, current_memories)
        shadow_comparison = sandman_gemma_hygiene.build_shadow_comparison(candidates, validation, issue_kinds=issue_kinds)
        _record_sandman_gemma_decision_actions(conn, run_id=run_id, validation=validation)
        candidate_count = sum(len(items) for items in candidates.values())
        _record_sandman_gemma_timeline_event(
            conn,
            event_type="sandman_agent.gemma_preview_completed",
            run_id=run_id,
            project_key=project_key,
            title="Sandman Gemma preview completed",
            payload={
                "candidate_count": candidate_count,
                "parse_status": parsed.status,
                "parse_error_count": len(parsed.errors),
                "accepted_auto_apply_count": len(validation["accepted_auto_apply"]),
                "needs_human_review_count": len(validation["needs_human_review"]),
                "rejected_by_validator_count": len(validation["rejected_by_validator"]),
                "shadow_comparison": shadow_comparison["totals"],
            },
        )
        finalize_sleep_run(
            conn,
            run_id,
            status="completed" if parsed.status in {"ok", "partial"} else "failed",
            scanned_count=candidate_count,
            changed_count=0,
            archived_count=0,
            downgraded_count=0,
            duplicate_count=0,
        )
        conn.commit()
        response = {
            "status": "preview_completed" if parsed.status in {"ok", "partial"} else "preview_failed",
            "run_id": run_id,
            "project_key": normalize_optional_text(project_key),
            "feature_flag_evaluation": flag_status["evaluation"],
            "model_client": resolved_model_client,
            "fake_mode": fake_mode if resolved_model_client == "fake" else None,
            "candidate_count": candidate_count,
            "prompt_chars": len(prompt),
            "parse_status": parsed.status,
            "parse_errors": parsed.errors,
            "accepted_auto_apply_count": len(validation["accepted_auto_apply"]),
            "needs_human_review_count": len(validation["needs_human_review"]),
            "rejected_by_validator_count": len(validation["rejected_by_validator"]),
            "validation": validation,
            "shadow_comparison": shadow_comparison,
        }
        if debug:
            response["raw_model_response"] = raw_response
        else:
            response["raw_model_response_chars"] = len(raw_response)
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _sandman_gemma_shadow_payload_from_timeline_row(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    payload_raw = row.get("payload_json")
    if not payload_raw:
        return {}
    try:
        payload = json.loads(str(payload_raw))
    except json.JSONDecodeError:
        return {"payload_decode_error": True}
    return payload if isinstance(payload, dict) else {}


@mcp.tool
def get_sandman_gemma_shadow_report(project_key: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Return recent Sandman Gemma preview/shadow runs and agreement metrics. Read-only."""
    normalized_project_key = normalize_optional_text(project_key)
    safe_limit = max(1, min(int(limit or 10), 100))
    filters = ["mode = 'sandman_gemma_preview'"]
    params: list[Any] = []
    if normalized_project_key is not None:
        filters.append("project_key = ?")
        params.append(normalized_project_key)
    conn = get_db_connection()
    try:
        run_rows = conn.execute(
            f"""
            SELECT *
            FROM sleep_runs
            WHERE {' AND '.join(filters)}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, safe_limit),
        ).fetchall()
        runs: list[dict[str, Any]] = []
        aggregate = {
            "run_count": 0,
            "candidate_count": 0,
            "agreement_count": 0,
            "model_only_count": 0,
            "oracle_only_count": 0,
            "rejected_decision_count": 0,
        }
        for run_row in run_rows:
            run = row_to_dict(run_row)
            run_id = int(run["id"])
            action_rows = conn.execute(
                """
                SELECT action_type, COUNT(*) AS count
                FROM sleep_run_actions
                WHERE run_id = ?
                GROUP BY action_type
                ORDER BY action_type ASC
                """,
                (run_id,),
            ).fetchall()
            completed_event_row = conn.execute(
                """
                SELECT *
                FROM timeline_events
                WHERE run_id = ? AND event_type = 'sandman_agent.gemma_preview_completed'
                ORDER BY id DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            payload = _sandman_gemma_shadow_payload_from_timeline_row(row_to_dict(completed_event_row) if completed_event_row else None)
            shadow_totals = payload.get("shadow_comparison") if isinstance(payload.get("shadow_comparison"), dict) else {}
            run_item = {
                "run": run,
                "action_summary": [row_to_dict(row) for row in action_rows],
                "shadow_comparison": shadow_totals,
                "candidate_count": int(payload.get("candidate_count") or run.get("scanned_count") or 0),
                "accepted_auto_apply_count": int(payload.get("accepted_auto_apply_count") or 0),
                "needs_human_review_count": int(payload.get("needs_human_review_count") or 0),
                "rejected_by_validator_count": int(payload.get("rejected_by_validator_count") or 0),
            }
            runs.append(run_item)
            aggregate["run_count"] += 1
            aggregate["candidate_count"] += int(run_item["candidate_count"])
            for key in ("agreement_count", "model_only_count", "oracle_only_count", "rejected_decision_count"):
                aggregate[key] += int(shadow_totals.get(key) or 0)
        comparable = aggregate["agreement_count"] + aggregate["model_only_count"]
        aggregate["agreement_rate"] = round(aggregate["agreement_count"] / comparable, 4) if comparable else None
        return {
            "status": "ok",
            "project_key": normalized_project_key,
            "limit": safe_limit,
            "aggregate": aggregate,
            "runs": runs,
        }
    finally:
        conn.close()




def _sandman_gemma_owner_target(memory: dict[str, Any]) -> tuple[str, str]:
    existing_role = normalize_optional_text(memory.get("owner_role"))
    project_key = normalize_optional_text(memory.get("project_key"))
    if existing_role == "review_team":
        return "review_team", "global_review_ops"
    if existing_role == "project_maintainer":
        return "project_maintainer", "global_project_ops"
    if existing_role == "maintainer":
        return "maintainer", "global_memory_ops"
    if project_key is not None:
        return "project_maintainer", "global_project_ops"
    return existing_role or "maintainer", "global_memory_ops"


def _apply_sandman_gemma_action(conn, *, decision: dict[str, Any], memory: dict[str, Any], notes: str | None = None) -> dict[str, Any]:
    memory_id = int(decision["memory_id"])
    action = decision["proposed_action"]
    now = utc_now_iso()
    if action == "set_owner":
        owner_role, owner_id = _sandman_gemma_owner_target(memory)
        previous = {"owner_role": memory.get("owner_role"), "owner_id": memory.get("owner_id")}
        conn.execute(
            """
            UPDATE memories
            SET owner_role = ?, owner_id = ?, last_accessed_at = ?
            WHERE id = ? AND archived_at IS NULL
            """,
            (owner_role, owner_id, now, memory_id),
        )
        payload = {
            "action": action,
            "previous": previous,
            "owner_role": owner_role,
            "owner_id": owner_id,
            "decision": decision,
            "notes": normalize_optional_text(notes),
        }
        audit_event_id = timeline.record_timeline_event(
            conn,
            event_type="sandman_agent.gemma_action_applied",
            memory_id=memory_id,
            timeline_scope="memory",
            semantic_kind="runtime_event",
            title="Sandman Gemma applied owner repair",
            project_key=normalize_optional_text(memory.get("project_key")),
            origin="sandman_agent_auto",
            payload=payload,
        )
        return {"memory_id": memory_id, "action": action, "status": "applied", "owner_role": owner_role, "owner_id": owner_id, "audit_event_id": audit_event_id}
    if action == "normalize_project_scope":
        previous = {"scope_code": memory.get("scope_code"), "layer_code": memory.get("layer_code"), "area_code": memory.get("area_code")}
        conn.execute(
            """
            UPDATE memories
            SET scope_code = 'project',
                layer_code = CASE WHEN layer_code IS NULL OR trim(layer_code) = '' OR layer_code = 'buffer' THEN 'projects' ELSE layer_code END,
                area_code = CASE WHEN area_code IS NULL OR trim(area_code) = '' THEN 'projects' ELSE area_code END,
                validation_source = COALESCE(validation_source, 'sandman_gemma_hygiene'),
                last_accessed_at = ?
            WHERE id = ? AND archived_at IS NULL
            """,
            (now, memory_id),
        )
        payload = {
            "action": action,
            "previous": previous,
            "scope_code": "project",
            "decision": decision,
            "notes": normalize_optional_text(notes),
        }
        audit_event_id = timeline.record_timeline_event(
            conn,
            event_type="sandman_agent.gemma_action_applied",
            memory_id=memory_id,
            timeline_scope="memory",
            semantic_kind="runtime_event",
            title="Sandman Gemma applied scope repair",
            project_key=normalize_optional_text(memory.get("project_key")),
            origin="sandman_agent_auto",
            payload=payload,
        )
        return {"memory_id": memory_id, "action": action, "status": "applied", "scope_code": "project", "audit_event_id": audit_event_id}
    return {"memory_id": memory_id, "action": action, "status": "skipped", "reason": "action_not_implemented_for_auto_apply"}


@mcp.tool
def run_sandman_gemma_hygiene(
    project_key: str | None = None,
    issue_kinds_json: str | None = None,
    limit: int = 100,
    max_auto_apply: int = 20,
    fake_mode: str | None = None,
    notes: str | None = None,
    model_client: str | None = "lms",
) -> dict[str, Any]:
    """Run Gemma-style Sandman hygiene for low-risk validated actions only. Semantic archive stays review-only."""
    issue_kinds = _parse_issue_kinds(issue_kinds_json) or [
        "owner_gap",
        "project_scope_mismatch",
        "test_probe_noise",
    ]
    # Preflight: brak Gemmy = blad, nie cichy fallback
    _resolved_client = (normalize_optional_text(model_client) or "lms")
    if _resolved_client not in {"fake", "test", "stub"}:
        sandman_gemma_runtime.ensure_gemma_ready()
    conn = get_db_connection()
    try:
        flag_status = _require_sandman_gemma_run_access(conn, project_key=project_key)
        run_id = create_sleep_run(conn, mode="sandman_gemma_run", freedom_level=0, notes=notes, project_key=project_key)
        _record_sandman_gemma_timeline_event(
            conn,
            event_type="sandman_agent.gemma_run_started",
            run_id=run_id,
            project_key=project_key,
            title="Sandman Gemma run started",
            payload={"issue_kinds": sorted(issue_kinds), "limit": int(limit), "max_auto_apply": int(max_auto_apply), "model_client": model_client},
        )
        candidates = _sandman_gemma_collect_candidates(conn, project_key=project_key, issue_kinds=issue_kinds, limit=limit)
        prompt = sandman_gemma_hygiene.build_sandman_gemma_hygiene_prompt(candidates, project_key=project_key)
        resolved_model_client, client = _sandman_gemma_preview_client(model_client, fake_mode)
        raw_response = client.complete_json(prompt, timeout_seconds=sandman_gemma_client.SANDMAN_GEMMA_TIMEOUT_SECONDS)
        parsed = sandman_gemma_hygiene.parse_gemma_decisions(raw_response)
        memory_ids = sorted({int(decision["memory_id"]) for decision in parsed.decisions})
        current_memories = _current_memory_map(conn, memory_ids)
        validation = sandman_gemma_hygiene.validate_sandman_decisions(parsed.decisions, current_memories)
        _record_sandman_gemma_decision_actions(conn, run_id=run_id, validation=validation)
        auto_apply_limit = max(0, min(int(max_auto_apply or 0), 100))
        accepted = validation["accepted_auto_apply"][:auto_apply_limit]
        overflow = validation["accepted_auto_apply"][len(accepted):]
        applied: list[dict[str, Any]] = []
        for decision in accepted:
            memory = current_memories.get(int(decision["memory_id"]))
            if memory is None:
                continue
            applied_action = _apply_sandman_gemma_action(conn, decision=decision, memory=memory, notes=notes)
            add_sleep_action(conn, run_id, "gemma_action_applied", int(decision["memory_id"]), {"memory": memory}, applied_action, str(decision.get("reason_code") or "low_risk_validated"))
            applied.append(applied_action)
        candidate_count = sum(len(items) for items in candidates.values())
        _record_sandman_gemma_timeline_event(
            conn,
            event_type="sandman_agent.gemma_run_completed",
            run_id=run_id,
            project_key=project_key,
            title="Sandman Gemma run completed",
            payload={
                "candidate_count": candidate_count,
                "parse_status": parsed.status,
                "auto_apply_requested_count": len(validation["accepted_auto_apply"]),
                "auto_apply_limit": auto_apply_limit,
                "auto_applied_count": len(applied),
                "overflow_count": len(overflow),
                "needs_human_review_count": len(validation["needs_human_review"]),
                "rejected_by_validator_count": len(validation["rejected_by_validator"]),
            },
        )
        finalize_sleep_run(
            conn,
            run_id,
            status="completed" if parsed.status in {"ok", "partial"} else "failed",
            scanned_count=candidate_count,
            changed_count=len(applied),
            archived_count=0,
            downgraded_count=0,
            duplicate_count=0,
        )
        conn.commit()
        return {
            "status": "completed" if parsed.status in {"ok", "partial"} else "failed",
            "run_id": run_id,
            "project_key": normalize_optional_text(project_key),
            "feature_flag_evaluation": flag_status["evaluation"],
            "model_client": resolved_model_client,
            "fake_mode": fake_mode if resolved_model_client == "fake" else None,
            "candidate_count": candidate_count,
            "parse_status": parsed.status,
            "parse_errors": parsed.errors,
            "auto_apply_requested_count": len(validation["accepted_auto_apply"]),
            "auto_apply_limit": auto_apply_limit,
            "auto_applied_count": len(applied),
            "overflow_count": len(overflow),
            "needs_human_review_count": len(validation["needs_human_review"]),
            "rejected_by_validator_count": len(validation["rejected_by_validator"]),
            "applied_actions": applied,
            "needs_human_review": validation["needs_human_review"],
            "rejected_by_validator": validation["rejected_by_validator"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _sandman_collect_touched_memories_for_dream(conn, memory_ids: list[int]) -> list[dict[str, Any]]:
    unique_ids = []
    for memory_id in memory_ids:
        try:
            normalized_id = int(memory_id)
        except (TypeError, ValueError):
            continue
        if normalized_id not in unique_ids:
            unique_ids.append(normalized_id)
    if not unique_ids:
        return []
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(f"SELECT * FROM memories WHERE id IN ({placeholders})", tuple(unique_ids)).fetchall()
    rows_by_id = {int(row["id"]): row_to_dict(row) for row in rows}
    return [rows_by_id[memory_id] for memory_id in unique_ids if memory_id in rows_by_id]


def _sandman_build_run_dream(
    conn,
    *,
    run_id: int,
    project_key: str | None,
    touched_memory_ids: list[int],
    action_hints: list[dict[str, Any]],
) -> dict[str, Any] | None:
    touched_memories = _sandman_collect_touched_memories_for_dream(conn, touched_memory_ids)
    context = sandman_dreams.build_dream_context(
        run_id=run_id,
        project_key=project_key,
        touched_memories=touched_memories,
        action_hints=action_hints,
        narrator_name="gemma_v1",
    )
    if context is None:
        return None
    artifact = sandman_dreams.make_dream_artifact(context, narrator=sandman_dreams.GemmaDreamNarrator())
    payload = sandman_dreams.artifact_payload(artifact)
    event_id = timeline.record_timeline_event(
        conn,
        event_type="sandman_agent.dream_generated",
        run_id=run_id,
        timeline_scope="run",
        semantic_kind="runtime_event",
        title="MAPI dream generated by Sandman run",
        project_key=normalize_optional_text(project_key),
        source_table="sleep_runs",
        source_row_id=run_id,
        origin="sandman_agent_auto",
        payload=payload,
    )
    add_sleep_action(
        conn,
        run_id,
        "dream_generated",
        None,
        None,
        {**payload, "timeline_event_id": event_id},
        "sandman_dream_side_effect",
    )
    return {**payload, "timeline_event_id": event_id}


@mcp.tool
def preview_sandman_v1(
    freedom_level: int = 1,
    notes: str | None = None,
    workspace_key: str | None = None,
    project_key: str | None = None,
) -> dict[str, Any]:
    """
    Sandman V1 (preview) Ă˘â‚¬â€ť podglĂ„â€¦d kandydatÄ‚Ĺ‚w do archiwizacji i downgrade.
    workspace_key: ogranicz do wspomnieÄąâ€ž z danego workspace (Faza 3).
    project_key: ogranicz do wspomnieÄąâ€ž z danego projektu (Faza 3).
    """
    if freedom_level not in {0, 1}:
        return {"status": "error", "error": 'Sandman V1 obsÄąâ€šuguje freedom_level 0 albo 1'}
    conn = get_db_connection()
    try:
        resolved_workspace_id = _resolve_workspace_id(conn, workspace_key) if workspace_key else None
        run_id = create_sleep_run(conn, mode="preview", freedom_level=freedom_level, notes=notes, workspace_id=resolved_workspace_id, project_key=project_key)
        duplicate_candidates = sandman_logic.get_duplicate_candidates(conn)
        archive_source = sandman_logic.get_archive_candidates(conn, workspace_id=resolved_workspace_id, project_key=project_key)
        downgrade_source = sandman_logic.get_downgrade_candidates(conn, workspace_id=resolved_workspace_id, project_key=project_key)
        archive_candidates, archive_skipped_due_to_duplicates = sandman_logic.filter_archive_candidates_for_duplicates(conn, archive_source, duplicate_candidates)
        downgrade_candidates, downgrade_skipped_due_to_duplicates = sandman_logic.filter_downgrade_candidates_for_duplicates(conn, downgrade_source, duplicate_candidates)
        secondary_duplicate_ids = sandman_logic.get_secondary_duplicate_memory_ids(conn, duplicate_candidates)
        protected_canonical_ids = sandman_logic.get_protected_canonical_memory_ids(conn, duplicate_candidates)
        dream_link_candidates = _sandman_get_dream_link_candidates(conn, workspace_id=resolved_workspace_id, project_key=project_key, max_links=80)
        dream_link_brake = dream_link_candidates[0].get("adaptive_brake") if dream_link_candidates else _sandman_adaptive_dream_link_limit(conn, workspace_id=resolved_workspace_id, project_key=project_key, requested_max_links=80)
        archive_candidates_dict = []
        for row in archive_candidates:
            row_dict = row_to_dict(row)
            if int(row["id"]) in secondary_duplicate_ids:
                row_dict["archive_reason"] = "duplicate_secondary_preferred_archive"
            archive_candidates_dict.append(row_dict)
        downgrade_candidates_dict = [row_to_dict(row) for row in downgrade_candidates]
        canonical_evidence_boost_candidates = []
        for canonical_id in sorted(protected_canonical_ids):
            memory = require_memory_row(conn, canonical_id)
            current_evidence = int(memory["evidence_count"] or 1)
            target_evidence = max(current_evidence, 1 + sandman_logic.get_incoming_duplicate_count(conn, canonical_id))
            if target_evidence > current_evidence:
                canonical_evidence_boost_candidates.append({"memory_id": canonical_id, "old_evidence_count": current_evidence, "new_evidence_count": target_evidence})

        for row in archive_candidates_dict:
            add_sleep_action(conn, run_id, "archive_candidate", int(row["id"]), {"activity_state": row.get("activity_state"), "importance_score": row.get("importance_score")}, {"activity_state": "archived"}, row.get("archive_reason", "working_low_value_no_recall"))
        for row in archive_skipped_due_to_duplicates:
            add_sleep_action(conn, run_id, "archive_skipped_duplicate_canonical", int(row["id"]), {"activity_state": row.get("activity_state"), "importance_score": row.get("importance_score")}, {"skipped": True}, "duplicate_pair_canonical_protected")
        for row in downgrade_candidates_dict:
            proposed_importance = round(max(float(row["importance_score"]) - 0.10, 0.05), 3)
            add_sleep_action(conn, run_id, "downgrade_candidate", int(row["id"]), {"importance_score": row.get("importance_score")}, {"importance_score": proposed_importance}, "low_activity_low_value")
        for row in downgrade_skipped_due_to_duplicates:
            add_sleep_action(conn, run_id, "downgrade_skipped_duplicate", int(row["id"]), {"importance_score": row.get("importance_score")}, {"skipped": True}, row.get("skip_reason", "duplicate_skip"))
        for pair in duplicate_candidates:
            add_sleep_action(conn, run_id, "duplicate_candidate", int(pair["duplicate_memory_id"]), {"canonical_memory_id": pair["canonical_memory_id"], "duplicate_memory_id": pair["duplicate_memory_id"]}, {"relation_type": "duplicate_of", "from_memory_id": pair["duplicate_memory_id"], "to_memory_id": pair["canonical_memory_id"]}, "same_content_or_high_similarity")
        for item in canonical_evidence_boost_candidates:
            add_sleep_action(conn, run_id, "canonical_evidence_boost_candidate", int(item["memory_id"]), {"evidence_count": item["old_evidence_count"]}, {"evidence_count": item["new_evidence_count"]}, "duplicate_support_bonus")

        scanned_count = conn.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
        finalize_sleep_run(conn, run_id, status="preview_completed", scanned_count=int(scanned_count), changed_count=0, archived_count=0, downgraded_count=0, duplicate_count=len(duplicate_candidates), conflict_count=0, created_summary_count=0)
        scope_info = {"workspace_key": workspace_key, "workspace_id": resolved_workspace_id, "project_key": project_key}
        return {"status": "preview_completed", "run_id": run_id, "freedom_level": freedom_level, "scanned_count": int(scanned_count), "scope": scope_info, "archive_candidates": archive_candidates_dict, "archive_skipped_due_to_duplicates": archive_skipped_due_to_duplicates, "downgrade_candidates": [{**row, "proposed_importance_score": round(max(float(row["importance_score"]) - 0.10, 0.05), 3)} for row in downgrade_candidates_dict], "downgrade_skipped_due_to_duplicates": downgrade_skipped_due_to_duplicates, "duplicate_candidates": duplicate_candidates, "canonical_evidence_boost_candidates": canonical_evidence_boost_candidates, "dream_link_candidates": dream_link_candidates, "dream_link_brake": dream_link_brake, "summary": {"archive_count": len(archive_candidates_dict), "archive_skipped_due_to_duplicates_count": len(archive_skipped_due_to_duplicates), "downgrade_count": len(downgrade_candidates_dict), "duplicate_count": len(duplicate_candidates), "skipped_duplicate_downgrade_count": len(downgrade_skipped_due_to_duplicates), "canonical_evidence_boost_count": len(canonical_evidence_boost_candidates), "dream_link_candidate_count": len(dream_link_candidates)}}
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


@mcp.tool
def run_sandman_v1(
    freedom_level: int = 1,
    notes: str | None = None,
    workspace_key: str | None = None,
    project_key: str | None = None,
) -> dict[str, Any]:
    """
    Sandman V1 Ă˘â‚¬â€ť archiwizacja, downgrade, duplikaty.
    workspace_key: ogranicz do wspomnieÄąâ€ž z danego workspace (Faza 3).
    project_key: ogranicz do wspomnieÄąâ€ž z danego projektu (Faza 3).
    """
    if freedom_level not in {0, 1}:
        return {"status": "error", "error": 'Sandman V1 obsÄąâ€šuguje freedom_level 0 albo 1'}
    conn = get_db_connection()
    try:
        resolved_workspace_id = _resolve_workspace_id(conn, workspace_key) if workspace_key else None
        run_id = create_sleep_run(conn, mode="run", freedom_level=freedom_level, notes=notes, workspace_id=resolved_workspace_id, project_key=project_key)
        duplicate_candidates = sandman_logic.get_duplicate_candidates(conn)
        archive_source = sandman_logic.get_archive_candidates(conn, workspace_id=resolved_workspace_id, project_key=project_key)
        downgrade_source = sandman_logic.get_downgrade_candidates(conn, workspace_id=resolved_workspace_id, project_key=project_key)
        archive_candidates, archive_skipped_due_to_duplicates = sandman_logic.filter_archive_candidates_for_duplicates(conn, archive_source, duplicate_candidates)
        downgrade_candidates, downgrade_skipped_due_to_duplicates = sandman_logic.filter_downgrade_candidates_for_duplicates(conn, downgrade_source, duplicate_candidates)
        secondary_duplicate_ids = sandman_logic.get_secondary_duplicate_memory_ids(conn, duplicate_candidates)
        protected_canonical_ids = sandman_logic.get_protected_canonical_memory_ids(conn, duplicate_candidates)
        dream_link_candidates = _sandman_get_dream_link_candidates(conn, workspace_id=resolved_workspace_id, project_key=project_key, max_links=80)
        dream_link_brake = dream_link_candidates[0].get("adaptive_brake") if dream_link_candidates else _sandman_adaptive_dream_link_limit(conn, workspace_id=resolved_workspace_id, project_key=project_key, requested_max_links=80)

        archived_items: list[dict[str, Any]] = []
        downgraded_items: list[dict[str, Any]] = []
        duplicate_links_created: list[dict[str, Any]] = []
        dream_links_created: list[dict[str, Any]] = []
        canonical_evidence_boosted: list[dict[str, Any]] = []
        dream_action_hints: list[dict[str, Any]] = []
        dream_touched_memory_ids: list[int] = []

        for row in archive_candidates:
            memory_id = int(row["id"])
            archived_at = utc_now_iso()
            if memory_id in secondary_duplicate_ids:
                archive_reason = "duplicate_secondary_preferred_archive"
                sandman_note = "Sandman V1: duplicate_secondary_preferred_archive"
            else:
                archive_reason = "working_low_value_no_recall"
                sandman_note = "Sandman V1: working_low_value_no_recall"
            conn.execute("UPDATE memories SET activity_state = 'archived', state_code = 'archived', archived_at = ?, sandman_note = ? WHERE id = ?", (archived_at, sandman_note, memory_id))
            add_sleep_action(conn, run_id, "archived", memory_id, {"activity_state": row["activity_state"], "state_code": row["state_code"]}, {"activity_state": "archived", "state_code": "archived", "archived_at": archived_at}, archive_reason)
            dream_touched_memory_ids.append(memory_id)
            dream_action_hints.append({"memory_id": memory_id, "action_type": "archived", "reason": archive_reason})
            updated = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            archived_items.append(row_to_dict(updated))

        for row in archive_skipped_due_to_duplicates:
            add_sleep_action(conn, run_id, "archive_skipped_duplicate_canonical", int(row["id"]), {"activity_state": row.get("activity_state"), "importance_score": row.get("importance_score")}, {"skipped": True}, "duplicate_pair_canonical_protected")
        for row in downgrade_candidates:
            memory_id = int(row["id"])
            old_importance = float(row["importance_score"])
            new_importance = round(max(old_importance - 0.10, 0.05), 3)
            conn.execute("UPDATE memories SET importance_score = ?, sandman_note = ? WHERE id = ?", (new_importance, "Sandman V1: low_activity_low_value", memory_id))
            add_sleep_action(conn, run_id, "downgraded", memory_id, {"importance_score": old_importance}, {"importance_score": new_importance}, "low_activity_low_value")
            dream_touched_memory_ids.append(memory_id)
            dream_action_hints.append({"memory_id": memory_id, "action_type": "downgraded", "reason": "low_activity_low_value"})
            updated = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            downgraded_items.append(row_to_dict(updated))
        for row in downgrade_skipped_due_to_duplicates:
            add_sleep_action(conn, run_id, "downgrade_skipped_duplicate", int(row["id"]), {"importance_score": row.get("importance_score")}, {"skipped": True}, row.get("skip_reason", "duplicate_skip"))

        for pair in duplicate_candidates:
            canonical_id = int(pair["canonical_memory_id"])
            duplicate_id = int(pair["duplicate_memory_id"])
            if not sandman_logic.duplicate_link_exists(conn, duplicate_id, canonical_id):
                item = _create_link(conn, duplicate_id, canonical_id, "duplicate_of", 0.95, "sandman_v1_auto")
                duplicate_links_created.append(item)
                add_sleep_action(conn, run_id, "duplicate_link_created", duplicate_id, None, {"link_id": item["id"], "from_memory_id": duplicate_id, "to_memory_id": canonical_id, "relation_type": "duplicate_of"}, "same_content_or_high_similarity")
                dream_touched_memory_ids.extend([duplicate_id, canonical_id])
                dream_action_hints.append({"memory_id": duplicate_id, "action_type": "duplicate_link_created", "relation_type": "duplicate_of", "related_memory_id": canonical_id})
                dream_action_hints.append({"memory_id": canonical_id, "action_type": "duplicate_link_created", "relation_type": "duplicate_of", "related_memory_id": duplicate_id})
            conn.execute("UPDATE memories SET sandman_note = ? WHERE id = ?", (f"Sandman V1: duplicate_of {canonical_id}", duplicate_id))

        add_sleep_action(conn, run_id, "dream_link_brake", None, None, dream_link_brake, "adaptive_density_brake")

        for item in dream_link_candidates:
            if not item.get("from_memory_id") or not item.get("to_memory_id") or not item.get("relation_type"):
                continue
            created = _create_link(conn, int(item["from_memory_id"]), int(item["to_memory_id"]), str(item["relation_type"]), float(item.get("weight") or 0.5), "sandman_v1_dream")
            dream_links_created.append(created)
            from_id = int(item["from_memory_id"])
            to_id = int(item["to_memory_id"])
            add_sleep_action(conn, run_id, "dream_link_created", from_id, None, {**item, "link_id": created.get("id")}, item.get("reason", "sandman_dream_linking"))
            dream_touched_memory_ids.extend([from_id, to_id])
            dream_action_hints.append({"memory_id": from_id, "action_type": "dream_link_created", "relation_type": item.get("relation_type"), "related_memory_id": to_id})
            dream_action_hints.append({"memory_id": to_id, "action_type": "dream_link_created", "relation_type": item.get("relation_type"), "related_memory_id": from_id})

        for canonical_id in sorted(protected_canonical_ids):
            boosted = sandman_logic.boost_canonical_evidence_count(conn, canonical_id)
            if boosted is not None:
                canonical_evidence_boosted.append(boosted)
                add_sleep_action(conn, run_id, "canonical_evidence_boosted", canonical_id, {"evidence_count": boosted["old_evidence_count"]}, {"evidence_count": boosted["new_evidence_count"]}, "duplicate_support_bonus")
                dream_touched_memory_ids.append(canonical_id)
                dream_action_hints.append({"memory_id": canonical_id, "action_type": "canonical_evidence_boosted", "reason": "duplicate_support_bonus"})

        dream_artifact = _sandman_build_run_dream(
            conn,
            run_id=run_id,
            project_key=project_key,
            touched_memory_ids=dream_touched_memory_ids,
            action_hints=dream_action_hints,
        )

        conn.commit()
        scanned_count = conn.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
        finalize_sleep_run(conn, run_id, status="completed", scanned_count=int(scanned_count), changed_count=len(archived_items) + len(downgraded_items) + len(duplicate_links_created) + len(dream_links_created) + len(canonical_evidence_boosted), archived_count=len(archived_items), downgraded_count=len(downgraded_items), duplicate_count=len(duplicate_candidates), conflict_count=0, created_summary_count=0)
        scope_info = {"workspace_key": workspace_key, "workspace_id": resolved_workspace_id, "project_key": project_key}
        return {"status": "completed", "run_id": run_id, "freedom_level": freedom_level, "scanned_count": int(scanned_count), "scope": scope_info, "archived_items": archived_items, "archive_skipped_due_to_duplicates": archive_skipped_due_to_duplicates, "downgraded_items": downgraded_items, "downgrade_skipped_due_to_duplicates": downgrade_skipped_due_to_duplicates, "duplicate_candidates": duplicate_candidates, "duplicate_links_created": duplicate_links_created, "dream_link_candidates": dream_link_candidates, "dream_links_created": dream_links_created, "dream_artifact": dream_artifact, "dream_link_brake": dream_link_brake, "canonical_evidence_boosted": canonical_evidence_boosted, "summary": {"changed_count": len(archived_items) + len(downgraded_items) + len(duplicate_links_created) + len(dream_links_created) + len(canonical_evidence_boosted), "archived_count": len(archived_items), "archive_skipped_due_to_duplicates_count": len(archive_skipped_due_to_duplicates), "downgraded_count": len(downgraded_items), "duplicate_count": len(duplicate_candidates), "duplicate_links_created_count": len(duplicate_links_created), "dream_links_created_count": len(dream_links_created), "dream_generated": dream_artifact is not None, "skipped_duplicate_downgrade_count": len(downgrade_skipped_due_to_duplicates), "canonical_evidence_boost_count": len(canonical_evidence_boosted)}}
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


@mcp.tool
def preview_sandman_ai(freedom_level: int = 1, notes: str | None = None) -> dict[str, Any]:
    """
    Sandman AI (preview) Ă˘â‚¬â€ť uÄąÄ˝ywa LM Studio (Qwen) do oceny wspomnieÄąâ€ž.
    freedom_level: 0=konserwatywny, 1=normalny, 2=agresywny.
    Nie wprowadza ÄąÄ˝adnych zmian w bazie.
    """
    if freedom_level not in {0, 1, 2}:
        return {"status": "error", "error": 'Sandman AI obsÄąâ€šuguje freedom_level 0, 1 lub 2'}
    conn = get_db_connection()
    try:
        run_id = create_sleep_run(conn, mode="ai_preview", freedom_level=freedom_level, notes=notes)
        duplicate_candidates = sandman_logic.get_duplicate_candidates(conn)
        protected_canonical_ids = sandman_logic.get_protected_canonical_memory_ids(conn, duplicate_candidates)
        secondary_duplicate_ids = sandman_logic.get_secondary_duplicate_memory_ids(conn, duplicate_candidates)

        archive_decisions, downgrade_decisions, keep_decisions = sandman_ai.get_ai_decisions(conn, freedom_level)

        canonical_evidence_boost_candidates = []
        for canonical_id in sorted(protected_canonical_ids):
            memory = require_memory_row(conn, canonical_id)
            current_evidence = int(memory["evidence_count"] or 1)
            target_evidence = max(current_evidence, 1 + sandman_logic.get_incoming_duplicate_count(conn, canonical_id))
            if target_evidence > current_evidence:
                canonical_evidence_boost_candidates.append({"memory_id": canonical_id, "old_evidence_count": current_evidence, "new_evidence_count": target_evidence})

        for item in archive_decisions:
            add_sleep_action(conn, run_id, "archive_candidate", int(item["id"]), {"activity_state": item.get("activity_state"), "importance_score": item.get("importance_score")}, {"activity_state": "archived"}, item.get("ai_reason", "ai_decision"))
        for item in downgrade_decisions:
            old_importance = float(item.get("importance_score") or 0.5)
            proposed = item.get("ai_new_importance") or round(max(old_importance - 0.10, 0.05), 3)
            add_sleep_action(conn, run_id, "downgrade_candidate", int(item["id"]), {"importance_score": old_importance}, {"importance_score": proposed}, item.get("ai_reason", "ai_decision"))
        for pair in duplicate_candidates:
            add_sleep_action(conn, run_id, "duplicate_candidate", int(pair["duplicate_memory_id"]), {"canonical_memory_id": pair["canonical_memory_id"], "duplicate_memory_id": pair["duplicate_memory_id"]}, {"relation_type": "duplicate_of", "from_memory_id": pair["duplicate_memory_id"], "to_memory_id": pair["canonical_memory_id"]}, "same_content_or_high_similarity")
        for item in canonical_evidence_boost_candidates:
            add_sleep_action(conn, run_id, "canonical_evidence_boost_candidate", int(item["memory_id"]), {"evidence_count": item["old_evidence_count"]}, {"evidence_count": item["new_evidence_count"]}, "duplicate_support_bonus")

        scanned_count = conn.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
        finalize_sleep_run(conn, run_id, status="preview_completed", scanned_count=int(scanned_count), changed_count=0, archived_count=0, downgraded_count=0, duplicate_count=len(duplicate_candidates), conflict_count=0, created_summary_count=0)

        return {
            "status": "preview_completed",
            "run_id": run_id,
            "freedom_level": freedom_level,
            "model": sandman_ai.LM_STUDIO_MODEL,
            "scanned_count": int(scanned_count),
            "archive_candidates": archive_decisions,
            "downgrade_candidates": [
                {**item, "proposed_importance_score": item.get("ai_new_importance") or round(max(float(item.get("importance_score") or 0.5) - 0.10, 0.05), 3)}
                for item in downgrade_decisions
            ],
            "keep_count": len(keep_decisions),
            "duplicate_candidates": duplicate_candidates,
            "canonical_evidence_boost_candidates": canonical_evidence_boost_candidates,
            "summary": {
                "archive_count": len(archive_decisions),
                "downgrade_count": len(downgrade_decisions),
                "keep_count": len(keep_decisions),
                "duplicate_count": len(duplicate_candidates),
                "canonical_evidence_boost_count": len(canonical_evidence_boost_candidates),
            },
        }
    finally:
        conn.close()


@mcp.tool
def run_sandman_ai(freedom_level: int = 1, notes: str | None = None) -> dict[str, Any]:
    """
    Sandman AI (wykonanie) Ă˘â‚¬â€ť uÄąÄ˝ywa LM Studio (Qwen) do oceny wspomnieÄąâ€ž i wprowadza zmiany.
    freedom_level: 0=konserwatywny, 1=normalny, 2=agresywny.
    Wszystkie zmiany sĂ„â€¦ undo-safe (moÄąÄ˝na cofnĂ„â€¦Ă„â€ˇ przez undo_run).
    """
    if freedom_level not in {0, 1, 2}:
        return {"status": "error", "error": 'Sandman AI obsÄąâ€šuguje freedom_level 0, 1 lub 2'}
    conn = get_db_connection()
    try:
        run_id = create_sleep_run(conn, mode="ai_run", freedom_level=freedom_level, notes=notes)
        duplicate_candidates = sandman_logic.get_duplicate_candidates(conn)
        protected_canonical_ids = sandman_logic.get_protected_canonical_memory_ids(conn, duplicate_candidates)
        secondary_duplicate_ids = sandman_logic.get_secondary_duplicate_memory_ids(conn, duplicate_candidates)

        archive_decisions, downgrade_decisions, _ = sandman_ai.get_ai_decisions(conn, freedom_level)

        archived_items: list[dict[str, Any]] = []
        downgraded_items: list[dict[str, Any]] = []
        duplicate_links_created: list[dict[str, Any]] = []
        canonical_evidence_boosted: list[dict[str, Any]] = []

        for item in archive_decisions:
            memory_id = int(item["id"])
            archived_at = utc_now_iso()
            sandman_note = f"Sandman AI: {item.get('ai_reason', 'ai_decision')}"
            conn.execute("UPDATE memories SET activity_state = 'archived', archived_at = ?, sandman_note = ? WHERE id = ?", (archived_at, sandman_note, memory_id))
            add_sleep_action(conn, run_id, "archived", memory_id, {"activity_state": item.get("activity_state")}, {"activity_state": "archived", "archived_at": archived_at}, item.get("ai_reason", "ai_decision"))
            updated = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            archived_items.append(row_to_dict(updated))

        for item in downgrade_decisions:
            memory_id = int(item["id"])
            old_importance = float(item.get("importance_score") or 0.5)
            new_importance = item.get("ai_new_importance") or round(max(old_importance - 0.10, 0.05), 3)
            sandman_note = f"Sandman AI: {item.get('ai_reason', 'ai_decision')}"
            conn.execute("UPDATE memories SET importance_score = ?, sandman_note = ? WHERE id = ?", (new_importance, sandman_note, memory_id))
            add_sleep_action(conn, run_id, "downgraded", memory_id, {"importance_score": old_importance}, {"importance_score": new_importance}, item.get("ai_reason", "ai_decision"))
            updated = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            downgraded_items.append(row_to_dict(updated))

        for pair in duplicate_candidates:
            canonical_id = int(pair["canonical_memory_id"])
            duplicate_id = int(pair["duplicate_memory_id"])
            if not sandman_logic.duplicate_link_exists(conn, duplicate_id, canonical_id):
                item = _create_link(conn, duplicate_id, canonical_id, "duplicate_of", 0.95, "sandman_ai_auto")
                duplicate_links_created.append(item)
                add_sleep_action(conn, run_id, "duplicate_link_created", duplicate_id, None, {"link_id": item["id"], "from_memory_id": duplicate_id, "to_memory_id": canonical_id, "relation_type": "duplicate_of"}, "same_content_or_high_similarity")
            conn.execute("UPDATE memories SET sandman_note = ? WHERE id = ?", (f"Sandman AI: duplicate_of {canonical_id}", duplicate_id))

        for canonical_id in sorted(protected_canonical_ids):
            boosted = sandman_logic.boost_canonical_evidence_count(conn, canonical_id)
            if boosted is not None:
                canonical_evidence_boosted.append(boosted)
                add_sleep_action(conn, run_id, "canonical_evidence_boosted", canonical_id, {"evidence_count": boosted["old_evidence_count"]}, {"evidence_count": boosted["new_evidence_count"]}, "duplicate_support_bonus")

        conn.commit()
        scanned_count = conn.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
        changed_count = len(archived_items) + len(downgraded_items) + len(duplicate_links_created) + len(canonical_evidence_boosted)
        finalize_sleep_run(conn, run_id, status="completed", scanned_count=int(scanned_count), changed_count=changed_count, archived_count=len(archived_items), downgraded_count=len(downgraded_items), duplicate_count=len(duplicate_candidates), conflict_count=0, created_summary_count=0)

        return {
            "status": "completed",
            "run_id": run_id,
            "freedom_level": freedom_level,
            "model": sandman_ai.LM_STUDIO_MODEL,
            "scanned_count": int(scanned_count),
            "archived_items": archived_items,
            "downgraded_items": downgraded_items,
            "duplicate_candidates": duplicate_candidates,
            "duplicate_links_created": duplicate_links_created,
            "canonical_evidence_boosted": canonical_evidence_boosted,
            "summary": {
                "changed_count": changed_count,
                "archived_count": len(archived_items),
                "downgraded_count": len(downgraded_items),
                "duplicate_count": len(duplicate_candidates),
                "duplicate_links_created_count": len(duplicate_links_created),
                "canonical_evidence_boost_count": len(canonical_evidence_boosted),
            },
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MEMORY LINKING PASS V1 / V1.1  (implementation -> app/memory/linking.py)
# ---------------------------------------------------------------------------

@mcp.tool
def preview_memory_linking_pass(
    project_key: str | None = None,
    limit: int = 50,
    max_links_per_memory: int = 4,
    min_score: float = 0.45,
    notes: str | None = None,
) -> dict[str, Any]:
    """Preview kandydatow do deterministycznego linkowania grafu pamieci."""
    conn = get_db_connection()
    try:
        return preview_memory_linking_pass_payload(
            conn,
            project_key=project_key,
            limit=limit,
            max_links_per_memory=max_links_per_memory,
            min_score=min_score,
            notes=notes,
            row_to_dict=row_to_dict,
            create_sleep_run=create_sleep_run,
            add_sleep_action=add_sleep_action,
            finalize_sleep_run=finalize_sleep_run,
        )
    finally:
        conn.close()


@mcp.tool
def run_memory_linking_pass(
    project_key: str | None = None,
    limit: int = 50,
    max_links_per_memory: int = 4,
    min_score: float = 0.45,
    notes: str | None = None,
) -> dict[str, Any]:
    """Deterministycznie tworzy brakujace linki grafu pamieci."""
    conn = get_db_connection()
    try:
        return run_memory_linking_pass_payload(
            conn,
            project_key=project_key,
            limit=limit,
            max_links_per_memory=max_links_per_memory,
            min_score=min_score,
            notes=notes,
            row_to_dict=row_to_dict,
            create_sleep_run=create_sleep_run,
            add_sleep_action=add_sleep_action,
            finalize_sleep_run=finalize_sleep_run,
            create_link=_create_link,
        )
    finally:
        conn.close()


@mcp.tool
def preview_consolidation_v1(notes: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        run_id = create_sleep_run(conn, mode="consolidation_preview", freedom_level=0, notes=notes)
        candidates = consolidation_logic.get_consolidation_candidates(conn)
        for candidate in candidates:
            add_sleep_action(conn, run_id, "consolidation_candidate", int(candidate["central_memory_id"]), {"member_ids": candidate["member_ids"]}, {"supporting_memory_ids": candidate["supporting_memory_ids"], "support_links_to_create_count": candidate["support_links_to_create_count"], "summary_memory_to_create": candidate["summary_memory_to_create"], "summary_links_to_create_count": candidate["summary_links_to_create_count"]}, "gravity_cluster_candidate")
        scanned_count = conn.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
        finalize_sleep_run(conn, run_id, status="preview_completed", scanned_count=int(scanned_count), changed_count=0, archived_count=0, downgraded_count=0, duplicate_count=0, conflict_count=0, created_summary_count=0)
        return {"status": "preview_completed", "run_id": run_id, "scanned_count": int(scanned_count), "consolidation_candidates": candidates, "summary": {"cluster_count": len(candidates), "support_links_to_create_count": sum(int(item["support_links_to_create_count"]) for item in candidates), "summary_memory_to_create_count": sum(1 for item in candidates if bool(item["summary_memory_to_create"])), "summary_links_to_create_count": sum(int(item["summary_links_to_create_count"]) for item in candidates), "total_links_to_create_count": sum(int(item["total_links_to_create_count"]) for item in candidates)}}
    finally:
        conn.close()


@mcp.tool
def run_consolidation_v1(notes: str | None = None) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        run_id = create_sleep_run(conn, mode="consolidation_run", freedom_level=0, notes=notes)
        candidates = consolidation_logic.get_consolidation_candidates(conn)
        support_links_created: list[dict[str, Any]] = []
        summary_links_created: list[dict[str, Any]] = []
        created_summary_memories: list[dict[str, Any]] = []
        central_evidence_boosted: list[dict[str, Any]] = []

        for candidate in candidates:
            central_id = int(candidate["central_memory_id"])
            links_created_for_cluster = 0
            for member_id in candidate["supporting_memory_ids"]:
                if consolidation_logic.support_link_exists(conn, int(member_id), central_id):
                    continue
                item = _create_link(conn, int(member_id), central_id, "supports", float(candidate["average_gravity"] or 0.5), "consolidation_v1_auto")
                support_links_created.append(item)
                links_created_for_cluster += 1
                add_sleep_action(conn, run_id, "support_link_created", int(member_id), None, {"link_id": item["id"], "from_memory_id": int(member_id), "to_memory_id": central_id, "relation_type": "supports"}, "gravity_support_link")

            summary_memory_id = candidate.get("existing_summary_memory_id")
            if summary_memory_id is None:
                proposed_summary = candidate["proposed_summary_memory"]
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

            if not consolidation_logic.summary_link_exists(conn, int(summary_memory_id), central_id, "summarizes"):
                item = _create_link(conn, int(summary_memory_id), central_id, "summarizes", 1.0, "consolidation_v1_auto")
                summary_links_created.append(item)
                add_sleep_action(conn, run_id, "summary_link_created", int(summary_memory_id), None, {"link_id": item["id"], "from_memory_id": int(summary_memory_id), "to_memory_id": central_id, "relation_type": "summarizes"}, "gravity_summary_link")

            for member_id in candidate["member_ids"]:
                if consolidation_logic.summary_link_exists(conn, int(summary_memory_id), int(member_id), "consolidated_from"):
                    continue
                item = _create_link(conn, int(summary_memory_id), int(member_id), "consolidated_from", 1.0, "consolidation_v1_auto")
                summary_links_created.append(item)
                add_sleep_action(conn, run_id, "summary_link_created", int(summary_memory_id), None, {"link_id": item["id"], "from_memory_id": int(summary_memory_id), "to_memory_id": int(member_id), "relation_type": "consolidated_from"}, "gravity_summary_link")

            if links_created_for_cluster > 0:
                central_memory = require_memory_row(conn, central_id)
                old_evidence_count = int(central_memory["evidence_count"] or 1)
                new_evidence_count = old_evidence_count + links_created_for_cluster
                conn.execute("UPDATE memories SET evidence_count = ?, sandman_note = ? WHERE id = ?", (new_evidence_count, f"Consolidation V1: gravity cluster of {candidate['member_count']} memories", central_id))
                boosted = {"memory_id": central_id, "old_evidence_count": old_evidence_count, "new_evidence_count": new_evidence_count}
                central_evidence_boosted.append(boosted)
                add_sleep_action(conn, run_id, "canonical_evidence_boosted", central_id, {"evidence_count": old_evidence_count}, {"evidence_count": new_evidence_count}, "gravity_cluster_support_bonus")

        conn.commit()
        scanned_count = conn.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
        changed_count = len(support_links_created) + len(summary_links_created) + len(created_summary_memories) + len(central_evidence_boosted)
        finalize_sleep_run(conn, run_id, status="completed", scanned_count=int(scanned_count), changed_count=changed_count, archived_count=0, downgraded_count=0, duplicate_count=0, conflict_count=0, created_summary_count=len(created_summary_memories))
        return {"status": "completed", "run_id": run_id, "scanned_count": int(scanned_count), "consolidation_candidates": candidates, "support_links_created": support_links_created, "summary_links_created": summary_links_created, "created_summary_memories": created_summary_memories, "central_evidence_boosted": central_evidence_boosted, "summary": {"cluster_count": len(candidates), "links_created_count": len(support_links_created), "support_links_created_count": len(support_links_created), "summary_links_created_count": len(summary_links_created), "summary_memories_created_count": len(created_summary_memories), "central_evidence_boost_count": len(central_evidence_boosted), "changed_count": changed_count}}
    finally:
        conn.close()



def _record_agent_session_to_timeline(
    conn: "sqlite3.Connection",
    *,
    user_query: str,
    result: "dict[str, Any]",
) -> None:
    """Zapisuje sesjĂ„â„˘ sandman_agent do timeline. BÄąâ€šĂ„â„˘dy sĂ„â€¦ ignorowane (best-effort)."""
    try:
        tools_used = list({
            step["tool_name"]
            for step in result.get("trace", [])
            if step.get("tool_name") and step["tool_name"] != "none"
        })
        write_tools = {"create_memory", "archive_memory", "link_memories", "update_memory_importance"}
        payload = {
            "query": (user_query or "")[:200],
            "steps": result.get("steps", 0),
            "status": result.get("status", "unknown"),
            "tools_used": tools_used,
            "write_tools_used": [t for t in tools_used if t in write_tools],
        }
        timeline.record_timeline_event(
            conn,
            event_type="sandman_agent.session",
            origin="sandman_agent_auto",
            timeline_scope="system",
            semantic_kind="runtime_event",
            title=f"Sandman agent: {(user_query or '')[:80]}",
            payload=payload,
        )
        conn.commit()
    except Exception:
        pass


@mcp.tool
def sandman_memory_chat(user_query: str, max_steps: int = 4) -> dict[str, Any]:
    """
    Sandman Memory Chat Ă˘â‚¬â€ť host steruje narzĂ„â„˘dziami MAPI dla lokalnego modelu.
    Model moÄąÄ˝e iteracyjnie woÄąâ€šaĂ„â€ˇ wyszukiwanie wspomnieÄąâ€ž, odczyt pamiĂ„â„˘ci, linkÄ‚Ĺ‚w i osi projektu.
    """
    if not user_query or not user_query.strip():
        return {"status": "error", "error": 'user_query nie moÄąÄ˝e byĂ„â€ˇ puste'}
    if max_steps < 1 or max_steps > 16:
        return {"status": "error", "error": 'max_steps musi byĂ„â€ˇ w zakresie 1..16'}
    conn = get_db_connection()
    try:
        from app import sandman_agent
        result = sandman_agent.run_memory_tool_agent(conn, user_query=user_query, max_steps=max_steps)
        _record_agent_session_to_timeline(conn, user_query=user_query, result=result)
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sprint 4 Ă˘â‚¬â€ś layer promotion / demotion helpers and tools
# ---------------------------------------------------------------------------

def _validate_layer_transition(from_layer: str | None, to_layer: str) -> None:
    return validate_layer_transition(from_layer, to_layer, layer_order=LAYER_ORDER)


def _do_layer_move(conn, memory_id: int, target_layer: str, reason: str, direction: str) -> dict[str, Any]:
    """
    Core implementation shared by promote_memory / demote_memory.
    direction: 'promote' | 'demote'
    Returns the updated memory dict.
    """
    return layer_move_payload(
        conn,
        memory_id,
        target_layer,
        reason,
        direction,
        layer_order=LAYER_ORDER,
        require_memory_row=require_memory_row,
        row_to_dict=row_to_dict,
        validate_layer_transition=_validate_layer_transition,
        record_timeline_event=timeline.record_timeline_event,
    )


@mcp.tool
def promote_memory(memory_id: int, target_layer: str, reason: str) -> dict[str, Any]:
    """
    Awansuje wspomnienie na wyÄąÄ˝szĂ„â€¦ warstwĂ„â„˘.
    Dozwolone warstwy (rosnĂ„â€¦co): buffer Ă˘â€ â€™ working Ă˘â€ â€™ projects Ă˘â€ â€™ autobio Ă˘â€ â€™ identity Ă˘â€ â€™ core.
    Chroni warstwy core i identity przed nadpisaniem przez niÄąÄ˝sze.
    """
    conn = get_db_connection()
    try:
        return promote_memory_payload(
            conn,
            memory_id=memory_id,
            target_layer=target_layer,
            reason=reason,
            protected_layers=SANDMAN_PROTECTED_LAYERS,
            normalize_layer_code=normalize_layer_code,
            layer_move=_do_layer_move,
        )
    finally:
        conn.close()


@mcp.tool
def demote_memory(memory_id: int, target_layer: str, reason: str) -> dict[str, Any]:
    """
    Degraduje wspomnienie do niÄąÄ˝szej warstwy.
    Nie moÄąÄ˝na degradowaĂ„â€ˇ wspomnieÄąâ€ž z chronionych warstw core/identity.
    """
    conn = get_db_connection()
    try:
        return demote_memory_payload(
            conn,
            memory_id=memory_id,
            target_layer=target_layer,
            reason=reason,
            protected_layers=SANDMAN_PROTECTED_LAYERS,
            normalize_layer_code=normalize_layer_code,
            require_memory_row=require_memory_row,
            row_to_dict=row_to_dict,
            layer_move=_do_layer_move,
        )
    finally:
        conn.close()


@mcp.tool
def get_promotion_candidates(
    min_evidence: int = 2,
    min_importance: float = 0.6,
    min_confidence: float = 0.6,
    source_layer: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """
    Zwraca listĂ„â„˘ wspomnieÄąâ€ž, ktÄ‚Ĺ‚re speÄąâ€šniajĂ„â€¦ kryteria awansu (evidence_count, importance_score, confidence_score).
    Opcjonalnie filtruje po warstwie ÄąĹźrÄ‚Ĺ‚dÄąâ€šowej.
    """
    conn = get_db_connection()
    try:
        return promotion_candidates_payload(
            conn,
            min_evidence=min_evidence,
            min_importance=min_importance,
            min_confidence=min_confidence,
            source_layer=source_layer,
            limit=limit,
            get_promotion_candidates=sandman_logic.get_promotion_candidates,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sprint 6 Ă˘â‚¬â€ś operational insight tools
# ---------------------------------------------------------------------------

@mcp.tool
def get_layer_stats() -> dict[str, Any]:
    """
    Zwraca statystyki rozkÄąâ€šadu wspomnieÄąâ€ž wedÄąâ€šug layer_code, area_code i state_code.
    Przydatne do monitorowania kondycji bazy wspomnieÄąâ€ž.
    """
    conn = get_db_connection()
    try:
        return layer_stats_payload(conn)
    finally:
        conn.close()


@mcp.tool
def get_version_lineage(memory_id: int) -> dict[str, Any]:
    """
    Zwraca peÄąâ€šne drzewo wersji wspomnienia (przodkowie i potomkowie przez supersedes_memory_id).
    Posortowane wedÄąâ€šug version ASC, id ASC.
    """
    conn = get_db_connection()
    try:
        return version_lineage_payload(conn, memory_id=memory_id, collect_version_lineage=_collect_version_lineage)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Faza 4 Ă˘â‚¬â€ť scope promotion governance
# ---------------------------------------------------------------------------

@mcp.tool
def propose_scope_promotion(
    memory_id: int,
    target_scope: str,
    reason: str,
    user_key: str | None = None,
) -> dict[str, Any]:
    """
    ZgÄąâ€šasza wniosek o poszerzenie scope wspomnienia (np. private Ă˘â€ â€™ project, project Ă˘â€ â€™ workspace).
    Nie zmienia scope natychmiast Ă˘â‚¬â€ť tworzy rekord w review queue.
    Wymaga zatwierdzenia przez approve_scope_promotion.
    """
    if not reason or not reason.strip():
        return {"status": "error", "error": "Pole 'reason' jest wymagane"}
    target_scope = (target_scope or "").strip().lower()
    if target_scope not in _SCOPE_ORDER:
        return {"status": "error", "error": f"Nieznany target_scope: '{target_scope}'. DostĂ„â„˘pne: {', '.join(_SCOPE_ORDER)}"}

    conn = get_db_connection()
    try:
        if not _is_multiuser_feature_active(conn, MULTIUSER_SCOPE_PROMOTION_FLAG):
            return {"status": "disabled", "message": f"Feature flag '{MULTIUSER_SCOPE_PROMOTION_FLAG}' is off."}

        mem = require_memory_row(conn, memory_id)
        memory = row_to_dict(mem)
        current_scope = memory.get("visibility_scope") or "private"

        if current_scope == target_scope:
            return {"status": "noop", "message": f"Wspomnienie juÄąÄ˝ ma scope '{current_scope}'", "memory_id": memory_id}

        if current_scope in _SCOPE_ORDER and target_scope in _SCOPE_ORDER:
            if _SCOPE_ORDER.index(target_scope) <= _SCOPE_ORDER.index(current_scope):
                return {"status": "error", "error": f"Promocja wymaga szerszego scope. Obecny: '{current_scope}', docelowy: '{target_scope}'."}

        # Resolve proposing user
        proposed_by_user_id: int | None = None
        if user_key:
            user_row = conn.execute("SELECT id FROM users WHERE external_user_key = ?", (user_key.strip(),)).fetchone()
            if user_row:
                proposed_by_user_id = int(user_row["id"])

        workspace_id = memory.get("workspace_id")
        project_key = memory.get("project_key")

        # Check for an existing pending proposal for this memory+target
        existing = conn.execute(
            "SELECT id FROM scope_promotion_proposals WHERE memory_id = ? AND target_scope = ? AND status = 'pending'",
            (memory_id, target_scope),
        ).fetchone()
        if existing:
            return {
                "status": "already_pending",
                "message": "Istnieje juÄąÄ˝ oczekujĂ„â€¦cy wniosek dla tego wspomnienia i scope docelowego.",
                "proposal_id": int(existing["id"]),
                "memory_id": memory_id,
            }

        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO scope_promotion_proposals
                (memory_id, proposed_by_user_id, current_scope, target_scope, reason, status, workspace_id, project_key, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (memory_id, proposed_by_user_id, current_scope, target_scope, reason.strip(), workspace_id, project_key, utc_now_iso()),
        )
        proposal_id = int(cursor.lastrowid)
        conn.commit()

        try:
            timeline.record_timeline_event(
                conn,
                event_type="sandman.scope_promotion_proposed",
                memory_id=memory_id,
                summary=f"Scope promotion proposed: {current_scope} Ă˘â€ â€™ {target_scope}",
                details={"proposal_id": proposal_id, "reason": reason.strip()},
                origin="memory_api",
            )
        except Exception:
            pass

        return {
            "status": "created",
            "proposal_id": proposal_id,
            "memory_id": memory_id,
            "current_scope": current_scope,
            "target_scope": target_scope,
            "reason": reason.strip(),
        }
    finally:
        conn.close()


@mcp.tool
def list_scope_promotion_proposals(
    status: str | None = None,
    workspace_key: str | None = None,
    memory_id: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """
    WyÄąâ€şwietla wnioski o promocjĂ„â„˘ scope.
    status: 'pending' | 'approved' | 'rejected' (brak = wszystkie)
    workspace_key: filtruj po workspace
    memory_id: filtruj po konkretnym wspomnieniu
    """
    conn = get_db_connection()
    try:
        sql = "SELECT * FROM scope_promotion_proposals WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status.strip().lower())
        if workspace_key:
            ws_id = _resolve_workspace_id(conn, workspace_key)
            sql += " AND workspace_id = ?"
            params.append(ws_id)
        if memory_id is not None:
            sql += " AND memory_id = ?"
            params.append(int(memory_id))
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        proposals = [row_to_dict(r) for r in rows]
        return {"status": "ok", "count": len(proposals), "proposals": proposals}
    finally:
        conn.close()


@mcp.tool
def approve_scope_promotion(
    proposal_id: int,
    reviewer_user_key: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """
    Zatwierdza wniosek o promocjĂ„â„˘ scope i natychmiast zmienia visibility_scope wspomnienia.
    Prywatne wspomnienia nie mogĂ„â€¦ same awansowaĂ„â€ˇ Ă˘â‚¬â€ť wymagane jest jawne zatwierdzenie.
    """
    conn = get_db_connection()
    try:
        if not _is_multiuser_feature_active(conn, MULTIUSER_SCOPE_PROMOTION_FLAG):
            return {"status": "disabled", "message": f"Feature flag '{MULTIUSER_SCOPE_PROMOTION_FLAG}' is off."}

        proposal_row = conn.execute(
            "SELECT * FROM scope_promotion_proposals WHERE id = ?",
            (int(proposal_id),),
        ).fetchone()
        if proposal_row is None:
            return {"status": "error", "error": f'Wniosek #{proposal_id} nie istnieje'}
        proposal = row_to_dict(proposal_row)

        if proposal["status"] != "pending":
            return {
                "status": "noop",
                "message": f"Wniosek #{proposal_id} ma status '{proposal['status']}' Ă˘â‚¬â€ť nie moÄąÄ˝na zatwierdziĂ„â€ˇ.",
                "proposal": proposal,
            }

        # Resolve reviewer
        reviewed_by_user_id: int | None = None
        if reviewer_user_key:
            user_row = conn.execute("SELECT id FROM users WHERE external_user_key = ?", (reviewer_user_key.strip(),)).fetchone()
            if user_row:
                reviewed_by_user_id = int(user_row["id"])

        memory_id = int(proposal["memory_id"])
        target_scope = proposal["target_scope"]
        now = utc_now_iso()

        # Apply the scope change
        conn.execute(
            "UPDATE memories SET visibility_scope = ?, last_modified_by_user_id = ? WHERE id = ?",
            (target_scope, reviewed_by_user_id, memory_id),
        )

        # Mark proposal approved
        conn.execute(
            "UPDATE scope_promotion_proposals SET status = 'approved', reviewed_at = ?, reviewed_by_user_id = ?, review_note = ? WHERE id = ?",
            (now, reviewed_by_user_id, normalize_optional_text(note), int(proposal_id)),
        )
        conn.commit()

        updated_memory = row_to_dict(conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone())

        try:
            timeline.record_timeline_event(
                conn,
                event_type="sandman.scope_promotion_approved",
                memory_id=memory_id,
                summary=f"Scope promoted: {proposal['current_scope']} Ă˘â€ â€™ {target_scope}",
                details={"proposal_id": proposal_id, "reviewed_by": reviewer_user_key, "note": note},
                origin="memory_api",
            )
        except Exception:
            pass

        return {
            "status": "approved",
            "proposal_id": proposal_id,
            "memory_id": memory_id,
            "old_scope": proposal["current_scope"],
            "new_scope": target_scope,
            "memory": updated_memory,
        }
    finally:
        conn.close()


@mcp.tool
def reject_scope_promotion(
    proposal_id: int,
    reviewer_user_key: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """
    Odrzuca wniosek o promocjĂ„â„˘ scope. Nie zmienia visibility_scope wspomnienia.
    """
    conn = get_db_connection()
    try:
        if not _is_multiuser_feature_active(conn, MULTIUSER_SCOPE_PROMOTION_FLAG):
            return {"status": "disabled", "message": f"Feature flag '{MULTIUSER_SCOPE_PROMOTION_FLAG}' is off."}

        proposal_row = conn.execute(
            "SELECT * FROM scope_promotion_proposals WHERE id = ?",
            (int(proposal_id),),
        ).fetchone()
        if proposal_row is None:
            return {"status": "error", "error": f'Wniosek #{proposal_id} nie istnieje'}
        proposal = row_to_dict(proposal_row)

        if proposal["status"] != "pending":
            return {
                "status": "noop",
                "message": f"Wniosek #{proposal_id} ma status '{proposal['status']}' Ă˘â‚¬â€ť nie moÄąÄ˝na odrzuciĂ„â€ˇ.",
                "proposal": proposal,
            }

        reviewed_by_user_id: int | None = None
        if reviewer_user_key:
            user_row = conn.execute("SELECT id FROM users WHERE external_user_key = ?", (reviewer_user_key.strip(),)).fetchone()
            if user_row:
                reviewed_by_user_id = int(user_row["id"])

        now = utc_now_iso()
        conn.execute(
            "UPDATE scope_promotion_proposals SET status = 'rejected', reviewed_at = ?, reviewed_by_user_id = ?, review_note = ? WHERE id = ?",
            (now, reviewed_by_user_id, normalize_optional_text(note), int(proposal_id)),
        )
        conn.commit()

        try:
            timeline.record_timeline_event(
                conn,
                event_type="sandman.scope_promotion_rejected",
                memory_id=int(proposal["memory_id"]),
                summary=f"Scope promotion rejected: {proposal['current_scope']} Ă˘â€ â€™ {proposal['target_scope']}",
                details={"proposal_id": proposal_id, "reviewed_by": reviewer_user_key, "note": note},
                origin="memory_api",
            )
        except Exception:
            pass

        return {
            "status": "rejected",
            "proposal_id": proposal_id,
            "memory_id": int(proposal["memory_id"]),
            "current_scope": proposal["current_scope"],
            "target_scope": proposal["target_scope"],
        }
    finally:
        conn.close()













# ---------------------------------------------------------------------------
# Research Ingest MVP: quarantine + evidence pipeline
# ---------------------------------------------------------------------------

def _normalize_ingest_status(value: str | None, default: str = "new") -> str:
    return normalize_ingest_status(value, default=default, normalize_optional_text=normalize_optional_text)


def _normalize_source_type(value: str | None) -> str:
    return normalize_source_type(value, normalize_optional_text=normalize_optional_text)


def _normalize_claims_json(extracted_claims_json: str | None, normalized_text: str) -> str | None:
    return normalize_claims_json(extracted_claims_json, normalized_text, normalize_optional_text=normalize_optional_text)


def _row_to_ingest_item(row) -> dict[str, Any]:
    return row_to_ingest_item(row, row_to_dict=row_to_dict)


def _require_ingest_item(conn, ingest_item_id: int) -> dict[str, Any]:
    return require_ingest_item(conn, ingest_item_id, row_to_ingest_item=_row_to_ingest_item)


def _ensure_ingest_source(
    conn,
    source_ref: str | None,
    source_type: str,
    title: str | None,
    reliability_score: float,
    notes: str | None = None,
) -> int | None:
    return ensure_ingest_source(
        conn,
        source_ref,
        source_type,
        title,
        reliability_score,
        notes,
        normalize_optional_text=normalize_optional_text,
        normalize_score=normalize_score,
        utc_now_iso=utc_now_iso,
    )


@mcp.tool
def create_ingest_item(
    raw_text: str,
    source_type: str = "manual",
    source_ref: str | None = None,
    title: str | None = None,
    normalized_text: str | None = None,
    extracted_claims_json: str | None = None,
    project_key: str | None = None,
    tags: str | None = None,
    quality_score: float = 0.5,
    source_reliability_score: float = 0.5,
    ingest_status: str = "new",
) -> dict[str, Any]:
    """Creates a quarantined research ingest item. Does not create normal memory."""
    conn = get_db_connection()
    try:
        return create_ingest_item_payload(
            conn,
            raw_text=raw_text,
            source_type=source_type,
            source_ref=source_ref,
            title=title,
            normalized_text=normalized_text,
            extracted_claims_json=extracted_claims_json,
            project_key=project_key,
            tags=tags,
            quality_score=quality_score,
            source_reliability_score=source_reliability_score,
            ingest_status=ingest_status,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            normalize_score=normalize_score,
            utc_now_iso=utc_now_iso,
            normalize_source_type=_normalize_source_type,
            normalize_ingest_status=_normalize_ingest_status,
            normalize_claims_json=_normalize_claims_json,
            ensure_ingest_source=_ensure_ingest_source,
            require_ingest_item=_require_ingest_item,
        )
    finally:
        conn.close()


@mcp.tool
def list_ingest_queue(
    ingest_status: str | None = None,
    project_key: str | None = None,
    source_type: str | None = None,
    tag: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Lists quarantined research ingest items."""
    conn = get_db_connection()
    try:
        return list_ingest_queue_payload(
            conn,
            ingest_status=ingest_status,
            project_key=project_key,
            source_type=source_type,
            tag=tag,
            limit=limit,
            normalize_optional_text=normalize_optional_text,
            normalize_ingest_status=_normalize_ingest_status,
            normalize_source_type=_normalize_source_type,
            row_to_ingest_item=_row_to_ingest_item,
        )
    finally:
        conn.close()


@mcp.tool
def get_ingest_item(ingest_item_id: int) -> dict[str, Any]:
    """Returns a single quarantined research ingest item."""
    conn = get_db_connection()
    try:
        return get_ingest_item_payload(conn, ingest_item_id=ingest_item_id, require_ingest_item=_require_ingest_item)
    finally:
        conn.close()


@mcp.tool
def reject_ingest_item(ingest_item_id: int, reason: str, reviewed_by: str | None = None) -> dict[str, Any]:
    """Rejects a research ingest item without touching normal memory."""
    conn = get_db_connection()
    try:
        return reject_ingest_item_payload(
            conn,
            ingest_item_id=ingest_item_id,
            reason=reason,
            reviewed_by=reviewed_by,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            require_ingest_item=_require_ingest_item,
        )
    finally:
        conn.close()


@mcp.tool
def archive_ingest_item(ingest_item_id: int, reason: str | None = None, reviewed_by: str | None = None) -> dict[str, Any]:
    """Archives a research ingest item in quarantine."""
    conn = get_db_connection()
    try:
        return archive_ingest_item_payload(
            conn,
            ingest_item_id=ingest_item_id,
            reason=reason,
            reviewed_by=reviewed_by,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            require_ingest_item=_require_ingest_item,
        )
    finally:
        conn.close()


@mcp.tool
def promote_ingest_item(
    ingest_item_id: int,
    memory_content: str,
    memory_type: str = "research_note",
    summary_short: str | None = None,
    tags: str | None = None,
    importance_score: float = 0.5,
    confidence_score: float = 0.6,
    reviewed_by: str | None = None,
) -> dict[str, Any]:
    """Promotes a quarantined ingest item into one concise normal memory."""
    conn = get_db_connection()
    try:
        return promote_ingest_item_payload(
            conn,
            ingest_item_id=ingest_item_id,
            memory_content=memory_content,
            memory_type=memory_type,
            summary_short=summary_short,
            tags=tags,
            importance_score=importance_score,
            confidence_score=confidence_score,
            reviewed_by=reviewed_by,
            normalize_required_text=normalize_required_text,
            normalize_optional_text=normalize_optional_text,
            utc_now_iso=utc_now_iso,
            require_ingest_item=_require_ingest_item,
            insert_memory=_insert_memory,
            ensure_memory_embedding_best_effort=_ensure_memory_embedding_best_effort,
        )
    finally:
        conn.close()


@mcp.tool
def preview_research_ingest_review(project_key: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Heuristic Sandman-style preview for research ingest quarantine review."""
    conn = get_db_connection()
    try:
        return preview_research_ingest_review_payload(
            conn,
            project_key=project_key,
            limit=limit,
            normalize_optional_text=normalize_optional_text,
            row_to_ingest_item=_row_to_ingest_item,
        )
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Retrieval v2 / Context Engine / Memory Steward
# ---------------------------------------------------------------------------


def get_mapi_capabilities(include_debug: bool = False) -> dict[str, Any]:
    """Return the explicit client-visible MCP/workshop capability contract."""
    readiness = get_runtime_readiness(include_debug=bool(include_debug))
    payload = build_mapi_capabilities_payload(runtime_readiness=readiness)
    if include_debug:
        payload["debug"] = {
            "readiness_status": readiness.get("status"),
            "reason_codes": list(readiness.get("reason_codes") or []),
        }
    return payload



@mcp.tool
def get_agent_self_snapshot(
    subject_key: str | None = None,
    display_name: str | None = None,
    project_key: str | None = None,
    include_global: bool = True,
    limit: int = 300,
    include_content: bool = False,
) -> dict[str, Any]:
    """Build a read-only evidence-first self snapshot for a configured agent subject."""
    conn = get_db_connection()
    try:
        return build_agent_self_snapshot_payload(
            conn, subject_key=subject_key, display_name=display_name, project_key=project_key,
            include_global=bool(include_global), limit=int(limit), include_content=bool(include_content), row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_agent_commitment_ledger(
    subject_key: str | None = None,
    display_name: str | None = None,
    project_key: str | None = None,
    include_global: bool = True,
    limit: int = 300,
    include_content: bool = False,
) -> dict[str, Any]:
    """Return explicit commitments and guardrails bound to the configured agent subject."""
    conn = get_db_connection()
    try:
        return build_agent_commitment_ledger_payload(
            conn, subject_key=subject_key, display_name=display_name, project_key=project_key,
            include_global=bool(include_global), limit=int(limit), include_content=bool(include_content), row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_agent_autobiographical_timeline(
    subject_key: str | None = None,
    display_name: str | None = None,
    project_key: str | None = None,
    include_global: bool = True,
    limit: int = 50,
    include_content: bool = False,
) -> dict[str, Any]:
    """Return a bounded chronological timeline derived only from explicit self evidence."""
    conn = get_db_connection()
    try:
        return build_agent_autobiographical_timeline_payload(
            conn, subject_key=subject_key, display_name=display_name, project_key=project_key,
            include_global=bool(include_global), limit=int(limit), include_content=bool(include_content), row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_agent_self_capsule(
    subject_key: str | None = None,
    display_name: str | None = None,
    project_key: str | None = None,
    include_global: bool = True,
    limit: int = 50,
    include_content: bool = False,
) -> dict[str, Any]:
    """Return a compact source-linked self capsule suitable for bootstrap/context composition."""
    conn = get_db_connection()
    try:
        return build_agent_self_capsule_payload(
            conn, subject_key=subject_key, display_name=display_name, project_key=project_key,
            include_global=bool(include_global), limit=int(limit), include_content=bool(include_content), row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_agent_self_delta(
    from_snapshot_json: str,
    to_snapshot_json: str | None = None,
    subject_key: str | None = None,
    display_name: str | None = None,
    project_key: str | None = None,
    include_global: bool = True,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Compare an evidence-first self snapshot to another snapshot or current self state."""
    conn = get_db_connection()
    try:
        return build_agent_self_delta_payload(
            conn, from_snapshot_json=from_snapshot_json, to_snapshot_json=to_snapshot_json,
            subject_key=subject_key, display_name=display_name, project_key=project_key,
            include_global=bool(include_global), include_debug=bool(include_debug), row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_agent_self_narrative(
    provider: str = "deterministic",
    subject_key: str | None = None,
    display_name: str | None = None,
    project_key: str | None = None,
    include_global: bool = True,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Render a controlled source-bound self narrative; providers may select claim IDs only."""
    conn = get_db_connection()
    try:
        return build_agent_self_narrative_payload(
            conn, subject_key=subject_key, display_name=display_name, project_key=project_key,
            include_global=bool(include_global), provider_name=provider, include_debug=bool(include_debug),
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool

def list_memories_page(
    page_size: int = 20,
    cursor: str | None = None,
    project_key: str | None = None,
    project_key_mode: str = "exact",
    scope_code: str | None = None,
    memory_type: str | None = None,
    state_code: str | None = None,
    truth_kind: str | None = None,
    tag: str | None = None,
    include_archived: bool = False,
    compact: bool = False,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Stable keyset-paginated memory inventory with bounded field projection."""
    if int(page_size) < 1 or int(page_size) > 100:
        return {"status": "error", "error": "page_size_out_of_range", "allowed_range": [1, 100], "actual": page_size}
    normalized_mode = str(project_key_mode or "exact").strip().casefold()
    if normalized_mode not in PROJECT_KEY_MODE_VALUES:
        return invalid_choice_payload(field="project_key_mode", actual=project_key_mode, allowed_values=PROJECT_KEY_MODE_VALUES)
    if fields is not None and not isinstance(fields, list):
        return {"status": "error", "error": "fields_must_be_array"}
    try:
        selected_fields = normalize_projection(fields=fields, compact=bool(compact))
    except ValueError as exc:
        error = str(exc)
        payload = {"status": "error", "error": error.split(":", 1)[0], "allowed_fields": list(PROJECTION_FIELDS)}
        if ":" in error:
            payload["field"] = error.split(":", 1)[1]
        return payload
    requested_project_key = normalize_optional_text(project_key)
    conn = get_db_connection()
    try:
        project_key_values, resolved_mode, canonical_project_key = _resolve_project_key_filter(
            conn, project_key=requested_project_key, project_key_mode=normalized_mode
        )
        filters = {
            "requested_project_key": requested_project_key,
            "canonical_project_key": canonical_project_key,
            "project_key_mode": resolved_mode,
            "project_key_values": list(project_key_values or []),
            "scope_code": normalize_scope_code(scope_code),
            "memory_type": normalize_optional_text(memory_type),
            "state_code": normalize_state_code(state_code),
            "truth_kind": normalize_truth_kind(truth_kind),
            "tag": normalize_optional_text(tag),
            "include_archived": bool(include_archived),
        }
        result = list_memory_page(conn, filters=filters, fields=selected_fields, compact=bool(compact), page_size=int(page_size), cursor=normalize_optional_text(cursor))
    finally:
        conn.close()
    result["requested_project_key"] = requested_project_key
    result["canonical_project_key"] = canonical_project_key
    return result


def _load_project_gravity_candidates(project_key: str, limit: int = 200) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 500))
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM memories
            WHERE project_key=? AND archived_at IS NULL
              AND COALESCE(state_code, 'active') NOT IN ('archived','superseded')
            ORDER BY identity_weight DESC, importance_score DESC, recall_count DESC, id DESC
            LIMIT ?
            """,
            (project_key, safe_limit),
        ).fetchall()
        return [enrich_memory_dict(row_to_dict(row)) for row in rows]
    finally:
        conn.close()


@mcp.tool
def get_agent_gravity_preview(
    query: str,
    project_key: str = "demo-project",
    limit: int = 8,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Read-only source-bound resurfacing preview for one project and query."""
    clean_query = normalize_required_text(query, "query")
    requested = normalize_required_text(project_key, "project_key")
    conn = get_db_connection()
    try:
        _values, _mode, canonical = _resolve_project_key_filter(conn, project_key=requested, project_key_mode="exact")
    finally:
        conn.close()
    canonical = canonical or requested
    candidates = _load_project_gravity_candidates(canonical, limit=200)
    self_capsule = get_agent_self_capsule(project_key=None, include_global=True, limit=50, include_content=False)
    self_candidates: list[dict[str, Any]] = []
    for section_name, source_kind in (
        ("identity", "self_capsule"),
        ("preferences", "self_capsule"),
        ("relationships", "self_capsule"),
        ("commitments", "commitment_ledger"),
        ("recent_autobiographical_events", "autobiographical_timeline"),
    ):
        for raw in self_capsule.get(section_name) or []:
            item = dict(raw)
            item["source_kinds"] = sorted(set(list(item.get("source_kinds") or []) + [source_kind]))
            self_candidates.append(item)
    return build_agent_gravity_preview(
        query=clean_query,
        project_key=canonical,
        candidates=[*candidates, *self_candidates],
        max_results=int(limit),
        include_debug=bool(include_debug),
    )


@mcp.tool
def get_agent_gravity_shadow(
    query: str,
    project_key: str = "demo-project",
    baseline_memory_ids_json: str = "[]",
    max_injections: int = 2,
) -> dict[str, Any]:
    """Compare canonical baseline ids with a read-only Gravity-augmented shadow view."""
    try:
        raw_ids = json.loads(str(baseline_memory_ids_json or "[]"))
    except json.JSONDecodeError as exc:
        return {"status": "error", "error": "baseline_memory_ids_json_invalid", "detail": str(exc)}
    if not isinstance(raw_ids, list):
        return {"status": "error", "error": "baseline_memory_ids_json_must_be_array"}
    baseline_ids = []
    for value in raw_ids:
        memory_id = int(value)
        if memory_id > 0 and memory_id not in baseline_ids:
            baseline_ids.append(memory_id)
    preview = get_agent_gravity_preview(query=query, project_key=project_key, limit=12, include_debug=False)
    return build_gravity_shadow_comparison(
        baseline_source_memory_ids=baseline_ids,
        gravity_payload=preview,
        max_injections=max_injections,
    )


@mcp.tool
def hybrid_search_memories(
    query: str,
    project_key: str | None = None,
    limit: int = 10,
    include_gravity: bool = True,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Fuse lexical and semantic retrieval with recency using a deterministic RRF ranker.

    Gravity is intentionally not enabled until the public evidence-bound Gravity
    contract is ported. The result reports that channel as disabled rather than
    silently pretending parity.
    """
    normalized_query = normalize_required_text(query, "query")
    safe_limit = max(1, min(int(limit), 50))
    requested_project_key = normalize_optional_text(project_key)

    conn = get_db_connection()
    try:
        _values, _mode, canonical_project_key = _resolve_project_key_filter(
            conn,
            project_key=requested_project_key,
            project_key_mode="exact",
        )
    finally:
        conn.close()

    candidate_limit = min(100, max(20, safe_limit * 4))
    lexical = find_memories(
        text_query=normalized_query,
        project_key=canonical_project_key,
        project_key_mode="exact",
        limit=candidate_limit,
        include_history=False,
        debug=True,
    )
    semantic = search_semantic(
        normalized_query,
        top_k=candidate_limit,
        project_key=canonical_project_key,
    )

    candidate_ids = sorted(
        {
            *[
                int(item["id"])
                for item in lexical.get("items") or []
                if item.get("id") is not None
            ],
            *[
                int(item["memory_id"])
                for item in semantic.get("results") or []
                if item.get("memory_id") is not None
            ],
        }
    )
    conn = get_db_connection()
    try:
        candidate_items: dict[int, dict[str, Any]] = {}
        if candidate_ids:
            placeholders = ",".join("?" for _ in candidate_ids)
            rows = conn.execute(
                f"SELECT * FROM memories WHERE id IN ({placeholders})",
                candidate_ids,
            ).fetchall()
            for row in rows:
                item = enrich_memory_dict(row_to_dict(row))
                memory_id = int(item.get("id") or 0)
                item_project = normalize_optional_text(item.get("project_key"))
                if memory_id <= 0:
                    continue
                if canonical_project_key is not None and item_project != canonical_project_key:
                    continue
                candidate_items[memory_id] = item
    finally:
        conn.close()

    gravity_block = (
        build_agent_gravity_preview(
            query=normalized_query,
            project_key=canonical_project_key or "",
            candidates=[dict(item, source_kinds=["retrieval_pool"]) for item in candidate_items.values()],
            max_results=8,
            include_debug=bool(include_debug),
        )
        if include_gravity and canonical_project_key
        else {
            "status": "disabled",
            "reason": "disabled_by_caller" if not include_gravity else "project_scope_required",
            "items": [],
            "attractors": [],
            "source_memory_ids": [],
        }
    )
    result = fuse_hybrid_results(
        query=normalized_query,
        requested_project_key=requested_project_key,
        canonical_project_key=canonical_project_key,
        lexical_payload=lexical,
        semantic_payload=semantic,
        candidate_items=candidate_items,
        gravity_block=gravity_block,
        limit=safe_limit,
    )
    if include_debug:
        result["debug"] = {
            "candidate_limit": candidate_limit,
            "lexical_strategy": list((lexical.get("debug") or {}).get("retrieval_strategy") or []),
            "lexical_count": int(lexical.get("count") or 0),
            "semantic_mode": semantic.get("retrieval_mode"),
            "semantic_count": int(semantic.get("results_count") or 0),
            "candidate_ids": candidate_ids,
            "accepted_candidate_ids": sorted(candidate_items),
        }
    return result


@mcp.tool
def build_agent_context(
    intent: str,
    project_key: str | None = None,
    token_budget: int = 2400,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Compose bounded, source-linked context for an explicitly selected project."""
    normalized_project = normalize_optional_text(project_key)
    if normalized_project is None:
        return {
            "status": "blocked",
            "schema": "mapi_context_engine.v1",
            "reason": "project_key_required",
            "requested_project_key": None,
            "project_key": None,
            "active_project_key": None,
            "source_memory_ids": [],
            "read_only": True,
        }
    restore = bootstrap_agent_context(project_key=normalized_project, limit=12)
    self_capsule = get_agent_self_capsule(project_key=None, include_global=True, limit=50, include_content=False)
    restore = dict(restore)
    self_identity = list(self_capsule.get("identity") or [])
    project_core = list(restore.get("core_memories") or [])
    merged_core: list[dict[str, Any]] = []
    seen_core_ids: set[int] = set()
    for raw in [*self_identity, *project_core]:
        item = dict(raw)
        memory_id = int(item.get("id") or 0)
        if memory_id <= 0 or memory_id in seen_core_ids:
            continue
        seen_core_ids.add(memory_id)
        merged_core.append(item)
    restore["core_memories"] = merged_core
    restore["core_identity"] = merged_core
    current_project = restore.get("current_project") or {}
    canonical_project_key = (
        normalize_optional_text(current_project.get("project_key"))
        or normalized_project
    )
    requested_project_key = (
        normalize_optional_text(current_project.get("requested_project_key"))
        or normalized_project
        or canonical_project_key
    )
    retrieval = hybrid_search_memories(
        query=intent,
        project_key=canonical_project_key,
        limit=8,
        include_debug=True,
    )
    commitment_ledger = get_agent_commitment_ledger(
        project_key=None, include_global=True, limit=100, include_content=False
    )
    canonical_ids = sorted({
        *[int(item.get("id") or 0) for item in retrieval.get("items") or [] if int(item.get("id") or 0) > 0],
        *[int(value) for value in restore.get("source_memory_ids") or [] if int(value or 0) > 0],
        *[int(value) for value in self_capsule.get("source_memory_ids") or [] if int(value or 0) > 0],
        *[int(value) for value in commitment_ledger.get("source_memory_ids") or [] if int(value or 0) > 0],
    })
    gravity_preview = get_agent_gravity_preview(
        query=intent, project_key=canonical_project_key, limit=8, include_debug=bool(include_debug)
    )
    gravity_block = build_gravity_context_block(
        gravity_payload=gravity_preview,
        canonical_source_memory_ids=canonical_ids,
        max_items=2,
    )
    result = build_agent_context_payload(
        intent=intent,
        requested_project_key=requested_project_key,
        canonical_project_key=canonical_project_key,
        token_budget=token_budget,
        restore_payload=restore,
        commitment_ledger=commitment_ledger,
        retrieval_payload=retrieval,
        gravity_block=gravity_block,
    )
    if include_debug and result.get("status") == "ok":
        result["debug"] = {
            "retrieval": retrieval.get("debug") or {},
            "bootstrap_policy": restore.get("bootstrap_policy") or {},
            "gravity_preview": gravity_preview.get("debug") or {},
            "self_capsule_status": self_capsule.get("status"),
            "commitment_ledger_status": commitment_ledger.get("status"),
            "deferred_channels": [],
        }
    return result


def _parse_steward_source_ids(source_memory_ids_json: str | None) -> list[int]:
    if not normalize_optional_text(source_memory_ids_json):
        return []
    try:
        parsed = json.loads(str(source_memory_ids_json))
    except json.JSONDecodeError as exc:
        raise ValueError(f"source_memory_ids_json must be valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("source_memory_ids_json must be a JSON array")
    source_ids: list[int] = []
    seen: set[int] = set()
    for value in parsed:
        memory_id = int(value)
        if memory_id <= 0 or memory_id in seen:
            continue
        seen.add(memory_id)
        source_ids.append(memory_id)
    return source_ids


def _resolve_steward_project_key(project_key: str | None) -> tuple[str, str]:
    requested = normalize_optional_text(project_key) or "demo-project"
    conn = get_db_connection()
    try:
        _values, _mode, canonical = _resolve_project_key_filter(
            conn,
            project_key=requested,
            project_key_mode="exact",
        )
    finally:
        conn.close()
    return requested, canonical or requested


@mcp.tool
def preview_memory_steward_before_action(
    intent: str,
    project_key: str = "demo-project",
    token_budget: int = 2400,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Read-only before-action retrieval through the public Context Engine."""
    context = build_agent_context(
        intent=intent,
        project_key=project_key,
        token_budget=int(token_budget),
        include_debug=bool(include_debug),
    )
    return before_action_payload(context=context)


@mcp.tool
def preview_memory_steward_after_action(
    action_summary: str,
    outcome_summary: str,
    durable_delta: str,
    project_key: str = "demo-project",
    conversation_key: str | None = None,
    source_context: str | None = None,
    source_event_ref: str | None = None,
    source_memory_ids_json: str | None = None,
) -> dict[str, Any]:
    """Build a source-evidenced after-action capture proposal without persistence."""
    action = normalize_required_text(action_summary, "action_summary")
    outcome = normalize_required_text(outcome_summary, "outcome_summary")
    delta = normalize_required_text(durable_delta, "durable_delta")
    source_ids = _parse_steward_source_ids(source_memory_ids_json)
    if not source_ids and not normalize_optional_text(source_event_ref):
        return {
            "status": "blocked",
            "schema": "memory_steward.v1",
            "phase": "after_action",
            "reason": "source_evidence_required",
            "safety": {"read_only": True, "memory_mutations_performed": 0, "capture_items_created": 0},
        }
    requested_project_key, canonical_project_key = _resolve_steward_project_key(project_key)
    content = after_action_content(
        action_summary=action,
        outcome_summary=outcome,
        durable_delta=delta,
        source_event_ref=source_event_ref,
    )
    capture = propose_memory_capture(
        content=content,
        project_key=canonical_project_key,
        source_context=source_context or "memory_steward:after_action",
        conversation_key=conversation_key,
        hint="memory_steward_after_action",
    )
    return capture_phase_payload(
        phase="after_action",
        capture_proposal=capture,
        requested_project_key=requested_project_key,
        canonical_project_key=canonical_project_key,
        content=content,
        source_context=source_context or "memory_steward:after_action",
        conversation_key=conversation_key,
        source_event_ref=source_event_ref,
        source_memory_ids=source_ids,
        hint="memory_steward_after_action",
    )


@mcp.tool
def preview_memory_steward_session_close(
    completed_summary: str,
    open_items_summary: str,
    next_step: str,
    project_key: str = "demo-project",
    conversation_key: str | None = None,
    source_context: str | None = None,
    source_event_ref: str | None = None,
    source_memory_ids_json: str | None = None,
) -> dict[str, Any]:
    """Build an explicit session-close checkpoint proposal without persistence."""
    completed = normalize_required_text(completed_summary, "completed_summary")
    open_items = normalize_required_text(open_items_summary, "open_items_summary")
    next_value = normalize_required_text(next_step, "next_step")
    source_ids = _parse_steward_source_ids(source_memory_ids_json)
    if (
        not source_ids
        and not normalize_optional_text(source_event_ref)
        and not normalize_optional_text(conversation_key)
    ):
        return {
            "status": "blocked",
            "schema": "memory_steward.v1",
            "phase": "session_close",
            "reason": "source_evidence_required",
            "safety": {"read_only": True, "memory_mutations_performed": 0, "capture_items_created": 0},
        }
    requested_project_key, canonical_project_key = _resolve_steward_project_key(project_key)
    content = session_close_content(
        completed_summary=completed,
        open_items_summary=open_items,
        next_step=next_value,
        source_event_ref=source_event_ref,
    )
    capture = propose_memory_capture(
        content=content,
        project_key=canonical_project_key,
        source_context=source_context or "memory_steward:session_close",
        conversation_key=conversation_key,
        hint="memory_steward_session_close",
    )
    return capture_phase_payload(
        phase="session_close",
        capture_proposal=capture,
        requested_project_key=requested_project_key,
        canonical_project_key=canonical_project_key,
        content=content,
        source_context=source_context or "memory_steward:session_close",
        conversation_key=conversation_key,
        source_event_ref=source_event_ref,
        source_memory_ids=source_ids,
        hint="memory_steward_session_close",
    )


@mcp.tool
def preview_memory_steward_nightly(
    project_key: str = "demo-project",
    candidate_limit: int = 8,
    proposal_budget: int = 3,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Aggregate existing proposal/review surfaces into one read-only nightly plan."""
    if candidate_limit < 1 or candidate_limit > 20:
        return {"status": "error", "error": "candidate_limit must be in range 1..20"}
    _requested_project_key, canonical_project_key = _resolve_steward_project_key(project_key)
    sandman = preview_sandman_canonical(
        project_key=canonical_project_key,
        scope_code="project",
        candidate_limit=int(candidate_limit),
        proposal_budget=int(proposal_budget),
        include_debug=bool(include_debug),
    )
    retention = preview_project_memory_retention(
        project_key=canonical_project_key,
        limit=int(candidate_limit),
        include_retain=False,
        include_debug=bool(include_debug),
    )
    revalidation = list_revalidation_queue(
        limit=int(candidate_limit),
        project_key=canonical_project_key,
    )
    capture_queue = list_memory_capture_review_items(
        status="pending",
        project_key=canonical_project_key,
        limit=int(candidate_limit),
        include_expired=False,
    )
    consolidation_queue = list_memory_consolidation_proposals(
        status="pending",
        project_key=canonical_project_key,
        limit=int(candidate_limit),
        include_rejected=False,
    )
    return nightly_payload(
        project_key=canonical_project_key,
        sandman=sandman,
        retention=retention,
        revalidation=revalidation,
        capture_queue=capture_queue,
        consolidation_queue=consolidation_queue,
        candidate_limit=int(candidate_limit),
    )


@mcp.tool
def get_memory_relation_contracts(relation: str | None = None) -> dict[str, Any]:
    """Return the canonical evidence-bound memory relation policy."""
    return get_relation_contracts_payload(relation=relation)


@mcp.tool
def preview_memory_relation(
    relation: str,
    from_memory_id: int | None = None,
    to_memory_id: int | None = None,
    project_key: str | None = None,
    evidence_kind: str | None = None,
    evidence_ref: str | None = None,
    reason: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Read-only evidence preview for one canonical memory relation."""
    requested_project_key = normalize_optional_text(project_key)
    canonical_project_key = requested_project_key
    normalized_relation = normalize_relation(relation)
    conn = get_db_connection()
    try:
        if requested_project_key is not None:
            canonical_project_key = resolve_canonical_project_key(
                conn,
                requested_project_key,
                normalize_optional_text=normalize_optional_text,
            )
        if normalized_relation in EVIDENCE_BOUND_RELATIONS:
            if from_memory_id is None or to_memory_id is None:
                result = {
                    "status": "blocked",
                    "schema": "memory_v3_evidence_relation_preview.v1",
                    "relation": normalized_relation,
                    "blocking_reasons": ["from_and_to_memory_id_required"],
                    "safety": {
                        "read_only": True,
                        "mutations_performed": 0,
                        "apply_supported": False,
                    },
                }
            else:
                result = preview_evidence_relation_payload(
                    conn,
                    relation=normalized_relation,
                    from_memory_id=int(from_memory_id),
                    to_memory_id=int(to_memory_id),
                    evidence_kind=evidence_kind,
                    evidence_ref=evidence_ref,
                    reason=reason,
                    project_key=canonical_project_key,
                    include_debug=bool(include_debug),
                    row_to_dict=row_to_dict,
                    canonical_json_hash=_canonical_json_hash,
                )
            contract_payload = get_relation_contracts_payload(relation=normalized_relation)
            if contract_payload.get("relations"):
                result["contract"] = contract_payload["relations"][0]
            result["apply"] = {
                "supported_directly_here": True,
                "route": "memory.relation_apply",
                "eligible": bool((result.get("safety") or {}).get("apply_supported")),
                "blocking_reasons": list(result.get("blocking_reasons") or []),
            }
        else:
            result = preview_relation_payload(
                conn,
                relation=relation,
                from_memory_id=from_memory_id,
                to_memory_id=to_memory_id,
                project_key=canonical_project_key,
                normalize_optional_text=normalize_optional_text,
                row_to_dict=row_to_dict,
            )
    finally:
        conn.close()
    result["requested_project_key"] = requested_project_key
    result["canonical_project_key"] = canonical_project_key
    return result


@mcp.tool
def apply_memory_relation(
    relation: str,
    from_memory_id: int,
    to_memory_id: int,
    evidence_kind: str,
    evidence_ref: str,
    reason: str,
    expected_preview_hash: str,
    applied_by: str,
    confirm_evidence_bound_relation: bool,
    project_key: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Apply supports/derived_from only through an evidence-bound guarded contract."""
    requested_project_key = normalize_optional_text(project_key)
    canonical_project_key = requested_project_key
    conn = get_db_connection()
    try:
        if requested_project_key is not None:
            canonical_project_key = resolve_canonical_project_key(
                conn,
                requested_project_key,
                normalize_optional_text=normalize_optional_text,
            )
        result = apply_evidence_relation_payload(
            conn,
            relation=relation,
            from_memory_id=int(from_memory_id),
            to_memory_id=int(to_memory_id),
            evidence_kind=evidence_kind,
            evidence_ref=evidence_ref,
            reason=reason,
            expected_preview_hash=expected_preview_hash,
            applied_by=applied_by,
            confirm_evidence_bound_relation=bool(confirm_evidence_bound_relation),
            project_key=canonical_project_key,
            include_debug=bool(include_debug),
            row_to_dict=row_to_dict,
            canonical_json_hash=_canonical_json_hash,
            utc_now_iso=utc_now_iso,
            insert_memory_event=insert_memory_event,
        )
    finally:
        conn.close()
    result["requested_project_key"] = requested_project_key
    result["canonical_project_key"] = canonical_project_key
    return result


@mcp.tool
def preview_memory_relation_rollback(
    link_id: int,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Preview rollback for a link created by evidence-bound supports/derived_from apply."""
    conn = get_db_connection()
    try:
        return preview_evidence_relation_rollback_payload(
            conn,
            link_id=int(link_id),
            include_debug=bool(include_debug),
            row_to_dict=row_to_dict,
            canonical_json_hash=_canonical_json_hash,
        )
    finally:
        conn.close()


@mcp.tool
def rollback_memory_relation(
    link_id: int,
    expected_rollback_preview_hash: str,
    rolled_back_by: str,
    notes: str | None = None,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Archive one materialized evidence-bound edge while preserving append-only audit evidence."""
    conn = get_db_connection()
    try:
        return rollback_evidence_relation_payload(
            conn,
            link_id=int(link_id),
            expected_rollback_preview_hash=expected_rollback_preview_hash,
            rolled_back_by=rolled_back_by,
            notes=notes,
            include_debug=bool(include_debug),
            row_to_dict=row_to_dict,
            canonical_json_hash=_canonical_json_hash,
            utc_now_iso=utc_now_iso,
            insert_memory_event=insert_memory_event,
        )
    finally:
        conn.close()


@mcp.tool
def get_canonical_truth_review(
    project_key: str | None = None,
    include_items: bool = True,
    sample_limit: int = 200,
) -> dict[str, Any]:
    """Read-only evidence and consumer-impact review for active legacy truth relations."""
    conn = get_db_connection()
    try:
        return build_canonical_truth_review_payload(
            conn,
            project_key=project_key,
            include_items=bool(include_items),
            sample_limit=int(sample_limit),
            row_to_dict=row_to_dict,
        )
    finally:
        conn.close()


@mcp.tool
def get_legacy_graph_audit(
    project_key: str | None = None,
    include_trusted: bool = False,
    include_candidates: bool = True,
    sample_limit: int = 100,
) -> dict[str, Any]:
    """Read-only audit of active graph debt without rewriting historical edges."""
    conn = get_db_connection()
    try:
        return build_legacy_graph_audit_payload(
            conn,
            project_key=project_key,
            include_trusted=bool(include_trusted),
            include_candidates=bool(include_candidates),
            sample_limit=int(sample_limit),
            row_to_dict=row_to_dict,
            resolve_project_key=lambda db, key: resolve_canonical_project_key(
                db, key, normalize_optional_text=normalize_optional_text
            ),
        )
    finally:
        conn.close()


@mcp.tool
def get_mapi_operations_observability(
    project_key: str | None = "mapi",
    timeout_budget_ms: int = 1500,
    include_debug: bool = False,
) -> dict[str, Any]:
    """Return one bounded read-only operations dashboard for the public MAPI runtime."""
    normalized_project = normalize_optional_text(project_key) or "mapi"
    try:
        return operations_observability_payload(
            project_key=normalized_project,
            timeout_budget_ms=int(timeout_budget_ms),
            include_debug=bool(include_debug),
            get_runtime_readiness=get_runtime_readiness,
            get_transport_status=get_mcp_transport_status,
            get_embedding_stats=get_semantic_embedding_stats,
            get_retrieval_qa=search_qa_report,
            get_provider_observability=get_sandman_provider_observability,
            get_legacy_graph_audit=get_legacy_graph_audit,
        )
    except ValueError as exc:
        if str(exc) == "timeout_budget_ms_out_of_range":
            return {
                "status": "error",
                "error": "timeout_budget_ms_out_of_range",
                "allowed_range": [50, 60000],
                "actual": timeout_budget_ms,
            }
        raise


@mcp.tool
def get_mapi_doctor_report(deep: bool = False) -> dict[str, Any]:
    """Portable read-only runtime/database/backup/network health report."""
    return collect_doctor_report(
        deep=bool(deep),
        qa_provider=(lambda: search_qa_report(limit_per_case=10)) if deep else None,
    )


@mcp.tool
def get_mapi_recovery_plan() -> dict[str, Any]:
    """Read-only recovery plan. Execution is intentionally CLI/operator-only."""
    return build_recovery_plan(doctor_report=get_mapi_doctor_report(deep=False))


@mcp.tool
def get_mcp_transport_status() -> dict[str, Any]:
    """Read-only transport/backpressure/keepalive contract and counters."""
    return transport_status_payload()


# ---------------------------------------------------------------------------
# Vector / Semantic Search
# ---------------------------------------------------------------------------

@mcp.tool
def search_semantic(
    query: str,
    top_k: int = 10,
    project_key: str | None = None,
) -> dict[str, Any]:
    """
    Wyszukuje wspomnienia semantycznie podobne do query uÄąÄ˝ywajĂ„â€¦c embeddingÄ‚Ĺ‚w (all-MiniLM-L6-v2).
    DziaÄąâ€ša niezaleÄąÄ˝nie od find_memories Ă˘â‚¬â€ť nie wymaga podania konkretnych sÄąâ€šÄ‚Ĺ‚w kluczowych.
    Zwraca top_k wynikÄ‚Ĺ‚w posortowanych malejĂ„â€¦co wedÄąâ€šug podobieÄąâ€žstwa (0.0-1.0).
    """
    return search_semantic_payload(query=query, top_k=top_k, project_key=project_key, get_db_connection=get_db_connection)


@mcp.tool
def backfill_semantic_embeddings(project_key: str | None = None) -> dict[str, Any]:
    """
    Generuje embeddingi dla wspomnieÄąâ€ž ktÄ‚Ĺ‚re jeszcze ich nie majĂ„â€¦.
    Uruchamiaj po dodaniu nowych wspomnieÄąâ€ž lub po pierwszej instalacji.
    """
    return backfill_semantic_embeddings_payload(project_key=project_key, get_db_connection=get_db_connection)


@mcp.tool
def get_semantic_embedding_stats() -> dict[str, Any]:
    """Zwraca statystyki pokrycia embeddingÄ‚Ĺ‚w Ă˘â‚¬â€ť ile wspomnieÄąâ€ž ma embeddingi, ile nie."""
    return semantic_embedding_stats_payload(get_db_connection=get_db_connection)

@mcp.tool
def archive_conversation(
    content: str,
    title: str | None = None,
    source: str = "manual",
    project_key: str | None = None,
    workspace_key: str = "default",
    user_key: str = "owner",
    tags: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """
    Archiwizuje peÄąâ€šny transkrypt rozmowy (Claude, ChatGPT, Slack, notatka wÄąâ€šasna itp.).
    Zwraca id i conversation_id nowego rekordu.
    source: 'claude' | 'chatgpt' | 'slack' | 'manual' (domyÄąâ€şlnie 'manual').
    conversation_id: opcjonalne unikalne ID; jeÄąâ€şli brak Ă˘â‚¬â€ť generowane automatycznie.
    """
    conn = get_db_connection()
    try:
        return archive_conversation_payload(
            conn,
            content=content,
            title=title,
            source=source,
            project_key=project_key,
            workspace_key=workspace_key,
            user_key=user_key,
            tags=tags,
            conversation_id=conversation_id,
            conversation_archive=conv_archive,
        )
    finally:
        conn.close()


@mcp.tool
def get_conversation(conversation_id: str) -> dict[str, Any]:
    """Zwraca peÄąâ€šny transkrypt rozmowy po conversation_id."""
    conn = get_db_connection()
    try:
        return get_conversation_payload(conn, conversation_id=conversation_id, conversation_archive=conv_archive)
    finally:
        conn.close()


@mcp.tool
def list_conversations(
    project_key: str | None = None,
    workspace_key: str | None = None,
    user_key: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Listuje zarchiwizowane rozmowy, opcjonalnie filtrujĂ„â€¦c po project_key / workspace_key / user_key.
    Zwraca metadane bez peÄąâ€šnego contentu (title, source, word_count, tags, archived_at).
    """
    conn = get_db_connection()
    try:
        return list_conversations_payload(
            conn,
            project_key=project_key,
            workspace_key=workspace_key,
            user_key=user_key,
            limit=limit,
            offset=offset,
            conversation_archive=conv_archive,
        )
    finally:
        conn.close()


@mcp.tool
def search_verbatim(
    query: str,
    scope: str = "all",
    project_key: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """
    Wyszukuje dokÄąâ€šadne dopasowania tekstu (verbatim, FTS5) w wspomnieniach i/lub archiwum rozmÄ‚Ĺ‚w.
    scope: 'memories' | 'conversations' | 'all' (domyÄąâ€şlnie 'all').
    Zwraca snippety z zaznaczonym trafieniem (w nawiasach kwadratowych).
    """
    conn = get_db_connection()
    try:
        return search_verbatim_payload(
            conn,
            query=query,
            scope=scope,
            project_key=project_key,
            limit=limit,
            conversation_archive=conv_archive,
        )
    finally:
        conn.close()



@mcp.tool
def reconstruct_day(
    date: str,
    timezone: str = "Europe/Warsaw",
    project_key: str | None = None,
    limit: int = 200,
    include_content: bool = True,
) -> dict[str, Any]:
    """Reconstruct one local calendar day from durable MAPI evidence.

    Checks memories, exact ISO-date mentions, conversation archives and timeline
    events. Coverage explicitly controls whether a no-data claim is justified for
    the checked first-party sources.
    """
    conn = get_db_connection()
    try:
        return reconstruct_day_payload(
            conn,
            date=date,
            timezone_name=timezone,
            project_key=project_key,
            limit=limit,
            include_content=include_content,
        )
    finally:
        conn.close()


# Bind only handlers declared by workshop packages.
bind_workshop_handlers(sys.modules[__name__], replace=True, strict=False)

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8015, path="/mcp/")
