from __future__ import annotations

"""Runtime identity, readiness and stale-runtime mutation guards."""

import hashlib
import importlib.metadata
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from app.runtime.context import get_runtime_context, runtime_data_dir, runtime_db_path, runtime_root
from app.runtime.private_mode import private_owner_key, runtime_mode
from app.runtime.writer_guard import mutation_writer_guard, writer_guard_status
from app.workshops.access_policy import canonical_profile_token

RUNTIME_METADATA_SCHEMA = "mapi_runtime_metadata.v1"
RUNTIME_READINESS_SCHEMA = "mapi_runtime_readiness.v1"
REGISTRY_VERSION = "mapi_workshop_registry.v9"
FRESHNESS_CONTRACT_VERSION = "mapi_runtime_freshness.v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8015
ARTIFACT_DISTRIBUTION_NAME = "mapi-agent-memory"

_LOCK = RLock()
_RUNTIME_METADATA: dict[str, Any] | None = None
MAX_REPOSITORY_PATHS = 20
ALLOWLISTED_UNTRACKED_PATHS = frozenset(
    {
        "data/still-me-portrait-project.zip",
        "data/still-me-portrait-project.zip.b64",
    }
)

# Local admin actions required to repair, test, commit and restart a stale runtime.
STALE_ADMIN_RECOVERY_ACTIONS = frozenset(
    {
        "db_info",
        "read_file",
        "write_file",
        "insert_before_marker",
        "insert_after_marker",
        "replace_once",
        "delete_path",
        "run_shell",
        "run_powershell",
        "run_pytest",
        "git_status",
        "git_commit",
        "git_push",
    }
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256_json(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_git(root: Path, *args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return int(result.returncode), str(result.stdout or "").rstrip()


def _porcelain_paths(status: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in status.splitlines():
        if len(raw) < 4:
            continue
        code = raw[:2]
        path = raw[3:].strip().replace("\\", "/")
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        tracked = code != "??"
        items.append(
            {
                "code": code,
                "path": path,
                "tracked": tracked,
                "allowlisted": (not tracked and path in ALLOWLISTED_UNTRACKED_PATHS),
            }
        )
    return items


def _worktree_inventory(output: str) -> list[dict[str, Any]]:
    worktrees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in output.splitlines() + [""]:
        if not line.strip():
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value.replace("\\", "/")
        elif key == "HEAD":
            current["head"] = value or None
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key in {"detached", "bare", "prunable"}:
            current[key] = True
    return worktrees


def repository_state(root: Path | None = None) -> dict[str, Any]:
    configured = str(os.environ.get("MAPI_REPOSITORY_ROOT") or "").strip()
    resolved_root = Path(root or configured or runtime_root()).resolve()
    head_code, head = _run_git(resolved_root, "rev-parse", "HEAD")
    tracked_code, tracked_status = _run_git(
        resolved_root, "status", "--porcelain=v1", "--untracked-files=no"
    )
    all_code, all_status = _run_git(
        resolved_root, "status", "--porcelain=v1", "--untracked-files=all"
    )
    worktree_code, worktree_output = _run_git(
        resolved_root, "worktree", "list", "--porcelain"
    )
    entries = _porcelain_paths(all_status) if all_code == 0 else []
    tracked_paths = [item["path"] for item in entries if item["tracked"]]
    untracked = [item for item in entries if not item["tracked"]]
    return {
        "root": str(resolved_root),
        "head": head if head_code == 0 and head else None,
        "dirty": bool(tracked_status) if tracked_code == 0 else None,
        "git_available": head_code == 0 and tracked_code == 0 and all_code == 0,
        "tracked_paths": tracked_paths[:MAX_REPOSITORY_PATHS],
        "untracked_paths": [item["path"] for item in untracked][:MAX_REPOSITORY_PATHS],
        "allowlisted_untracked_paths": [
            item["path"] for item in untracked if item["allowlisted"]
        ][:MAX_REPOSITORY_PATHS],
        "non_allowlisted_untracked_paths": [
            item["path"] for item in untracked if not item["allowlisted"]
        ][:MAX_REPOSITORY_PATHS],
        "paths_truncated": len(entries) > MAX_REPOSITORY_PATHS,
        "worktrees": _worktree_inventory(worktree_output) if worktree_code == 0 else [],
    }


def artifact_state() -> dict[str, Any]:
    """Return immutable installed-distribution provenance when MAPI is running from a wheel.

    The RECORD fingerprint changes on a package upgrade/reinstall, so a running process can
    detect that its installed artifact changed underneath it and require a restart.
    """
    try:
        distribution = importlib.metadata.distribution(ARTIFACT_DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return {
            "available": False,
            "distribution": ARTIFACT_DISTRIBUTION_NAME,
            "version": None,
            "fingerprint": None,
            "reason": "distribution_not_installed",
        }
    version = str(distribution.version or "").strip() or None
    record = distribution.read_text("RECORD")
    if not record:
        return {
            "available": False,
            "distribution": ARTIFACT_DISTRIBUTION_NAME,
            "version": version,
            "fingerprint": None,
            "reason": "distribution_record_missing",
        }
    record_sha256 = hashlib.sha256(record.encode("utf-8")).hexdigest()
    fingerprint = _sha256_json(
        {
            "distribution": ARTIFACT_DISTRIBUTION_NAME,
            "version": version,
            "record_sha256": record_sha256,
        }
    )
    return {
        "available": True,
        "distribution": ARTIFACT_DISTRIBUTION_NAME,
        "version": version,
        "record_sha256": record_sha256,
        "fingerprint": fingerprint,
        "reason": None,
    }


def runtime_provenance_state() -> dict[str, Any]:
    repository = repository_state()
    if repository.get("git_available"):
        return {"mode": "source", "repository": repository, "artifact": None}
    artifact = artifact_state()
    if artifact.get("available"):
        return {"mode": "artifact", "repository": repository, "artifact": artifact}
    return {"mode": "unavailable", "repository": repository, "artifact": artifact}


def schema_tail(db_path: Path | None = None) -> str | None:
    path = Path(db_path or runtime_db_path()).resolve()
    if not path.exists():
        return None
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY applied_at DESC, version DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] is not None else None


def registry_contract() -> dict[str, Any]:
    from app.workshops.catalog import WORKSHOPS

    actions: list[dict[str, Any]] = []
    for workshop in WORKSHOPS.values():
        for action in workshop.actions:
            actions.append(
                {
                    "area": workshop.area,
                    "action": action.action,
                    "tool_name": action.tool_name,
                    "requirement": action.min_profile,
                    "risk_class": action.risk_class,
                    "backup_required": action.backup_required,
                    "payload_schema": action.payload_schema or {},
                }
            )
    payload = {
        "version": REGISTRY_VERSION,
        "workshop_count": len(WORKSHOPS),
        "action_count": len(actions),
        "actions": actions,
    }
    return {
        "version": REGISTRY_VERSION,
        "workshop_count": len(WORKSHOPS),
        "action_count": len(actions),
        "fingerprint": _sha256_json(payload),
    }


def runtime_config_contract(*, profile: str | None = None) -> dict[str, Any]:
    from app.runtime.remote_auth_config import RemoteAuthConfig

    context = get_runtime_context()
    resolved_profile = canonical_profile_token(profile or os.environ.get("MCP_SURFACE_PROFILE"))
    remote = RemoteAuthConfig.from_env()
    redirect_hashes = [
        hashlib.sha256(uri.encode("utf-8")).hexdigest()
        for uri in remote.oauth_redirect_uris
    ]
    payload = {
        "root": str(context.root),
        "data_dir": str(context.data_dir),
        "db_path": str(context.db_path),
        "profile": resolved_profile,
        "host": os.environ.get("MAPI_RUNTIME_HOST", DEFAULT_HOST),
        "port": int(os.environ.get("MAPI_RUNTIME_PORT", str(DEFAULT_PORT))),
        "runtime_mode": runtime_mode(),
        "owner_key": private_owner_key(),
        "writer_guard": {
            "enabled": str(os.environ.get("MAPI_WRITER_GUARD_ENABLED", "0")).strip().lower()
            in {"1", "true", "yes", "on"},
            "mode": str(os.environ.get("MAPI_WRITER_MODE", "active")).strip().lower(),
            "instance_key": str(os.environ.get("MAPI_WRITER_INSTANCE_KEY", "")).strip() or None,
            "lock_path": str(os.environ.get("MAPI_WRITER_LOCK_PATH", "")).strip() or None,
        },
        "remote_auth": {
            "enabled": remote.enabled,
            "base_url": remote.base_url,
            "oauth_client_id": remote.oauth_client_id,
            "redirect_uri_hashes": redirect_hashes,
            "owner_login": remote.owner_login,
            "owner_password_hash_fingerprint": (
                hashlib.sha256(remote.owner_password_hash.encode("utf-8")).hexdigest()
                if remote.owner_password_hash
                else None
            ),
            "access_ttl_seconds": remote.access_ttl_seconds,
            "refresh_ttl_seconds": remote.refresh_ttl_seconds,
            "authorization_code_ttl_seconds": remote.authorization_code_ttl_seconds,
            "rate_limit_window_seconds": remote.rate_limit_window_seconds,
            "rate_limit_max_attempts": remote.rate_limit_max_attempts,
        },
    }
    return {"values": payload, "fingerprint": _sha256_json(payload)}


def runtime_metadata_path() -> Path:
    return runtime_data_dir() / "runtime" / "mapi-runtime.json"


def launcher_record_path() -> Path:
    return runtime_data_dir() / "runtime" / "mapi-launcher.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def freshness_enforcement_enabled() -> bool:
    raw = str(os.environ.get("MAPI_RUNTIME_ENFORCE_FRESHNESS", "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def initialize_runtime_metadata(*, force: bool = False) -> dict[str, Any]:
    global _RUNTIME_METADATA
    with _LOCK:
        if _RUNTIME_METADATA is not None and not force:
            return dict(_RUNTIME_METADATA)

        provenance = runtime_provenance_state()
        repo = provenance["repository"]
        artifact = provenance.get("artifact") or {}
        provenance_mode = str(provenance.get("mode") or "unavailable")
        registry = registry_contract()
        config = runtime_config_contract()
        metadata = {
            "schema": RUNTIME_METADATA_SCHEMA,
            "freshness_contract_version": FRESHNESS_CONTRACT_VERSION,
            "started_at": _utc_now(),
            "pid": os.getpid(),
            "python_executable": sys.executable,
            "instance_token": os.environ.get("MAPI_RUNTIME_INSTANCE_TOKEN") or uuid.uuid4().hex,
            "provenance_mode": provenance_mode,
            "expected_commit": (os.environ.get("MAPI_EXPECTED_COMMIT") or None) if provenance_mode == "source" else None,
            "commit_sha": repo.get("head") if provenance_mode == "source" else None,
            "dirty_at_start": repo.get("dirty") if provenance_mode == "source" else None,
            "artifact_distribution": artifact.get("distribution") if provenance_mode == "artifact" else None,
            "artifact_version": artifact.get("version") if provenance_mode == "artifact" else None,
            "artifact_fingerprint": artifact.get("fingerprint") if provenance_mode == "artifact" else None,
            "registry_version": registry["version"],
            "registry_fingerprint": registry["fingerprint"],
            "registry_action_count": registry["action_count"],
            "schema_tail": schema_tail(),
            "config_fingerprint": config["fingerprint"],
            "profile": config["values"]["profile"],
            "host": config["values"]["host"],
            "port": config["values"]["port"],
            "runtime_mode": config["values"]["runtime_mode"],
            "owner_key": config["values"]["owner_key"],
            "enforcement_enabled": freshness_enforcement_enabled(),
        }
        _RUNTIME_METADATA = metadata
        try:
            _write_json_atomic(runtime_metadata_path(), metadata)
        except OSError:
            pass
        return dict(metadata)


def runtime_metadata() -> dict[str, Any]:
    return initialize_runtime_metadata()


def _launcher_contract(metadata: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    if metadata.get("provenance_mode") == "artifact":
        # An installed wheel is launched directly by its console entry point. Its immutable
        # distribution fingerprint replaces the source-checkout launcher/commit contract.
        return None, reasons
    record = _read_json(launcher_record_path())
    if not freshness_enforcement_enabled():
        return record, reasons
    if record is None:
        return None, ["launcher_record_missing"]
    if record.get("instance_token") != metadata.get("instance_token"):
        reasons.append("launcher_instance_mismatch")
    if record.get("expected_commit") != metadata.get("expected_commit"):
        reasons.append("launcher_expected_commit_mismatch")
    if record.get("status") != "ready":
        reasons.append("launcher_not_ready")
    if record.get("runtime_mode") != metadata.get("runtime_mode"):
        reasons.append("launcher_runtime_mode_mismatch")
    if record.get("owner_key") != metadata.get("owner_key"):
        reasons.append("launcher_owner_key_mismatch")
    runtime_pid = record.get("runtime_pid")
    if runtime_pid is not None and int(runtime_pid) != os.getpid():
        reasons.append("launcher_runtime_pid_mismatch")
    return record, reasons


def get_runtime_readiness(include_debug: bool = False) -> dict[str, Any]:
    metadata = runtime_metadata()
    repo = repository_state()
    registry = registry_contract()
    config = runtime_config_contract()
    current_schema_tail = schema_tail()
    launcher, reasons = _launcher_contract(metadata)

    expected_commit = metadata.get("expected_commit")
    runtime_commit = metadata.get("commit_sha")
    repo_head = repo.get("head")
    provenance_mode = str(metadata.get("provenance_mode") or "unavailable")
    current_artifact = artifact_state() if provenance_mode == "artifact" else None

    if provenance_mode == "source":
        if not repo.get("git_available"):
            reasons.append("repository_state_unavailable")
        if runtime_commit is None:
            reasons.append("runtime_commit_missing")
        if repo_head is None:
            reasons.append("repository_head_missing")
        if runtime_commit and repo_head and runtime_commit != repo_head:
            reasons.append("runtime_commit_mismatch")
        if expected_commit and runtime_commit != expected_commit:
            reasons.append("runtime_expected_commit_mismatch")
        if freshness_enforcement_enabled() and not expected_commit:
            reasons.append("expected_commit_missing")
        if metadata.get("dirty_at_start") is True:
            reasons.append("runtime_started_dirty")
        if repo.get("dirty") is True:
            reasons.append("repository_dirty")
    elif provenance_mode == "artifact":
        runtime_artifact = metadata.get("artifact_fingerprint")
        if not current_artifact or not current_artifact.get("available"):
            reasons.append("artifact_state_unavailable")
        if not runtime_artifact:
            reasons.append("runtime_artifact_fingerprint_missing")
        current_fingerprint = (current_artifact or {}).get("fingerprint")
        if runtime_artifact and current_fingerprint and runtime_artifact != current_fingerprint:
            reasons.append("artifact_fingerprint_mismatch")
    elif freshness_enforcement_enabled():
        reasons.append("runtime_provenance_unavailable")
    if metadata.get("registry_fingerprint") != registry.get("fingerprint"):
        reasons.append("registry_fingerprint_mismatch")
    if metadata.get("schema_tail") != current_schema_tail:
        reasons.append("schema_tail_mismatch")
    if metadata.get("config_fingerprint") != config.get("fingerprint"):
        reasons.append("config_fingerprint_mismatch")

    unique_reasons = sorted(set(reasons))
    ready = not unique_reasons
    payload: dict[str, Any] = {
        "status": "ready" if ready else "stale",
        "schema": RUNTIME_READINESS_SCHEMA,
        "freshness_contract_version": FRESHNESS_CONTRACT_VERSION,
        "enforcement_enabled": freshness_enforcement_enabled(),
        "mutations_allowed": ready,
        "reason_codes": unique_reasons,
        "runtime": {
            "provenance_mode": provenance_mode,
            "commit_sha": runtime_commit,
            "dirty_at_start": metadata.get("dirty_at_start"),
            "artifact_distribution": metadata.get("artifact_distribution"),
            "artifact_version": metadata.get("artifact_version"),
            "artifact_fingerprint": metadata.get("artifact_fingerprint"),
            "started_at": metadata.get("started_at"),
            "pid": metadata.get("pid"),
            "instance_token": metadata.get("instance_token"),
            "profile": metadata.get("profile"),
            "registry_version": metadata.get("registry_version"),
            "registry_fingerprint": metadata.get("registry_fingerprint"),
            "schema_tail": metadata.get("schema_tail"),
            "config_fingerprint": metadata.get("config_fingerprint"),
            "runtime_mode": metadata.get("runtime_mode"),
            "owner_key": metadata.get("owner_key"),
        },
        "artifact": {
            "available": bool((current_artifact or {}).get("available")) if provenance_mode == "artifact" else False,
            "distribution": (current_artifact or {}).get("distribution") if provenance_mode == "artifact" else None,
            "version": (current_artifact or {}).get("version") if provenance_mode == "artifact" else None,
            "fingerprint": (current_artifact or {}).get("fingerprint") if provenance_mode == "artifact" else None,
        },
        "repository": {
            "head": repo_head,
            "dirty": repo.get("dirty"),
            "tracked_paths": list(repo.get("tracked_paths") or [])[:MAX_REPOSITORY_PATHS],
            "untracked_paths": list(repo.get("untracked_paths") or [])[:MAX_REPOSITORY_PATHS],
            "allowlisted_untracked_paths": list(
                repo.get("allowlisted_untracked_paths") or []
            )[:MAX_REPOSITORY_PATHS],
            "non_allowlisted_untracked_paths": list(
                repo.get("non_allowlisted_untracked_paths") or []
            )[:MAX_REPOSITORY_PATHS],
            "paths_truncated": bool(repo.get("paths_truncated")),
            "worktrees": list(repo.get("worktrees") or []),
            "guidance": (
                {
                    "action": "move_wip_to_dedicated_worktree",
                    "message": "Move tracked WIP to a dedicated Git worktree; do not disable freshness.",
                }
                if provenance_mode == "source" and repo.get("dirty")
                else None
            ),
        },
        "current_contract": {
            "registry_version": registry.get("version"),
            "registry_fingerprint": registry.get("fingerprint"),
            "registry_action_count": registry.get("action_count"),
            "schema_tail": current_schema_tail,
            "config_fingerprint": config.get("fingerprint"),
            "runtime_mode": config["values"].get("runtime_mode"),
            "owner_key": config["values"].get("owner_key"),
        },
    }
    if include_debug:
        payload["launcher"] = launcher
        payload["metadata_path"] = str(runtime_metadata_path())
        payload["launcher_record_path"] = str(launcher_record_path())
    return payload


def action_requires_fresh_runtime(
    *,
    area: str,
    action: str,
    risk_class: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    if risk_class == "R0":
        return False
    if area == "admin" and action in STALE_ADMIN_RECOVERY_ACTIONS:
        return False
    if area == "admin" and action == "query_sql":
        return bool((payload or {}).get("allow_write"))
    return True


def mutation_freshness_guard(
    *,
    area: str,
    action: str,
    risk_class: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    required = action_requires_fresh_runtime(
        area=area,
        action=action,
        risk_class=risk_class,
        payload=payload,
    )
    writer = mutation_writer_guard(required=required)
    if not required:
        return {"allowed": True, "required": False, "reason_codes": []}
    if not writer.get("allowed"):
        return {
            **writer,
            "allowed": False,
        }
    if not freshness_enforcement_enabled():
        return {
            "allowed": True,
            "required": True,
            "enforcement_enabled": False,
            "reason_codes": [],
            "writer_guard": writer_guard_status(),
        }
    readiness = get_runtime_readiness(include_debug=False)
    repository = readiness.get("repository") or {}
    return {
        "allowed": bool(readiness.get("mutations_allowed")),
        "required": True,
        "enforcement_enabled": True,
        "reason_codes": list(readiness.get("reason_codes") or []),
        "runtime_commit": (readiness.get("runtime") or {}).get("commit_sha"),
        "repository_head": repository.get("head"),
        "repository_details": {
            "tracked_paths": list(repository.get("tracked_paths") or [])[:MAX_REPOSITORY_PATHS],
            "untracked_paths": list(repository.get("untracked_paths") or [])[:MAX_REPOSITORY_PATHS],
            "allowlisted_untracked_paths": list(
                repository.get("allowlisted_untracked_paths") or []
            )[:MAX_REPOSITORY_PATHS],
            "non_allowlisted_untracked_paths": list(
                repository.get("non_allowlisted_untracked_paths") or []
            )[:MAX_REPOSITORY_PATHS],
            "paths_truncated": bool(repository.get("paths_truncated")),
            "worktrees": list(repository.get("worktrees") or []),
            "guidance": repository.get("guidance"),
        },
        "writer_guard": writer_guard_status(),
    }


def reset_runtime_metadata_for_tests() -> None:
    global _RUNTIME_METADATA
    with _LOCK:
        _RUNTIME_METADATA = None
