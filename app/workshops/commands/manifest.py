from __future__ import annotations

from app.workshops.contracts import Workshop, WorkshopAction

WORKSHOP = Workshop(
    area="commands",
    purpose="Operator-approved fixed command recipes only.",
    min_profile="reader",
    risk="medium",
    recommended_first_action="list_command_recipes",
    actions=(
        WorkshopAction(action="list_command_recipes", tool_name="list_project_command_recipes", purpose="List pre-approved command recipes for a project.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null"}),
        WorkshopAction(action="preview_command_recipe", tool_name="preview_project_command_recipe", purpose="Preview one fixed command recipe.", min_profile="agent", risk="medium", payload_schema={"project_key": "str|null", "recipe_id": "str"}),
        WorkshopAction(action="run_command_recipe", tool_name="run_project_command_recipe", purpose="Execute an exact previewed fixed recipe.", min_profile="maintainer", risk="high", payload_schema={"project_key": "str|null", "recipe_id": "str", "expected_preview_hash": "str", "confirmed": "bool"}),
        WorkshopAction(action="list_command_runs", tool_name="list_project_command_runs", purpose="List command execution audit rows.", min_profile="reader", risk="low", payload_schema={"project_key": "str|null", "recipe_id": "str|null", "limit": "int"}),
    ),
    guardrails=("No caller-supplied argv or shell text; commands are fixed operator-approved recipes and execution is audited before process start.",),
)
