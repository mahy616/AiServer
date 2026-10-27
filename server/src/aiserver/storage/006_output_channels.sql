-- Existing deltas remain content. Both channels share one durable sequence.
ALTER TABLE output_chunks ADD COLUMN channel TEXT NOT NULL DEFAULT 'content'
 CHECK(channel IN ('content','reasoning_content'));
PRAGMA user_version=6;
