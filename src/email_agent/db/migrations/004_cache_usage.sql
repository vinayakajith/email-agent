-- Prompt-cache accounting per agent run (cached_tokens is a subset of input_tokens).
ALTER TABLE agent_runs ADD COLUMN cached_tokens int, ADD COLUMN cache_write_tokens int;
