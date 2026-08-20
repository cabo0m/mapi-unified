from __future__ import annotations

from typing import Any

from mapi_core.schemas import normalize_optional_text


CANONICAL_MEMORY_STATES = frozenset(
    {
        "candidate",
        "validated",
        "stale",
        "conflicted",
        "archived",
        "superseded",
    }
)

LEGACY_STATE_ALIASES = {
    "active": "validated",
}

MEMORY_V3_RELATION_KINDS = frozenset(
    {
        "correction",
        "refinement",
        "replacement",
        "reinforcement",
        "contradiction",
        "association",
    }
)

SUPERSESSION_RELATION_KINDS = frozenset({"correction", "refinement", "replacement"})

ALLOWED_MEMORY_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"validated", "archived"}),
    "validated": frozenset({"stale", "conflicted", "archived", "superseded"}),
    "stale": frozenset({"candidate", "validated", "archived", "superseded"}),
    "conflicted": frozenset({"candidate", "validated", "archived", "superseded"}),
    "archived": frozenset({"candidate"}),
    "superseded": frozenset({"candidate"}),
}

MEMORY_V3_HASH_ALGORITHM = "sha256:canonical-json:v1"


def normalize_canonical_memory_state(value: str | None, *, allow_legacy: bool = True) -> str | None:
    normalized = normalize_optional_text(value)
    if normalized is None:
        return None
    normalized = normalized.lower().replace("-", "_").replace(" ", "_")
    if allow_legacy and normalized in LEGACY_STATE_ALIASES:
        normalized = LEGACY_STATE_ALIASES[normalized]
    if normalized not in CANONICAL_MEMORY_STATES:
        return None
    return normalized


def derive_canonical_memory_state(
    *,
    state_code: str | None,
    activity_state: str | None = None,
    contradiction_flag: Any | None = None,
    allow_legacy: bool = True,
) -> str:
    normalized_state = normalize_canonical_memory_state(state_code, allow_legacy=allow_legacy)
    if normalized_state is not None:
        return normalized_state
    if bool(contradiction_flag):
        return "conflicted"
    if normalize_optional_text(activity_state) == "archived":
        return "archived"
    raise ValueError(f"unknown lifecycle state_code: {state_code!r}")


def project_memory_v2_status(
    *,
    state_code: str | None,
    activity_state: str | None = None,
    contradiction_flag: Any | None = None,
    allow_legacy: bool = True,
) -> str:
    normalized_state = derive_canonical_memory_state(
        state_code=state_code,
        activity_state=activity_state,
        contradiction_flag=contradiction_flag,
        allow_legacy=allow_legacy,
    )
    if normalized_state == "candidate":
        return "proposed"
    if normalized_state == "validated":
        return "active"
    if normalized_state == "stale":
        return "stale"
    if normalized_state == "conflicted":
        return "contradicted"
    if normalized_state == "archived":
        return "archived"
    if normalized_state == "superseded":
        return "superseded"
    raise ValueError(f"unsupported canonical lifecycle state: {normalized_state!r}")


def is_transition_allowed(from_state: str | None, to_state: str | None, *, allow_legacy: bool = True) -> bool:
    normalized_from = normalize_canonical_memory_state(from_state, allow_legacy=allow_legacy)
    normalized_to = normalize_canonical_memory_state(to_state, allow_legacy=allow_legacy)
    if normalized_from is None or normalized_to is None:
        return False
    return normalized_to in ALLOWED_MEMORY_TRANSITIONS[normalized_from]


def normalize_relation_kind(value: str | None) -> str | None:
    normalized = normalize_optional_text(value)
    if normalized is None:
        return None
    normalized = normalized.lower().replace("-", "_").replace(" ", "_")
    if normalized not in MEMORY_V3_RELATION_KINDS:
        return None
    return normalized


def is_supersession_capable_relation_kind(value: str | None) -> bool:
    normalized = normalize_relation_kind(value)
    return normalized in SUPERSESSION_RELATION_KINDS
