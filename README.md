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

> **Status.** Everything on the [roadmap](#roadmap) except Sonarr is
> implemented: the Plex → AniList leg, the mapping engine, the activity log, the
> review queue, the setup wizard, and the AniList → MyAnimeList mirror with its
> full-reconcile preview. Dry-run is on by default. The suite passes in CI and the image
> builds for amd64 and arm64, but nothing has yet been run against a real Plex
> server, AniList or MyAnimeList; see [what is and is not verified](#what-is-and-is-not-verified).

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

Open the UI. A fresh install starts in the **setup wizard**, which walks
through each step in order; every step can be skipped and come back to, and
nothing needs a restart.

1. **Admin password** — first, because the following steps store tokens to
   your accounts.
2. **Plex** — **Sign in with Plex** sends you to plex.tv and back. The bridge
   then lists your servers and tries every address Plex knows for each one
   *from inside the container*, so you pick one that actually works from there
   (an address that works in your browser may not work from a Docker network).
   Only the chosen server's token is kept; your plex.tv account token is
   discarded once the server is saved.
3. **Your account** — your server is shared, so pick yourself. The account you
   signed in with is preselected. Events from every other account are
   discarded before anything about them is processed or logged — only a
   counter moves. If no account is chosen, **nothing** is accepted.
4. **Anime libraries** — libraries with "anime" in the name are pre-ticked.
5. **AniList** — register an API client on AniList's developer page using the
   redirect URL the wizard shows, enter its id and secret, and **Sign in with
   AniList**. The token is checked against AniList before it is stored. AniList
   tokens last a year.
6. **MyAnimeList** (optional) — the same, with MAL's own client page. Only
   needed for the [MyAnimeList mirror](#mirroring-to-myanimelist).
7. **Plex webhook** — paste the URL into Plex; the page notices when the first
   webhook arrives.

Dry-run stays on throughout. Leave it on until you have watched the activity
log for a while, then turn it off under *Settings → Sync*.

<details>
<summary>Doing it by hand instead</summary>

The Settings page still takes a Plex server URL and token, and a pasted AniList
access token. For a Plex token: open any item in Plex Web, choose **Get Info →
View XML**, and copy the `X-Plex-Token` value from the URL.
</details>

### Plex webhook

Copy the URL from the dashboard (shown until the first webhook arrives) or the
**System** page — it looks like:

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
repository. That is two one-time form fills, walked through by the setup
wizard. It is not ongoing configuration.

The redirect URL you register must match the address you open the bridge at
(for example `http://192.168.1.10:8787/setup/anilist/callback`), so sign in from
that same address. The sign-in uses a single-use, short-lived `state` value,
and MyAnimeList's PKCE verifier never leaves the server.

---

## Review and corrections

The **Activity** log lists every event. Each row links to a "why?" page showing:

- the one-sentence summary of how it resolved,
- every layer that was tried, what it found, and **why each losing candidate
  was rejected**,
- the exact API payload that was sent — or, in dry-run, that *would* have been
  sent, with the before and after state.

### The review queue

Items that are uncertain or unmatched are held, and listed on the **Review**
page (with a count in the top bar). Episodes of one show and season are one
item, so ten unmatched episodes are one thing to fix, not ten.

**Fix match** opens a two-step page:

1. **Pick the AniList title** — from the candidates the engine already
   considered, an AniList search, or by pasting an AniList id or
   `anilist.co/anime/…` link (which works even when AniList search cannot be
   reached).
2. **Set the episode range** — "Plex season 2 episodes 14 to *end* are AniList
   episodes starting at 1". A strip underneath shows every Plex episode and the
   AniList episode it becomes, highlights the ones waiting for review, and flags
   any that would land past the end of the title. Waiting episodes the range
   does not cover are listed, so you know they need a second correction (the
   usual case for a split cour).

A correction can apply to that Plex show only, or to every Plex show with the
same TVDB id. Saving stores it as a manual correction — the layer that beats
every dataset — and re-runs the covered episodes through the normal sync, so
dry-run, never-decrease and the rating hold all still apply. Later episodes of
that show need no review at all.

Corrections are listed at the bottom of the Review page and can be deleted.
Deleting one only changes how future episodes are matched; nothing already
written to AniList is touched. An item that should not be synced can be
**dismissed** instead, which writes nothing.

Events that matched but could not reach AniList are **deferred**, not failed —
see [When AniList is down or throttled](#when-anilist-is-down-or-throttled).
The Activity page filters by outcome with a count on each, and a failed or
deferred event has a **Retry now** button on its page.

### Rating a finished series

With *Settings → Sync → Ask for my rating before marking a series complete*
on (the default), finishing a series does not complete it straight away.
Watching the final episode of a 12-episode series sets AniList to 11/12, and
the series appears under **Ready to rate** on the dashboard, with a count in
the top bar. Enter a rating out of 10 and press **Complete**: AniList gets
COMPLETED, 12/12, your score, and a completion date of **the day you watched
the final episode** — not the day you rated it. Finish something on 2
November while travelling, rate it when you are home on the 4th, and AniList
still says the 2nd.

- The `…` menu offers **Complete without rating**, or **Don't complete**, which
  stops waiting and writes nothing.
- The rating is sent as AniList's 100-point `scoreRaw`, so it is shown in
  whatever score format you use on AniList.
- If AniList is down when you submit, the rating is kept and sent by the same
  retry job as deferred events.
- Dry-run applies: in dry-run both the hold and the completion are recorded on
  the event's page but not sent.
- Only a first completion of a finished-airing series is held. Rewatches and
  still-airing shows behave as before.

## Mirroring to MyAnimeList

AniList is the source of truth; MyAnimeList is a one-way mirror of it. Turn it
on under *Settings → Sync → MyAnimeList* (it needs MAL connected in setup).

**The mirror** checks AniList every 15 minutes (configurable) for list entries
changed since its last run, and updates the matching MAL entries. Titles are
matched by the MAL id AniList itself stores, so there is no guessing. It is
careful in the same ways the Plex leg is:

- **Progress is never lowered** on MAL, and a MAL entry is never moved off
  *completed*, automatically. Those are held back and listed, for the full
  reconcile.
- **Nothing is deleted** from MAL, and **comments and tags are never written**.
- Only the fields that differ are sent. Scores are copied unless turned off
  (AniList's 0–100 becomes 1–10, rounding half up; an unscored AniList entry
  never clears a MAL score). Start and finish dates are copied only if you turn
  that on.
- Status maps across directly; an AniList rewatch (REPEATING) is MAL's
  *completed* with *rewatching* set.
- Dry-run applies. It keeps its own place in the change list, so changes made
  while dry-run is on are still mirrored once it is turned off.
- If MAL is down or rate limiting, the run stops and the next one carries on
  from the same place. MAL tokens last about a month and are refreshed
  automatically.

The mirror's first run starts from "now". To bring existing entries across,
use **the full reconcile** on the **MyAnimeList** page (linked from System). It
reads both whole lists and shows every difference before anything is written:

| Group | Ticked by default |
|---|---|
| Add to MyAnimeList | yes |
| Update on MyAnimeList | yes |
| Would lower MyAnimeList (MAL is ahead) | **no** — tick only where AniList is right |
| No MyAnimeList id on AniList | shown only |
| Only on MyAnimeList | shown only; never deleted |

**Apply ticked** writes just those, through the same dry-run gate. If MAL fails
part-way, the rest stay ticked for another try.

---

## Operational notes

- **Dry-run is ON by default** and is enforced in one place (`WriteGate`), so no
  code path can bypass it.
- **Replays are harmless by construction.** Every AniList write is absolute
  (`progress = N`), never an increment, so re-processing an event cannot
  double-count. Event de-duplication is for a tidy log, not for correctness.
- **Progress is never lowered**, and **notes are never written**. Automatic
  writes never carry a score either — the field is not declared in their
  GraphQL mutation. A score is only ever sent from a rating you type in (see
  [Rating a finished series](#rating-a-finished-series)), through a separate
  mutation that also cannot carry notes.
- **Start and finish dates are the day you watched**, in your `TZ`, not the day
  the event happened to be processed — so an event deferred through an outage,
  or found later by the reconcile sweep, is still dated correctly.
- **Rate limits** are read from the APIs' own response headers rather than
  guessed. AniList is currently degraded to 30 requests/minute (90 normally);
  `Retry-After` and `X-RateLimit-Reset` are honoured on a 429. The System page
  shows the live limit and flags it when it is below normal.
- **Datasets** are cached in `/config/datasets` and in the database, refreshed
  daily with conditional GETs. If GitHub is unreachable the cached copy keeps
  working; a failed refresh never empties a working dataset. Your manual
  overrides live in a separate table and are consulted first, so a refresh can
  never clobber a correction.
- **Logs** are structured JSON on stdout. `/health` is unauthenticated and backs
  the Docker `HEALTHCHECK`.
- Runs as non-root (`PUID`/`PGID`) via `setpriv`.

### When AniList is down or throttled

AniList has had long outages and long stretches of reduced limits, so this is
handled as a normal state rather than an error:

- **Nothing is lost.** An event that matched but could not reach AniList is
  marked **deferred**, keeps its match, and is retried by a background job
  (every 5 minutes by default). Each event backs off on its own — 5 min, 10,
  20, 40, then hourly — and is given up on after 7 days, after which it can
  still be retried by hand. Retrying is safe because writes are absolute and
  the current AniList state is re-read first.
- **An outage is not hammered.** After three consecutive failures (5xx,
  timeouts, connection errors, HTML error pages, "API temporarily disabled")
  a circuit breaker pauses all AniList requests for 30 seconds, doubling each
  time it re-opens, up to 15 minutes. New events are deferred immediately
  instead of each sleeping through its own retries.
- **Heavy throttling defers instead of blocking.** A request that would have to
  wait longer than 120 seconds for a rate-limit slot (or is told to by a 429)
  is deferred rather than holding up everything else.
- **A bad token is not confused with an outage.** A 401 or "Invalid token"
  marks the token broken, and deferred events wait for you to re-authorise; a
  403 maintenance page or Cloudflare challenge does not touch the token.
  Trying a new token on the settings page never marks the stored one broken.

The **System** page's **AniList API** card shows the state (healthy, degraded,
throttled, down), the last error, when requests resume, and the deferred
backlog, with **Check AniList now** (one request, even while paused) and
**Retry deferred now** buttons. A banner on every page says when AniList is
down, throttling, or has rejected the token. All three thresholds are under
*Settings → AniList outages and throttling*. `/health` reports the AniList
state and backlog, but does not fail the container's healthcheck, because a
restart would not help.

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

### Releasing

The version is in four places: `app/config.py`, `pyproject.toml`,
`docker-compose.yml` and `unraid-template.xml`. The last two pin the exact
image tag, since nothing here uses `:latest`. `tests/test_version.py` fails if
they disagree. Bump all four in the PR, merge it, then tag **the merge commit
on `main`**:

```bash
git tag -a v0.4.0 -m "Release 0.4.0" && git push origin v0.4.0
```

The tag push is what publishes `ghcr.io/sy1ar-ui/plex-anilist-bridge:0.4.0`.
Until it is pushed, the compose file and template on `main` name a tag that
does not exist yet.

### Layout

```
app/
  mapping/      the engine, title normalisation, season arithmetic
  datasets/     dataset download, parsing and caching
  plex/         webhook parsing, account filter, Plex server client
  anilist/      GraphQL client, queries, the update rules
  mal/          MyAnimeList client, mirror rules, poller, full reconcile
  sync/         pipeline, write gate, reconcile
  web/          routes and templates
  migrations/   numbered SQL, applied at startup
tests/          fixtures and the suite
```

---

## Roadmap

- [x] Webhook intake, account filter, mapping engine, fixture tests
- [x] Plex → AniList in dry-run, activity log, settings page
- [x] Review queue, search-and-link, visual episode-range editor
- [x] Setup wizard, Plex PIN link flow, AniList + MAL OAuth
- [x] AniList → MAL leg with the full-reconcile preview
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
