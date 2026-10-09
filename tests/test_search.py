from datetime import date

from email_agent.ingest.embed import MAX_BATCH_TEXTS, batches
from email_agent.search.filters import SearchFilters
from email_agent.search.hybrid import collapse_to_emails, hybrid_search, rrf_fuse


def test_rrf_rewards_agreement_between_retrievers():
    fused = rrf_fuse({"bm25": [1, 2, 3], "vector": [3, 4, 1]})
    order = [d for d, _ in fused]
    assert order[:2] == [1, 3]  # in both lists
    assert abs(dict(fused)[1] - (1 / 61 + 1 / 63)) < 1e-12


def test_collapse_keeps_best_chunk_per_email():
    fused = [(10, 0.5), (11, 0.4), (20, 0.3), (12, 0.2)]
    assert collapse_to_emails(fused, {10: 1, 11: 1, 12: 1, 20: 2}, limit=5) == [(1, 10), (2, 20)]


def test_batches_respect_text_and_token_budgets():
    assert [len(r) for r in batches([10] * (MAX_BATCH_TEXTS + 5))] == [MAX_BATCH_TEXTS, 5]
    assert [list(r) for r in batches([50_000, 50_000, 10])] == [[0], [1, 2]]


async def test_every_stage_reaches_embedded(ingested):
    from email_agent.ingest.pipeline import stage_counts

    info = await stage_counts(ingested.id)
    assert info["stages"] == {"embedded": 4, "fetched": 1}  # one email failed enrichment
    assert info["chunks"] == 4


async def test_bm25_vector_and_hybrid_find_the_card_statement(ingested):
    for mode in ("bm25", "vector", "hybrid"):
        hits = await hybrid_search("credit card statement amount due", k=3, mode=mode)
        assert hits, mode
        assert hits[0].from_addr == "alerts@hdfcbank.net", mode
    hit = (await hybrid_search("credit card statement", k=1))[0]
    assert set(hit.ranks) == {"bm25", "vector", "fused", "rerank"}


async def test_filters_apply_to_both_retrievers(ingested):
    f = SearchFilters(sender="netflix")
    for mode in ("bm25", "vector", "hybrid"):
        hits = await hybrid_search("membership renewed", f, mode=mode)
        assert {h.from_addr for h in hits} == {"info@mailer.netflix.com"}, mode
    f = SearchFilters(date_from=date(2026, 10, 1), category="bill")
    hits = await hybrid_search("invoice statement", f, mode="hybrid")
    assert {h.from_addr for h in hits} == {"alerts@hdfcbank.net", "billing@aws.amazon.com"}
    assert await hybrid_search("membership", SearchFilters(date_to=date(2026, 9, 1))) == []
