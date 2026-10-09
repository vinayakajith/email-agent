"""Send mail through Gmail SMTP. Gmail files sent messages in Sent automatically."""

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def build_message(
    sender: str,
    to: list[str],
    cc: list[str],
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@", 1)[1])
    if in_reply_to:
        # Both headers are needed for Gmail and other clients to thread the reply.
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    msg.set_content(body)
    return msg


def send_message(host: str, email: str, password: str, msg: EmailMessage) -> str:
    with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=60) as smtp:
        smtp.login(email, password)
        smtp.send_message(msg)
    return msg["Message-ID"]
