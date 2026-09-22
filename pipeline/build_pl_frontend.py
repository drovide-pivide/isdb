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

"excitingness" is always the default (shipped) model's score. If the CSV also
has excitingness_<key> columns — the two alternate models predict_excitingness.py
scores for the frontend's Advanced selector, e.g. excitingness_final5swing,
excitingness_goalsonly — they're carried through under "models" using the same
key, so the site can offer a switch without needing this script to know their
names in advance. Any excitingness_<key> column found gets included
automatically; there's nothing to hardcode here when a new one is added.

Row written to the DB's frontend_matches table (competition = 'pl2526' or
'pl2627'), one per match:
  {
    "competition": "pl2526", "match_id": 28830,
    "gw": 1,
    "home": "LIV", "away": "BOU",
    "hg": 4, "ag": 2,
    "excitingness": 8.6,
    "models": {"final5swing": 8.2, "goalsonly": 7.9},
    "date": "2025-08-15"
  }
match_id is included so this can be upserted (it used to be dropped from the
JSON-file output entirely, which is fine for a flat list with no dedup key,
but a DB table needs one).

Usage:
  python3 build_pl_frontend.py                        # DB -> DB (default)
  python3 build_pl_frontend.py --scores-csv ../outputs/pl_excitingness.csv   # CSV -> DB
  python3 build_pl_frontend.py --out-json ../data/frontend   # also write the legacy JSON files
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "db"))
import db  # noqa: E402

SEASON_LABELS = {"2025": "2526", "2026": "2627"}
MATCHES_PER_GW = 10


def load_rows(scores_csv: str | None) -> list[dict]:
    """Rows from the DB's pl_excitingness table by default; pass --scores-csv
    to read the old CSV instead (e.g. for a local test run with no DB)."""
    if scores_csv:
        with open(scores_csv, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    return db.read_rows("pl_excitingness")


def alt_model_keys(rows: list[dict]) -> list[str]:
    """Any excitingness_<key> column present in the CSV, in the order they
    first appear — so a new alternate model just needs predict_excitingness.py
    to write its column; nothing here needs updating."""
    if not rows:
        return []
    return [c[len("excitingness_"):] for c in rows[0]
            if c.startswith("excitingness_")]


def build_season_file(rows: list[dict], season_year: str, competition: str,
                       model_keys: list[str]) -> list[dict]:
    season_rows = [r for r in rows if str(r["season_year"]) == str(season_year)]
    # Chronological order; match_id as a stable tiebreaker for same-day fixtures.
    season_rows.sort(key=lambda r: (str(r["date"]), int(r["match_id"])))

    out = []
    for i, r in enumerate(season_rows):
        gw = i // MATCHES_PER_GW + 1
        date_val = r["date"]
        date_str = date_val.isoformat() if hasattr(date_val, "isoformat") else date_val
        match = {
            "competition": competition,
            "match_id": int(r["match_id"]),
            "gw": gw,
            "home": r["home"],
            "away": r["away"],
            "hg": int(r["home_score"]),
            "ag": int(r["away_score"]),
            "excitingness": round(float(r["excitingness"]), 2),
            "date": date_str,
        }
        models = {}
        for key in model_keys:
            val = r.get(f"excitingness_{key}", "")
            if val not in ("", None):
                models[key] = round(float(val), 2)
        if models:
            match["models"] = models
        out.append(match)
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--scores-csv", default=None,
                    help="read scores from this CSV instead of the DB's pl_excitingness table")
    ap.add_argument("--out-json", default=None,
                    help="also write the legacy per-season JSON files to this directory "
                         "(only needed if the frontend still reads static JSON)")
    args = ap.parse_args()

    rows = load_rows(args.scores_csv)
    model_keys = alt_model_keys(rows)
    if model_keys:
        print(f"[i] found alternate model columns: {', '.join(model_keys)}")

    if args.out_json:
        os.makedirs(args.out_json, exist_ok=True)

    for season_year, label in SEASON_LABELS.items():
        competition = f"pl{label}"
        matches = build_season_file(rows, season_year, competition, model_keys)
        if not matches:
            continue

        n = db.upsert_rows("frontend_matches", matches, ["competition", "match_id"])
        n_gw = matches[-1]["gw"]
        print(f"[✓] frontend_matches ({competition}): {n} matches across {n_gw} gameweeks")

        if args.out_json:
            # legacy shape: no competition/match_id, matches the old file schema exactly
            legacy = [{k: v for k, v in m.items() if k not in ("competition", "match_id")}
                      for m in matches]
            out_path = os.path.join(args.out_json, f"matches_pl{label}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(legacy, f, indent=2)
            print(f"    also wrote {out_path}")


if __name__ == "__main__":
    main()
