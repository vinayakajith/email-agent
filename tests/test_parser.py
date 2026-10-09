from pathlib import Path

from email_agent.ingest.parser import parse_email

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return parse_email((FIXTURES / name).read_bytes())


def test_multipart_alternative_prefers_rich_plain_text():
    e = load("multipart_alt.eml")
    assert e.from_addr == "alerts@hdfcbank.net"
    assert e.from_name == "HDFC Bank"
    assert e.to_addrs == ["me@gmail.com"]
    assert "Total amount due: Rs 12,450.00" in e.body_text
    assert "Payment due date: 20 Oct 2026" in e.body_text
    assert e.sent_at.isoformat() == "2026-10-02T09:15:00+05:30"
    assert e.message_id == "<stmt-123@hdfcbank.net>"
    assert not e.has_attachments


def test_html_only_strips_scripts_and_keeps_links():
    e = load("html_only.eml")
    assert "alert(1)" not in e.body_text
    assert "color:red" not in e.body_text
    assert "$15.49" in e.body_text
    assert "Manage account (https://www.netflix.com/account)" in e.body_text
    assert e.list_unsubscribe == "https://www.netflix.com/unsubscribe?id=abc"


def test_reply_strips_quoted_history():
    e = load("reply_quoted.eml")
    assert e.body_text == "Sounds good, can you send the final version by Friday?"
    assert e.in_reply_to == "<r1@gmail.com>"
    assert e.cc_addrs == ["team@acme.com"]


def test_unknown_charset_does_not_crash():
    e = load("bad_charset.eml")
    assert e.from_name == "André"
    assert "4 euros" in e.body_text


def test_attachment_metadata():
    e = load("attachment.eml")
    assert e.has_attachments
    assert e.attachments[0]["filename"] == "invoice.pdf"
    assert e.attachments[0]["content_type"] == "application/pdf"
    assert "USD 42.17" in e.body_text
    assert "JVBER" not in e.body_text


def test_html_inline_tags_stay_on_one_line():
    from email_agent.ingest.parser import html_to_text

    text = html_to_text("<p>Total due: <b>Rs 1,200</b></p><table><tr><td>Item</td><td>$5</td></tr></table>")
    assert "Total due: Rs 1,200" in text
    assert "Item $5" in text
