from __future__ import annotations

"""Resolve trustworthy provenance for memory writes.

Explicit caller provenance always wins. When a memory write is executed through
FastMCP, the transport session/request identifiers are durable evidence and can
be used without inventing a conversation identity. Outside an MCP request this
helper deliberately returns None for missing values; lower layers may attach an
internal creation-event reference, but must not fabricate a conversation key.
"""

from typing import Any, Callable


def _active_fastmcp_context() -> Any | None:
    try:
        from fastmcp.server.dependencies import get_context

        return get_context()
    except (ImportError, RuntimeError):
        return None


def resolve_write_provenance(
    *,
    conversation_key: str | None,
    source_event_ref: str | None,
    normalize_optional_text: Callable[[Any], str | None],
) -> dict[str, Any]:
    resolved_conversation = normalize_optional_text(conversation_key)
    resolved_event = normalize_optional_text(source_event_ref)
    origins: list[str] = []

    if resolved_conversation:
        origins.append("explicit_conversation_key")
    if resolved_event:
        origins.append("explicit_source_event_ref")

    if resolved_conversation and resolved_event:
        return {
            "conversation_key": resolved_conversation,
            "source_event_ref": resolved_event,
            "origins": origins,
            "transport_session_id": None,
            "transport_request_id": None,
        }

    transport_session_id: str | None = None
    transport_request_id: str | None = None
    context = _active_fastmcp_context()
    if context is not None:
        try:
            transport_session_id = normalize_optional_text(context.session_id)
        except (RuntimeError, AttributeError):
            transport_session_id = None
        try:
            transport_request_id = normalize_optional_text(context.origin_request_id)
            if not transport_request_id and getattr(context, "request_context", None) is not None:
                transport_request_id = normalize_optional_text(context.request_id)
        except (RuntimeError, AttributeError):
            transport_request_id = None

    if not resolved_conversation and transport_session_id:
        resolved_conversation = f"mcp-session:{transport_session_id}"
        origins.append("mcp_session")
    if not resolved_event and transport_request_id:
        if transport_session_id:
            resolved_event = f"mcp-request:{transport_session_id}:{transport_request_id}"
        else:
            resolved_event = f"mcp-request:{transport_request_id}"
        origins.append("mcp_request")

    return {
        "conversation_key": resolved_conversation,
        "source_event_ref": resolved_event,
        "origins": origins,
        "transport_session_id": transport_session_id,
        "transport_request_id": transport_request_id,
    }
