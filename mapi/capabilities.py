from __future__ import annotations

from pathlib import Path

from app.workshops.catalog import WORKSHOPS


WRITE_PREFIXES = (
    "apply",
    "approve",
    "archive",
    "bulk",
    "cancel",
    "create",
    "decide",
    "delete",
    "demote",
    "expire",
    "link",
    "move",
    "promote",
    "propose",
    "recall",
    "record",
    "reject",
    "rollback",
    "run",
    "save",
    "set",
    "start",
    "undo",
    "update",
    "upsert",
    "write",
)

WORKSHOP_PURPOSES = {
    "memory": "Create, retrieve, relate and govern durable memories.",
    "timeline": "Inspect project, memory and conversation history.",
    "conflicts": "Detect conflicts and record guarded review decisions.",
    "governance": "Inspect quality, queues, ownership and operational health.",
    "owner_catalog": "Manage responsibility metadata and owner catalogue health.",
    "feature_flags": "Inspect and control feature rollout configuration.",
    "research_ingest": "Quarantine, review and promote external research.",
    "semantic": "Use optional vector-based retrieval and embedding status.",
    "sandman": "Run deterministic and proposal-only memory maintenance.",
    "memory_linking": "Preview and run deterministic relationship discovery.",
    "gemma": "Use optional local-model worker capabilities.",
    "files": "Use project-bound file reads and guarded writes.",
    "git": "Inspect project Git state and use guarded stage/commit operations.",
    "commands": "Run operator-approved fixed command recipes.",
    "admin": "Perform dangerous local database, file and process operations.",
}


def _humanize(value: str) -> str:
    return value.replace("_", " ").strip().capitalize() + "."


def _is_write(action_name: str, tool_name: str) -> bool:
    tokens = (action_name.lower(), tool_name.lower())
    return any(value.startswith(WRITE_PREFIXES) for value in tokens)


def _uses_external_model(area: str, tool_name: str) -> bool:
    value = f"{area}:{tool_name}".lower()
    return any(token in value for token in ("gemini", "gemma", "model"))


def render_capabilities() -> str:
    lines = [
        "# MAPI capability catalogue",
        "",
        "This file is generated from the public workshop registry. Run `mapi-capabilities` after registry changes.",
        "",
    ]
    for workshop in WORKSHOPS.values():
        action_names = {action.action for action in workshop.actions}
        tool_names = {action.tool_name for action in workshop.actions}
        lines.extend(
            [
                f"## `{workshop.area}`",
                "",
                f"- Purpose: {WORKSHOP_PURPOSES.get(workshop.area, _humanize(workshop.area))}",
                f"- Workshop risk: {workshop.risk}",
                f"- Minimum profile: `{workshop.min_profile}`",
                f"- Recommended first action: `{workshop.recommended_first_action}`",
                "",
                "| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for action in workshop.actions:
            write = _is_write(action.action, action.tool_name)
            preview = action.action.startswith("preview") or any(
                candidate in action_names or candidate in tool_names
                for candidate in (f"preview_{action.action}", f"preview_{action.tool_name}")
            )
            rollback = action.action.startswith(("rollback", "undo")) or any(
                candidate in action_names or candidate in tool_names
                for candidate in (f"rollback_{action.action}", f"rollback_{action.tool_name}")
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{action.action}`",
                        _humanize(action.action),
                        f"`{action.tool_name}`",
                        f"`{action.min_profile}`",
                        f"`{action.risk_class}`",
                        "write" if write else "read",
                        "yes" if _uses_external_model(workshop.area, action.tool_name) else "no",
                        "yes" if write else "no",
                        "yes" if preview else "no",
                        "yes" if rollback else "no",
                    ]
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    destination = root / "docs" / "CAPABILITIES.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_capabilities(), encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    main()
