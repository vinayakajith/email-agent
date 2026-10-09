"""New-mail detection: one IMAP IDLE connection per account, in its own thread.

The watcher's only job is to enqueue a `sync`; the queue collapses bursts into one job and the
runner does the work. A future Gmail push (Pub/Sub) trigger would replace this module only.
"""

import logging
import threading
from collections.abc import Callable

from email_agent.accounts import Account
from email_agent.ingest.imap_client import GmailIMAP

log = logging.getLogger(__name__)

IDLE_SECONDS = 60
BACKOFF_START, BACKOFF_MAX = 5.0, 300.0


def idle_loop(
    account: Account,
    on_new_mail: Callable[[], None],
    stop: threading.Event,
    imap_factory=GmailIMAP,
) -> None:
    """Block until `stop` is set, calling `on_new_mail` whenever the mailbox gains messages."""
    backoff = BACKOFF_START
    while not stop.is_set():
        try:
            with imap_factory(account.imap_host, account.email, account.password) as imap:
                log.info("watching %s (IMAP IDLE)", account.email)
                backoff = BACKOFF_START
                # Anything that arrived while disconnected is picked up by a sync right away.
                on_new_mail()
                while not stop.is_set():
                    if imap.wait_for_changes(IDLE_SECONDS):
                        log.info("new mail for %s", account.email)
                        on_new_mail()
        except Exception as e:
            if stop.is_set():
                break
            log.warning("IDLE connection for %s dropped (%s: %s); reconnecting in %.0fs",
                        account.email, type(e).__name__, e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)


class Watcher:
    def __init__(self, account: Account, on_new_mail: Callable[[], None], imap_factory=GmailIMAP):
        self.account = account
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=idle_loop,
            args=(account, on_new_mail, self.stop_event, imap_factory),
            name=f"idle-{account.email}",
            daemon=True,  # a blocked IDLE read must never keep the process alive on shutdown
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
