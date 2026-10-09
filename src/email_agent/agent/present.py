"""Human-facing presentation shared by every client (CLI now, web and mobile later)."""

from email_agent.db.pool import connection


def _quote(text: str, limit: int = 40) -> str:
    text = " ".join(str(text).split())
    return f"“{text[:limit]}{'…' if len(text) > limit else ''}”"


def activity_label(tool: str, args: dict | None = None) -> str:
    """What the agent is doing, in plain words, for the live thinking row."""
    args = args or {}
    match tool:
        case "search_emails":
            return f"Searching your mail for {_quote(args['query'])}" if args.get("query") else "Searching your mail"
        case "filter_emails":
            return "Going through your inbox"
        case "get_email":
            return "Reading an email"
        case "get_thread":
            return "Reading the conversation"
        case "aggregate_spend":
            return "Adding up your bills" if str(args.get("kind", "")).endswith("bill") else "Adding up the amounts"
        case "list_subscriptions":
            return "Checking your subscriptions"
        case "draft_email":
            return "Drafting a reply" if args.get("reply_to_email_id") else "Drafting an email"
        case "send_email":
            return "Getting it ready for your approval"
        case "get_user_context":
            return "Recalling what I know about you"
        case "save_memory":
            return "Saving that"
        case "delete_memory":
            return "Forgetting that"
        case _:
            return "Working on it"


def done_label(tool: str) -> str:
    """Past-tense step for the quiet trail under the thinking row."""
    return {
        "search_emails": "Searched your mail",
        "filter_emails": "Went through your inbox",
        "get_email": "Read an email",
        "get_thread": "Read a conversation",
        "aggregate_spend": "Added up amounts",
        "list_subscriptions": "Checked subscriptions",
        "draft_email": "Drafted",
        "send_email": "Asked for approval",
        "get_user_context": "Recalled your context",
        "save_memory": "Saved to memory",
        "delete_memory": "Removed from memory",
    }.get(tool, "Done")


async def sources(account_id: int, email_ids: list[int]) -> list[dict]:
    """Subject, sender and date for cited emails, in citation order."""
    if not email_ids:
        return []
    async with connection() as conn:
        rows = await (await conn.execute(
            """SELECT id, subject, from_name, from_addr, sent_at FROM emails
               WHERE account_id = %s AND id = ANY(%s)""",
            (account_id, email_ids),
        )).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [
        {
            "id": i,
            "subject": by_id[i]["subject"],
            "from": by_id[i]["from_name"] or by_id[i]["from_addr"],
            "date": f"{by_id[i]['sent_at']:%Y-%m-%d}" if by_id[i]["sent_at"] else None,
        }
        for i in email_ids
        if i in by_id
    ]
