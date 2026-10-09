"""Per-email structured extraction with OpenAI structured outputs.

These facts turn questions like "credit card bills total this month" or "what needs my
attention" into exact SQL, instead of the agent guessing from retrieved snippets.
"""

import asyncio
from datetime import date
from typing import Literal

import tiktoken
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, InternalServerError, RateLimitError
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from email_agent.config import get_settings
from email_agent.db.pool import connection
from email_agent.prompt_cache import cache_key

MAX_BODY_TOKENS = 2_000
# Bump whenever EmailFacts or INSTRUCTIONS change meaning; `email-agent reenrich` then re-extracts
# facts produced by older versions so stored data matches the current definitions.
EXTRACTOR_VERSION = 2
PAGE_SIZE = 200
_enc = tiktoken.get_encoding("o200k_base")

Category = Literal[
    "personal", "work", "bill", "receipt", "subscription", "banking", "security", "travel",
    "shipping", "newsletter", "promotion", "social", "notification", "other",
]


class EmailFacts(BaseModel):
    category: Category = Field(
        description="bill = a statement/invoice asking for payment (incl. credit card statements); "
        "receipt = confirmation of a one-off payment already made; subscription = renewal, charge or "
        "plan notice for a recurring service; banking = card/account transaction alerts and bank notices."
    )
    priority: Literal[0, 1, 2, 3] = Field(
        description="3 = urgent and needs the owner soon (security alert, payment due within ~7 days, a "
        "real person waiting on a reply); 2 = needs action, not urgent; 1 = relevant FYI; "
        "0 = bulk, promotional or automated noise."
    )
    needs_action: bool = Field(description="True only if the mailbox owner must do something.")
    action_summary: str | None = Field(description="Imperative one-liner of what to do, or null.")
    deadline: str | None = Field(description="YYYY-MM-DD by which action is needed, or null.")
    amount: float | None = Field(description="The main money amount, as a plain number, or null.")
    amount_type: Literal["amount_due", "minimum_due", "charged", "refund", "balance", "other"] | None = Field(
        description="amount_due = the TOTAL due on a bill; minimum_due = only when the total is missing "
        "and the email gives just the minimum amount due."
    )
    currency: str | None = Field(description="ISO 4217 code, e.g. USD, INR, EUR; null if no amount.")
    merchant: str | None = Field(description="Company or service the money relates to, e.g. 'Netflix'.")
    is_recurring: bool = Field(description="True for subscription or other recurring charges.")
    billing_period: Literal["weekly", "monthly", "quarterly", "yearly", "one_time"] | None
    due_date: str | None = Field(description="Payment due date YYYY-MM-DD for bills, or null.")
    is_credit_card_bill: bool = Field(description="True only for a credit card statement / bill.")
    summary: str = Field(description="One sentence: what this email is about, with key specifics.")


INSTRUCTIONS = """You extract structured facts from one email in {owner}'s mailbox.
Be literal: use only what the email states. Use null when a field does not apply or is unknown.
Resolve relative dates ("by Friday") against the email's date. Amounts are plain numbers
without symbols or thousands separators. If the email was sent by {owner}, needs_action is false.
Only record amounts the email actually states; text that is not a real number (e.g. an
unfilled template field) is not an amount. For a bill, amount is the total due; when the total
is not stated but a minimum due is, record that with amount_type "minimum_due"."""


def truncate_tokens(text: str, max_tokens: int) -> str:
    tokens = _enc.encode(text, disallowed_special=())
    return text if len(tokens) <= max_tokens else _enc.decode(tokens[:max_tokens]) + "\n[truncated]"


def render_email(e: dict) -> str:
    attachments = ", ".join(a["filename"] for a in e["attachments"]) or "none"
    date_line = f"Date: {e['sent_at']:%Y-%m-%d %H:%M %Z}\n" if e["sent_at"] else "Date: unknown\n"
    return (
        f"From: {e['from_name']} <{e['from_addr']}>\n"
        f"To: {', '.join(e['to_addrs'])}\n"
        f"{date_line}"
        f"Subject: {e['subject']}\n"
        f"Gmail labels: {', '.join(e['labels']) or 'none'}\n"
        f"Attachments: {attachments}\n"
        f"Has unsubscribe link: {'yes' if e['list_unsubscribe'] else 'no'}\n\n"
        f"{truncate_tokens(e['body_text'], MAX_BODY_TOKENS)}"
    )


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


