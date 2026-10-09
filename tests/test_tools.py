import json

from email_agent.agent.tools import TOOL_SCHEMAS, ToolContext, call_tool
from email_agent.db.pool import connection


async def run(account, name, **args):
    return await call_tool(ToolContext(account), name, json.dumps(args))


def test_schemas_are_strict():
    for t in TOOL_SCHEMAS:
        params = t["parameters"]
        assert t["strict"] and params["additionalProperties"] is False
        assert set(params["required"]) == set(params["properties"]), t["name"]
        for name, prop in params["properties"].items():
            if "enum" in prop and "null" in prop["type"]:
                assert None in prop["enum"], (t["name"], name)


async def test_filter_emails_triage_order(ingested):
    res = await run(ingested, "filter_emails", needs_action=True, order_by="priority")
    assert res["total_matching"] == 3
    assert res["emails"][0]["from"].endswith("<alerts@hdfcbank.net>")
    assert res["emails"][0]["priority"] == 3
    assert "Pay HDFC card" == res["emails"][0]["action_summary"]


async def test_aggregate_spend_separates_currencies(ingested):
    res = await run(ingested, "aggregate_spend", kind="bill", date_from="2026-10-01", date_to="2026-10-31", group_by="merchant")
    assert res["totals_by_currency"] == {"INR": 12450.0, "USD": 42.17}
    assert {g["merchant"] for g in res["groups"]} == {"HDFC Bank", "AWS"}
    card = await run(ingested, "aggregate_spend", kind="credit_card_bill", date_from="2026-10-01", date_to="2026-10-31")
    assert card["totals_by_currency"] == {"INR": 12450.0} and card["count"] == 1
    sept = await run(ingested, "aggregate_spend", kind="credit_card_bill", date_from="2026-09-01", date_to="2026-09-30")
    assert sept["count"] == 0


async def test_aggregate_spend_flags_duplicate_reminders(ingested):
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO emails (account_id, gm_msgid, subject, from_addr, sent_at, stage)
               VALUES (%s, 999, 'Reminder: card payment due', 'alerts@hdfcbank.net', '2026-10-05', 'embedded')""",
            (ingested.id,),
        )
        await conn.execute(
            """INSERT INTO email_facts (email_id, category, amount, amount_type, currency, merchant, is_credit_card_bill, model)
               SELECT id, 'bill', 12450, 'amount_due', 'INR', 'HDFC Bank', true, 'test' FROM emails WHERE gm_msgid = 999"""
        )
    res = await run(ingested, "aggregate_spend", kind="credit_card_bill", date_from="2026-10-01", date_to="2026-10-31")
    assert res["totals_by_currency"] == {"INR": 24900.0}
    assert res["totals_excluding_possible_duplicates"] == {"INR": 12450.0}
    assert "possible_duplicate_of" in res["line_items"][1]


async def test_list_subscriptions(ingested):
    res = await run(ingested, "list_subscriptions", active_since="2026-01-01")
    services = {s["service"]: s for s in res["paid_services"]}
    assert services["Netflix"]["latest_amount"] == 15.49
    assert services["Netflix"]["unsubscribe"] == "https://www.netflix.com/unsubscribe?id=abc"
    assert "AWS" in services


async def test_get_email_and_thread(ingested):
    found = await run(ingested, "search_emails", query="Q4 planning deck")
    top = found["results"][0]
    assert top["subject"] == "Re: Q4 planning deck"
    email = await run(ingested, "get_email", email_id=top["email_id"])
    assert email["body_text"].startswith("Sounds good")
    thread = await run(ingested, "get_thread", thread_id=top["thread_id"])
    assert len(thread["messages"]) == 1


async def test_errors_become_observations(ingested):
    assert "error" in await run(ingested, "get_email", email_id=123456)
    assert "error" in await run(ingested, "aggregate_spend", kind="bill", date_from="not-a-date", date_to="2026-01-01")
    assert "error" in await call_tool(ToolContext(ingested), "nope", "{}")
