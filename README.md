ISDb running setups and stuffs to know (note for myself too actually)

# Try!

https://isdb-euy.vercel.app

_______

# Pipeline

Scripts for fetching and updating WC2026 data.

## Setup

```bash
pip install playwright
playwright install chromium
```

---

## Folder structure

```
project/
├── pipeline/
│   ├── imdb_rating.py
│   ├── update_matches.py
│   ├── update_schedule.py
│   └── README.md
├── data/
│   ├── imdb/
│   │   └── wc2026.json   (overwritten each run -- git history is the changelog)
│   ├── frontend/
│   │   └── matches_wc2026.json
│   └── schedule/
│       └── schedule_matches.json
```

---

## How to run a full update

Run these from the `pipeline/` folder, in order.

### Step 1 — Fetch latest IMDb ratings (the tt code here is unique id for each series (tournament in our case))

```bash
python3 imdb_rating.py tt32915471 --output ../data/imdb/wc2026.json
```

Scrapes IMDb for all episode ratings and overwrites `data/imdb/wc2026.json`.
We don't keep dated snapshot copies -- commit as usual and git history gives
you the full timeline of how ratings changed if you ever need it.

---

### Step 2 — Update schedule (home/away teams for knockouts)

```bash
python3 update_schedule.py
```

Fetches resolved results from openfootball on GitHub (plain HTTP, no browser)
and fills `home`/`away` into `data/schedule/schedule_matches.json`.
Knockout slots fill automatically once openfootball publishes the results.

---

### Step 3 — Build the frontend file

```bash
python3 update_matches.py
```

Reads `schedule_matches.json` + the latest IMDb file and writes
`data/frontend/matches_wc2026.json` with `imdb_score` and `oneline_comment`
on every resolved match.

---

## Preview without writing

Add `--dry-run` to either update script to see what would change without
touching any files:

```bash
python3 update_schedule.py --dry-run
python3 update_matches.py --dry-run
```

---

## When to run

| Situation | Steps to run |
|-----------|-------------|
| After each match (~90 min after kickoff) | Step 1 → Step 3 |
| After the last match of each group ends or knockout round concludes | Step 2 → Step 3 |
| Full refresh | Step 1 → Step 2 → Step 3 |

### However,
you can always run all them together (wont hurt):

On pipeline folder
```bash 
python update_imdb.py tt32915471 --output ../data/imdb/wc2026.json && python update_schedule.py && python update_matches.py
```

## Running it locally

On main folder
```bash
python3 -m http.server 8080
```
