"""The background worker: watches mailboxes and runs queued ingestion jobs.

One process runs:
- a Watcher (IMAP IDLE thread) per ingested account, which enqueues `sync` on new mail;
- a poll timer that enqueues `sync` for every account as a fallback (and flag refresh);
- WORKER_CONCURRENCY runner tasks that claim and execute jobs;
- a heartbeat and a reaper that requeues jobs whose worker died.

It shares nothing with chat but the database, so chat stays responsive whatever runs here.
"""

import asyncio
import contextlib
import logging
import signal

from email_agent import jobs
from email_agent.accounts import Account
from email_agent.config import get_settings
from email_agent.db.pool import connection
from email_agent.worker.runner import run_job
from email_agent.worker.watcher import Watcher

log = logging.getLogger(__name__)

IDLE_POLL_SECONDS = 2  # how often an idle runner checks the queue
HEARTBEAT_SECONDS = 10
ACCOUNT_RESCAN_SECONDS = 60
REAP_SECONDS = 60
DREAM_CHECK_SECONDS = 300


async def watchable_accounts() -> list[Account]:
    """Accounts with a completed initial ingest; a sync is meaningless before that."""
    async with connection() as conn:
        rows = await (await conn.execute(
            """SELECT a.id, a.email, a.imap_host, a.smtp_host, a.auth_method
               FROM accounts a JOIN sync_state s ON s.account_id = a.id ORDER BY a.id"""
        )).fetchall()
    return [Account(**r) for r in rows]


class Worker:
    def __init__(self, watcher_factory=Watcher):
        s = get_settings()
        self.id = jobs.new_worker_id()
        self.concurrency = max(1, s.worker_concurrency)
        self.poll_seconds = max(1, s.worker_poll_minutes) * 60
        self.watcher_factory = watcher_factory
        self.watchers: dict[int, Watcher] = {}
        self.stopping = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None

    def _enqueue_sync_threadsafe(self, account_id: int):
        """Called from IDLE threads; hands the enqueue to the event loop."""

        def notify() -> None:
            fut = asyncio.run_coroutine_threadsafe(jobs.enqueue(account_id, "sync"), self.loop)
            fut.add_done_callback(
                lambda f: f.exception() and log.warning("could not enqueue sync: %s", f.exception())
            )

        return notify

    async def _sync_watchers(self) -> None:
        accounts = {a.id: a for a in await watchable_accounts()}
        for account_id in set(self.watchers) - set(accounts):
            self.watchers.pop(account_id).stop()
        for account_id, account in accounts.items():
            if account_id not in self.watchers:
                w = self.watcher_factory(account, self._enqueue_sync_threadsafe(account_id))
                w.start()
                self.watchers[account_id] = w

    async def _every(self, seconds: float, fn) -> None:
        while not self.stopping.is_set():
            try:
                await fn()
            except Exception:
                log.exception("periodic task %s failed", getattr(fn, "__name__", fn))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), seconds)

    async def _schedule_dreams(self) -> None:
        """Queue tonight's dream for accounts with new messages (see dream.is_due)."""
        from email_agent import dream, profile

        s = get_settings()
        if not s.dream_enabled:
            return
        async with connection() as conn:
            account_ids = [r["id"] for r in await (await conn.execute("SELECT id FROM accounts")).fetchall()]
        for account_id in account_ids:
            user = await profile.get(account_id)
            if await dream.is_due(account_id, user.timezone if user else s.tz, s.dream_hour):
                log.info("queueing dream for account %s", account_id)
                await jobs.enqueue(account_id, "dream")

    async def _reap(self) -> None:
        await jobs.requeue_stale()
        await jobs.prune_workers()

    async def _poll_all(self) -> None:
        for account_id in list(self.watchers):
            await jobs.enqueue(account_id, "sync")

    async def _runner(self, n: int) -> None:
        while not self.stopping.is_set():
            try:
                job = await jobs.claim(self.id)
                if job is not None:
                    await run_job(job)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:  # e.g. the database restarting; keep the runner alive
                log.exception("runner %s: error while claiming or running a job", n)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), IDLE_POLL_SECONDS)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                self.loop.add_signal_handler(sig, self.stopping.set)
        await jobs.register_worker(self.id)
        log.info("worker %s started (concurrency %s, poll every %s min)", self.id, self.concurrency, self.poll_seconds // 60)
        await jobs.requeue_stale()
        background = [
            asyncio.create_task(self._every(HEARTBEAT_SECONDS, lambda: jobs.worker_heartbeat(self.id))),
            asyncio.create_task(self._every(ACCOUNT_RESCAN_SECONDS, self._sync_watchers)),
            asyncio.create_task(self._every(REAP_SECONDS, self._reap)),
            asyncio.create_task(self._every(DREAM_CHECK_SECONDS, self._schedule_dreams)),
            asyncio.create_task(self._delayed_poll()),
        ]
        runners = [asyncio.create_task(self._runner(i)) for i in range(self.concurrency)]
        try:
            await self.stopping.wait()
        finally:
            log.info("worker %s stopping", self.id)
            for w in self.watchers.values():
                w.stop()
            # Cancelling a runner mid-job hands its job back to the queue (see runner.run_job).
            for t in runners + background:
                t.cancel()
            await asyncio.gather(*runners, *background, return_exceptions=True)
            await jobs.unregister_worker(self.id)
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    self.loop.remove_signal_handler(sig)
            log.info("worker %s stopped", self.id)

    async def _delayed_poll(self) -> None:
        # Watchers already sync on connect; the fallback poll starts one interval later.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stopping.wait(), self.poll_seconds)
        await self._every(self.poll_seconds, self._poll_all)
