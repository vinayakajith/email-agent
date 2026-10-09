import asyncio
from datetime import date

import pytest

from email_agent import jobs
from email_agent.accounts import Account
from email_agent.db.pool import connection
from email_agent.worker import runner


async def _account(email: str) -> Account:
    async with connection() as conn:
        row = await (await conn.execute(
            "INSERT INTO accounts (email, imap_host, smtp_host) VALUES (%s, 'imap', 'smtp') "
            "RETURNING id, email, imap_host, smtp_host, auth_method",
            (email,),
        )).fetchone()
    return Account(**row)


async def _set(job_id: int, **cols):
    sets = ", ".join(f"{k} = %s" for k in cols)
    async with connection() as conn:
        await conn.execute(f"UPDATE ingest_jobs SET {sets} WHERE id = %s", (*cols.values(), job_id))


async def test_enqueue_collapses_duplicates(account):
    assert await jobs.enqueue(account.id, "sync") == await jobs.enqueue(account.id, "sync")
    a = await jobs.enqueue(account.id, "since", date(2026, 9, 1))
    assert await jobs.enqueue(account.id, "since", date(2026, 1, 1)) == a
    assert (await jobs.get(a))["since_date"] == date(2026, 1, 1)  # merged into the wider range
    assert await jobs.enqueue(account.id, "process") != a
    with pytest.raises(ValueError):
        await jobs.enqueue(account.id, "nope")


async def test_concurrent_claims_never_run_two_jobs_for_one_account(account):
    other = await _account("other@gmail.com")
    await jobs.enqueue(account.id, "sync")
    await jobs.enqueue(account.id, "process")
    await jobs.enqueue(other.id, "sync")
    claimed = await asyncio.gather(*(jobs.claim(f"w{i}") for i in range(4)))
    got = [c for c in claimed if c]
    assert sorted(c["account_id"] for c in got) == sorted([account.id, other.id])
    # The account's second job waits until the first finishes.
    assert await jobs.claim("w9") is None
    first = next(c for c in got if c["account_id"] == account.id)
    await jobs.finish(first["id"])
    nxt = await jobs.claim("w9")
    assert nxt["account_id"] == account.id and nxt["id"] != first["id"]


async def test_transient_failures_back_off_then_fail(account):
    job_id = await jobs.enqueue(account.id, "sync")
    await jobs.claim("w")
    assert await jobs.fail(job_id, "RateLimitError") == "queued"
    job = await jobs.get(job_id)
    assert job["attempts"] == 1 and job["error"] == "RateLimitError"
    assert await jobs.claim("w") is None  # retry is scheduled ~1 minute out
    await _set(job_id, run_after=date(2000, 1, 1), attempts=jobs.MAX_ATTEMPTS - 1)
    await jobs.claim("w")
    assert await jobs.fail(job_id, "still failing") == "failed"


async def test_permanent_failure_is_not_retried(account):
    job_id = await jobs.enqueue(account.id, "sync")
    await jobs.claim("w")
    assert await jobs.fail(job_id, "no credentials", permanent=True) == "failed"


async def test_stale_running_jobs_are_requeued_or_superseded(account):
    job_id = await jobs.enqueue(account.id, "sync")
    await jobs.claim("dead-worker")
    await _set(job_id, heartbeat_at=date(2000, 1, 1))
    assert await jobs.requeue_stale() == 1
    assert (await jobs.get(job_id))["status"] == "queued"

    await jobs.claim("dead-worker")
    await _set(job_id, heartbeat_at=date(2000, 1, 1))
    newer = await jobs.enqueue(account.id, "sync")  # an identical job is already waiting
    await jobs.requeue_stale()
    assert (await jobs.get(job_id))["status"] == "failed"
    assert (await jobs.get(newer))["status"] == "queued"


async def test_release_refunds_the_attempt(account):
    job_id = await jobs.enqueue(account.id, "process")
    await jobs.claim("w")
    assert await jobs.release(job_id) == "queued"
    assert (await jobs.get(job_id))["attempts"] == 0


async def test_runner_records_failures(account, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("voyage down")

    monkeypatch.setattr(runner.pipeline, "process_stages", boom)
    job_id = await jobs.enqueue(account.id, "process")
    assert await runner.run_now(job_id, "w") == "queued"
    assert (await jobs.get(job_id))["error"] == "RuntimeError: voyage down"

    sync_id = await jobs.enqueue(account.id, "sync")  # nothing ingested yet: permanent
    await _set(job_id, status="done")
    assert await runner.run_now(sync_id, "w") == "failed"


async def test_runner_cancellation_hands_job_back(account, monkeypatch):
    started = asyncio.Event()

    async def slow(*a, **k):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(runner.pipeline, "process_stages", slow)
    job_id = await jobs.enqueue(account.id, "process")
    task = asyncio.create_task(runner.run_now(job_id, "w"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    job = await jobs.get(job_id)
    assert job["status"] == "queued" and job["attempts"] == 0


async def test_cli_runs_inline_without_worker_and_defers_to_live_worker(ingested):
    from email_agent.cli import _submit

    await _submit(ingested, "process")  # no worker registered: runs here
    async with connection() as conn:
        done = await (await conn.execute("SELECT status FROM ingest_jobs WHERE mode = 'process'")).fetchone()
    assert done["status"] == "done"

    await jobs.register_worker("bg-worker")
    follow = asyncio.create_task(_submit(ingested, "process"))
    await asyncio.sleep(1.5)
    queued = [j for j in await jobs.open_jobs(ingested.id) if j["mode"] == "process"]
    assert queued and queued[0]["status"] == "queued"  # left for the worker, not run inline
    follow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follow
