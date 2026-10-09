import json

import httpx
import pytest

from email_agent import conversations, jobs
from email_agent.api import routes, views
from email_agent.api.app import create_app
from email_agent.agent.loop import Agent
from tests.test_agent_loop import Item, Resp, ScriptedClient, Summary, call

HEADERS = {"X-Email-Agent": "1"}


def client(**kw):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://127.0.0.1:8765", **kw)


def parse_sse(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


@pytest.fixture
def scripted(monkeypatch):
    holder = {}

    def use(*responses):
        holder["client"] = ScriptedClient(list(responses))
        monkeypatch.setattr(routes, "make_agent", lambda account, cid: Agent(account, cid, client=holder["client"]))
        return holder["client"]

    return use


async def test_guard_blocks_foreign_hosts_and_headerless_writes(db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://evil.example") as c:
        assert (await c.get("/api/status")).status_code == 403
    async with client() as c:
        assert (await c.post("/api/onboarding/ingest", json={"range": "30d"})).status_code == 403
        assert (await c.get("/api/status")).status_code == 200


async def test_session_bootstrap(ingested):
    async with client() as c:
        body = (await c.get("/api/session")).json()
    assert body["onboarding"]["account"] == "me@gmail.com"
    assert body["onboarding"]["next"] == "profile"
    assert len(body["suggestions"]) == 3 and "What needs my attention this week?" in body["suggestions"]
    assert body["status"]["status"] in {"ok", "attention"}


async def test_onboarding_profile_and_ingest(ingested):
    async with client(headers=HEADERS) as c:
        bad = await c.post("/api/onboarding/profile", json={"name": "V", "timezone": "Mars/Base"})
        assert bad.status_code == 400 and "timezone" in bad.json()["detail"]
        assert (await c.post("/api/onboarding/profile", json={"name": "Vinayak", "timezone": "Asia/Kolkata"})).status_code == 200
        job_id = (await c.post("/api/onboarding/ingest", json={"range": "90d"})).json()["job_id"]
        job = (await c.get(f"/api/jobs/{job_id}")).json()
    assert job["mode"] == "since" and job["status"] == "queued"
    assert (await jobs.get(job_id))["since_date"] is not None


async def test_chat_streams_events_in_order(ingested, scripted):
    scripted(
        Resp([Item("reasoning", summary=[Summary("Check the bills.")]),
              call("c1", "aggregate_spend", kind="credit_card_bill", date_from="2026-10-01", date_to="2026-10-31")]),
        Resp([Item("message", text="Your card bills total INR 12,450 [#4].")]),
    )
    async with client(headers=HEADERS) as c:
        resp = await c.post("/api/chat", json={"message": "card bills this month?"})
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    names = [e for e, _ in events]
    assert names[0] == "conversation"
    assert names.index("thought_delta") < names.index("activity") < names.index("step_done") < names.index("text_delta")
    labels = [d["label"] for e, d in events if e == "activity"]
    assert labels[-1] == "Adding up your bills"
    final = events[-1]
    assert final[0] == "final" and final[1]["text"].startswith("Your card bills total")
    assert [s["id"] for s in final[1]["sources"]] == [4]
    assert "".join(d["text"] for e, d in events if e == "text_delta") == final[1]["text"]


async def test_conversation_render_and_resume(ingested, scripted):
    scripted(
        Resp([Item("message", text="Let me look."), call("c1", "filter_emails", needs_action=True)]),
        Resp([Item("message", text="Two things need you [#4].")]),
        Resp([Item("message", text="Nothing else.")]),
    )
    async with client(headers=HEADERS) as c:
        first = parse_sse((await c.post("/api/chat", json={"message": "what needs attention?"})).text)
        cid = first[0][1]["id"]
        assert any(e == "preamble" for e, _ in first)
        await c.post("/api/chat", json={"message": "anything else?", "conversation_id": cid})
        convo = (await c.get(f"/api/conversations/{cid}")).json()
        listed = (await c.get("/api/conversations")).json()
    roles = [(m["role"], m.get("text")) for m in convo["messages"]]
    assert roles == [
        ("user", "what needs attention?"), ("assistant", "Two things need you [#4]."),
        ("user", "anything else?"), ("assistant", "Nothing else."),
    ]  # the preamble "Let me look." is not shown as an answer
    assert convo["messages"][1]["steps"] == ["Went through your inbox"]
    assert convo["messages"][1]["sources"][0]["id"] == 4
    assert listed[0]["id"] == cid


async def test_approval_flow_over_http(ingested, scripted, monkeypatch):
    from email_agent.smtp import sender

    sent = []
    monkeypatch.setattr(sender, "send_message", lambda h, e, p, msg: sent.append(msg) or msg["Message-ID"])
    monkeypatch.setattr("email_agent.accounts.get_password", lambda email: "secret")
    scripted(
        Resp([call("c1", "draft_email", to=["a@x.com"], subject="Hi", body="Hello")]),
        Resp([call("c2", "send_email", draft_id=1)]),
        Resp([Item("message", text="Ready for your approval.")]),
    )
    async with client(headers=HEADERS) as c:
        events = parse_sse((await c.post("/api/chat", json={"message": "email a@x.com hi"})).text)
        approval = next(d for e, d in events if e == "approval")["draft"]
        cid = events[0][1]["id"]
        pending = (await c.get(f"/api/conversations/{cid}")).json()["messages"][-1]
        assert pending["role"] == "approval" and sent == []
        assert (await c.post(f"/api/drafts/{approval['draft_id']}/approve")).json()["sent"] is True
        assert (await c.post(f"/api/drafts/{approval['draft_id']}/approve")).status_code == 400
        after = (await c.get(f"/api/conversations/{cid}")).json()["messages"]
    assert len(sent) == 1
    assert after[-1] == {"role": "note", "text": "Sent.", "draft_id": approval["draft_id"]}


async def test_disconnect_mid_stream_leaves_valid_conversation(ingested):
    client_ = ScriptedClient([Resp([call("c1", "filter_emails")]), Resp([Item("message", text="never reached")])])
    agent = Agent(ingested, client=client_)
    gen = views.chat_events(agent, "q")
    async for event, _ in gen:
        if event == "activity":
            break  # the browser went away mid-turn
    await gen.aclose()
    stored = await conversations.items(agent.conversation_id)
    calls = {i["call_id"] for i in stored if i.get("type") == "function_call"}
    outputs = {i["call_id"] for i in stored if i.get("type") == "function_call_output"}
    assert calls == outputs


async def test_errors_end_the_stream_cleanly(ingested, scripted):
    sc = scripted()

    async def boom(**kwargs):
        raise ValueError("model exploded")

    sc.create = boom
    async with client(headers=HEADERS) as c:
        events = parse_sse((await c.post("/api/chat", json={"message": "q"})).text)
    assert events[-1][0] == "error"
