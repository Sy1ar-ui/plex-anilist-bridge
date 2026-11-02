-- Series finished in Plex but held one episode short of COMPLETED until the
-- user rates them in the UI.
--
-- completed_on is the local date the final episode was watched, so the
-- completion date on AniList is right however long the rating takes.
--
-- status: awaiting  -- waiting for the user
--         queued    -- rated (or "complete without rating"), waiting for AniList
--         done      -- written (or, in dry run, would have been)
--         dismissed -- the user chose not to complete it from here

CREATE TABLE pending_completion (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    anilist_id      INTEGER NOT NULL,
    event_id        INTEGER,
    title           TEXT,
    total_episodes  INTEGER NOT NULL,
    completed_on    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'awaiting',
    score           REAL,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    FOREIGN KEY (event_id) REFERENCES event (id)
);

-- At most one open completion per title, so a replayed final episode is a no-op.
CREATE UNIQUE INDEX pending_completion_open
    ON pending_completion (anilist_id) WHERE status IN ('awaiting', 'queued');
CREATE INDEX pending_completion_status ON pending_completion (status, next_attempt_at);
