CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_search;

CREATE TABLE accounts (
    id           bigserial PRIMARY KEY,
    email        text NOT NULL UNIQUE,
    auth_method  text NOT NULL DEFAULT 'app_password' CHECK (auth_method IN ('app_password', 'oauth')),
    imap_host    text NOT NULL,
    smtp_host    text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sync_state (
    account_id     bigint NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    folder         text NOT NULL,
    uidvalidity    bigint NOT NULL,
    last_uid       bigint NOT NULL DEFAULT 0,
    -- Earliest date covered by ingestion; NULL means the full mailbox.
    since_date     date,
    last_synced_at timestamptz,
    PRIMARY KEY (account_id, folder)
);

CREATE TABLE ingest_jobs (
    id          bigserial PRIMARY KEY,
    account_id  bigint NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    mode        text NOT NULL CHECK (mode IN ('full', 'since', 'sync')),
    since_date  date,
    status      text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    fetched     int NOT NULL DEFAULT 0,
    enriched    int NOT NULL DEFAULT 0,
    embedded    int NOT NULL DEFAULT 0,
    error       text,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE emails (
    id               bigserial PRIMARY KEY,
    account_id       bigint NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    gm_msgid         bigint NOT NULL,
    gm_thrid         bigint,
    uid              bigint,
    message_id       text,
    in_reply_to      text,
    "references"     text,
    subject          text NOT NULL DEFAULT '',
    from_name        text NOT NULL DEFAULT '',
    from_addr        text NOT NULL DEFAULT '',
    to_addrs         text[] NOT NULL DEFAULT '{}',
    cc_addrs         text[] NOT NULL DEFAULT '{}',
    sent_at          timestamptz,
    labels           text[] NOT NULL DEFAULT '{}',
    is_unread        boolean NOT NULL DEFAULT false,
    is_starred       boolean NOT NULL DEFAULT false,
    has_attachments  boolean NOT NULL DEFAULT false,
    attachments      jsonb NOT NULL DEFAULT '[]',
    body_text        text NOT NULL DEFAULT '',
    snippet          text NOT NULL DEFAULT '',
    list_unsubscribe text,
    size_bytes       int,
    stage            text NOT NULL DEFAULT 'fetched' CHECK (stage IN ('fetched', 'enriched', 'embedded')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, gm_msgid)
);
CREATE INDEX emails_sent_at_idx ON emails (sent_at DESC);
CREATE INDEX emails_from_addr_idx ON emails (from_addr);
CREATE INDEX emails_thread_idx ON emails (gm_thrid);
CREATE INDEX emails_stage_idx ON emails (stage);
CREATE INDEX emails_labels_idx ON emails USING gin (labels);

CREATE TABLE email_facts (
    email_id            bigint PRIMARY KEY REFERENCES emails(id) ON DELETE CASCADE,
    category            text NOT NULL,
    priority            smallint NOT NULL DEFAULT 0,
    needs_action        boolean NOT NULL DEFAULT false,
    action_summary      text,
    deadline            date,
    amount              numeric(14, 2),
    currency            text,
    merchant            text,
    is_recurring        boolean NOT NULL DEFAULT false,
    billing_period      text,
    due_date            date,
    is_credit_card_bill boolean NOT NULL DEFAULT false,
    summary             text,
    model               text NOT NULL,
    extracted_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX email_facts_category_idx ON email_facts (category);
CREATE INDEX email_facts_action_idx ON email_facts (needs_action, priority DESC);

CREATE TABLE chunks (
    id          bigserial PRIMARY KEY,
    email_id    bigint NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    chunk_index int NOT NULL,
    content     text NOT NULL,
    token_count int NOT NULL,
    -- Copied from emails so filtered BM25 and ANN queries avoid a join.
    sent_at     timestamptz,
    from_addr   text NOT NULL DEFAULT '',
    category    text,
    embedding   vector(1024) NOT NULL,
    UNIQUE (email_id, chunk_index)
);
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_bm25_idx ON chunks USING bm25 (id, content, from_addr, category, sent_at);

CREATE TABLE drafts (
    id                   bigserial PRIMARY KEY,
    account_id           bigint NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    to_addrs             text[] NOT NULL,
    cc_addrs             text[] NOT NULL DEFAULT '{}',
    subject              text NOT NULL,
    body                 text NOT NULL,
    in_reply_to_email_id bigint REFERENCES emails(id) ON DELETE SET NULL,
    status               text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'discarded')),
    smtp_message_id      text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    sent_at              timestamptz
);

CREATE TABLE agent_runs (
    id              bigserial PRIMARY KEY,
    conversation_id uuid NOT NULL,
    question        text NOT NULL,
    events          jsonb NOT NULL DEFAULT '[]',
    answer          text,
    input_tokens    int,
    output_tokens   int,
    latency_ms      int,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_runs_conversation_idx ON agent_runs (conversation_id, created_at);
