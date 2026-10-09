"""Gmail account connection: validate IMAP + SMTP login and store the app password.

The secret goes to the OS keychain; the database only records the account. A future UI
calls `connect()` exactly as the CLI wizard does.
"""

import asyncio
import smtplib
import ssl
from dataclasses import dataclass

import keyring
from imapclient import IMAPClient

from email_agent.config import GMAIL_IMAP_HOST, GMAIL_SMTP_HOST, KEYRING_SERVICE, get_settings
from email_agent.db.pool import connection


class AccountError(Exception):
    pass


@dataclass
class Account:
    id: int
    email: str
    imap_host: str
    smtp_host: str
    auth_method: str

    @property
    def password(self) -> str:
        return get_password(self.email)


def normalize_app_password(raw: str) -> str:
    # Google displays app passwords as "abcd efgh ijkl mnop".
    return "".join(raw.split())


def check_imap(email: str, password: str, host: str = GMAIL_IMAP_HOST) -> str:
    """Log in over IMAP and return the All Mail folder name."""
    from email_agent.ingest.imap_client import find_all_mail

    try:
        with IMAPClient(host, ssl=True, timeout=30) as client:
            client.login(email, password)
            return find_all_mail(client)
    except IMAPClient.Error as e:
        raise AccountError(
            f"IMAP login failed: {e}. Check the app password and that IMAP is enabled in Gmail settings."
        ) from e


def check_smtp(email: str, password: str, host: str = GMAIL_SMTP_HOST) -> None:
    try:
        with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(email, password)
    except smtplib.SMTPException as e:
        raise AccountError(f"SMTP login failed: {e}") from e


def get_password(email: str) -> str:
    try:
        secret = keyring.get_password(KEYRING_SERVICE, email)
    except Exception:
        secret = None
    if secret:
        return secret
    s = get_settings()
    if s.gmail_app_password and s.gmail_address.lower() == email.lower():
        return normalize_app_password(s.gmail_app_password)
    raise AccountError(f"No stored password for {email}; run `email-agent connect`.")


async def connect(email: str, app_password: str) -> Account:
    """Validate credentials against Gmail, store the secret, and upsert the account row."""
    email = email.strip().lower()
    password = normalize_app_password(app_password)
    # Network logins are blocking; keep them off the event loop (the API server shares it).
    await asyncio.to_thread(check_imap, email, password)
    await asyncio.to_thread(check_smtp, email, password)
    try:
        await asyncio.to_thread(keyring.set_password, KEYRING_SERVICE, email, password)
    except Exception as e:
        raise AccountError(
            f"Could not save to the keychain ({e}). Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env instead."
        ) from e
    async with connection() as conn:
        row = await (
            await conn.execute(
                """
                INSERT INTO accounts (email, auth_method, imap_host, smtp_host)
                VALUES (%s, 'app_password', %s, %s)
                ON CONFLICT (email) DO UPDATE SET imap_host = EXCLUDED.imap_host
                RETURNING id, email, imap_host, smtp_host, auth_method
                """,
                (email, GMAIL_IMAP_HOST, GMAIL_SMTP_HOST),
            )
        ).fetchone()
    return Account(**row)


async def get_account(email: str | None = None) -> Account:
    """Return the named account, or the only connected one."""
    async with connection() as conn:
        if email:
            cur = await conn.execute(
                "SELECT id, email, imap_host, smtp_host, auth_method FROM accounts WHERE email = %s",
                (email.lower(),),
            )
        else:
            cur = await conn.execute(
                "SELECT id, email, imap_host, smtp_host, auth_method FROM accounts ORDER BY id"
            )
        rows = await cur.fetchall()
    if not rows:
        raise AccountError("No Gmail account connected; run `email-agent connect`.")
    if len(rows) > 1 and not email:
        raise AccountError("Several accounts connected; pass --account.")
    return Account(**rows[0])


async def get_account_by_id(account_id: int) -> Account:
    async with connection() as conn:
        row = await (await conn.execute(
            "SELECT id, email, imap_host, smtp_host, auth_method FROM accounts WHERE id = %s", (account_id,)
        )).fetchone()
    if row is None:
        raise AccountError(f"Account {account_id} no longer exists.")
    return Account(**row)
