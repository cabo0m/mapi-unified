from __future__ import annotations

"""Deterministic, source-bound context composer for public MAPI agents.

The engine does not retrieve independently and does not synthesize new facts. It
compacts already source-bound surfaces into one injectable context string with a
hard conservative token upper bound.
"""

import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence


CONTEXT_ENGINE_SCHEMA = "mapi_context_engine.v1"
TOKEN_BUDGET_POLICY = "mapi_utf8_byte_token_upper_bound_v1"
DEFAULT_TOKEN_BUDGET = 2400
MAX_TOKEN_BUDGET = 12000
MAX_SECTION_ITEMS = {
    "identity": 2,
    "commitments_guardrails": 5,
    "current_project": 2,
    "query_relevant_memories": 6,
    "recent_delta": 4,
    "conflicts_uncertainty": 4,
    "gravity": 2,
    "next_step": 1,
}
SECTION_ORDER = tuple(MAX_SECTION_ITEMS)


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def conservative_token_upper_bound(text: str) -> int:
    """A hard upper bound for byte-based subword tokenizers.

    Every non-empty tokenizer token consumes at least one UTF-8 byte, therefore
    UTF-8 byte length is deliberately conservative. It is not presented as an
    exact GPT token count.
    """
    return len(str(text or "").encode("utf-8"))


