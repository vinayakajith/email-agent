import json

import pytest

from email_agent.agent.events import ConfirmationRequired, Observation
from email_agent.agent.loop import Agent
from email_agent.agent.tools import ToolContext, call_tool
from email_agent.db.pool import connection
from email_agent.smtp import sender
from email_agent.smtp.sender import build_message
from tests.test_agent_loop import Item, Resp, ScriptedClient, call


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    def fake_send(host, email, password, msg):
        sent.append(msg)
        return msg["Message-ID"]

    monkeypatch.setattr(sender, "send_message", fake_send)
    monkeypatch.setattr("email_agent.accounts.get_password", lambda email: "secret")
    return sent


async def _email_id(subject_like: str) -> int:
    async with connection() as conn:
        return (await (await conn.execute("SELECT id FROM emails WHERE subject ILIKE %s", (subject_like,))).fetchone())["id"]


def test_build_reply_threads_correctly():
    msg = build_message("me@gmail.com", ["a@x.com"], [], "Re: Hi", "Body", "<orig@x>", "<root@x>")
    assert msg["In-Reply-To"] == "<orig@x>"
    assert msg["References"] == "<root@x> <orig@x>"
    assert msg["Message-ID"].endswith("@gmail.com>")


async def test_reply_draft_defaults_recipient_and_subject(ingested):
    ctx = ToolContext(ingested)
    reply_to = await _email_id("Re: Q4 planning deck")
    d = await call_tool(ctx, "draft_email", json.dumps({"body": "Will do.", "reply_to_email_id": reply_to}))
    assert d["to"] == ["priya@acme.com"] and d["subject"] == "Re: Q4 planning deck"
    bad = await call_tool(ctx, "draft_email", json.dumps({"body": "x", "to": ["not an email"], "subject": "s"}))
    assert "error" in bad


async def test_send_requires_confirmation(ingested, outbox):
    declined = ToolContext(ingested)  # default confirm denies
    d = await call_tool(declined, "draft_email", json.dumps({"to": ["a@x.com"], "subject": "Hi", "body": "Hello"}))
    res = await call_tool(declined, "send_email", json.dumps({"draft_id": d["draft_id"]}))
    assert res["sent"] is False and outbox == []

    async def yes(draft):
        return True

    approved = ToolContext(ingested, confirm=yes)
    res = await call_tool(approved, "send_email", json.dumps({"draft_id": d["draft_id"]}))
    assert res["sent"] is True and len(outbox) == 1
    assert outbox[0]["To"] == "a@x.com" and outbox[0]["From"] == "me@gmail.com"
    again = await call_tool(approved, "send_email", json.dumps({"draft_id": d["draft_id"]}))
    assert "already sent" in again["error"] and len(outbox) == 1


async def test_loop_emits_confirmation_and_sends_reply(ingested, outbox):
    reply_to = await _email_id("Re: Q4 planning deck")
    seen = []

    async def confirm(draft):
        seen.append(draft)
        return True

    client = ScriptedClient([
        Resp([call("c1", "draft_email", body="Sending Friday.", reply_to_email_id=reply_to)]),
        Resp([call("c2", "send_email", draft_id=1)]),
        Resp([Item("message", text="Sent your reply to Priya.")]),
    ])
    events = [e async for e in Agent(ingested, confirm=confirm, client=client).run("reply to priya that I'll send it friday")]
    confirm_events = [e for e in events if isinstance(e, ConfirmationRequired)]
    assert len(confirm_events) == 1 and confirm_events[0].draft["to"] == ["priya@acme.com"]
    assert seen and seen[0]["subject"] == "Re: Q4 planning deck"
    sent_obs = [e for e in events if isinstance(e, Observation) and e.name == "send_email"][0]
    assert sent_obs.result["sent"] is True
    assert outbox[0]["In-Reply-To"] == "<r2@acme.com>"
