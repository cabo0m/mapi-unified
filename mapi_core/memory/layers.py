from __future__ import annotations

"""Memory layer promotion and demotion payloads."""

from typing import Any, Callable


def validate_layer_transition(
    from_layer: str | None,
    to_layer: str,
    *,
    layer_order: list[str] | tuple[str, ...],
) -> None:
    """Raises ValueError if the from->to direction is invalid or layers are unknown."""
    to_layer = (to_layer or "").strip().lower()
    if to_layer not in layer_order:
        raise ValueError(f"Nieznana warstwa docelowa: '{to_layer}'. DostÄ™pne: {', '.join(layer_order)}")
    if from_layer is None:
        return
    from_layer = (from_layer or "").strip().lower()
    if from_layer not in layer_order:
        return
    if from_layer == to_layer:
        raise ValueError(f"Wspomnienie jest juĹĽ w warstwie '{to_layer}'")


def layer_move_payload(
    conn: Any,
    memory_id: int,
    target_layer: str,
    reason: str,
    direction: str,
    *,
    layer_order: list[str] | tuple[str, ...],
    require_memory_row: Callable[[Any, int], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
    validate_layer_transition: Callable[[str | None, str], None],
    record_timeline_event: Callable[..., Any],
) -> dict[str, Any]:
    row = require_memory_row(conn, memory_id)
    memory = row_to_dict(row)
    from_layer = memory.get("layer_code")
    validate_layer_transition(from_layer, target_layer)

    from_idx = layer_order.index(from_layer) if from_layer in layer_order else -1
    to_idx = layer_order.index(target_layer)

    if direction == "promote" and from_idx >= to_idx:
        raise ValueError(
            f"Awans wymaga wyĹĽszej warstwy. Obecna: '{from_layer}' (poziom {from_idx}), docelowa: '{target_layer}' (poziom {to_idx})."
        )
    if direction == "demote" and from_idx <= to_idx and from_idx != -1:
        raise ValueError(
            f"Degradacja wymaga niĹĽszej warstwy. Obecna: '{from_layer}' (poziom {from_idx}), docelowa: '{target_layer}' (poziom {to_idx})."
        )

    if direction == "promote":
        conn.execute(
            "UPDATE memories SET layer_code = ?, promoted_from_id = ?, sandman_note = ? WHERE id = ?",
            (target_layer, memory_id, f"Promoted from '{from_layer}' to '{target_layer}': {reason}", memory_id),
        )
    else:
        conn.execute(
            "UPDATE memories SET layer_code = ?, demoted_from_id = ?, sandman_note = ? WHERE id = ?",
            (target_layer, memory_id, f"Demoted from '{from_layer}' to '{target_layer}': {reason}", memory_id),
        )

    try:
        record_timeline_event(
            conn,
            event_type=f"sandman.layer_{direction}d",
            memory_id=memory_id,
            summary=f"Layer {direction}: {from_layer} â†’ {target_layer}",
            details={"reason": reason, "from_layer": from_layer, "to_layer": target_layer},
            origin="memory_api",
        )
    except Exception:
        pass

    conn.commit()
    updated = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return row_to_dict(updated)


def promote_memory_payload(
    conn: Any,
    *,
    memory_id: int,
    target_layer: str,
    reason: str,
    protected_layers: set[str],
    normalize_layer_code: Callable[[Any], str | None],
    layer_move: Callable[[Any, int, str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    if not reason or not reason.strip():
        return {"status": "error", "error": "Pole 'reason' jest wymagane"}
    target = normalize_layer_code(target_layer)
    if target is None:
        return {"status": "error", "error": f"Nieznana warstwa: '{target_layer}'"}
    if target in protected_layers:
        return {"status": "error", "error": f"Warstwa '{target}' jest chroniona â€” awans wymaga rÄ™cznej decyzji operatora"}
    try:
        result = layer_move(conn, memory_id, target, reason.strip(), "promote")
        return {"status": "promoted", "memory": result, "target_layer": target}
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def demote_memory_payload(
    conn: Any,
    *,
    memory_id: int,
    target_layer: str,
    reason: str,
    protected_layers: set[str],
    normalize_layer_code: Callable[[Any], str | None],
    require_memory_row: Callable[[Any, int], Any],
    row_to_dict: Callable[[Any], dict[str, Any]],
    layer_move: Callable[[Any, int, str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    if not reason or not reason.strip():
        return {"status": "error", "error": "Pole 'reason' jest wymagane"}
    target = normalize_layer_code(target_layer)
    if target is None:
        return {"status": "error", "error": f"Nieznana warstwa: '{target_layer}'"}
    try:
        row = require_memory_row(conn, memory_id)
        memory = row_to_dict(row)
        current_layer = memory.get("layer_code")
        if current_layer in protected_layers:
            return {"status": "error", "error": f"Wspomnienie jest w chronionej warstwie '{current_layer}' â€” degradacja zablokowana"}
        result = layer_move(conn, memory_id, target, reason.strip(), "demote")
        return {"status": "demoted", "memory": result, "target_layer": target}
    except ValueError as e:
        return {"status": "error", "error": str(e)}


def promotion_candidates_payload(
    conn: Any,
    *,
    min_evidence: int = 2,
    min_importance: float = 0.6,
    min_confidence: float = 0.6,
    source_layer: str | None = None,
    limit: int = 50,
    get_promotion_candidates: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    candidates = get_promotion_candidates(
        conn,
        source_layer=source_layer,
        min_evidence=min_evidence,
        min_importance=min_importance,
        min_confidence=min_confidence,
        limit=limit,
    )
    return {
        "status": "ok",
        "count": len(candidates),
        "candidates": candidates,
        "filters": {
            "min_evidence": min_evidence,
            "min_importance": min_importance,
            "min_confidence": min_confidence,
            "source_layer": source_layer,
            "limit": limit,
        },
    }
