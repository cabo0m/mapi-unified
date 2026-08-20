from __future__ import annotations

from typing import Any

from app.runtime.remote_auth_contract import (
    REMOTE_ALLOWED_PROFILES,
    REMOTE_AUTH_OWNER_KEY,
    REMOTE_FORBIDDEN_PROFILES,
)


def access_token_actor(token: Any | None = None) -> dict[str, Any] | None:
    active = token
    if active is None:
        try:
            from fastmcp.server.dependencies import get_access_token

            active = get_access_token()
        except (ImportError, ModuleNotFoundError, RuntimeError):
            active = None
    if active is None:
        return None
    claims = dict(getattr(active, "claims", None) or {})
    owner_key = str(claims.get("owner_key") or "").strip().lower()
    requested_profile = str(claims.get("profile") or "").strip().lower()
    channel = str(claims.get("auth_channel") or "").strip().lower()
    valid = (
        owner_key == REMOTE_AUTH_OWNER_KEY
        and requested_profile in REMOTE_ALLOWED_PROFILES
        and requested_profile not in REMOTE_FORBIDDEN_PROFILES
        and requested_profile == "admin"
        and channel in {"oauth", "service"}
    )
    return {
        "authenticated": True,
        "valid": valid,
        "owner_key": owner_key or None,
        "profile": requested_profile if valid else "reader",
        "requested_profile": requested_profile or None,
        "auth_channel": channel or None,
        "client_id": getattr(active, "client_id", None),
        "scopes": list(getattr(active, "scopes", None) or []),
        "reason_code": "authenticated_remote_actor" if valid else "invalid_remote_actor_claims",
    }


def remote_surface_profile() -> str | None:
    actor = access_token_actor()
    if actor is None:
        return None
    return str(actor["profile"])
