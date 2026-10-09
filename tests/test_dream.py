from datetime import datetime, timezone

import pytest

from email_agent import dream, jobs, memory
from email_agent.agent.loop import Agent
from email_agent.db.pool import connection
from tests.conftest import run_job_now
from tests.test_agent_loop import Item, Resp, ScriptedClient, call


async def _chat(account, question, answer, with_tool=True):
    script = [Resp([call("c1", "filter_emails", needs_action=True)])] if with_tool else []
    client = ScriptedClient(script + [Resp([Item("message", text=answer)])])
    [e async for e in Agent(account, client=client).run(question)]


@pytest.fixture
def consolidation(monkeypatch):
    """Stub the single model call; records what the dream was shown."""
    seen = {}

    def set_result(*insights, summary="ok"):
        async def fake(context):
            seen["context"] = context
            return dream.DreamResult(
                insights=[dream.Insight(topic=t, content=c) for t, c in insights], changes_summary=summary
            ), {"input_tokens": 10, "output_tokens": 5}

        monkeypatch.setattr(dream, "_consolidate", fake)

    seen["set"] = set_result
    return seen


async def test_no_messages_means_no_dream(ingested, consolidation):
    assert not await dream.is_due(ingested.id, "UTC", 3, now=datetime(2026, 10, 11, 4, tzinfo=timezone.utc))
    job = await run_job_now(ingested, "dream")
    assert job["result"] == {"skipped": "no new messages since the last dream"}
    assert "context" not in consolidation  # no model call


async def test_dream_builds_insights_from_user_and_assistant_text_only(ingested, consolidation):
    await _chat(ingested, "Priya is my cofounder; summarise what she asked", "Priya asked for the final deck by Friday.")
    consolidation["set"](("people/priya", "Cofounder; collaborates on the Q4 deck."), summary="Added Priya.")
    job = await run_job_now(ingested, "dream")

    ctx = consolidation["context"]
    assert "User: Priya is my cofounder" in ctx and "Assistant: Priya asked for the final deck" in ctx
    assert "filter_emails" not in ctx and "function_call" not in ctx  # tool traffic never reaches the dream
    assert "action_summary" not in ctx  # raw tool output (email facts) excluded
    assert job["result"]["changes"]["create"] == 1 and job["result"]["summary"] == "Added Priya."
    notes = await memory.list_all(ingested.id)
    assert [(n["topic"], n["source"]) for n in notes] == [("people/priya", "dream")]
    assert {v["job_id"] for v in await memory.history(ingested.id)} == {job["id"]}


async def test_dream_never_touches_user_notes(ingested, consolidation):
    await memory.save(ingested.id, "preferences/answers", "Short bullet points", source="user")
    await _chat(ingested, "keep answers short", "Sure.", with_tool=False)
    consolidation["set"](("preferences/answers", "Likes long essays"), ("finance/cards", "ICICI and Axis cards."))
    job = await run_job_now(ingested, "dream")
    assert "Short bullet points" in consolidation["context"]  # shown as read-only context
    notes = {n["topic"]: (n["content"], n["source"]) for n in await memory.list_all(ingested.id)}
    assert notes["preferences/answers"] == ("Short bullet points", "user")
    assert notes["finance/cards"][1] == "dream"
    assert job["result"]["changes"]["skipped"] == 1


async def test_dream_rewrites_and_removes_its_own_insights_then_rolls_back(ingested, consolidation):
    for t, c in (("a", "alpha " * 20), ("b", "beta " * 20), ("c", "gamma " * 20)):
        await memory.save(ingested.id, t, c, source="dream")
    before = {n["topic"]: n["content"] for n in await memory.list_all(ingested.id)}
    await _chat(ingested, "q", "a", with_tool=False)
    consolidation["set"](("a", "alpha merged with beta " * 8), ("c", "gamma " * 20))
    job = await run_job_now(ingested, "dream")
    assert job["result"]["changes"]["delete"] == 1 and job["result"]["changes"]["update"] == 1
    assert await memory.rollback_job(ingested.id, job["id"]) == 2
    assert {n["topic"]: n["content"] for n in await memory.list_all(ingested.id)} == before


async def test_shrink_guard_blocks_mass_removal(ingested, consolidation):
    for t in ("a", "b", "c", "d"):
        await memory.save(ingested.id, t, f"{t} insight " * 20, source="dream")
    await _chat(ingested, "q", "a", with_tool=False)
    consolidation["set"](("a", "tiny"))
    job = await run_job_now(ingested, "dream")
    assert job["status"] == "failed" and "shrink" in job["error"]
    assert len(await memory.list_all(ingested.id)) == 4


async def test_scheduler_due_once_per_local_day_only_with_new_messages(ingested, consolidation):
    early = datetime(2026, 10, 11, 2, 59, tzinfo=timezone.utc)
    late = datetime(2026, 10, 11, 3, 1, tzinfo=timezone.utc)
    assert not await dream.is_due(ingested.id, "UTC", 3, now=late)  # nothing new
    await _chat(ingested, "hello", "hi", with_tool=False)
    assert not await dream.is_due(ingested.id, "UTC", 3, now=early)  # before 3am
    assert await dream.is_due(ingested.id, "UTC", 3, now=late)
    assert not await dream.is_due(ingested.id, "Asia/Kolkata", 3, now=datetime(2026, 10, 10, 21, 0, tzinfo=timezone.utc))  # 02:30 IST

    job_id = await jobs.enqueue(ingested.id, "dream")
    assert not await dream.is_due(ingested.id, "UTC", 3, now=late)  # one already queued
    consolidation["set"]()
    from email_agent.worker.runner import run_now

    await run_now(job_id, "w")
    today = datetime.now(timezone.utc)
    await _chat(ingested, "more", "ok", with_tool=False)
    assert not await dream.is_due(ingested.id, "UTC", 0, now=today)  # already dreamt today
    async with connection() as conn:
        await conn.execute("UPDATE ingest_jobs SET finished_at = finished_at - interval '1 day' WHERE id = %s", (job_id,))
    assert await dream.is_due(ingested.id, "UTC", 0, now=today)  # catch-up: new messages, no dream today


async def test_worker_enqueues_due_dreams(ingested, monkeypatch, consolidation):
    from email_agent.worker.main import Worker

    from email_agent.config import get_settings

    await _chat(ingested, "hello", "hi", with_tool=False)
    monkeypatch.setenv("DREAM_HOUR", "0")
    get_settings.cache_clear()
    try:
        await Worker()._schedule_dreams()
        await Worker()._schedule_dreams()  # idempotent while one is queued
    finally:
        get_settings.cache_clear()
    assert [j["mode"] for j in await jobs.open_jobs(ingested.id)] == ["dream"]
