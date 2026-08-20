from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastmcp.server.auth import AccessToken, MultiAuth, OAuthProvider, TokenVerifier
from mcp.server.auth.handlers.authorize import AuthorizationRequest
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mapi_platform.identity import distribution_name

from app.runtime.owner_credentials import verify_owner_password
from app.runtime.remote_auth_config import RemoteAuthConfig
from app.runtime.remote_auth_contract import (
    PKCE_CHALLENGE_PATTERN,
    PKCE_METHOD,
    REMOTE_AUTH_OWNER_KEY,
    REMOTE_AUTH_POLICY_VERSION,
    REMOTE_AUTH_SCHEMA_VERSION,
    REMOTE_CODEX_PROFILE,
    REMOTE_CODEX_SCOPES,
    REMOTE_OAUTH_PROFILE,
    REMOTE_OAUTH_SCOPES,
    REMOTE_REQUIRED_SCOPE,
    REMOTE_SERVICE_PROFILE,
    REMOTE_SERVICE_SCOPES,
    TOKEN_KINDS,
)
from app.runtime.remote_auth_store import ensure_remote_auth_schema

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_epoch() -> int:
    return int(time.time())


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fingerprint_from_hash(value: str) -> str:
    return value[:16]


def _json_list(values: Iterable[str]) -> str:
    return json.dumps(sorted({str(value).strip() for value in values if str(value).strip()}), separators=(",", ":"))


def _parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded if str(item).strip()]


