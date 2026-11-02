# plex-anilist-bridge

[![CI](https://github.com/Sy1ar-ui/plex-anilist-bridge/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Sy1ar-ui/plex-anilist-bridge/actions/workflows/ci.yml)
[![Image](https://github.com/Sy1ar-ui/plex-anilist-bridge/actions/workflows/docker.yml/badge.svg?branch=main)](https://github.com/Sy1ar-ui/plex-anilist-bridge/actions/workflows/docker.yml)
[![Licence: GPL v3](https://img.shields.io/badge/licence-GPL--3.0--or--later-blue.svg)](LICENSE)

Syncs your Plex watch activity to AniList, and mirrors AniList to MyAnimeList,
from one container. AniList is the source of truth; the MAL sync is one-way;
nothing is ever written back to Plex.

It exists because PlexAniSync works but is painful to live with. The things it
fixes, deliberately:

| Problem | What this does instead |
|---|---|
| Hand-editing `custom_mappings.yaml` | Corrections are made in the web UI and stored in the database. Export/import is for backup, not the normal workflow. |
| Restarting to apply a change | Settings and corrections take effect on the next event. Intervals are rescheduled live. |
| Opaque failures | Every item records what it matched, **why**, which dataset and offset decided it, and how confident it is. Failures say what was missing. |
| Batch-run-and-pray | Event driven via Plex webhooks, with a per-event history. A periodic reconcile is a safety net, not the mechanism. |
| Config sprawl | Six environment variables, all optional. Everything else is in the UI. |
| Title matching | Matching is by **ID** (TVDB/TMDB/IMDb → AniList), not by comparing title strings. |

> **Status.** The Plex → AniList leg, the mapping engine, the activity log and
> settings are implemented, with dry-run on by default. The review queue,
> search-and-link UI, setup wizard with OAuth, and the AniList → MAL leg are the
> next stages — see [Roadmap](#roadmap). **140 tests pass in CI** and the image
> builds for amd64 and arm64, but nothing has yet been run against a real Plex
> server; see [what is and is not verified](#what-is-and-is-not-verified).

---

## How the matching works

This is the part that matters, so it is worth understanding.

A Plex webhook tells you an episode finished, but **it does not include the
show's TVDB or TMDB id** — there is no `grandparentGuid`, and the episode-level
`Guid` array is not dependable. So every event is enriched by asking your Plex
server about the show. That is why a Plex server URL and token are required
rather than optional.

With `(tvdb_id, season, episode)` in hand, layers are tried in order and the
first one to produce a candidate wins:

1. **Your manual corrections.** Always win, over everything below.
2. **The community dataset** ([PlexAniSync-Custom-Mappings]), keyed on exact
   Plex GUIDs. Human-curated, no guesswork.
3. **[Fribb/anime-lists]**, keyed on TVDB/TMDB id + season + episode offset.
4. **Sonarr** (optional, not yet wired up) purely to recover a missing TVDB id.
5. **An AniList title search**, which is a guess and is therefore *never*
   confident — it produces an "uncertain" match at best.

Everything uses one piece of arithmetic:

```
entry_episode = plex_episode - ep_offset
```

### The cases that usually go wrong

**Split cours.** Re:Zero season 2 is two AniList entries. The dataset gives the
second one an offset of 13, so:

```
S02E13 → offset 0  → AniList 108632 "Re:Zero S2 Part 1", episode 13
S02E14 → offset 13 → AniList 119661 "Re:Zero S2 Part 2", episode 1
```

**Absolute numbering (One Piece).** Upstream marks these shows
`defaulttvdbseason="a"`, and Fribb drops the non-numeric season — so *the
absence of a season is the signal*. The absolute episode number is then worked
out from your own library:

```
S21E1089 → 1088 episodes in seasons 1-20 → absolute 1089
         → AniList 21 "One Piece", offset 0 → episode 1089
```

If any earlier season has missing episodes, that sum is wrong, so the match is
downgraded to **uncertain** and says which season has holes. It does not quietly
believe a number it cannot trust.

**Several Plex seasons continuing one AniList entry.** Gintama's TVDB seasons
1–4 are all one AniList entry, so the episode count carries across them
(`S02E01` → episode 50, not episode 1).

**Specials and movies.** Season 0 is skipped by default and configurable.
Movies match on TMDB movie id or IMDb id.

### Confidence

- **confident** — a direct dataset hit. Applied automatically.
- **uncertain** — a heuristic, an ambiguous dataset, or arithmetic that depends
  on an incomplete library. Held for review by default; can be set to
  apply-and-flag.
- **unmatched** — queued for review. Never guessed.

---

## Install

### Unraid

Add [`unraid-template.xml`](unraid-template.xml) as a template, or install from
Community Applications once published. The defaults are right for Unraid
(`PUID` 99, `PGID` 100, `/config` on your cache SSD).

### docker compose

```bash
git clone https://github.com/Sy1ar-ui/plex-anilist-bridge
cd plex-anilist-bridge
docker compose up --build -d
```

Then open `http://<host>:8787`.

### Environment variables

These are all of them, and all are optional:

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8787` | Web UI and webhook port |
| `PUID` / `PGID` | `99` / `100` | User/group to run as |
| `UMASK` | `022` | Mask for files written to `/config` |
| `TZ` | `UTC` | Timezone for timestamps |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Everything else — Plex server, your account, libraries, tokens, every behaviour
toggle, every interval — lives in the database and is set in the UI.

---

## Setting it up

Open the UI and work down the Settings page. Nothing needs a restart.

**1. Plex server.** The URL (`http://192.168.1.10:32400`) and a token. The
connection is tested before it is saved.

<details>
<summary>Getting a Plex token</summary>

Open any item in Plex Web, choose **Get Info → View XML**, and copy the
`X-Plex-Token` value from the URL. The browser sign-in flow that replaces this
paste box arrives with the setup wizard.
</details>

**2. Which account counts.** Your server is shared, so pick yourself from the
list of accounts. Events from every other account are discarded before anything
about them is processed or logged — only a counter moves. There is a test that
asserts no other user's show title can reach the database.

If no account is chosen, **nothing** is accepted. Defaulting to "accept
everything" on a shared server is the failure this is guarding against.

**3. Anime libraries.** Tick the ones to watch.

**4. AniList.** Paste an access token for now; it is verified against AniList
before being stored.

**5. Behaviour.** Dry-run is on. Leave it on until you have watched the activity
log for a while.

**6. Admin password.** Set one. This page holds tokens to your accounts.

### Plex webhook

Copy the URL from the dashboard — it looks like:

```
http://192.168.1.10:8787/webhook/plex/3f9a1c...
```

In Plex: **Settings → Webhooks → Add Webhook**, paste, save. Plex Pass is
required for webhooks.

The random part of the path **is the credential**: Plex cannot send an
authentication header with a webhook, so the secret has to be in the URL. Treat
it like a password. Opening that URL in a browser returns a small status page,
which is a quick way to prove the path reaches the container.

### A note on API clients

AniList and MyAnimeList both require an API client that **you** register — there
is no way around it, and a shared client id cannot be shipped in a public GPL
repository. That is two one-time form fills, walked through by the setup wizard
when it lands. It is not ongoing configuration.

---

## Review and corrections

The **Activity** log lists every event. Each row links to a "why?" page showing:

- the one-sentence summary of how it resolved,
- every layer that was tried, what it found, and **why each losing candidate
  was rejected**,
- the exact API payload that was sent — or, in dry-run, that *would* have been
  sent, with the before and after state.

Items that are uncertain or unmatched are held and counted on the dashboard. The
queue UI for fixing them, including the visual episode-range editor, is the next
stage of work.

---

## Operational notes

- **Dry-run is ON by default** and is enforced in one place (`WriteGate`), so no
  code path can bypass it.
- **Replays are harmless by construction.** Every AniList write is absolute
  (`progress = N`), never an increment, so re-processing an event cannot
  double-count. Event de-duplication is for a tidy log, not for correctness.
- **Progress is never lowered**, and **scores and notes are never written** —
  those fields are not even declared in the GraphQL mutation.
- **Rate limits** are read from the APIs' own response headers rather than
  guessed. AniList is currently degraded to 30 requests/minute (90 normally);
  `Retry-After` and `X-RateLimit-Reset` are honoured on a 429.
- **Datasets** are cached in `/config/datasets` and in the database, refreshed
  daily with conditional GETs. If GitHub is unreachable the cached copy keeps
  working; a failed refresh never empties a working dataset. Your manual
  overrides live in a separate table and are consulted first, so a refresh can
  never clobber a correction.
- **Logs** are structured JSON on stdout. `/health` is unauthenticated and backs
  the Docker `HEALTHCHECK`.
- Runs as non-root (`PUID`/`PGID`) via `setpriv`.

### On token storage, plainly

Your AniList and MAL tokens are stored in the SQLite database in `/config`, with
the file mode restricted to the owner. They are **not encrypted**. Encrypting
them would mean keeping the key next to the database in the same directory,
which protects against nothing, so this does not pretend otherwise. Treat
`/config` as sensitive, and set the admin password.

---

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The suite is offline: the mapping datasets come from fixture slices in
`tests/fixtures/` (cut from the real datasets, with real ids) and the API
clients are replaced with stubs. Coverage is aimed at the things that go wrong:
split cours, absolute numbering, seasons that continue one entry, specials,
movies, override precedence and ranges, the never-decrease rule, score/notes
never being written, rate-limit backoff, idempotent replays, and the account
filter leaving no trace of another user's viewing.

### What is and is not verified

CI runs the suite on CPython 3.12 and builds the image on every push. Latest run:

- **140 tests pass, 0 fail**;
- the image builds for **linux/amd64 and linux/arm64** from the pinned base
  digest and pushes to GHCR — so the `setpriv` privilege drop and the
  dependency-free Python healthcheck survive a real build.

This was written on a machine with neither Python nor Docker, so before CI
existed the suite ran under CPython 3.13 in WebAssembly (Pyodide). That covered
130 of the 140: Starlette's `TestClient` needs an OS thread, which WebAssembly
does not have, so the ten webhook *HTTP endpoint* tests could not run until CI.
They pass there. The pure account-filter tests — the ones that matter for the
shared-server guarantee — ran in both.

**Not verified:** nothing has been exercised against a real Plex server, a real
AniList account, or a running container. That is the next step:

```bash
pytest
docker compose up --build   # then open http://localhost:8787
curl -s localhost:8787/health
```

### Layout

```
app/
  mapping/      the engine, title normalisation, season arithmetic
  datasets/     dataset download, parsing and caching
  plex/         webhook parsing, account filter, Plex server client
  anilist/      GraphQL client, queries, the update rules
  sync/         pipeline, write gate, reconcile
  web/          routes and templates
  migrations/   numbered SQL, applied at startup
tests/          fixtures and the suite
```

---

## Roadmap

- [x] Webhook intake, account filter, mapping engine, fixture tests
- [x] Plex → AniList in dry-run, activity log, settings page
- [ ] Review queue, search-and-link, visual episode-range editor
- [ ] Setup wizard, Plex PIN link flow, AniList + MAL OAuth
- [ ] AniList → MAL leg with the full-reconcile preview
- [ ] Sonarr as an optional ID source

---

## Licence and attribution

GPL-3.0-or-later. See [LICENSE](LICENSE).

This project reuses work from **[PlexAniSync]** by RickDB and contributors, also
GPL-3.0. Specifically:

- `app/mapping/titles.py` — the title normaliser and season-suffix variant
  generation, from `plexanisync/anilist.py`.
- `app/anilist/rules.py` — the status-transition and never-decrease behaviour,
  from `Anilist.__update_entry`.
- `app/anilist/queries.py` — adapted from `plexanisync/graphql.py`.

Each of those files carries an attribution header. The mapping engine itself is
new: PlexAniSync matches on title text, while this matches on IDs, which is the
central difference between the two projects.

The curated mapping data comes from **[PlexAniSync-Custom-Mappings]**, and the
ID dataset from **[Fribb/anime-lists]**, which merges
[Anime-Lists/anime-lists][anime-lists] with
[manami-project/anime-offline-database][manami]. Thanks to all of them — this
project would be guesswork without that data.

[PlexAniSync]: https://github.com/RickDB/PlexAniSync
[PlexAniSync-Custom-Mappings]: https://github.com/RickDB/PlexAniSync-Custom-Mappings
[Fribb/anime-lists]: https://github.com/Fribb/anime-lists
[anime-lists]: https://github.com/Anime-Lists/anime-lists
[manami]: https://github.com/manami-project/anime-offline-database
