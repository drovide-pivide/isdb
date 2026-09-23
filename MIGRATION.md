# Migrating ISDb to Postgres

## What changed

Every file the pipeline used to read and write on disk now lives in Postgres
instead:

| Used to be...                                    | Now lives in...                          |
|---------------------------------------------------|-------------------------------------------|
| `data/match_stats/raw/*.json`                      | `raw_cache` (source=`sofascore_match`)    |
| `data/xg_timeline/pl/*.json`, `pl_features.csv`    | `raw_cache` (source=`xg_pl`), `pl_xg_features` |
| `data/xg_timeline/wc/*.json`, `wc_features.csv`, `wc_labelled.csv` | `raw_cache` (source=`xg_wc`), `wc_xg_features`, `wc_labelled` |
| `data/imdb/*.json`                                 | `imdb_ratings`                            |
| `outputs/pl_excitingness.csv`                      | `pl_excitingness`                         |
| `data/frontend/matches_*.json`                     | `frontend_matches`                        |
| `teams.json`, `data/schedule/schedule_matches.json`| `wc_teams`, `wc_schedule` (still hand-edited as JSON — see below) |

`models/*.joblib` is **not** part of this migration — it's a small, versioned
code artifact, not scraped data, and stays in git exactly as before.

Every pipeline script (`match_stats_fetch.py`, `pl_xg_timeline_fetch.py`,
`wc_xg_timeline_fetch.py`, `update_imdb.py`, `build_pl_frontend.py`,
`predict_excitingness.py`) had only its file-reading and file-writing lines
changed to call `db/db.py` instead — none of the scraping, parsing, or model
logic was touched. This was checked directly: recomputing features from the
cached raw payloads that shipped in your sample data reproduces the exact
same numbers as the original CSVs (e.g. `pl2526_match_28778` → `total_xg
3.9031` both ways), and scoring the Premier League with the real shipped
model through the DB reproduces the exact same top-ranked matches as
`outputs/pl_excitingness.csv` (`CRY 2-1 LIV` at `9.48`, `CRY 3-3 BOU` at
`9.37`).

One thing did change on purpose: `build_pl_frontend.py`'s output used to
drop `match_id` entirely (it was only used internally as a sort tiebreaker).
It's now included, because a database table needs a stable key to upsert
against — a flat JSON file didn't.

## 1. Create a free Postgres database

**Recommended: [Neon](https://neon.tech).** Its free tier (0.5 GB storage,
100 compute-hours/month) is comfortably more than this project needs — the
whole dataset here is a few hundred matches — and its free-tier compute
**autosuspends when idle and wakes itself on the next query**, with nothing
to restore by hand. That matters specifically because your fetch jobs will
run on a schedule and sit untouched in between.

