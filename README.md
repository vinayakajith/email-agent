# email-agent

An autonomous research agent over your own Gmail. Ask questions in plain English, such as
"what deserves my attention?", "which subscriptions should I cancel?" or "total of my credit
card bills this month". It researches your mailbox and answers with citations. It can also
draft email, which is sent only after you approve it.

**Core stack:** Gmail over IMAP/SMTP · Postgres (ParadeDB: pgvector + pg_search BM25) ·
Voyage `voyage-4-large` embeddings · Voyage `rerank-3` · OpenAI GPT-6-class models.
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

## Ingest and keep up to date

```bash
uv run email-agent ingest               # asks: 30 days / 90 days / 1 year / custom / full mailbox
uv run email-agent worker               # keep running: picks up new mail within seconds
uv run email-agent status               # stages, worker state, queued/running jobs
```

`ingest`, `sync`, `resume` and `reenrich` only **queue a job** and show its progress. The
worker runs it. If no worker is running, the command runs the job itself, through the same
code path.

The worker:
- holds an IMAP IDLE connection per account and queues a `sync` when new mail arrives;
- polls every 10 minutes as a fallback, which also refreshes read/starred state;
- runs queued jobs.

Ctrl-C stops it cleanly, and any job it was running goes back to the queue.

Ingestion never marks mail as read (the folder is opened read-only). Every stage is
idempotent, so an interrupted job simply runs again. Choosing a wider time frame later only
downloads what is missing.

## Ask

```bash
uv run email-agent ask "what deserves my attention this week?"
uv run email-agent chat                 # multi-turn; saved server-side
uv run email-agent chat --list          # recent conversations
uv run email-agent chat --resume <id>   # continue one (from any machine using this database)
```

The agent shows its trace (thought → tool call → observation) and cites emails as `[#id]`.
Each turn also tells it how fresh the index is, so it says so when recent mail is still
being processed.

**Sending is your action, never the agent's.** The agent can draft an email and request
approval. You approve or reject it, the email is sent only on approval, and the outcome is
added to the conversation.

## Architecture

```
 clients (CLI today; web / mobile later)
   │  enqueue jobs, read progress      │  Agent.run(): stream of events
   ▼                                   ▼
 ┌──────────────────────── Postgres (only stateful service) ─────────────────────────┐
 │ ingest_jobs (queue) · workers · emails · email_facts · chunks (BM25 + HNSW)        │
 │ conversations · conversation_items · drafts · agent_runs                           │
 └────────────────────────────────────────────────────────────────────────────────────┘
   ▲  claim (FOR UPDATE SKIP LOCKED), heartbeat, progress
   │
 worker process(es): IMAP IDLE watchers → enqueue sync · runners → fetch → extract → embed
```

- **Chat is isolated from ingestion.** The worker is a separate process. Chunks and facts are
  replaced atomically per batch, so chat never sees a half-processed email. Model calls retry
  through rate limits caused by bulk work.
- **Multi-worker and multi-account safe by construction.** Partial unique indexes allow only
  one queued job per (account, mode) and one running job per account. A job whose worker
  stops heartbeating is requeued. Every query is scoped to an `account_id`.
- **Prompt caching.** Requests are laid out stable-first (tools → static system prompt →
  append-only history) with explicit cache breakpoints. Conversations are stored append-only,
  so a resumed conversation keeps its cache.

| Stage | What happens |
|---|---|
| Fetch | IMAP `[Gmail]/All Mail`, `BODY.PEEK[]`, Gmail ids/threads/labels; HTML → text, quoted replies stripped |
| Extract | OpenAI structured outputs: category, priority, needs-action, amounts (total vs minimum due), merchant, due dates, recurrence. Versioned; `reenrich` upgrades old facts |
| Embed | Header-prefixed ~600-token chunks → `voyage-4-large` (1024-d) into pgvector HNSW + pg_search BM25 |
| Search | BM25 top-50 ∥ vector top-50 → reciprocal rank fusion → best chunk per email → `rerank-3` |
| Agent | Hand-written ReAct loop on the OpenAI Responses API with tools `search_emails`, `filter_emails`, `get_email`, `get_thread`, `aggregate_spend`, `list_subscriptions`, `draft_email`, `send_email` (approval request only) |

Totals and triage use the extracted facts via SQL. Repeated reminders for the same bill are
merged by due date, so "how much do I owe" is an exact sum with line items.

### Why no LangGraph (yet)

What we'd use it for is small to own here:
- stored conversation state is one append-only table;
- pausing for human approval is the stored draft approval;
- streaming already comes from `Agent.run`'s event stream.

A framework layer would also sit between us and Responses API features we rely on
(encrypted reasoning items, prompt-cache breakpoints and options). Revisit it if the product
grows several specialised agents with hand-offs, or long workflows that run over days. Clients
only see `Agent.run`'s event stream, so the internals can change without touching them.

### Not built yet (deliberately)

- API server, authentication, tenants and row-level security.
- OAuth with Gmail push notifications (these replace the IDLE watcher at cloud scale; the
  queue and pipeline stay the same).
- A cloud credential store (the keychain sits behind `accounts.get_password`).
- Trimming old tool results from long conversations.

## Development

```bash
uv run pytest                           # offline: fake OpenAI/Voyage/IMAP, real Postgres (email_agent_test DB)
uv run email-agent search "netflix" --mode bm25|vector|hybrid [--no-rerank]
uv run email-agent eval-retrieval       # recall@10 / MRR per mode; see evals/retrieval_golden.example.yaml
```

The test database is created once with:
`docker compose exec db psql -U email_agent -c "CREATE DATABASE email_agent_test"`.
