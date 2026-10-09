"""Agent tools: strict JSON schemas plus their implementations.

Each tool returns a JSON-serialisable dict. Results carry email ids so the agent can cite
them and the CLI can render sources.
"""

import asyncio
import json
import re
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
        "Exact totals of money amounts extracted from emails. Always use this for totals instead of "
        "adding numbers yourself. Totals are per currency. Several emails about the same bill "
        "(statement, reminders, third-party payment nudges) are merged into one line item using the "
        "latest amount; each line item lists all its email ids.",
        {
            "kind": {
                "type": "string",
                "enum": ["credit_card_bill", "bill", "receipt", "subscription", "refund"],
                "description": "credit_card_bill = card statements; bill = any amount-due invoice/statement; "
                "receipt = one-off payments made; subscription = recurring service charges.",
            },
            "date_from": DATE,
            "date_to": {**DATE, "description": "YYYY-MM-DD, inclusive"},
            "date_basis": {
                "type": "string",
                "enum": ["due_date", "email_date"],
                "description": "Which date must fall in the range. Default: due_date for bills (falls back "
                "to email date when a bill has no due date), email_date for everything else.",
            },
            "group_by": {"type": "string", "enum": ["merchant", "month"]},
            "merchant": {"type": "string", "description": "Substring filter on merchant name."},
        },
        required=["kind", "date_from", "date_to"],
    ),
    _fn(
        "draft_email",
        "Prepare an email or a reply. Nothing is sent: it returns a draft_id to show the user. For "
        "replies pass reply_to_email_id; 'to' then defaults to the original sender and the subject "
        "and threading headers are set automatically.",
        {
            "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient addresses."},
            "cc": {"type": "array", "items": {"type": "string"}},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain-text body, signed as the mailbox owner."},
            "reply_to_email_id": {"type": "integer"},
        },
        required=["body"],
    ),
    _fn(
        "send_email",
        "Send a previously drafted email. Only call this when the user has asked for the email to be "
        "sent. The user is always shown the draft and must approve it; if they decline, ask what to change.",
        {"draft_id": {"type": "integer"}},
        required=["draft_id"],
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
    "credit_card_bill": "f.is_credit_card_bill AND f.amount_type IN ('amount_due', 'minimum_due')",
    "bill": "f.category = 'bill' AND f.amount_type IN ('amount_due', 'minimum_due')",
    "receipt": "f.category IN ('receipt', 'shipping', 'travel', 'banking') AND f.amount_type = 'charged'",
    "subscription": "(f.category = 'subscription' OR f.is_recurring) AND f.amount_type IN ('charged', 'amount_due')",
    "refund": "f.amount_type = 'refund'",
}


BILL_KINDS = {"credit_card_bill", "bill"}
# Without a due date, the same merchant + amount within this window is treated as one bill.
SAME_BILL_WINDOW_DAYS = {"bill": 35, "other": 10}


def merge_repeats(items: list[dict], bills: bool) -> list[dict]:
    """Collapse repeated emails about the same charge into one line item.

    Bills with a due date are keyed on (merchant, currency, due date): every reminder for that
    statement merges, and the amount is the latest one (it drops after partial payments).
    Anything else merges when merchant, currency and amount repeat within a short window.
    """
    window = SAME_BILL_WINDOW_DAYS["bill" if bills else "other"]
    merged: list[dict] = []
    by_due: dict[tuple, dict] = {}
    for it in sorted(items, key=lambda i: (i["date"], i["email_id"])):
        merchant = it["merchant"].strip().lower()
        target = None
        if bills and it["due_date"]:
            target = by_due.get((merchant, it["currency"], it["due_date"]))
        else:
            for m in reversed(merged):
                if (
                    m["_merchant"] == merchant and m["currency"] == it["currency"] and m["amount"] == it["amount"]
                    and not m.get("due_date") and (it["date"] - m["last_email_date"]).days <= window
                ):
                    target = m
                    break
        if target is None:
            target = {
                "_merchant": merchant, "merchant": it["merchant"], "amount": it["amount"], "currency": it["currency"],
                "due_date": it["due_date"], "first_email_date": it["date"], "last_email_date": it["date"],
                "latest_email_id": it["email_id"], "email_ids": [it["email_id"]], "subject": it["subject"],
            }
            merged.append(target)
            if bills and it["due_date"]:
                by_due[(merchant, it["currency"], it["due_date"])] = target
            continue
        if it["amount"] != target["amount"]:
            target.setdefault("earlier_amounts", []).append(target["amount"])
            target["amount"] = it["amount"]
        target["email_ids"].append(it["email_id"])
        target["latest_email_id"] = it["email_id"]
        target["last_email_date"] = it["date"]
        target["subject"] = it["subject"]
    for m in merged:
        del m["_merchant"]
    return merged


