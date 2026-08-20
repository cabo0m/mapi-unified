from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from app import db_migrations
from mapi.env import apply_runtime_environment, default_instance_root, parse_environment_file


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False



def _runtime_root_arg() -> Path | None:
    args = sys.argv[1:]
    for index, token in enumerate(args):
        if token in {"--root", "--instance-root"}:
            if index + 1 >= len(args):
                raise SystemExit("--root requires a path")
            return Path(args[index + 1]).expanduser().resolve()
        if token.startswith("--root=") or token.startswith("--instance-root="):
            return Path(token.split("=", 1)[1]).expanduser().resolve()
    return None


def _apply_runtime_cli_environment() -> dict[str, Any]:
    root = _runtime_root_arg()
    if root is not None:
        os.environ["MAPI_ROOT"] = str(root)
        os.environ["MAPI_ENV_FILE"] = str(root / ".env")
    return apply_runtime_environment()

def _database_path() -> Path:
    runtime = _apply_runtime_cli_environment()
    path = Path(runtime["db_path"]).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path



def _init_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize a fresh MAPI instance safely")
    parser.add_argument("--root", type=Path, default=default_instance_root(), help="Instance root; defaults to ~/.mapi-agent-memory")
    parser.add_argument("--mode", choices=("local", "vps-proxy", "vps-remote-auth"))
    parser.add_argument("--owner-key")
    parser.add_argument("--agent-subject-key")
    parser.add_argument("--agent-name")
    parser.add_argument("--agent-project-key")
    parser.add_argument("--port", type=int)
    parser.add_argument("--profile", choices=("reader", "agent", "maintainer", "admin"))
    parser.add_argument("--public-url")
    parser.add_argument("--oauth-client-id")
    parser.add_argument("--oauth-redirect-uri", action="append", default=[])
    parser.add_argument("--owner-login")
    parser.add_argument("--service-user")
    parser.add_argument("--service-name", help="Linux systemd service name for VPS modes")
    parser.add_argument("--recovery-command-json")
    parser.add_argument("--resume", action="store_true", help="Resume an existing init without duplicating self seeds")
    parser.add_argument("--no-self-seed", action="store_true", help="Do not create neutral Agent Self Model bootstrap records")
    service_group = parser.add_mutually_exclusive_group()
    service_group.add_argument("--install-service", action="store_true", help="Install and start generated systemd service")
    service_group.add_argument("--no-install-service", action="store_true", help="Generate systemd unit but do not install it")
    parser.add_argument("--no-verify-endpoint", action="store_true", help="Skip post-start endpoint reachability probes")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only")
    parser.add_argument("--non-interactive", action="store_true", help="Never prompt; use flags/defaults or fail closed")
    return parser


