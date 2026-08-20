from __future__ import annotations

"""Semantic search and embedding payloads."""

from typing import Any, Callable


def search_semantic_payload(
    *,
    query: str,
    top_k: int = 10,
    project_key: str | None = None,
    get_db_connection: Callable[[], Any],
) -> dict[str, Any]:
    try:
        from vector_store import embedding_stats, search_semantic as vector_search_semantic
    except ImportError as e:
        return {"status": "error", "error": f"vector_store niedostÄ™pny: {e}. Zainstaluj: pip install sqlite-vec sentence-transformers"}

    if not query or not query.strip():
        return {"status": "error", "error": "query nie moĹĽe byÄ‡ pusty"}
    if top_k < 1 or top_k > 50:
        top_k = 10

    conn = get_db_connection()
    try:
        results = vector_search_semantic(conn, query.strip(), top_k=top_k, project_key=project_key)
        stats = embedding_stats(conn)
        return {
            "status": "ok",
            "query": query,
            "project_key": project_key,
            "top_k": top_k,
            "results_count": len(results),
            "results": results,
            "embedding_coverage": stats,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


def backfill_semantic_embeddings_payload(
    *,
    project_key: str | None = None,
    get_db_connection: Callable[[], Any],
) -> dict[str, Any]:
    try:
        from vector_store import backfill_embeddings, embedding_stats, ensure_embeddings_table
    except ImportError as e:
        return {"status": "error", "error": f"vector_store niedostÄ™pny: {e}"}

    conn = get_db_connection()
    try:
        ensure_embeddings_table(conn)
        result = backfill_embeddings(conn, project_key=project_key)
        stats = embedding_stats(conn)
        return {
            "status": "ok",
            "backfill": result,
            "coverage": stats,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


def semantic_embedding_stats_payload(*, get_db_connection: Callable[[], Any]) -> dict[str, Any]:
    try:
        from vector_store import embedding_stats, ensure_embeddings_table
    except ImportError as e:
        return {"status": "error", "error": f"vector_store niedostÄ™pny: {e}"}

    conn = get_db_connection()
    try:
        ensure_embeddings_table(conn)
        stats = embedding_stats(conn)
        return {"status": "ok", **stats}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()
