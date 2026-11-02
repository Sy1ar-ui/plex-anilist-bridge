-- Sonarr as an optional source of TVDB ids for shows Plex gives none.
--
-- sonarr_series is a local copy of Sonarr's series list, refreshed on a
-- schedule, so the mapping engine stays offline and deterministic: it reads
-- this table the way it reads the mapping datasets.
CREATE TABLE sonarr_series (
    sonarr_id   INTEGER PRIMARY KEY,
    title       TEXT,
    clean_title TEXT,
    year        INTEGER,
    tvdb_id     INTEGER,
    tmdb_id     INTEGER,
    imdb_id     TEXT,
    folder      TEXT,           -- last path component, lower case
    alt_titles  TEXT,           -- JSON list of cleaned alternate titles
    refreshed_at TEXT NOT NULL
);
CREATE INDEX sonarr_series_imdb ON sonarr_series (imdb_id);
CREATE INDEX sonarr_series_tmdb ON sonarr_series (tmdb_id);
CREATE INDEX sonarr_series_folder ON sonarr_series (folder);
CREATE INDEX sonarr_series_title ON sonarr_series (clean_title);

-- The show's folder name in Plex, matched against Sonarr's series folder.
ALTER TABLE show ADD COLUMN folder TEXT;
