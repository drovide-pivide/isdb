"""
wc_xg_timeline_fetch.py
------------------------
Fetches shot-level data for WC2022 and WC2026, computes xG-by-minute
timeline features, and writes:

  data/xg_timeline/wc/wc2022_match_{id}.json   — raw shots per WC2022 match
  data/xg_timeline/wc/wc2026_match_{id}.json   — raw shots per WC2026 match
  data/xg_timeline/wc/wc_features.csv          — one row per match, all features

WC2022 and WC2026 both have full xG coverage. WC2018 does not and is excluded.
Momentum is sourced from Sofascore separately for all competitions.

Run this from the pipeline/ folder.

Resumable: any match whose JSON already exists in data/xg_timeline/wc/ is skipped,
so re-running after an interruption picks up exactly where it left off.

Rate limiting
  Set the DELAY constant below to match your tier:
    13.0 → GOAT free trial (5 req/min limit). This is the default.
     0.2 → paid GOAT (600 req/min)
  1 call per match (shots only). 168 matches total (64 + 104).
  At 5 req/min that is ~34 min. At 600 req/min it takes ~34 seconds.

Usage:
  pip install requests

  # Full run:
  python wc_xg_timeline_fetch.py --api-key YOUR_KEY

  # Resume after interruption (skips already-saved matches):
  python wc_xg_timeline_fetch.py --api-key YOUR_KEY

  # Recompute features CSV from already-downloaded files only:
  python wc_xg_timeline_fetch.py --api-key YOUR_KEY --features-only
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict

# Imported lazily via _requests() so that --features-only works offline,
# without requiring the `requests` package to be installed.
requests = None


def _requests():
    """Import `requests` on first use, with a helpful error if it's missing."""
    global requests
    if requests is None:
        try:
            import requests as _rq
        except ImportError:
            raise SystemExit("error: fetching needs `requests`.  pip install requests")
        requests = _rq
    return requests

BASE_URL  = "https://api.balldontlie.io/fifa/worldcup/v1"
# Paths are relative to the pipeline/ folder, matching the other scripts.
RAW_DIR   = "../data/xg_timeline/wc"
FEAT_PATH = "../data/xg_timeline/wc/wc_features.csv"

SEASONS = [2022, 2026]  # both have full xG; 2018 excluded (no xG data)

# Seconds to wait between API calls.
#   13.0 → safe on the GOAT free trial (5 req/min limit)
#    0.2 → paid GOAT (600 req/min)
DELAY = 13.0

WINDOWS       = [(0, 15), (15, 30), (30, 45), (45, 60), (60, 75), (75, 200)]
WINDOW_LABELS = ["0-15", "15-30", "30-45", "45-60", "60-75", "75-90plus"]


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def headers(api_key: str) -> dict:
    return {"Authorization": api_key}


def paginate(url: str, api_key: str, params=None) -> list:
    """
    Paginate a BallDontLie endpoint.
    params can be a dict or a list of (key, value) tuples — the latter
    is needed for multi-value query params like seasons[]=2022&seasons[]=2026.
    """
    results = []
    if isinstance(params, dict):
        base_params = list(params.items())
    else:
        base_params = list(params or [])
    cursor = None
    while True:
        p = base_params.copy()
        if cursor:
            p.append(("cursor", cursor))
        r = _requests().get(url, headers=headers(api_key), params=p, timeout=20)
        if r.status_code == 429:
            print("    [rate limited] waiting 60 s...")
            time.sleep(60)
            continue
        if r.status_code == 401:
            sys.exit("Error: Unauthorized. Check your API key and tier.")
        r.raise_for_status()
        data = r.json()
        results.extend(data["data"])
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor:
            break
    return results


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_all_matches(api_key: str) -> list:
    # Pass both seasons as repeated query params: seasons[]=2022&seasons[]=2026
    return paginate(f"{BASE_URL}/matches", api_key,
                    [("seasons[]", 2022), ("seasons[]", 2026), ("per_page", 100)])


def fetch_shots(api_key: str, match_id: int) -> list:
    return paginate(f"{BASE_URL}/match_shots", api_key,
                    {"match_ids[]": match_id, "per_page": 100})


# ---------------------------------------------------------------------------
# Feature computation (same logic as wc2026_shot_timeline.py)
# ---------------------------------------------------------------------------

def window_idx(minute: int) -> int:
    for i, (start, end) in enumerate(WINDOWS):
        if start <= minute < end:
            return i
    return len(WINDOWS) - 1


def compute_features(match: dict, shots: list) -> dict:
    shots_sorted = sorted(shots,
                          key=lambda s: (s.get("time_minute") or 0,
                                         s.get("time_seconds") or 0))

    home_xg_w = defaultdict(float)
    away_xg_w = defaultdict(float)

    for shot in shots_sorted:
        minute = shot.get("time_minute") or 0
        xg     = shot.get("xg") or 0.0
        w      = window_idx(minute)
        if shot.get("is_home"):
            home_xg_w[w] += xg
        else:
            away_xg_w[w] += xg

    # Largest xG swing between consecutive windows
    largest_swing       = 0.0
    largest_swing_label = ""
    for i in range(1, len(WINDOWS)):
        prev_diff = home_xg_w[i - 1] - away_xg_w[i - 1]
        curr_diff = home_xg_w[i]     - away_xg_w[i]
        swing = abs(curr_diff - prev_diff)
        if swing > largest_swing:
            largest_swing       = swing
            largest_swing_label = f"{WINDOW_LABELS[i-1]}_to_{WINDOW_LABELS[i]}"

    # xG lead changes
    lead_changes = 0
    cum_home = cum_away = 0.0
    prev_leader = None
    for shot in shots_sorted:
        xg = shot.get("xg") or 0.0
        if shot.get("is_home"):
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

    # Per-window xG for both teams and their diff
    for i, label in enumerate(WINDOW_LABELS):
        feat[f"xg_home_{label}"] = round(home_xg_w[i], 4)
        feat[f"xg_away_{label}"] = round(away_xg_w[i], 4)
        feat[f"xg_diff_{label}"] = round(home_xg_w[i] - away_xg_w[i], 4)

    return feat


