from __future__ import annotations

import os

from .selector import current_platform


def default_distribution_name() -> str:
    platform = current_platform()
    if platform == "windows":
        return "Aurora"
    if platform == "linux":
        return "Polaris"
    return "MAPI"


def distribution_name() -> str:
    configured = str(os.environ.get("MAPI_DISTRIBUTION_NAME", "")).strip()
    return configured or default_distribution_name()


def distribution_slug() -> str:
    return distribution_name().strip().casefold().replace(" ", "-") or "mapi"


__all__ = ["default_distribution_name", "distribution_name", "distribution_slug"]
