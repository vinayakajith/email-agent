"""Postgres job queue for ingestion work.

Clients (CLI today; API and mobile later) only enqueue jobs and read their progress. Workers
claim jobs with FOR UPDATE SKIP LOCKED. Partial unique indexes guarantee one queued job per
(account, mode) and one running job per account, so the guarantees hold across any number of
worker processes and survive crashes. Every pipeline stage is idempotent, which makes
re-running an interrupted job safe.
"""

import logging
import os
import socket
from datetime import date

from psycopg.errors import UniqueViolation

from email_agent.db.pool import connection

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
STALE_AFTER = "5 minutes"
WORKER_ALIVE_WITHIN = "30 seconds"
MODES = ("full", "since", "sync", "process", "reenrich")


class PermanentJobError(Exception):
    """A failure that retrying cannot fix (e.g. nothing ingested yet, bad credentials)."""


async def enqueue(account_id: int, mode: str, since: date | None = None) -> int:
    """Queue a job, or return the already-queued one for this account and mode."""
    if mode not in MODES:
        raise ValueError(f"unknown job mode {mode!r}")
    async with connection() as conn:
        row = await (await conn.execute(
            """INSERT INTO ingest_jobs (account_id, mode, since_date) VALUES (%s, %s, %s)
               ON CONFLICT (account_id, mode) WHERE status = 'queued'
               -- Two queued 'since' requests merge into the wider one.
               DO UPDATE SET since_date = LEAST(ingest_jobs.since_date, EXCLUDED.since_date)
               RETURNING id""",
            (account_id, mode, since),
        )).fetchone()
    return row["id"]


async def claim(worker_id: str, job_id: int | None = None) -> dict | None:
    """Take the next runnable job (or a specific one). None if nothing can run right now."""
    only = "AND j.id = %(job)s" if job_id is not None else ""
    try:
        async with connection() as conn:
            return await (await conn.execute(
                f"""UPDATE ingest_jobs SET status = 'running', started_at = now(), heartbeat_at = now(),
                           attempts = attempts + 1, worker_id = %(worker)s, error = NULL
                    WHERE id = (
                        SELECT j.id FROM ingest_jobs j
                        WHERE j.status = 'queued' AND j.run_after <= now() {only}
                          AND NOT EXISTS (SELECT 1 FROM ingest_jobs r
                                          WHERE r.account_id = j.account_id AND r.status = 'running')
                        ORDER BY j.run_after, j.id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1)
                    RETURNING *""",
                {"worker": worker_id, "job": job_id},
            )).fetchone()
    except UniqueViolation:
        # Another worker started a job for this account at the same moment.
        return None


async def get(job_id: int) -> dict | None:
    async with connection() as conn:
        return await (await conn.execute("SELECT * FROM ingest_jobs WHERE id = %s", (job_id,))).fetchone()


async def heartbeat(job_id: int) -> None:
    async with connection() as conn:
        await conn.execute("UPDATE ingest_jobs SET heartbeat_at = now() WHERE id = %s", (job_id,))


async def progress(job_id: int, stage: str, done: int, total: int) -> None:
    async with connection() as conn:
        await conn.execute(
            """UPDATE ingest_jobs SET stage = %s, stage_done = %s, stage_total = %s, heartbeat_at = now()
               WHERE id = %s""",
            (stage, done, total, job_id),
        )


async def finish(job_id: int) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE ingest_jobs SET status = 'done', finished_at = now(), heartbeat_at = now() WHERE id = %s",
            (job_id,),
        )


async def _back_to_queue(job_id: int, delay_seconds: float, error: str | None, refund_attempt: bool) -> str:
    """Requeue a running job, unless an identical job is already queued (then it is redundant)."""
    async with connection() as conn:
        try:
            row = await (await conn.execute(
                """UPDATE ingest_jobs j SET
                       status = 'queued', run_after = now() + make_interval(secs => %(delay)s),
                       error = %(error)s, worker_id = NULL, heartbeat_at = NULL,
                       attempts = attempts - %(refund)s
                   WHERE id = %(id)s AND NOT EXISTS (
                       SELECT 1 FROM ingest_jobs q WHERE q.account_id = j.account_id AND q.mode = j.mode
                         AND q.status = 'queued')
                   RETURNING id""",
                {"id": job_id, "delay": delay_seconds, "error": error, "refund": int(refund_attempt)},
            )).fetchone()
        except UniqueViolation:
            row = None
        if row:
            return "queued"
        await conn.execute(
            """UPDATE ingest_jobs SET status = 'failed', finished_at = now(),
                      error = COALESCE(%s, 'superseded by an identical queued job') WHERE id = %s""",
            (error, job_id),
        )
        return "failed"


async def fail(job_id: int, error: str, permanent: bool = False) -> str:
    """Record a failure; transient ones are retried with exponential backoff. Returns the new status."""
    job = await get(job_id)
    if permanent or job is None or job["attempts"] >= MAX_ATTEMPTS:
        async with connection() as conn:
            await conn.execute(
                "UPDATE ingest_jobs SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
                (error, job_id),
            )
        return "failed"
    delay = 60 * 2 ** (job["attempts"] - 1)  # 1, 2, 4, 8 minutes
    return await _back_to_queue(job_id, delay, error, refund_attempt=False)


async def release(job_id: int) -> str:
    """Hand a running job back untouched (graceful shutdown or a cancelled CLI run)."""
    return await _back_to_queue(job_id, 0, None, refund_attempt=True)


async def requeue_stale() -> int:
    """Running jobs whose worker stopped heartbeating are given back to the queue."""
    async with connection() as conn:
        stale = await (await conn.execute(
            f"""SELECT id FROM ingest_jobs WHERE status = 'running'
                AND heartbeat_at < now() - interval '{STALE_AFTER}'"""
        )).fetchall()
    for r in stale:
        log.warning("job %s lost its worker; requeueing", r["id"])
        await _back_to_queue(r["id"], 0, "worker stopped responding", refund_attempt=False)
    return len(stale)


def new_worker_id(kind: str = "worker") -> str:
    return f"{kind}-{socket.gethostname()}-{os.getpid()}"


async def register_worker(worker_id: str) -> None:
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO workers (id, hostname, pid) VALUES (%s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET heartbeat_at = now()""",
            (worker_id, socket.gethostname(), os.getpid()),
        )


async def worker_heartbeat(worker_id: str) -> None:
    async with connection() as conn:
        await conn.execute("UPDATE workers SET heartbeat_at = now() WHERE id = %s", (worker_id,))


async def unregister_worker(worker_id: str) -> None:
    async with connection() as conn:
        await conn.execute("DELETE FROM workers WHERE id = %s", (worker_id,))


async def live_workers() -> int:
    async with connection() as conn:
        row = await (await conn.execute(
            f"SELECT count(*) AS n FROM workers WHERE heartbeat_at > now() - interval '{WORKER_ALIVE_WITHIN}'"
        )).fetchone()
    return row["n"]


async def open_jobs(account_id: int) -> list[dict]:
    async with connection() as conn:
        return await (await conn.execute(
            """SELECT * FROM ingest_jobs WHERE account_id = %s AND status IN ('queued', 'running')
               ORDER BY status DESC, id""",
            (account_id,),
        )).fetchall()
