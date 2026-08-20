from __future__ import annotations

import re

REMOTE_AUTH_SCHEMA_VERSION = "mapi_remote_auth.v1"
REMOTE_AUTH_POLICY_VERSION = "mapi_remote_auth_policy.v1"
REMOTE_AUTH_OWNER_KEY = "owner"
REMOTE_OAUTH_PROFILE = "admin"
REMOTE_CODEX_PROFILE = "maintainer"
REMOTE_SERVICE_PROFILE = "admin"
REMOTE_ALLOWED_PROFILES = frozenset({REMOTE_OAUTH_PROFILE})
REMOTE_FORBIDDEN_PROFILES = frozenset()
REMOTE_REQUIRED_SCOPE = "mapi:read"
REMOTE_OAUTH_SCOPES = ("mapi:read", "mapi:write", "mapi:admin", "offline_access")
REMOTE_CODEX_SCOPES = ("mapi:read", "mapi:propose")
REMOTE_SERVICE_SCOPES = ("mapi:read", "mapi:write", "mapi:admin")
PKCE_METHOD = "S256"
PKCE_CHALLENGE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
TOKEN_KINDS = frozenset({"access", "refresh", "codex", "service"})
