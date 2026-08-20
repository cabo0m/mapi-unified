from __future__ import annotations

from app.workshops.catalog import WORKSHOPS
from app.workshops.runtime_registry import clear_workshop_handlers_for_tests, bind_workshop_handlers, validate_workshop_handler_registry
from app.runtime import capability_tools


def _actions(area: str):
    return {action.action: action for action in WORKSHOPS[area].actions}


def test_common_workshops_exist_with_guarded_risk_classes() -> None:
    assert {"files", "git", "commands"}.issubset(WORKSHOPS)
    files = _actions("files")
    git = _actions("git")
    commands = _actions("commands")
    assert files["read_file_text"].min_profile == "reader"
    assert files["preview_file_write"].min_profile == "agent"
    assert files["apply_file_write"].min_profile == "maintainer"
    assert files["apply_file_write"].risk_class == "R2"
    assert git["preview_git_commit"].min_profile == "agent"
    assert git["apply_git_commit"].min_profile == "maintainer"
    assert git["rollback_git_stage"].risk_class == "R2"
    assert commands["preview_command_recipe"].min_profile == "agent"
    assert commands["run_command_recipe"].min_profile == "maintainer"
    assert commands["run_command_recipe"].risk_class == "R2"


def test_capability_runtime_provider_binds_all_owned_handlers() -> None:
    clear_workshop_handlers_for_tests()
    result = bind_workshop_handlers(capability_tools, replace=True, strict=False, local_only=True)
    expected = sum(len(WORKSHOPS[area].actions) for area in ("files", "git", "commands"))
    assert result["bound_count"] == expected
    snapshot = validate_workshop_handler_registry()
    for area in ("files", "git", "commands"):
        for action in WORKSHOPS[area].actions:
            assert action.tool_name not in snapshot["unresolved"]
