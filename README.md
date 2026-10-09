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
