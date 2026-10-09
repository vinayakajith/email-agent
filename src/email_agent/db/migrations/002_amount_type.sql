-- Distinguishes amount due on a bill from an amount charged, refunded, etc.
ALTER TABLE email_facts ADD COLUMN amount_type text;
