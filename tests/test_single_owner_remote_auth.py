from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from app.runtime.remote_actor import access_token_actor
from app.runtime.remote_auth_contract import REMOTE_OAUTH_PROFILE, REMOTE_OAUTH_SCOPES, REMOTE_SERVICE_PROFILE, REMOTE_SERVICE_SCOPES

ROOT = Path(__file__).resolve().parents[1]


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT) if not current else str(ROOT) + os.pathsep + current
    return env


def test_single_remote_oauth_identity_is_admin() -> None:
    assert REMOTE_OAUTH_PROFILE == "admin"
    assert "mapi:admin" in REMOTE_OAUTH_SCOPES
    assert "offline_access" in REMOTE_OAUTH_SCOPES


def test_remote_actor_accepts_owner_oauth_or_explicit_service_admin() -> None:
    admin = SimpleNamespace(
        claims={"owner_key": "owner", "profile": "admin", "auth_channel": "oauth"},
        client_id="owner-client",
        scopes=["mapi:read", "mapi:write", "mapi:admin", "offline_access"],
    )
    actor = access_token_actor(admin)
    assert actor is not None
    assert actor["valid"] is True
    assert actor["profile"] == "admin"

    service = SimpleNamespace(
        claims={"owner_key": "owner", "profile": "admin", "auth_channel": "service"},
        client_id="service-client",
        scopes=["mapi:read", "mapi:write", "mapi:admin"],
    )
    service_actor = access_token_actor(service)
    assert service_actor is not None
    assert service_actor["valid"] is True
    assert service_actor["profile"] == "admin"

    for claims in (
        {"owner_key": "owner", "profile": "agent", "auth_channel": "oauth"},
        {"owner_key": "owner", "profile": "admin", "auth_channel": "codex"},
        {"owner_key": "someone-else", "profile": "admin", "auth_channel": "oauth"},
    ):
        denied = access_token_actor(SimpleNamespace(claims=claims, client_id="x", scopes=[]))
        assert denied is not None
        assert denied["valid"] is False
        assert denied["profile"] == "reader"


def test_runtime_auth_has_owner_oauth_plus_explicit_service_path(tmp_path) -> None:
    code = """
from pathlib import Path
from app.runtime.owner_credentials import hash_owner_password
from app.runtime.remote_auth import build_remote_auth_provider, issue_codex_bearer_token, ServiceBearerVerifier
from app.runtime.remote_auth_config import RemoteAuthConfig
config = RemoteAuthConfig(
    enabled=True,
    base_url='https://mapi.example.test',
    owner_key='owner',
    oauth_client_id='owner-client',
    oauth_redirect_uris=('https://client.example.test/callback',),
    owner_login='owner',
    owner_password_hash=hash_owner_password('a sufficiently long owner password'),
)
provider = build_remote_auth_provider(config=config, db_path=Path('auth-test.db'))
assert provider.server is not None
assert len(provider.verifiers) == 1
assert isinstance(provider.verifiers[0], ServiceBearerVerifier)
try:
    issue_codex_bearer_token(db_path='ignored.db')
except RuntimeError as exc:
    assert str(exc) == 'codex_bearer_retired_single_owner_admin_oauth'
else:
    raise AssertionError('legacy bearer issuance unexpectedly enabled')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_dynamic_client_registration_removes_manual_client_id(tmp_path) -> None:
    code = r'''
import asyncio
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
from starlette.applications import Starlette

from app.runtime.owner_credentials import hash_owner_password
from app.runtime.remote_auth import PrivateSQLiteOAuthProvider
from app.runtime.remote_auth_config import RemoteAuthConfig

BASE = 'https://mapi.example.test'
REDIRECT = 'https://chatgpt.com/connector/oauth/dcr-test-callback'
PASSWORD = 'a sufficiently long owner password'
config = RemoteAuthConfig(
    enabled=True,
    base_url=BASE,
    owner_key='owner',
    oauth_client_id='chatgpt-private',
    oauth_redirect_uris=(),
    owner_login='michal',
    owner_password_hash=hash_owner_password(PASSWORD),
)
provider = PrivateSQLiteOAuthProvider(config=config, db_path='dcr-flow.db')
app = Starlette(routes=provider.get_routes('/mcp/'))

async def main():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, follow_redirects=False) as client:
        metadata = await client.get('/.well-known/oauth-authorization-server')
        assert metadata.status_code == 200, metadata.text
        body = metadata.json()
        assert body['registration_endpoint'] == BASE + '/register'

        denied = await client.post('/register', json={
            'redirect_uris': ['https://evil.example/callback'],
            'token_endpoint_auth_method': 'none',
            'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'],
        })
        assert denied.status_code == 400, denied.text

        registered = await client.post('/register', json={
            'redirect_uris': [REDIRECT],
            'token_endpoint_auth_method': 'none',
            'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'],
            'scope': 'mapi:read mapi:write mapi:admin offline_access',
            'client_name': 'ChatGPT',
        })
        assert registered.status_code == 201, registered.text
        registration = registered.json()
        client_id = registration['client_id']
        assert client_id
        assert registration['token_endpoint_auth_method'] == 'none'

        verifier = 'd' * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        params = {
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': REDIRECT,
            'scope': 'mapi:read mapi:write mapi:admin offline_access',
            'state': 'dcr-state',
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        }

        login_page = await client.get('/authorize', params=params)
        assert login_page.status_code == 200, login_page.text
        assert 'Zaloguj się do Polaris' in login_page.text

        accepted = await client.post('/authorize', data={
            **params,
            'username': 'michal',
            'password': PASSWORD,
        })
        assert accepted.status_code == 302, accepted.text
        callback = urlparse(accepted.headers['location'])
        query = parse_qs(callback.query)
        assert f'{callback.scheme}://{callback.netloc}{callback.path}' == REDIRECT
        assert query['state'] == ['dcr-state']

        token = await client.post('/token', data={
            'grant_type': 'authorization_code',
            'code': query['code'][0],
            'client_id': client_id,
            'redirect_uri': REDIRECT,
            'code_verifier': verifier,
        })
        assert token.status_code == 200, token.text
        token_body = token.json()
        assert token_body['access_token']
        assert token_body['refresh_token']

asyncio.run(main())
'''
    completed = subprocess.run(
        [sys.executable, '-c', code],
        cwd=tmp_path,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_owner_login_is_directly_on_authorize_and_issues_refresh_token(tmp_path) -> None:
    code = r"""
