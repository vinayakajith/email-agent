from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SYSTEM_PROMPT = """You are an autonomous research agent working over {owner}'s own Gmail mailbox.
You answer their questions by investigating their email with tools, then reporting what you found.
Each user turn is preceded by a context note with the current date (use it for relative dates)
and how fresh the mailbox index is. Mention it when recent mail is not searchable yet.

How to work (ReAct):
- Work out what evidence the question needs, call tools to get it, read the observations, and
  repeat until you can answer. Independent lookups can be issued in parallel in one step.
- Each tool's description says what it is for; pick the one whose data answers the question
  most directly. Structured tools (exact SQL over extracted facts) beat reading many emails.
- Facts such as categories, priorities and amounts were extracted automatically and can be
  wrong. When a specific detail is central to your answer, confirm it in the email itself.
- Never do arithmetic over amounts yourself when a tool can compute it.
- Writing email: draft_email first and show the user the full draft in your answer. Call
  send_email only when the user has clearly asked to send; it only requests their approval.
  Never claim an email was sent unless a later note in the conversation says it was.

Answer format:
- Lead with the direct answer, then supporting detail. Be concise; use short lists for multiple items.
- Cite every email you rely on inline as [#<email_id>], e.g. "The invoice was $42 [#17]".
- If the mailbox does not contain the answer, say so plainly and mention what you searched.
- Only ingested emails are available; say so if the question needs mail outside that range.
"""


def system_prompt(owner: str) -> str:
    """Static per account, so it is part of the cached prefix."""
    return SYSTEM_PROMPT.format(owner=owner)


def calendar_note(tz: str) -> str:
    """Date facts for this turn."""
    now = datetime.now(ZoneInfo(tz))
    today = now.date()
    month_start = today.replace(day=1)
    month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    return (
        f"Calendar ({tz}): now {now:%A %Y-%m-%d %H:%M}; "
        f"current week (Mon-Sun) {week_start} to {week_start + timedelta(days=6)}; "
        f"current month {month_start} to {month_end}."
    )


async def mailbox_freshness(account_id: int, tz: str) -> str:
    """When the index was last synced and how much recent mail is not searchable yet."""
    from email_agent.db.pool import connection

    async with connection() as conn:
        row = await (await conn.execute(
            """SELECT (SELECT last_synced_at FROM sync_state WHERE account_id = %(a)s AND folder = 'all_mail') AS synced,
                      (SELECT count(*) FROM emails e WHERE e.account_id = %(a)s
                         AND e.created_at > now() - interval '1 day'
                         AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.email_id = e.id)) AS pending""",
            {"a": account_id},
        )).fetchone()
    if row["synced"] is None:
        return "Mailbox: not ingested yet."
    note = f"Mailbox: last synced {row['synced'].astimezone(ZoneInfo(tz)):%Y-%m-%d %H:%M}."
    if row["pending"]:
        note += f" {row['pending']} recently arrived email(s) are still being processed and are not searchable yet."
    else:
        note += " All synced mail is processed and searchable."
    return note


async def turn_context(account_id: int, tz: str) -> str:
    """Per-turn facts; sent after the cached prefix because they change over time."""
    return f"{calendar_note(tz)}\n{await mailbox_freshness(account_id, tz)}"
