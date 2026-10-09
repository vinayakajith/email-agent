"""Voyage embeddings: token-aware batching and the embed stage of the pipeline."""

import asyncio
from collections.abc import Sequence

import numpy as np
import voyageai
import voyageai.error as verr
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from email_agent.config import EMBED_DIM, EMBED_MODEL, get_settings
from email_agent.db.pool import connection
from email_agent.ingest.chunker import chunk_email

# Stay well under Voyage's per-request limits (texts and total tokens); tiktoken counts are
# only an approximation of Voyage's tokenizer, hence the margin.
MAX_BATCH_TEXTS = 128
MAX_BATCH_TOKENS = 80_000
PAGE_SIZE = 100

_RETRYABLE = (verr.RateLimitError, verr.ServiceUnavailableError, verr.APIConnectionError, verr.Timeout, verr.ServerError, verr.TryAgain)


def batches(token_counts: Sequence[int]) -> list[range]:
    """Group consecutive indices into batches under both the text and token budgets."""
    out, start, tokens = [], 0, 0
    for i, n in enumerate(token_counts):
        if i > start and (i - start >= MAX_BATCH_TEXTS or tokens + n > MAX_BATCH_TOKENS):
            out.append(range(start, i))
            start, tokens = i, 0
        tokens += n
    if start < len(token_counts):
        out.append(range(start, len(token_counts)))
    return out


class Embedder:
    def __init__(self):
        self.client = voyageai.AsyncClient(api_key=get_settings().voyage_api_key, timeout=120)

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    async def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        res = await self.client.embed(texts, model=EMBED_MODEL, input_type=input_type, output_dimension=EMBED_DIM)
        return res.embeddings

    async def embed_documents(self, texts: list[str], token_counts: list[int]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for r in batches(token_counts):
            vectors.extend(await self._embed([texts[i] for i in r], "document"))
        return vectors

    async def embed_query(self, query: str) -> list[float]:
        return (await self._embed([query], "query"))[0]


INSERT_CHUNK = """
INSERT INTO chunks (account_id, email_id, chunk_index, content, token_count, sent_at, from_addr, category, embedding)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


async def embed_stage(account, job_id: int, progress, embedder: Embedder | None = None) -> int:
    """Chunk and embed every email at stage 'enriched'."""
    embedder = embedder or Embedder()
    async with connection() as conn:
        total = (
            await (
                await conn.execute(
                    "SELECT count(*) AS n FROM emails WHERE account_id = %s AND stage = 'enriched'", (account.id,)
                )
            ).fetchone()
        )["n"]
    done = 0
    await progress("embed", 0, total)
    while True:
        async with connection() as conn:
            emails = await (
                await conn.execute(
                    """SELECT e.id, e.from_name, e.from_addr, e.to_addrs, e.sent_at, e.subject, e.body_text,
                              f.summary, f.category
                       FROM emails e LEFT JOIN email_facts f ON f.email_id = e.id
                       WHERE e.account_id = %s AND e.stage = 'enriched'
                       ORDER BY e.id LIMIT %s""",
                    (account.id, PAGE_SIZE),
                )
            ).fetchall()
        if not emails:
            break
        # Tokenizing and chunking is CPU-bound; keep it off the event loop.
        rows = await asyncio.to_thread(lambda: [(e, c) for e in emails for c in chunk_email(e)])
        vectors = await embedder.embed_documents([c.content for _, c in rows], [c.token_count for _, c in rows])
        ids = [e["id"] for e in emails]
        async with connection() as conn:
            async with conn.transaction():
                # Re-embedding replaces old chunks, keeping the stage idempotent.
                await conn.execute("DELETE FROM chunks WHERE email_id = ANY(%s)", (ids,))
                async with conn.cursor() as cur:
                    await cur.executemany(
                        INSERT_CHUNK,
                        [
                            (account.id, e["id"], c.index, c.content, c.token_count, e["sent_at"], e["from_addr"], e["category"],
                             np.asarray(v, dtype=np.float32))
                            for (e, c), v in zip(rows, vectors)
                        ],
                    )
                await conn.execute("UPDATE emails SET stage = 'embedded' WHERE id = ANY(%s)", (ids,))
                await conn.execute("UPDATE ingest_jobs SET embedded = embedded + %s WHERE id = %s", (len(ids), job_id))
        done += len(emails)
        await progress("embed", done, total)
    return done
