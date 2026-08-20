from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from mapi_core.memory.agent_self_model import build_agent_self_capsule_payload
from mapi_core.sandman.providers.gemini import (
    GoogleGenAIInteractionsTransport,
    PRIMARY_MODEL,
    estimate_cost_usd,
    extract_usage,
)

AGENT_SELF_NARRATIVE_SCHEMA = "mapi_agent_self_narrative.v1"
AGENT_SELF_CLAIM_CATALOG_SCHEMA = "mapi_agent_self_claim_catalog.v1"
AGENT_SELF_NARRATIVE_REQUEST_SCHEMA = "mapi_agent_self_narrative_request.v1"
AGENT_SELF_NARRATIVE_VALIDATION_SCHEMA = "mapi_agent_self_narrative_validation.v1"
ALLOWED_PROVIDER_NAMES = frozenset({"deterministic", "gemini"})
SECTION_KEYS = ("identity", "preferences", "relationships", "commitments", "autobiography")
MAX_PARAGRAPHS = 5
MAX_CLAIMS_PER_PARAGRAPH = 4
MAX_CLAIMS = 20
MAX_SENTENCE_CHARACTERS = 420
MAX_NARRATIVE_CHARACTERS = 6500


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _consciousness_claim(value: Any) -> bool:
    text = _text(value).casefold()
    markers = (
        "i am conscious", "i'm conscious", "i am sentient", "i'm sentient",
        "jestem świadom", "jestem swiadom", "mam świadomość", "mam swiadomosc",
    )
    return any(marker in text for marker in markers)


def _sentence(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if len(text) > MAX_SENTENCE_CHARACTERS:
        text = text[: MAX_SENTENCE_CHARACTERS - 3].rstrip() + "..."
    return text


def _claim_id(*, section: str, sentence: str, source_memory_ids: list[int]) -> str:
    digest = _fingerprint({"section": section, "sentence": sentence, "source_memory_ids": sorted(source_memory_ids)})[:20]
    return f"claim:{section}:{digest}"


def _claim(section: str, raw: Mapping[str, Any]) -> dict[str, Any] | None:
    memory_id = int(raw.get("id") or raw.get("source_memory_id") or 0)
    if memory_id <= 0:
        return None
    sentence = _sentence(raw.get("statement") or raw.get("summary_short") or raw.get("title"))
    if not sentence or _consciousness_claim(sentence):
        return None
    return {
        "claim_id": _claim_id(section=section, sentence=sentence, source_memory_ids=[memory_id]),
        "section_key": section,
        "sentence": sentence,
        "source_memory_ids": [memory_id],
    }


def build_claim_catalog(capsule: Mapping[str, Any]) -> dict[str, Any]:
    sources = {
        "identity": list(capsule.get("identity") or []),
        "preferences": list(capsule.get("preferences") or []),
        "relationships": list(capsule.get("relationships") or []),
        "commitments": list(capsule.get("commitments") or []),
        "autobiography": list(capsule.get("recent_autobiographical_events") or []),
    }
    claims: dict[str, list[dict[str, Any]]] = {key: [] for key in SECTION_KEYS}
    total = 0
    for section in SECTION_KEYS:
        seen: set[str] = set()
        for raw in sources[section]:
            item = _claim(section, raw)
            if item is None or item["claim_id"] in seen:
                continue
            seen.add(item["claim_id"])
            claims[section].append(item)
            total += 1
            if len(claims[section]) >= MAX_CLAIMS_PER_PARAGRAPH or total >= MAX_CLAIMS:
                break
        if total >= MAX_CLAIMS:
            break
    allowed_ids = sorted({int(mid) for values in claims.values() for item in values for mid in item["source_memory_ids"]})
    core = {"schema": AGENT_SELF_CLAIM_CATALOG_SCHEMA, "sections": claims, "allowed_memory_ids": allowed_ids}
    return {**core, "claim_catalog_fingerprint": _fingerprint(core)}


def build_default_claim_selection(catalog: Mapping[str, Any]) -> dict[str, list[str]]:
    sections = catalog.get("sections") or {}
    return {key: [str(item["claim_id"]) for item in list(sections.get(key) or [])[:MAX_CLAIMS_PER_PARAGRAPH]] for key in SECTION_KEYS}


def claim_selection_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(SECTION_KEYS),
        "properties": {key: {"type": "array", "items": {"type": "string"}, "maxItems": MAX_CLAIMS_PER_PARAGRAPH} for key in SECTION_KEYS},
    }


