-- ingest_jobs becomes a Postgres job queue: clients enqueue, workers claim with SKIP LOCKED.
ALTER TABLE ingest_jobs DROP CONSTRAINT ingest_jobs_mode_check;
ALTER TABLE ingest_jobs DROP CONSTRAINT ingest_jobs_status_check;
ALTER TABLE ingest_jobs ADD CONSTRAINT ingest_jobs_mode_check
    CHECK (mode IN ('full', 'since', 'sync', 'process', 'reenrich'));
ALTER TABLE ingest_jobs ADD CONSTRAINT ingest_jobs_status_check
    CHECK (status IN ('queued', 'running', 'done', 'failed'));
ALTER TABLE ingest_jobs ALTER COLUMN status SET DEFAULT 'queued';
ALTER TABLE ingest_jobs ALTER COLUMN started_at DROP NOT NULL;
ALTER TABLE ingest_jobs ALTER COLUMN started_at DROP DEFAULT;
ALTER TABLE ingest_jobs
    ADD COLUMN queued_at    timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN run_after    timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN attempts     int NOT NULL DEFAULT 0,
    ADD COLUMN heartbeat_at timestamptz,
    ADD COLUMN worker_id    text,
    ADD COLUMN stage        text,
    ADD COLUMN stage_done   int NOT NULL DEFAULT 0,
    ADD COLUMN stage_total  int NOT NULL DEFAULT 0;
UPDATE ingest_jobs SET queued_at = COALESCE(started_at, now());
-- Rows left 'running' from before the queue existed belong to processes that are gone.
UPDATE ingest_jobs SET status = 'failed', error = COALESCE(error, 'interrupted'), finished_at = COALESCE(finished_at, now())
WHERE status = 'running';

-- Repeated requests (e.g. many new-mail events) collapse into one queued job per account and mode.
CREATE UNIQUE INDEX ingest_jobs_one_queued ON ingest_jobs (account_id, mode) WHERE status = 'queued';
-- At most one running job per account: stages for one mailbox never run concurrently.
CREATE UNIQUE INDEX ingest_jobs_one_running ON ingest_jobs (account_id) WHERE status = 'running';
CREATE INDEX ingest_jobs_claim_idx ON ingest_jobs (run_after, id) WHERE status = 'queued';

CREATE TABLE workers (
    id           text PRIMARY KEY,
    hostname     text NOT NULL,
    pid          int NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now()
);
