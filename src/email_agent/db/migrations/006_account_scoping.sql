-- Every retrieval query must be scoped to one account; chunks carry account_id like the
-- other denormalized filter columns so BM25 and ANN can filter without a join.
ALTER TABLE chunks ADD COLUMN account_id bigint REFERENCES accounts(id) ON DELETE CASCADE;
UPDATE chunks c SET account_id = e.account_id FROM emails e WHERE e.id = c.email_id;
ALTER TABLE chunks ALTER COLUMN account_id SET NOT NULL;
CREATE INDEX chunks_account_idx ON chunks (account_id);
DROP INDEX chunks_bm25_idx;
CREATE INDEX chunks_bm25_idx ON chunks USING bm25 (id, content, account_id, from_addr, category, sent_at);

-- Agent traces belong to an account. Existing rows predate multi-account support and all came
-- from the single connected account.
ALTER TABLE agent_runs ADD COLUMN account_id bigint REFERENCES accounts(id) ON DELETE CASCADE;
UPDATE agent_runs SET account_id = (SELECT min(id) FROM accounts) WHERE account_id IS NULL;
DELETE FROM agent_runs WHERE account_id IS NULL;
ALTER TABLE agent_runs ALTER COLUMN account_id SET NOT NULL;
CREATE INDEX agent_runs_account_idx ON agent_runs (account_id, created_at);