# ---------------------------------------------------------------------------
# Match metadata for the CSV header row
# ---------------------------------------------------------------------------

def match_meta(match: dict) -> dict:
    home = match.get("home_team") or {}
    away = match.get("away_team") or {}
    season_year = (match.get("season") or {}).get("year", "")
    return {
        "match_id":    match["id"],
        "season_year": season_year,
        "date":        (match.get("datetime") or "")[:10],
        "stage":       (match.get("stage") or {}).get("name", ""),
        "home":        home.get("abbreviation", ""),
        "away":        away.get("abbreviation", ""),
        "home_score":  match.get("home_score"),
        "away_score":  match.get("away_score"),
        "home_pens":   match.get("home_score_penalties"),
        "away_pens":   match.get("away_score_penalties"),
        "extra_time":  match.get("has_extra_time", False),
        "penalties":   match.get("has_penalty_shootout", False),
        "total_shots": None,   # filled below
        "total_xg":    None,
    }


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

# Fixed column order for the CSV
META_COLS = [
    "match_id", "season_year", "date", "stage", "home", "away",
    "home_score", "away_score", "home_pens", "away_pens",
    "extra_time", "penalties", "total_shots", "total_xg",
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


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ALL_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n✓ Features CSV written to {path}  ({len(rows)} rows)")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_raw_file(path: str) -> dict | None:
    """Load a saved raw JSON and recompute features without an API call."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"    [skip] could not load {path}: {e}")
        return None

    match = raw["match"]
    shots = raw["shots"]

    meta                = match_meta(match)
    meta["total_shots"] = len(shots)
    meta["total_xg"]    = round(sum(s.get("xg") or 0 for s in shots), 4)

    features = compute_features(match, shots)
    return {**meta, **features}


def run(api_key: str, features_only: bool) -> None:
    os.makedirs(RAW_DIR, exist_ok=True)

    # ── Steps 1 & 2: fetch match list, then shots for each match ──────────
    # Skipped entirely under --features-only so no network is touched.
    if not features_only:
        print(f"Fetching match list for WC{SEASONS}...")
        all_matches = fetch_all_matches(api_key)
        finished = [m for m in all_matches if m.get("status_state") == "final"
                    or m.get("status") in ("completed", "final")]
        print(f"  {len(finished)} finished matches found (out of {len(all_matches)} total)")

        for i, match in enumerate(finished, 1):
            mid  = match["id"]
            year = (match.get("season") or {}).get("year", "wc")
            raw_path = os.path.join(RAW_DIR, f"wc{year}_match_{mid}.json")

            if os.path.exists(raw_path):
                home = (match.get("home_team") or {}).get("abbreviation", "?")
                away = (match.get("away_team") or {}).get("abbreviation", "?")
                print(f"  [{i:>3}/{len(finished)}] wc{year} match {mid:>4}  {home} vs {away} — already saved, skipping")
                continue

            home = (match.get("home_team") or {}).get("abbreviation", "?")
            away = (match.get("away_team") or {}).get("abbreviation", "?")
            print(f"  [{i:>3}/{len(finished)}] wc{year} match {mid:>4}  {home} vs {away} ...", end=" ", flush=True)

            shots = fetch_shots(api_key, mid)
            time.sleep(DELAY)

            payload = {"match": match, "shots": shots}
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)

            print(f"{len(shots)} shots")

    # ── Step 3: compute features from all saved raw files ─────────────────
    print("\nComputing features from saved raw files...")
    rows = []
    raw_files = sorted(
        f for f in os.listdir(RAW_DIR)
        if f.startswith("wc") and f.endswith(".json")
    )

    for fname in raw_files:
        row = process_raw_file(os.path.join(RAW_DIR, fname))
        if row:
            rows.append(row)
            mid   = row["match_id"]
            home  = row["home"]
            away  = row["away"]
            score = f"{row['home_score']}–{row['away_score']}"
            print(f"  match {mid:>4}  {home} vs {away}  {score}  "
                  f"shots={row['total_shots']}  total_xg={row['total_xg']}")

    # Sort by match_id
    rows.sort(key=lambda r: r["match_id"])
    write_csv(rows, FEAT_PATH)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch WC2022 + WC2026 shot xG timeline data and build a features CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Full run:\n"
            "  python wc_xg_timeline_fetch.py --api-key YOUR_KEY\n\n"
            "  # Recompute features only from already-downloaded files:\n"
            "  python wc_xg_timeline_fetch.py --api-key YOUR_KEY --features-only"
        ),
    )
    parser.add_argument("--api-key",      default=None,
                        help="BallDontLie API key (not needed with --features-only)")
    parser.add_argument("--features-only", action="store_true",
                        help="Skip API calls; recompute CSV from existing raw files")
    args = parser.parse_args()

    if not args.features_only and not args.api_key:
        parser.error("--api-key is required unless you pass --features-only")

    run(args.api_key, args.features_only)


if __name__ == "__main__":
    main()