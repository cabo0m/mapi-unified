from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any, Iterable, Mapping

from mapi_core.memory.sensitivity import classify_memory_sensitivity
from mapi_core.sandman.contracts import (
    EXTERNAL_DATA_POLICY,
    MAX_REDACTED_CHARS_PER_CANDIDATE,
    REDACTION_MANIFEST_SCHEMA_VERSION,
    REDACTION_POLICY_VERSION,
)


EXCLUDED_SENSITIVITY_CLASSES = frozenset(
    {"credential_secret", "private_key", "never_store", "health_sensitive", "financial_sensitive"}
)

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){7,15}(?!\d)")
_PERSON_ID = re.compile(r"(?<!\d)\d{11}(?!\d)")
_IP = re.compile(r"\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b")
_ADDRESS = re.compile(
    r"\b(?:home address|private address|adres domowy|adres zamieszkania)\s*[:=-]\s*[^\n;,]{4,160}",
    re.IGNORECASE,
)
_CREDENTIAL_URI = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]{1,128}:[^\s/@]{4,256}@[^\s]+", re.IGNORECASE)
_HARD_SECRET_MARKERS = re.compile(
    r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----|"
    r"\bauthorization\s*:\s*(?:bearer|basic)\s+[A-Za-z0-9+/_=.-]{8,}|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\b(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)\b\s*[:=]\s*[\"']?[^\s,;\"']{8,}",
    re.IGNORECASE,
)