def init() -> None:
    from mapi.initialize import InitOptions, initialize_instance, slugify_identity

    args = _init_parser().parse_args()
    interactive = bool(sys.stdin.isatty()) and not args.non_interactive
    existing_env: dict[str, str] = {}
    existing_env_path = args.root.expanduser().resolve() / ".env"
    if args.resume and existing_env_path.exists():
        existing_env = parse_environment_file(existing_env_path)

    mode = args.mode
    if mode is None and existing_env:
        remote_enabled = existing_env.get("MAPI_REMOTE_AUTH_ENABLED", "false").casefold() in {"1", "true", "yes", "on"}
        mode = "vps-remote-auth" if remote_enabled else ("vps-proxy" if existing_env.get("MAPI_REMOTE_BASE_URL") else "local")
    if mode is None and interactive:
        mode = input("Deployment mode [local/vps-proxy/vps-remote-auth] (local): ").strip() or "local"
    mode = mode or "local"

    display_name = args.agent_name or existing_env.get("MAPI_AGENT_DISPLAY_NAME")
    if mode == "vps-remote-auth":
        # Polaris is the product/runtime label. The human-facing assistant name
        # is chosen later by the user during first-run MCP onboarding.
        display_name = display_name or "Polaris"
    else:
        if not display_name and interactive:
            display_name = input("Agent display name (Agent): ").strip() or "Agent"
        display_name = display_name or "Agent"
    subject = args.agent_subject_key or existing_env.get("MAPI_AGENT_SUBJECT_KEY") or slugify_identity(display_name)
    owner = args.owner_key or existing_env.get("MAPI_OWNER_KEY") or subject
    project = args.agent_project_key or existing_env.get("MAPI_AGENT_PROJECT_KEY") or f"{subject}-self"
    port = args.port if args.port is not None else int(existing_env.get("MAPI_RUNTIME_PORT", "8015"))
    profile = args.profile or existing_env.get("MCP_SURFACE_PROFILE") or ("admin" if mode == "vps-remote-auth" else "agent")
    service_name = args.service_name or existing_env.get("MAPI_SYSTEMD_SERVICE_NAME") or "mapi"

    public_url = args.public_url or existing_env.get("MAPI_REMOTE_BASE_URL")
    if mode != "local" and not public_url and interactive:
        public_url = input("Public HTTPS origin, e.g. https://mapi.example.com: ").strip()

    owner_login = args.owner_login or existing_env.get("MAPI_REMOTE_OWNER_LOGIN") or "owner"
    owner_password_hash = existing_env.get("MAPI_REMOTE_OWNER_PASSWORD_HASH") or os.environ.get("MAPI_REMOTE_OWNER_PASSWORD_HASH")
    redirects = list(args.oauth_redirect_uri or [])
    if not redirects and existing_env.get("MAPI_REMOTE_OAUTH_REDIRECT_URIS"):
        redirects = [item.strip() for item in existing_env["MAPI_REMOTE_OAUTH_REDIRECT_URIS"].split(",") if item.strip()]
    if mode == "vps-remote-auth" and interactive:
        if not args.owner_login and not existing_env.get("MAPI_REMOTE_OWNER_LOGIN"):
            owner_login = input("Polaris owner login (owner): ").strip() or "owner"
        if not owner_password_hash:
            import getpass
            from app.runtime.owner_credentials import hash_owner_password

            first = getpass.getpass("Polaris owner password: ")
            second = getpass.getpass("Repeat owner password: ")
            if first != second:
                print(json.dumps({"status": "error", "error": "owner_password_confirmation_mismatch"}, indent=2))
                raise SystemExit(2)
            owner_password_hash = hash_owner_password(first)
            first = ""
            second = ""

    install_service = bool(args.install_service)
    if mode != "local" and not args.install_service and not args.no_install_service and interactive:
        from mapi.system_install import systemd_available

        if systemd_available():
            answer = input("Install and start MAPI as a systemd service now? [Y/n]: ").strip().casefold()
            install_service = answer not in {"n", "no", "nie"}

    options = InitOptions(
        root=args.root,
        mode=mode,
        owner_key=owner,
        agent_subject_key=subject,
        agent_display_name=display_name,
        agent_project_key=project,
        port=port,
        profile=profile,
        public_url=public_url,
        oauth_client_id=args.oauth_client_id or existing_env.get("MAPI_REMOTE_OAUTH_CLIENT_ID") or "chatgpt-private",
        oauth_redirect_uris=tuple(redirects),
        owner_login=owner_login,
        owner_password_hash=owner_password_hash,
        service_user=args.service_user,
        service_name=service_name,
        recovery_command_json=args.recovery_command_json or existing_env.get("MAPI_RECOVERY_COMMAND_JSON"),
        resume=bool(args.resume),
        seed_self=not bool(args.no_self_seed),
        install_service=install_service,
        allow_sudo_prompt=interactive,
        verify_endpoint=not bool(args.no_verify_endpoint),
    )
    try:
        result = initialize_instance(options)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "schema": "mapi_instance_init.v1", "error": str(exc)}, indent=2))
        raise SystemExit(2) from None
    connection = dict(result.get("connection") or {})
    recommended = connection.get("recommended_mcp_url")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"MAPI init status: {result.get('status')}")
        print(f"Instance root: {result.get('root')}")
        print(f"Database: {result.get('database')}")
        print(f"Doctor: {result.get('doctor_status')}")
        service = dict(result.get("system_service") or {})
        if service.get("status") != "not_requested":
            print(f"System service: {service.get('status')}")
        if connection.get("public_mcp_url") and connection.get("loopback_mcp_url") != recommended:
            print(f"Local loopback: {connection.get('loopback_mcp_url')}")
        print(f"Endpoint status: {connection.get('status', 'configured')}")
        if recommended:
            print(f"MAPI MCP address: {recommended}")
    if result.get("status") == "blocked":
        raise SystemExit(2)