def validate_claim_selection(value: Any, catalog: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if not isinstance(value, Mapping):
        return {"schema": AGENT_SELF_NARRATIVE_VALIDATION_SCHEMA, "accepted": False, "status": "rejected", "reason_codes": ["selection_must_be_object"], "normalized_selection": build_default_claim_selection(catalog), "invented_claim_ids": []}
    if set(value.keys()) != set(SECTION_KEYS):
        reasons.append("unsupported_freeform_text_or_unknown_field")
    by_id = {str(item["claim_id"]): item for items in (catalog.get("sections") or {}).values() for item in items}
    normalized: dict[str, list[str]] = {key: [] for key in SECTION_KEYS}
    invented: set[str] = set()
    for section in SECTION_KEYS:
        raw = value.get(section, [])
        if not isinstance(raw, list) or len(raw) > MAX_CLAIMS_PER_PARAGRAPH:
            reasons.append("invalid_claim_list")
            continue
        for claim_id in raw:
            if not isinstance(claim_id, str):
                reasons.append("claim_id_must_be_string")
                continue
            claim = by_id.get(claim_id)
            if claim is None:
                invented.add(claim_id)
                continue
            if claim.get("section_key") != section:
                reasons.append("claim_section_mismatch")
                continue
            if claim_id not in normalized[section]:
                normalized[section].append(claim_id)
    if invented:
        reasons.append("invented_claim_id")
    accepted = not reasons
    return {
        "schema": AGENT_SELF_NARRATIVE_VALIDATION_SCHEMA,
        "accepted": accepted,
        "status": "accepted" if accepted else "rejected",
        "reason_codes": sorted(set(reasons)),
        "invented_claim_ids": sorted(invented),
        "normalized_selection": normalized if accepted else build_default_claim_selection(catalog),
    }


def build_narrative_request(catalog: Mapping[str, Any]) -> dict[str, Any]:
    provider_catalog = {
        section: [{"claim_id": item["claim_id"], "sentence": item["sentence"]} for item in catalog.get("sections", {}).get(section, [])]
        for section in SECTION_KEYS
    }
    return {
        "schema": AGENT_SELF_NARRATIVE_REQUEST_SCHEMA,
        "claim_catalog_schema": catalog.get("schema"),
        "claim_catalog_fingerprint": catalog.get("claim_catalog_fingerprint"),
        "sections": provider_catalog,
        "instructions": "Select only supplied claim IDs. Do not return prose, memory IDs, new facts, consciousness claims or extra keys.",
    }


def render_narrative(selection: Mapping[str, list[str]], catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_id = {str(item["claim_id"]): item for items in (catalog.get("sections") or {}).values() for item in items}
    paragraphs: list[dict[str, Any]] = []
    total_chars = 0
    for section in SECTION_KEYS:
        selected = list(selection.get(section) or [])[:MAX_CLAIMS_PER_PARAGRAPH]
        claims = [by_id[claim_id] for claim_id in selected if claim_id in by_id]
        if not claims:
            continue
        source_ids = sorted({int(mid) for claim in claims for mid in claim["source_memory_ids"]})
        parts = [f"{claim['sentence']} " + " ".join(f"[#{mid}]" for mid in claim["source_memory_ids"]) for claim in claims]
        text = " ".join(parts)
        remaining = MAX_NARRATIVE_CHARACTERS - total_chars
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[: max(0, remaining - 3)].rstrip() + "..."
        if _consciousness_claim(text):
            continue
        paragraphs.append({"section_key": section, "claim_ids": [claim["claim_id"] for claim in claims], "source_memory_ids": source_ids, "text": text})
        total_chars += len(text)
        if len(paragraphs) >= MAX_PARAGRAPHS:
            break
    return paragraphs


@dataclass(frozen=True)
class NarrativeGeminiConfig:
    api_key_configured: bool
    model: str = PRIMARY_MODEL
    thinking_level: str = "minimal"
    timeout_seconds: float = 30.0
    max_output_tokens: int = 1200

    @classmethod
    def from_env(cls) -> "NarrativeGeminiConfig":
        enabled = str(os.environ.get("MAPI_GEMINI_ENABLED") or "").strip().casefold() in {"1", "true", "yes", "on"}
        return cls(
            api_key_configured=enabled and bool(str(os.environ.get("GEMINI_API_KEY") or "").strip()),
            model=PRIMARY_MODEL,
            thinking_level=str(os.environ.get("SANDMAN_GEMINI_THINKING_LEVEL") or "minimal").strip().casefold(),
            timeout_seconds=float(os.environ.get("SANDMAN_GEMINI_TIMEOUT_SECONDS") or 30.0),
        ).validated()

    def validated(self) -> "NarrativeGeminiConfig":
        if self.model != PRIMARY_MODEL:
            raise ValueError("model_not_allowlisted")
        if self.thinking_level not in {"minimal", "low"}:
            raise ValueError("thinking_level_not_allowlisted")
        if self.timeout_seconds <= 0 or self.max_output_tokens < 1:
            raise ValueError("invalid_provider_budget")
        return self


class NarrativeProviderError(RuntimeError):
    pass


class GeminiNarrativePlanner:
    def __init__(self, *, config: NarrativeGeminiConfig, transport: Any | None = None) -> None:
        self.config = config.validated()
        self.transport = transport

    def plan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not self.config.api_key_configured:
            raise NarrativeProviderError("provider_unconfigured")
        transport = self.transport or GoogleGenAIInteractionsTransport(api_key=str(os.environ.get("GEMINI_API_KEY") or "").strip(), timeout_seconds=self.config.timeout_seconds)
        instruction = "Return exactly the requested JSON claim-selection object. Use only supplied claim IDs. No prose, memory IDs, new facts, tools, background work, or consciousness claims."
        call = {
            "model": self.config.model,
            "input": f"{instruction}\n\n{_canonical_json(request)}",
            "store": False,
            "response_format": {"type": "text", "mime_type": "application/json", "schema": claim_selection_json_schema()},
            "generation_config": {"thinking_level": self.config.thinking_level, "max_output_tokens": self.config.max_output_tokens},
        }
        started = time.monotonic()
        try:
            interaction = transport.create(**call)
        except Exception as exc:
            raise NarrativeProviderError(type(exc).__name__) from exc
        output_text = getattr(interaction, "output_text", None)
        if not isinstance(output_text, str):
            raise NarrativeProviderError("invalid_response")
        try:
            selection = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise NarrativeProviderError("invalid_json") from exc
        usage = extract_usage(interaction)
        cost, pricing_reason = estimate_cost_usd(self.config.model, usage)
        return {
            "selection": selection,
            "metadata": {
                "provider_name": "gemini", "model_name": self.config.model,
                "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                "usage": usage, "estimated_cost_usd": cost, "pricing_reason": pricing_reason,
                "store": False, "tools_used": False, "background_used": False,
            },
        }


def build_agent_self_narrative_payload(conn: Any, *, subject_key: str | None, display_name: str | None, project_key: str | None, include_global: bool, provider_name: str = "deterministic", include_debug: bool = False, row_to_dict: Callable[[Any], dict[str, Any]], planner: Callable[[Mapping[str, Any]], Any] | None = None) -> dict[str, Any]:
    provider = _text(provider_name).casefold() or "deterministic"
    if provider not in ALLOWED_PROVIDER_NAMES:
        return {"status": "error", "schema": AGENT_SELF_NARRATIVE_SCHEMA, "error": "provider_not_allowlisted", "provider_name": provider}
    capsule = build_agent_self_capsule_payload(conn, subject_key=subject_key, display_name=display_name, project_key=project_key, include_global=include_global, limit=50, include_content=False, row_to_dict=row_to_dict)
    catalog = build_claim_catalog(capsule)
    default_selection = build_default_claim_selection(catalog)
    validation = validate_claim_selection(default_selection, catalog)
    selection = default_selection
    provider_status = "not_requested"
    narrative_mode = "deterministic"
    provider_metadata: dict[str, Any] = {}
    warnings: list[str] = []
    request = build_narrative_request(catalog)
    if provider == "gemini":
        active_planner = planner
        if active_planner is None:
            active_planner = GeminiNarrativePlanner(config=NarrativeGeminiConfig.from_env()).plan
        try:
            value = active_planner(request)
            if isinstance(value, Mapping) and "selection" in value:
                candidate = value.get("selection")
                provider_metadata = dict(value.get("metadata") or {})
            else:
                candidate = value
            candidate_validation = validate_claim_selection(candidate, catalog)
            if candidate_validation["accepted"]:
                selection = candidate_validation["normalized_selection"]
                validation = candidate_validation
                provider_status = "accepted"
                narrative_mode = "gemini_planned"
            else:
                provider_status = "rejected"
                narrative_mode = "provider_fallback"
                validation = candidate_validation
                warnings.append("Provider selection rejected; deterministic selection used.")
        except Exception as exc:
            provider_status = "failed"
            narrative_mode = "provider_fallback"
            warnings.append("Provider call failed; deterministic selection used.")
            provider_metadata = {"provider_name": "gemini", "error_category": _text(exc)[:96] or type(exc).__name__}
    paragraphs = render_narrative(selection, catalog)
    source_ids = sorted({int(mid) for paragraph in paragraphs for mid in paragraph["source_memory_ids"]})
    core = {
        "schema": AGENT_SELF_NARRATIVE_SCHEMA,
        "subject": capsule.get("subject") or {},
        "read_only": True,
        "provider_requested": provider,
        "provider_status": provider_status,
        "narrative_mode": narrative_mode,
        "claim_catalog_fingerprint": catalog["claim_catalog_fingerprint"],
        "allowed_memory_ids": catalog["allowed_memory_ids"],
        "paragraphs": paragraphs,
        "source_memory_ids": source_ids,
        "warnings": warnings,
        "disclaimers": [
            "Narrative is assembled only from allowlisted source claims.",
            "It does not prove consciousness, subjective experience or autonomous agency.",
            "Provider output can select claim IDs only; host code renders the prose.",
        ],
    }
    result = {**core, "status": "ok", "validation": validation, "narrative_fingerprint": _fingerprint(core), "safety": {"source_bound": True, "provider_can_write_prose": False, "provider_can_return_memory_ids": False, "writes_performed": 0}, "debug": {}}
    if include_debug:
        result["debug"] = {"claim_catalog": catalog, "request": request, "selected_claims": selection, "provider_metadata": provider_metadata}
    return result
