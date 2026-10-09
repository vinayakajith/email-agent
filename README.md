# email-agent

An autonomous research agent over your own Gmail. Ask questions in plain English, such as
"what deserves my attention?", "which subscriptions should I cancel?" or "total of my credit
card bills this month". It researches your mailbox and answers with citations. It can also
draft email, which is sent only after you approve it.

**Core stack:** Gmail over IMAP/SMTP · Postgres (ParadeDB: pgvector + pg_search BM25) ·
Voyage `voyage-4-large` embeddings · Voyage `rerank-3` · OpenAI GPT-6-class models.
Retrieval is hybrid: BM25 + vector → reciprocal rank fusion → rerank.

## Run it

```bash
docker compose up -d                     # Postgres on localhost:5434
uv sync
(cd web && npm install && npm run build) # the web UI
uv run email-agent app                   # opens http://127.0.0.1:8765 — onboarding, then chat
```

`email-agent app` serves the UI and API on 127.0.0.1 only, starts the background worker if
none is running, and stops both on Ctrl-C. Onboarding asks for your OpenAI and Voyage keys
(checked, then stored in the OS keychain), connects Gmail with an app password (turn on 2-Step
Verification, create one at https://myaccount.google.com/apppasswords, enable IMAP), asks a
little about you, and runs the first read of your mail.

### The interface

- **Streaming chat.** Answers stream token by token through a smoother that reveals whole words
  at an even pace, so bursty network delivery never looks jittery.
- **Thinking you can see.** While the agent works, a glimmering label says what it is actually
  doing ("Searching your mail for “netflix”", "Adding up your bills"). Finished answers keep a
  "Thought for 6s · …" disclosure with the reasoning summary.
- **Grounded answers.** Citations are numbered chips; hover to see the email. A sources row
  closes each answer.
- **Your approval to send.** Drafts appear as a card with Send and Discard. Nothing goes out
  otherwise.
- **Pebble.** A small stone-like character above the chat. It reacts to what you type (money,
  urgency, greetings, writing, memory) and to what the agent is doing (scanning while it
  searches, counting while it adds up, looking at the card while it waits for you), and follows
  your pointer when idle. It respects reduced-motion settings.

The CLI below remains for development and administration.

## Setup without the UI

```bash
uv run email-agent db migrate
uv run email-agent setup        # API keys → Gmail → your profile → first ingest (resumable)
uv run email-agent worker       # keep running: new mail, retries, nightly dreaming
```

`setup` checks each API key with a cheap call and stores it in the OS keychain. Environment
variables (`.env`) still take precedence. Your profile (name, timezone, one line about you) is
the only thing that goes into every conversation's system prompt; edit it with
`email-agent profile`.

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

## Memory and dreaming

| Kind | Written by | When |
|---|---|---|
| Your notes | the chat agent | only when you explicitly ask: "remember…", "forget…" |
| Insights | the nightly dream | after 3 am (your timezone), only if you chatted since the last dream |

The agent reads both through a `get_user_context` tool when personal context helps. Each turn
gets one line saying how many notes exist; nothing else is added to the prompt.

**Dreaming** consolidates your recent conversations into insights: it merges duplicates, lets
newer information win, and drops trivia and secrets. Guards:
- **Poisoning guard:** it reads only your messages and the assistant's answers, never tool
  output or raw email content, so instructions hidden in an email can't become memory.
- **Ownership:** it only edits its own insights, never your notes.
- **Shrink guard:** it refuses a result that would shrink the insights by more than half.
- **Reversible:** every change is versioned.

```bash
uv run email-agent memory list          # your notes + insights
uv run email-agent memory history       # every change, with the dream job that made it
uv run email-agent memory forget <topic>
uv run email-agent dream --now          # consolidate now (no-op if nothing new)
uv run email-agent memory rollback <job>
```

## Health and logs

```bash
uv run email-agent status               # one line for humans, e.g. "Up to date · synced 2 min ago"
uv run email-agent health [--json]      # developer report; exit code 1 if anything fails
uv run email-agent doctor               # health + live OpenAI/Voyage checks
uv run email-agent logs -f              # tail logs/email-agent.log (rotating)
```

Health derives everything from state the app already records (worker heartbeats, sync times,
job rows, unprocessed mail, dream backlog, pending drafts). Per-turn and per-job detail lives
in `agent_runs` and `ingest_jobs`.

## Architecture

```
 clients (CLI today; web / mobile later)
   │  enqueue jobs, read progress      │  Agent.run(): stream of events
   ▼                                   ▼
 ┌──────────────────────── Postgres (only stateful service) ─────────────────────────┐
 │ ingest_jobs (queue) · workers · emails · email_facts · chunks (BM25 + HNSW)        │
 │ conversations · conversation_items · drafts · agent_runs                           │
 │ user_profile · memories · memory_versions                                          │
 └────────────────────────────────────────────────────────────────────────────────────┘
   ▲  claim (FOR UPDATE SKIP LOCKED), heartbeat, progress
   │
 worker process(es): IMAP IDLE watchers → enqueue sync · runners → fetch → extract → embed
                     dream scheduler (3 am local, only with new messages) → dream job
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
| Agent | Hand-written ReAct loop on the OpenAI Responses API with tools `search_emails`, `filter_emails`, `get_email`, `get_thread`, `aggregate_spend`, `list_subscriptions`, `draft_email`, `send_email` (approval request only), `get_user_context`, `save_memory`, `delete_memory` |
| Dream | Nightly worker job: new user/assistant messages + current insights → one structured call → versioned insight updates |

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
uv run pytest                           # backend: fake OpenAI/Voyage/IMAP, real Postgres (email_agent_test DB)
(cd web && npm test)                    # frontend: SSE parser, smoother, Pebble mood engine
uv run email-agent app --no-browser     # API on :8765, then in another terminal:
(cd web && npm run dev)                 # UI with hot reload on :5173 (proxies /api); #pebbles shows every mood
node scripts/drive.mjs <dir> "ask:<question>"   # drive the running app in Chrome and save screenshots
uv run email-agent search "netflix" --mode bm25|vector|hybrid [--no-rerank]
uv run email-agent eval-retrieval       # recall@10 / MRR per mode; see evals/retrieval_golden.example.yaml
```

The test database is created once with:
`docker compose exec db psql -U email_agent -c "CREATE DATABASE email_agent_test"`.