async def aggregate_spend(ctx: ToolContext, kind, date_from, date_to, date_basis=None, group_by=None, merchant=None):
    bills = kind in BILL_KINDS
    basis = date_basis or ("due_date" if bills else "email_date")
    date_expr = "COALESCE(f.due_date, e.sent_at::date)" if basis == "due_date" else "e.sent_at::date"
    where = [
        "e.account_id = %s", "f.amount IS NOT NULL", SPEND_KIND_SQL[kind],
        f"{date_expr} >= %s", f"{date_expr} <= %s",
    ]
    params: list = [ctx.account.id, _date(date_from), _date(date_to)]
    if merchant:
        where.append("f.merchant ILIKE %s")
        params.append(f"%{merchant}%")
    async with connection() as conn:
        rows = await (await conn.execute(
            f"""SELECT e.id AS email_id, e.sent_at::date AS date, COALESCE(f.merchant, e.from_addr) AS merchant,
                       f.amount, COALESCE(f.currency, '???') AS currency, f.due_date, e.subject, f.amount_type
                FROM emails e JOIN email_facts f ON f.email_id = e.id
                WHERE {' AND '.join(where)}""",
            params,
        )).fetchall()
    # A bill whose total is unknown (only the minimum due was stated) cannot be summed honestly.
    minimum_only = merge_repeats([r for r in rows if r["amount_type"] == "minimum_due"], bills)
    items = merge_repeats([r for r in rows if r["amount_type"] != "minimum_due"], bills)
    totals: dict[str, Decimal] = {}
    groups: dict[tuple[str, str], Decimal] = {}
    for it in items:
        totals[it["currency"]] = totals.get(it["currency"], Decimal(0)) + it["amount"]
        if group_by:
            when = it["due_date"] or it["first_email_date"]
            key = it["merchant"] if group_by == "merchant" else f"{when:%Y-%m}"
            groups[(key, it["currency"])] = groups.get((key, it["currency"]), Decimal(0)) + it["amount"]
    result = {
        "kind": kind,
        "period": f"{date_from} to {date_to}",
        "date_basis": basis,
        "distinct_items": len(items),
        "emails_considered": len(rows),
        "totals_by_currency": {c: float(v) for c, v in totals.items()},
        "line_items": [_clean({**it, "amount": float(it["amount"]),
                               "earlier_amounts": [float(a) for a in it.get("earlier_amounts", [])]})
                       for it in items[:60]],
    }
    if minimum_only:
        result["bills_with_only_minimum_due"] = [
            _clean({**it, "minimum_due": float(it.pop("amount")), "earlier_amounts": []}) for it in minimum_only
        ]
        result["note_on_minimum_due"] = (
            "These bills state only a minimum amount due (total missing from the email); they are NOT in "
            "the totals. Mention them separately."
        )
    if any("earlier_amounts" in it for it in items):
        result["note_on_amounts"] = (
            "Some bills changed amount across emails (often a partial payment, or two cards from the same "
            "issuer sharing a due date); the latest amount is used. Check with get_email if it matters."
        )
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


EMAIL_RE = re.compile(r"^[^@\s<>,]+@[^@\s<>,]+\.[^@\s<>,]+$")


def _addresses(values: list[str] | None) -> list[str]:
    out = []
    for v in values or []:
        v = v.strip()
        if not EMAIL_RE.match(v):
            raise ValueError(f"Not a valid email address: {v!r}")
        out.append(v.lower())
    return out


