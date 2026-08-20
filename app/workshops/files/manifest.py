from __future__ import annotations

from app.workshops.contracts import Workshop, WorkshopAction

WORKSHOP = Workshop(
    area="files",
    purpose="Project-bound UTF-8 file reads and guarded writes.",
    min_profile="reader",
    risk="medium",
    recommended_first_action="list_file_roots",
    actions=(
        WorkshopAction(action="list_file_roots", tool_name="list_project_file_roots", purpose="List configured project file roots.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null"}),
        WorkshopAction(action="list_directory", tool_name="list_project_directory", purpose="List one bounded directory.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null", "root_id": "str", "relative_path": "str", "limit": "int"}),
        WorkshopAction(action="read_file_text", tool_name="read_project_file_text", purpose="Read one bounded UTF-8 text file.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null", "root_id": "str", "relative_path": "str"}),
        WorkshopAction(action="preview_file_write", tool_name="preview_project_file_write", purpose="Preview a guarded file write.", min_profile="agent", risk="medium", payload_schema={"project_key": "str|null", "root_id": "str", "relative_path": "str", "content": "str"}),
        WorkshopAction(action="apply_file_write", tool_name="apply_project_file_write", purpose="Apply an exact previewed file write.", min_profile="maintainer", risk="high", payload_schema={"project_key": "str|null", "root_id": "str", "relative_path": "str", "content": "str", "expected_preview_hash": "str", "confirmed": "bool"}),
        WorkshopAction(action="list_file_operations", tool_name="list_project_file_operations", purpose="List guarded file operation audit rows.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null", "status": "str|null", "limit": "int"}),
        WorkshopAction(action="preview_file_rollback", tool_name="preview_project_file_rollback", purpose="Preview rollback of a guarded file write.", min_profile="agent", risk="medium", payload_schema={"project_key": "str|null", "operation_id": "int"}),
        WorkshopAction(action="rollback_file_write", tool_name="rollback_project_file_write", purpose="Rollback an exact file operation preview.", min_profile="maintainer", risk="high", payload_schema={"project_key": "str|null", "operation_id": "int", "expected_preview_hash": "str", "confirmed": "bool", "rollback_note": "str|null"}),
    ),
    guardrails=("Roots and write permissions are operator-configured; mutations require preview hash plus confirmation.",),
)
