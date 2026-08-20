from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from mapi_core.sandman.contracts import (
    ContractError,
    PROVIDER_RESPONSE_SCHEMA_VERSION,
    canonical_json,
    parse_provider_response,
    provider_response_json_schema,
    validate_provider_request,
)
from mapi_core.sandman.validator import validate_provider_response


PRIMARY_MODEL = "gemini-3.1-flash-lite"
ESCALATION_MODEL = "gemini-3.5-flash"
MODEL_ALLOWLIST = frozenset({PRIMARY_MODEL, ESCALATION_MODEL})
THINKING_LEVELS = frozenset({"minimal", "low", "medium"})
PROVIDER_CONFIG_VERSION = "sandman_gemini_provider.v2"
PRICING_SCHEMA_VERSION = "sandman_gemini_pricing.v1"
PRICING_SOURCE_DATE = "2026-07-16"
PRICING_USD_PER_MILLION = {
    PRIMARY_MODEL: {"input": 0.25, "output": 1.50},
    ESCALATION_MODEL: {"input": 1.50, "output": 9.00},
}
CAPABILITIES = {
    "proposal_only": True,
    "supports_tools": False,
    "supports_mutation": False,
    "supports_queue_routing": False,
    "supports_external_network": True,
    "shadow_only": True,
    "stateless": True,
    "store": False,
}
STATELESS_AUDIT = {
    "store_requested": False,
    "previous_interaction_id_used": False,
    "background_used": False,
    "tools_used": False,
    "file_api_used": False,
    "grounding_used": False,
}
INSTRUCTION = (
    "Analyze only supplied allowlisted candidates. Return exactly the requested JSON schema. "
    "Proposal-only; no tools, external facts, or inferred memory IDs. Use only allowed actions "
    "and allowlisted evidence IDs. For every proposal, evidence_memory_ids must include every "
    "source_memory_id and the target_memory_id; otherwise abstain. Abstain when evidence is insufficient. "
    "Dream is not fact. Do not repeat sensitive values. Keep reasons concise and based only on supplied redacted data."
)