async def draft_email(ctx: ToolContext, body, to=None, cc=None, subject=None, reply_to_email_id=None):
    from email_agent.smtp.sender import reply_subject

    to_addrs, cc_addrs = _addresses(to), _addresses(cc)
    if reply_to_email_id is not None:
        async with connection() as conn:
            original = await (await conn.execute(
                "SELECT subject, from_addr FROM emails WHERE id = %s AND account_id = %s",
                (reply_to_email_id, ctx.account.id),
            )).fetchone()
        if original is None:
            return {"error": f"No email with id {reply_to_email_id} to reply to"}
        to_addrs = to_addrs or [original["from_addr"]]
        subject = subject or reply_subject(original["subject"])
    if not to_addrs:
        return {"error": "A draft needs at least one recipient"}
    if not subject:
        return {"error": "A new email needs a subject"}
    async with connection() as conn:
        row = await (await conn.execute(
            """INSERT INTO drafts (account_id, to_addrs, cc_addrs, subject, body, in_reply_to_email_id)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (ctx.account.id, to_addrs, cc_addrs, subject, body, reply_to_email_id),
        )).fetchone()
    return {
        "draft_id": row["id"], "to": to_addrs, "cc": cc_addrs, "subject": subject, "body": body,
        "status": "draft saved, not sent",
    }


async def _load_draft(ctx: ToolContext, draft_id: int) -> dict | None:
    async with connection() as conn:
        return await (await conn.execute(
            """SELECT d.*, e.message_id AS reply_message_id, e."references" AS reply_references
               FROM drafts d LEFT JOIN emails e ON e.id = d.in_reply_to_email_id
               WHERE d.id = %s AND d.account_id = %s""",
            (draft_id, ctx.account.id),
        )).fetchone()


def _draft_preview(d: dict) -> dict:
    return {"draft_id": d["id"], "to": d["to_addrs"], "cc": d["cc_addrs"], "subject": d["subject"], "body": d["body"]}


async def send_email(ctx: ToolContext, draft_id):
    from email_agent.smtp.sender import build_message, send_message

    d = await _load_draft(ctx, draft_id)
    if d is None:
        return {"error": f"No draft with id {draft_id}"}
    if d["status"] != "pending":
        return {"error": f"Draft {draft_id} is already {d['status']}"}
    # The tool asks for confirmation itself, so nothing is ever sent without the user's yes.
    if not await ctx.confirm(_draft_preview(d)):
        return {"sent": False, "draft_id": draft_id, "reason": "The user declined to send this draft."}
    msg = build_message(
        ctx.account.email, d["to_addrs"], d["cc_addrs"], d["subject"], d["body"],
        d["reply_message_id"], d["reply_references"],
    )
    message_id = await asyncio.to_thread(
        send_message, ctx.account.smtp_host, ctx.account.email, ctx.account.password, msg
    )
    async with connection() as conn:
        await conn.execute(
            "UPDATE drafts SET status = 'sent', smtp_message_id = %s, sent_at = now() WHERE id = %s",
            (message_id, draft_id),
        )
    return {"sent": True, "draft_id": draft_id, "message_id": message_id, "to": d["to_addrs"]}


TOOLS: dict[str, Callable[..., Awaitable[dict]]] = {
    "search_emails": search_emails,
    "filter_emails": filter_emails,
    "get_email": get_email,
    "get_thread": get_thread,
    "aggregate_spend": aggregate_spend,
    "list_subscriptions": list_subscriptions,
    "draft_email": draft_email,
    "send_email": send_email,
}


# Tools that act on the outside world and must be confirmed by the user first.
CONFIRM_TOOLS: set[str] = {"send_email"}


async def confirmation_preview(ctx: ToolContext, name: str, arguments: str) -> dict | None:
    """What a UI shows before a CONFIRM_TOOLS call runs (None if the call is invalid)."""
    try:
        d = await _load_draft(ctx, json.loads(arguments)["draft_id"])
    except Exception:
        return None
    return _draft_preview(d) if d and d["status"] == "pending" else None


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