def _append_query(url: str, **params: str | None) -> str:
    split = urlsplit(url)
    query = list(parse_qsl(split.query, keep_blank_values=True))
    query.extend((key, value) for key, value in params.items() if value is not None)
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class RemoteAuthStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            conn.commit()

    def save_client(self, client_info: OAuthClientInformationFull) -> None:
        client_id = str(client_info.client_id or "").strip()
        if not client_id:
            raise ValueError("dynamic_client_id_required")
        payload = client_info.model_dump_json(exclude_none=True)
        now = _utc_now_iso()
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            conn.execute(
                """
                INSERT INTO remote_auth_clients(client_id, client_json, created_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    client_json=excluded.client_json,
                    last_seen_at=excluded.last_seen_at
                """,
                (client_id, payload, now, now),
            )
            conn.commit()

    def load_client(self, client_id: str) -> OAuthClientInformationFull | None:
        normalized = str(client_id or "").strip()
        if not normalized:
            return None
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            row = conn.execute(
                "SELECT client_json FROM remote_auth_clients WHERE client_id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE remote_auth_clients SET last_seen_at=? WHERE client_id=?",
                (_utc_now_iso(), normalized),
            )
            conn.commit()
        try:
            return OAuthClientInformationFull.model_validate_json(str(row["client_json"]))
        except Exception:
            return None

    def dynamic_client_count(self) -> int:
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            return int(conn.execute("SELECT COUNT(*) FROM remote_auth_clients").fetchone()[0])

    def audit(
        self,
        *,
        event_type: str,
        channel: str,
        outcome: str,
        reason_code: str,
        token_hash: str | None = None,
        client_id: str | None = None,
        owner_key: str | None = None,
        profile: str | None = None,
    ) -> None:
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            conn.execute(
                """
                INSERT INTO remote_auth_audit_events (
                    event_type, channel, client_id, owner_key, profile,
                    outcome, reason_code, token_fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event_type),
                    str(channel),
                    client_id,
                    owner_key,
                    profile,
                    str(outcome),
                    str(reason_code),
                    None if token_hash is None else _fingerprint_from_hash(token_hash),
                    _utc_now_iso(),
                ),
            )
            conn.commit()

    def rate_allowed(
        self,
        *,
        bucket: str,
        action: str,
        window_seconds: int,
        max_attempts: int,
    ) -> bool:
        now = _now_epoch()
        threshold = now - int(window_seconds)
        bucket_hash = _secret_hash(bucket)
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            conn.execute("DELETE FROM remote_auth_rate_events WHERE occurred_at < ?", (threshold,))
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM remote_auth_rate_events WHERE bucket_hash=? AND action=? AND occurred_at>=?",
                    (bucket_hash, action, threshold),
                ).fetchone()[0]
            )
            if count >= int(max_attempts):
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO remote_auth_rate_events(bucket_hash, action, occurred_at) VALUES (?, ?, ?)",
                (bucket_hash, action, now),
            )
            conn.commit()
            return True

    def insert_authorization_code(
        self,
        *,
        raw_code: str,
        client_id: str,
        redirect_uri: str,
        scopes: Iterable[str],
        code_challenge: str,
        owner_key: str,
        profile: str,
        expires_at: int,
    ) -> None:
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            conn.execute(
                """
                INSERT INTO remote_auth_authorization_codes (
                    code_hash, client_id, redirect_uri, scopes_json, code_challenge,
                    owner_key, profile, expires_at, created_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    _secret_hash(raw_code),
                    client_id,
                    redirect_uri,
                    _json_list(scopes),
                    code_challenge,
                    owner_key,
                    profile,
                    int(expires_at),
                    _utc_now_iso(),
                ),
            )
            conn.commit()

    def load_authorization_code(self, raw_code: str) -> sqlite3.Row | None:
        code_hash = _secret_hash(raw_code)
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            row = conn.execute(
                "SELECT * FROM remote_auth_authorization_codes WHERE code_hash=?",
                (code_hash,),
            ).fetchone()
            return row

    def consume_authorization_code(self, raw_code: str) -> bool:
        code_hash = _secret_hash(raw_code)
        now = _now_epoch()
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            cursor = conn.execute(
                """
                UPDATE remote_auth_authorization_codes
                SET consumed_at=?
                WHERE code_hash=? AND consumed_at IS NULL AND expires_at>?
                """,
                (_utc_now_iso(), code_hash, now),
            )
            conn.commit()
            return int(cursor.rowcount or 0) == 1

    def insert_token(
        self,
        *,
        raw_token: str,
        token_kind: str,
        client_id: str,
        owner_key: str,
        profile: str,
        scopes: Iterable[str],
        expires_at: int | None,
        pair_hash: str | None = None,
        label: str | None = None,
    ) -> str:
        if token_kind not in TOKEN_KINDS:
            raise ValueError("invalid_remote_token_kind")
        token_hash = _secret_hash(raw_token)
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            conn.execute(
                """
                INSERT INTO remote_auth_tokens (
                    token_hash, token_kind, client_id, owner_key, profile,
                    scopes_json, expires_at, pair_hash, rotated_to_hash, label,
                    created_at, last_seen_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, NULL)
                """,
                (
                    token_hash,
                    token_kind,
                    client_id,
                    owner_key,
                    profile,
                    _json_list(scopes),
                    expires_at,
                    pair_hash,
                    label,
                    _utc_now_iso(),
                ),
            )
            conn.commit()
        return token_hash

    def load_token(self, raw_token: str, *, token_kind: str | None = None) -> sqlite3.Row | None:
        token_hash = _secret_hash(raw_token)
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            if token_kind is None:
                return conn.execute(
                    "SELECT * FROM remote_auth_tokens WHERE token_hash=?",
                    (token_hash,),
                ).fetchone()
            return conn.execute(
                "SELECT * FROM remote_auth_tokens WHERE token_hash=? AND token_kind=?",
                (token_hash, token_kind),
            ).fetchone()

    def touch_token(self, raw_token: str) -> None:
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            conn.execute(
                "UPDATE remote_auth_tokens SET last_seen_at=? WHERE token_hash=?",
                (_utc_now_iso(), _secret_hash(raw_token)),
            )
            conn.commit()

    def revoke_token_pair(self, raw_token: str, *, rotated_to_hash: str | None = None) -> None:
        token_hash = _secret_hash(raw_token)
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            row = conn.execute(
                "SELECT pair_hash FROM remote_auth_tokens WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            hashes = [token_hash]
            if row is not None and row["pair_hash"]:
                hashes.append(str(row["pair_hash"]))
            now = _utc_now_iso()
            for item_hash in hashes:
                conn.execute(
                    """
                    UPDATE remote_auth_tokens
                    SET revoked_at=COALESCE(revoked_at, ?),
                        rotated_to_hash=COALESCE(?, rotated_to_hash)
                    WHERE token_hash=?
                    """,
                    (now, rotated_to_hash, item_hash),
                )
            conn.commit()

    def token_status(self, row: sqlite3.Row | None) -> str:
        if row is None:
            return "missing"
        if row["revoked_at"]:
            return "revoked"
        expires_at = row["expires_at"]
        if expires_at is not None and int(expires_at) <= _now_epoch():
            return "expired"
        return "ok"

    def list_redacted_tokens(self, *, token_kind: str | None = None) -> list[dict[str, Any]]:
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            if token_kind is None:
                rows = conn.execute(
                    "SELECT * FROM remote_auth_tokens ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM remote_auth_tokens WHERE token_kind=? ORDER BY created_at DESC",
                    (token_kind,),
                ).fetchall()
            return [
                {
                    "token_fingerprint": _fingerprint_from_hash(str(row["token_hash"])),
                    "token_kind": row["token_kind"],
                    "client_id": row["client_id"],
                    "owner_key": row["owner_key"],
                    "profile": row["profile"],
                    "scopes": _parse_json_list(row["scopes_json"]),
                    "expires_at": row["expires_at"],
                    "label": row["label"],
                    "created_at": row["created_at"],
                    "last_seen_at": row["last_seen_at"],
                    "revoked_at": row["revoked_at"],
                }
                for row in rows
            ]

    def revoke_by_fingerprint(self, fingerprint: str) -> int:
        normalized = str(fingerprint or "").strip().lower()
        if len(normalized) < 8 or not re.fullmatch(r"[0-9a-f]+", normalized):
            raise ValueError("invalid_token_fingerprint")
        with _connect(self.db_path) as conn:
            ensure_remote_auth_schema(conn)
            rows = conn.execute(
                "SELECT token_hash, pair_hash FROM remote_auth_tokens WHERE token_hash LIKE ?",
                (normalized + "%",),
            ).fetchall()
            hashes = {str(row["token_hash"]) for row in rows}
            hashes.update(str(row["pair_hash"]) for row in rows if row["pair_hash"])
            if len(rows) != 1:
                return 0
            now = _utc_now_iso()
            for token_hash in hashes:
                conn.execute(
                    "UPDATE remote_auth_tokens SET revoked_at=COALESCE(revoked_at, ?) WHERE token_hash=?",
                    (now, token_hash),
                )
            conn.commit()
            return len(hashes)


class PrivateSQLiteOAuthProvider(OAuthProvider):
    def __init__(
        self,
        *,
        config: RemoteAuthConfig,
        db_path: str | Path,
    ) -> None:
        errors = config.validate()
        if errors:
            raise ValueError("invalid_remote_auth_config:" + ",".join(errors))
        super().__init__(
            base_url=config.base_url,
            resource_base_url=config.base_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=list(REMOTE_OAUTH_SCOPES),
                default_scopes=list(REMOTE_OAUTH_SCOPES),
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[REMOTE_REQUIRED_SCOPE],
        )
        self.config = config
        self.store = RemoteAuthStore(db_path)
        self.client = None
        if config.oauth_redirect_uris:
            self.client = OAuthClientInformationFull(
                client_id=config.oauth_client_id,
                client_name="Private ChatGPT MCP client",
                redirect_uris=list(config.oauth_redirect_uris),
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=" ".join(REMOTE_OAUTH_SCOPES),
            )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if (
            self.client is not None
            and hmac.compare_digest(str(client_id), self.config.oauth_client_id)
        ):
            return self.client
        return self.store.load_client(str(client_id))

    @staticmethod
    def _dynamic_redirect_allowed(uri: str) -> bool:
        parsed = urlsplit(str(uri))
        return (
            parsed.scheme == "https"
            and parsed.netloc.casefold() == "chatgpt.com"
            and parsed.path.startswith("/connector/oauth/")
            and bool(parsed.path.removeprefix("/connector/oauth/").strip("/"))
            and not parsed.fragment
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_id = str(client_info.client_id or "").strip()
        redirects = [str(uri) for uri in (client_info.redirect_uris or [])]
        if not client_id:
            raise RegistrationError("invalid_client_metadata", "client_id_required")
        if not redirects or not all(self._dynamic_redirect_allowed(uri) for uri in redirects):
            raise RegistrationError(
                "invalid_redirect_uri",
                "dynamic_registration_allows_only_chatgpt_connector_callbacks",
            )
        if client_info.token_endpoint_auth_method not in {
            "none",
            "client_secret_post",
            "client_secret_basic",
        }:
            raise RegistrationError(
                "invalid_client_metadata",
                "unsupported_token_endpoint_auth_method",
            )
        if not {"authorization_code", "refresh_token"}.issubset(
            set(client_info.grant_types or [])
        ):
            raise RegistrationError("invalid_client_metadata", "required_grant_types_missing")
        if "code" not in set(client_info.response_types or []):
            raise RegistrationError("invalid_client_metadata", "code_response_type_required")
        scopes = set(str(client_info.scope or "").split())
        if scopes and not scopes.issubset(set(REMOTE_OAUTH_SCOPES)):
            raise RegistrationError("invalid_client_metadata", "scope_not_allowed")
        if not self.store.rate_allowed(
            bucket="dynamic-client-registration",
            action="oauth_client_register",
            window_seconds=self.config.rate_limit_window_seconds,
            max_attempts=min(30, self.config.rate_limit_max_attempts),
        ):
            raise RegistrationError("invalid_client_metadata", "registration_rate_limited")
        self.store.save_client(client_info)
        self.store.audit(
            event_type="oauth_client_register",
            channel="oauth",
            outcome="allowed",
            reason_code="dynamic_client_registered",
            client_id=client_id,
            owner_key=self.config.owner_key,
            profile=REMOTE_OAUTH_PROFILE,
        )

    def _rate_or_raise(self, *, bucket: str, action: str, channel: str) -> None:
        if self.store.rate_allowed(
            bucket=bucket,
            action=action,
            window_seconds=self.config.rate_limit_window_seconds,
            max_attempts=self.config.rate_limit_max_attempts,
        ):
            return
        self.store.audit(
            event_type=action,
            channel=channel,
            outcome="denied",
            reason_code="rate_limited",
            client_id=self.config.oauth_client_id,
            owner_key=self.config.owner_key,
            profile=REMOTE_OAUTH_PROFILE,
        )
        raise AuthorizeError(error="temporarily_unavailable", error_description="rate_limited")

    def _token_rate_or_raise(self, *, bucket: str, action: str) -> None:
        if self.store.rate_allowed(
            bucket=bucket,
            action=action,
            window_seconds=self.config.rate_limit_window_seconds,
            max_attempts=self.config.rate_limit_max_attempts,
        ):
            return
        self.store.audit(
            event_type=action,
            channel="oauth",
            outcome="denied",
            reason_code="rate_limited",
            client_id=self.config.oauth_client_id,
            owner_key=self.config.owner_key,
            profile=REMOTE_OAUTH_PROFILE,
        )
        raise TokenError("temporarily_unavailable", "rate_limited")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        # The SDK authorization handler is replaced by _direct_authorize_route in get_routes().
        # Keep this method only to satisfy the OAuth provider interface and fail closed if called.
        del client, params
        raise AuthorizeError(error="server_error", error_description="direct_authorize_route_required")

    def _oauth_error_response(
        self,
        *,
        error: str,
        description: str,
        state: str | None = None,
        redirect_uri: str | None = None,
    ) -> Response:
        payload = {"error": error, "error_description": description}
        if state is not None:
            payload["state"] = state
        if redirect_uri and (
            redirect_uri in self.config.oauth_redirect_uris
            or self._dynamic_redirect_allowed(redirect_uri)
        ):
            return RedirectResponse(
                _append_query(
                    redirect_uri,
                    error=error,
                    error_description=description,
                    state=state,
                ),
                status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(payload, status_code=400, headers={"Cache-Control": "no-store"})

    async def _validate_direct_authorization_request(
        self, request: Request
    ) -> tuple[dict[str, Any] | None, Response | None]:
        params = request.query_params if request.method == "GET" else await request.form()
        state = str(params.get("state")) if params.get("state") is not None else None
        try:
            auth_request = AuthorizationRequest.model_validate(params)
        except Exception as exc:
            return None, self._oauth_error_response(
                error="invalid_request",
                description=str(exc),
                state=state,
            )

        client = await self.get_client(auth_request.client_id)
        if client is None:
            return None, self._oauth_error_response(
                error="invalid_request",
                description=f"Client ID '{auth_request.client_id}' not found",
                state=auth_request.state,
            )

        try:
            redirect_uri_obj = client.validate_redirect_uri(auth_request.redirect_uri)
        except Exception as exc:
            return None, self._oauth_error_response(
                error="invalid_request",
                description=str(exc),
                state=auth_request.state,
            )
        redirect_uri = str(redirect_uri_obj)

        try:
            scopes = tuple(client.validate_scope(auth_request.scope))
        except Exception as exc:
            return None, self._oauth_error_response(
                error="invalid_scope",
                description=str(exc),
                state=auth_request.state,
                redirect_uri=redirect_uri,
            )

        if auth_request.code_challenge_method != PKCE_METHOD:
            return None, self._oauth_error_response(
                error="invalid_request",
                description="pkce_s256_required",
                state=auth_request.state,
                redirect_uri=redirect_uri,
            )
        if not PKCE_CHALLENGE_PATTERN.fullmatch(str(auth_request.code_challenge or "")):
            return None, self._oauth_error_response(
                error="invalid_request",
                description="pkce_s256_required",
                state=auth_request.state,
                redirect_uri=redirect_uri,
            )
        if not scopes:
            scopes = tuple(REMOTE_OAUTH_SCOPES)
        if not set(scopes).issubset(set(REMOTE_OAUTH_SCOPES)):
            return None, self._oauth_error_response(
                error="invalid_scope",
                description="scope_not_allowed",
                state=auth_request.state,
                redirect_uri=redirect_uri,
            )
        if REMOTE_REQUIRED_SCOPE not in scopes:
            return None, self._oauth_error_response(
                error="invalid_scope",
                description="required_scope_missing",
                state=auth_request.state,
                redirect_uri=redirect_uri,
            )

        return {
            "client_id": str(auth_request.client_id),
            "redirect_uri": redirect_uri,
            "scopes": scopes,
            "scope": " ".join(scopes),
            "state": auth_request.state,
            "code_challenge": str(auth_request.code_challenge),
            "code_challenge_method": PKCE_METHOD,
            "resource": auth_request.resource,
        }, None

    def _issue_authorization_code(self, auth: dict[str, Any]) -> str:
        raw_code = "mapi_ac_" + secrets.token_urlsafe(32)
        expires_at = _now_epoch() + self.config.authorization_code_ttl_seconds
        self.store.insert_authorization_code(
            raw_code=raw_code,
            client_id=str(auth["client_id"]),
            redirect_uri=str(auth["redirect_uri"]),
            scopes=auth["scopes"],
            code_challenge=str(auth["code_challenge"]),
            owner_key=self.config.owner_key,
            profile=REMOTE_OAUTH_PROFILE,
            expires_at=expires_at,
        )
        self.store.audit(
            event_type="oauth_authorize",
            channel="oauth",
            outcome="allowed",
            reason_code="authorization_code_issued",
            token_hash=_secret_hash(raw_code),
            client_id=str(auth["client_id"]),
            owner_key=self.config.owner_key,
            profile=REMOTE_OAUTH_PROFILE,
        )
        return _append_query(
            str(auth["redirect_uri"]),
            code=raw_code,
            state=auth.get("state"),
        )

    def _login_html(self, *, auth: dict[str, Any], error: str | None = None) -> str:
        safe_login = html.escape(self.config.owner_login, quote=True)
        safe_product = html.escape(distribution_name(), quote=True)
        error_html = (
            '<div class="error">Nieprawidłowy login lub hasło.</div>' if error else ""
        )

        hidden_fields: list[str] = []
        values = {
            "response_type": "code",
            "client_id": auth["client_id"],
            "redirect_uri": auth["redirect_uri"],
            "scope": auth["scope"],
            "state": auth.get("state"),
            "code_challenge": auth["code_challenge"],
            "code_challenge_method": auth["code_challenge_method"],
            "resource": auth.get("resource"),
        }
        for key, value in values.items():
            if value is None:
                continue
            hidden_fields.append(
                f'<input type="hidden" name="{html.escape(str(key), quote=True)}" '
                f'value="{html.escape(str(value), quote=True)}">'
            )
        hidden = "\n".join(hidden_fields)

        return f"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_product} · logowanie</title>
<style>
:root {{ color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#0f1115; color:#f5f7fb; }}
.card {{ width:min(420px, calc(100vw - 40px)); padding:32px; border:1px solid #2b3038; border-radius:18px; background:#171a20; box-shadow:0 20px 70px #0008; }}
h1 {{ margin:0 0 8px; font-size:24px; }}
p {{ margin:0 0 24px; color:#aeb6c3; line-height:1.45; }}
label {{ display:block; margin:14px 0 7px; font-size:14px; color:#d7dce5; }}
input {{ box-sizing:border-box; width:100%; padding:12px 13px; border-radius:10px; border:1px solid #39414d; background:#0f1115; color:#fff; font:inherit; }}
button {{ width:100%; margin-top:20px; padding:12px 14px; border:0; border-radius:10px; background:#f3f5f7; color:#111318; font-weight:700; cursor:pointer; }}
.error {{ margin:0 0 16px; padding:10px 12px; border-radius:9px; background:#481d24; color:#ffd9de; font-size:14px; }}
.small {{ margin-top:18px; font-size:12px; color:#7f8997; }}
</style>
</head>
<body>
<main class="card">
<h1>Zaloguj się do {safe_product}</h1>
<p>Jedno logowanie autoryzuje połączenie ChatGPT z Twoją instancją MCP.</p>
{error_html}
<form method="post" action="/authorize" autocomplete="on">
{hidden}
<label for="username">Login</label>
<input id="username" name="username" type="text" value="{safe_login}" autocomplete="username" required autofocus>
<label for="password">Hasło</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Zaloguj i połącz z ChatGPT</button>
</form>
<div class="small">{safe_product} · single-owner OAuth admin</div>
</main>
</body>
</html>"""

    def _login_response(
        self,
        *,
        auth: dict[str, Any],
        status_code: int = 200,
        error: str | None = None,
    ) -> HTMLResponse:
        redirect = urlsplit(str(auth["redirect_uri"]))
        redirect_origin = f"{redirect.scheme}://{redirect.netloc}"
        csp = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            f"form-action 'self' {redirect_origin}; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        return HTMLResponse(
            self._login_html(auth=auth, error=error),
            status_code=status_code,
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": csp,
            },
        )

    async def _direct_authorize_route(self, request: Request) -> Response:
        auth, error_response = await self._validate_direct_authorization_request(request)
        if error_response is not None:
            return error_response
        assert auth is not None

        if request.method == "GET":
            self._rate_or_raise(
                bucket=str(auth["client_id"]),
                action="oauth_authorize",
                channel="oauth",
            )
            return self._login_response(auth=auth)

        form = await request.form()
        username = str(form.get("username") or "").strip()
        password = str(form.get("password") or "")
        client_host = request.client.host if request.client else "unknown"
        bucket = f"{client_host}:{auth['client_id']}"
        allowed = self.store.rate_allowed(
            bucket=bucket,
            action="owner_login",
            window_seconds=self.config.rate_limit_window_seconds,
            max_attempts=min(10, self.config.rate_limit_max_attempts),
        )
        login_ok = hmac.compare_digest(username.casefold(), self.config.owner_login.casefold())
        password_ok = verify_owner_password(password, self.config.owner_password_hash)
        password = ""
        if not allowed or not login_ok or not password_ok:
            self.store.audit(
                event_type="owner_login",
                channel="oauth",
                outcome="denied",
                reason_code="rate_limited" if not allowed else "invalid_owner_credentials",
                client_id=str(auth["client_id"]),
                owner_key=None,
                profile=None,
            )
            return self._login_response(
                auth=auth,
                status_code=429 if not allowed else 401,
                error="invalid_credentials",
            )

        self.store.audit(
            event_type="owner_login",
            channel="oauth",
            outcome="allowed",
            reason_code="owner_authenticated",
            client_id=str(auth["client_id"]),
            owner_key=self.config.owner_key,
            profile=REMOTE_OAUTH_PROFILE,
        )
        return RedirectResponse(
            self._issue_authorization_code(auth),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = list(super().get_routes(mcp_path))
        replaced: list[Route] = []
        for route in routes:
            if isinstance(route, Route) and route.path == "/authorize":
                replaced.append(
                    Route(
                        "/authorize",
                        endpoint=self._direct_authorize_route,
                        methods=["GET", "POST"],
                    )
                )
            else:
                replaced.append(route)
        return replaced

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self.store.load_authorization_code(authorization_code)
        if row is None:
            return None
        if row["consumed_at"] or int(row["expires_at"]) <= _now_epoch():
            return None
        if str(row["client_id"]) != str(client.client_id or ""):
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=_parse_json_list(row["scopes_json"]),
            expires_at=float(row["expires_at"]),
            client_id=str(row["client_id"]),
            code_challenge=str(row["code_challenge"]),
            redirect_uri=str(row["redirect_uri"]),
            redirect_uri_provided_explicitly=True,
        )

    def _insert_pair(
        self,
        *,
        client_id: str,
        scopes: Iterable[str],
        rotated_from_raw: str | None = None,
    ) -> OAuthToken:
        now = _now_epoch()
        access_raw = "mapi_at_" + secrets.token_urlsafe(48)
        refresh_raw = "mapi_rt_" + secrets.token_urlsafe(48)
        access_hash = _secret_hash(access_raw)
        refresh_hash = _secret_hash(refresh_raw)
        self.store.insert_token(
            raw_token=access_raw,
            token_kind="access",
            client_id=client_id,
            owner_key=self.config.owner_key,
            profile=REMOTE_OAUTH_PROFILE,
            scopes=scopes,
            expires_at=now + self.config.access_ttl_seconds,
            pair_hash=refresh_hash,
        )
        self.store.insert_token(
            raw_token=refresh_raw,
            token_kind="refresh",
            client_id=client_id,
            owner_key=self.config.owner_key,
            profile=REMOTE_OAUTH_PROFILE,
            scopes=scopes,
            expires_at=now + self.config.refresh_ttl_seconds,
            pair_hash=access_hash,
        )
        if rotated_from_raw is not None:
            self.store.revoke_token_pair(rotated_from_raw, rotated_to_hash=refresh_hash)
        return OAuthToken(
            access_token=access_raw,
            token_type="Bearer",
            expires_in=self.config.access_ttl_seconds,
            refresh_token=refresh_raw,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._token_rate_or_raise(
            bucket=f"{client.client_id}:authorization_code",
            action="oauth_token_exchange",
        )
        if not self.store.consume_authorization_code(authorization_code.code):
            raise TokenError("invalid_grant", "authorization_code_invalid_or_consumed")
        token = self._insert_pair(client_id=str(client.client_id or ""), scopes=authorization_code.scopes)
        self.store.audit(
            event_type="oauth_token_exchange",
            channel="oauth",
            outcome="allowed",
            reason_code="access_and_refresh_issued",
            token_hash=_secret_hash(token.access_token),
            client_id=str(client.client_id or ""),
            owner_key=self.config.owner_key,
            profile=REMOTE_OAUTH_PROFILE,
        )
        return token

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        self._token_rate_or_raise(
            bucket=f"{client.client_id}:refresh_load",
            action="oauth_refresh_load",
        )
        row = self.store.load_token(refresh_token, token_kind="refresh")
        status = self.store.token_status(row)
        if status != "ok" or row is None:
            self.store.audit(
                event_type="oauth_refresh_load",
                channel="oauth",
                outcome="denied",
                reason_code=f"refresh_{status}",
                token_hash=_secret_hash(refresh_token),
                client_id=str(client.client_id or ""),
            )
            return None
        if str(row["client_id"]) != str(client.client_id or ""):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=str(row["client_id"]),
            scopes=_parse_json_list(row["scopes_json"]),
            expires_at=None if row["expires_at"] is None else int(row["expires_at"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._token_rate_or_raise(
            bucket=f"{client.client_id}:refresh_exchange",
            action="oauth_refresh_exchange",
        )
        original_scopes = set(refresh_token.scopes)
        requested_scopes = list(scopes or refresh_token.scopes)
        if not set(requested_scopes).issubset(original_scopes):
            raise TokenError("invalid_scope", "refresh_scope_escalation_denied")
        token = self._insert_pair(
            client_id=str(client.client_id or ""),
            scopes=requested_scopes,
            rotated_from_raw=refresh_token.token,
        )
        self.store.audit(
            event_type="oauth_refresh_exchange",
            channel="oauth",
            outcome="allowed",
            reason_code="refresh_rotated",
            token_hash=_secret_hash(token.access_token),
            client_id=str(client.client_id or ""),
            owner_key=self.config.owner_key,
            profile=REMOTE_OAUTH_PROFILE,
        )
        return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        token_hash = _secret_hash(token)
        bucket = "oauth-token:" + _fingerprint_from_hash(token_hash)
        if not self.store.rate_allowed(
            bucket=bucket,
            action="oauth_access_verify",
            window_seconds=self.config.rate_limit_window_seconds,
            max_attempts=self.config.rate_limit_max_attempts,
        ):
            self.store.audit(
                event_type="oauth_access_verify",
                channel="oauth",
                outcome="denied",
                reason_code="rate_limited",
                token_hash=token_hash,
            )
            return None
        row = self.store.load_token(token, token_kind="access")
        status = self.store.token_status(row)
        if status != "ok" or row is None:
            self.store.audit(
                event_type="oauth_access_verify",
                channel="oauth",
                outcome="denied",
                reason_code=f"access_{status}",
                token_hash=token_hash,
            )
            return None
        if str(row["owner_key"]) != self.config.owner_key or str(row["profile"]) != REMOTE_OAUTH_PROFILE:
            return None
        self.store.touch_token(token)
        return AccessToken(
            token=token,
            client_id=str(row["client_id"]),
            scopes=_parse_json_list(row["scopes_json"]),
            expires_at=None if row["expires_at"] is None else int(row["expires_at"]),
            claims={
                "owner_key": str(row["owner_key"]),
                "profile": str(row["profile"]),
                "auth_channel": "oauth",
                "token_kind": "access",
                "subject": str(row["owner_key"]),
            },
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke_token_pair(token.token)
        self.store.audit(
            event_type="oauth_revoke",
            channel="oauth",
            outcome="allowed",
            reason_code="token_pair_revoked",
            token_hash=_secret_hash(token.token),
            client_id=token.client_id,
            owner_key=self.config.owner_key,
            profile=REMOTE_OAUTH_PROFILE,
        )


class ServiceBearerVerifier(TokenVerifier):
    """Revocable non-interactive admin bearer for explicitly authorized automation."""

    def __init__(self, *, config: RemoteAuthConfig, db_path: str | Path) -> None:
        errors = config.validate()
        if errors:
            raise ValueError("invalid_remote_auth_config:" + ",".join(errors))
        super().__init__(base_url=config.base_url, required_scopes=[REMOTE_REQUIRED_SCOPE])
        self.config = config
        self.store = RemoteAuthStore(db_path)

    async def verify_token(self, token: str) -> AccessToken | None:
        token_hash = _secret_hash(token)
        bucket = "service-token:" + _fingerprint_from_hash(token_hash)
        if not self.store.rate_allowed(
            bucket=bucket,
            action="service_bearer_verify",
            window_seconds=self.config.rate_limit_window_seconds,
            max_attempts=self.config.rate_limit_max_attempts,
        ):
            self.store.audit(
                event_type="service_bearer_verify",
                channel="service",
                outcome="denied",
                reason_code="rate_limited",
                token_hash=token_hash,
            )
            return None
        row = self.store.load_token(token, token_kind="service")
        status = self.store.token_status(row)
        if status != "ok" or row is None:
            self.store.audit(
                event_type="service_bearer_verify",
                channel="service",
                outcome="denied",
                reason_code=f"service_{status}",
                token_hash=token_hash,
            )
            return None
        if str(row["owner_key"]) != self.config.owner_key or str(row["profile"]) != REMOTE_SERVICE_PROFILE:
            return None
        scopes = _parse_json_list(row["scopes_json"])
        if not set(REMOTE_SERVICE_SCOPES).issubset(set(scopes)):
            return None
        self.store.touch_token(token)
        return AccessToken(
            token=token,
            client_id=str(row["client_id"]),
            scopes=scopes,
            expires_at=None if row["expires_at"] is None else int(row["expires_at"]),
            claims={
                "owner_key": str(row["owner_key"]),
                "profile": str(row["profile"]),
                "auth_channel": "service",
                "token_kind": "service",
                "subject": str(row["owner_key"]),
                "label": row["label"],
            },
        )


class CodexBearerVerifier(TokenVerifier):
    def __init__(self, *, config: RemoteAuthConfig, db_path: str | Path) -> None:
        errors = config.validate()
        if errors:
            raise ValueError("invalid_remote_auth_config:" + ",".join(errors))
        super().__init__(base_url=config.base_url, required_scopes=[REMOTE_REQUIRED_SCOPE])
        self.config = config
        self.store = RemoteAuthStore(db_path)

    async def verify_token(self, token: str) -> AccessToken | None:
        token_hash = _secret_hash(token)
        bucket = "codex-token:" + _fingerprint_from_hash(token_hash)
        if not self.store.rate_allowed(
            bucket=bucket,
            action="codex_bearer_verify",
            window_seconds=self.config.rate_limit_window_seconds,
            max_attempts=self.config.rate_limit_max_attempts,
        ):
            self.store.audit(
                event_type="codex_bearer_verify",
                channel="codex",
                outcome="denied",
                reason_code="rate_limited",
                token_hash=token_hash,
            )
            return None
        row = self.store.load_token(token, token_kind="codex")
        status = self.store.token_status(row)
        if status != "ok" or row is None:
            self.store.audit(
                event_type="codex_bearer_verify",
                channel="codex",
                outcome="denied",
                reason_code=f"codex_{status}",
                token_hash=token_hash,
            )
            return None
        if str(row["owner_key"]) != self.config.owner_key or str(row["profile"]) != REMOTE_CODEX_PROFILE:
            return None
        self.store.touch_token(token)
        return AccessToken(
            token=token,
            client_id=str(row["client_id"]),
            scopes=_parse_json_list(row["scopes_json"]),
            expires_at=None if row["expires_at"] is None else int(row["expires_at"]),
            claims={
                "owner_key": str(row["owner_key"]),
                "profile": str(row["profile"]),
                "auth_channel": "codex",
                "token_kind": "codex",
                "subject": str(row["owner_key"]),
                "label": row["label"],
            },
        )


def issue_codex_bearer_token(**_: Any) -> dict[str, Any]:
    raise RuntimeError("codex_bearer_retired_single_owner_admin_oauth")


def issue_service_bearer_token(
    *,
    db_path: str | Path,
    owner_key: str = REMOTE_AUTH_OWNER_KEY,
    label: str = "service",
    ttl_seconds: int = 90 * 24 * 3600,
    now: int | None = None,
) -> dict[str, Any]:
    owner = str(owner_key or "").strip().lower()
    if owner != REMOTE_AUTH_OWNER_KEY:
        raise ValueError("service_token_owner_must_be_owner")
    normalized_label = str(label or "").strip()
    if not normalized_label or len(normalized_label) > 80:
        raise ValueError("service_token_label_invalid")
    ttl = int(ttl_seconds)
    if ttl < 3600 or ttl > 10 * 365 * 24 * 3600:
        raise ValueError("service_token_ttl_out_of_range")
    raw_token = "mapi_sv_" + secrets.token_urlsafe(48)
    store = RemoteAuthStore(db_path)
    expires_at = int(now if now is not None else _now_epoch()) + ttl
    token_hash = store.insert_token(
        raw_token=raw_token,
        token_kind="service",
        client_id="service-client",
        owner_key=owner,
        profile=REMOTE_SERVICE_PROFILE,
        scopes=REMOTE_SERVICE_SCOPES,
        expires_at=expires_at,
        label=normalized_label,
    )
    store.audit(
        event_type="service_token_issue",
        channel="service",
        outcome="allowed",
        reason_code="service_token_issued",
        token_hash=token_hash,
        client_id="service-client",
        owner_key=owner,
        profile=REMOTE_SERVICE_PROFILE,
    )
    return {
        "status": "issued",
        "token": raw_token,
        "token_fingerprint": _fingerprint_from_hash(token_hash),
        "owner_key": owner,
        "profile": REMOTE_SERVICE_PROFILE,
        "scopes": list(REMOTE_SERVICE_SCOPES),
        "expires_at": expires_at,
        "warning": "The raw token is returned once and is never stored in the database.",
    }


def revoke_token_fingerprint(*, db_path: str | Path, fingerprint: str) -> dict[str, Any]:
    revoked = RemoteAuthStore(db_path).revoke_by_fingerprint(fingerprint)
    return {
        "status": "revoked" if revoked else "not_found",
        "fingerprint": fingerprint,
        "revoked_token_rows": revoked,
    }


def build_remote_auth_provider(
    *,
    config: RemoteAuthConfig,
    db_path: str | Path,
) -> MultiAuth:
    oauth = PrivateSQLiteOAuthProvider(config=config, db_path=db_path)
    service = ServiceBearerVerifier(config=config, db_path=db_path)
    return MultiAuth(
        server=oauth,
        verifiers=[service],
        base_url=config.base_url,
        resource_base_url=config.base_url,
        required_scopes=[REMOTE_REQUIRED_SCOPE],
    )


def configure_remote_auth(mcp: Any, *, db_path: str | Path, config: RemoteAuthConfig | None = None) -> dict[str, Any]:
    resolved = config or RemoteAuthConfig.from_env()
    if not resolved.enabled:
        mcp.auth = None
        return {
            "status": "disabled",
            "schema_version": REMOTE_AUTH_SCHEMA_VERSION,
            "policy_version": REMOTE_AUTH_POLICY_VERSION,
            "enabled": False,
        }
    errors = resolved.validate()
    if errors:
        raise RuntimeError("remote_auth_config_invalid:" + ",".join(errors))
    mcp.auth = build_remote_auth_provider(config=resolved, db_path=db_path)
    return remote_auth_status(db_path=db_path, config=resolved)


def remote_auth_status(
    *,
    db_path: str | Path,
    config: RemoteAuthConfig | None = None,
) -> dict[str, Any]:
    resolved = config or RemoteAuthConfig.from_env()
    path = Path(db_path).resolve()
    store = RemoteAuthStore(path)
    with _connect(path) as conn:
        ensure_remote_auth_schema(conn)
        counts = {
            "dynamic_clients": store.dynamic_client_count(),
            "authorization_codes": int(conn.execute("SELECT COUNT(*) FROM remote_auth_authorization_codes").fetchone()[0]),
            "active_access_tokens": int(
                conn.execute(
                    "SELECT COUNT(*) FROM remote_auth_tokens WHERE token_kind='access' AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)",
                    (_now_epoch(),),
                ).fetchone()[0]
            ),
            "active_refresh_tokens": int(
                conn.execute(
                    "SELECT COUNT(*) FROM remote_auth_tokens WHERE token_kind='refresh' AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)",
                    (_now_epoch(),),
                ).fetchone()[0]
            ),
            "active_codex_tokens": int(
                conn.execute(
                    "SELECT COUNT(*) FROM remote_auth_tokens WHERE token_kind='codex' AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)",
                    (_now_epoch(),),
                ).fetchone()[0]
            ),
            "active_service_tokens": int(
                conn.execute(
                    "SELECT COUNT(*) FROM remote_auth_tokens WHERE token_kind='service' AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)",
                    (_now_epoch(),),
                ).fetchone()[0]
            ),
            "revoked_tokens": int(
                conn.execute("SELECT COUNT(*) FROM remote_auth_tokens WHERE revoked_at IS NOT NULL").fetchone()[0]
            ),
            "audit_events": int(conn.execute("SELECT COUNT(*) FROM remote_auth_audit_events").fetchone()[0]),
        }
    return {
        "status": "ready" if resolved.enabled and not resolved.validate() else ("disabled" if not resolved.enabled else "blocked"),
        "schema_version": REMOTE_AUTH_SCHEMA_VERSION,
        "policy_version": REMOTE_AUTH_POLICY_VERSION,
        "enabled": resolved.enabled,
        "owner_key": resolved.owner_key,
        "oauth": {
            "client_id": resolved.oauth_client_id,
            "redirect_uri_count": len(resolved.oauth_redirect_uris),
            "owner_login": resolved.owner_login,
            "owner_login_path": "/authorize",
            "login_ui": "built_in",
            "pkce_method": PKCE_METHOD,
            "dynamic_registration": True,
            "dynamic_registration_endpoint": "/register",
            "dynamic_redirect_prefix": "https://chatgpt.com/connector/oauth/",
            "manual_client_id_required": False,
            "manual_callback_required": False,
            "refresh_rotation": True,
            "access_ttl_seconds": resolved.access_ttl_seconds,
            "refresh_ttl_seconds": resolved.refresh_ttl_seconds,
            "authorization_code_ttl_seconds": resolved.authorization_code_ttl_seconds,
            "profile": REMOTE_OAUTH_PROFILE,
        },
        "service_tokens": {
            "profile": REMOTE_SERVICE_PROFILE,
            "scopes": list(REMOTE_SERVICE_SCOPES),
            "stored_hashed": True,
            "revocation_supported": True,
            "issued_by_operator_only": True,
        },
        "legacy_codex": {
            "status": "retired_not_accepted",
            "stored_token_rows_ignored": True,
        },
        "remote_admin_exposed": True,
        "remote_admin_auth_channel": "owner_oauth_or_explicit_service_token",
        "single_remote_user": True,
        "profiles_derive_from_auth": True,
        "raw_tokens_stored": False,
        "rate_limit": {
            "window_seconds": resolved.rate_limit_window_seconds,
            "max_attempts": resolved.rate_limit_max_attempts,
        },
        "counts": counts,
        "config_errors": resolved.validate(),
        "tokens": store.list_redacted_tokens(),
    }
