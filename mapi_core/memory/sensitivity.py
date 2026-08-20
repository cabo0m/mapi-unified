from __future__ import annotations

import re
from typing import Any

from mapi_core.memory.lifecycle_contracts import MEMORY_V3_HASH_ALGORITHM


SENSITIVITY_SCHEMA_VERSION = "memory_v3_sensitivity.v1"
SENSITIVITY_CLASSES = frozenset(
    {
        "public",
        "internal",
        "personal",
        "health_sensitive",
        "financial_sensitive",
        "credential_secret",
        "private_key",
        "never_store",
    }
)
RESTRICTED_CAPTURE_CLASSES = frozenset({"credential_secret", "private_key", "never_store"})

_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----",
    re.IGNORECASE,
)
_AUTH_HEADER_PATTERN = re.compile(
    r"\bauthorization\s*:\s*(?:bearer|basic)\s+[A-Za-z0-9+/_=.-]{8,}",
    re.IGNORECASE,
)
_CREDENTIAL_URI_PATTERN = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s/:@]{1,128}:[^\s/@]{4,256}@",
    re.IGNORECASE,
)
_AWS_ACCESS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_STRUCTURED_SECRET_PATTERN = re.compile(
    r"\b(password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret)\b\s*[:=]\s*[\"']?([^\s,;\"']{6,256})",
    re.IGNORECASE,
)
_NEVER_STORE_PATTERN = re.compile(r"(?:\[\s*never[_ -]?store\s*\]|\bnever_store\s*[:=]\s*true\b)", re.IGNORECASE)


def _normalized_tokens(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        raw = " ".join(str(item) for item in value)
    else:
        raw = str(value or "")
    return {token for token in re.findall(r"[a-z0-9_]+", raw.casefold()) if token}


def _looks_like_real_secret(value: str) -> bool:
    normalized = value.strip().strip("<>[]{}()")
    lowered = normalized.casefold()
    if lowered in {
        "example",
        "example_value",
        "placeholder",
        "redacted",
        "changeme",
        "password",
        "secret",
        "token",
        "your_token",
        "your_api_key",
    }:
        return False
    if normalized.startswith("${") or set(normalized) <= {"*", "x", "X", "-", "_"}:
        return False
    return len(normalized) >= 16 or (
        len(normalized) >= 8
        and any(char.isdigit() for char in normalized)
        and any(not char.isalnum() for char in normalized)
    )


def classify_memory_sensitivity(
    content: str | None,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = str(content or "")
    lowered = text.casefold()
    metadata = dict(metadata or {})
    tags = _normalized_tokens(metadata.get("tags"))
    matched_rule_ids: list[str] = []

    explicit_never_store = bool(metadata.get("never_store")) or bool(_NEVER_STORE_PATTERN.search(text))
    private_key = bool(_PRIVATE_KEY_PATTERN.search(text))
    credential_rules: list[str] = []
    if _AUTH_HEADER_PATTERN.search(text):
        credential_rules.append("authorization_header_value")
    if _CREDENTIAL_URI_PATTERN.search(text):
        credential_rules.append("credential_uri_value")
    if _AWS_ACCESS_KEY_PATTERN.search(text):
        credential_rules.append("cloud_access_key_value")
    if any(_looks_like_real_secret(match.group(2)) for match in _STRUCTURED_SECRET_PATTERN.finditer(text)):
        credential_rules.append("structured_credential_value")

    if explicit_never_store:
        sensitivity_class = "never_store"
        matched_rule_ids.append("explicit_never_store_marker")
    elif private_key:
        sensitivity_class = "private_key"
        matched_rule_ids.append("private_key_material")
    elif credential_rules:
        sensitivity_class = "credential_secret"
        matched_rule_ids.extend(credential_rules)
    else:
        health_signal = bool(
            {"health", "medical", "patient", "diagnosis", "medication"} & tags
            or re.search(r"\b(?:diagnos(?:is|ed)|diagnoz[ay]|medical record|patient id|blood pressure|prescription|medication|icd[- ]?10)\b", lowered)
        )
        financial_signal = bool(
            {"financial", "banking", "salary", "private_finance"} & tags
            or re.search(r"\b(?:iban\s*[A-Z]{2}\d{2}|bank account|account balance|salary|credit card|debit card|tax id)\b", text, re.IGNORECASE)
        )
        personal_signal = bool(
            {"personal", "private", "pii"} & tags
            or re.search(r"\b(?:pesel|home address|private address|personal phone|personal email)\b", lowered)
        )
        public_signal = bool(
            str(metadata.get("visibility_scope") or "").casefold() == "public"
            or "public" in tags
            or metadata.get("public_signal") is True
        )
        if health_signal:
            sensitivity_class = "health_sensitive"
            matched_rule_ids.append("health_context_signal")
        elif financial_signal:
            sensitivity_class = "financial_sensitive"
            matched_rule_ids.append("private_financial_context_signal")
        elif personal_signal:
            sensitivity_class = "personal"
            matched_rule_ids.append("personal_context_signal")
        elif public_signal:
            sensitivity_class = "public"
            matched_rule_ids.append("explicit_public_signal")
        else:
            sensitivity_class = "internal"
            matched_rule_ids.append("default_internal")

    matched_rule_ids = sorted(set(matched_rule_ids))
    capture_allowed = sensitivity_class not in RESTRICTED_CAPTURE_CLASSES
    return {
        "schema_version": SENSITIVITY_SCHEMA_VERSION,
        "sensitivity_class": sensitivity_class,
        "reason_codes": matched_rule_ids,
        "matched_rule_ids": matched_rule_ids,
        "restricted_external_provider": sensitivity_class not in {"public", "internal"},
        "capture_allowed": capture_allowed,
        "redaction_required": sensitivity_class != "public",
        "confidence_band": "deterministic",
        "hash_algorithm": MEMORY_V3_HASH_ALGORITHM,
    }


def capture_sensitivity_gate(
    content: str | None,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sensitivity = classify_memory_sensitivity(content, metadata=metadata)
    if sensitivity["capture_allowed"]:
        return {"status": "allowed", "sensitivity": sensitivity}
    return {
        "status": "blocked_never_store",
        "schema_version": "memory_v3_capture_sensitivity_gate.v1",
        "reason_codes": list(sensitivity["reason_codes"]),
        "sensitivity_class": sensitivity["sensitivity_class"],
        "safety": {
            "raw_secret_exposed": False,
            "memory_mutations_performed": 0,
            "queue_mutations_performed": 0,
        },
    }