def migrate() -> None:
    path = _database_path()
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        applied = db_migrations.apply_all_migrations(connection)
        connection.commit()
        versions = sorted(db_migrations.applied_migration_versions(connection))
    print(
        json.dumps(
            {
                "status": "ok",
                "database": str(path),
                "applied_now": applied,
                "migration_tail": versions[-1] if versions else None,
            },
            indent=2,
        )
    )


def doctor() -> None:
    _apply_runtime_cli_environment()
    from app.runtime.doctor import collect_doctor_report

    deep = "--deep" in sys.argv[1:]
    result = collect_doctor_report(deep=deep)
    print(json.dumps(result, indent=2))
    if result["status"] == "BLOCKED":
        raise SystemExit(2)


def recover() -> None:
    _apply_runtime_cli_environment()
    from app.runtime.recovery import recover_runtime

    execute = "--execute" in sys.argv[1:]
    result = recover_runtime(execute=execute)
    print(json.dumps(result, indent=2))
    if result.get("status") in {"error", "attention"}:
        raise SystemExit(2)


def seed_demo() -> None:
    from mapi.seed import seed_demo_database

    print(json.dumps(seed_demo_database(_database_path()), indent=2))


def demo() -> None:
    from mapi.demo import run_isolated_demo

    result = run_isolated_demo()
    print(result["human_output"])


def server() -> None:
    _apply_runtime_cli_environment()
    os.environ.setdefault("MCP_SURFACE_PROFILE", "agent")
    os.environ.setdefault("MAPI_RUNTIME_HOST", "127.0.0.1")
    from mapi_platform.network import mcp_connection_urls

    urls = mcp_connection_urls(
        public_origin=os.environ.get("MAPI_REMOTE_BASE_URL"),
        port=int(os.environ.get("MAPI_RUNTIME_PORT", "8015")),
    )
    print(f"MAPI MCP address: {urls['recommended_mcp_url']}")
    if urls.get("public_mcp_url"):
        print(f"Local loopback: {urls['loopback_mcp_url']}")
    from app.runtime.server_runtime import run_server

    run_server()



def _main_help() -> str:
    return """MAPI

Usage:
  mapi init [options]
  mapi start [--root PATH]
  mapi doctor [--root PATH] [--deep]
  mapi migrate [--root PATH]
  mapi recover [--root PATH] [--execute]
  mapi maintenance --root PATH [--apply-safe-metadata] [--json]
  mapi capabilities
  mapi version
"""


def main() -> None:
    import importlib.metadata

    argv = list(sys.argv[1:])
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(_main_help())
        return
    command = argv.pop(0)
    if command in {"-V", "--version", "version"}:
        try:
            print(importlib.metadata.version("mapi-agent-memory"))
        except importlib.metadata.PackageNotFoundError:
            print("source-checkout")
        return
    if command == "maintenance":
        from mapi.maintenance import main as maintenance_main

        raise SystemExit(maintenance_main(argv))
    if command == "capabilities":
        from mapi.capabilities import main as capabilities_main

        if argv:
            raise SystemExit("mapi capabilities takes no arguments")
        capabilities_main()
        return
    handlers = {
        "init": init,
        "start": server,
        "server": server,
        "doctor": doctor,
        "migrate": migrate,
        "recover": recover,
        "seed-demo": seed_demo,
        "demo": demo,
    }
    handler = handlers.get(command)
    if handler is None:
        print(f"unknown command: {command}", file=sys.stderr)
        print(_main_help(), file=sys.stderr)
        raise SystemExit(2)
    sys.argv = [f"mapi {command}", *argv]
    handler()
