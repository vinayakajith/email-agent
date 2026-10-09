-- Basic onboarding info; rendered into the (static, cached) system prompt.
CREATE TABLE user_profile (
    account_id   bigint PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    display_name text NOT NULL,
    timezone     text NOT NULL,
    about        text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Memory about the user. Two owners, never mixed:
--   user  = saved by the chat agent only when the user explicitly asked;
--   dream = the nightly consolidation's inferred picture of the user.
CREATE TABLE memories (
    id         bigserial PRIMARY KEY,
    account_id bigint NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    topic      text NOT NULL,
    content    text NOT NULL,
    source     text NOT NULL CHECK (source IN ('user', 'dream')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, topic)
);

-- Every memory change, for audit and rollback.
CREATE TABLE memory_versions (
    id              bigserial PRIMARY KEY,
    account_id      bigint NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    topic           text NOT NULL,
    op              text NOT NULL CHECK (op IN ('create', 'update', 'delete')),
    old_content     text,
    new_content     text,
    source          text NOT NULL,
    conversation_id uuid,
    job_id          bigint,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memory_versions_account_idx ON memory_versions (account_id, id DESC);
CREATE INDEX memory_versions_job_idx ON memory_versions (job_id) WHERE job_id IS NOT NULL;

-- Jobs record a small structured result (sync counts, dream summary, tokens).
ALTER TABLE ingest_jobs ADD COLUMN result jsonb;
ALTER TABLE ingest_jobs DROP CONSTRAINT ingest_jobs_mode_check;
ALTER TABLE ingest_jobs ADD CONSTRAINT ingest_jobs_mode_check
    CHECK (mode IN ('full', 'since', 'sync', 'process', 'reenrich', 'dream'));
