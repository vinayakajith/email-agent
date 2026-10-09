-- OpenAI usage of the extraction stage, to verify prompt caching on bulk calls.
ALTER TABLE ingest_jobs
    ADD COLUMN enrich_input_tokens bigint NOT NULL DEFAULT 0,
    ADD COLUMN enrich_cached_tokens bigint NOT NULL DEFAULT 0,
    ADD COLUMN enrich_output_tokens bigint NOT NULL DEFAULT 0;
