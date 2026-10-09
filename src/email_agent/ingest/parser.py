"""RFC 822 bytes -> ParsedEmail with clean text suitable for search and the LLM."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from email_reply_parser import EmailReplyParser
from selectolax.lexbor import LexborHTMLParser

MAX_BODY_CHARS = 100_000
_WS_RE = re.compile(r"[ \t\u00a0\u200b\u200c\u200d\ufeff]+")
_BLANKS_RE = re.compile(r"\n{3,}")
_URL_RE = re.compile(r"<(https?://[^>]+)>")
_MAILTO_RE = re.compile(r"<(mailto:[^>]+)>")


@dataclass
class ParsedEmail:
    message_id: str | None
    in_reply_to: str | None
    references: str | None
    subject: str
    from_name: str
    from_addr: str
    to_addrs: list[str]
    cc_addrs: list[str]
    sent_at: datetime | None
    body_text: str
    snippet: str
    attachments: list[dict] = field(default_factory=list)
    list_unsubscribe: str | None = None

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)


def _clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS_RE.sub("\n\n", text).strip()


_BLOCK_END_RE = re.compile(r"(<br\s*/?>|</(?:p|div|tr|li|h[1-6]|table|blockquote|section|article|header|footer)\s*>)", re.I)
_CELL_END_RE = re.compile(r"(</t[dh]\s*>)", re.I)


def html_to_text(html: str) -> str:
    # Newlines only at block boundaries so inline tags (<b>, <span>) stay on one line.
    html = _BLOCK_END_RE.sub(r"\1\n", html)
    html = _CELL_END_RE.sub(r"\1 ", html)
    tree = LexborHTMLParser(html)
    for tag in tree.css("script, style, head, noscript, svg"):
        tag.decompose()
    # Keep link targets for unsubscribe / payment links the agent may need.
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        label = a.text(strip=True)
        if href.startswith("http") and label and href not in label and len(href) < 300:
            a.replace_with(f"{label} ({href})")
    body = tree.body or tree.root
    return body.text(separator="") if body else ""


def _part_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace")


def _header(msg: EmailMessage, name: str) -> str | None:
    try:
        v = msg.get(name)
    except Exception:
        return None
    return str(v).strip() if v is not None else None


def _addresses(msg: EmailMessage, name: str) -> list[tuple[str, str]]:
    try:
        values = msg.get_all(name, [])
    except Exception:
        return []
    return [(n, a.lower()) for n, a in getaddresses([str(v) for v in values]) if a and "@" in a]


def _sent_at(msg: EmailMessage, fallback: datetime | None) -> datetime | None:
    raw = _header(msg, "Date")
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, IndexError):
            pass
    if fallback is not None and fallback.tzinfo is None:
        fallback = fallback.astimezone()
    return fallback


def _unsubscribe(msg: EmailMessage) -> str | None:
    raw = _header(msg, "List-Unsubscribe")
    if not raw:
        return None
    m = _URL_RE.search(raw) or _MAILTO_RE.search(raw)
    return m.group(1) if m else raw[:500]


def _bodies(msg: EmailMessage) -> tuple[str, str, list[dict]]:
    plain, html, attachments = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition == "attachment" or (filename and not ctype.startswith("text/")):
            payload = part.get_payload(decode=True) or b""
            attachments.append({"filename": filename or "unnamed", "content_type": ctype, "size": len(payload)})
            continue
        if ctype == "text/plain":
            plain.append(_part_text(part))
        elif ctype == "text/html":
            html.append(_part_text(part))
    return "\n".join(plain), "\n".join(html), attachments


def _choose_body(plain: str, html: str) -> str:
    plain_c = _clean(plain)
    if not html:
        return plain_c
    html_c = _clean(html_to_text(html))
    # Marketing mail often ships a stub text/plain ("view this email in your browser").
    if len(plain_c) >= 0.5 * len(html_c) or len(plain_c) >= 2000:
        return plain_c
    return html_c or plain_c


def parse_email(raw: bytes, internal_date: datetime | None = None) -> ParsedEmail:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)
    plain, html, attachments = _bodies(msg)
    body = _choose_body(plain, html)
    in_reply_to = _header(msg, "In-Reply-To")
    if in_reply_to and body:
        # Only strip quoted history on replies; forwards and receipts keep everything.
        stripped = _clean(EmailReplyParser.parse_reply(body))
        if len(stripped) >= 20:
            body = stripped
    body = body[:MAX_BODY_CHARS]

    senders = _addresses(msg, "From")
    from_name, from_addr = senders[0] if senders else ("", "")
    return ParsedEmail(
        message_id=_header(msg, "Message-ID"),
        in_reply_to=in_reply_to,
        references=_header(msg, "References"),
        subject=_clean(_header(msg, "Subject") or "")[:1000],
        from_name=from_name.replace("\x00", ""),
        from_addr=from_addr,
        to_addrs=[a for _, a in _addresses(msg, "To")],
        cc_addrs=[a for _, a in _addresses(msg, "Cc")],
        sent_at=_sent_at(msg, internal_date),
        body_text=body,
        snippet=" ".join(body.split())[:280],
        attachments=attachments,
        list_unsubscribe=_unsubscribe(msg),
    )
