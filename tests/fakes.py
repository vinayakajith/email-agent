"""Deterministic stand-ins for OpenAI and Voyage so tests run offline."""

from email_agent.ingest.enrich import EmailFacts


def _facts(**over) -> EmailFacts:
    base = dict(
        category="other", priority=1, needs_action=False, action_summary=None, deadline=None, amount=None,
        amount_type=None, currency=None, merchant=None, is_recurring=False, billing_period=None,
        due_date=None, is_credit_card_bill=False, summary="",
    )
    return EmailFacts(**{**base, **over})


class FakeEnricher:
    model = "fake-enrich"

    def __init__(self, owner: str = "me@gmail.com", prompt_cache_key: str | None = None):
        self.calls = 0
        self.usage = {"in": 0, "cached": 0, "cache_write": 0, "out": 0}

    async def extract(self, email: dict) -> EmailFacts:
        self.calls += 1
        subject = email["subject"].lower()
        if email["from_addr"] == "andre@example.fr":
            raise RuntimeError("model exploded")
        if "credit card" in subject:
            return _facts(category="bill", priority=3, needs_action=True, action_summary="Pay HDFC card",
                          amount=12450.0, amount_type="amount_due", currency="inr", merchant="HDFC Bank",
                          due_date="2026-10-20", deadline="2026-10-20", is_credit_card_bill=True,
                          summary="HDFC credit card statement")
        if "membership" in subject:
            return _facts(category="subscription", priority=0, amount=15.49, amount_type="charged",
                          currency="USD", merchant="Netflix", is_recurring=True, billing_period="monthly",
                          summary="Netflix renewal")
        if "aws" in subject:
            return _facts(category="bill", priority=2, needs_action=True, amount=42.17, amount_type="amount_due",
                          currency="USD", merchant="AWS", is_recurring=True, billing_period="monthly",
                          due_date="not a date", summary="AWS invoice")
        return _facts(category="work", priority=2, needs_action=True, action_summary="Send final deck",
                      summary="Priya asks for the final deck")


import hashlib
import math
import re

_WORD = re.compile(r"[a-z0-9]+")


def _hash_vector(text: str, dim: int = 1024) -> list[float]:
    vec = [0.0] * dim
    for w in _WORD.findall(text.lower()):
        vec[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class FakeEmbedder:
    """Bag-of-words hashing: texts sharing words get similar vectors."""

    async def embed_documents(self, texts, token_counts):
        return [_hash_vector(t) for t in texts]

    async def embed_query(self, query):
        return _hash_vector(query)


class FakeReranker:
    async def rerank(self, query, documents, top_k):
        q = set(_WORD.findall(query.lower()))
        scored = [(i, len(q & set(_WORD.findall(d.lower()))) / (len(q) or 1)) for i, d in enumerate(documents)]
        return sorted(scored, key=lambda x: x[1], reverse=True)[:top_k]
