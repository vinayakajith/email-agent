"""Hybrid retrieval: BM25 + vector in parallel -> reciprocal rank fusion -> rerank-3."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from email_agent.db.pool import connection
from email_agent.ingest.embed import Embedder
from email_agent.search.bm25 import bm25_search
from email_agent.search.filters import SearchFilters
from email_agent.search.rerank import Reranker
from email_agent.search.vector import vector_search

RRF_K = 60
CANDIDATES_PER_RETRIEVER = 50
RERANK_POOL = 40
Mode = Literal["bm25", "vector", "hybrid"]

_embedder: Embedder | None = None
_reranker: Reranker | None = None


def get_embedder() -> Embedder:
    global _embedder
    _embedder = _embedder or Embedder()
    return _embedder


def get_reranker() -> Reranker:
    global _reranker
    _reranker = _reranker or Reranker()
    return _reranker


@dataclass
class Hit:
    email_id: int
    thread_id: int | None
    subject: str
    from_name: str
    from_addr: str
    sent_at: datetime | None
    excerpt: str
    score: float
    ranks: dict = field(default_factory=dict)  # per-stage ranks for debugging and evals


def rrf_fuse(rank_lists: dict[str, list[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal rank fusion: score(d) = sum over lists of 1 / (k + rank), rank starting at 1."""
    scores: dict[int, float] = {}
    for ids in rank_lists.values():
        for rank, doc_id in enumerate(ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def collapse_to_emails(fused: list[tuple[int, float]], chunk_to_email: dict[int, int], limit: int) -> list[tuple[int, int]]:
    """Keep the best chunk per email, in fused order. Returns (email_id, chunk_id)."""
    seen: set[int] = set()
    out = []
    for chunk_id, _ in fused:
        email_id = chunk_to_email[chunk_id]
        if email_id not in seen:
            seen.add(email_id)
            out.append((email_id, chunk_id))
            if len(out) == limit:
                break
    return out


async def _load(pairs: list[tuple[int, int]]) -> dict[int, dict]:
    if not pairs:
        return {}
    async with connection() as conn:
        rows = await (
            await conn.execute(
                """SELECT c.id AS chunk_id, c.content, e.id AS email_id, e.gm_thrid, e.subject,
                          e.from_name, e.from_addr, e.sent_at
                   FROM chunks c JOIN emails e ON e.id = c.email_id WHERE c.id = ANY(%s)""",
                ([c for _, c in pairs],),
            )
        ).fetchall()
    return {r["chunk_id"]: r for r in rows}


async def hybrid_search(
    query: str,
    filters: SearchFilters | None = None,
    k: int = 10,
    mode: Mode = "hybrid",
    rerank: bool = True,
) -> list[Hit]:
    filters = filters or SearchFilters()
    lists: dict[str, list[dict]] = {}
    if mode == "bm25":
        lists["bm25"] = await bm25_search(query, filters, CANDIDATES_PER_RETRIEVER)
    else:
        qvec = await get_embedder().embed_query(query)
        if mode == "vector":
            lists["vector"] = await vector_search(qvec, filters, CANDIDATES_PER_RETRIEVER)
        else:
            lists["bm25"], lists["vector"] = await asyncio.gather(
                bm25_search(query, filters, CANDIDATES_PER_RETRIEVER),
                vector_search(qvec, filters, CANDIDATES_PER_RETRIEVER),
            )

    chunk_to_email = {r["id"]: r["email_id"] for rows in lists.values() for r in rows}
    stage_ranks = {name: {r["id"]: i for i, r in enumerate(rows, start=1)} for name, rows in lists.items()}
    fused = rrf_fuse({name: [r["id"] for r in rows] for name, rows in lists.items()})
    pairs = collapse_to_emails(fused, chunk_to_email, RERANK_POOL if rerank else k)
    rows = await _load(pairs)
    candidates = [rows[c] for _, c in pairs if c in rows]

    def hit(r: dict, score: float, fused_rank: int, rerank_rank: int | None = None) -> Hit:
        ranks = {name: ranks.get(r["chunk_id"]) for name, ranks in stage_ranks.items()}
        ranks["fused"] = fused_rank
        if rerank_rank is not None:
            ranks["rerank"] = rerank_rank
        return Hit(r["email_id"], r["gm_thrid"], r["subject"], r["from_name"], r["from_addr"], r["sent_at"],
                   r["content"], score, ranks)

    if not rerank or not candidates:
        fused_scores = dict(fused)
        return [hit(r, fused_scores[r["chunk_id"]], i) for i, r in enumerate(candidates[:k], start=1)]

    order = await get_reranker().rerank(query, [r["content"] for r in candidates], top_k=k)
    return [hit(candidates[idx], score, idx + 1, i) for i, (idx, score) in enumerate(order, start=1)]
