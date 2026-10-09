"""Split an email into header-prefixed chunks for embedding and BM25.

Every chunk repeats From/To/Date/Subject so it is self-contained for both retrievers and
for the reranker, which only ever sees one chunk per email.
"""

from dataclasses import dataclass

import tiktoken

CHUNK_TOKENS = 600
OVERLAP_TOKENS = 80
MAX_CHUNKS = 24  # caps cost on giant newsletters; the opening of an email carries the signal
_enc = tiktoken.get_encoding("o200k_base")


@dataclass
class Chunk:
    index: int
    content: str
    token_count: int


def count_tokens(text: str) -> int:
    return len(_enc.encode(text, disallowed_special=()))


def email_header(e: dict) -> str:
    lines = [
        f"From: {e['from_name']} <{e['from_addr']}>",
        f"To: {', '.join(e['to_addrs'][:5])}",
        f"Date: {e['sent_at']:%Y-%m-%d}" if e.get("sent_at") else "Date: unknown",
        f"Subject: {e['subject']}",
    ]
    if e.get("summary"):
        lines.append(f"Summary: {e['summary']}")
    return "\n".join(lines)


def _windows(tokens: list[int]) -> list[list[int]]:
    step = CHUNK_TOKENS - OVERLAP_TOKENS
    return [tokens[i : i + CHUNK_TOKENS] for i in range(0, max(len(tokens) - OVERLAP_TOKENS, 1), step)]


def chunk_email(e: dict) -> list[Chunk]:
    header = email_header(e)
    body = e.get("body_text") or ""
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    # Pack whole paragraphs; only hard-split paragraphs that alone exceed the budget.
    for para in (p for p in body.split("\n\n") if p.strip()):
        n = count_tokens(para)
        if n > CHUNK_TOKENS:
            if current:
                pieces.append("\n\n".join(current))
                current, current_tokens = [], 0
            pieces.extend(_enc.decode(w) for w in _windows(_enc.encode(para, disallowed_special=())))
            continue
        if current_tokens + n > CHUNK_TOKENS and current:
            pieces.append("\n\n".join(current))
            # Carry the last paragraph forward as overlap when it is small enough.
            tail = current[-1]
            current, current_tokens = ([tail], count_tokens(tail)) if count_tokens(tail) <= OVERLAP_TOKENS else ([], 0)
        current.append(para)
        current_tokens += n
    if current:
        pieces.append("\n\n".join(current))
    if not pieces:
        pieces = [""]
    chunks = []
    for i, piece in enumerate(pieces[:MAX_CHUNKS]):
        content = f"{header}\n\n{piece}".strip()
        chunks.append(Chunk(i, content, count_tokens(content)))
    return chunks
