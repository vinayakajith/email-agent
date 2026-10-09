import json

import pytest

from email_agent import conversations
from email_agent.agent.events import ConfirmationRequired, FinalAnswer, Observation
from email_agent.agent.loop import Agent
from email_agent.agent.tools import ToolContext, call_tool
from email_agent.db.pool import connection
from email_agent.drafts import DraftError, approve_draft, pending_drafts, reject_draft
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


async def test_agent_can_only_request_approval(ingested, outbox):
    ctx = ToolContext(ingested)
    d = await call_tool(ctx, "draft_email", json.dumps({"to": ["a@x.com"], "subject": "Hi", "body": "Hello"}))
    with pytest.raises(DraftError):
        await approve_draft(ingested, d["draft_id"])  # never requested: cannot be sent
    res = await call_tool(ctx, "send_email", json.dumps({"draft_id": d["draft_id"]}))
    assert res["awaiting_user_approval"] is True and outbox == []
    assert [p["draft_id"] for p in await pending_drafts(ingested)] == [d["draft_id"]]

    assert (await approve_draft(ingested, d["draft_id"]))["sent"] is True
    assert len(outbox) == 1 and outbox[0]["To"] == "a@x.com" and outbox[0]["From"] == "me@gmail.com"
    with pytest.raises(DraftError):
        await approve_draft(ingested, d["draft_id"])  # sends exactly once
    assert len(outbox) == 1 and await pending_drafts(ingested) == []


async def test_reject_discards(ingested, outbox):
    ctx = ToolContext(ingested)
    d = await call_tool(ctx, "draft_email", json.dumps({"to": ["a@x.com"], "subject": "Hi", "body": "Hello"}))
    await call_tool(ctx, "send_email", json.dumps({"draft_id": d["draft_id"]}))
    await reject_draft(ingested, d["draft_id"])
    with pytest.raises(DraftError):
        await approve_draft(ingested, d["draft_id"])
    assert outbox == []


async def test_failed_send_stays_awaiting_approval(ingested, monkeypatch):
    def broken(*a):
        raise OSError("smtp down")

    monkeypatch.setattr(sender, "send_message", broken)
    monkeypatch.setattr("email_agent.accounts.get_password", lambda email: "secret")
    ctx = ToolContext(ingested)
    d = await call_tool(ctx, "draft_email", json.dumps({"to": ["a@x.com"], "subject": "Hi", "body": "Hello"}))
    await call_tool(ctx, "send_email", json.dumps({"draft_id": d["draft_id"]}))
    with pytest.raises(DraftError, match="smtp down"):
        await approve_draft(ingested, d["draft_id"])
    assert [p["draft_id"] for p in await pending_drafts(ingested)] == [d["draft_id"]]


async def test_approval_flow_across_turns_with_threading(ingested, outbox):
    reply_to = await _email_id("Re: Q4 planning deck")
    client = ScriptedClient([
        Resp([call("c1", "draft_email", body="Sending Friday.", reply_to_email_id=reply_to)]),
        Resp([call("c2", "send_email", draft_id=1)]),
        Resp([Item("message", text="Draft ready; approve it to send.")]),
        Resp([Item("message", text="Done, it was sent.")]),
    ])
    agent = Agent(ingested, client=client)
    events = [e async for e in agent.run("reply to priya that I'll send it friday")]
    approvals = [e for e in events if isinstance(e, ConfirmationRequired)]
    assert len(approvals) == 1 and approvals[0].draft["to"] == ["priya@acme.com"]
    assert outbox == []  # the turn ends without sending
    send_obs = next(e for e in events if isinstance(e, Observation) and e.name == "send_email")
    model_view = client.calls[2]["input"][-1]["output"]
    assert "requires_user_approval" not in model_view and send_obs.result["awaiting_user_approval"]

    await approve_draft(ingested, approvals[0].draft["draft_id"])  # e.g. tapped on another device
    assert outbox[0]["In-Reply-To"] == "<r2@acme.com>"

    [e async for e in agent.run("did it go out?")]
    notes = [i["content"][0]["text"] for i in client.calls[3]["input"] if i.get("role") == "developer"]
    assert any("approved draft 1; it was sent" in n for n in notes)


async def test_conversation_resumes_with_identical_prefix(ingested):
    first = ScriptedClient([Resp([call("c1", "filter_emails")]), Resp([Item("message", text="One.")])])
    a = Agent(ingested, client=first)
    [e async for e in a.run("first question")]
    final_input = first.calls[-1]["input"]

    second = ScriptedClient([Resp([Item("message", text="Two.")])])
    b = Agent(ingested, conversation_id=a.conversation_id, client=second)
    events = [e async for e in b.run("follow-up")]
    assert isinstance(events[-1], FinalAnswer)
    resumed = second.calls[0]["input"]
    assert resumed[: len(final_input)] == final_input  # same prefix => prompt cache still applies
    assert resumed[len(final_input)]["role"] == "assistant"  # turn 1's answer, then the new turn
    assert [c["title"] for c in await conversations.recent(ingested.id)] == ["first question"]


async def test_failed_step_is_not_persisted(ingested):
    class Boom(ScriptedClient):
        async def create(self, **kwargs):
            if len(self.calls) == 1:
                self.calls.append(kwargs)
                raise ValueError("model exploded")
            return await super().create(**kwargs)

    client = Boom([Resp([call("c1", "filter_emails")])])
    agent = Agent(ingested, client=client)
    with pytest.raises(ValueError):
        [e async for e in agent.run("q")]
    stored = await conversations.items(agent.conversation_id)
    calls = {i["call_id"] for i in stored if i.get("type") == "function_call"}
    outputs = {i["call_id"] for i in stored if i.get("type") == "function_call_output"}
    assert calls == outputs  # never a tool call without its output
    assert agent.history == stored
