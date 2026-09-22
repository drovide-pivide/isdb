"""
pl_xg_timeline_fetch.py
------------------------
Fetches shot-level xG data for Premier League seasons from Understat,
computes the same xG-by-minute timeline features as wc_xg_timeline_fetch.py.

Uses jeke-understat-scrapper, a maintained fork of understatapi with fixes
for Understat's AJAX-based data loading:

  pip uninstall understatapi -y
  pip install jeke-understat-scrapper

Outputs (Postgres, via db/db.py — set DATABASE_URL before running):
  raw_cache (source='xg_pl')   — raw shots per match, one row per (season, match id)
  pl_xg_features                — one row per match, all features

Run this from the pipeline/ folder.

Resumable: any match already present in raw_cache is skipped.

Usage:
  python pl_xg_timeline_fetch.py
  python pl_xg_timeline_fetch.py --features-only
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from itertools import groupby

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "db"))
import db  # noqa: E402

# Imported lazily inside fetch_season() so that --features-only works offline,
# without requiring the scraper package to be installed.
UnderstatClient = None

CACHE_SOURCE = "xg_pl"

# Maps filename label → Understat API season parameter
# 2025/26 → label "2526", API "2025"
# 2026/27 → label "2627", API "2026"
SEASONS = {
    "2526": "2025",
    "2627": "2026",
}
DELAY     = 2.0   # seconds between requests — be polite to Understat

WINDOWS       = [(0, 15), (15, 30), (30, 45), (45, 60), (60, 75), (75, 200)]
WINDOW_LABELS = ["0-15", "15-30", "30-45", "45-60", "60-75", "75-90plus"]


# ---------------------------------------------------------------------------
# Feature computation — identical logic to wc_xg_timeline_fetch.py
# Understat uses "xG" (capital G) and string-typed values
# ---------------------------------------------------------------------------

def window_idx(minute: int) -> int:
    for i, (start, end) in enumerate(WINDOWS):
        if start <= minute < end:
            return i
    return len(WINDOWS) - 1


def compute_features(shots_h: list, shots_a: list) -> dict:
    all_shots = (
        [(s, True)  for s in shots_h] +
        [(s, False) for s in shots_a]
    )
    all_shots.sort(key=lambda x: int(x[0].get("minute") or 0))

    home_xg_w = defaultdict(float)
    away_xg_w = defaultdict(float)

    for shot, is_home in all_shots:
        minute = int(shot.get("minute") or 0)
        xg     = float(shot.get("xG") or 0.0)
        w      = window_idx(minute)
        if is_home:
            home_xg_w[w] += xg
        else:
            away_xg_w[w] += xg

    largest_swing       = 0.0
    largest_swing_label = ""
    for i in range(1, len(WINDOWS)):
        prev_diff = home_xg_w[i - 1] - away_xg_w[i - 1]
        curr_diff = home_xg_w[i]     - away_xg_w[i]
        swing = abs(curr_diff - prev_diff)
        if swing > largest_swing:
            largest_swing       = swing
            largest_swing_label = f"{WINDOW_LABELS[i-1]}_to_{WINDOW_LABELS[i]}"

    lead_changes = 0
    cum_home = cum_away = 0.0
    prev_leader = None
    for shot, is_home in all_shots:
        xg = float(shot.get("xG") or 0.0)
        if is_home:
            cum_home += xg
        else:
            cum_away += xg
        leader = "home" if cum_home > cum_away else ("away" if cum_away > cum_home else "level")
        if prev_leader and leader != "level" and prev_leader != "level" and leader != prev_leader:
            lead_changes += 1
        prev_leader = leader

    feat = {
        "largest_xg_swing":        round(largest_swing, 4),
        "largest_xg_swing_window": largest_swing_label,
        "xg_lead_changes":         lead_changes,
        "xg_first_15_home":        round(home_xg_w[0], 4),
        "xg_first_15_away":        round(away_xg_w[0], 4),
        "xg_last_15_home":         round(home_xg_w[5], 4),
        "xg_last_15_away":         round(away_xg_w[5], 4),
    }
    for i, label in enumerate(WINDOW_LABELS):
        feat[f"xg_home_{label}"] = round(home_xg_w[i], 4)
        feat[f"xg_away_{label}"] = round(away_xg_w[i], 4)
        feat[f"xg_diff_{label}"] = round(home_xg_w[i] - away_xg_w[i], 4)

    return feat


# ---------------------------------------------------------------------------
# CSV schema
# ---------------------------------------------------------------------------

META_COLS = [
    "match_id", "season_year", "competition", "date",
    "home", "away", "home_score", "away_score",
    "total_shots", "total_xg",
]
FEATURE_COLS = [
    "largest_xg_swing", "largest_xg_swing_window", "xg_lead_changes",
    "xg_first_15_home", "xg_first_15_away",
    "xg_last_15_home",  "xg_last_15_away",
] + [
    f"{kind}_{label}"
    for label in WINDOW_LABELS
    for kind in ["xg_home", "xg_away", "xg_diff"]
]
ALL_COLS = META_COLS + FEATURE_COLS


def write_features(rows: list) -> None:
    n = db.upsert_rows("pl_xg_features", rows, ["match_id"])
    print(f"\n✓ pl_xg_features: {n} rows upserted")


def load_existing_rows() -> dict:
    """match_id (int) -> row dict, from what's already in pl_xg_features.
    Lets run() skip reprocessing (and reprinting) matches that haven't
    changed since the last run instead of walking every cached payload
    every time."""
    out = {}
    for row in db.read_rows("pl_xg_features"):
        out[int(row["match_id"])] = row
    return out


# ---------------------------------------------------------------------------
# Process a cached raw payload and compute its feature row
# ---------------------------------------------------------------------------

def process_raw_payload(raw: dict, cache_key: str) -> dict | None:
    if raw is None:
        print(f"    [skip] no cached payload for {cache_key}")
        return None

    match   = raw["match"]
    shots_h = raw["shots_h"]
    shots_a = raw["shots_a"]
    all_xg  = [float(s.get("xG") or 0) for s in shots_h + shots_a]

    meta = {
        "match_id":    int(match["id"]),
        "season_year": match.get("season", ""),
        "competition": "PL",
        "date":        (match.get("datetime") or "")[:10],
        "home":        (match.get("h") or {}).get("short_title", ""),
        "away":        (match.get("a") or {}).get("short_title", ""),
        "home_score":  (match.get("goals") or {}).get("h"),
        "away_score":  (match.get("goals") or {}).get("a"),
        "total_shots": len(shots_h) + len(shots_a),
        "total_xg":    round(sum(all_xg), 4),
    }
    return {**meta, **compute_features(shots_h, shots_a)}


# ---------------------------------------------------------------------------
# Fetch one season
# ---------------------------------------------------------------------------

def fetch_season(season_label: str) -> None:
    global UnderstatClient
    if UnderstatClient is None:
        try:
            from understatapi import UnderstatClient as _UC
        except ImportError:
            raise SystemExit(
                "error: fetching needs the Understat scraper.\n"
                "  pip uninstall understatapi -y && pip install jeke-understat-scrapper")
        UnderstatClient = _UC
    # Map friendly label to Understat's API season parameter
    api_season = {"2526": "2025", "2627": "2026"}.get(season_label, season_label)
    understat  = UnderstatClient()

    print(f"\nFetching PL {season_label} match list from Understat...")
    matches  = understat.league(league="EPL").get_match_data(season=api_season)
    finished = [m for m in matches if m.get("isResult")]
    # Sort chronologically before bucketing into gameweeks of 10 — same
    # ordering build_pl_frontend.py uses. Don't rely on the API's own
    # return order actually being chronological; match_id as a stable
    # tiebreaker for same-day fixtures.
    finished.sort(key=lambda m: (m.get("datetime") or "", m["id"]))
    print(f"  {len(finished)} finished matches (out of {len(matches)} total)")

    for date, group_iter in groupby(finished, key=lambda m: (m.get("datetime") or "")[:10]):
        group = list(group_iter)
        group_keys = [f"pl{season_label}_match_{m['id']}" for m in group]

        if all(db.cache_exists(CACHE_SOURCE, k) for k in group_keys):
            print(f"  {date} — all {len(group)} matches already saved, skipping")
            continue

        for i, match in enumerate(group, 1):
            mid       = match["id"]
            cache_key = f"pl{season_label}_match_{mid}"

            home = (match.get("h") or {}).get("short_title", "?")
            away = (match.get("a") or {}).get("short_title", "?")

            if db.cache_exists(CACHE_SOURCE, cache_key):
                print(f"  [{date} {i}/{len(group)}] pl{season_label} match {mid}  {home} vs {away} — already saved, skipping")
                continue

            print(f"  [{date} {i}/{len(group)}] pl{season_label} match {mid}  {home} vs {away} ...", end=" ", flush=True)

            shot_data = understat.match(match=mid).get_shot_data()

            if isinstance(shot_data, dict):
                shots_h = shot_data.get("h", [])
                shots_a = shot_data.get("a", [])
            else:
                shots_h = [s for s in shot_data if s.get("h_a") == "h"]
                shots_a = [s for s in shot_data if s.get("h_a") == "a"]

            match["season"] = api_season
            payload = {"match": match, "shots_h": shots_h, "shots_a": shots_a}
            db.cache_put(CACHE_SOURCE, cache_key, payload, match_id=int(mid), season_year=int(api_season))

            print(f"{len(shots_h) + len(shots_a)} shots")
            time.sleep(DELAY)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(features_only: bool) -> None:
    if not features_only:
        for season_label in SEASONS:
            fetch_season(season_label)

    print("\nComputing features from cached raw payloads...")
    existing = load_existing_rows()
    valid_prefixes = tuple(f"pl{label}_match_" for label in SEASONS)
    cached = [(k, payload) for k, payload in db.cache_all(CACHE_SOURCE) if k.startswith(valid_prefixes)]
    cached.sort(key=lambda kp: kp[0])

    rows = []
    new_count = 0
    for cache_key, payload in cached:
        m   = re.search(r"_match_(\d+)$", cache_key)
        mid = int(m.group(1)) if m else None

        if mid is not None and mid in existing:
            rows.append(existing[mid])
            continue

        row = process_raw_payload(payload, cache_key)
        if row:
            rows.append(row)
            new_count += 1
            score = f"{row['home_score']}–{row['away_score']}"
            print(f"  {cache_key:<34}  {row['home']} vs {row['away']}  "
                  f"{score}  shots={row['total_shots']}  xg={row['total_xg']}")

    if new_count == 0:
        print(f"  all {len(rows)} matches already in pl_xg_features, nothing new to process")
    else:
        reused = len(rows) - new_count
        print(f"  processed {new_count} new match(es)"
              + (f"; reused {reused} already in pl_xg_features" if reused else ""))

    rows.sort(key=lambda r: (r["season_year"], r["match_id"]))
    write_features(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch PL xG timeline data from Understat and build a features CSV",
        epilog=(
            "Examples:\n"
            "  python pl_xg_timeline_fetch.py\n"
            "  python pl_xg_timeline_fetch.py --features-only"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--features-only", action="store_true",
                        help="Recompute CSV from existing files only, no requests")
    args = parser.parse_args()
    run(args.features_only)


if __name__ == "__main__":
    main()