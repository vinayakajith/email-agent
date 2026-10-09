"""Deterministic stand-ins for OpenAI and Voyage so tests run offline."""

from email_agent.ingest.enrich import EmailFacts


def _facts(**over) -> EmailFacts:
    base = dict(
        category="other", priority=1, needs_action=False, action_summary=None, deadline=None, amount=None,
        amount_type=None, currency=None, merchant=None, is_recurring=False, billing_period=None,
        due_date=None, is_credit_card_bill=False, summary="",
    )
    return EmailFacts(**{**base, **over})


class FakeEnricher:
    model = "fake-enrich"

    def __init__(self, owner: str = "me@gmail.com"):
        self.calls = 0

    async def extract(self, email: dict) -> EmailFacts:
        self.calls += 1
        subject = email["subject"].lower()
        if email["from_addr"] == "andre@example.fr":
            raise RuntimeError("model exploded")
        if "credit card" in subject:
            return _facts(category="bill", priority=3, needs_action=True, action_summary="Pay HDFC card",
                          amount=12450.0, amount_type="amount_due", currency="inr", merchant="HDFC Bank",
                          due_date="2026-10-20", deadline="2026-10-20", is_credit_card_bill=True,
                          summary="HDFC credit card statement")
        if "membership" in subject:
            return _facts(category="subscription", priority=0, amount=15.49, amount_type="charged",
                          currency="USD", merchant="Netflix", is_recurring=True, billing_period="monthly",
                          summary="Netflix renewal")
        if "aws" in subject:
            return _facts(category="bill", priority=2, needs_action=True, amount=42.17, amount_type="amount_due",
                          currency="USD", merchant="AWS", is_recurring=True, billing_period="monthly",
                          due_date="not a date", summary="AWS invoice")
        return _facts(category="work", priority=2, needs_action=True, action_summary="Send final deck",
                      summary="Priya asks for the final deck")
