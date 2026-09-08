#!/usr/bin/env python3
"""
ISDb — score Premier League matches for "excitingness".

Loads the latest PL matches (optionally fetching new ones from Understat),
builds the feature set, and applies the trained model to produce a ranked
excitingness score for every match.

The model is trained on World Cup matches, where IMDb episode ratings act as a
crowd-sourced excitingness label. It is applied to the Premier League, which has
no such ratings. Predictions are therefore *unvalidated* on PL data: trust the
ranking more than the absolute score, which is compressed toward the mean.

Usage
-----
  # score whatever is already cached on disk (no network)
  python3 predict_excitingness.py

  # fetch any new finished matches first, then score
  python3 predict_excitingness.py --fetch

  # fetch only a specific season, write somewhere else
  python3 predict_excitingness.py --fetch --season 2627 -o out/latest.csv

  # score, and show the 20 most exciting
  python3 predict_excitingness.py --top 20

Retraining
----------
This script trains from the labelled World Cup data on each run (it is fast —
~150 rows) unless a cached model is passed with --model. To produce a reusable
model file, run with --save-model PATH.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import rankdata
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RIDGE_ALPHA = 30.0
BIG_XG_THRESH = 0.30          # a shot with xG >= this is a "big chance"
LATE_MINUTE = 75              # "late" for chasing features
EXCLUDE_EXTRA_TIME = True     # WC extra-time matches distort the 75-90+ window

WINDOWS = ["0-15", "15-30", "30-45", "45-60", "60-75", "75-90plus"]

# The shipped feature set. See the notebook's model-comparison section: this
# beat every alternative on combined WC calibration + rank and the PL check.
FEATURE_SET = [
    "total_goals", "goal_diff_abs", "xg_absdiff_30-45", "xg_absdiff_0-15",
    "chasing_xg", "final5_swing_count", "big_chances_60-75", "xg_absdiff_75-90plus",
    "avg_strength", "gap_strength", "upset",
]

# Team strength. WC = FIFA world ranking just before the tournament;
# PL = preseason expected finish. Different scales, so both are converted to a
# within-competition percentile before use — that is what lets the feature
# transfer from the World Cup to the Premier League.
FIFA_RANK = {
    2022: {"BRA":1,"BEL":2,"ARG":3,"FRA":4,"ENG":5,"ESP":7,"NED":8,"POR":9,"DEN":10,"GER":11,
           "CRO":12,"MEX":13,"URU":14,"SUI":15,"USA":16,"SEN":18,"WAL":19,"IRN":20,"SRB":21,
           "MAR":22,"JPN":24,"POL":26,"KOR":28,"TUN":30,"CRC":31,"AUS":38,"CAN":41,"CAM":43,
           "ECU":44,"QAT":50,"KSA":51,"GHA":61},
    2026: {"ARG":1,"ESP":2,"FRA":3,"ENG":4,"BRA":5,"POR":6,"NED":7,"BEL":8,"CRO":9,"GER":10,
           "MAR":11,"MEX":13,"COL":14,"URU":15,"SUI":16,"USA":17,"IRN":18,"JPN":19,"SEN":20,
           "KOR":23,"AUT":24,"ECU":25,"TUR":26,"AUS":27,"ALG":28,"NOR":29,"PAN":30,"CAN":30,
           "SWE":31,"EGY":34,"SCO":36,"PAR":39,"TUN":40,"CZE":41,"CIV":42,"UZB":50,"QAT":51,
           "COD":57,"IRQ":58,"KSA":60,"RSA":61,"JOR":63,"CPV":68,"GHA":72,"BIH":74,"CUW":82,
           "HAI":84,"NZL":86},
}
# PL expected finish (1 = predicted champion). Update each season.
PL_EXPECTED = {
    2025: {"LIV":1,"ARS":2,"MCI":3,"CHE":4,"AVL":5,"NEW":6,"CRY":7,"BRI":8,"BOU":9,"BRE":10,
           "NOT":11,"MUN":12,"EVE":13,"TOT":14,"FLH":15,"WHU":16,"WOL":17,"BUR":18,"LED":19,"SUN":20},
    2026: {"ARS":1,"MCI":2,"LIV":3,"CHE":4,"AVL":5,"NEW":6,"MUN":7,"BRI":8,"BRE":9,"TOT":10,
           "EVE":11,"BOU":12,"CRY":13,"NOT":14,"LED":15,"HUL":16,"SUN":17,"IPS":18,"COV":19,"FLH":20},
}
DEFAULT_STRENGTH_PCT = 0.6    # used for a team with no ranking entry (e.g. promoted side)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def find_data_dir(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not (p / "xg_timeline").exists():
            sys.exit(f"error: {p} does not look like the data/ directory")
        return p
    here = Path(__file__).resolve().parent
    for cand in [Path("data"), Path("../data"), here / "data", here / "../data",
                 here.parent / "data"]:
        if (cand / "xg_timeline").exists():
            return cand.resolve()
    sys.exit("error: could not locate data/ — pass --data-dir")


def find_pipeline_dir(data_dir: Path) -> Path | None:
    for cand in [data_dir.parent / "pipeline", Path("pipeline"), Path("../pipeline")]:
        if cand.exists():
            return cand.resolve()
    return None


# ---------------------------------------------------------------------------
# Shot parsing (WC = BallDontLie, PL = Understat)
# ---------------------------------------------------------------------------

def wc_shots(raw: dict) -> list[dict]:
    return [{"min": s.get("time_minute") or 0,
             "sec": s.get("time_seconds") or 0,
             "added": s.get("added_time") or 0,
             "team": "H" if s.get("is_home") else "A",
             "xg": s.get("xg") or 0.0,
             "goal": s.get("shot_type") == "goal",
             "own": s.get("goal_type") == "own"}
            for s in raw["shots"]]


def pl_shots(raw: dict) -> list[dict]:
    out = []
    for side, shots in (("H", raw.get("shots_h", [])), ("A", raw.get("shots_a", []))):
        for s in shots:
            res = s.get("result")
            out.append({"min": int(float(s.get("minute") or 0)),
                        "sec": 0, "added": 0, "team": side,
                        "xg": float(s.get("xG") or 0.0),
                        "goal": res in ("Goal", "OwnGoal"),
                        "own": res == "OwnGoal"})
    return out


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

def base_features(df: pd.DataFrame) -> pd.DataFrame:
    """Symmetric xG-timeline features. Home/away is meaningless at neutral World
    Cup venues, so every home/away pair collapses to sum / absolute difference."""
    f = pd.DataFrame(index=df.index)
    f["total_goals"] = df.home_score + df.away_score
    f["goal_diff_abs"] = (df.home_score - df.away_score).abs()
    f["total_shots"] = df.total_shots
    f["total_xg"] = df.total_xg
    f["xg_per_shot"] = df.total_xg / df.total_shots.replace(0, np.nan)
    f["largest_xg_swing"] = df.largest_xg_swing
    f["xg_lead_changes"] = df.xg_lead_changes
    for wdw in WINDOWS:
        h, a = df[f"xg_home_{wdw}"], df[f"xg_away_{wdw}"]
        f[f"xg_sum_{wdw}"] = h + a
        f[f"xg_absdiff_{wdw}"] = (h - a).abs()
    return f.fillna(0.0)


def shot_features(shots: list[dict]) -> dict:
    """Chasing (trailing-team) pressure, late swings, and big chances.

    `final5_swing_count` counts lead changes in the last 5 minutes plus all
    stoppage time, using a true minute (WC stores added_time separately; PL's
    minute already runs past 90)."""
    shots = sorted(shots, key=lambda s: (s["min"], s["sec"]))
    gh = ga = 0
    chase_xg = 0.0
    final5_swings = 0
    big60 = 0
    for s in shots:
        shooter, opp = (gh, ga) if s["team"] == "H" else (ga, gh)
        if shooter < opp:                       # this team is behind
            chase_xg += s["xg"]
        if s["xg"] >= BIG_XG_THRESH and 60 <= s["min"] < 75:
            big60 += 1
        if s["goal"]:
            scorer = ("A" if s["team"] == "H" else "H") if s["own"] else s["team"]
            before = (gh > ga) - (gh < ga)
            if scorer == "H":
                gh += 1
            else:
                ga += 1
            after = (gh > ga) - (gh < ga)
            if s["min"] + s["added"] >= 85 and after != before:
                final5_swings += 1
    return {"chasing_xg": round(chase_xg, 4),
            "final5_swing_count": final5_swings,
            "big_chances_60-75": big60}


def strength_features(row, pct_map: dict) -> dict:
    ph = pct_map.get(row.home, DEFAULT_STRENGTH_PCT)
    pa = pct_map.get(row.away, DEFAULT_STRENGTH_PCT)
    fav, dog = min(ph, pa), max(ph, pa)
    gap = dog - fav
    if row.home_score == row.away_score:
        dog_result = 0.5
    else:
        home_won = row.home_score > row.away_score
        fav_is_home = ph <= pa
        dog_result = 0.0 if (home_won == fav_is_home) else 1.0
    return {"avg_strength": (ph + pa) / 2,   # match quality (lower = bigger match)
            "gap_strength": gap,             # mismatch size
            "upset": gap * dog_result}       # David-vs-Goliath payoff


def pct_map(rank_dict: dict) -> dict:
    codes = list(rank_dict)
    pos = np.array([rank_dict[c] for c in codes])
    return dict(zip(codes, rankdata(pos, method="average") / len(codes)))


def build_matrix(df: pd.DataFrame, shot_lookup, strength_maps) -> pd.DataFrame:
    base = base_features(df).reset_index(drop=True)
    sf = pd.DataFrame([shot_lookup(r) for _, r in df.iterrows()]).reset_index(drop=True)
    st = pd.DataFrame([
        strength_features(r, strength_maps.get(int(r.season_year), {}))
        for _, r in df.iterrows()
    ]).reset_index(drop=True)
    return pd.concat([base, sf, st], axis=1)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def ensure_wc_labels(data_dir: Path, pipeline_dir: Path | None) -> pd.DataFrame:
    """Load the labelled WC training set, building it from raw files if needed."""
    lab_path = data_dir / "xg_timeline/wc/wc_labelled.csv"
    feat_path = data_dir / "xg_timeline/wc/wc_features.csv"

    if not lab_path.exists():
        if not feat_path.exists():
            if not pipeline_dir:
                sys.exit(f"error: {feat_path} missing and pipeline/ not found to rebuild it")
            print("wc_features.csv missing — rebuilding from cached shot JSON ...")
            import subprocess
            subprocess.run([sys.executable, "wc_xg_timeline_fetch.py", "--features-only"],
                           cwd=str(pipeline_dir), check=True)
        if not pipeline_dir:
            sys.exit(f"error: {lab_path} missing and pipeline/ not found to build it")
        print("wc_labelled.csv missing — joining features to IMDb ratings ...")
        sys.path.insert(0, str(pipeline_dir))
        from match_stats_fetch import title_to_codes      # reuse the repo's matcher

        alias = {"CAM": "CMR"}                             # BallDontLie -> IMDb code
        title_fix = {"Cura\u00e7ao": "CUW"}                # accent-stripping edge case

        pair = {}
        for fname, yr in [("wc2022.json", 2022), ("wc2026.json", 2026)]:
            path = data_dir / "imdb" / fname
            if not path.exists():
                continue
            for ep in json.load(open(path))["episodes"]:
                title = ep.get("title", "")
                codes = title_to_codes(title)
                for name, code in title_fix.items():
                    if name in title and code not in codes:
                        codes.append(code)
                if len(codes) == 2:
                    pair[(yr, frozenset(codes))] = (ep.get("rating"), ep.get("votes"))

        wc = pd.read_csv(feat_path)
        got = [pair.get((int(r.season_year),
                         frozenset([alias.get(r.home, r.home), alias.get(r.away, r.away)])),
                        (np.nan, np.nan)) for _, r in wc.iterrows()]
        wc["imdb_rating"] = [g[0] for g in got]
        wc["imdb_votes"] = [g[1] for g in got]
        wc = wc.dropna(subset=["imdb_rating"])
        wc.to_csv(lab_path, index=False)
        print(f"  built wc_labelled.csv: {len(wc)} labelled matches")

    lab = pd.read_csv(lab_path)
    if EXCLUDE_EXTRA_TIME and "extra_time" in lab.columns:
        n0 = len(lab)
        lab = lab[~lab.extra_time].reset_index(drop=True)
        print(f"training matches: {n0} -> {len(lab)} (extra-time matches excluded)")
    else:
        print(f"training matches: {len(lab)}")
    return lab


def fetch_latest(pipeline_dir: Path | None, season: str | None) -> None:
    """Fetch any new finished PL matches from Understat (resumable — skips cached).

    `pl_xg_timeline_fetch.py` fetches every season in its SEASONS dict. To limit
    it to one season we temporarily narrow that dict via an env var the script
    does not read, so instead we filter by invoking it and letting it skip
    already-cached matches — cheap, since only new matches hit the network."""
    if not pipeline_dir:
        sys.exit("error: --fetch needs the pipeline/ directory")
    import subprocess
    cmd = [sys.executable, "pl_xg_timeline_fetch.py"]
    if season:
        print(f"note: fetching all configured seasons; {season} will be included "
              f"(cached matches are skipped, so this is cheap)")
    print(f"fetching latest PL matches via {pipeline_dir/'pl_xg_timeline_fetch.py'} ...")
    r = subprocess.run(cmd, cwd=str(pipeline_dir))
    if r.returncode != 0:
        sys.exit("error: fetch failed (is `jeke-understat-scrapper` installed?)")


def load_pl(data_dir: Path) -> tuple[pd.DataFrame, dict]:
    feat = data_dir / "xg_timeline/pl/pl_features.csv"
    if not feat.exists():
        sys.exit(f"error: {feat} not found — run with --fetch first")
    pl = pd.read_csv(feat)
    cache = {}
    missing = []
    for mid in pl.match_id:
        hits = glob.glob(str(data_dir / f"xg_timeline/pl/pl*_match_{mid}.json"))
        if hits:
            cache[mid] = pl_shots(json.load(open(hits[0])))
        else:
            missing.append(mid)
    if missing:
        print(f"warning: {len(missing)} matches have no raw shot file; "
              f"their shot-derived features will be zero")
        for mid in missing:
            cache[mid] = []
    return pl, cache


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Score Premier League matches for excitingness.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true",
                    help="fetch new finished PL matches from Understat before scoring")
    ap.add_argument("--season", default=None,
                    help="season label to fetch (e.g. 2627); default fetches all configured")
    ap.add_argument("--data-dir", default=None, help="path to data/ (auto-detected)")
    ap.add_argument("-o", "--out", default="pl_excitingness_latest.csv",
                    help="output CSV path")
    ap.add_argument("--top", type=int, default=15, help="how many to print (0 = none)")
    ap.add_argument("--save-model", default=None, help="also save the fitted model to PATH")
    args = ap.parse_args()

    data_dir = find_data_dir(args.data_dir)
    pipeline_dir = find_pipeline_dir(data_dir)
    print(f"data dir: {data_dir}")

    if args.fetch:
        fetch_latest(pipeline_dir, args.season)

    # ---- train on the World Cup -------------------------------------------
    lab = ensure_wc_labels(data_dir, pipeline_dir)
    wc_cache = {}
    for _, r in lab.iterrows():
        p = data_dir / f"xg_timeline/wc/wc{int(r.season_year)}_match_{r.match_id}.json"
        wc_cache[r.match_id] = wc_shots(json.load(open(p))) if p.exists() else []

    wc_maps = {yr: pct_map(d) for yr, d in FIFA_RANK.items()}
    X_wc = build_matrix(lab, lambda r: shot_features(wc_cache[r.match_id]), wc_maps)

    y = lab.imdb_rating.values
    w = (lab.imdb_votes.astype(float) / lab.imdb_votes.mean()).values   # vote-weighted

    model = Pipeline([("scale", StandardScaler()),
                      ("ridge", Ridge(alpha=RIDGE_ALPHA))])
    model.fit(X_wc[FEATURE_SET].values, y, ridge__sample_weight=w)
    print(f"model fitted on {len(lab)} matches, {len(FEATURE_SET)} features")

    if args.save_model:
        import joblib
        joblib.dump({"model": model, "features": FEATURE_SET}, args.save_model)
        print(f"saved model -> {args.save_model}")

    # ---- score the Premier League -----------------------------------------
    pl, pl_cache = load_pl(data_dir)
    pl_maps = {yr: pct_map(d) for yr, d in PL_EXPECTED.items()}
    X_pl = build_matrix(pl, lambda r: shot_features(pl_cache[r.match_id]), pl_maps)
    pred = np.clip(model.predict(X_pl[FEATURE_SET].values), 1.0, 10.0)

    out = pl[["match_id", "season_year", "date", "home", "away",
              "home_score", "away_score", "total_xg"]].copy()
    out["total_goals"] = pl.home_score + pl.away_score
    for col in ["chasing_xg", "final5_swing_count", "big_chances_60-75", "upset"]:
        out[col] = X_pl[col].values
    out["excitingness"] = pred.round(2)
    out = out.sort_values("excitingness", ascending=False).reset_index(drop=True)
    out.insert(0, "rank", out.index + 1)
    out.to_csv(args.out, index=False)

    print(f"\nscored {len(out)} matches  (mean {pred.mean():.2f}, std {pred.std():.2f})")
    print(f"wrote {args.out}")

    if args.top:
        print(f"\nMost exciting ({args.top}):")
        show = out.head(args.top)
        for _, r in show.iterrows():
            print(f"  {r['rank']:>3}. {r.excitingness:5.2f}  "
                  f"{r.home} {int(r.home_score)}-{int(r.away_score)} {r.away}"
                  f"   xG {r.total_xg:.1f}  {r.date}")

    print("\nNote: trained on World Cup ratings and applied to the Premier League, "
          "which has no excitingness labels. Predictions are unvalidated here — "
          "the ranking is more trustworthy than the absolute score.")


if __name__ == "__main__":
    main()
