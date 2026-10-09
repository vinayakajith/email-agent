import json

import pytest

from email_agent import memory
from email_agent.agent.loop import Agent
from email_agent.agent.tools import ToolContext, call_tool
from email_agent.db.pool import connection
from tests.test_agent_loop import Item, Resp, ScriptedClient, call


async def test_save_replace_forget_with_versions(account):
    assert await memory.save(account.id, "Preferences/Answers", "Short answers") == "create"
    assert await memory.save(account.id, "preferences/answers", "Short answers") == "unchanged"
    assert await memory.save(account.id, "preferences/answers", "Very short answers") == "update"
    assert await memory.forget(account.id, "preferences/answers")
    assert not await memory.forget(account.id, "preferences/answers")
    ops = [(v["op"], v["old_content"], v["new_content"]) for v in reversed(await memory.history(account.id))]
    assert ops == [
        ("create", None, "Short answers"),
        ("update", "Short answers", "Very short answers"),
        ("delete", "Very short answers", None),
    ]


async def test_limits_and_secret_guard(account, monkeypatch):
    with pytest.raises(memory.MemoryError):
        await memory.save(account.id, "x", "a" * (memory.MAX_CONTENT + 1))
    with pytest.raises(memory.MemoryError):
        await memory.save(account.id, "bad topic!", "ok")
    for secret in ("card 4111 1111 1111 1111", "my password: hunter2", "OTP: 123456"):
        with pytest.raises(memory.MemoryError):
            await memory.save(account.id, "s", secret)
    monkeypatch.setattr(memory, "MAX_MEMORIES", 2)
    await memory.save(account.id, "a", "1")
    await memory.save(account.id, "b", "2")
    with pytest.raises(memory.MemoryError, match="full"):
        await memory.save(account.id, "c", "3")
    assert await memory.save(account.id, "a", "1b") == "update"  # replacing is still allowed


async def test_dream_writes_never_touch_user_notes(account):
    await memory.save(account.id, "people/priya", "My cofounder", source="user")
    async with connection() as conn:
        async with conn.transaction():
            assert await memory.save_in(conn, account.id, "people/priya", "A colleague", "dream") == "skipped"
            assert not await memory.delete_in(conn, account.id, "people/priya", "dream", only_source="dream")
    assert (await memory.list_all(account.id))[0]["content"] == "My cofounder"


async def test_rollback_restores_previous_state(account):
    await memory.save(account.id, "a", "old", source="dream")
    await memory.save(account.id, "b", "keep", source="dream")
    before = {n["topic"]: n["content"] for n in await memory.list_all(account.id)}
    async with connection() as conn:
        job = (await (await conn.execute(
            "INSERT INTO ingest_jobs (account_id, mode) VALUES (%s, 'dream') RETURNING id", (account.id,))).fetchone())["id"]
        async with conn.transaction():
            await memory.save_in(conn, account.id, "a", "new", "dream", job_id=job)
            await memory.delete_in(conn, account.id, "b", "dream", only_source="dream", job_id=job)
            await memory.save_in(conn, account.id, "c", "created", "dream", job_id=job)
    assert await memory.rollback_job(account.id, job) == 3
    assert {n["topic"]: n["content"] for n in await memory.list_all(account.id)} == before


async def test_tools_write_only_user_notes(ingested):
    ctx = ToolContext(ingested)
    res = await call_tool(ctx, "save_memory", json.dumps({"topic": "preferences/answers", "content": "Keep it short"}))
    assert res["saved"] and res["change"] == "create"
    async with connection() as conn:
        await conn.execute(
            "INSERT INTO memories (account_id, topic, content, source) VALUES (%s, 'people/priya', 'Colleague at Acme', 'dream')",
            (ingested.id,),
        )
    context = await call_tool(ctx, "get_user_context", "{}")
    assert [n["topic"] for n in context["saved_by_user"]] == ["preferences/answers"]
    assert [n["topic"] for n in context["insights_from_past_conversations"]] == ["people/priya"]
    sources = {n["topic"]: n["source"] for n in await memory.list_all(ingested.id)}
    assert sources["preferences/answers"] == "user"  # chat writes are always the user's
    assert (await call_tool(ctx, "delete_memory", json.dumps({"topic": "people/priya"})))["deleted"]
    assert "error" in await call_tool(ctx, "delete_memory", json.dumps({"topic": "people/priya"}))


async def test_prompt_rule_and_turn_memory_line(ingested):
    await memory.save(ingested.id, "preferences/answers", "Keep it short")
    client = ScriptedClient([Resp([call("c1", "get_user_context")]), Resp([Item("message", text="ok")])])
    [e async for e in Agent(ingested, client=client).run("hi")]
    system = client.calls[0]["input"][0]["content"][0]["text"]
    assert "ONLY when the user explicitly asks" in system
    context = client.calls[0]["input"][1]["content"][0]["text"]
    assert "Memory: 1 saved note(s) and 0 insight(s)" in context
    assert "Keep it short" in client.calls[1]["input"][-1]["output"]
