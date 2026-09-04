"""
pl_xg_timeline_fetch.py
------------------------
Fetches shot-level xG data for Premier League seasons from Understat,
computes the same xG-by-minute timeline features as wc_xg_timeline_fetch.py.

Uses jeke-understat-scrapper, a maintained fork of understatapi with fixes
for Understat's AJAX-based data loading:

  pip uninstall understatapi -y
  pip install jeke-understat-scrapper

Outputs:
  data/xg_timeline/pl/pl2025_match_{id}.json   — raw shots per PL 2025/26 match
  data/xg_timeline/pl/pl2026_match_{id}.json   — raw shots per PL 2026/27 match
  data/xg_timeline/pl/pl_features.csv          — one row per match, all features

Resumable: any match whose JSON already exists in xg_timeline/ is skipped.

Usage:
  python pl_xg_timeline_fetch.py
  python pl_xg_timeline_fetch.py --features-only
"""

import argparse
import csv
import json
import os
import time
from collections import defaultdict

from understatapi import UnderstatClient

RAW_DIR   = "data/xg_timeline"
FEAT_PATH = "data/xg_timeline/pl_features.csv"
SEASONS   = ["2025", "2026"]
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


def write_csv(rows: list, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ALL_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n✓ Features CSV written to {path}  ({len(rows)} rows)")


# ---------------------------------------------------------------------------
# Load a saved raw file and compute its feature row
# ---------------------------------------------------------------------------

def process_raw_file(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"    [skip] could not load {path}: {e}")
        return None

    match   = raw["match"]
    shots_h = raw["shots_h"]
    shots_a = raw["shots_a"]
    all_xg  = [float(s.get("xG") or 0) for s in shots_h + shots_a]

    meta = {
        "match_id":    match["id"],
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

def fetch_season(season: str) -> None:
    understat = UnderstatClient()

    print(f"\nFetching PL {season} match list from Understat...")
    matches  = understat.league(league="EPL").get_match_data(season=season)
    finished = [m for m in matches if m.get("isResult")]
    print(f"  {len(finished)} finished matches (out of {len(matches)} total)")

    for i, match in enumerate(finished, 1):
        mid      = match["id"]
        raw_path = os.path.join(RAW_DIR, f"pl{season}_match_{mid}.json")

        home = (match.get("h") or {}).get("short_title", "?")
        away = (match.get("a") or {}).get("short_title", "?")

        if os.path.exists(raw_path):
            print(f"  [{i:>3}/{len(finished)}] pl{season} match {mid}  {home} vs {away} — already saved, skipping")
            continue

        print(f"  [{i:>3}/{len(finished)}] pl{season} match {mid}  {home} vs {away} ...", end=" ", flush=True)

        shot_data = understat.match(match=mid).get_shot_data()

        # get_shot_data() returns either {"h": [...], "a": [...]}
        # or a flat list with h_a field — handle both
        if isinstance(shot_data, dict):
            shots_h = shot_data.get("h", [])
            shots_a = shot_data.get("a", [])
        else:
            shots_h = [s for s in shot_data if s.get("h_a") == "h"]
            shots_a = [s for s in shot_data if s.get("h_a") == "a"]

        match["season"] = season
        payload = {"match": match, "shots_h": shots_h, "shots_a": shots_a}
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        print(f"{len(shots_h) + len(shots_a)} shots")
        time.sleep(DELAY)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(features_only: bool) -> None:
    os.makedirs(RAW_DIR, exist_ok=True)

    if not features_only:
        for season in SEASONS:
            fetch_season(season)

    print("\nComputing features from saved raw files...")
    rows = []
    raw_files = sorted(
        f for f in os.listdir(RAW_DIR)
        if f.startswith("pl") and f.endswith(".json")
    )
    for fname in raw_files:
        row = process_raw_file(os.path.join(RAW_DIR, fname))
        if row:
            rows.append(row)
            score = f"{row['home_score']}–{row['away_score']}"
            print(f"  {fname:<38}  {row['home']} vs {row['away']}  "
                  f"{score}  shots={row['total_shots']}  xg={row['total_xg']}")

    rows.sort(key=lambda r: (r["season_year"], r["match_id"]))
    write_csv(rows, FEAT_PATH)


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