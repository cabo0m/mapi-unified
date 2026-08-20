from __future__ import annotations

"""Canonical role, capability and risk policy for MAPI workshop actions."""

from dataclasses import dataclass, replace
from typing import Iterable

from app.workshops.contracts import Workshop, WorkshopAction

SURFACE_PROFILES = (
    "reader",
    "agent",
    "maintainer",
    "admin",
)

SURFACE_PROFILE_ALIASES = {
    "public": "reader",
    "operator": "maintainer",
    "debug": "maintainer",
}

ACCESS_REQUIREMENTS = frozenset(
    {
        "reader",
        "agent",
        "maintainer",
        "admin",
    }
)

PROFILE_ALLOWED_REQUIREMENTS = {
    "reader": frozenset({"reader"}),
    "agent": frozenset({"reader", "agent"}),
    "maintainer": frozenset({"reader", "agent", "maintainer"}),
    "admin": ACCESS_REQUIREMENTS,
}

RISK_CLASSES = frozenset({"R0", "R1", "R2", "R3"})

PUBLIC_WORKSHOP_PURPOSES = {
    "memory": "Create, retrieve, relate, and govern durable memories.",
    "timeline": "Inspect project, memory, and conversation history.",
    "conflicts": "Detect conflicts and record guarded review decisions.",
    "governance": "Inspect quality, queues, ownership, and operational health.",
    "owner_catalog": "Manage responsibility metadata and owner catalogue health.",
    "feature_flags": "Inspect and control feature rollout configuration.",
    "research_ingest": "Quarantine, review, and promote external research.",
    "semantic": "Use optional vector retrieval and inspect embedding status.",
    "sandman": "Run deterministic and proposal-only memory maintenance.",
    "memory_linking": "Preview and run deterministic relationship discovery.",
    "gemma": "Use optional local-model worker capabilities.",
    "files": "Use project-bound file reads and guarded writes.",
    "git": "Inspect project Git state and use guarded stage/commit operations.",
    "commands": "Run operator-approved fixed command recipes.",
    "admin": "Perform dangerous local database, file, and process operations.",
}

PROTECTED_LIFECYCLE_TOOLS = frozenset(
    {
        "apply_memory_pointer_lifecycle_remediation_execution",
        "rollback_memory_pointer_lifecycle_remediation_execution",
        "apply_memory_lifecycle_remediation",
        "rollback_memory_lifecycle_remediation",
        "create_memory_direct_confirmed",
        "admin_memory_write",
        "create_memory",
        "apply_memory_hygiene",
        "rollback_memory_hygiene_run",
        "apply_memory_provenance_backfill",

    }
)

MAINTENANCE_TOOLS = frozenset(
    {
        "approve_memory_consolidation_proposal",
        "reject_memory_consolidation_proposal",
        "apply_approved_memory_consolidation_proposal",
        "rollback_memory_consolidation_apply_run",
        "apply_memory_supersession",
        "rollback_memory_supersession_run",
        "apply_memory_relation",
        "rollback_memory_relation",
        "review_memory_capture_item",
        "expire_memory_capture_item",
        "apply_memory_capture_reconciliation",
        "save_memory_capture_proposal",
        "save_memory_retention_review",
        "decide_memory_retention_review",
        "apply_memory_retention_review",
        "apply_memory_retention_batch",
        "rollback_memory_retention_review",
        "create_memory_from_proposal",
        "upsert_project_key_alias",
        "record_conflict_decision",
        "promote_ingest_item",
        "reject_ingest_item",
        "run_memory_linking_pass",
        "run_sandman_gemini_shadow",
        "run_sandman_model_queue_canary",
        "run_sandman_v1",
        "run_sandman_ai",
        "confirm_memory_self_healing_resolution",
        "apply_project_file_write",
        "rollback_project_file_write",
        "apply_project_git_stage",
        "rollback_project_git_stage",
        "apply_project_git_commit",
        "rollback_project_git_commit",
        "run_project_command_recipe",
    }
)

MAINTAINER_ONLY_TOOLS = frozenset(
    {
        "gemma_worker_create_job",
        "gemma_worker_prepare_plan",
        "gemma_worker_approve_job",
        "gemma_worker_reject_job",
        "gemma_worker_cancel_job",
        "gemma_worker_run_job",
        "gemma_worker_run_task",
        "gemma_worker_prepare_task",
        "gemma_worker_report",
        "gemma_lms_load",
        "gemma_lms_unload",
        "gemma_ask",
        "gemma_coding_task",
    }
)

