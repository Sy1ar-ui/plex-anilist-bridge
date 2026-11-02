-- The AniList -> MyAnimeList leg.
--
-- mal_sync_log: one row per AniList entry the poller considered, so the MAL
-- page can say what was mirrored, skipped (no MAL id, would lower progress)
-- or failed, the way the activity log does for Plex events.
CREATE TABLE mal_sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    source      TEXT NOT NULL,          -- poll | reconcile
    anilist_id  INTEGER,
    mal_id      INTEGER,
    title       TEXT,
    outcome     TEXT NOT NULL,          -- written | would_write | no_change | skipped | error | deferred
    detail      TEXT
);
CREATE INDEX mal_sync_log_at ON mal_sync_log (id DESC);

-- The full-reconcile preview, on the tables created for it in 001.
--   kind:     add | update | lower | unmapped | only_on_mal
--   decision: approved | pending | info | applied | would_write | error | skipped
ALTER TABLE mal_reconcile_item ADD COLUMN kind TEXT;
ALTER TABLE mal_reconcile_item ADD COLUMN applied_at TEXT;
ALTER TABLE mal_reconcile_plan ADD COLUMN summary_json TEXT;
ALTER TABLE mal_reconcile_plan ADD COLUMN applied_at TEXT;
