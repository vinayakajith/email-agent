"""Semantic retrieval over pgvector HNSW with Voyage query embeddings."""

import numpy as np

from email_agent.db.pool import connection
from email_agent.search.filters import SearchFilters


async def vector_search(
    account_id: int, query_vector: list[float], filters: SearchFilters, limit: int = 50
) -> list[dict]:
    where, params = filters.chunk_sql(account_id)
    vec = np.asarray(query_vector, dtype=np.float32)
    # Iterative scans keep filtered HNSW queries from returning too few rows; relaxed order
    # needs the outer re-sort.
    sql = f"""
        WITH nearest AS MATERIALIZED (
            SELECT c.id, c.email_id, c.embedding <=> %s AS distance
            FROM chunks c
            WHERE {where}
            ORDER BY distance
            LIMIT %s
        )
        SELECT id, email_id, 1 - distance AS score FROM nearest ORDER BY distance
    """
    async with connection() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            await conn.execute("SET LOCAL hnsw.ef_search = 100")
            return await (await conn.execute(sql, [vec, *params, limit])).fetchall()
