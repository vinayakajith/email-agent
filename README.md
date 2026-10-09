# email-agent

An autonomous research agent over your own Gmail. Ask questions in plain English —
"what deserves my attention?", "which subscriptions should I cancel?", "total of my credit
card bills this month" — and it researches your mailbox and answers with citations. It can
also draft and (after you confirm) send email.

**Core stack (v1):** Gmail over IMAP/SMTP · Postgres (ParadeDB: pgvector + pg_search BM25) ·
Voyage `voyage-4-large` embeddings · Voyage `rerank-3` · OpenAI for the ReAct agent.
Retrieval is hybrid: BM25 + vector → reciprocal rank fusion → rerank.

## Setup

```bash
docker compose up -d            # Postgres on localhost:5434
cp .env.example .env            # add OPENAI_API_KEY and VOYAGE_API_KEY
uv sync
uv run email-agent db migrate
uv run email-agent doctor       # every row should be green
```

## Connect Gmail

Turn on 2-Step Verification, create an app password at
https://myaccount.google.com/apppasswords, and make sure IMAP is enabled in Gmail settings.

```bash
uv run email-agent connect      # tests IMAP + SMTP, stores the password in the macOS Keychain
```

## Ingest

```bash
uv run email-agent ingest               # asks: 30 days / 90 days / 1 year / custom / full mailbox
uv run email-agent ingest --since 2026-01-01
uv run email-agent ingest --full
uv run email-agent status               # emails per stage: fetched -> enriched -> embedded
uv run email-agent resume               # finish extraction/embedding after an interruption
uv run email-agent sync                 # fetch new mail since the last run
```

Ingestion never marks mail as read (the folder is opened read-only). Each stage is
idempotent, so re-running an interrupted ingest continues where it stopped, and choosing a
wider time frame later only downloads what is missing.

## Ask

```bash
uv run email-agent ask "what deserves my attention this week?"
uv run email-agent ask "total of my credit card bills this month"
uv run email-agent ask "which subscriptions should I consider cancelling?"
uv run email-agent chat                 # multi-turn; can draft replies and send after you approve
```

The agent shows its trace (thought → tool call → observation) and cites emails as `[#id]`.
Sending always shows the full draft and waits for your yes. Every run is stored in
`agent_runs` for inspection.

## How it works

| Stage | What happens |
|---|---|
| Fetch | IMAP `[Gmail]/All Mail`, `BODY.PEEK[]`, Gmail ids/threads/labels; HTML → text, quoted replies stripped |
| Enrich | OpenAI structured outputs extract category, priority, needs-action, amounts, merchant, due dates, recurrence |
| Embed | Header-prefixed ~600-token chunks → `voyage-4-large` (1024-d) into pgvector HNSW + pg_search BM25 |
| Search | BM25 top-50 ∥ vector top-50 → reciprocal rank fusion → best chunk per email → `rerank-3` |
| Agent | ReAct loop on the OpenAI Responses API: `search_emails`, `filter_emails`, `get_email`, `get_thread`, `aggregate_spend`, `list_subscriptions`, `draft_email`, `send_email` |

Totals and triage use the extracted facts via SQL, so "how much did I spend" is an exact sum
with line items, not the model adding numbers from snippets.

## Development

```bash
uv run pytest                           # offline: fake OpenAI/Voyage/IMAP, real Postgres (email_agent_test DB)
uv run email-agent search "netflix" --mode bm25|vector|hybrid [--no-rerank]
uv run email-agent eval-retrieval       # recall@10 / MRR per mode; see evals/retrieval_golden.example.yaml
```

The test database is created once with:
`docker compose exec db psql -U email_agent -c "CREATE DATABASE email_agent_test"`.
