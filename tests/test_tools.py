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
    assert card["totals_by_currency"] == {"INR": 12450.0} and card["distinct_items"] == 1
    # Counted by due date (Oct 20), so it is not a September bill.
    sept = await run(ingested, "aggregate_spend", kind="credit_card_bill", date_from="2026-09-01", date_to="2026-09-30")
    assert sept["distinct_items"] == 0


async def _add_bill(account, gm_msgid, sent, merchant, amount, due, sender="protect@cred.club"):
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO emails (account_id, gm_msgid, subject, from_addr, sent_at, stage)
               VALUES (%s, %s, 'Your credit card bill is due', %s, %s, 'embedded')""",
            (account.id, gm_msgid, sender, sent),
        )
        await conn.execute(
            """INSERT INTO email_facts (email_id, category, amount, amount_type, currency, merchant, due_date,
                                        is_credit_card_bill, model)
               SELECT id, 'bill', %s, 'amount_due', 'INR', %s, %s, true, 'test' FROM emails WHERE gm_msgid = %s""",
            (amount, merchant, due, gm_msgid),
        )


async def test_repeated_bill_reminders_merge_into_one_bill(ingested):
    # Real-world pattern: a statement plus app reminders spread over 2+ weeks, same due date.
    for i, sent in enumerate(["2026-09-17", "2026-09-17", "2026-09-30", "2026-10-02"]):
        await _add_bill(ingested, 100 + i, sent, "ICICI Bank", 71407.09, "2026-10-04")
    # Partial payment: the amount drops between reminders for the same due date.
    await _add_bill(ingested, 200, "2026-09-16", "Axis Bank", 44108.78, "2026-10-05")
    await _add_bill(ingested, 201, "2026-10-01", "Axis Bank", 37215.78, "2026-10-05")
    # Due in late September: outside October even though reminders keep arriving.
    await _add_bill(ingested, 300, "2026-09-19", "Kotak Mahindra Bank", 10623.66, "2026-09-23")

    res = await run(ingested, "aggregate_spend", kind="credit_card_bill", date_from="2026-10-01", date_to="2026-10-31")
    items = {i["merchant"]: i for i in res["line_items"]}
    assert set(items) == {"ICICI Bank", "Axis Bank", "HDFC Bank"}
    assert len(items["ICICI Bank"]["email_ids"]) == 4
    assert items["Axis Bank"]["amount"] == 37215.78 and items["Axis Bank"]["earlier_amounts"] == [44108.78]
    assert res["totals_by_currency"] == {"INR": round(71407.09 + 37215.78 + 12450.0, 2)}
    assert res["emails_considered"] == 7 and res["distinct_items"] == 3
    assert "note_on_amounts" in res


def test_merge_without_due_date_uses_amount_window():
    from datetime import date
    from decimal import Decimal

    from email_agent.agent.tools import merge_repeats

    def item(eid, d, amount):
        return {"email_id": eid, "date": date.fromisoformat(d), "merchant": "Netflix", "amount": Decimal(amount),
                "currency": "USD", "due_date": None, "subject": "s"}

    merged = merge_repeats([item(1, "2026-09-01", "15.49"), item(2, "2026-09-05", "15.49"),
                            item(3, "2026-10-01", "15.49")], bills=False)
    assert [m["email_ids"] for m in merged] == [[1, 2], [3]]  # monthly charges stay separate


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
