from __future__ import annotations

from app.workshops.contracts import Workshop, WorkshopAction

WORKSHOP = Workshop(
    area="git",
    purpose="Project-bound Git inspection, guarded staging and guarded local commits.",
    min_profile="reader",
    risk="medium",
    recommended_first_action="git_status",
    actions=(
        WorkshopAction(action="list_git_repositories", tool_name="list_project_git_repositories", purpose="List configured project repositories.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null"}),
        WorkshopAction(action="git_info", tool_name="project_git_info", purpose="Read repository identity.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null", "repo_id": "str"}),
        WorkshopAction(action="git_status", tool_name="project_git_status", purpose="Read Git status.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null", "repo_id": "str"}),
        WorkshopAction(action="git_diff", tool_name="project_git_diff", purpose="Read bounded Git diff.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null", "repo_id": "str", "staged": "bool"}),
        WorkshopAction(action="git_log", tool_name="project_git_log", purpose="Read bounded Git log.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null", "repo_id": "str", "limit": "int"}),
        WorkshopAction(action="preview_git_stage", tool_name="preview_project_git_stage", purpose="Preview staging tracked text files.", min_profile="agent", risk="medium", payload_schema={"project_key": "str|null", "repo_id": "str", "paths": "list[str]"}),
        WorkshopAction(action="apply_git_stage", tool_name="apply_project_git_stage", purpose="Apply an exact staging preview.", min_profile="maintainer", risk="high", payload_schema={"project_key": "str|null", "repo_id": "str", "paths": "list[str]", "expected_preview_hash": "str", "confirmed": "bool"}),
        WorkshopAction(action="list_git_stage_operations", tool_name="list_project_git_stage_operations", purpose="List staging audit rows.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null", "status": "str|null", "limit": "int"}),
        WorkshopAction(action="preview_git_stage_rollback", tool_name="preview_project_git_stage_rollback", purpose="Preview staged-index rollback.", min_profile="agent", risk="medium", payload_schema={"project_key": "str|null", "operation_id": "int"}),
        WorkshopAction(action="rollback_git_stage", tool_name="rollback_project_git_stage", purpose="Rollback an exact staging operation preview.", min_profile="maintainer", risk="high", payload_schema={"project_key": "str|null", "operation_id": "int", "expected_preview_hash": "str", "confirmed": "bool", "rollback_note": "str|null"}),
        WorkshopAction(action="preview_git_commit", tool_name="preview_project_git_commit", purpose="Preview local commit of exact staged index.", min_profile="agent", risk="medium", payload_schema={"project_key": "str|null", "repo_id": "str", "message": "str"}),
        WorkshopAction(action="apply_git_commit", tool_name="apply_project_git_commit", purpose="Create an exact previewed local commit.", min_profile="maintainer", risk="high", payload_schema={"project_key": "str|null", "repo_id": "str", "message": "str", "expected_preview_hash": "str", "confirmed": "bool"}),
        WorkshopAction(action="list_git_commit_operations", tool_name="list_project_git_commit_operations", purpose="List local commit audit rows.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null", "status": "str|null", "limit": "int"}),
        WorkshopAction(action="preview_git_commit_rollback", tool_name="preview_project_git_commit_rollback", purpose="Preview local commit rollback.", min_profile="agent", risk="medium", payload_schema={"project_key": "str|null", "operation_id": "int"}),
        WorkshopAction(action="rollback_git_commit", tool_name="rollback_project_git_commit", purpose="Rollback an exact local commit operation preview.", min_profile="maintainer", risk="high", payload_schema={"project_key": "str|null", "operation_id": "int", "expected_preview_hash": "str", "confirmed": "bool", "rollback_note": "str|null"}),
    ),
    guardrails=("Repositories are operator-configured; staging and commits require project binding, exact preview hash and confirmation.",),
)
