"""Thin synchronous wrapper over Gmail IMAP. Callers run it via asyncio.to_thread."""

from dataclasses import dataclass
from datetime import date, datetime

from imapclient import IMAPClient
from imapclient.imapclient import ALL

from email_agent.config import GMAIL_ALL_MAIL

FETCH_ITEMS = ["BODY.PEEK[]", "X-GM-MSGID", "X-GM-THRID", "X-GM-LABELS", "FLAGS", "RFC822.SIZE", "INTERNALDATE"]


@dataclass
class RawMessage:
    uid: int
    gm_msgid: int
    gm_thrid: int
    labels: list[str]
    flags: list[str]
    size: int
    internal_date: datetime | None
    raw: bytes


def _s(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


def find_all_mail(client: IMAPClient) -> str:
    # The folder name is localized in non-English accounts; SPECIAL-USE \All is reliable.
    return client.find_special_folder(ALL) or GMAIL_ALL_MAIL


class GmailIMAP:
    def __init__(self, host: str, email: str, password: str):
        self.host, self.email, self.password = host, email, password
        self.client: IMAPClient | None = None
        self.uidvalidity: int | None = None

    def __enter__(self) -> "GmailIMAP":
        self.client = IMAPClient(self.host, ssl=True, timeout=120)
        self.client.login(self.email, self.password)
        # readonly guarantees nothing is ever marked as read.
        info = self.client.select_folder(find_all_mail(self.client), readonly=True)
        self.uidvalidity = int(info[b"UIDVALIDITY"])
        return self

    def __exit__(self, *exc) -> None:
        if self.client is not None:
            try:
                self.client.logout()
            except Exception:
                pass

    def search(self, since: date | None = None, min_uid: int | None = None) -> list[int]:
        criteria: list = []
        if since is not None:
            criteria += ["SINCE", since]
        if min_uid is not None:
            criteria += ["UID", f"{min_uid}:*"]
        uids = self.client.search(criteria or ["ALL"])
        # "UID n:*" always matches the highest UID even if it is below n.
        if min_uid is not None:
            uids = [u for u in uids if u >= min_uid]
        return sorted(uids)

    def gm_msgids(self, uids: list[int], chunk: int = 2000) -> dict[int, int]:
        """Cheap metadata fetch: uid -> X-GM-MSGID, used to skip already-stored messages."""
        out: dict[int, int] = {}
        for i in range(0, len(uids), chunk):
            for uid, data in self.client.fetch(uids[i : i + chunk], ["X-GM-MSGID"]).items():
                out[uid] = int(data[b"X-GM-MSGID"])
        return out

    def flags_and_labels(self, uids: list[int], chunk: int = 2000) -> dict[int, tuple[list[str], list[str]]]:
        out: dict[int, tuple[list[str], list[str]]] = {}
        for i in range(0, len(uids), chunk):
            for uid, data in self.client.fetch(uids[i : i + chunk], ["FLAGS", "X-GM-LABELS"]).items():
                out[uid] = ([_s(f) for f in data.get(b"FLAGS", ())], [_s(l) for l in data.get(b"X-GM-LABELS", ())])
        return out

    def fetch(self, uids: list[int]) -> list[RawMessage]:
        msgs = []
        for uid, data in self.client.fetch(uids, FETCH_ITEMS).items():
            raw = data.get(b"BODY[]")
            if raw is None:
                continue
            msgs.append(
                RawMessage(
                    uid=uid,
                    gm_msgid=int(data[b"X-GM-MSGID"]),
                    gm_thrid=int(data[b"X-GM-THRID"]),
                    labels=[_s(l) for l in data.get(b"X-GM-LABELS", ())],
                    flags=[_s(f) for f in data.get(b"FLAGS", ())],
                    size=int(data.get(b"RFC822.SIZE", len(raw))),
                    internal_date=data.get(b"INTERNALDATE"),
                    raw=raw,
                )
            )
        return sorted(msgs, key=lambda m: m.uid)
