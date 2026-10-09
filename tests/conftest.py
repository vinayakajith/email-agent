import os

# Tests must not depend on the developer's .env (environment variables take precedence).
os.environ.update({
    "OPENAI_MODEL": "gpt-6-luna", "OPENAI_ENRICH_MODEL": "gpt-6-luna", "AGENT_REASONING_EFFORT": "medium",
    # Never read the developer's keychain from tests.
    "OPENAI_API_KEY": "test-openai-key", "VOYAGE_API_KEY": "test-voyage-key", "LOG_FILE": "off",
})
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://email_agent:email_agent@localhost:5434/email_agent_test"
)

import psycopg  # noqa: E402
import pytest  # noqa: E402

from email_agent.config import get_settings  # noqa: E402
from email_agent.db.migrate import migrate  # noqa: E402

TABLES = (
    "agent_runs, drafts, conversation_items, conversations, chunks, email_facts, emails, ingest_jobs, sync_state, "
    "workers, memory_versions, memories, user_profile, accounts"
)


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
    from email_agent.ingest import embed, enrich
    from email_agent.search import hybrid
    from tests.fakes import FakeEmbedder, FakeEnricher, FakeReranker

    monkeypatch.setattr(enrich, "Enricher", FakeEnricher)
    monkeypatch.setattr(embed, "Embedder", FakeEmbedder)
    monkeypatch.setattr(hybrid, "_embedder", FakeEmbedder())
    monkeypatch.setattr(hybrid, "_reranker", FakeReranker())


@pytest.fixture
async def ingested(account, monkeypatch):
    """The fixture mailbox, fully ingested through every stage with fake models."""
    from email_agent.ingest import pipeline
    from tests.test_fetch import FakeIMAP

    monkeypatch.setattr(pipeline, "GmailIMAP", FakeIMAP)
    monkeypatch.setattr("email_agent.accounts.get_password", lambda email: "secret")
    job = await run_job_now(account, "full")
    assert job["status"] == "done", job["error"]
    return account


async def run_job_now(account, mode: str, since=None) -> dict:
    """Enqueue a job and run it in-process through the real runner; returns the final job row."""
    from email_agent import jobs
    from email_agent.worker.runner import run_now

    job_id = await jobs.enqueue(account.id, mode, since)
    await run_now(job_id, "test-worker")
    return await jobs.get(job_id)
