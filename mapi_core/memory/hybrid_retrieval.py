from __future__ import annotations

"""Deterministic fusion for lexical + semantic + recency + bounded Gravity retrieval."""

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable, Mapping


HYBRID_RETRIEVAL_SCHEMA = "mapi_hybrid_retrieval.v1"
RRF_K = 60
LEXICAL_WEIGHT = 1.0
SEMANTIC_WEIGHT = 1.0
RECENCY_WEIGHT = 0.20
GRAVITY_WEIGHT = 0.50


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _rrf(rank: int | None, weight: float) -> float:
    if rank is None or rank <= 0:
        return 0.0
    return weight / float(RRF_K + rank)


def _timestamp(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


def fuse_hybrid_results(
    *,
    query: str,
    requested_project_key: str | None,
    canonical_project_key: str | None,
    lexical_payload: Mapping[str, Any],
    semantic_payload: Mapping[str, Any],
    candidate_items: Mapping[int, Mapping[str, Any]],
    gravity_block: Mapping[str, Any],
    limit: int,
) -> dict[str, Any]:
    lexical_rank: dict[int, int] = {}
    lexical_debug: dict[int, Mapping[str, Any]] = {}
    for rank, item in enumerate(lexical_payload.get("items") or [], start=1):
        memory_id = int(item.get("id") or 0)
        if memory_id <= 0 or memory_id not in candidate_items:
            continue
        lexical_rank.setdefault(memory_id, rank)
        lexical_debug[memory_id] = dict(item.get("match_debug") or {})

    semantic_rank: dict[int, int] = {}
    semantic_debug: dict[int, Mapping[str, Any]] = {}
    for rank, item in enumerate(semantic_payload.get("results") or [], start=1):
        memory_id = int(item.get("memory_id") or 0)
        if memory_id <= 0 or memory_id not in candidate_items:
            continue
        semantic_rank.setdefault(memory_id, rank)
        semantic_debug[memory_id] = dict(item)

    recency_order = sorted(
        candidate_items,
        key=lambda memory_id: (
            _timestamp(candidate_items[memory_id].get("created_at")),
            int(memory_id),
        ),
        reverse=True,
    )
    recency_rank = {memory_id: rank for rank, memory_id in enumerate(recency_order, start=1)}

    gravity_items = list(gravity_block.get("items") or [])[:2]
    gravity_rank: dict[int, int] = {}
    gravity_metadata: dict[int, dict[str, Any]] = {}
    for rank, item in enumerate(gravity_items, start=1):
        memory_id = int(item.get("memory_id") or 0)
        if memory_id <= 0 or memory_id not in candidate_items:
            continue
        gravity_rank[memory_id] = rank
        gravity_metadata[memory_id] = {
            "lane": item.get("lane"),
            "gravity_score": item.get("gravity_score"),
            "source_memory_ids": list(item.get("source_memory_ids") or [memory_id]),
        }

    rows: list[dict[str, Any]] = []
    for memory_id, raw in candidate_items.items():
        lexical_position = lexical_rank.get(memory_id)
        semantic_position = semantic_rank.get(memory_id)
        if lexical_position is None and semantic_position is None:
            continue
        gravity_position = gravity_rank.get(memory_id)
        score_parts = {
            "lexical_rrf": _rrf(lexical_position, LEXICAL_WEIGHT),
            "semantic_rrf": _rrf(semantic_position, SEMANTIC_WEIGHT),
            "recency_rrf": _rrf(recency_rank.get(memory_id), RECENCY_WEIGHT),
            "gravity_rrf": _rrf(gravity_position, GRAVITY_WEIGHT),
        }
        score = sum(score_parts.values())
        matched_by = list((lexical_debug.get(memory_id) or {}).get("matched_by") or [])
        if semantic_position is not None and "semantic" not in matched_by:
            matched_by.append("semantic")
        if gravity_position is not None and "gravity" not in matched_by:
            matched_by.append("gravity")
        item = dict(raw)
        item["match_debug"] = {
            **dict(lexical_debug.get(memory_id) or {}),
            "matched_by": matched_by,
            "hybrid_score": round(score, 8),
            "lexical_rank": lexical_position,
            "semantic_rank": semantic_position,
            "recency_rank": recency_rank.get(memory_id),
            "gravity_rank": gravity_position,
            "score_parts": {key: round(value, 8) for key, value in score_parts.items()},
            "semantic": {
                "similarity": (semantic_debug.get(memory_id) or {}).get("similarity"),
                "hybrid_score": (semantic_debug.get(memory_id) or {}).get("hybrid_score"),
                "lexical_coverage": (semantic_debug.get(memory_id) or {}).get("lexical_coverage"),
            },
            "gravity": gravity_metadata.get(memory_id),
        }
        rows.append(item)

    rows.sort(
        key=lambda item: (
            -float((item.get("match_debug") or {}).get("hybrid_score") or 0.0),
            -float(((item.get("match_debug") or {}).get("semantic") or {}).get("hybrid_score") or 0.0),
            int((item.get("match_debug") or {}).get("lexical_rank") or 10**9),
            int((item.get("match_debug") or {}).get("semantic_rank") or 10**9),
            -int(item.get("id") or 0),
        )
    )
    final = rows[: max(1, int(limit))]
    for rank, item in enumerate(final, start=1):
        item["match_debug"]["hybrid_rank"] = rank

    payload = {
        "status": "ok",
        "schema": HYBRID_RETRIEVAL_SCHEMA,
        "retrieval_mode": "lexical_semantic_current_state_recency_gravity",
        "query": str(query).strip(),
        "requested_project_key": requested_project_key,
        "project_key": canonical_project_key,
        "count": len(final),
        "items": final,
        "weights": {
            "rrf_k": RRF_K,
            "lexical": LEXICAL_WEIGHT,
            "semantic": SEMANTIC_WEIGHT,
            "recency": RECENCY_WEIGHT,
            "gravity": GRAVITY_WEIGHT,
        },
        "channels": {
            "lexical_count": len(lexical_rank),
            "semantic_count": len(semantic_rank),
            "union_count": len(candidate_items),
            "gravity_count": len(gravity_rank),
        },
        "gravity": {
            "status": gravity_block.get("status"),
            "reason": gravity_block.get("reason"),
            "selected_memory_ids": sorted(gravity_rank),
        },
        "safety": {
            "read_only": True,
            "gravity_can_introduce_new_candidates": False,
            "durable_importance_modified": False,
            "recall_telemetry_modified": False,
        },
    }
    payload["retrieval_fingerprint"] = _fingerprint(
        {
            "schema": payload["schema"],
            "query": payload["query"],
            "project_key": payload["project_key"],
            "weights": payload["weights"],
            "result_ids": [int(item["id"]) for item in final],
            "scores": [item["match_debug"]["hybrid_score"] for item in final],
            "gravity_ids": payload["gravity"]["selected_memory_ids"],
        }
    )
    return payload
