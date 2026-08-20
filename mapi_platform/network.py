from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from typing import Any


def wait_for_listener(
    host: str,
    port: int,
    *,
    timeout_seconds: float = 12.0,
    interval_seconds: float = 0.25,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    attempts = 0
    last_error = ""
    while time.monotonic() < deadline:
        attempts += 1
        try:
            with socket.create_connection(
                (host, int(port)), timeout=min(1.0, max(0.1, timeout_seconds))
            ):
                return {"status": "ready", "host": host, "port": int(port), "attempts": attempts}
        except OSError as exc:
            last_error = type(exc).__name__
            time.sleep(max(0.05, float(interval_seconds)))
    return {
        "status": "timeout",
        "host": host,
        "port": int(port),
        "attempts": attempts,
        "last_error": last_error,
    }


def probe_http_endpoint(url: str, *, timeout_seconds: float = 4.0) -> dict[str, Any]:
    request = urllib.request.Request(
        str(url),
        method="GET",
        headers={"Accept": "application/json, text/event-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
            code = int(getattr(response, "status", 200))
            return {
                "status": "reachable",
                "url": str(url),
                "http_status": code,
                "server_response": "accepted",
            }
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        if code in {401, 403, 405, 406, 415, 422, 426, 429}:
            return {
                "status": "reachable",
                "url": str(url),
                "http_status": code,
                "server_response": "protocol_or_auth_boundary",
            }
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
