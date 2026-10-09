"""Draft lifecycle. The agent can draft and request approval; only a user action sends.

    pending --(agent: send_email)--> awaiting_approval --(user: approve)--> sending --> sent
                                                       \\--(user: reject)--> discarded

Approval is stored, not an in-process callback, so it can come from any client or device
after the agent's turn has ended. The outcome is appended to the conversation so the agent
knows about it on its next turn.
"""

import asyncio
import logging
import uuid

from email_agent.accounts import Account
from email_agent.db.pool import connection
from email_agent.prompt_cache import text_message

log = logging.getLogger(__name__)


class DraftError(Exception):
    pass


def preview(d: dict) -> dict:
    return {"draft_id": d["id"], "to": d["to_addrs"], "cc": d["cc_addrs"], "subject": d["subject"], "body": d["body"]}


async def _note(conversation_id: uuid.UUID | None, text: str) -> None:
    from email_agent import conversations

    if conversation_id is not None:
        await conversations.append(conversation_id, [text_message("developer", text, breakpoint=False)])


async def request_approval(account: Account, draft_id: int, conversation_id: uuid.UUID | None) -> dict:
    """Mark a draft as waiting for the user's decision. Never sends anything."""
    async with connection() as conn:
        d = await (await conn.execute(
            """UPDATE drafts SET status = 'awaiting_approval', conversation_id = COALESCE(%s, conversation_id)
               WHERE id = %s AND account_id = %s AND status IN ('pending', 'awaiting_approval')
               RETURNING *""",
            (conversation_id, draft_id, account.id),
        )).fetchone()
        if d is None:
            row = await (await conn.execute(
                "SELECT status FROM drafts WHERE id = %s AND account_id = %s", (draft_id, account.id)
            )).fetchone()
            raise DraftError(f"Draft {draft_id} is {row['status']}" if row else f"No draft with id {draft_id}")
    return preview(d)


async def pending_drafts(account: Account) -> list[dict]:
    async with connection() as conn:
        rows = await (await conn.execute(
            "SELECT * FROM drafts WHERE account_id = %s AND status = 'awaiting_approval' ORDER BY id",
            (account.id,),
        )).fetchall()
    return [preview(r) for r in rows]


async def approve_draft(account: Account, draft_id: int) -> dict:
    """The user approved: send the draft once. Returns {"sent": True, "message_id": ...}."""
    from email_agent.smtp.sender import build_message, send_message

    async with connection() as conn:
        # Moving to 'sending' first means two simultaneous approvals cannot both send.
        d = await (await conn.execute(
            """UPDATE drafts d SET status = 'sending', error = NULL
               FROM drafts src LEFT JOIN emails e ON e.id = src.in_reply_to_email_id
               WHERE d.id = src.id AND d.id = %s AND d.account_id = %s AND d.status = 'awaiting_approval'
               RETURNING d.*, e.message_id AS reply_message_id, e."references" AS reply_references""",
            (draft_id, account.id),
        )).fetchone()
    if d is None:
        raise DraftError(f"Draft {draft_id} is not awaiting approval")
    msg = build_message(
        account.email, d["to_addrs"], d["cc_addrs"], d["subject"], d["body"], d["reply_message_id"], d["reply_references"]
    )
    try:
        message_id = await asyncio.to_thread(send_message, account.smtp_host, account.email, account.password, msg)
    except Exception as e:
        async with connection() as conn:
            await conn.execute(
                "UPDATE drafts SET status = 'awaiting_approval', error = %s WHERE id = %s", (f"{type(e).__name__}: {e}", draft_id)
            )
        raise DraftError(f"Sending failed: {type(e).__name__}: {e}") from e
    async with connection() as conn:
        await conn.execute(
            "UPDATE drafts SET status = 'sent', smtp_message_id = %s, sent_at = now() WHERE id = %s",
            (message_id, draft_id),
        )
    await _note(d["conversation_id"], f"The user approved draft {draft_id}; it was sent to {', '.join(d['to_addrs'])}.")
    log.info("draft %s sent as %s", draft_id, message_id)
    return {"sent": True, "draft_id": draft_id, "message_id": message_id}


async def reject_draft(account: Account, draft_id: int) -> dict:
    async with connection() as conn:
        d = await (await conn.execute(
            """UPDATE drafts SET status = 'discarded' WHERE id = %s AND account_id = %s
               AND status IN ('pending', 'awaiting_approval') RETURNING *""",
            (draft_id, account.id),
        )).fetchone()
    if d is None:
        raise DraftError(f"Draft {draft_id} cannot be rejected")
    await _note(d["conversation_id"], f"The user rejected draft {draft_id}; it was not sent.")
    return {"sent": False, "draft_id": draft_id}