AGENT_TOOLS = frozenset({"save_memory"})

OPERATOR_WRITE_TOOLS = frozenset(
    {
        "preview_project_file_write",
        "preview_project_file_rollback",
        "preview_project_git_stage",
        "preview_project_git_stage_rollback",
        "preview_project_git_commit",
        "preview_project_git_commit_rollback",
        "preview_project_command_recipe",
        "recall_memory",
        "archive_conversation",
        "propose_memory",
        "create_ingest_item",
        "propose_memory_self_healing_resolution",
    }
)

BACKUP_REQUIRED_TOOLS = frozenset(
    {
        "apply_memory_pointer_lifecycle_remediation_execution",
        "rollback_memory_pointer_lifecycle_remediation_execution",
        "apply_memory_lifecycle_remediation",
        "rollback_memory_lifecycle_remediation",
        "apply_memory_retention_batch",
        "apply_memory_hygiene",
        "rollback_memory_hygiene_run",
        "apply_memory_provenance_backfill",
        "confirm_memory_self_healing_resolution",
    }
)


@dataclass(frozen=True)
class ActionAccessPolicy:
    requirement: str
    risk_class: str
    backup_required: bool = False


def canonical_profile_token(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    return SURFACE_PROFILE_ALIASES.get(raw, raw)


def canonical_requirement_token(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"public", "reader"}:
        return "reader"
    if raw in {"operator", "maintainer"}:
        return "maintainer"
    return raw


def profile_allows_requirement(profile: str, requirement: str | None) -> bool:
    required = canonical_requirement_token(requirement)
    return required in PROFILE_ALLOWED_REQUIREMENTS.get(profile, frozenset())


def action_access_policy(area: str, action: WorkshopAction) -> ActionAccessPolicy:
    tool_name = action.tool_name
    if area == "admin" or tool_name in PROTECTED_LIFECYCLE_TOOLS:
        return ActionAccessPolicy("admin", "R3", tool_name in BACKUP_REQUIRED_TOOLS)
    if tool_name in MAINTENANCE_TOOLS:
        return ActionAccessPolicy("maintainer", "R2", tool_name in BACKUP_REQUIRED_TOOLS)
    if area == "sandman":
        return ActionAccessPolicy("agent", "R0")
    if tool_name in MAINTAINER_ONLY_TOOLS:
        return ActionAccessPolicy("maintainer", "R1")
    if tool_name in AGENT_TOOLS:
        return ActionAccessPolicy("agent", "R1")
    if tool_name in OPERATOR_WRITE_TOOLS:
        return ActionAccessPolicy("agent", "R1")
    return ActionAccessPolicy("reader", "R0")


def _public_action_purpose(action: WorkshopAction) -> str:
    return action.action.replace("_", " ").strip().capitalize() + "."


def apply_workshop_access_policy(workshop: Workshop) -> Workshop:
    workshop_requirement = "admin" if workshop.area == "admin" else (
        "maintainer" if workshop.area == "memory_linking" else (
            "agent" if workshop.area == "sandman" else "reader"
        )
    )
    actions = tuple(
        replace(
            action,
            min_profile=policy.requirement,
            risk_class=policy.risk_class,
            backup_required=policy.backup_required,
            purpose=_public_action_purpose(action),
        )
        for action in workshop.actions
        for policy in (action_access_policy(workshop.area, action),)
    )
    return replace(
        workshop,
        purpose=PUBLIC_WORKSHOP_PURPOSES.get(
            workshop.area,
            workshop.area.replace("_", " ").strip().capitalize() + ".",
        ),
        min_profile=workshop_requirement,
        actions=actions,
        guardrails=(
            "Enforce the declared profile, risk class, and documented operational controls.",
        )
        if workshop.guardrails
        else (),
    )


def permission_matrix(workshops: Iterable[Workshop]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for workshop in workshops:
        for action in workshop.actions:
            rows.append(
                {
                    "area": workshop.area,
                    "action": action.action,
                    "tool_name": action.tool_name,
                    "requirement": action.min_profile,
                    "risk": action.risk,
                    "risk_class": action.risk_class,
                    "backup_required": action.backup_required,
                }
            )
    return rows
