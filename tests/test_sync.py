from dataclasses import replace

import pytest

from email_agent.db.pool import connection
from email_agent.ingest import pipeline
from tests.test_fetch import MAILBOX, FakeIMAP

NEW_MAIL = b"""From: Bob <bob@example.com>
To: me@gmail.com
Subject: Dinner on Saturday?
Date: Fri, 09 Oct 2026 19:00:00 +0530
Message-ID: <new-1@example.com>
Content-Type: text/plain; charset="utf-8"

Are you free for dinner on Saturday at 8?
"""


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(pipeline, "GmailIMAP", FakeIMAP)
    monkeypatch.setattr("email_agent.accounts.get_password", lambda email: "secret")


async def test_sync_fetches_only_new_mail_and_refreshes_flags(ingested, monkeypatch):
    with pytest.raises(RuntimeError):
        await pipeline.run_sync(replace(ingested, id=999))  # never ingested

    new_uid = max(MAILBOX) + 1
    monkeypatch.setitem(MAILBOX, new_uid, replace(MAILBOX[1], uid=new_uid, gm_msgid=9999, flags=[], raw=NEW_MAIL))
    monkeypatch.setitem(MAILBOX, 2, replace(MAILBOX[2], flags=["\\Seen", "\\Flagged"]))  # read + starred in Gmail

    fetched, refreshed = await pipeline.run_sync(ingested)
    assert fetched == 1 and refreshed == len(MAILBOX)
    async with connection() as conn:
        new = await (await conn.execute("SELECT stage, is_unread FROM emails WHERE gm_msgid = 9999")).fetchone()
        flagged = await (await conn.execute("SELECT is_unread, is_starred FROM emails WHERE uid = 2")).fetchone()
        state = await (await conn.execute("SELECT last_uid FROM sync_state")).fetchone()
    assert new == {"stage": "embedded", "is_unread": True}
    assert flagged == {"is_unread": False, "is_starred": True}
    assert state["last_uid"] == new_uid

    assert await pipeline.run_sync(ingested) == (0, len(MAILBOX))
