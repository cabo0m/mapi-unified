from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, BinaryIO

from .command_config import CommandCapabilityConfig, CommandRecipe
from mapi_core.core.time_score import utc_now_iso
from mapi_core.memory.sensitivity import capture_sensitivity_gate
from .store import CapabilityStore, row_to_dict
from mapi_platform.processes import popen_platform_kwargs, terminate_process_tree

COMMAND_LIST_SCHEMA = "mapi_public_command_recipes.v1"
COMMAND_PREVIEW_SCHEMA = "mapi_public_command_preview.v1"
COMMAND_RUN_SCHEMA = "mapi_public_command_run.v1"
COMMAND_RUNS_SCHEMA = "mapi_public_command_runs.v1"

_BASE_ENV_KEYS = (
    "SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP",
    "HOME", "USERPROFILE", "LANG", "LC_ALL", "TERM",
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(raw)


def _project(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _public_run(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id", "run_key", "project_key", "recipe_id", "recipe_name", "status", "preview_hash",
        "recipe_fingerprint", "exit_code", "stdout_sha256", "stderr_sha256", "stdout_bytes",
        "stderr_bytes", "output_truncated", "output_blocked", "started_at", "completed_at",
        "duration_ms", "created_at",
    )
    result = {field: row.get(field) for field in fields}
    result["output_truncated"] = bool(result.get("output_truncated"))
    result["output_blocked"] = bool(result.get("output_blocked"))
    return result


@dataclass
class _CapturedStream:
    limit: int
    digest: Any
    total_bytes: int = 0
    prefix: bytearray | None = None

    def __post_init__(self) -> None:
        self.prefix = bytearray()

    def consume(self, stream: BinaryIO) -> None:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            self.digest.update(chunk)
            self.total_bytes += len(chunk)
            assert self.prefix is not None
            remaining = self.limit - len(self.prefix)
            if remaining > 0:
                self.prefix.extend(chunk[:remaining])

    def result(self) -> tuple[str, int, bytes, bool]:
        assert self.prefix is not None
        return self.digest.hexdigest(), self.total_bytes, bytes(self.prefix), self.total_bytes > len(self.prefix)


class CommandRecipeService:
    def __init__(self, config: CommandCapabilityConfig, store: CapabilityStore) -> None:
        errors = config.validation_errors()
        if not config.enabled:
            raise ValueError("command_capability_disabled")
        if errors:
            raise ValueError("command_capability_invalid:" + ",".join(errors))
        self.config = config
        self.store = store
        self._lock = threading.RLock()

    def _recipe(self, recipe_id: str, project_key: str | None) -> CommandRecipe:
        recipe = self.config.recipe(recipe_id, project_key)
        if recipe is None:
            raise ValueError("command_recipe_not_bound_to_project")
        return recipe

    def _resolved_argv(self, recipe: CommandRecipe) -> tuple[str, ...]:
        executable = recipe.argv[0]
        if os.path.isabs(executable) or any(sep in executable for sep in ("/", "\\")):
            candidate = recipe.workdir / executable if not os.path.isabs(executable) else recipe.workdir.__class__(executable)
            resolved = candidate.resolve(strict=True)
        else:
            found = shutil.which(executable)
            if not found:
                raise ValueError("command_executable_not_found")
            resolved = recipe.workdir.__class__(found).resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("command_executable_not_file")
        return (str(resolved), *recipe.argv[1:])

    def _file_sha256(self, path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _argument_file_hashes(self, recipe: CommandRecipe, resolved_argv: tuple[str, ...]) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for index, raw in enumerate(resolved_argv[1:], start=1):
            if not raw or raw.startswith("-"):
                continue
            candidate = recipe.workdir.__class__(raw)
            if not candidate.is_absolute():
                candidate = recipe.workdir / candidate
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_file():
                hashes[str(index)] = self._file_sha256(str(resolved))
        return hashes

    def _environment(self, recipe: CommandRecipe) -> tuple[dict[str, str], dict[str, str]]:
        env: dict[str, str] = {}
        for key in _BASE_ENV_KEYS:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        for key in recipe.env_allowlist:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        env_hashes = {key: _sha256(value.encode("utf-8")) for key, value in sorted(env.items())}
        return env, env_hashes

    def _recipe_fingerprint(self, recipe: CommandRecipe, resolved_argv: tuple[str, ...], env_hashes: dict[str, str]) -> str:
        return _canonical_hash({
            **recipe.fingerprint_payload(),
            "resolved_executable": resolved_argv[0],
            "resolved_executable_sha256": self._file_sha256(resolved_argv[0]),
            "argument_file_hashes": self._argument_file_hashes(recipe, resolved_argv),
            "env_value_hashes": env_hashes,
        })

    def list_recipes(self, *, project_key: str | None) -> dict[str, Any]:
        recipes = [recipe.public_dict() for recipe in self.config.recipes_for_project(project_key)]
        return {"status": "ok", "schema": COMMAND_LIST_SCHEMA, "count": len(recipes), "recipes": recipes}

    def preview(self, *, project_key: str | None, recipe_id: str) -> dict[str, Any]:
        try:
            recipe = self._recipe(recipe_id, project_key)
            resolved_argv = self._resolved_argv(recipe)
            _env, env_hashes = self._environment(recipe)
        except (ValueError, OSError) as exc:
            return {"status": "denied", "schema": COMMAND_PREVIEW_SCHEMA, "error": str(exc)}
        recipe_fingerprint = self._recipe_fingerprint(recipe, resolved_argv, env_hashes)
        payload = {
            "project_key": _project(project_key),
            "recipe_id": recipe.recipe_id,
            "recipe_fingerprint": recipe_fingerprint,
        }
        return {
            "status": "preview_ready",
            "schema": COMMAND_PREVIEW_SCHEMA,
            "preview_hash": _canonical_hash(payload),
            "recipe": recipe.public_dict(),
            "safety": {
                "read_only": True,
                "processes_started": 0,
                "shell_used": False,
                "caller_arguments_allowed": False,
                "environment_mode": "base_plus_explicit_allowlist",
                "arbitrary_environment_inherited": False,
                "explicit_env_allowlist_count": len(recipe.env_allowlist),
            },
        }

    def _decode_public_output(self, raw: bytes) -> tuple[str, bool]:
        try:
            text = raw.decode("utf-8")
            invalid = "\x00" in text
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            invalid = True
        return text.replace("\r\n", "\n"), invalid

    def _start_run(
        self, *, project_key: str | None, recipe: CommandRecipe, preview_hash: str,
        recipe_fingerprint: str, started_at: str,
    ) -> dict[str, Any]:
        run_key = secrets.token_hex(16)
        empty_sha = _sha256(b"")
        with self.store.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO command_recipe_runs (
                    run_key,project_key,recipe_id,recipe_name,status,preview_hash,recipe_fingerprint,exit_code,
                    stdout_sha256,stderr_sha256,stdout_bytes,stderr_bytes,output_truncated,output_blocked,
                    started_at,completed_at,duration_ms,created_at
                ) VALUES (?,?,?,?, 'running', ?,?,NULL,?,?,0,0,0,0,?,NULL,NULL,?)
                """,
                (
                    run_key, _project(project_key), recipe.recipe_id, recipe.name, preview_hash,
                    recipe_fingerprint, empty_sha, empty_sha, started_at, started_at,
                ),
            )
            run_id = int(cursor.lastrowid)
            conn.commit()
            row = row_to_dict(conn.execute("SELECT * FROM command_recipe_runs WHERE id=?", (run_id,)).fetchone()) or {}
        return _public_run(row)

    def _finish_run(
        self, *, run_id: int, status: str, exit_code: int | None, stdout_sha: str, stderr_sha: str,
        stdout_bytes: int, stderr_bytes: int, output_truncated: bool, output_blocked: bool,
        completed_at: str, duration_ms: int,
    ) -> dict[str, Any]:
        with self.store.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE command_recipe_runs
                SET status=?,exit_code=?,stdout_sha256=?,stderr_sha256=?,stdout_bytes=?,stderr_bytes=?,
                    output_truncated=?,output_blocked=?,completed_at=?,duration_ms=?
                WHERE id=? AND status='running'
                """,
                (
                    status, exit_code, stdout_sha, stderr_sha, int(stdout_bytes), int(stderr_bytes),
                    int(output_truncated), int(output_blocked), completed_at, int(duration_ms), int(run_id),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("command_run_audit_state_changed")
            conn.commit()
            row = row_to_dict(conn.execute("SELECT * FROM command_recipe_runs WHERE id=?", (int(run_id),)).fetchone()) or {}
        return _public_run(row)

    def run(
        self, *, project_key: str | None, recipe_id: str, expected_preview_hash: str, confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            return {"status": "confirmation_required", "schema": COMMAND_RUN_SCHEMA}
        with self._lock:
            preview = self.preview(project_key=project_key, recipe_id=recipe_id)
            if preview.get("status") != "preview_ready":
                return {**preview, "schema": COMMAND_RUN_SCHEMA}
            if str(expected_preview_hash or "") != str(preview["preview_hash"]):
                return {
                    "status": "stale_preview", "schema": COMMAND_RUN_SCHEMA,
                    "expected_preview_hash": str(expected_preview_hash or ""),
                    "current_preview_hash": preview["preview_hash"],
                }
            try:
                recipe = self._recipe(recipe_id, project_key)
                resolved_argv = self._resolved_argv(recipe)
                env, env_hashes = self._environment(recipe)
            except (ValueError, OSError) as exc:
                return {"status": "stale_preview", "schema": COMMAND_RUN_SCHEMA, "error": str(exc)}
            recipe_fingerprint = self._recipe_fingerprint(recipe, resolved_argv, env_hashes)
            started_at = utc_now_iso()
            try:
                running = self._start_run(
                    project_key=project_key, recipe=recipe, preview_hash=str(preview["preview_hash"]),
                    recipe_fingerprint=recipe_fingerprint, started_at=started_at,
                )
            except Exception:
                return {"status": "error", "schema": COMMAND_RUN_SCHEMA, "error": "command_audit_preflight_failed"}
            run_id = int(running["id"])
            started_clock = time.monotonic()
            out_capture = _CapturedStream(self.config.max_output_bytes, hashlib.sha256())
            err_capture = _CapturedStream(self.config.max_output_bytes, hashlib.sha256())
            timed_out = False
            try:
                process = subprocess.Popen(
                    list(resolved_argv), cwd=str(recipe.workdir), env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                    **popen_platform_kwargs(),
                )
                assert process.stdout is not None and process.stderr is not None
                out_thread = threading.Thread(target=out_capture.consume, args=(process.stdout,), daemon=True)
                err_thread = threading.Thread(target=err_capture.consume, args=(process.stderr,), daemon=True)
                out_thread.start(); err_thread.start()
                try:
                    exit_code = process.wait(timeout=recipe.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_process_tree(process)
                    exit_code = None
                    process.wait(timeout=10)
                out_thread.join(timeout=10); err_thread.join(timeout=10)
                process.stdout.close()
                process.stderr.close()
            except OSError as exc:
                completed_at = utc_now_iso()
                duration_ms = max(0, int((time.monotonic() - started_clock) * 1000))
                try:
                    run = self._finish_run(
                        run_id=run_id, status="start_failed", exit_code=None, stdout_sha=_sha256(b""),
                        stderr_sha=_sha256(b""), stdout_bytes=0, stderr_bytes=0, output_truncated=False,
                        output_blocked=False, completed_at=completed_at, duration_ms=duration_ms,
                    )
                except Exception:
                    return {"status": "error", "schema": COMMAND_RUN_SCHEMA, "error": "command_start_failed_audit_update_failed", "run_id": run_id}
                return {
                    "status": "start_failed", "schema": COMMAND_RUN_SCHEMA,
                    "error": f"command_start_failed:{type(exc).__name__}", "run": run,
                }
            stdout_sha, stdout_bytes, stdout_prefix, stdout_truncated = out_capture.result()
            stderr_sha, stderr_bytes, stderr_prefix, stderr_truncated = err_capture.result()
            stdout_text, stdout_invalid = self._decode_public_output(stdout_prefix)
            stderr_text, stderr_invalid = self._decode_public_output(stderr_prefix)
            combined_gate = capture_sensitivity_gate(stdout_text + "\n" + stderr_text)
            invalid_output = stdout_invalid or stderr_invalid
            output_blocked = invalid_output or combined_gate.get("status") != "allowed"
            truncated = stdout_truncated or stderr_truncated
            status = "timeout" if timed_out else ("output_blocked" if output_blocked else ("completed" if exit_code == 0 else "failed"))
            completed_at = utc_now_iso()
            duration_ms = max(0, int((time.monotonic() - started_clock) * 1000))
            try:
                run = self._finish_run(
                    run_id=run_id, status=status, exit_code=exit_code, stdout_sha=stdout_sha, stderr_sha=stderr_sha,
                    stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes, output_truncated=truncated,
                    output_blocked=output_blocked, completed_at=completed_at, duration_ms=duration_ms,
                )
            except Exception:
                return {
                    "status": "error", "schema": COMMAND_RUN_SCHEMA,
                    "error": "command_result_audit_update_failed", "run_id": run_id,
                }
            result = {"status": status, "schema": COMMAND_RUN_SCHEMA, "run": run}
            if output_blocked:
                result["output"] = {
                    "blocked": True,
                    "reason": (
                        "binary_or_non_utf8_command_output_not_returned"
                        if invalid_output else "sensitive_command_output_not_returned"
                    ),
                    "sensitivity_class": None if invalid_output else combined_gate.get("sensitivity_class"),
                }
            else:
                result["output"] = {
                    "blocked": False,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                }
            return result

    def list_runs(self, *, project_key: str | None, recipe_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            return {"status": "error", "schema": COMMAND_RUNS_SCHEMA, "error": "limit_out_of_range"}
        sql = "SELECT * FROM command_recipe_runs WHERE COALESCE(project_key,'')=COALESCE(?, '')"
        params: list[Any] = [_project(project_key)]
        if recipe_id is not None:
            recipe = self.config.recipe(recipe_id, project_key)
            if recipe is None:
                return {"status": "denied", "schema": COMMAND_RUNS_SCHEMA, "error": "command_recipe_not_bound_to_project"}
            sql += " AND recipe_id=?"
            params.append(recipe.recipe_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self.store.connection() as conn:
            items = [_public_run(row_to_dict(row) or {}) for row in conn.execute(sql, params).fetchall()]
        return {"status": "ok", "schema": COMMAND_RUNS_SCHEMA, "count": len(items), "items": items}