def _source_ids(values: Iterable[Any]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            memory_id = int(value)
        except (TypeError, ValueError):
            continue
        if memory_id <= 0 or memory_id in seen:
            continue
        seen.add(memory_id)
        out.append(memory_id)
    return out


def _item(
    statement: Any,
    source_memory_ids: Sequence[int],
    *,
    kind: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    clean = _text(statement)
    sources = _source_ids(source_memory_ids)
    if not clean or not sources:
        return None
    return {
        "kind": kind,
        "statement": clean,
        "source_memory_ids": sources,
        "metadata": dict(metadata or {}),
    }


def _memory_statement(memory: Mapping[str, Any]) -> str:
    return _text(memory.get("summary_short") or memory.get("title") or memory.get("content"))


def _identity_items(restore: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = list(restore.get("core_memories") or [])
    ranked = sorted(
        rows,
        key=lambda row: (
            0 if _text(row.get("memory_type")).lower() == "identity" else 1,
            -float(row.get("identity_weight") or 0.0),
            -float(row.get("importance_score") or 0.0),
            int(row.get("id") or 0),
        ),
    )
    items: list[dict[str, Any]] = []
    for row in ranked:
        candidate = _item(
            _memory_statement(row),
            [row.get("id")],
            kind="identity",
            metadata={"memory_type": row.get("memory_type")},
        )
        if candidate:
            items.append(candidate)
        if len(items) >= MAX_SECTION_ITEMS["identity"]:
            break
    return items


def _commitment_items(ledger: Mapping[str, Any], canonical_project_key: str) -> list[dict[str, Any]]:
    allowed: list[dict[str, Any]] = []
    kind_rank = {
        "behavioral_guardrail": 0,
        "safety_boundary": 1,
        "privacy_boundary": 2,
        "project_workflow_rule": 3,
        "operator_instruction": 4,
        "memory_use_rule": 5,
        "memory_write_rule": 6,
        "interaction_rule": 7,
        "relationship_commitment": 8,
    }
    for raw in ledger.get("commitments") or []:
        scope = raw.get("scope") or {}
        scope_project = _text(scope.get("project_key"))
        commitment_kind = _text(raw.get("commitment_kind"))
        action_key = _text(raw.get("action_key"))
        if scope_project and scope_project != canonical_project_key:
            continue
        if not scope_project:
            universal_action_keys = {
                "response.evidence_truthfulness",
                "testing.scope",
                "memory.use",
                "memory.operation_communication",
                "agent.behavior",
            }
            if action_key not in universal_action_keys:
                # Unscoped commitments are admitted only when operationally universal.
                continue
        if _text(raw.get("status")).lower() != "active":
            continue
        source_id = raw.get("source_memory_id")
        candidate = _item(
            raw.get("statement"),
            [source_id],
            kind="commitment_guardrail",
            metadata={
                "commitment_kind": raw.get("commitment_kind"),
                "action_key": raw.get("action_key"),
                "polarity": raw.get("polarity"),
                "scope_code": scope.get("scope_code"),
                "scope_project_key": scope_project or None,
            },
        )
        if candidate:
            allowed.append(candidate)
    allowed.sort(
        key=lambda item: (
            kind_rank.get(_text(item["metadata"].get("commitment_kind")), 99),
            0 if item["metadata"].get("scope_project_key") == canonical_project_key else 1,
            item["source_memory_ids"][0],
        )
    )
    return allowed[: MAX_SECTION_ITEMS["commitments_guardrails"]]


def _current_project_items(restore: Mapping[str, Any], canonical_project_key: str) -> list[dict[str, Any]]:
    rows_by_id: dict[int, Mapping[str, Any]] = {}
    for row in list(restore.get("project_anchors") or []) + list(restore.get("recent_context") or []):
        memory_id = int(row.get("id") or 0)
        if memory_id <= 0 or _text(row.get("project_key")) != canonical_project_key:
            continue
        rows_by_id[memory_id] = row
    rows = list(rows_by_id.values())
    rows.sort(key=lambda row: (str(row.get("created_at") or ""), int(row.get("id") or 0)), reverse=True)
    items: list[dict[str, Any]] = []
    for row in rows:
        candidate = _item(
            _memory_statement(row),
            [row.get("id")],
            kind="current_project_anchor",
            metadata={"project_key": canonical_project_key, "memory_type": row.get("memory_type")},
        )
        if candidate:
            items.append(candidate)
        if len(items) >= MAX_SECTION_ITEMS["current_project"]:
            break
    return items


def _relevant_items(retrieval: Mapping[str, Any], canonical_project_key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in retrieval.get("items") or []:
        if _text(row.get("project_key")) != canonical_project_key:
            continue
        matched_by = list((row.get("match_debug") or {}).get("matched_by") or [])
        # Context Engine fails closed on fallback-only/project-only retrieval.
        if matched_by and not any(signal in matched_by for signal in ("text", "token", "phrase", "semantic")):
            continue
        candidate = _item(
            _memory_statement(row),
            [row.get("id")],
            kind="query_relevant_memory",
            metadata={
                "project_key": row.get("project_key"),
                "memory_type": row.get("memory_type"),
                "truth_kind": row.get("truth_kind"),
                "confidence_score": row.get("confidence_score"),
                "matched_by": matched_by,
            },
        )
        if candidate:
            items.append(candidate)
        if len(items) >= MAX_SECTION_ITEMS["query_relevant_memories"]:
            break
    return items


def _recent_items(
    restore: Mapping[str, Any],
    canonical_project_key: str,
    excluded_ids: set[int],
) -> list[dict[str, Any]]:
    rows = [
        row for row in restore.get("recent_context") or []
        if _text(row.get("project_key")) == canonical_project_key
        and int(row.get("id") or 0) not in excluded_ids
    ]
    rows.sort(key=lambda row: (str(row.get("created_at") or ""), int(row.get("id") or 0)), reverse=True)
    items: list[dict[str, Any]] = []
    for row in rows:
        candidate = _item(
            _memory_statement(row),
            [row.get("id")],
            kind="recent_delta",
            metadata={"project_key": canonical_project_key, "created_at": row.get("created_at")},
        )
        if candidate:
            items.append(candidate)
        if len(items) >= MAX_SECTION_ITEMS["recent_delta"]:
            break
    return items


def _conflict_and_uncertainty_items(
    ledger: Mapping[str, Any],
    retrieval: Mapping[str, Any],
    canonical_project_key: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for conflict in ledger.get("conflicts") or []:
        source_ids = _source_ids(
            list(conflict.get("source_memory_ids") or [])
            + [conflict.get("left_source_memory_id"), conflict.get("right_source_memory_id")]
        )
        if not source_ids:
            continue
        statement = conflict.get("statement") or conflict.get("summary") or f"Unresolved commitment conflict across memories {source_ids}."
        candidate = _item(statement, source_ids, kind="unresolved_conflict", metadata={"status": "unresolved"})
        if candidate:
            items.append(candidate)
    for row in retrieval.get("items") or []:
        if _text(row.get("project_key")) != canonical_project_key:
            continue
        truth_kind = _text(row.get("truth_kind")).lower()
        confidence = float(row.get("confidence_score") or 0.0)
        contradiction = bool(row.get("contradiction_flag"))
        state_code = _text(row.get("state_code")).lower()
        reasons: list[str] = []
        if truth_kind in {"proposal", "interpretation", "dream"}:
            reasons.append(f"truth_kind={truth_kind}")
        if confidence and confidence < 0.8:
            reasons.append(f"confidence_score={confidence:.2f}")
        if contradiction:
            reasons.append("contradiction_flag=true")
        if state_code == "review":
            reasons.append("state_code=review")
        if not reasons:
            continue
        candidate = _item(
            f"Memory #{int(row.get('id') or 0)} is uncertain: {', '.join(reasons)}.",
            [row.get("id")],
            kind="retrieval_uncertainty",
            metadata={"reasons": reasons},
        )
        if candidate:
            items.append(candidate)
        if len(items) >= MAX_SECTION_ITEMS["conflicts_uncertainty"]:
            break
    return items[: MAX_SECTION_ITEMS["conflicts_uncertainty"]]


def _gravity_items(gravity_block: Mapping[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in gravity_block.get("items") or []:
        candidate = _item(
            raw.get("statement"),
            raw.get("source_memory_ids") or [raw.get("memory_id")],
            kind="gravity",
            metadata={"lane": raw.get("lane"), "gravity_score": raw.get("gravity_score")},
        )
        if candidate:
            items.append(candidate)
        if len(items) >= MAX_SECTION_ITEMS["gravity"]:
            break
    return items


def _next_step_excerpt(memory: Mapping[str, Any]) -> str:
    content = str(memory.get("content") or "")
    if not content:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", content)
    for sentence in reversed(sentences):
        lowered = sentence.lower()
        if any(marker in lowered for marker in ("nastÄ™pny krok", "nastepny krok", "next step", "nastÄ™pna", "nastepna")):
            return _text(sentence)
    return ""


def _next_step_items(restore: Mapping[str, Any], canonical_project_key: str) -> list[dict[str, Any]]:
    rows = [
        row for row in restore.get("recent_context") or []
        if _text(row.get("project_key")) == canonical_project_key
    ]
    rows.sort(key=lambda row: (str(row.get("created_at") or ""), int(row.get("id") or 0)), reverse=True)
    for row in rows:
        statement = _next_step_excerpt(row)
        if not statement:
            continue
        candidate = _item(
            statement,
            [row.get("id")],
            kind="next_step",
            metadata={"project_key": canonical_project_key, "extraction": "verbatim_sentence"},
        )
        return [candidate] if candidate else []
    return []


def _line(section: str, item: Mapping[str, Any]) -> str:
    source_label = ",".join(f"#{memory_id}" for memory_id in item.get("source_memory_ids") or [])
    kind = _text(item.get("kind"))
    return f"[{section}|{kind}|{source_label}] {_text(item.get('statement'))}"


def _fit_sections_to_budget(
    sections: Mapping[str, Sequence[Mapping[str, Any]]],
    token_budget: int,
) -> tuple[dict[str, list[dict[str, Any]]], str, int, list[dict[str, Any]]]:
    selected: dict[str, list[dict[str, Any]]] = {section: [] for section in SECTION_ORDER}
    lines: list[str] = []
    dropped: list[dict[str, Any]] = []
    attempted: set[tuple[str, int]] = set()
    used = 0

    def attempt(section: str, index: int) -> None:
        nonlocal used
        key = (section, index)
        if key in attempted:
            return
        attempted.add(key)
        values = list(sections.get(section) or [])
        if index >= len(values):
            return
        item = dict(values[index])
        line = _line(section, item)
        prefix = "\n" if lines else ""
        cost = conservative_token_upper_bound(prefix + line)
        if used + cost <= token_budget:
            selected[section].append(item)
            lines.append(line)
            used += cost
        else:
            dropped.append({
                "section": section,
                "source_memory_ids": list(item.get("source_memory_ids") or []),
                "reason": "token_budget",
                "cost_upper_bound": cost,
            })

    # First pass protects task utility under tight budgets: one identity anchor,
    # current project, query evidence, one guardrail and a sourced next step.
    for section in (
        "identity",
        "current_project",
        "query_relevant_memories",
        "commitments_guardrails",
        "next_step",
        "conflicts_uncertainty",
        "gravity",
        "recent_delta",
    ):
        attempt(section, 0)

    # Second pass fills remaining room in the canonical section order.
    for section in SECTION_ORDER:
        for index, _raw in enumerate(sections.get(section) or []):
            attempt(section, index)

    context_text = "\n".join(lines)
    actual = conservative_token_upper_bound(context_text)
    if actual != used:
        used = actual
    return selected, context_text, used, dropped


def build_agent_context_payload(
    *,
    intent: str,
    requested_project_key: str,
    canonical_project_key: str,
    token_budget: int,
    restore_payload: Mapping[str, Any],
    commitment_ledger: Mapping[str, Any],
    retrieval_payload: Mapping[str, Any],
    gravity_block: Mapping[str, Any],
) -> dict[str, Any]:
    clean_intent = _text(intent)
    if not clean_intent:
        return {"status": "error", "schema": CONTEXT_ENGINE_SCHEMA, "error": "intent_required"}
    requested_budget = max(1, min(int(token_budget or DEFAULT_TOKEN_BUDGET), MAX_TOKEN_BUDGET))
    canonical = _text(canonical_project_key)
    requested = _text(requested_project_key)
    if not canonical:
        return {"status": "error", "schema": CONTEXT_ENGINE_SCHEMA, "error": "canonical_project_key_required"}

    identity = _identity_items(restore_payload)
    commitments = _commitment_items(commitment_ledger, canonical)
    current_project = _current_project_items(restore_payload, canonical)
    relevant = _relevant_items(retrieval_payload, canonical)
    excluded_ids = {
        source_id
        for item in current_project + relevant
        for source_id in item["source_memory_ids"]
    }
    recent = _recent_items(restore_payload, canonical, excluded_ids)
    uncertainty = _conflict_and_uncertainty_items(commitment_ledger, retrieval_payload, canonical)
    gravity = _gravity_items(gravity_block)
    next_step = _next_step_items(restore_payload, canonical)

    raw_sections: dict[str, list[dict[str, Any]]] = {
        "identity": identity,
        "commitments_guardrails": commitments,
        "current_project": current_project,
        "query_relevant_memories": relevant,
        "recent_delta": recent,
        "conflicts_uncertainty": uncertainty,
        "gravity": gravity,
        "next_step": next_step,
    }
    sections, context_text, used, dropped = _fit_sections_to_budget(raw_sections, requested_budget)
    source_memory_ids = sorted({
        source_id
        for section in sections.values()
        for item in section
        for source_id in item.get("source_memory_ids") or []
    })
    section_counts = {section: len(sections[section]) for section in SECTION_ORDER}
    invariants = {
        "token_budget_respected": used <= requested_budget,
        "all_items_source_bound": all(
            bool(item.get("source_memory_ids"))
            for section in sections.values()
            for item in section
        ),
        "gravity_bounded": len(sections["gravity"]) <= 2,
        "retrieval_project_scoped": all(
            item.get("metadata", {}).get("project_key") == canonical
            for item in sections["query_relevant_memories"]
        ),
        "writes_performed": False,
        "model_calls": False,
        "freeform_synthesis": False,
    }
    result: dict[str, Any] = {
        "status": "ok",
        "schema": CONTEXT_ENGINE_SCHEMA,
        "read_only": True,
        "intent": clean_intent,
        "requested_project_key": requested,
        "project_key": canonical,
        "active_project_key": canonical,
        "sections": sections,
        "section_counts": section_counts,
        "context_text": context_text,
        "source_memory_ids": source_memory_ids,
        "budget": {
            "requested_token_budget": requested_budget,
            "used_token_upper_bound": used,
            "remaining_token_upper_bound": requested_budget - used,
            "policy": TOKEN_BUDGET_POLICY,
            "note": "UTF-8 byte length is used as a conservative upper bound, not as an exact model tokenizer count.",
            "dropped_item_count": len(dropped),
        },
        "invariants": invariants,
        "source_status": {
            "restore_status": restore_payload.get("status"),
            "commitment_ledger_status": commitment_ledger.get("status"),
            "retrieval_count": retrieval_payload.get("count"),
            "gravity_status": gravity_block.get("status"),
        },
        "dropped_items": dropped,
    }
    result["context_fingerprint"] = _fingerprint({
        "schema": result["schema"],
        "intent": result["intent"],
        "project_key": result["project_key"],
        "sections": result["sections"],
        "budget": result["budget"],
        "source_memory_ids": result["source_memory_ids"],
        "invariants": result["invariants"],
    })
    return result
