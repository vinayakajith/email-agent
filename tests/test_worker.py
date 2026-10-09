import asyncio
import threading
from dataclasses import replace

import pytest

from email_agent import jobs
from email_agent.db.pool import connection
from email_agent.worker import main as worker_main
from email_agent.worker import runner, watcher
from tests.test_fetch import MAILBOX
from tests.test_sync import NEW_MAIL


class ScriptedIMAP:
    """Stands in for GmailIMAP inside the IDLE loop."""

    def __init__(self, script, stop):
        self.script, self.stop = list(script), stop

    def __call__(self, *args):
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        self.changes = list(step)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def wait_for_changes(self, timeout):
        if not self.changes:
            self.stop.set()
            return False
        return self.changes.pop(0)


def test_idle_loop_notifies_on_connect_and_new_mail_and_reconnects(account, monkeypatch):
    monkeypatch.setattr(watcher, "BACKOFF_START", 0.01)
    monkeypatch.setattr("email_agent.accounts.get_password", lambda email: "secret")
    stop = threading.Event()
    calls = []
    imap = ScriptedIMAP([ConnectionResetError("dropped"), [False, True, False, True]], stop)
    watcher.idle_loop(account, lambda: calls.append(1), stop, imap_factory=imap)
    assert len(calls) == 3  # once after (re)connecting, then once per EXISTS


class FakeWatcher:
    instances = []

    def __init__(self, account, on_new_mail, **kw):
        self.account, self.on_new_mail, self.stopped = account, on_new_mail, False
        FakeWatcher.instances.append(self)

    def start(self):
        # A burst of new-mail notifications from the IDLE thread.
        for _ in range(3):
            threading.Thread(target=self.on_new_mail).start()

    def stop(self):
        self.stopped = True


async def _wait_for(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.1)


async def test_worker_picks_up_new_mail_end_to_end(ingested, monkeypatch):
    FakeWatcher.instances.clear()
    new_uid = max(MAILBOX) + 1
    monkeypatch.setitem(MAILBOX, new_uid, replace(MAILBOX[1], uid=new_uid, gm_msgid=7777, flags=[], raw=NEW_MAIL))
    monkeypatch.setattr(worker_main, "IDLE_POLL_SECONDS", 0.1)

    w = worker_main.Worker(watcher_factory=FakeWatcher)
    task = asyncio.create_task(w.run())

    async def embedded():
        async with connection() as conn:
            row = await (await conn.execute("SELECT stage FROM emails WHERE gm_msgid = 7777")).fetchone()
        return row is not None and row["stage"] == "embedded"

    await _wait_for(embedded)
    assert await jobs.live_workers() == 1
    async with connection() as conn:
        syncs = await (await conn.execute("SELECT status FROM ingest_jobs WHERE mode = 'sync'")).fetchall()
    assert [r["status"] for r in syncs] == ["done"]  # the burst collapsed into one job

    w.stopping.set()
    await task
    assert FakeWatcher.instances[0].stopped
    assert await jobs.live_workers() == 0


async def test_worker_shutdown_returns_running_job_to_queue(account, monkeypatch):
    started = asyncio.Event()

    async def slow(*a, **k):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(runner.pipeline, "process_stages", slow)
    monkeypatch.setattr(worker_main, "IDLE_POLL_SECONDS", 0.1)
    job_id = await jobs.enqueue(account.id, "process")
    w = worker_main.Worker(watcher_factory=FakeWatcher)
    task = asyncio.create_task(w.run())
    await asyncio.wait_for(started.wait(), 10)
    w.stopping.set()
    await task
    job = await jobs.get(job_id)
    assert job["status"] == "queued" and job["attempts"] == 0
