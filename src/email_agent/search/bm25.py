"""Keyword retrieval with real BM25 scoring via ParadeDB pg_search."""

from email_agent.db.pool import connection
from email_agent.search.filters import SearchFilters


async def bm25_search(query: str, filters: SearchFilters, limit: int = 50) -> list[dict]:
    where, params = filters.chunk_sql()
    # `|||` matches any query term (OR); BM25 then ranks by term rarity and frequency.
    sql = f"""
        SELECT c.id, c.email_id, pdb.score(c.id) AS score
        FROM chunks c
        WHERE c.content ||| %s AND {where}
        ORDER BY score DESC
        LIMIT %s
    """
    async with connection() as conn:
        return await (await conn.execute(sql, [query, *params, limit])).fetchall()
