ISDb running setups and stuffs to know (note for myself too actually)

# Try!

https://isdb-euy.vercel.app

_______

# Pipeline

Two things live in this repo now:

1. **Website pipeline** — fetches WC2026 data and builds the JSON the site reads.
2. **ML data collection** — gathers match features across WC2022, WC2026 and the
   Premier League to train an excitingness scoring model, with IMDb ratings as
   the target variable.

Both share the same Sofascore fetcher, so there's one source of truth for match
data instead of several.

## Setup

```bash
pip install requests playwright
playwright install chromium
```

Playwright is only needed for the IMDb scraper. Everything else is plain HTTP.

For the Premier League xG timeline data:

```bash
pip install jeke-understat-scrapper
```

> Note: `jeke-understat-scrapper` is a maintained fork of `understatapi` with
> fixes for Understat's AJAX-based loading. If you have the original
> `understatapi` installed, uninstall it first — they share a namespace.

---

## Folder structure

```
project/
├── pipeline/
│   ├── update_imdb.py              IMDb ratings (target variable + site display)
│   ├── match_stats_fetch.py        single source: scores, stats, events, momentum
│   ├── pl_xg_timeline_fetch.py     PL shot-level xG (Understat)
│   └── README.md
├── data/
│   ├── imdb/
│   │   └── wc2026.json             (overwritten each run — git history is the changelog)
│   ├── frontend/
│   │   └── matches_wc2026.json     what the website reads
│   └── schedule/
│       └── schedule_matches.json   hand-curated match identity + venue metadata
│   ├── match_stats/
│   │   ├── raw/                    cached API payloads, one file per match
│   │   └── {comp}_match_stats.csv  match stats, events, cards, fouls, momentum
│   └── xg_timeline/
│       └── pl/
│           ├── pl2025_match_{id}.json    raw shots
│           ├── pl2627_match_{id}.json
│           └── pl_features.csv           PL xG timeline features
```

---

# Website pipeline

Run these from the `pipeline/` folder, in order.

### Step 1 — Fetch latest IMDb ratings

The tt code is the unique id for each series (a tournament, in our case).

```bash
python3 update_imdb.py tt32915471 --output ../data/imdb/wc2026.json
```

Scrapes IMDb for all episode ratings and overwrites `data/imdb/wc2026.json`.
We don't keep dated snapshot copies — commit as usual and git history gives
you the full timeline of how ratings changed if you ever need it.

---

### Step 2 — Fetch match data and build the frontend file

```bash
python3 match_stats_fetch.py --competition wc2026
```

Pulls scores, extra time, penalties, stats, goal/card events and momentum from
Sofascore, then writes `data/frontend/matches_wc2026.json` with every field the
site expects.

Match identity (`match_id` 1–104, stadium, city, group, `time_et`) still comes
from `schedule_matches.json`, which is hand-curated static reference data rather
than a fetched source. Sofascore supplies the scores; IMDb supplies the ratings;
any existing `oneline_comment` is preserved.

Raw API responses are cached in `data/match_stats/raw/`, so a re-run skips matches it
already has. Safe to interrupt and resume.

---

## Preview without writing

```bash
python3 match_stats_fetch.py --competition wc2026 --dry-run
```

Fetches everything and prints a preview without touching the frontend file.

To rebuild the frontend JSON from the cache with no network calls at all:

```bash
python3 match_stats_fetch.py --competition wc2026 --features-only
```

---

## When to run

| Situation | Steps to run |
|-----------|-------------|
| After each match (~90 min after kickoff) | Step 1 → Step 2 |
| Full refresh | Step 1 → Step 2 |

Knockout fixtures resolve themselves — Sofascore already knows who played whom,
so there's no separate schedule-resolution step any more.

### Or just run both together

From the `pipeline/` folder:

```bash
python3 update_imdb.py tt32915471 --output ../data/imdb/wc2026.json && python3 match_stats_fetch.py --competition wc2026
```

## Running it locally

From the main folder:

```bash
python3 -m http.server 8080
```

---

# ML data collection

Training data for the excitingness scoring model. IMDb ratings are the target
variable; everything below is a feature.

## Data sources

| Source | Covers | Cost | Provides |
|---|---|---|---|
| **Sofascore** | WC2022, WC2026, PL | Free | Scores, goal/card events with minute, possession, shots, corners, fouls, big chances, xG totals, momentum per minute |
| **Understat** | PL 2025/26, 2026/27 | Free | Shot-level xG with minute |
| **IMDb** | WC2022, WC2026 | Free | Episode ratings → target variable |

Sofascore covers everything except shot-level xG timelines; Understat fills that
gap for the Premier League.

## Collecting it

```bash
# Match stats, events, cards, fouls, momentum — all competitions
python3 match_stats_fetch.py --competition wc2022
python3 match_stats_fetch.py --competition wc2026
python3 match_stats_fetch.py --competition pl2025
python3 match_stats_fetch.py --competition pl2627

# Shot-level xG timeline (Premier League)
python3 pl_xg_timeline_fetch.py
```

Run everything from the `pipeline/` folder.

All scripts cache per match and skip what they already have, so an
interrupted run resumes with the same command.

## Features

**Goals** — total, difference, per team, first/last goal minute, goals in first
and last 15 minutes, lead changes, comeback flag, draw flag, penalties and own
goals.

**Score state** — time spent level, time spent with each team leading, whether
the match was still level after the 75th minute.

**xG totals** — per team, combined, difference, whether the losing side
out-created the winner, goals minus xG per team.

**Shot quality** — total shots, shots on target, big chances, big chances
missed, average xG per shot.

**Shot timeline** — cumulative xG per 15-minute window per team, xG in the first
and last 15 minutes, largest xG swing between consecutive windows, number of
times the xG lead changed hands.

**Momentum** — peak home and away momentum, direction flips, average absolute
value.

**Cards** — yellow and red totals, first red card minute, whether it came in the
second half, whether the match was within one goal at the time.

**Fouls** — total, per team, difference.

## A note on scraping

Understat's support have confirmed their data is free for non-commercial use,
and Sofascore's internal API is what the site's own frontend calls. Both
scrapers use polite delays. If ISDb ever goes commercial, revisit this.