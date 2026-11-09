-- Shows the user has explicitly dropped: live-action that shares a Plex
-- server, something filed into the wrong folder, anything that should never
-- reach the review queue again.
--
-- Separate from mapping_override on purpose: an override says "this is that
-- AniList title", while this says "this is not anime, stop asking".
CREATE TABLE ignored_show (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,          -- 'plex_guid' | 'tvdb'
    scope_key  TEXT NOT NULL,
    title      TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (scope_type, scope_key)
);
CREATE INDEX ignored_show_lookup ON ignored_show (scope_type, scope_key);
