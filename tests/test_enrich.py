from datetime import date

import pytest

from email_agent.db.pool import connection
from email_agent.ingest import pipeline
from tests.test_fetch import MAILBOX, FakeIMAP


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(pipeline, "GmailIMAP", FakeIMAP)
    monkeypatch.setattr("email_agent.accounts.get_password", lambda email: "secret")


async def test_enrich_saves_facts_and_leaves_failures_for_retry(account):
    await pipeline.run_ingest(account, await pipeline.plan_fetch(account, None), None)
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
