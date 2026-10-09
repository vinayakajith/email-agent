from email_agent.agent.present import activity_label, done_label, sources


def test_activity_labels_are_plain_language():
    assert activity_label("search_emails", {"query": "netflix renewal"}) == "Searching your mail for “netflix renewal”"
    assert activity_label("search_emails", {"query": "x" * 60}).endswith("…”")
    assert activity_label("search_emails") == "Searching your mail"
    assert activity_label("aggregate_spend", {"kind": "credit_card_bill"}) == "Adding up your bills"
    assert activity_label("aggregate_spend", {"kind": "receipt"}) == "Adding up the amounts"
    assert activity_label("draft_email", {"reply_to_email_id": 3}) == "Drafting a reply"
    assert activity_label("unknown_tool") == "Working on it"
    assert done_label("get_email") == "Read an email"


async def test_sources_keep_citation_order_and_scope(ingested):
    from email_agent.db.pool import connection

    async with connection() as conn:
        ids = [r["id"] for r in await (await conn.execute("SELECT id FROM emails ORDER BY id DESC LIMIT 2")).fetchall()]
    found = await sources(ingested.id, ids + [999999])
    assert [s["id"] for s in found] == ids
    assert set(found[0]) == {"id", "subject", "from", "date"}
    assert await sources(ingested.id + 1, ids) == []
