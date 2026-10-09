-- Conversations are stored server-side so any client (CLI, web, mobile) can resume them.
CREATE TABLE conversations (
    id         uuid PRIMARY KEY,
    account_id bigint NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title      text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_account_idx ON conversations (account_id, updated_at DESC);

-- Append-only Responses API input items, in order. Never rewritten, so a resumed
-- conversation reproduces the exact prompt prefix (and its cache).
CREATE TABLE conversation_items (
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq             int NOT NULL,
    item            jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, seq)
);

-- Sending is a user action: the agent can only request approval.
ALTER TABLE drafts DROP CONSTRAINT drafts_status_check;
ALTER TABLE drafts ADD CONSTRAINT drafts_status_check
    CHECK (status IN ('pending', 'awaiting_approval', 'sending', 'sent', 'discarded'));
ALTER TABLE drafts ADD COLUMN conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL;
ALTER TABLE drafts ADD COLUMN error text;
