"""Ingestion orchestration: fetch -> enrich -> embed.

Every stage is idempotent and works off `emails.stage`, so an interrupted run resumes by
simply running again. Fetch skips messages already stored (matched on X-GM-MSGID), which
also makes widening the time frame later only download what is missing.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date

from psycopg.types.json import Jsonb

from email_agent.accounts import Account
from email_agent.db.pool import connection
from email_agent.ingest.imap_client import GmailIMAP, RawMessage
from email_agent.ingest.parser import parse_email

log = logging.getLogger(__name__)
FETCH_BATCH = 100
Progress = Callable[[str, int, int], Awaitable[None]]  # (stage, done, total)


async def _noop(stage: str, done: int, total: int) -> None:
    pass


@dataclass
class FetchPlan:
    uidvalidity: int
    to_fetch: list[int]
    already_stored: int


async def _stored_msgids(account_id: int) -> set[int]:
    async with connection() as conn:
        cur = await conn.execute("SELECT gm_msgid FROM emails WHERE account_id = %s", (account_id,))
        return {r["gm_msgid"] for r in await cur.fetchall()}


async def plan_fetch(account: Account, since: date | None, min_uid: int | None = None) -> FetchPlan:
    """Work out which UIDs in the time frame are not stored yet. Metadata only, no bodies."""

    def _scan() -> tuple[int, dict[int, int]]:
        with GmailIMAP(account.imap_host, account.email, account.password) as imap:
            uids = imap.search(since=since, min_uid=min_uid)
            return imap.uidvalidity, imap.gm_msgids(uids)

    uidvalidity, uid_to_msgid = await asyncio.to_thread(_scan)
    stored = await _stored_msgids(account.id)
    to_fetch = sorted(uid for uid, msgid in uid_to_msgid.items() if msgid not in stored)
    return FetchPlan(uidvalidity, to_fetch, len(uid_to_msgid) - len(to_fetch))


UPSERT_EMAIL = """
INSERT INTO emails (
    account_id, gm_msgid, gm_thrid, uid, message_id, in_reply_to, "references", subject,
    from_name, from_addr, to_addrs, cc_addrs, sent_at, labels, is_unread, is_starred,
    has_attachments, attachments, body_text, snippet, list_unsubscribe, size_bytes
) VALUES (
    %(account_id)s, %(gm_msgid)s, %(gm_thrid)s, %(uid)s, %(message_id)s, %(in_reply_to)s, %(references)s,
    %(subject)s, %(from_name)s, %(from_addr)s, %(to_addrs)s, %(cc_addrs)s, %(sent_at)s, %(labels)s,
    %(is_unread)s, %(is_starred)s, %(has_attachments)s, %(attachments)s, %(body_text)s, %(snippet)s,
    %(list_unsubscribe)s, %(size_bytes)s
)
ON CONFLICT (account_id, gm_msgid) DO UPDATE SET
    uid = EXCLUDED.uid, labels = EXCLUDED.labels,
    is_unread = EXCLUDED.is_unread, is_starred = EXCLUDED.is_starred
"""


def _row(account_id: int, m: RawMessage) -> dict:
    p = parse_email(m.raw, m.internal_date)
    return {
        "account_id": account_id,
        "gm_msgid": m.gm_msgid,
        "gm_thrid": m.gm_thrid,
        "uid": m.uid,
        "message_id": p.message_id,
        "in_reply_to": p.in_reply_to,
        "references": p.references,
        "subject": p.subject,
        "from_name": p.from_name,
        "from_addr": p.from_addr,
        "to_addrs": p.to_addrs,
        "cc_addrs": p.cc_addrs,
        "sent_at": p.sent_at,
        "labels": m.labels,
        "is_unread": "\\Seen" not in m.flags,
        "is_starred": "\\Flagged" in m.flags,
        "has_attachments": p.has_attachments,
        "attachments": Jsonb(p.attachments),
        "body_text": p.body_text,
        "snippet": p.snippet,
        "list_unsubscribe": p.list_unsubscribe,
        "size_bytes": m.size,
    }


def _rows(account_id: int, msgs: list[RawMessage]) -> list[dict]:
    rows = []
    for m in msgs:
        try:
            rows.append(_row(account_id, m))
        except Exception as e:  # one malformed message must not stop the mailbox
            log.warning("skipping uid %s: %s: %s", m.uid, type(e).__name__, e)
    return rows


async def fetch_stage(account: Account, plan: FetchPlan, job_id: int, progress: Progress = _noop) -> int:
    total, done = len(plan.to_fetch), 0
    await progress("fetch", 0, total)
    if not total:
        return 0
    imap = GmailIMAP(account.imap_host, account.email, account.password)
    await asyncio.to_thread(imap.__enter__)
    try:
        for i in range(0, total, FETCH_BATCH):
            batch = plan.to_fetch[i : i + FETCH_BATCH]
            msgs = await asyncio.to_thread(imap.fetch, batch)
            # MIME/HTML parsing is CPU-bound; keep it off the event loop.
            rows = await asyncio.to_thread(_rows, account.id, msgs)
            async with connection() as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.executemany(UPSERT_EMAIL, rows)
                    await conn.execute(
                        "UPDATE ingest_jobs SET fetched = fetched + %s WHERE id = %s", (len(rows), job_id)
                    )
            done += len(batch)
            await progress("fetch", done, total)
    finally:
        await asyncio.to_thread(imap.__exit__, None, None, None)
    return done


async def save_sync_state(account: Account, uidvalidity: int, since: date | None) -> None:
    """Record the high-water UID and the earliest date covered, for incremental sync."""
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO sync_state (account_id, folder, uidvalidity, last_uid, since_date, last_synced_at)
            SELECT %(a)s, 'all_mail', %(v)s, COALESCE(MAX(uid), 0), %(since)s, now()
            FROM emails WHERE account_id = %(a)s
            ON CONFLICT (account_id, folder) DO UPDATE SET
                uidvalidity = EXCLUDED.uidvalidity,
                last_uid = EXCLUDED.last_uid,
                -- Widen only: NULL (full mailbox) wins, otherwise keep the earliest date.
                since_date = CASE
                    WHEN sync_state.since_date IS NULL OR EXCLUDED.since_date IS NULL THEN NULL
                    ELSE LEAST(sync_state.since_date, EXCLUDED.since_date) END,
                last_synced_at = now()
            """,
            {"a": account.id, "v": uidvalidity, "since": since},
        )


