-- Deferred events: what is needed to retry an event after an AniList outage.
--
-- input_json is the WatchEvent as the pipeline first received it, so a retry
-- runs exactly the same input again. attempts and next_attempt_at drive the
-- retry job's backoff; next_attempt_at is NULL unless the event is deferred.

ALTER TABLE event ADD COLUMN input_json TEXT;
ALTER TABLE event ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE event ADD COLUMN next_attempt_at TEXT;

CREATE INDEX event_retry_due ON event (status, next_attempt_at);
