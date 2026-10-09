"""Executes claimed ingestion jobs. The worker and the CLI's no-worker fallback both use this."""

import asyncio
import contextlib
import logging

from email_agent import jobs
from email_agent.accounts import AccountError, get_account_by_id
from email_agent.ingest import pipeline

log = logging.getLogger(__name__)
HEARTBEAT_SECONDS = 30


async def _dispatch(job: dict) -> dict | None:
    account = await get_account_by_id(job["account_id"])
    job_id = job["id"]

    async def progress(stage: str, done: int, total: int) -> None:
        await jobs.progress(job_id, stage, done, total)

    mode = job["mode"]
    if mode in ("full", "since"):
        await pipeline.run_ingest(account, job["since_date"] if mode == "since" else None, job_id, progress)
    elif mode == "sync":
        await pipeline.run_sync(account, job_id, progress)
    elif mode == "process":
        await pipeline.process_stages(account, job_id, progress)
    elif mode == "reenrich":
        await pipeline.mark_stale_facts(account)
        await pipeline.process_stages(account, job_id, progress)
    elif mode == "dream":
        from email_agent import dream

        return await dream.run_dream(account, job_id)
    else:
        raise jobs.PermanentJobError(f"unknown job mode {mode!r}")
    return None


async def _keep_alive(job_id: int) -> None:
    # Long model calls can go minutes without a progress update; the heartbeat proves liveness.
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        await jobs.heartbeat(job_id)


async def run_job(job: dict) -> str:
    """Run one claimed job to completion. Returns its final status ('done', 'queued' or 'failed')."""
    job_id = job["id"]
    log.info("job %s: %s for account %s started (attempt %s)", job_id, job["mode"], job["account_id"], job["attempts"])
    beat = asyncio.create_task(_keep_alive(job_id))
    try:
        result = await _dispatch(job)
    except asyncio.CancelledError:
        status = await asyncio.shield(jobs.release(job_id))
        log.info("job %s: cancelled, now %s", job_id, status)
        raise
    except (jobs.PermanentJobError, AccountError) as e:
        log.error("job %s: failed permanently: %s", job_id, e)
        return await jobs.fail(job_id, str(e), permanent=True)
    except Exception as e:
        status = await jobs.fail(job_id, f"{type(e).__name__}: {e}")
        log.warning("job %s: %s: %s (now %s)", job_id, type(e).__name__, e, status)
        return status
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
    await jobs.finish(job_id, result)
    log.info("job %s: done", job_id)
    return "done"


async def run_now(job_id: int, worker_id: str) -> str | None:
    """Claim a specific job and run it in this process. None if it cannot be claimed right now."""
    job = await jobs.claim(worker_id, job_id=job_id)
    if job is None:
        return None
    return await run_job(job)
