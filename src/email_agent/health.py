"""Health checks: a detailed report for developers and one friendly line for users.

Everything is derived from state the app already records (migrations, worker heartbeats,
sync times, job rows, conversation items), so health adds no moving parts of its own.
`deep=True` also pings OpenAI and Voyage (the former `doctor` command).
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Literal

import psycopg
import voyageai
from openai import OpenAI

from email_agent.config import EMBED_DIM, EMBED_MODEL, RERANK_MODEL, get_settings
from email_agent.db.migrate import MIGRATIONS_DIR

Status = Literal["ok", "warn", "fail"]
STUCK_AFTER_HOURS = 1
QUEUE_WAIT_MINUTES = 15
DREAM_OVERDUE_HOURS = 26


@dataclass
class Check:
    name: str
    status: Status
    detail: str


def _ago(ts: datetime) -> str:
    minutes = int((datetime.now(timezone.utc) - ts).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


def _db_checks(conn) -> list[Check]:
    s = get_settings()
    checks = []
    exts = dict(conn.execute("SELECT extname, extversion FROM pg_extension").fetchall())
    applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    pending = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql") if p.name not in applied)
    if pending:
        checks.append(Check("database", "fail", f"{len(pending)} migration(s) pending; run `email-agent db migrate`"))
    else:
        checks.append(Check("database", "ok", f"pgvector {exts.get('vector')}, pg_search {exts.get('pg_search')}"))

    workers = conn.execute("SELECT count(*) FROM workers WHERE heartbeat_at > now() - interval '30 seconds'").fetchone()[0]
    checks.append(Check("worker", "ok", f"{workers} running") if workers
                  else Check("worker", "warn", "not running; new mail isn't picked up (`email-agent worker`)"))

    for email, synced in conn.execute(
        "SELECT a.email, s.last_synced_at FROM accounts a LEFT JOIN sync_state s ON s.account_id = a.id ORDER BY a.id"
    ).fetchall():
        if synced is None:
            checks.append(Check(f"sync {email}", "warn", "not ingested yet (`email-agent ingest`)"))
            continue
        stale = (datetime.now(timezone.utc) - synced).total_seconds() > 2 * s.worker_poll_minutes * 60
        checks.append(Check(f"sync {email}", "warn" if stale else "ok", f"last synced {_ago(synced)}"))

    stuck = conn.execute(
        f"""SELECT count(*) FROM emails WHERE stage <> 'embedded'
            AND created_at < now() - interval '{STUCK_AFTER_HOURS} hour'"""
    ).fetchone()[0]
    checks.append(Check("processing", "warn", f"{stuck} email(s) stuck before search indexing") if stuck
                  else Check("processing", "ok", "all fetched mail is searchable"))

    # Only failures not yet followed by a successful job for the same account: "failing now",
    # not something a later run already fixed.
    failed, last_error = conn.execute(
        """SELECT count(*), (array_agg(f.mode || ': ' || f.error ORDER BY f.finished_at DESC))[1]
           FROM ingest_jobs f
           WHERE f.status = 'failed' AND f.finished_at > now() - interval '24 hours'
             AND f.finished_at > COALESCE((SELECT max(d.finished_at) FROM ingest_jobs d
                                           WHERE d.account_id = f.account_id AND d.status = 'done'), '-infinity')"""
    ).fetchone()
    waiting = conn.execute(
        f"""SELECT count(*) FROM ingest_jobs WHERE status = 'queued' AND run_after < now()
            AND queued_at < now() - interval '{QUEUE_WAIT_MINUTES} minutes'"""
    ).fetchone()[0]
    if failed:
        checks.append(Check("jobs", "warn", f"{failed} failing since the last success; latest: {(last_error or '')[:120]}"))
    elif waiting and workers:
        checks.append(Check("jobs", "warn", f"{waiting} job(s) waiting over {QUEUE_WAIT_MINUTES} min"))
    else:
        checks.append(Check("jobs", "ok", "background jobs succeeding"))

    if s.dream_enabled:
        oldest_pending = conn.execute(
            """SELECT min(ci.created_at) FROM conversation_items ci
               WHERE ci.item->>'role' = 'user' AND ci.created_at > COALESCE(
                   (SELECT max(started_at) FROM ingest_jobs WHERE mode = 'dream' AND status = 'done'), '-infinity')"""
        ).fetchone()[0]
        last_dream = conn.execute(
            "SELECT max(finished_at) FROM ingest_jobs WHERE mode = 'dream' AND status = 'done'"
        ).fetchone()[0]
        overdue = oldest_pending and (datetime.now(timezone.utc) - oldest_pending).total_seconds() > DREAM_OVERDUE_HOURS * 3600
        detail = f"last dream {_ago(last_dream)}" if last_dream else "no dream yet"
        checks.append(Check("dreaming", "warn" if overdue else "ok",
                            f"{detail}; new messages waiting over {DREAM_OVERDUE_HOURS} h" if overdue else detail))

    drafts = conn.execute("SELECT count(*) FROM drafts WHERE status = 'awaiting_approval'").fetchone()[0]
    checks.append(Check("drafts", "ok", f"{drafts} awaiting your approval" if drafts else "none awaiting approval"))
    return checks


def _api_checks() -> list[Check]:
    s = get_settings()
    checks = []
    for label, model in (("openai agent model", s.openai_model), ("openai extraction model", s.openai_enrich_model)):
        try:
            OpenAI(api_key=s.openai_api_key).models.retrieve(model)
            checks.append(Check(label, "ok", model))
        except Exception as e:
            checks.append(Check(label, "fail", f"{model}: {type(e).__name__}: {e}"[:200]))
    client = voyageai.Client(api_key=s.voyage_api_key)
    try:
        dim = len(client.embed(["ping"], model=EMBED_MODEL, input_type="query", output_dimension=EMBED_DIM).embeddings[0])
        checks.append(Check("voyage embeddings", "ok", f"{EMBED_MODEL} dim={dim}"))
    except Exception as e:
        checks.append(Check("voyage embeddings", "fail", f"{type(e).__name__}: {e}"[:200]))
    try:
        client.rerank("ping", ["pong", "ping"], model=RERANK_MODEL, top_k=1)
        checks.append(Check("voyage rerank", "ok", RERANK_MODEL))
    except Exception as e:
        checks.append(Check("voyage rerank", "fail", f"{type(e).__name__}: {e}"[:200]))
    return checks


def run(deep: bool = False) -> list[Check]:
    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=5) as conn:
            checks = _db_checks(conn)
    except Exception as e:
        checks = [Check("database", "fail", f"unreachable: {type(e).__name__}: {e}"[:200])]
    return checks + _api_checks() if deep else checks


def overall(checks: list[Check]) -> Status:
    statuses = {c.status for c in checks}
    return "fail" if "fail" in statuses else "warn" if "warn" in statuses else "ok"


def report(deep: bool = False) -> dict:
    checks = run(deep)
    return {"status": overall(checks), "checks": [asdict(c) for c in checks]}


def summary(checks: list[Check] | None = None) -> dict:
    """One status and one sentence for end users."""
    checks = checks if checks is not None else run()
    by_name = {c.name: c for c in checks}
    db = by_name.get("database")
    if db and db.status == "fail":
        if db.detail.startswith("unreachable"):
            return {"status": "down", "message": "The assistant can't reach its database right now."}
        return {"status": "down", "message": "The assistant needs an update before it can continue."}
    if by_name.get("worker") and by_name["worker"].status != "ok":
        return {"status": "attention", "message": "Background sync is paused, so new mail won't appear yet."}
    problems = [c for c in checks if c.status != "ok"]
    if any(c.name == "jobs" for c in problems):
        return {"status": "attention", "message": "Background work is failing right now; it will be retried."}
    if any(c.name == "processing" for c in problems):
        return {"status": "attention", "message": "Some mail couldn't be processed yet; it will be retried."}
    if any(c.name.startswith("sync ") for c in problems):
        return {"status": "attention", "message": "Mail sync is behind; it will catch up shortly."}
    syncs = [c.detail.removeprefix("last synced ") for c in checks if c.name.startswith("sync ") and c.status == "ok"]
    message = f"Up to date · synced {syncs[0]}" if syncs else "Up to date"
    drafts = by_name.get("drafts")
    if drafts and drafts.detail[0].isdigit():
        message += f" · {drafts.detail.split()[0]} draft(s) waiting for your approval"
    return {"status": "ok", "message": message}
