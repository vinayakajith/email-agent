from datetime import date

import pytest

from email_agent.db.pool import connection
from email_agent.ingest.enrich import Enricher as RealEnricher  # imported before conftest patches it
from email_agent.ingest import pipeline
from tests.test_fetch import MAILBOX, FakeIMAP


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(pipeline, "GmailIMAP", FakeIMAP)
    monkeypatch.setattr("email_agent.accounts.get_password", lambda email: "secret")


async def test_enrich_saves_facts_and_leaves_failures_for_retry(account):
    from tests.conftest import run_job_now

    await run_job_now(account, "full")
    async with connection() as conn:
        facts = {
            r["merchant"]: r
            for r in await (await conn.execute("SELECT * FROM email_facts")).fetchall()
        }
        stages = dict(
            (r["from_addr"], r["stage"])
            for r in await (await conn.execute("SELECT from_addr, stage FROM emails")).fetchall()
        )
    card = facts["HDFC Bank"]
    assert card["is_credit_card_bill"] and card["currency"] == "INR" and float(card["amount"]) == 12450.0
    assert card["due_date"] == date(2026, 10, 20)
    assert facts["AWS"]["due_date"] is None  # unparseable date is dropped, not fatal
    # The fake raises for this sender; the email must stay at 'fetched' for the next run.
    assert stages["andre@example.fr"] == "fetched"
    assert sum(1 for s in stages.values() if s == "embedded") == len(MAILBOX) - 1


def test_render_email_without_date():
    from email_agent.ingest.enrich import render_email

    text = render_email({
        "from_name": "", "from_addr": "x@y.com", "to_addrs": [], "sent_at": None, "subject": "s",
        "labels": [], "attachments": [], "list_unsubscribe": None, "body_text": "hi",
    })
    assert "Date: unknown" in text


async def test_stale_facts_are_reextracted(ingested):
    from email_agent.ingest.enrich import EXTRACTOR_VERSION
    from email_agent import jobs
    from email_agent.ingest.pipeline import _noop, mark_stale_facts, process_stages

    async with connection() as conn:
        await conn.execute("UPDATE email_facts SET extractor_version = %s", (EXTRACTOR_VERSION - 1,))
    assert await mark_stale_facts(ingested) == len(MAILBOX) - 1
    await process_stages(ingested, await jobs.enqueue(ingested.id, "process"), _noop)
    async with connection() as conn:
        versions = await (await conn.execute("SELECT DISTINCT extractor_version AS v FROM email_facts")).fetchall()
        stages = await (await conn.execute("SELECT stage, count(*) AS n FROM emails GROUP BY stage")).fetchall()
    assert versions == [{"v": EXTRACTOR_VERSION}]
    assert {r["stage"]: r["n"] for r in stages} == {"embedded": len(MAILBOX) - 1, "fetched": 1}


async def test_enricher_request_layout_and_usage():
    from dataclasses import dataclass, field

    from tests.fakes import _facts

    @dataclass
    class Details:
        cached_tokens: int = 700
        cache_write_tokens: int = 0

    @dataclass
    class Usage:
        input_tokens: int = 1000
        output_tokens: int = 50
        input_tokens_details: Details = field(default_factory=Details)

    class Client:
        def __init__(self):
            self.calls = []
            self.responses = self

        async def parse(self, **kwargs):
            self.calls.append(kwargs)
            return type("R", (), {"output_parsed": _facts(summary="x"), "usage": Usage()})()

    client = Client()
    enricher = RealEnricher("me@gmail.com", "email-agent:enrich:1", client=client)
    email = {"from_name": "A", "from_addr": "a@x.com", "to_addrs": [], "sent_at": None, "subject": "s",
             "labels": [], "attachments": [], "list_unsubscribe": None, "body_text": "hello"}
    await enricher.extract(email)
    await enricher.extract({**email, "body_text": "different"})

    first, second = client.calls
    # Same breakpoint-marked instructions block first; only the email differs.
    assert first["input"][0] == second["input"][0]
    assert first["input"][0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert "prompt_cache_breakpoint" not in first["input"][1]["content"][0]
    assert first["prompt_cache_key"] == "email-agent:enrich:1" and first["prompt_cache_options"] == {"mode": "explicit"}
    assert enricher.usage == {"in": 2000, "cached": 1400, "cache_write": 0, "out": 100}
