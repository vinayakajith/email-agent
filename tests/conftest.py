import os

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://email_agent:email_agent@localhost:5434/email_agent_test"
)

import psycopg  # noqa: E402
import pytest  # noqa: E402

from email_agent.config import get_settings  # noqa: E402
from email_agent.db.migrate import migrate  # noqa: E402

TABLES = "agent_runs, drafts, chunks, email_facts, emails, ingest_jobs, sync_state, accounts"


@pytest.fixture(scope="session")
def migrated():
    migrate()


@pytest.fixture
async def db(migrated):
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    yield
    from email_agent.db.pool import close_pool

    await close_pool()


@pytest.fixture
async def account(db):
    from email_agent.accounts import Account
    from email_agent.db.pool import connection

    async with connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO accounts (email, imap_host, smtp_host) VALUES ('me@gmail.com', 'imap', 'smtp') "
                "RETURNING id, email, imap_host, smtp_host, auth_method"
            )
        ).fetchone()
    return Account(**row)


@pytest.fixture(autouse=True)
def offline_models(monkeypatch):
    """Never call OpenAI or Voyage from tests."""
    from email_agent.ingest import enrich
    from tests.fakes import FakeEnricher

    monkeypatch.setattr(enrich, "Enricher", FakeEnricher)
