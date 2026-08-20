from __future__ import annotations

from typing import Any

from .manifest import WORKSHOP

TOOL_NAMES = tuple(action.tool_name for action in WORKSHOP.actions)

def bind_handlers(provider: Any) -> dict[str, Any]:
    handlers: dict[str, Any] = {}
    for tool_name in TOOL_NAMES:
        handler = getattr(provider, tool_name, None)
        if callable(handler):
            handlers[tool_name] = handler
    return handlers