class Enricher:
    def __init__(self, owner: str, prompt_cache_key: str | None = None):
        s = get_settings()
        self.client = AsyncOpenAI(api_key=s.openai_api_key)
        self.model = s.openai_enrich_model
        self.reasoning = {"effort": s.enrich_reasoning_effort} if s.enrich_reasoning_effort else None
        self.instructions = INSTRUCTIONS.format(owner=owner)
        self.semaphore = asyncio.Semaphore(s.enrich_concurrency)
        # Every call shares the instructions + output schema prefix; the key keeps them on one cache.
        self.prompt_cache_key = prompt_cache_key

    @retry(
        retry=retry_if_exception_type((RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)),
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    async def extract(self, email: dict) -> EmailFacts:
        kwargs = {"reasoning": self.reasoning} if self.reasoning else {}
        async with self.semaphore:
            resp = await self.client.responses.parse(
                model=self.model,
                instructions=self.instructions,
                input=render_email(email),
                text_format=EmailFacts,
                store=False,
                prompt_cache_key=self.prompt_cache_key,
                **kwargs,
            )
        if resp.output_parsed is None:
            raise ValueError("model returned no parsed output (refusal or truncation)")
        return resp.output_parsed


SAVE_FACTS = """
INSERT INTO email_facts (
    email_id, category, priority, needs_action, action_summary, deadline, amount, amount_type,
    currency, merchant, is_recurring, billing_period, due_date, is_credit_card_bill, summary, model,
    extractor_version
) VALUES (
    %(email_id)s, %(category)s, %(priority)s, %(needs_action)s, %(action_summary)s, %(deadline)s,
    %(amount)s, %(amount_type)s, %(currency)s, %(merchant)s, %(is_recurring)s, %(billing_period)s,
    %(due_date)s, %(is_credit_card_bill)s, %(summary)s, %(model)s, %(extractor_version)s
)
ON CONFLICT (email_id) DO UPDATE SET
    category = EXCLUDED.category, priority = EXCLUDED.priority, needs_action = EXCLUDED.needs_action,
    action_summary = EXCLUDED.action_summary, deadline = EXCLUDED.deadline, amount = EXCLUDED.amount,
    amount_type = EXCLUDED.amount_type, currency = EXCLUDED.currency, merchant = EXCLUDED.merchant,
    is_recurring = EXCLUDED.is_recurring, billing_period = EXCLUDED.billing_period,
    due_date = EXCLUDED.due_date, is_credit_card_bill = EXCLUDED.is_credit_card_bill,
    summary = EXCLUDED.summary, model = EXCLUDED.model, extractor_version = EXCLUDED.extractor_version,
    extracted_at = now()
"""


def facts_row(email_id: int, f: EmailFacts, model: str) -> dict:
    return {
        **f.model_dump(),
        "email_id": email_id,
        "deadline": _date(f.deadline),
        "due_date": _date(f.due_date),
        "currency": f.currency.upper()[:3] if f.currency else None,
        "model": model,
        "extractor_version": EXTRACTOR_VERSION,
    }


async def enrich_stage(account, job_id: int, progress, enricher: Enricher | None = None) -> tuple[int, int]:
    """Extract facts for every email at stage 'fetched'. Returns (enriched, failed).

    Failures stay at 'fetched' and are retried on the next run.
    """
    enricher = enricher or Enricher(account.email, cache_key(account.id, "enrich"))
    async with connection() as conn:
        total = (
            await (
                await conn.execute(
                    "SELECT count(*) AS n FROM emails WHERE account_id = %s AND stage = 'fetched'", (account.id,)
                )
            ).fetchone()
        )["n"]
    done = failed = 0
    after_id = 0
    progress("enrich", 0, total)
    while True:
        async with connection() as conn:
            emails = await (
                await conn.execute(
                    """SELECT id, from_name, from_addr, to_addrs, sent_at, subject, labels, attachments,
                              list_unsubscribe, body_text
                       FROM emails WHERE account_id = %s AND stage = 'fetched' AND id > %s
                       ORDER BY id LIMIT %s""",
                    (account.id, after_id, PAGE_SIZE),
                )
            ).fetchall()
        if not emails:
            break
        after_id = emails[-1]["id"]
        results = await asyncio.gather(*(enricher.extract(e) for e in emails), return_exceptions=True)
        rows = []
        for e, r in zip(emails, results):
            if isinstance(r, BaseException):
                failed += 1
                print(f"enrich failed for email {e['id']}: {type(r).__name__}: {r}")
            else:
                rows.append(facts_row(e["id"], r, enricher.model))
        async with connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.executemany(SAVE_FACTS, rows)
                await conn.execute(
                    "UPDATE emails SET stage = 'enriched' WHERE id = ANY(%s)", ([r["email_id"] for r in rows],)
                )
                await conn.execute("UPDATE ingest_jobs SET enriched = enriched + %s WHERE id = %s", (len(rows), job_id))
        done += len(emails)
        progress("enrich", done, total)
    return done - failed, failed
