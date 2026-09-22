#!/usr/bin/env python3
"""
migrate_existing_data.py
-------------------------
One-time backfill: loads everything currently sitting in data/, outputs/
and teams.json into Postgres (DATABASE_URL), so the DB starts out exactly
where the file-based pipeline left off. Run this once, right after
`psql ... -f db/schema.sql`, before you start relying on the DB-backed
pipeline scripts.

Safe to re-run: every load is an upsert, so running it twice just
re-syncs whatever's changed on disk.

Usage:
    export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
    pip install -r db/requirements.txt
    python3 migrate_existing_data.py                 # everything found
    python3 migrate_existing_data.py --seed-only      # just teams.json + schedule
    python3 migrate_existing_data.py --data-dir path/to/data --root path/to/repo
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
import db  # noqa: E402


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Reference data (hand-curated, small)
# ---------------------------------------------------------------------------

def migrate_teams(root: Path) -> None:
    path = root / "teams.json"
    if not path.exists():
        print(f"skip teams: {path} not found")
        return
    groups = load_json(path)
    rows = []
    for group_letter, teams in groups.items():
        for t in teams:
            rows.append({
                "tournament": "wc2026",
                "group_name": group_letter,
                "code": t["code"],
                "name": t["name"],
                "color_1": t.get("c1"),
                "color_2": t.get("c2"),
            })
    n = db.upsert_rows("wc_teams", rows, ["tournament", "code"])
    print(f"wc_teams: {n} rows from {path}")


def migrate_schedule(data_dir: Path) -> None:
    path = data_dir / "schedule" / "schedule_matches.json"
    if not path.exists():
        print(f"skip wc_schedule: {path} not found")
        return
    data = load_json(path)
    matches = data.get("matches", data)
    rows = [{
        "match_id": m.get("match_id"),
        "date": m.get("date"),
        "time_et": m.get("time_et"),
        "stage": m.get("stage"),
        "group_name": m.get("group"),
        "stadium": m.get("stadium"),
        "city": m.get("city"),
        "country_played": m.get("country_played"),
        "home": m.get("home"),
        "away": m.get("away"),
    } for m in matches]
    n = db.upsert_rows("wc_schedule", rows, ["match_id"])
    print(f"wc_schedule: {n} rows from {path}")


# ---------------------------------------------------------------------------
# IMDb ratings
# ---------------------------------------------------------------------------

def migrate_imdb(data_dir: Path) -> None:
    imdb_dir = data_dir / "imdb"
    if not imdb_dir.exists():
        print(f"skip imdb_ratings: {imdb_dir} not found")
        return
    total = 0
    for path in sorted(imdb_dir.glob("*.json")):
        data = load_json(path)
        series_title_id = data.get("title_id")
        episodes = data.get("episodes", data) if isinstance(data, dict) else data
        rows = [{
            "episode_id": ep.get("id"),
            "series_title_id": series_title_id,
            "title": ep.get("title"),
            "season": ep.get("season"),
            "episode": ep.get("episode"),
            "year": ep.get("year"),
            "month": ep.get("month"),
            "day": ep.get("day"),
            "rating": ep.get("rating"),
            "votes": ep.get("votes"),
            "url": ep.get("url"),
        } for ep in episodes if ep.get("id")]
        n = db.upsert_rows("imdb_ratings", rows, ["episode_id"])
        total += n
        print(f"imdb_ratings: {n} rows from {path}")
    print(f"imdb_ratings: {total} total")


# ---------------------------------------------------------------------------
# Raw per-match JSON caches
# ---------------------------------------------------------------------------

_WC_KEY_RE = re.compile(r"^wc(?P<year>\d{4})_match_(?P<mid>\d+)$")
_PL_KEY_RE = re.compile(r"^pl(?P<season>\d+)_match_(?P<mid>\d+)$")
_SOFA_KEY_RE = re.compile(r"^(?P<comp>[a-z0-9]+)_(?P<mid>\d+)$")


def migrate_raw_cache_dir(dir_path: Path, source: str, key_regex: re.Pattern) -> int:
    if not dir_path.exists():
        print(f"skip {source}: {dir_path} not found")
        return 0
    n = 0
    for path in sorted(dir_path.glob("*.json")):
        cache_key = path.stem
        payload = load_json(path)
        m = key_regex.match(cache_key)
        match_id = int(m.group("mid")) if m else None
        season_year = None
        if m and "year" in m.groupdict() and m.group("year"):
            season_year = int(m.group("year"))
        db.cache_put(source, cache_key, payload, match_id=match_id, season_year=season_year)
        n += 1
    print(f"{source}: {n} raw payloads from {dir_path}")
    return n


def migrate_raw_caches(data_dir: Path) -> None:
    migrate_raw_cache_dir(data_dir / "match_stats" / "raw", "sofascore_match", _SOFA_KEY_RE)
    migrate_raw_cache_dir(data_dir / "xg_timeline" / "pl", "xg_pl", _PL_KEY_RE)
    migrate_raw_cache_dir(data_dir / "xg_timeline" / "wc", "xg_wc", _WC_KEY_RE)


# ---------------------------------------------------------------------------
# Feature CSVs
# ---------------------------------------------------------------------------

def migrate_csv(path: Path, table: str, key_cols: list[str]) -> None:
    if not path.exists():
        print(f"skip {table}: {path} not found")
        return
    rows = load_csv(path)
    n = db.upsert_rows(table, rows, key_cols)
    print(f"{table}: {n} rows from {path}")


def migrate_features(data_dir: Path) -> None:
    migrate_csv(data_dir / "xg_timeline" / "pl" / "pl_features.csv", "pl_xg_features", ["match_id"])
    migrate_csv(data_dir / "xg_timeline" / "wc" / "wc_features.csv", "wc_xg_features", ["match_id"])
    migrate_csv(data_dir / "xg_timeline" / "wc" / "wc_labelled.csv", "wc_labelled", ["match_id"])
    # match_stats_fetch.py's own CSV, if it's ever been run (may not exist yet
    # in a repo that has only used Sofascore for the WC2026 frontend so far)
    for csv_path in sorted((data_dir / "match_stats").glob("*_match_stats.csv")) if (data_dir / "match_stats").exists() else []:
        migrate_csv(csv_path, "match_features", ["event_id"])


# ---------------------------------------------------------------------------
# predict_excitingness.py output + frontend JSON
# ---------------------------------------------------------------------------

def migrate_excitingness(root: Path) -> None:
    path = root / "outputs" / "pl_excitingness.csv"
    migrate_csv(path, "pl_excitingness", ["match_id"])


def migrate_frontend(data_dir: Path) -> None:
    frontend_dir = data_dir / "frontend"
    if not frontend_dir.exists():
        print(f"skip frontend_matches: {frontend_dir} not found")
        return

    wc_path = frontend_dir / "matches_wc2026.json"
    if wc_path.exists():
        matches = load_json(wc_path)
        rows = [{
            "competition": "wc2026",
            "match_id": m.get("match_id"),
            "home": m.get("home"), "away": m.get("away"),
            "hg": m.get("hg"), "ag": m.get("ag"),
            "et_hg": m.get("et_hg"), "et_ag": m.get("et_ag"),
            "pens_hg": m.get("pens_hg"), "pens_ag": m.get("pens_ag"),
            "source": m.get("source"),
            "date": m.get("date"), "time_et": m.get("time_et"),
            "stage": m.get("stage"), "group_name": m.get("group"),
            "stadium": m.get("stadium"), "city": m.get("city"),
            "country_played": m.get("country_played"),
            "imdb_score": m.get("imdb_score"),
            "oneline_comment": m.get("oneline_comment"),
        } for m in matches]
        n = db.upsert_rows("frontend_matches", rows, ["competition", "match_id"])
        print(f"frontend_matches (wc2026): {n} rows from {wc_path}")

    for season_key, fname in [("pl2526", "matches_pl2526.json"), ("pl2627", "matches_pl2627.json")]:
        path = frontend_dir / fname
        if not path.exists():
            continue
        matches = load_json(path)
        rows = [{
            "competition": season_key,
            "match_id": m.get("match_id") or m.get("id"),  # older exports may lack match_id
            "home": m.get("home"), "away": m.get("away"),
            "hg": m.get("hg"), "ag": m.get("ag"),
            "gw": m.get("gw"),
            "date": m.get("date"),
            "excitingness": m.get("excitingness"),
            "models": m.get("models"),
        } for m in matches]
        # PL frontend files, as built by build_pl_frontend.py, don't carry a
        # stable match_id today — see MIGRATION.md's note on that script.
        # Rows without one can't be upserted against a (competition, match_id)
        # key, so they're skipped here rather than guessed at.
        rows = [r for r in rows if r["match_id"] is not None]
        n = db.upsert_rows("frontend_matches", rows, ["competition", "match_id"])
        print(f"frontend_matches ({season_key}): {n} rows from {path}")


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--data-dir", default=None, help="path to data/ (default: <root>/data)")
    ap.add_argument("--seed-only", action="store_true", help="only load teams.json + schedule")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    data_dir = Path(args.data_dir).resolve() if args.data_dir else root / "data"

    print(f"root:     {root}")
    print(f"data dir: {data_dir}\n")

    migrate_teams(root)
    migrate_schedule(data_dir)

    if args.seed_only:
        return

    migrate_imdb(data_dir)
    migrate_raw_caches(data_dir)
    migrate_features(data_dir)
    migrate_excitingness(root)
    migrate_frontend(data_dir)

    print("\ndone.")


if __name__ == "__main__":
    main()
