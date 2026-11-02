-- Every notification the bridge decided to send, so repeats can be suppressed
-- (one per show and season to review, one per finished series, one per token
-- problem a day) and the settings page can show what was sent and what failed.
CREATE TABLE notification_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- review | rating | token | test
    dedupe_key  TEXT,
    title       TEXT NOT NULL,
    body        TEXT,
    link        TEXT,
    result      TEXT NOT NULL,          -- pending | sent | failed
    error       TEXT
);
CREATE INDEX notification_log_key ON notification_log (dedupe_key, at);
