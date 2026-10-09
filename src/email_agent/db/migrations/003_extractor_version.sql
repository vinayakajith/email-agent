-- Facts extracted before versioning came from extractor version 1.
ALTER TABLE email_facts ADD COLUMN extractor_version int NOT NULL DEFAULT 1;