**Alternative: [Supabase](https://supabase.com).** Also free, also plenty of
headroom (500 MB storage), and it additionally gives you a ready-made
read-only HTTP API for the frontend (see §4) with no extra code. The
trade-off: a Supabase free project **pauses after 7 days with too little
database activity, and has to be restored by hand from the dashboard** —
a real risk if the weekly workflow below is ever the only thing touching the
DB. If you go this route, either run the workflow more than once a week, or
add a trivial daily "ping" step (a `SELECT 1`) to keep it active — this is a
common, well-documented pattern for Supabase's free tier.

Either way, once the project exists, copy its Postgres connection string
(on Neon: dashboard → Connection Details; on Supabase: Project Settings →
Database → Connection string → URI). It looks like:

```
postgresql://user:password@host:5432/dbname
```

## 2. Apply the schema and backfill your existing data

```bash
export DATABASE_URL="postgresql://user:password@host:5432/dbname"

psql "$DATABASE_URL" -f db/schema.sql

pip install -r db/requirements.txt
python3 migrate_existing_data.py
```

`migrate_existing_data.py` is safe to re-run — every load is an upsert — so
if you tweak `teams.json` or `data/schedule/schedule_matches.json` later,
just run it again with `--seed-only` to push the change into `wc_schedule`
/ `wc_teams` without touching anything else.

For local development, copy `.env.example` to `.env` and fill in
`DATABASE_URL`; the scripts read it via `python-dotenv` if you `source` it
or use a tool like `direnv`, or just `export` it in your shell as above.

## 3. Running the pipeline day to day

Same scripts, same flags, in the same `pipeline/` layout — they just no
longer need `data/` to exist locally:

```bash
cd pipeline
python3 match_stats_fetch.py --competition wc2026
python3 match_stats_fetch.py --competition pl2025
python3 pl_xg_timeline_fetch.py
python3 wc_xg_timeline_fetch.py --api-key YOUR_KEY
python3 update_imdb.py tt32915471          # WC2026 episode ratings
cd ..
python3 predict_excitingness.py --top 15
python3 pipeline/build_pl_frontend.py
```

`--features-only` / `--dry-run` still work, and now run entirely against
cached data already in Postgres — no network calls. Every script also
gained an escape hatch back to a file for local testing/debugging:
`build_pl_frontend.py --scores-csv path.csv`, `--out-json DIR` on both
`build_pl_frontend.py` and `match_stats_fetch.py`, and `-o path.csv` on
`predict_excitingness.py` — none of these are needed for normal use; the DB
is always the source of truth.

## 4. Automating it

Two workflows are in `.github/workflows/`:

- **`data-pipeline.yml`** — the routine job: Sofascore for WC2026 + the
  current PL season, Understat's PL xG, scoring, and rebuilding
  `frontend_matches`. Fires every morning at 06:00 UTC, but the
  `check-matchday` job first checks yesterday's date against
  `.github/pl_matchdays_2026_27.json` — the real 2026/27 fixture calendar
  (64 distinct match dates, sourced from fixturedownload.com and
  cross-checked against the official PL release's international-break
  dates) — and skips the rest of the run entirely on a day with nothing
  new. A manual run from the Actions tab always goes through regardless of
  date. **This file needs regenerating for the 2027/28 season next
  summer**, and refreshing sooner if you notice TV reschedules or
  postponements have drifted a date — regenerate it the same way it was
  built: pull the current fixture list from
  `https://fixturedownload.com/results/epl-<year>` (or the official PL
  fixtures page) and collapse it to the distinct set of match dates.
- **`data-pipeline-manual.yml`** — `wc_xg_timeline_fetch.py` (needs a paid,
  rate-limited GOAT API key, and World Cup data only needs backfilling
  occasionally) and `update_imdb.py` (drives a real browser via Playwright
  and needs an explicit IMDb title id) — both triggered by hand from the
  Actions tab when you actually want them, not on a schedule.

Set these secrets once, in **Settings → Secrets and variables → Actions**:

| Secret | Required for | Value |
|---|---|---|
| `DATABASE_URL` | all workflows | the connection string from step 1 |
| `GOAT_API_KEY` | `wc-xg` job only | your BallDontLie GOAT key |

Nothing about this touches the git repo — the workflow reads code from git
as usual, but every byte of data it fetches goes straight to Postgres.

**If you're on Supabase:** also add **`db-keep-alive.yml`**. It's a third
workflow that runs `SELECT 1` against the DB once a day — cheap insurance
against the 7-day low-activity pause described in §1. Note that
`data-pipeline.yml`'s own daily trigger only *checks* the fixture calendar
every morning — it doesn't touch the database at all on a non-matchday, and
gaps between fixture dates run up to 21 days (international breaks), so the
pipeline alone can't be relied on to keep a Supabase project active. The
keep-alive ping covers that gap, plus the full off-season (June-July) when
`data-pipeline.yml` has nothing to check against at all. It's harmless to
add even before you're sure; on Neon it's simply unnecessary (nothing to
keep alive), so delete the file if you end up there instead.

## 5. The frontend

You have two options; pick one.

**Recommended — query the DB directly, drop the JSON files entirely.**
Neon and Supabase both expose the database over HTTP (Neon's Data API;
Supabase's auto-generated REST API via PostgREST), so a static frontend can
read `frontend_matches` straight from the browser with no backend of your
own. On Supabase, after applying `db/schema.sql` (which already enables RLS
and a public-read policy scoped to exactly `frontend_matches`), the anon
public key it gives you can only ever read that one table — nothing else is
exposed. A request looks like:

```js
const res = await fetch(
  `${SUPABASE_URL}/rest/v1/frontend_matches?competition=eq.wc2026&select=*`,
  { headers: { apikey: SUPABASE_ANON_KEY } }
);
const matches = await res.json();
```

This removes the "generate JSON, commit it, redeploy" step completely —
the site is always showing whatever's currently in the DB, and there's
nothing data-related left in git at all.

**Alternative — keep generating static JSON files.** Every script that
used to write one still can: `match_stats_fetch.py --competition wc2026
--out-json ../data/frontend` and `build_pl_frontend.py --out-json
../data/frontend` reproduce the exact old file shapes byte-for-byte (this
was checked directly). Wire one of these as an extra step at the end of
`data-pipeline.yml`, followed by a commit-and-push, if you'd rather the
frontend keep reading local files. Note this puts the generated files back
in git, which is a smaller, cleaner footprint than the old raw/intermediate
data but isn't a full "nothing data-related in git" — worth knowing if that
was the point of item 1 on your list.

## 6. What's still in git, and why

`.gitignore` now excludes everything scraped or computed by the pipeline.
Two things are deliberately **not** ignored:

- `teams.json` and `data/schedule/schedule_matches.json` — these are
  hand-curated, not scraped (kickoff times, stadiums, team colors). They're
  easiest to fix via a normal pull request, so they stay as the editable
  source of truth in git; `migrate_existing_data.py --seed-only` is how a
  change gets from the file into `wc_schedule` / `wc_teams`.
- `models/*.joblib` — the shipped model is a small, versioned code
  artifact you build once via the notebook, not something the scheduled
  jobs write.

## Optional: run observability

`db/schema.sql` includes a `pipeline_runs` table and `db.py` has
`log_run_start()` / `log_run_end()` helpers, but nothing calls them yet.
Wrapping each script's `main()` with them (or adding a step to the GitHub
Actions workflow that runs before/after) turns "did last Monday's job
actually succeed?" into a one-row query instead of a log search — worth
adding once you've run this for real a few times and want that visibility.

## Troubleshooting: Sofascore blocking requests from GitHub Actions

`match_stats_fetch.py` talks to Sofascore's internal, undocumented API —
there's no official key or authentication, which also means there's no
official promise it'll keep working. In practice, it actively blocks
automated traffic from datacenter IPs:

- **From a residential connection** (your own machine), a plain `requests`
  call with spoofed browser headers mostly works, but can intermittently
  fail with a `ConnectionResetError` — retrying the same command again
  usually succeeds.
- **From a datacenter IP** — confirmed on both GitHub Actions' runners and,
  separately, the sandbox this migration was built in — the same request
  fails hard with a clean `403 Forbidden`. A retry doesn't fix this one.

**What was tried, and what actually worked:**

1. **`curl_cffi` with Chrome TLS impersonation** — the theory was that
   Sofascore was fingerprinting the TLS handshake itself, not just the IP.
   Tested directly: still a clean 403 from a datacenter IP. Ruled out —
   impersonation alone was never enough, so the block is IP-based, not
   fingerprint-based.
2. **A residential proxy (IPRoyal)** — routes the request through a normal
   consumer IP instead of a datacenter one. This is what actually works:
   confirmed with a real 200 response and real data.
3. One snag along the way, in case it recurs: combining `curl_cffi`'s
   impersonation with this specific proxy produced a TLS certificate
   verification error (`no alternative certificate subject name matches
   target hostname`) — a known category of `curl_cffi`-proxy interaction bug,
   not a problem with the proxy itself. Confirmed by testing the identical
   proxy with plain `requests`, which worked cleanly. Since impersonation
   was never actually necessary (see point 1), the fix was simply to drop
   `curl_cffi` again and use plain `requests` with the proxy — simpler, and
   avoids the bug entirely rather than working around it.

**Current setup:** `match_stats_fetch.py` uses plain `requests`. If the
`SOFASCORE_PROXY_URL` environment variable is set, it's used as both the
HTTP and HTTPS proxy; if unset, requests go out directly (correct for local
runs, where a residential IP already works fine unproxied).

**To use this yourself:**
1. Sign up for a residential proxy provider (pay-as-you-go, no minimum
   commitment — at this project's volume, cost is negligible; see
   `test_proxy.py` below for how to confirm a specific provider actually
   works before committing to it).
2. Add the proxy's connection string as a GitHub secret named
   `SOFASCORE_PROXY_URL`, in the same `http://user:pass@host:port` shape as
   `DATABASE_URL`.
3. Locally, leave `SOFASCORE_PROXY_URL` unset — a residential IP doesn't
   need it.

**`test_proxy.py`**, in the repo root, tests this in isolation before
wiring anything into the real pipeline: hits the exact endpoint that
returned 403, with and without a proxy, and with and without `curl_cffi`'s
impersonation (`--plain` flag), so a failure points at the right cause
instead of requiring a guess.

```bash
pip install curl_cffi requests
python3 test_proxy.py --no-proxy      # confirms the block reproduces locally
export PROXY_URL="http://user:pass@host:port"   # from your provider
python3 test_proxy.py --plain
```

Note `test_proxy.py` reads `PROXY_URL` (a local, throwaway name for testing)
while the real pipeline and GitHub Actions read `SOFASCORE_PROXY_URL` (the
actual secret name) — same value, deliberately different variable name, so
testing never risks touching the real secret.

If a future proxy provider's connection genuinely doesn't work even with
plain `requests` (not just a `curl_cffi` quirk), the next escalations,
roughly in order of cost:

1. Try a different proxy provider or a fresh session — one blocked IP in a
   rotating pool doesn't mean the whole pool is blocked.
2. Switch to full Playwright browser automation for this script too (same
   approach `update_imdb.py` already uses for IMDb) — more invasive, but
   addresses fingerprinting if it turns out to matter after all.
3. Run this one job on a self-hosted runner (e.g. your own machine) instead
   of GitHub-hosted infrastructure — free, but means the job depends on
   that machine being reachable at run time, which cuts against the point
   of automating it.

`pl_xg_timeline_fetch.py` (Understat) and `wc_xg_timeline_fetch.py`
(BallDontLie, a real authenticated API) haven't shown this problem — this
is specific to Sofascore.
