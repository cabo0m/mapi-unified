from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from pydantic import BaseModel, Field, field_validator

LAYER_CODES = frozenset({"core", "identity", "autobio", "projects", "working", "buffer"})
AREA_CODES = frozenset({"identity", "relation", "projects", "knowledge", "preferences", "history", "rumination", "meta", "sandman"})
STATE_CODES = frozenset({"candidate", "active", "validated", "conflicted", "archived", "superseded"})
SCOPE_CODES = frozenset({"global", "user", "project", "conversation", "system"})

MEMORY_ENTRY_TYPES = frozenset(
    {"core", "user_profile", "project", "decision", "incident", "experiment", "dream", "raw_note", "conflict", "open_question"}
)
TRUTH_KINDS = frozenset({"fact", "decision", "preference", "interpretation", "dream", "proposal"})
MEMORY_V2_STATUSES = frozenset({"active", "stale", "archived", "proposed", "contradicted", "superseded"})
IMPORTANCE_LEVELS = frozenset({"low", "medium", "high", "critical"})

DEFAULT_LAYER_CODE = "buffer"
DEFAULT_AREA_CODE = "knowledge"
DEFAULT_STATE_CODE = "active"
DEFAULT_SCOPE_CODE = "global"
DEFAULT_ENTRY_TYPE = "raw_note"
DEFAULT_TRUTH_KIND = "fact"
DEFAULT_MEMORY_V2_STATUS = "active"

# Hierarchia warstw od najniższej (buffer) do najwyższej (core).
# Używana w promote_memory / demote_memory do walidacji kierunku przejścia.
LAYER_ORDER: list[str] = ["buffer", "working", "projects", "autobio", "identity", "core"]

# Warstwy chronione przed automatyczną archiwizacją i downgrade'em przez Sandmana.
SANDMAN_PROTECTED_LAYERS: frozenset[str] = frozenset({"core", "identity"})

# State codes chronione przed downgrade'em przez Sandmana.
SANDMAN_PROTECTED_STATES: frozenset[str] = frozenset({"validated", "canonical"})


def _norm_code(value: str | None, allowed: frozenset[str], field_name: str) -> str | None:
    if value is None:
        return None
    value = value.strip().lower().replace("-", "_").replace(" ", "_")
    if not value:
        return None
    if value not in allowed:
        raise ValueError(f"{field_name} must be one of: {', '.join(sorted(allowed))}")
    return value


def normalize_layer_code(value: str | None) -> str | None:
    return _norm_code(value, LAYER_CODES, "layer_code")


def normalize_area_code(value: str | None) -> str | None:
    return _norm_code(value, AREA_CODES, "area_code")


def normalize_state_code(value: str | None) -> str | None:
    return _norm_code(value, STATE_CODES, "state_code")


def normalize_scope_code(value: str | None) -> str | None:
    return _norm_code(value, SCOPE_CODES, "scope_code")


def normalize_memory_entry_type(value: str | None) -> str | None:
    return _norm_code(value, MEMORY_ENTRY_TYPES, "entry_type")


def normalize_truth_kind(value: str | None) -> str | None:
    return _norm_code(value, TRUTH_KINDS, "truth_kind")


def normalize_memory_v2_status(value: str | None) -> str | None:
    return _norm_code(value, MEMORY_V2_STATUSES, "memory_v2_status")


def normalize_importance_level(value: str | None) -> str | None:
    return _norm_code(value, IMPORTANCE_LEVELS, "importance_level")


def normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def normalize_required_text(value: str, field_name: str) -> str:
    value = normalize_optional_text(value)
    if value is None:
        raise ValueError(f"{field_name} cannot be empty")
    return value


def derive_state_code(raw_state_code: str | None, activity_state: str | None = None, contradiction_flag: Any | None = None) -> str:
    state = normalize_state_code(raw_state_code)
    if state is not None:
        return state
    if bool(contradiction_flag):
        return "conflicted"
    if normalize_optional_text(activity_state) == "archived":
        return "archived"
    return DEFAULT_STATE_CODE


def derive_entry_type(
    *,
    entry_type: str | None,
    memory_type: str | None,
    layer_code: str | None,
    area_code: str | None,
    project_key: str | None,
) -> str:
    normalized = normalize_memory_entry_type(entry_type)
    if normalized is not None:
        return normalized

    memory_type_norm = (normalize_optional_text(memory_type) or "").lower()
    layer = normalize_layer_code(layer_code) or DEFAULT_LAYER_CODE
    area = normalize_area_code(area_code) or DEFAULT_AREA_CODE

    if memory_type_norm == "dream" or area == "sandman":
        return "dream"
    if "decision" in memory_type_norm or layer == "core":
        return "decision" if "decision" in memory_type_norm else "core"
    if "incident" in memory_type_norm or "error" in memory_type_norm or "bug" in memory_type_norm:
        return "incident"
    if "experiment" in memory_type_norm or "hypothesis" in memory_type_norm:
        return "experiment"
    if "conflict" in memory_type_norm or area == "rumination":
        return "conflict" if "conflict" in memory_type_norm else "raw_note"
    if "question" in memory_type_norm:
        return "open_question"
    if area in {"identity", "preferences", "relation"} or "preference" in memory_type_norm:
        return "user_profile"
    if project_key or area == "projects" or layer == "projects":
        if "decision" in memory_type_norm:
            return "decision"
        return "project"
    return DEFAULT_ENTRY_TYPE


