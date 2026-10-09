"""Ingestion orchestration: fetch -> enrich -> embed.

Every stage is idempotent and works off `emails.stage`, so an interrupted run resumes by
simply running again. Fetch skips messages already stored (matched on X-GM-MSGID), which
also makes widening the time frame later only download what is missing.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from psycopg.types.json import Jsonb

from email_agent.accounts import Account
from email_agent.db.pool import connection
from email_agent.ingest.imap_client import GmailIMAP, RawMessage
from email_agent.ingest.parser import parse_email

FETCH_BATCH = 100
Progress = Callable[[str, int, int], None]  # (stage, done, total)


def _noop(stage: str, done: int, total: int) -> None:
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


async def fetch_stage(account: Account, plan: FetchPlan, job_id: int, progress: Progress = _noop) -> int:
    total, done = len(plan.to_fetch), 0
    progress("fetch", 0, total)
    if not total:
        return 0
    imap = GmailIMAP(account.imap_host, account.email, account.password)
    await asyncio.to_thread(imap.__enter__)
    try:
        for i in range(0, total, FETCH_BATCH):
            batch = plan.to_fetch[i : i + FETCH_BATCH]
            msgs = await asyncio.to_thread(imap.fetch, batch)
            rows = []
            for m in msgs:
                try:
                    rows.append(_row(account.id, m))
                except Exception as e:  # one malformed message must not stop the mailbox
                    print(f"skip uid {m.uid}: {type(e).__name__}: {e}")
            async with connection() as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.executemany(UPSERT_EMAIL, rows)
                    await conn.execute(
                        "UPDATE ingest_jobs SET fetched = fetched + %s WHERE id = %s", (len(rows), job_id)
                    )
            done += len(batch)
            progress("fetch", done, total)
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


async def start_job(account: Account, mode: str, since: date | None) -> int:
    async with connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO ingest_jobs (account_id, mode, since_date) VALUES (%s, %s, %s) RETURNING id",
                (account.id, mode, since),
            )
        ).fetchone()
    return row["id"]


async def finish_job(job_id: int, error: str | None = None) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE ingest_jobs SET status = %s, error = %s, finished_at = now() WHERE id = %s",
            ("failed" if error else "done", error, job_id),
        )


async def process_stages(account: Account, job_id: int, progress: Progress = _noop) -> None:
    """Run the post-fetch stages over everything not yet processed (also used for resume)."""
    from email_agent.ingest.embed import embed_stage
    from email_agent.ingest.enrich import enrich_stage

    await enrich_stage(account, job_id, progress)
    await embed_stage(account, job_id, progress)


async def run_ingest(account: Account, plan: FetchPlan, since: date | None, progress: Progress = _noop) -> int:
    job_id = await start_job(account, "since" if since else "full", since)
    try:
        await fetch_stage(account, plan, job_id, progress)
        await save_sync_state(account, plan.uidvalidity, since)
        await process_stages(account, job_id, progress)
    except BaseException as e:
        await finish_job(job_id, f"{type(e).__name__}: {e}")
        raise
    await finish_job(job_id)
    return job_id


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
