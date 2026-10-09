from pathlib import Path

import pytest

from email_agent.db.pool import connection
from email_agent.ingest import pipeline
from email_agent.ingest.imap_client import RawMessage

FIXTURES = Path(__file__).parent / "fixtures"
MAILBOX = {
    uid: RawMessage(uid, 1000 + uid, 5000 + uid, ["\\Inbox"], ["\\Seen"] if uid % 2 else [], 100, None, path.read_bytes())
    for uid, path in enumerate(sorted(FIXTURES.glob("*.eml")), start=1)
}


class FakeIMAP:
    uidvalidity = 7

    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def search(self, since=None, min_uid=None):
        return [u for u in MAILBOX if min_uid is None or u >= min_uid]

    def gm_msgids(self, uids):
        return {u: MAILBOX[u].gm_msgid for u in uids}

    def fetch(self, uids):
        return [MAILBOX[u] for u in uids]


@pytest.fixture(autouse=True)
def fake_imap(monkeypatch):
    monkeypatch.setattr(pipeline, "GmailIMAP", FakeIMAP)
    monkeypatch.setattr("email_agent.accounts.get_password", lambda email: "secret")


async def test_ingest_stores_emails_and_is_idempotent(account):
    plan = await pipeline.plan_fetch(account, None)
    assert len(plan.to_fetch) == len(MAILBOX)
    await pipeline.run_ingest(account, plan, None)

    again = await pipeline.plan_fetch(account, None)
    assert again.to_fetch == [] and again.already_stored == len(MAILBOX)

    async with connection() as conn:
        rows = await (await conn.execute("SELECT uid, from_addr, is_unread, stage FROM emails ORDER BY uid")).fetchall()
        sync = await (await conn.execute("SELECT * FROM sync_state")).fetchone()
        job = await (await conn.execute("SELECT * FROM ingest_jobs")).fetchone()
    assert len(rows) == len(MAILBOX)
    assert all(r["stage"] == "fetched" for r in rows)
    assert rows[0]["is_unread"] is False and rows[1]["is_unread"] is True
    assert sync["last_uid"] == len(MAILBOX) and sync["since_date"] is None
    assert job["status"] == "done" and job["fetched"] == len(MAILBOX)