def derive_truth_kind(
    *,
    truth_kind: str | None,
    entry_type: str | None,
    memory_type: str | None,
    area_code: str | None,
) -> str:
    normalized = normalize_truth_kind(truth_kind)
    if normalized is not None:
        return normalized

    derived_type = derive_entry_type(
        entry_type=entry_type,
        memory_type=memory_type,
        layer_code=None,
        area_code=area_code,
        project_key=None,
    )
    if derived_type == "dream":
        return "dream"
    if derived_type == "decision":
        return "decision"
    if derived_type == "user_profile":
        return "preference"
    if derived_type in {"experiment", "raw_note", "open_question"}:
        return "proposal"
    if derived_type == "conflict":
        return "interpretation"
    return DEFAULT_TRUTH_KIND


def derive_memory_v2_status(
    *,
    memory_v2_status: str | None,
    state_code: str | None,
    activity_state: str | None,
    contradiction_flag: Any | None,
) -> str:
    normalized = normalize_memory_v2_status(memory_v2_status)
    if normalized is not None:
        return normalized

    state = derive_state_code(state_code, activity_state, contradiction_flag)
    if state == "candidate":
        return "proposed"
    if state == "conflicted":
        return "contradicted"
    if state == "archived":
        return "archived"
    if state == "superseded":
        return "superseded"
    return DEFAULT_MEMORY_V2_STATUS


