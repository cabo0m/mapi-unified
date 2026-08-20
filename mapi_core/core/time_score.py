from __future__ import annotations

"""Small score and UTC time helpers shared by MAPI modules."""

from datetime import datetime, timedelta, timezone
from typing import Any, Callable


def normalize_score(value: float, *, normalizer: Callable[[float], float]) -> float:
    return normalizer(value)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_offset_days_iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=int(days))).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def shift_iso_days(value: str | None, days: int, *, normalize_optional_text: Callable[[Any], str | None]) -> str | None:
    normalized_value = normalize_optional_text(value)
    if normalized_value is None:
        return None
    candidate = normalized_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed.astimezone(timezone.utc) + timedelta(days=int(days))).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compute_days_overdue(due_at_iso: str, as_of_iso: str) -> int:
    def _parse(value: str) -> datetime:
        candidate = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    try:
        due = _parse(due_at_iso)
        as_of = _parse(as_of_iso)
        return max(0, (as_of - due).days)
    except (ValueError, AttributeError):
        return 0


def safe_event_timestamp(value: str | None, *, normalize_optional_text: Callable[[Any], str | None]) -> float | None:
    normalized_value = normalize_optional_text(value)
    if normalized_value is None:
        return None
    candidate = normalized_value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).timestamp()
    except ValueError:
        return None
