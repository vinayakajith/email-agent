import voyageai
import voyageai.error as verr
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from email_agent.config import RERANK_MODEL, get_settings

# rerank-3 follows natural-language instructions supplied alongside the query.
INSTRUCTION = (
    "Instruction: rank emails from the user's own mailbox by how directly they help answer the "
    "user's question. Prefer emails containing the specific facts asked for (amounts, dates, "
    "names, decisions) over ones that merely mention the topic.\nQuestion: "
)
_RETRYABLE = (verr.RateLimitError, verr.ServiceUnavailableError, verr.APIConnectionError, verr.Timeout, verr.ServerError)


class Reranker:
    def __init__(self):
        self.client = voyageai.AsyncClient(api_key=get_settings().voyage_api_key, timeout=60)

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        wait=wait_random_exponential(min=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def rerank(self, query: str, documents: list[str], top_k: int) -> list[tuple[int, float]]:
        """Return (document index, relevance score) pairs, best first."""
        if not documents:
            return []
        res = await self.client.rerank(INSTRUCTION + query, documents, model=RERANK_MODEL, top_k=top_k)
        return [(r.index, r.relevance_score) for r in res.results]
