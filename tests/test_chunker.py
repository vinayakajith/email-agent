from datetime import datetime, timezone

from email_agent.ingest.chunker import CHUNK_TOKENS, MAX_CHUNKS, chunk_email, count_tokens


def email(body: str) -> dict:
    return {
        "from_name": "Ann", "from_addr": "ann@x.com", "to_addrs": ["me@gmail.com"],
        "sent_at": datetime(2026, 10, 1, tzinfo=timezone.utc), "subject": "Hello", "body_text": body,
        "summary": "Ann says hello",
    }


def test_short_email_is_one_chunk_with_header():
    chunks = chunk_email(email("Hi there.\n\nSee you soon."))
    assert len(chunks) == 1
    c = chunks[0].content
    assert c.startswith("From: Ann <ann@x.com>\nTo: me@gmail.com\nDate: 2026-10-01\nSubject: Hello\nSummary: Ann says hello")
    assert "See you soon." in c


def test_empty_body_still_yields_header_chunk():
    chunks = chunk_email(email(""))
    assert len(chunks) == 1 and "Subject: Hello" in chunks[0].content


def test_long_email_respects_budget_and_cap():
    paragraphs = [f"Paragraph {i} " + "word " * 120 for i in range(60)]
    chunks = chunk_email(email("\n\n".join(paragraphs)))
    header_tokens = count_tokens(chunk_email(email(""))[0].content)
    assert 1 < len(chunks) <= MAX_CHUNKS
    assert all(c.token_count <= CHUNK_TOKENS + header_tokens + 5 for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_giant_paragraph_is_hard_split():
    chunks = chunk_email(email("token " * 3000))
    assert len(chunks) >= 5
