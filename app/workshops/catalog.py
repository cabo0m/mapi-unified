from __future__ import annotations

from app.workshops import memory
from app.workshops import sandman
from app.workshops import timeline
from app.workshops import conflicts
from app.workshops import governance
from app.workshops import owner_catalog
from app.workshops import feature_flags
from app.workshops import research_ingest
from app.workshops import semantic
from app.workshops import gemma
from app.workshops import memory_linking
from app.workshops import files
from app.workshops import git
from app.workshops import commands
from app.workshops import admin
from app.workshops.access_policy import apply_workshop_access_policy
from app.workshops.contracts import Workshop

WORKSHOP_PACKAGES = (
    memory,
    sandman,
    timeline,
    conflicts,
    governance,
    owner_catalog,
    feature_flags,
    research_ingest,
    semantic,
    gemma,
    memory_linking,
    files,
    git,
    commands,
    admin,
)

WORKSHOPS: dict[str, Workshop] = {}
_tool_owners: dict[str, str] = {}
for package in WORKSHOP_PACKAGES:
    workshop = apply_workshop_access_policy(package.WORKSHOP)
    if workshop.area in WORKSHOPS:
        raise RuntimeError(f"Duplicate workshop area: {workshop.area}")
    WORKSHOPS[workshop.area] = workshop
    action_names: set[str] = set()
    for action in workshop.actions:
        if action.action in action_names:
            raise RuntimeError(f"Duplicate action '{action.action}' in workshop '{workshop.area}'")
        action_names.add(action.action)
        previous_owner = _tool_owners.get(action.tool_name)
        if previous_owner is not None:
            raise RuntimeError(
                f"Workshop tool '{action.tool_name}' is owned by both '{previous_owner}' and '{workshop.area}'"
            )
        _tool_owners[action.tool_name] = workshop.area

WORKSHOP_TOOL_OWNERS = dict(_tool_owners)
