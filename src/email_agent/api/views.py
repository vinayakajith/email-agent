"""Client-facing views over the core: chat event stream, conversation rendering, suggestions.

Kept free of HTTP details so the web app today and a mobile client later share them.
"""

import re
from collections.abc import AsyncIterator
from datetime import date

from email_agent.agent import present
from email_agent.agent.events import (
    ConfirmationRequired,
    FinalAnswer,
    Observation,
    StepPreamble,
    TextDelta,
    ThoughtDelta,
    ToolCall,
    ToolStarted,
)
from email_agent.agent.loop import CITATION_RE, Agent
from email_agent.db.pool import connection

_APPROVAL_NOTE = re.compile(r"^The user (approved|rejected) draft (\d+)")


async def chat_events(agent: Agent, message: str) -> AsyncIterator[tuple[str, dict]]:
    """Agent events mapped to the small set of stream events clients render."""
    announced = False
    try:
        async for ev in agent.run(message):
            if not announced and agent.conversation_id:
                announced = True
                yield "conversation", {"id": str(agent.conversation_id)}
            if isinstance(ev, TextDelta):
                yield "text_delta", {"text": ev.text}
            elif isinstance(ev, ThoughtDelta):
                yield "thought_delta", {"text": ev.text}
            elif isinstance(ev, ToolStarted):
                yield "activity", {"tool": ev.name, "label": present.activity_label(ev.name)}
            elif isinstance(ev, ToolCall):
                yield "activity", {"tool": ev.name, "label": present.activity_label(ev.name, ev.arguments)}
            elif isinstance(ev, Observation):
                yield "step_done", {"tool": ev.name, "label": present.done_label(ev.name), "error": ev.error}
            elif isinstance(ev, StepPreamble):
                yield "preamble", {}
            elif isinstance(ev, ConfirmationRequired):
                yield "approval", {"draft": ev.draft}
            elif isinstance(ev, FinalAnswer):
                yield "final", {
                    "text": ev.text,
                    "sources": await present.sources(agent.account.id, ev.cited_email_ids),
                    "steps": ev.steps,
                }
    except Exception as e:  # the stream must end with something the client can show
        yield "error", {"message": f"Something went wrong: {type(e).__name__}"}


def _text(item: dict) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


async def render_conversation(account_id: int, items: list[dict], conversation_id) -> list[dict]:
    """Stored Responses API items -> what a chat screen shows.

    User messages and final answers are kept; tool calls become a quiet trail of steps on the
    answer; text that preceded tool calls (a preamble) is dropped; approval outcomes become notes.
    """
    from email_agent.drafts import preview

    messages: list[dict] = []
    steps: list[str] = []
    turn = 0
    for item in items:
        role, kind = item.get("role"), item.get("type")
        if role == "user":
            turn += 1
            steps = []
            messages.append({"role": "user", "text": _text(item)})
        elif kind == "function_call":
            if messages and messages[-1]["role"] == "assistant" and messages[-1]["turn"] == turn:
                messages.pop()  # text right before a tool call was a preamble, not the answer
            steps.append(present.done_label(item.get("name", "")))
        elif kind == "message" and role == "assistant":
            messages.append({"role": "assistant", "text": _text(item), "steps": list(dict.fromkeys(steps)), "turn": turn})
        elif role == "developer" and (m := _APPROVAL_NOTE.match(_text(item))):
            messages.append({"role": "note", "text": "Sent." if m.group(1) == "approved" else "Not sent.",
                             "draft_id": int(m.group(2))})
    for msg in messages:
        if msg["role"] == "assistant":
            msg.pop("turn")
            msg["sources"] = await present.sources(account_id, sorted({int(i) for i in CITATION_RE.findall(msg["text"])}))
    async with connection() as conn:
        drafts = await (await conn.execute(
            "SELECT * FROM drafts WHERE conversation_id = %s AND status = 'awaiting_approval' ORDER BY id",
            (conversation_id,),
        )).fetchall()
    return messages + [{"role": "approval", "draft": preview(d)} for d in drafts]


async def suggestions(account_id: int) -> list[str]:
    """Three starting questions grounded in what this mailbox actually contains."""
    month_start = date.today().replace(day=1)
    async with connection() as conn:
        row = await (await conn.execute(
            """SELECT
                 EXISTS (SELECT 1 FROM emails e JOIN email_facts f ON f.email_id = e.id
                         WHERE e.account_id = %(a)s AND f.is_credit_card_bill
                           AND COALESCE(f.due_date, e.sent_at::date) >= %(m)s) AS card_bills,
                 EXISTS (SELECT 1 FROM emails e JOIN email_facts f ON f.email_id = e.id
                         WHERE e.account_id = %(a)s AND f.needs_action AND e.sent_at > now() - interval '7 days') AS actions,
                 EXISTS (SELECT 1 FROM emails e JOIN email_facts f ON f.email_id = e.id
                         WHERE e.account_id = %(a)s AND (f.category = 'subscription' OR f.is_recurring)) AS subs""",
            {"a": account_id, "m": month_start},
        )).fetchone()
    picks = []
    if row["actions"]:
        picks.append("What needs my attention this week?")
    if row["card_bills"]:
        picks.append("What do my credit card bills add up to this month?")
    if row["subs"]:
        picks.append("Which subscriptions could I cancel?")
    for fallback in ("What's new in my inbox today?", "Who emailed me most this month?", "Anything I forgot to reply to?"):
        if len(picks) == 3:
            break
        picks.append(fallback)
    return picks[:3]
