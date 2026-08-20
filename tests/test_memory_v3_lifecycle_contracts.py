from __future__ import annotations

import pytest

from mapi_core.memory.lifecycle_contracts import (
    ALLOWED_MEMORY_TRANSITIONS,
    CANONICAL_MEMORY_STATES,
    MEMORY_V3_RELATION_KINDS,
    SUPERSESSION_RELATION_KINDS,
    derive_canonical_memory_state,
    is_supersession_capable_relation_kind,
    is_transition_allowed,
    normalize_canonical_memory_state,
    normalize_relation_kind,
    project_memory_v2_status,
)


def test_canonical_memory_states_and_projection_matrix() -> None:
    assert CANONICAL_MEMORY_STATES == {
        "candidate",
        "validated",
        "stale",
        "conflicted",
        "archived",
        "superseded",
    }
    assert project_memory_v2_status(state_code="candidate") == "proposed"
    assert project_memory_v2_status(state_code="validated") == "active"
    assert project_memory_v2_status(state_code="stale") == "stale"
    assert project_memory_v2_status(state_code="conflicted") == "contradicted"
    assert project_memory_v2_status(state_code="archived") == "archived"
    assert project_memory_v2_status(state_code="superseded") == "superseded"


def test_legacy_active_state_is_treated_as_validated() -> None:
    assert normalize_canonical_memory_state("active") == "validated"
    assert derive_canonical_memory_state(state_code="active") == "validated"
    assert project_memory_v2_status(state_code="active") == "active"


def test_unknown_state_is_controlled() -> None:
    assert normalize_canonical_memory_state("mystery") is None
    with pytest.raises(ValueError):
        derive_canonical_memory_state(state_code="mystery")
    with pytest.raises(ValueError):
        project_memory_v2_status(state_code="mystery")


def test_transition_matrix_covers_allowed_and_blocked_paths() -> None:
    assert ALLOWED_MEMORY_TRANSITIONS["candidate"] == {"validated", "archived"}
    assert is_transition_allowed("candidate", "validated") is True
    assert is_transition_allowed("validated", "stale") is True
    assert is_transition_allowed("validated", "candidate") is False
    assert is_transition_allowed("archived", "candidate") is True
    assert is_transition_allowed("superseded", "validated") is False


def test_relation_kinds_and_supersession_subset() -> None:
    assert MEMORY_V3_RELATION_KINDS == {
        "correction",
        "refinement",
        "replacement",
        "reinforcement",
        "contradiction",
        "association",
    }
    assert SUPERSESSION_RELATION_KINDS == {
        "correction",
        "refinement",
        "replacement",
    }
    assert normalize_relation_kind("replacement") == "replacement"
    assert normalize_relation_kind("refinement") == "refinement"
    assert normalize_relation_kind("unsupported") is None
    assert is_supersession_capable_relation_kind("replacement") is True
    assert is_supersession_capable_relation_kind("association") is False