_PERSONAL_RULES = (
    ("credential_uri", _CREDENTIAL_URI, "[REDACTED_CREDENTIAL_URI]"),
    ("email", _EMAIL, "[REDACTED_EMAIL]"),
    ("person_id", _PERSON_ID, "[REDACTED_PERSON_ID]"),
    ("phone", _PHONE, "[REDACTED_PHONE]"),
    ("ip", _IP, "[REDACTED_IP]"),
    ("address", _ADDRESS, "[REDACTED_ADDRESS]"),
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def redact_personal_content(content: str) -> tuple[str, dict[str, int]]:
    redacted = content
    counts: Counter[str] = Counter()
    for rule_id, pattern, placeholder in _PERSONAL_RULES:
        redacted, count = pattern.subn(placeholder, redacted)
        if count:
            counts[rule_id] += count
    return redacted, dict(sorted(counts.items()))


def residual_sensitive_reason_codes(content: str) -> list[str]:
    reasons: list[str] = []
    if _HARD_SECRET_MARKERS.search(content) or _CREDENTIAL_URI.search(content):
        reasons.append("hard_secret_marker")
    sensitivity = classify_memory_sensitivity(content, metadata={})
    if sensitivity["sensitivity_class"] in EXCLUDED_SENSITIVITY_CLASSES:
        reasons.append("restricted_sensitivity_class")
    # Provider response reasons must not echo direct personal identifiers either.
    if any(pattern.search(content) for _, pattern, _ in _PERSONAL_RULES):
        reasons.append("personal_identifier")
    return sorted(set(reasons))


def _truncate_safely(value: str) -> tuple[str, bool]:
    if len(value) <= MAX_REDACTED_CHARS_PER_CANDIDATE:
        return value, False
    truncated = value[:MAX_REDACTED_CHARS_PER_CANDIDATE]
    if truncated.rfind("[") > truncated.rfind("]"):
        truncated = truncated[:truncated.rfind("[")]
    return truncated.rstrip(), True


def derive_artifact_kind(memory: Mapping[str, Any]) -> str:
    entry_type = str(memory.get("entry_type") or "").casefold()
    memory_type = str(memory.get("memory_type") or "").casefold()
    truth_kind = str(memory.get("truth_kind") or "").casefold()
    tokens = {entry_type, memory_type, truth_kind}
    if "dream" in tokens:
        return "dream"
    if "decision" in tokens or "project_decision" in tokens:
        return "decision"
    if "preference" in tokens or "confirmed_preference" in tokens:
        return "preference"
    if truth_kind in {"fact", "user_confirmed"} or entry_type in {"fact", "identity"}:
        return "fact"
    if truth_kind in {"hypothesis", "interpretation", "proposal"}:
        return "hypothesis"
    if entry_type in {"operational", "task"} or memory_type in {"operational", "task", "project_note", "working"}:
        return "operational"
    return "unknown"


def build_redacted_candidates(
    memories: Iterable[Mapping[str, Any]],
    *,
    links_by_source: Mapping[int, list[Mapping[str, Any]]] | None = None,
    requested_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    memories = sorted((dict(item) for item in memories), key=lambda item: int(item["id"]))
    requested = sorted(set(int(item) for item in (requested_ids or [item["id"] for item in memories])))
    links_by_source = links_by_source or {}
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    replacements: Counter[str] = Counter()
    truncated_ids: list[int] = []

    for memory in memories:
        memory_id = int(memory["id"])
        content = str(memory.get("content") or "")
        sensitivity = classify_memory_sensitivity(
            content,
            metadata={
                "tags": memory.get("tags"),
                "visibility_scope": memory.get("visibility_scope"),
                "never_store": memory.get("never_store"),
            },
        )
        sensitivity_class = sensitivity["sensitivity_class"]
        if sensitivity_class in EXCLUDED_SENSITIVITY_CLASSES:
            excluded.append({
                "memory_id": memory_id,
                "sensitivity_class": sensitivity_class,
                "reason_codes": ["sensitivity_class_excluded"],
            })
            continue

        redacted = content
        local_counts: dict[str, int] = {}
        if sensitivity_class == "personal":
            redacted, local_counts = redact_personal_content(content)
            if not local_counts:
                excluded.append({
                    "memory_id": memory_id,
                    "sensitivity_class": sensitivity_class,
                    "reason_codes": ["personal_redaction_not_proven"],
                })
                continue
        residual = residual_sensitive_reason_codes(redacted)
        if residual:
            excluded.append({
                "memory_id": memory_id,
                "sensitivity_class": sensitivity_class,
                "reason_codes": ["residual_sensitive_material"],
            })
            continue
        redacted, truncated = _truncate_safely(redacted)
        if truncated:
            truncated_ids.append(memory_id)
        replacements.update(local_counts)
        candidate_links = [
            {"relation_type": str(link["relation_type"]), "target_memory_id": int(link["target_memory_id"])}
            for link in links_by_source.get(memory_id, [])
        ]
        included.append(
            {
                "memory_id": memory_id,
                "project_key": memory.get("project_key"),
                "scope_code": memory.get("scope_code"),
                "workspace_id": memory.get("workspace_id"),
                "memory_type": memory.get("memory_type"),
                "truth_kind": memory.get("truth_kind"),
                "state_code": memory.get("state_code"),
                "artifact_kind": derive_artifact_kind(memory),
                "created_at": memory.get("created_at"),
                "updated_at": memory.get("updated_at"),
                "content_redacted": redacted,
                "content_sha256": _sha256(content),
                "redacted_content_sha256": _sha256(redacted),
                "sensitivity_class": sensitivity_class,
                "redaction_applied": bool(local_counts or truncated),
                "supersedes_memory_id": memory.get("supersedes_memory_id"),
                "superseded_by_memory_id": memory.get("superseded_by_memory_id"),
                "allowlisted_links": sorted(candidate_links, key=lambda item: (item["relation_type"], item["target_memory_id"])),
            }
        )

    included_id_set = {item["memory_id"] for item in included}
    for candidate in included:
        if candidate["supersedes_memory_id"] not in included_id_set:
            candidate["supersedes_memory_id"] = None
        if candidate["superseded_by_memory_id"] not in included_id_set:
            candidate["superseded_by_memory_id"] = None
        candidate["allowlisted_links"] = [
            link for link in candidate["allowlisted_links"] if link["target_memory_id"] in included_id_set
        ]

    manifest = {
        "schema_version": REDACTION_MANIFEST_SCHEMA_VERSION,
        "policy_version": REDACTION_POLICY_VERSION,
        "external_data_policy": EXTERNAL_DATA_POLICY,
        "candidate_count_requested": len(requested),
        "candidate_count_included": len(included),
        "candidate_count_excluded": len(excluded),
        "included_memory_ids": [item["memory_id"] for item in included],
        "excluded_candidates": sorted(excluded, key=lambda item: item["memory_id"]),
        "replacement_counts": dict(sorted(replacements.items())),
        "truncated_memory_ids": sorted(truncated_ids),
        "raw_secret_exposed": False,
        "full_project_dump": False,
    }
    if len(included) < 2:
        status = "blocked"
        reason_codes = ["insufficient_safe_candidates"]
    elif excluded:
        status = "request_ready_partial"
        reason_codes = []
    else:
        status = "request_ready"
        reason_codes = []
    return {"status": status, "candidates": included, "redaction_manifest": manifest, "reason_codes": reason_codes}