async def process_stages(account: Account, job_id: int, progress: Progress = _noop) -> None:
    """Run the post-fetch stages over everything not yet processed (also used for resume)."""
    from email_agent.ingest.embed import embed_stage
    from email_agent.ingest.enrich import enrich_stage

    await enrich_stage(account, job_id, progress)
    await embed_stage(account, job_id, progress)


async def mark_stale_facts(account: Account) -> int:
    """Send emails whose facts came from an older extractor back through enrich + embed.

    Chunks embed the extracted summary, so they are rebuilt too.
    """
    from email_agent.ingest.enrich import EXTRACTOR_VERSION

    async with connection() as conn:
        cur = await conn.execute(
            """UPDATE emails e SET stage = 'fetched'
               FROM email_facts f
               WHERE f.email_id = e.id AND e.account_id = %s AND f.extractor_version < %s""",
            (account.id, EXTRACTOR_VERSION),
        )
        return cur.rowcount


async def run_ingest(account: Account, since: date | None, job_id: int, progress: Progress = _noop) -> int:
    """Fetch everything in the time frame that is not stored yet, then process it.

    `since=None` means the full mailbox. Returns the number of emails fetched.
    """
    plan = await plan_fetch(account, since)
    fetched = await fetch_stage(account, plan, job_id, progress)
    await save_sync_state(account, plan.uidvalidity, since)
    await process_stages(account, job_id, progress)
    return fetched


FLAG_REFRESH_DAYS = 30


async def refresh_flags(account: Account) -> int:
    """Re-read read/starred state and labels for recent mail, which changes after ingestion."""
    from datetime import timedelta

    def _scan():
        with GmailIMAP(account.imap_host, account.email, account.password) as imap:
            uids = imap.search(since=date.today() - timedelta(days=FLAG_REFRESH_DAYS))
            return imap.uidvalidity, imap.flags_and_labels(uids)

    uidvalidity, state = await asyncio.to_thread(_scan)
    rows = [
        {"a": account.id, "uid": uid, "labels": labels, "unread": "\\Seen" not in flags, "starred": "\\Flagged" in flags}
        for uid, (flags, labels) in state.items()
    ]
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                """UPDATE emails SET labels = %(labels)s, is_unread = %(unread)s, is_starred = %(starred)s
                   WHERE account_id = %(a)s AND uid = %(uid)s""",
                rows,
            )
    return len(rows)


async def get_sync_state(account: Account) -> dict | None:
    async with connection() as conn:
        return await (
            await conn.execute("SELECT * FROM sync_state WHERE account_id = %s AND folder = 'all_mail'", (account.id,))
        ).fetchone()


async def run_sync(account: Account, job_id: int, progress: Progress = _noop) -> tuple[int, int]:
    """Fetch mail newer than the last sync, process it, and refresh recent flags.

    Returns (new emails fetched, flags refreshed).
    """
    from email_agent.jobs import PermanentJobError

    state = await get_sync_state(account)
    if state is None:
        raise PermanentJobError("Nothing ingested yet; run `email-agent ingest` first.")
    plan = await plan_fetch(account, None, min_uid=state["last_uid"] + 1)
    if plan.uidvalidity != state["uidvalidity"]:
        # UIDs were renumbered by the server: rescan the covered range; gm_msgid dedupes.
        plan = await plan_fetch(account, state["since_date"])
    fetched = await fetch_stage(account, plan, job_id, progress)
    await save_sync_state(account, plan.uidvalidity, state["since_date"])
    await process_stages(account, job_id, progress)
    refreshed = await refresh_flags(account)
    return fetched, refreshed


async def stage_counts(account_id: int) -> dict:
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT stage, count(*) AS n FROM emails WHERE account_id = %s GROUP BY stage", (account_id,)
        )
        counts = {r["stage"]: r["n"] for r in await cur.fetchall()}
        chunks = await (
            await conn.execute(
                "SELECT count(*) AS n FROM chunks c JOIN emails e ON e.id = c.email_id WHERE e.account_id = %s",
                (account_id,),
            )
        ).fetchone()
        sync = await (
            await conn.execute("SELECT * FROM sync_state WHERE account_id = %s", (account_id,))
        ).fetchone()
        job = await (
            await conn.execute(
                "SELECT * FROM ingest_jobs WHERE account_id = %s ORDER BY id DESC LIMIT 1", (account_id,)
            )
        ).fetchone()
    return {"stages": counts, "chunks": chunks["n"], "sync": sync, "last_job": job}
