#!/usr/bin/env python3
"""
ISDb — build frontend JSON for the Premier League seasons.

Reads the scored output of predict_excitingness.py (outputs/pl_excitingness.csv)
and emits one frontend-ready JSON file per season:

  data/frontend/matches_pl2526.json   (season_year 2025 -> PL 2025/26)
  data/frontend/matches_pl2627.json   (season_year 2026 -> PL 2026/27)

Each file is a flat list of matches, chronologically ordered and bucketed into
gameweeks of 10 matches (a full 20-club matchday). The site has no gameweek
field to draw on upstream, so gameweek number is inferred purely from
chronological order within the season — postponed/rearranged fixtures may
therefore land a gameweek or two off from the real BBC/PL numbering, but the
grouping is stable and good enough for the grid display.

Schema per match:
  {
    "gw": 1,
    "home": "LIV", "away": "BOU",
    "hg": 4, "ag": 2,
    "excitingness": 8.6,
    "date": "2025-08-15"
  }

Usage:
  python3 build_pl_frontend.py
  python3 build_pl_frontend.py --scores ../outputs/pl_excitingness.csv --out ../data/frontend
"""
from __future__ import annotations

import argparse
import csv
import json
import os

SEASON_LABELS = {"2025": "2526", "2026": "2627"}
MATCHES_PER_GW = 10


def load_rows(scores_path: str) -> list[dict]:
    with open(scores_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_season_file(rows: list[dict], season_year: str) -> list[dict]:
    season_rows = [r for r in rows if r["season_year"] == season_year]
    # Chronological order; match_id as a stable tiebreaker for same-day fixtures.
    season_rows.sort(key=lambda r: (r["date"], int(r["match_id"])))

    out = []
    for i, r in enumerate(season_rows):
        gw = i // MATCHES_PER_GW + 1
        out.append({
            "gw": gw,
            "home": r["home"],
            "away": r["away"],
            "hg": int(r["home_score"]),
            "ag": int(r["away_score"]),
            "excitingness": round(float(r["excitingness"]), 2),
            "date": r["date"],
        })
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--scores", default=os.path.join(here, "..", "outputs", "pl_excitingness.csv"))
    ap.add_argument("--out", default=os.path.join(here, "..", "data", "frontend"))
    args = ap.parse_args()

    rows = load_rows(args.scores)
    os.makedirs(args.out, exist_ok=True)

    for season_year, label in SEASON_LABELS.items():
        matches = build_season_file(rows, season_year)
        if not matches:
            continue
        out_path = os.path.join(args.out, f"matches_pl{label}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(matches, f, indent=2)
        n_gw = matches[-1]["gw"]
        print(f"[✓] {out_path}: {len(matches)} matches across {n_gw} gameweeks")


if __name__ == "__main__":
    main()
