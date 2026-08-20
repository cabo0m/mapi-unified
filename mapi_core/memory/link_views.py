from __future__ import annotations

"""Read-only response builders for memory links."""

from typing import Any, Callable


CANONICAL_TRUTH_RELATIONS = frozenset({"supports", "contradicts", "supersedes", "refines", "derived_from"})


def link_memories_payload(
    conn: Any,
    *,
    from_memory_id: int,
    to_memory_id: int,
    relation_type: str,
    weight: float = 0.5,
    origin: str | None = None,
    allow_legacy_unsafe: bool = False,
    new_operation_id: Callable[[str], str],
    create_link: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    if not relation_type or not relation_type.strip():
        return {"status": "error", "error": "relation_type nie moĹĽe byÄ‡ puste"}
    normalized_relation = relation_type.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized_relation in CANONICAL_TRUTH_RELATIONS and not bool(allow_legacy_unsafe):
        return {
            "status": "blocked",
            "error": "canonical_relation_requires_evidence_bound_route",
            "relation_type": normalized_relation,
            "canonical_route": "memory.relation_preview/relation_apply or dedicated lifecycle/review route",
            "legacy_unsafe_available": True,
        }
    if conn.execute("SELECT id FROM memories WHERE id = ?", (from_memory_id,)).fetchone() is None:
        return {"status": "error", "error": "Jedno lub oba wspomnienia nie istniejÄ…"}
    if conn.execute("SELECT id FROM memories WHERE id = ?", (to_memory_id,)).fetchone() is None:
        return {"status": "error", "error": "Jedno lub oba wspomnienia nie istniejÄ…"}
    operation_id = new_operation_id("link")
    link = create_link(
        conn,
        from_memory_id,
        to_memory_id,
        relation_type.strip(),
        float(weight),
        origin.strip() if isinstance(origin, str) else origin,
        operation_id=operation_id,
    )
    conn.commit()
    return {"status": "created", "link": link}


def memory_links_response(
    memory_id: int,
    outgoing_rows: list[Any],
    incoming_rows: list[Any],
    *,
    row_to_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    outgoing_links = [row_to_dict(row) for row in outgoing_rows]
    incoming_links = [row_to_dict(row) for row in incoming_rows]
    links: list[dict[str, Any]] = []
    for link in outgoing_links:
        item = dict(link)
        item["direction"] = "outgoing"
        item["other_memory_id"] = item.get("to_memory_id")
        links.append(item)
    for link in incoming_links:
        item = dict(link)
        item["direction"] = "incoming"
        item["other_memory_id"] = item.get("from_memory_id")
        links.append(item)
    links.sort(key=lambda item: int(item.get("id") or 0))
    return {
        "memory_id": memory_id,
        "link_count": len(links),
        "outgoing_link_count": len(outgoing_links),
        "incoming_link_count": len(incoming_links),
        "links": links,
        "outgoing_links": outgoing_links,
        "incoming_links": incoming_links,
    }


def attach_links_to_memory_items(
    conn: Any,
    items: list[dict[str, Any]],
    *,
    row_to_dict: Callable[[Any], dict[str, Any]],
    include_links: bool = False,
) -> list[dict[str, Any]]:
    if not include_links or not items:
        return items
    for item in items:
        memory_id = int(item["id"])
        outgoing = conn.execute(
            "SELECT * FROM memory_links WHERE archived_at IS NULL AND from_memory_id = ? ORDER BY id ASC",
            (memory_id,),
        ).fetchall()
        incoming = conn.execute(
            "SELECT * FROM memory_links WHERE archived_at IS NULL AND to_memory_id = ? ORDER BY id ASC",
            (memory_id,),
        ).fetchall()
        link_payload = memory_links_response(memory_id, outgoing, incoming, row_to_dict=row_to_dict)
        item["link_count"] = link_payload["link_count"]
        item["outgoing_link_count"] = link_payload["outgoing_link_count"]
        item["incoming_link_count"] = link_payload["incoming_link_count"]
        item["links"] = link_payload["links"]
        item["outgoing_links"] = link_payload["outgoing_links"]
        item["incoming_links"] = link_payload["incoming_links"]
        linked_memories = {
            int(link["other_memory_id"])
            for link in link_payload["links"]
            if link.get("other_memory_id") is not None
        }
        item["linked_memories"] = sorted(linked_memories)
    return items
