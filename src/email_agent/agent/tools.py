"""Agent tools: strict JSON schemas plus their implementations.

Each tool returns a JSON-serialisable dict. Results carry email ids so the agent can cite
them and the CLI can render sources.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from email_agent.accounts import Account
from email_agent.db.pool import connection
from email_agent.ingest.enrich import truncate_tokens
from email_agent.search.filters import SearchFilters
from email_agent.search.hybrid import hybrid_search

CATEGORIES = [
    "personal", "work", "bill", "receipt", "subscription", "banking", "security", "travel",
    "shipping", "newsletter", "promotion", "social", "notification", "other",
]
OBSERVATION_MAX_TOKENS = 4_000


def _nullable(schema: dict) -> dict:
    t = schema["type"]
    out = {**schema, "type": [t, "null"] if isinstance(t, str) else [*t, "null"]}
    if "enum" in schema:
        out["enum"] = [*schema["enum"], None]
    return out


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    """Strict function schema: every property is required; optional ones accept null."""
    required = set(required or [])
    props = {k: (v if k in required else _nullable(v)) for k, v in properties.items()}
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


DATE = {"type": "string", "description": "YYYY-MM-DD"}
CATEGORY = {"type": "string", "enum": CATEGORIES}

TOOL_SCHEMAS = [
    _fn(
        "search_emails",
        "Hybrid keyword + semantic search over the mailbox, reranked by relevance. Use for finding "
        "emails about a topic, person, company, or event. Returns the best-matching excerpt per email.",
        {
            "query": {"type": "string", "description": "What to look for, in natural language or keywords."},
            "date_from": DATE,
            "date_to": {**DATE, "description": "YYYY-MM-DD, inclusive"},
            "sender": {"type": "string", "description": "Substring of sender address or name, e.g. 'amazon'."},
            "category": CATEGORY,
            "limit": {"type": "integer", "description": "Max results, default 10, max 25."},
        },
        required=["query"],
    ),
    _fn(
        "filter_emails",
        "Exact structured listing of emails (no semantic matching). Use for triage questions like "
        "'what needs my attention', 'unread from X', 'everything from last week'. Facts such as "
        "priority (0-3), needs_action and deadlines were extracted from each email at ingest.",
        {
            "date_from": DATE,
            "date_to": {**DATE, "description": "YYYY-MM-DD, inclusive"},
            "sender": {"type": "string"},
            "category": CATEGORY,
            "needs_action": {"type": "boolean"},
            "min_priority": {"type": "integer", "description": "0-3; 2+ means important."},
            "unread": {"type": "boolean"},
            "has_attachment": {"type": "boolean"},
            "order_by": {"type": "string", "enum": ["newest", "oldest", "priority", "deadline"]},
            "limit": {"type": "integer", "description": "Default 20, max 50."},
        },
    ),
    _fn(
        "get_email",
        "Read one email in full (body truncated if very long) with its extracted facts.",
        {"email_id": {"type": "integer"}},
        required=["email_id"],
    ),
    _fn(
        "get_thread",
        "Read a whole conversation thread in date order.",
        {"thread_id": {"type": "integer"}},
        required=["thread_id"],
    ),
    _fn(
        "aggregate_spend",
        "Exact totals of money amounts extracted from emails, by email date. Always use this for "
        "totals instead of adding numbers yourself. Totals are per currency. Line items include "
        "email ids and flag likely duplicates (e.g. a statement and its reminder) for you to check.",
        {
            "kind": {
                "type": "string",
                "enum": ["credit_card_bill", "bill", "receipt", "subscription", "refund"],
                "description": "credit_card_bill = card statements; bill = any amount-due invoice/statement; "
                "receipt = one-off payments made; subscription = recurring service charges.",
            },
            "date_from": DATE,
            "date_to": {**DATE, "description": "YYYY-MM-DD, inclusive"},
            "group_by": {"type": "string", "enum": ["merchant", "month"]},
            "merchant": {"type": "string", "description": "Substring filter on merchant name."},
        },
        required=["kind", "date_from", "date_to"],
    ),
    _fn(
        "list_subscriptions",
        "Recurring paid services detected in the mailbox, with cadence, latest amount, last charge "
        "date and unsubscribe link. Set include_newsletters to also list mailing lists with how many "
        "of their emails are left unread, which helps decide what to unsubscribe from.",
        {
            "active_since": {**DATE, "description": "Only services charged since this date. Default: 13 months ago."},
            "include_newsletters": {"type": "boolean"},
        },
    ),
]


async def deny_all(draft: dict) -> bool:
    return False


@dataclass
class ToolContext:
    account: Account
    # Asked before any irreversible action (sending email); returns True to proceed.
    confirm: Callable[[dict], Awaitable[bool]] = deny_all


def _iso(v):
    if isinstance(v, (date,)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _clean(row: dict) -> dict:
    return {k: _iso(v) for k, v in row.items() if v is not None and v != [] and v != ""}


def _date(v: str | None) -> date | None:
    return date.fromisoformat(v) if v else None


async def search_emails(ctx: ToolContext, query, date_from=None, date_to=None, sender=None, category=None, limit=None):
    filters = SearchFilters(_date(date_from), _date(date_to), sender, category)
    hits = await hybrid_search(query, filters, k=min(limit or 10, 25))
    results = []
    for h in hits:
        # Drop the repeated header lines from the excerpt; they are already in the fields.
        body = h.excerpt.split("\n\n", 1)[1] if "\n\n" in h.excerpt else h.excerpt
        results.append(
            {
                "email_id": h.email_id,
                "thread_id": h.thread_id,
                "date": f"{h.sent_at:%Y-%m-%d}" if h.sent_at else None,
                "from": f"{h.from_name} <{h.from_addr}>",
                "subject": h.subject,
                "relevance": round(h.score, 3),
                "excerpt": truncate_tokens(body, 250),
            }
        )
    return {"results": results, "count": len(results)}


FILTER_ORDER = {
    "newest": "e.sent_at DESC",
    "oldest": "e.sent_at ASC",
    "priority": "f.priority DESC NULLS LAST, e.sent_at DESC",
    "deadline": "COALESCE(f.deadline, f.due_date) ASC NULLS LAST, f.priority DESC NULLS LAST",
}


async def filter_emails(
    ctx: ToolContext, date_from=None, date_to=None, sender=None, category=None, needs_action=None,
    min_priority=None, unread=None, has_attachment=None, order_by=None, limit=None,
):
    clauses, params = ["e.account_id = %s"], [ctx.account.id]
    if date_from:
        clauses.append("e.sent_at >= %s")
        params.append(_date(date_from))
    if date_to:
        clauses.append("e.sent_at < %s")
        params.append(_date(date_to) + timedelta(days=1))
    if sender:
        clauses.append("(e.from_addr ILIKE %s OR e.from_name ILIKE %s)")
        params += [f"%{sender}%"] * 2
    if category:
        clauses.append("f.category = %s")
        params.append(category)
    if needs_action is not None:
        clauses.append("f.needs_action = %s")
        params.append(needs_action)
    if min_priority is not None:
        clauses.append("f.priority >= %s")
        params.append(min_priority)
    if unread is not None:
        clauses.append("e.is_unread = %s")
        params.append(unread)
    if has_attachment is not None:
        clauses.append("e.has_attachments = %s")
        params.append(has_attachment)
    where = " AND ".join(clauses)
    order = FILTER_ORDER.get(order_by or "newest", FILTER_ORDER["newest"])
    limit = min(limit or 20, 50)
    async with connection() as conn:
        total = (await (await conn.execute(
            f"SELECT count(*) AS n FROM emails e LEFT JOIN email_facts f ON f.email_id = e.id WHERE {where}", params
        )).fetchone())["n"]
        rows = await (await conn.execute(
            f"""SELECT e.id AS email_id, e.gm_thrid AS thread_id, e.sent_at::date AS date,
                       e.from_name || ' <' || e.from_addr || '>' AS "from", e.subject, e.is_unread AS unread,
                       f.category, f.priority, f.needs_action, f.action_summary, f.deadline, f.due_date,
                       f.amount, f.currency, f.summary
                FROM emails e LEFT JOIN email_facts f ON f.email_id = e.id
                WHERE {where} ORDER BY {order} LIMIT %s""",
            [*params, limit],
        )).fetchall()
    return {"total_matching": total, "returned": len(rows), "emails": [_clean(r) for r in rows]}


async def get_email(ctx: ToolContext, email_id):
    async with connection() as conn:
        row = await (await conn.execute(
            """SELECT e.id AS email_id, e.gm_thrid AS thread_id, e.sent_at, e.from_name, e.from_addr,
                      e.to_addrs, e.cc_addrs, e.subject, e.labels, e.is_unread AS unread, e.attachments,
                      e.list_unsubscribe, e.body_text, f.category, f.priority, f.needs_action,
                      f.action_summary, f.deadline, f.amount, f.amount_type, f.currency, f.merchant,
                      f.due_date, f.is_recurring, f.billing_period
               FROM emails e LEFT JOIN email_facts f ON f.email_id = e.id
               WHERE e.id = %s AND e.account_id = %s""",
            (email_id, ctx.account.id),
        )).fetchone()
    if row is None:
        return {"error": f"No email with id {email_id}"}
    row["sent_at"] = f"{row['sent_at']:%Y-%m-%d %H:%M %Z}" if row["sent_at"] else None
    row["attachments"] = [a["filename"] for a in row["attachments"]]
    row["body_text"] = truncate_tokens(row["body_text"], 3_000)
    return _clean(row)


async def get_thread(ctx: ToolContext, thread_id):
    async with connection() as conn:
        rows = await (await conn.execute(
            """SELECT id AS email_id, sent_at, from_name, from_addr, to_addrs, subject, body_text
               FROM emails WHERE gm_thrid = %s AND account_id = %s ORDER BY sent_at LIMIT 30""",
            (thread_id, ctx.account.id),
        )).fetchall()
    if not rows:
        return {"error": f"No thread with id {thread_id}"}
    budget = max(300, 3_500 // len(rows))
    for r in rows:
        r["sent_at"] = f"{r['sent_at']:%Y-%m-%d %H:%M}" if r["sent_at"] else None
        r["body_text"] = truncate_tokens(r["body_text"], budget)
    return {"thread_id": thread_id, "messages": [_clean(r) for r in rows]}


SPEND_KIND_SQL = {
    "credit_card_bill": "f.is_credit_card_bill",
    "bill": "f.category = 'bill' AND f.amount_type = 'amount_due'",
    "receipt": "f.category IN ('receipt', 'shipping', 'travel', 'banking') AND f.amount_type = 'charged'",
    "subscription": "(f.category = 'subscription' OR f.is_recurring) AND f.amount_type IN ('charged', 'amount_due')",
    "refund": "f.amount_type = 'refund'",
}


async def aggregate_spend(ctx: ToolContext, kind, date_from, date_to, group_by=None, merchant=None):
    where = [
        "e.account_id = %s", "f.amount IS NOT NULL", SPEND_KIND_SQL[kind], "e.sent_at >= %s", "e.sent_at < %s",
    ]
    params: list = [ctx.account.id, _date(date_from), _date(date_to) + timedelta(days=1)]
    if merchant:
        where.append("f.merchant ILIKE %s")
        params.append(f"%{merchant}%")
    async with connection() as conn:
        items = await (await conn.execute(
            f"""SELECT e.id AS email_id, e.sent_at::date AS date, COALESCE(f.merchant, e.from_addr) AS merchant,
                       f.amount, COALESCE(f.currency, '???') AS currency, f.due_date, e.subject
                FROM emails e JOIN email_facts f ON f.email_id = e.id
                WHERE {' AND '.join(where)} ORDER BY e.sent_at""",
            params,
        )).fetchall()
    # Same merchant + amount + currency within 10 days is very likely one bill mailed twice.
    for i, it in enumerate(items):
        for prev in items[:i]:
            if (
                prev["merchant"].lower() == it["merchant"].lower()
                and prev["amount"] == it["amount"]
                and prev["currency"] == it["currency"]
                and abs((it["date"] - prev["date"]).days) <= 10
            ):
                it["possible_duplicate_of"] = prev["email_id"]
                break
    totals: dict[str, Decimal] = {}
    groups: dict[tuple[str, str], Decimal] = {}
    for it in items:
        totals[it["currency"]] = totals.get(it["currency"], Decimal(0)) + it["amount"]
        if group_by:
            key = it["merchant"] if group_by == "merchant" else f"{it['date']:%Y-%m}"
            groups[(key, it["currency"])] = groups.get((key, it["currency"]), Decimal(0)) + it["amount"]
    dupes = [it for it in items if "possible_duplicate_of" in it]
    result = {
        "kind": kind,
        "period": f"{date_from} to {date_to}",
        "count": len(items),
        "totals_by_currency": {c: float(v) for c, v in totals.items()},
        "line_items": [_clean(it) for it in items[:60]],
    }
    if dupes:
        result["totals_excluding_possible_duplicates"] = {
            c: float(v - sum((d["amount"] for d in dupes if d["currency"] == c), Decimal(0)))
            for c, v in totals.items()
        }
    if group_by:
        result["groups"] = [
            {group_by: k, "currency": c, "total": float(v)} for (k, c), v in sorted(groups.items(), key=lambda kv: -kv[1])
        ]
    if len(items) > 60:
        result["note"] = f"Showing 60 of {len(items)} line items; totals include all."
    return result


async def list_subscriptions(ctx: ToolContext, active_since=None, include_newsletters=None):
    since = _date(active_since) or (date.today() - timedelta(days=395))
    async with connection() as conn:
        paid = await (await conn.execute(
            """WITH s AS (
                   SELECT lower(COALESCE(f.merchant, e.from_addr)) AS key, COALESCE(f.merchant, e.from_addr) AS name,
                          e.id, e.sent_at, f.amount, f.currency, f.billing_period, e.list_unsubscribe
                   FROM emails e JOIN email_facts f ON f.email_id = e.id
                   WHERE e.account_id = %s AND e.sent_at >= %s
                     AND (f.category = 'subscription' OR f.is_recurring)
               )
               SELECT max(name) AS service, count(*) AS emails,
                      max(sent_at)::date AS last_email,
                      (array_agg(amount ORDER BY sent_at DESC) FILTER (WHERE amount IS NOT NULL))[1] AS latest_amount,
                      (array_agg(currency ORDER BY sent_at DESC) FILTER (WHERE amount IS NOT NULL))[1] AS currency,
                      mode() WITHIN GROUP (ORDER BY billing_period) AS billing_period,
                      (array_agg(list_unsubscribe ORDER BY sent_at DESC) FILTER (WHERE list_unsubscribe IS NOT NULL))[1] AS unsubscribe,
                      (array_agg(id ORDER BY sent_at DESC))[1:3] AS recent_email_ids
               FROM s GROUP BY key ORDER BY max(sent_at) DESC""",
            (ctx.account.id, since),
        )).fetchall()
        result = {"since": since.isoformat(), "paid_services": [_clean(r) for r in paid]}
        if include_newsletters:
            lists = await (await conn.execute(
                """SELECT max(e.from_name) AS sender, e.from_addr, count(*) AS emails,
                          round(avg(e.is_unread::int) * 100) AS pct_unread, max(e.sent_at)::date AS last_email,
                          (array_agg(e.list_unsubscribe ORDER BY e.sent_at DESC))[1] AS unsubscribe
                   FROM emails e JOIN email_facts f ON f.email_id = e.id
                   WHERE e.account_id = %s AND e.sent_at >= %s AND e.list_unsubscribe IS NOT NULL
                     AND f.category IN ('newsletter', 'promotion', 'social')
                   GROUP BY e.from_addr ORDER BY count(*) DESC LIMIT 40""",
                (ctx.account.id, since),
            )).fetchall()
            result["mailing_lists"] = [_clean(r) for r in lists]
    return result


TOOLS: dict[str, Callable[..., Awaitable[dict]]] = {
    "search_emails": search_emails,
    "filter_emails": filter_emails,
    "get_email": get_email,
    "get_thread": get_thread,
    "aggregate_spend": aggregate_spend,
    "list_subscriptions": list_subscriptions,
}


# Tools that act on the outside world and must be confirmed by the user first.
CONFIRM_TOOLS: set[str] = set()


async def confirmation_preview(ctx: ToolContext, name: str, arguments: str) -> dict | None:
    """What the user is shown before a CONFIRM_TOOLS call runs."""
    return None


async def call_tool(ctx: ToolContext, name: str, arguments: str) -> dict:
    """Run a tool; errors become observations so the agent can recover instead of crashing."""
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"Unknown tool {name}"}
    try:
        args = json.loads(arguments or "{}")
        return await fn(ctx, **args)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def render_observation(result: dict) -> str:
    return truncate_tokens(json.dumps(result, default=str, ensure_ascii=False), OBSERVATION_MAX_TOKENS)