def derive_importance_level(importance_score: Any) -> str:
    try:
        score = float(importance_score or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if score >= 0.85:
        return "critical"
    if score >= 0.65:
        return "high"
    if score >= 0.35:
        return "medium"
    return "low"


def enrich_memory_dict(memory: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
    item = dict(memory)
    item["layer_code"] = normalize_layer_code(item.get("layer_code")) or DEFAULT_LAYER_CODE
    item["area_code"] = normalize_area_code(item.get("area_code")) or DEFAULT_AREA_CODE
    item["scope_code"] = normalize_scope_code(item.get("scope_code")) or DEFAULT_SCOPE_CODE
    item["state_code"] = derive_state_code(item.get("state_code"), item.get("activity_state"), item.get("contradiction_flag"))
    item["version"] = max(int(item.get("version") or 1), 1)
    item["decay_score"] = float(item.get("decay_score") or 0.0)
    item["emotional_weight"] = float(item.get("emotional_weight") or 0.0)
    item["identity_weight"] = float(item.get("identity_weight") or 0.0)
    item["schema_version"] = max(int(item.get("schema_version") or 1), 1)
    item["entry_type"] = derive_entry_type(
        entry_type=item.get("entry_type"),
        memory_type=item.get("memory_type"),
        layer_code=item.get("layer_code"),
        area_code=item.get("area_code"),
        project_key=item.get("project_key"),
    )
    item["type"] = item["entry_type"]
    item["truth_kind"] = derive_truth_kind(
        truth_kind=item.get("truth_kind"),
        entry_type=item["entry_type"],
        memory_type=item.get("memory_type"),
        area_code=item.get("area_code"),
    )
    item["memory_v2_status"] = derive_memory_v2_status(
        memory_v2_status=item.get("memory_v2_status"),
        state_code=item.get("state_code"),
        activity_state=item.get("activity_state"),
        contradiction_flag=item.get("contradiction_flag"),
    )
    item["status"] = item["memory_v2_status"]
    item["importance_level"] = normalize_importance_level(item.get("importance_level")) or derive_importance_level(item.get("importance_score"))
    item["title"] = normalize_optional_text(item.get("title")) or normalize_optional_text(item.get("summary_short")) or normalize_required_text(str(item.get("memory_type") or "memory"), "title")
    item["source_context"] = normalize_optional_text(item.get("source_context"))
    item["source_event_ref"] = normalize_optional_text(item.get("source_event_ref"))
    item["updated_at"] = normalize_optional_text(item.get("updated_at")) or normalize_optional_text(item.get("created_at"))
    item["last_confirmed_at"] = normalize_optional_text(item.get("last_confirmed_at")) or normalize_optional_text(item.get("last_validated_at"))
    item["superseded_by_memory_id"] = item.get("superseded_by_memory_id")
    item["requires_user_confirmation"] = bool(int(item.get("requires_user_confirmation") or 0))
    if (
        not item["requires_user_confirmation"]
        and item["memory_v2_status"] in {"proposed", "stale", "contradicted"}
        and item["truth_kind"] in {"interpretation", "dream", "proposal"}
    ):
        item["requires_user_confirmation"] = True
    raw_should_resurface = item.get("should_resurface_when")
    if raw_should_resurface is None:
        raw_should_resurface = item.get("should_resurface_when_json")
    if isinstance(raw_should_resurface, str):
        try:
            decoded_should_resurface = json.loads(raw_should_resurface)
        except json.JSONDecodeError:
            decoded_should_resurface = []
    else:
        decoded_should_resurface = raw_should_resurface
    item["should_resurface_when"] = decoded_should_resurface if isinstance(decoded_should_resurface, list) else []
    item["linked_memories"] = item.get("linked_memories") or []
    return item


class MemorySaveRequest(BaseModel):
    content: str
    memory_type: str = "project_note"
    summary_short: Optional[str] = None
    project_key: Optional[str] = None
    scope_code: Optional[str] = None
    tags: Optional[str] = None
    title: Optional[str] = None
    entry_type: Optional[str] = None
    truth_kind: Optional[str] = None
    source_context: Optional[str] = None
    conversation_key: Optional[str] = None
    source_event_ref: Optional[str] = None
    importance_score: float = Field(default=0.75, ge=0.0, le=1.0)
    confidence_score: float = Field(default=0.9, ge=0.0, le=1.0)
    write_intent: str = "user_explicit"
    supersedes_memory_id: Optional[int] = Field(default=None, ge=1)
    supersession_relation: Optional[str] = None
    supersession_scope: Optional[str] = None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return normalize_required_text(value, "content")

    @field_validator("memory_type")
    @classmethod
    def validate_memory_type(cls, value: str) -> str:
        return normalize_required_text(value, "memory_type")

    @field_validator(
        "summary_short", "project_key", "tags", "title", "entry_type", "truth_kind",
        "source_context", "conversation_key", "source_event_ref", "supersession_scope",
        mode="before",
    )
    @classmethod
    def validate_optional_texts(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)

    @field_validator("scope_code", mode="before")
    @classmethod
    def validate_scope(cls, value: str | None) -> str | None:
        return normalize_scope_code(value)

    @field_validator("write_intent", mode="before")
    @classmethod
    def validate_write_intent(cls, value: str) -> str:
        normalized = normalize_required_text(value, "write_intent").lower()
        if normalized not in {"user_explicit", "agent_autonomous"}:
            raise ValueError("write_intent must be user_explicit or agent_autonomous")
        return normalized

    @field_validator("supersession_relation", mode="before")
    @classmethod
    def validate_supersession_relation(cls, value: str | None) -> str | None:
        normalized = normalize_optional_text(value)
        if normalized is not None and normalized not in {"supersedes", "refines", "partially_supersedes"}:
            raise ValueError("supersession_relation must be supersedes, refines or partially_supersedes")
        return normalized


class MemoryProposalRequest(BaseModel):
    content: str
    project_key: Optional[str] = None
    scope_code: Optional[str] = None
    source_context: Optional[str] = None
    conversation_key: Optional[str] = None
    source_event_ref: Optional[str] = None
    hint: Optional[str] = None
    expires_at: Optional[str] = None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return normalize_required_text(value, "content")

    @field_validator(
        "project_key", "source_context", "conversation_key", "source_event_ref", "hint", "expires_at",
        mode="before",
    )
    @classmethod
    def validate_optional_texts(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)

    @field_validator("scope_code", mode="before")
    @classmethod
    def validate_scope(cls, value: str | None) -> str | None:
        return normalize_scope_code(value)


class MemoryCreateRequest(BaseModel):
    content: str
    summary_short: Optional[str] = None
    memory_type: str
    source: Optional[str] = None
    importance_score: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence_score: float = Field(default=0.5, ge=0.0, le=1.0)
    tags: Optional[str] = None
    layer_code: Optional[str] = None
    area_code: Optional[str] = None
    state_code: Optional[str] = None
    scope_code: Optional[str] = None
    parent_memory_id: Optional[int] = Field(default=None, ge=1)
    version: int = Field(default=1, ge=1)
    promoted_from_id: Optional[int] = Field(default=None, ge=1)
    demoted_from_id: Optional[int] = Field(default=None, ge=1)
    supersedes_memory_id: Optional[int] = Field(default=None, ge=1)
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    decay_score: float = Field(default=0.0, ge=0.0, le=1.0)
    emotional_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    identity_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    project_key: Optional[str] = None
    conversation_key: Optional[str] = None
    last_validated_at: Optional[str] = None
    validation_source: Optional[str] = None
    schema_version: int = Field(default=2, ge=1)
    entry_type: Optional[str] = None
    truth_kind: Optional[str] = None
    title: Optional[str] = None
    source_context: Optional[str] = None
    source_event_ref: Optional[str] = None
    updated_at: Optional[str] = None
    last_confirmed_at: Optional[str] = None
    memory_v2_status: Optional[str] = None
    importance_level: Optional[str] = None
    superseded_by_memory_id: Optional[int] = Field(default=None, ge=1)
    requires_user_confirmation: bool = False
    should_resurface_when: Optional[list[str]] = None
    owner_role: Optional[str] = None
    owner_id: Optional[str] = None
    review_due_at: Optional[str] = None
    revalidation_due_at: Optional[str] = None
    expired_due_at: Optional[str] = None
    priority: Optional[str] = None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return normalize_required_text(value, "content")

    @field_validator("memory_type")
    @classmethod
    def validate_memory_type(cls, value: str) -> str:
        return normalize_required_text(value, "memory_type")

    @field_validator(
        "summary_short",
        "source",
        "tags",
        "valid_from",
        "valid_to",
        "project_key",
        "conversation_key",
        "last_validated_at",
        "validation_source",
        "title",
        "source_context",
        "source_event_ref",
        "updated_at",
        "last_confirmed_at",
        mode="before",
    )
    @classmethod
    def validate_texts(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)

    @field_validator("layer_code", mode="before")
    @classmethod
    def validate_layer_code(cls, value: str | None) -> str | None:
        return normalize_layer_code(value)

    @field_validator("area_code", mode="before")
    @classmethod
    def validate_area_code(cls, value: str | None) -> str | None:
        return normalize_area_code(value)

    @field_validator("state_code", mode="before")
    @classmethod
    def validate_state_code(cls, value: str | None) -> str | None:
        return normalize_state_code(value)

    @field_validator("scope_code", mode="before")
    @classmethod
    def validate_scope_code(cls, value: str | None) -> str | None:
        return normalize_scope_code(value)

    @field_validator("entry_type", mode="before")
    @classmethod
    def validate_entry_type(cls, value: str | None) -> str | None:
        return normalize_memory_entry_type(value)

    @field_validator("truth_kind", mode="before")
    @classmethod
    def validate_truth_kind(cls, value: str | None) -> str | None:
        return normalize_truth_kind(value)

    @field_validator("memory_v2_status", mode="before")
    @classmethod
    def validate_memory_v2_status(cls, value: str | None) -> str | None:
        return normalize_memory_v2_status(value)

    @field_validator("importance_level", mode="before")
    @classmethod
    def validate_importance_level(cls, value: str | None) -> str | None:
        return normalize_importance_level(value)


class MemoryLinkRequest(BaseModel):
    from_memory_id: int
    to_memory_id: int
    relation_type: str
    weight: float
    origin: Optional[str] = None


class MemoryRecallRequest(BaseModel):
    memory_id: int
    recall_type: str = "direct"
    source: Optional[str] = None
    strength: Optional[float] = 0.1


class MemoryResponse(BaseModel):
    id: int
    content: str
    summary_short: Optional[str] = None
    memory_type: str
    source: Optional[str] = None
    importance_score: float
    confidence_score: float
    tags: Optional[str] = None
    layer_code: str = DEFAULT_LAYER_CODE
    area_code: str = DEFAULT_AREA_CODE
    state_code: str = DEFAULT_STATE_CODE
    scope_code: str = DEFAULT_SCOPE_CODE
    parent_memory_id: Optional[int] = None
    version: int = 1
    promoted_from_id: Optional[int] = None
    demoted_from_id: Optional[int] = None
    supersedes_memory_id: Optional[int] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    decay_score: float = 0.0
    emotional_weight: float = 0.0
    identity_weight: float = 0.0
    project_key: Optional[str] = None
    conversation_key: Optional[str] = None
    last_validated_at: Optional[str] = None
    validation_source: Optional[str] = None
    schema_version: int = 1
    entry_type: str = DEFAULT_ENTRY_TYPE
    truth_kind: str = DEFAULT_TRUTH_KIND
    title: Optional[str] = None
    source_context: Optional[str] = None
    source_event_ref: Optional[str] = None
    updated_at: Optional[str] = None
    last_confirmed_at: Optional[str] = None
    memory_v2_status: str = DEFAULT_MEMORY_V2_STATUS
    importance_level: str = "medium"
    superseded_by_memory_id: Optional[int] = None
    requires_user_confirmation: bool = False
    linked_memories: list[int] = Field(default_factory=list)
    should_resurface_when: list[str] = Field(default_factory=list)


class MemoryLinkResponse(BaseModel):
    id: int
    from_memory_id: int
    to_memory_id: int
    relation_type: str
    weight: float
    origin: Optional[str] = None
