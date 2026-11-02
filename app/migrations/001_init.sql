-- Initial schema.

CREATE TABLE setting (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE plex_server (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    base_url   TEXT NOT NULL,
    token      TEXT NOT NULL,
    machine_id TEXT,
    name       TEXT,
    verified_at TEXT
);

-- The Plex account whose activity counts. Exactly one row.
CREATE TABLE plex_account (
    id    INTEGER PRIMARY KEY CHECK (id >= 0),
    name  TEXT,
    thumb TEXT,
    chosen_at TEXT NOT NULL
);

CREATE TABLE library (
    section_id INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    kind       TEXT,
    enabled    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE oauth_client (
    provider      TEXT PRIMARY KEY,   -- 'anilist' | 'mal'
    client_id     TEXT NOT NULL,
    client_secret TEXT
);

CREATE TABLE oauth_token (
    provider      TEXT PRIMARY KEY,   -- 'anilist' | 'mal'
    access_token  TEXT,
    refresh_token TEXT,
    expires_at    TEXT,
    account_name  TEXT,
    account_id    INTEGER,
    status        TEXT NOT NULL DEFAULT 'absent',  -- absent|ok|expiring|broken
    last_error    TEXT,
    updated_at    TEXT
);

-- Shows seen in the enabled libraries, with their external ids.
CREATE TABLE show (
    plex_guid   TEXT PRIMARY KEY,     -- plex://show/<hex>
    rating_key  TEXT,
    section_id  INTEGER,
    title       TEXT,
    year        INTEGER,
    tvdb_id     INTEGER,
    tmdb_id     INTEGER,
    imdb_id     TEXT,
    thumb       TEXT,
    last_seen   TEXT
);
CREATE INDEX show_tvdb ON show (tvdb_id);
CREATE INDEX show_rating_key ON show (rating_key);

-- Per-season episode counts read from the Plex library, used for absolute
-- numbering and for seasons that continue one AniList entry.
CREATE TABLE show_season (
    plex_guid   TEXT NOT NULL,
    season      INTEGER NOT NULL,
    episode_count INTEGER NOT NULL,
    min_episode INTEGER,
    max_episode INTEGER,
    contiguous  INTEGER NOT NULL DEFAULT 1,
    refreshed_at TEXT,
    PRIMARY KEY (plex_guid, season)
);

-- Manual corrections made in the UI. Always beat every dataset.
CREATE TABLE mapping_override (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,          -- 'plex_guid' | 'tvdb'
    scope_key  TEXT NOT NULL,
    season     INTEGER NOT NULL,
    ep_from    INTEGER NOT NULL,
    ep_to      INTEGER,                -- NULL = open ended
    anilist_id INTEGER NOT NULL,
    ep_offset  INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX mapping_override_scope
    ON mapping_override (scope_type, scope_key, season);

-- Curated community dataset (PlexAniSync-Custom-Mappings), keyed by Plex GUID.
-- The upstream 'start' field is normalised to ep_offset = start - 1 so that
-- every layer uses one arithmetic: entry_episode = episode - ep_offset.
CREATE TABLE ds_community (
    plex_guid  TEXT NOT NULL,
    season     INTEGER NOT NULL,
    ep_offset  INTEGER NOT NULL DEFAULT 0,
    anilist_id INTEGER NOT NULL,
    title      TEXT,
    source     TEXT NOT NULL,
    PRIMARY KEY (plex_guid, season, ep_offset, anilist_id)
);
CREATE INDEX ds_community_lookup ON ds_community (plex_guid, season);

-- Fribb/anime-lists. tvdb_season IS NULL means the upstream entry used
-- defaulttvdbseason="a", i.e. absolute episode numbering.
CREATE TABLE ds_fribb (
    anidb_id    INTEGER PRIMARY KEY,
    anilist_id  INTEGER,
    mal_id      INTEGER,
    kind        TEXT,                  -- TV|MOVIE|OVA|ONA|SPECIAL...
    tvdb_id     INTEGER,
    tvdb_season INTEGER,
    tvdb_offset INTEGER NOT NULL DEFAULT 0,
    tmdb_tv_id  INTEGER,
    tmdb_season INTEGER,
    tmdb_offset INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ds_fribb_tvdb ON ds_fribb (tvdb_id, tvdb_season);
CREATE INDEX ds_fribb_tmdb ON ds_fribb (tmdb_tv_id, tmdb_season);
CREATE INDEX ds_fribb_anilist ON ds_fribb (anilist_id);
CREATE INDEX ds_fribb_mal ON ds_fribb (mal_id);

-- Movie / imdb ids, exploded so they can be indexed.
CREATE TABLE ds_fribb_ext (
    anidb_id INTEGER NOT NULL,
    kind     TEXT NOT NULL,            -- 'tmdb_movie' | 'imdb'
    value    TEXT NOT NULL
);
CREATE INDEX ds_fribb_ext_lookup ON ds_fribb_ext (kind, value);

CREATE TABLE dataset_meta (
    name        TEXT PRIMARY KEY,
    etag        TEXT,
    fetched_at  TEXT,
    row_count   INTEGER,
    status      TEXT,
    last_error  TEXT
);

CREATE TABLE anilist_media_cache (
    anilist_id    INTEGER PRIMARY KEY,
    title_romaji  TEXT,
    title_english TEXT,
    format        TEXT,
    season_year   INTEGER,
    episodes      INTEGER,
    cover_url     TEXT,
    id_mal        INTEGER,
    fetched_at    TEXT
);

-- One row per watch event considered. dedupe_key makes replays cheap no-ops.
CREATE TABLE event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,     -- webhook|reconcile|replay
    received_at     TEXT NOT NULL,
    viewed_at       TEXT,
    plex_account_id INTEGER,
    plex_rating_key TEXT,
    show_plex_guid  TEXT,
    show_title      TEXT,
    season          INTEGER,
    episode         INTEGER,
    media_kind      TEXT,              -- episode|movie
    status          TEXT NOT NULL,     -- written|would_write|no_change|held|unmatched|skipped|error
    confidence      TEXT,              -- override|confident|uncertain|unmatched
    anilist_id      INTEGER,
    entry_episode   INTEGER,
    detail          TEXT,
    trace_json      TEXT,
    dedupe_key      TEXT NOT NULL UNIQUE
);
CREATE INDEX event_received ON event (received_at DESC);
CREATE INDEX event_status ON event (status);
CREATE INDEX event_show ON event (show_plex_guid, season);

CREATE TABLE write_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     INTEGER,
    target       TEXT NOT NULL,        -- anilist|mal
    dry_run      INTEGER NOT NULL,
    operation    TEXT,
    request_json TEXT,
    before_json  TEXT,
    after_json   TEXT,
    result       TEXT,                 -- ok|skipped|error
    error        TEXT,
    at           TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES event (id)
);
CREATE INDEX write_log_event ON write_log (event_id);
CREATE INDEX write_log_at ON write_log (at DESC);

CREATE TABLE watermark (
    name  TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE counter (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE mal_reconcile_plan (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL           -- draft|approved|applied|cancelled
);

CREATE TABLE mal_reconcile_item (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     INTEGER NOT NULL,
    anilist_id  INTEGER,
    mal_id      INTEGER,
    title       TEXT,
    before_json TEXT,
    after_json  TEXT,
    decision    TEXT NOT NULL DEFAULT 'pending',   -- pending|approved|rejected|applied|error
    error       TEXT,
    FOREIGN KEY (plan_id) REFERENCES mal_reconcile_plan (id)
);
CREATE INDEX mal_reconcile_item_plan ON mal_reconcile_item (plan_id);
