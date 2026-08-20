from __future__ import annotations

import inspect

import server_core  # noqa: F401 - import binds the authoritative core handlers

from app.workshops.catalog import WORKSHOPS, WORKSHOP_PACKAGES, WORKSHOP_TOOL_OWNERS
from app.workshops.runner import run_workshop_action_payload
from app.workshops.runtime_registry import (
    get_workshop_handler,
    validate_workshop_handler_registry,
    workshop_handler_sources,
)

EXPECTED_AREAS = (
    "memory",
    "sandman",
    "timeline",
    "conflicts",
    "governance",
    "owner_catalog",
    "feature_flags",
    "research_ingest",
    "semantic",
    "gemma",
    "memory_linking",
    "files",
    "git",
    "commands",
    "admin",
)


def test_each_workshop_has_one_package_manifest_and_handler_list() -> None:
    assert tuple(WORKSHOPS) == EXPECTED_AREAS
    assert len(WORKSHOP_PACKAGES) == len(EXPECTED_AREAS)
    assert sum(len(workshop.actions) for workshop in WORKSHOPS.values()) == len(WORKSHOP_TOOL_OWNERS)

    for package in WORKSHOP_PACKAGES:
        workshop = package.WORKSHOP
        resolved = WORKSHOPS[workshop.area]
        assert package.TOOL_NAMES == tuple(action.tool_name for action in resolved.actions)
        assert tuple(action.action for action in resolved.actions) == tuple(action.action for action in workshop.actions)
        assert len(package.TOOL_NAMES) == len(set(package.TOOL_NAMES))
        assert len({action.action for action in resolved.actions}) == len(resolved.actions)
        assert all(WORKSHOP_TOOL_OWNERS[name] == resolved.area for name in package.TOOL_NAMES)


def test_runtime_registry_is_complete_and_contains_only_workshop_handlers() -> None:
    import server  # noqa: F401,PLC0415 - final composition owns completeness

    report = validate_workshop_handler_registry()
    assert report == {
        "expected_count": len(WORKSHOP_TOOL_OWNERS),
        "bound_count": len(WORKSHOP_TOOL_OWNERS),
        "unresolved": [],
        "extra": [],
        "complete": True,
    }
    assert callable(get_workshop_handler("find_memories"))
    assert get_workshop_handler("not_a_real_workshop_tool") is None


def test_runner_uses_canonical_registry_instead_of_server_globals() -> None:
    signature = inspect.signature(run_workshop_action_payload)
    assert "handlers" not in signature.parameters


def test_server_module_rebinds_owned_runtime_handlers() -> None:
    import server  # noqa: F401,PLC0415 - verifies the final composition module binding

    sources = dict(workshop_handler_sources())
    assert sources["recall_memory"] == "app.runtime.server_runtime"
    assert sources["get_sandman_canonical_status"] == "server_core"
    assert "run_sandman_v1" not in sources
    assert sources["run_powershell"] == "app.runtime.admin_tools"
    assert sources["run_pytest"] == "app.runtime.admin_tools"
    assert sources["git_push"] == "app.runtime.admin_tools"
    assert sources["find_memories"] == "server_core"
    for tool_name in (
        "get_memory_hygiene_inventory",
        "preview_memory_hygiene",
        "apply_memory_hygiene",
        "get_memory_hygiene_run",
        "preview_memory_hygiene_rollback",
        "rollback_memory_hygiene_run",
    ):
        assert sources[tool_name] == "server_core"
    assert set(sources.values()) <= {
        "server_core",
        "app.runtime.server_runtime",
        "app.runtime.timeline_tools",
        "app.runtime.admin_tools",
        "app.runtime.capability_tools",
        "app.runtime.freshness",
        "app.runtime.private_mode",
    }