class GeminiInteractionsTransport(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class GoogleGenAIInteractionsTransport:
    def __init__(self, *, api_key: str, timeout_seconds: float) -> None:
        if not api_key:
            raise ValueError("provider_unconfigured")
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._timeout_seconds = timeout_seconds

    def create(self, **kwargs: Any) -> Any:
        return self._client.interactions.create(timeout=self._timeout_seconds, **kwargs)


class FakeGeminiInteractionsTransport:
    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.outcomes:
            raise RuntimeError("fake_transport_exhausted")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@dataclass(frozen=True)
class GeminiConfig:
    api_key_configured: bool
    primary_model: str = PRIMARY_MODEL
    escalation_model: str = ESCALATION_MODEL
    escalation_enabled: bool = False
    thinking_level: str = "minimal"
    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_base_seconds: float = 1.0
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: float = 300.0
    max_output_tokens: int = 2048

    @classmethod
    def from_env(cls) -> "GeminiConfig":
        provider_enabled = _env_bool("MAPI_GEMINI_ENABLED", False)
        return cls(
            api_key_configured=provider_enabled and bool(os.getenv("GEMINI_API_KEY", "").strip()),
            primary_model=os.getenv("SANDMAN_GEMINI_PRIMARY_MODEL", PRIMARY_MODEL).strip(),
            escalation_model=os.getenv("SANDMAN_GEMINI_ESCALATION_MODEL", ESCALATION_MODEL).strip(),
            escalation_enabled=_env_bool("SANDMAN_GEMINI_ESCALATION_ENABLED", False),
            thinking_level=os.getenv("SANDMAN_GEMINI_THINKING_LEVEL", "minimal").strip().lower(),
            timeout_seconds=float(os.getenv("SANDMAN_GEMINI_TIMEOUT_SECONDS", "30")),
            max_retries=int(os.getenv("SANDMAN_GEMINI_MAX_RETRIES", "2")),
            retry_base_seconds=float(os.getenv("SANDMAN_GEMINI_RETRY_BASE_SECONDS", "1.0")),
            circuit_failure_threshold=int(os.getenv("SANDMAN_GEMINI_CIRCUIT_FAILURE_THRESHOLD", "3")),
            circuit_cooldown_seconds=float(os.getenv("SANDMAN_GEMINI_CIRCUIT_COOLDOWN_SECONDS", "300")),
            max_output_tokens=int(os.getenv("SANDMAN_GEMINI_MAX_OUTPUT_TOKENS", "2048")),
        ).validated()

    def validated(self) -> "GeminiConfig":
        if self.primary_model != PRIMARY_MODEL or self.escalation_model != ESCALATION_MODEL:
            raise ValueError("model_not_allowlisted")
        if self.thinking_level not in THINKING_LEVELS:
            raise ValueError("thinking_level_not_allowlisted")
        if not 0 <= self.max_retries <= 2:
            raise ValueError("invalid_retry_budget")
        if self.timeout_seconds <= 0 or self.retry_base_seconds < 0:
            raise ValueError("invalid_timeout_config")
        if self.circuit_failure_threshold < 1 or self.circuit_cooldown_seconds < 0:
            raise ValueError("invalid_circuit_config")
        if self.max_output_tokens < 1:
            raise ValueError("invalid_output_token_budget")
        return self

    def model_for_role(self, model_role: str) -> str:
        if model_role == "primary":
            return self.primary_model
        if model_role == "escalation" and self.escalation_enabled:
            return self.escalation_model
        if model_role == "escalation":
            raise ValueError("escalation_disabled")
        raise ValueError("model_role_not_allowlisted")


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None
    half_open_probe_active: bool = False


class ModelCircuitBreaker:
    def __init__(self, *, threshold: int, cooldown_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._states: dict[str, _CircuitState] = {}
        self._lock = threading.RLock()

    def before_call(self, model: str) -> None:
        with self._lock:
            state = self._states.setdefault(model, _CircuitState())
            if state.opened_at is None:
                return
            if self.clock() - state.opened_at < self.cooldown_seconds:
                raise ProviderCallError("circuit_open", transient=False)
            if state.half_open_probe_active:
                raise ProviderCallError("circuit_open", transient=False)
            state.half_open_probe_active = True

    def success(self, model: str) -> None:
        with self._lock:
            self._states[model] = _CircuitState()

    def transient_failure(self, model: str) -> None:
        with self._lock:
            state = self._states.setdefault(model, _CircuitState())
            state.failures += 1
            state.half_open_probe_active = False
            if state.failures >= self.threshold:
                state.opened_at = self.clock()

    def permanent_failure(self, model: str) -> None:
        with self._lock:
            state = self._states.setdefault(model, _CircuitState())
            if state.half_open_probe_active:
                state.half_open_probe_active = False
                state.opened_at = self.clock()


_SHARED_CIRCUIT_REGISTRY: dict[tuple[str, int, float], ModelCircuitBreaker] = {}
_SHARED_CIRCUIT_REGISTRY_LOCK = threading.RLock()


def get_shared_model_circuit_breaker(config: GeminiConfig) -> ModelCircuitBreaker:
    validated = config.validated()
    key = (
        PROVIDER_CONFIG_VERSION,
        validated.circuit_failure_threshold,
        validated.circuit_cooldown_seconds,
    )
    with _SHARED_CIRCUIT_REGISTRY_LOCK:
        breaker = _SHARED_CIRCUIT_REGISTRY.get(key)
        if breaker is None:
            breaker = ModelCircuitBreaker(
                threshold=validated.circuit_failure_threshold,
                cooldown_seconds=validated.circuit_cooldown_seconds,
            )
            _SHARED_CIRCUIT_REGISTRY[key] = breaker
        return breaker


def _reset_shared_circuit_registry_for_tests() -> None:
    with _SHARED_CIRCUIT_REGISTRY_LOCK:
        _SHARED_CIRCUIT_REGISTRY.clear()


class ProviderCallError(RuntimeError):
    def __init__(self, category: str, *, transient: bool) -> None:
        self.category = category
        self.transient = transient
        super().__init__(category)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _error_category(exc: BaseException) -> tuple[str, bool]:
    status = getattr(exc, "status_code", None)
    if status in {429}:
        return "rate_limited", True
    if status in {500, 502, 503, 504}:
        return "server_error", True
    if status == 401:
        return "unauthorized", False
    if status == 403:
        return "forbidden", False
    if status in {400, 404, 409, 422}:
        return "invalid_request", False
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timeout" in text:
        return "timeout", True
    if any(token in name or token in text for token in ("connect", "network", "dns", "reset", "temporary")):
        return "network_error", True
    return "sdk_error", False


def extract_usage(interaction: Any) -> dict[str, int | None]:
    usage = getattr(interaction, "usage", None) or getattr(interaction, "usage_metadata", None)

    def read(*names: str) -> int | None:
        for name in names:
            value = usage.get(name) if isinstance(usage, Mapping) else getattr(usage, name, None)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    return {
        "input_tokens": read("input_tokens", "prompt_token_count", "prompt_tokens"),
        "output_tokens": read("output_tokens", "candidates_token_count", "completion_tokens"),
        "total_tokens": read("total_tokens", "total_token_count"),
    }


def estimate_cost_usd(model: str, usage: Mapping[str, int | None]) -> tuple[float | None, str | None]:
    rates = PRICING_USD_PER_MILLION.get(model)
    if rates is None:
        return None, "pricing_unknown_model"
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None or output_tokens is None:
        return None, "pricing_usage_unavailable"
    cost = input_tokens * rates["input"] / 1_000_000 + output_tokens * rates["output"] / 1_000_000
    return max(0.0, cost), None


class GeminiShadowProvider:
    name = "gemini"
    kind = "external_model"
    capabilities = CAPABILITIES

    def __init__(
        self,
        *,
        config: GeminiConfig,
        transport: GeminiInteractionsTransport,
        circuit_breaker: ModelCircuitBreaker | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.config = config.validated()
        self.transport = transport
        self.circuit = circuit_breaker or ModelCircuitBreaker(
            threshold=config.circuit_failure_threshold,
            cooldown_seconds=config.circuit_cooldown_seconds,
        )
        self.sleeper = sleeper
        self.jitter = jitter

    def analyze(self, request_value: Any, *, model_role: str = "primary") -> dict[str, Any]:
        request = validate_provider_request(request_value)
        model = self.config.model_for_role(model_role)
        if not self.config.api_key_configured:
            raise ProviderCallError("provider_unconfigured", transient=False)
        self.circuit.before_call(model)
        call = {
            "model": model,
            "input": f"{INSTRUCTION}\n\n{canonical_json(request)}",
            "store": False,
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": provider_response_json_schema(),
            },
            "generation_config": {
                "thinking_level": self.config.thinking_level,
                "max_output_tokens": self.config.max_output_tokens,
            },
        }
        retry_count = 0
        started = time.monotonic()
        while True:
            try:
                interaction = self.transport.create(**call)
                output_text = getattr(interaction, "output_text", None)
                if not isinstance(output_text, str):
                    raise ProviderCallError("invalid_response", transient=False)
                try:
                    response = parse_provider_response(output_text)
                except ContractError:
                    validation = validate_provider_response(request, output_text, provider_name=self.name)
                    usage = extract_usage(interaction)
                    cost, pricing_reason = estimate_cost_usd(model, usage)
                    self.circuit.success(model)
                    return {
                        "response": None,
                        "validation": validation,
                        "model_name": model,
                        "model_role": model_role,
                        "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                        "retry_count": retry_count,
                        "usage": usage,
                        "estimated_cost_usd": cost,
                        "pricing_reason": pricing_reason,
                        "pricing": {
                            "schema_version": PRICING_SCHEMA_VERSION,
                            "source_date": PRICING_SOURCE_DATE,
                            "currency": "USD",
                        },
                        "provider_metadata": {
                            "api_mode": "interactions",
                            "status": str(getattr(interaction, "status", "") or "")[:64],
                        },
                        "stateless_audit": dict(STATELESS_AUDIT),
                    }
                validation = validate_provider_response(request, response, provider_name=self.name)
                usage = extract_usage(interaction)
                cost, pricing_reason = estimate_cost_usd(model, usage)
                self.circuit.success(model)
                return {
                    "response": response,
                    "validation": validation,
                    "model_name": model,
                    "model_role": model_role,
                    "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                    "retry_count": retry_count,
                    "usage": usage,
                    "estimated_cost_usd": cost,
                    "pricing_reason": pricing_reason,
                    "pricing": {
                        "schema_version": PRICING_SCHEMA_VERSION,
                        "source_date": PRICING_SOURCE_DATE,
                        "currency": "USD",
                    },
                    "provider_metadata": {
                        "api_mode": "interactions",
                        "status": str(getattr(interaction, "status", "") or "")[:64],
                    },
                    "stateless_audit": dict(STATELESS_AUDIT),
                }
            except ProviderCallError as exc:
                if exc.category != "circuit_open":
                    self.circuit.permanent_failure(model)
                raise
            except BaseException as exc:
                category, transient = _error_category(exc)
                if not transient or retry_count >= self.config.max_retries:
                    if transient:
                        self.circuit.transient_failure(model)
                    else:
                        self.circuit.permanent_failure(model)
                    raise ProviderCallError(category, transient=transient) from None
                retry_count += 1
                self.sleeper(self.config.retry_base_seconds * (2 ** (retry_count - 1)) + self.jitter() * 0.1)
