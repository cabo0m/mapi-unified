from __future__ import annotations

"""Portable service installation and endpoint readiness helpers for MAPI first-run."""

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

SYSTEMD_SERVICE_NAME = "mapi.service"
SYSTEMD_DESTINATION = Path("/etc/systemd/system/mapi.service")


def normalize_systemd_service_name(value: str | None) -> str:
    raw = str(value or "mapi").strip()
    if raw.endswith(".service"):
        raw = raw[:-8]
    if not raw or len(raw) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@-" for ch in raw):
        raise ValueError("invalid_systemd_service_name")
    return raw + ".service"


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def _default_runner(argv: Sequence[str]) -> CommandResult:
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=False,
    )
    return CommandResult(tuple(str(item) for item in argv), completed.returncode, completed.stdout, completed.stderr)


def systemd_available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("systemctl") is not None


def _privilege_prefix(*, allow_prompt: bool) -> list[str]:
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and int(geteuid()) == 0:
        return []
    sudo = shutil.which("sudo")
    if not sudo:
        raise RuntimeError("system_service_install_requires_root_or_sudo")
    return [sudo] if allow_prompt else [sudo, "-n"]


def install_systemd_service(
    unit_path: str | Path,
    *,
    service_name: str = SYSTEMD_SERVICE_NAME,
    allow_sudo_prompt: bool,
    runner: Callable[[Sequence[str]], CommandResult] = _default_runner,
) -> dict[str, Any]:
    source = Path(unit_path).expanduser().resolve()
    if not source.is_file():
        raise RuntimeError("generated_systemd_unit_missing")
    if not systemd_available():
        raise RuntimeError("systemd_not_available")
    prefix = _privilege_prefix(allow_prompt=allow_sudo_prompt)
    unit_name = normalize_systemd_service_name(service_name)
    destination = Path("/etc/systemd/system") / unit_name
    commands = [
        [*prefix, "install", "-m", "0644", str(source), str(destination)],
        [*prefix, "systemctl", "daemon-reload"],
        [*prefix, "systemctl", "enable", "--now", unit_name],
    ]
    results: list[dict[str, Any]] = []
    for argv in commands:
        result = runner(argv)
        results.append(
            {
                "argv": list(result.argv),
                "returncode": int(result.returncode),
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
            }
        )
        if result.returncode != 0:
            raise RuntimeError("systemd_install_failed:" + Path(argv[-1]).name)

    status = runner(["systemctl", "is-active", unit_name])
    active = status.returncode == 0 and status.stdout.strip() == "active"
    return {
        "status": "active" if active else "installed_not_active",
        "service_name": unit_name,
        "unit_destination": str(destination),
        "active": active,
        "commands": results,
        "status_stdout": status.stdout.strip(),
        "status_stderr": status.stderr.strip(),
    }



def maintenance_unit_names(service_name: str) -> tuple[str, str]:
    base = normalize_systemd_service_name(service_name)[:-8]
    return f"{base}-maintenance.service", f"{base}-maintenance.timer"


def install_systemd_maintenance_timer(
    service_unit_path: str | Path,
    timer_unit_path: str | Path,
    *,
    service_name: str,
    allow_sudo_prompt: bool,
    runner: Callable[[Sequence[str]], CommandResult] = _default_runner,
) -> dict[str, Any]:
    service_source = Path(service_unit_path).expanduser().resolve()
    timer_source = Path(timer_unit_path).expanduser().resolve()
    if not service_source.is_file() or not timer_source.is_file():
        raise RuntimeError("generated_maintenance_systemd_unit_missing")
    if not systemd_available():
        raise RuntimeError("systemd_not_available")
    prefix = _privilege_prefix(allow_prompt=allow_sudo_prompt)
    maintenance_service, maintenance_timer = maintenance_unit_names(service_name)
    service_destination = Path("/etc/systemd/system") / maintenance_service
    timer_destination = Path("/etc/systemd/system") / maintenance_timer
    commands = [
        [*prefix, "install", "-m", "0644", str(service_source), str(service_destination)],
        [*prefix, "install", "-m", "0644", str(timer_source), str(timer_destination)],
        [*prefix, "systemctl", "daemon-reload"],
        [*prefix, "systemctl", "enable", "--now", maintenance_timer],
    ]
    results: list[dict[str, Any]] = []
    for argv in commands:
        result = runner(argv)
        results.append({
            "argv": list(result.argv),
            "returncode": int(result.returncode),
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
        })
        if result.returncode != 0:
            raise RuntimeError("systemd_maintenance_install_failed:" + Path(argv[-1]).name)
    enabled = runner(["systemctl", "is-enabled", maintenance_timer])
    active = runner(["systemctl", "is-active", maintenance_timer])
    timer_enabled = enabled.returncode == 0 and enabled.stdout.strip() == "enabled"
    timer_active = active.returncode == 0 and active.stdout.strip() == "active"
    return {
        "status": "active" if timer_enabled and timer_active else "installed_not_active",
        "service_name": maintenance_service,
        "timer_name": maintenance_timer,
        "service_destination": str(service_destination),
        "timer_destination": str(timer_destination),
        "enabled": timer_enabled,
        "active": timer_active,
        "commands": results,
    }

def wait_for_listener(host: str, port: int, *, timeout_seconds: float = 12.0, interval_seconds: float = 0.25) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    attempts = 0
    last_error = ""
    while time.monotonic() < deadline:
        attempts += 1
        try:
            with socket.create_connection((host, int(port)), timeout=min(1.0, max(0.1, timeout_seconds))):
                return {"status": "ready", "host": host, "port": int(port), "attempts": attempts}
        except OSError as exc:
            last_error = type(exc).__name__
            time.sleep(max(0.05, float(interval_seconds)))
    return {"status": "timeout", "host": host, "port": int(port), "attempts": attempts, "last_error": last_error}


def probe_http_endpoint(url: str, *, timeout_seconds: float = 4.0) -> dict[str, Any]:
    request = urllib.request.Request(
        str(url),
        method="GET",
        headers={"Accept": "application/json, text/event-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
            code = int(getattr(response, "status", 200))
            return {"status": "reachable", "url": str(url), "http_status": code, "server_response": "accepted"}
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        if code in {401, 403, 405, 406, 415, 422, 426, 429}:
            return {"status": "reachable", "url": str(url), "http_status": code, "server_response": "protocol_or_auth_boundary"}
        return {"status": "unhealthy", "url": str(url), "http_status": code}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": "unreachable", "url": str(url), "error": type(exc).__name__}


def mcp_connection_urls(*, public_origin: str | None, port: int) -> dict[str, str | None]:
    loopback = f"http://127.0.0.1:{int(port)}/mcp/"
    public = f"{str(public_origin).rstrip('/')}/mcp/" if public_origin else None
    return {
        "loopback_mcp_url": loopback,
        "public_mcp_url": public,
        "recommended_mcp_url": public or loopback,
    }