import asyncio
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
from starlette.applications import Starlette

from app.runtime.owner_credentials import hash_owner_password
from app.runtime.remote_auth import PrivateSQLiteOAuthProvider
from app.runtime.remote_auth_config import RemoteAuthConfig

BASE = 'https://mapi.example.test'
REDIRECT = 'https://chatgpt.com/connector/oauth/test-callback'
PASSWORD = 'a sufficiently long owner password'
config = RemoteAuthConfig(
    enabled=True,
    base_url=BASE,
    owner_key='owner',
    oauth_client_id='chatgpt-private',
    oauth_redirect_uris=(REDIRECT,),
    owner_login='michal',
    owner_password_hash=hash_owner_password(PASSWORD),
)
provider = PrivateSQLiteOAuthProvider(config=config, db_path='auth-flow.db')
app = Starlette(routes=provider.get_routes('/mcp/'))
verifier = 'v' * 64
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
params = {
    'response_type': 'code',
    'client_id': 'chatgpt-private',
    'redirect_uri': REDIRECT,
    'scope': 'mapi:read mapi:write mapi:admin offline_access',
    'state': 'state-123',
    'code_challenge': challenge,
    'code_challenge_method': 'S256',
}

async def main():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, follow_redirects=False) as client:
        metadata = await client.get('/.well-known/oauth-authorization-server')
        assert metadata.status_code == 200, metadata.text
        assert 'offline_access' in metadata.json()['scopes_supported']

        login_page = await client.get('/authorize', params=params)
        assert login_page.status_code == 200, login_page.text
        assert 'Zaloguj się do Polaris' in login_page.text
        assert 'action="/authorize"' in login_page.text
        assert '/oauth/login' not in login_page.text
        assert 'Basic' not in login_page.text
        assert PASSWORD not in login_page.text
        csp = login_page.headers['content-security-policy']
        assert "form-action 'self' https://chatgpt.com" in csp

        legacy_login = await client.get('/oauth/login')
        assert legacy_login.status_code == 404

        wrong = await client.post('/authorize', data={
            **params,
            'username': 'michal',
            'password': 'this is the wrong password',
        })
        assert wrong.status_code == 401
        assert 'Nieprawidłowy login lub hasło' in wrong.text

        accepted = await client.post('/authorize', data={
            **params,
            'username': 'michal',
            'password': PASSWORD,
        })
        assert accepted.status_code == 302, accepted.text
        callback = urlparse(accepted.headers['location'])
        assert f'{callback.scheme}://{callback.netloc}{callback.path}' == REDIRECT
        query = parse_qs(callback.query)
        assert query['state'] == ['state-123']
        code = query['code'][0]

        token = await client.post('/token', data={
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': 'chatgpt-private',
            'redirect_uri': REDIRECT,
            'code_verifier': verifier,
        })
        assert token.status_code == 200, token.text
        token_body = token.json()
        assert token_body['access_token']
        assert token_body['refresh_token']

        refresh = await client.post('/token', data={
            'grant_type': 'refresh_token',
            'refresh_token': token_body['refresh_token'],
            'client_id': 'chatgpt-private',
            'scope': 'mapi:read mapi:write mapi:admin offline_access',
        })
        assert refresh.status_code == 200, refresh.text
        refreshed = refresh.json()
        assert refreshed['access_token']
        assert refreshed['refresh_token']

asyncio.run(main())
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
