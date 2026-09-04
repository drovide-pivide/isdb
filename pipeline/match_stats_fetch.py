"""
match_stats_fetch.py
------------------
Single source of truth for match data across WC2022, WC2026 and the Premier
League. Replaces update_schedule.py, update_matches.py, scrape_wc2026_espn.py
and manual_score.py.

Uses Sofascore's internal JSON API directly (plain HTTP, no Playwright), which
is far faster and less brittle than driving a browser.

Per match it fetches four endpoints:
  /event/{id}              → scores, status, venue, round, extra time, penalties
  /event/{id}/statistics   → possession, shots, corners, fouls, cards, xG, big chances
  /event/{id}/incidents    → goals and cards with exact minute
  /event/{id}/graph        → attack momentum, one value per minute

Outputs
-------
  data/match_stats/raw/{comp}_{id}.json    raw payload per match (resumable cache)
  data/frontend/matches_wc2026.json        SAME schema the website already consumes
  data/match_stats/{comp}_match_stats.csv  ML features (goals, cards, fouls,
                                           momentum, score state, shot quality)

Frontend compatibility
----------------------
matches_wc2026.json keeps the exact field set the existing HTML expects:
  home, away, hg, ag, et_hg, et_ag, pens_hg, pens_ag, source, match_id, date,
  time_et, stage, group, stadium, city, country_played, imdb_score,
  oneline_comment

Match identity (match_id 1..104, stadium, city, group, time_et) still comes
from data/schedule/schedule_matches.json, which is hand-curated static
reference data rather than a fetched source. Sofascore supplies the scores.
imdb_score comes from data/imdb/wc2026.json, and any existing
oneline_comment in the current frontend file is preserved.

Usage
-----
  pip install requests

  # WC2026: fetch from Sofascore and rebuild the website JSON
  python match_stats_fetch.py --competition wc2026

  # WC2022 and PL (ML features only, no website output)
  python match_stats_fetch.py --competition wc2022
  python match_stats_fetch.py --competition pl2025
  python match_stats_fetch.py --competition pl2627

  # Rebuild outputs from the cache without any network calls
  python match_stats_fetch.py --competition wc2026 --features-only

  # Preview without writing the website JSON
  python match_stats_fetch.py --competition wc2026 --dry-run
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict

import requests

API = "https://api.sofascore.com/api/v1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}

# Paths are relative to the pipeline/ folder, matching the other scripts.
RAW_DIR = "../data/match_stats/raw"
DELAY   = 1.2   # seconds between matches — be polite

# Sofascore unique-tournament ids
COMPETITIONS = {
    "wc2026": {"tournament": 16, "year": "2026",      "label": "World Cup 2026"},
    "wc2022": {"tournament": 16, "year": "2022",      "label": "World Cup 2022"},
    "pl2025": {"tournament": 17, "year": "25/26",     "label": "Premier League 25/26"},
    "pl2627": {"tournament": 17, "year": "26/27",     "label": "Premier League 26/27"},
}

# Paths for the WC2026 website pipeline
SCHEDULE_PATH = "../data/schedule/schedule_matches.json"
IMDB_PATH     = "../data/imdb/wc2026.json"
FRONTEND_PATH = "../data/frontend/matches_wc2026.json"


# ===========================================================================
# HTTP
# ===========================================================================

def get_json(path: str, retries: int = 3):
    """GET an API path. Returns None on 404 (endpoint absent for that match)."""
    url = f"{API}{path}"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"    [rate limited] waiting {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                print(f"    [warn] {path} failed: {e}")
                return None
            time.sleep(3 * (attempt + 1))
    return None


# ===========================================================================
# Discovery: season id, then every event in that season
# ===========================================================================

def find_season_id(tournament_id: int, year: str):
    data = get_json(f"/unique-tournament/{tournament_id}/seasons")
    if not data:
        return None
    for s in data.get("seasons", []):
        if s.get("year") == year:
            return s["id"]
    print(f"  [warn] season '{year}' not found. Available: "
          f"{[s.get('year') for s in data.get('seasons', [])][:12]}")
    return None


def list_events(tournament_id: int, season_id: int) -> list:
    """Page through every finished event in a season."""
    events, page = [], 0
    while True:
        data = get_json(
            f"/unique-tournament/{tournament_id}/season/{season_id}/events/last/{page}"
        )
        if not data or not data.get("events"):
            break
        events.extend(data["events"])
        if not data.get("hasNextPage"):
            break
        page += 1
        time.sleep(0.4)
    # De-duplicate, keep only finished matches
    seen, out = set(), []
    for e in events:
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        if (e.get("status") or {}).get("type") == "finished":
            out.append(e)
    return out


def fetch_match(event_id: int) -> dict:
    """Fetch all four endpoints for one match."""
    detail = get_json(f"/event/{event_id}")
    return {
        "event":      (detail or {}).get("event", {}),
        "statistics": (get_json(f"/event/{event_id}/statistics") or {}).get("statistics", []),
        "incidents":  (get_json(f"/event/{event_id}/incidents")  or {}).get("incidents", []),
        "graph":      (get_json(f"/event/{event_id}/graph")      or {}).get("graphPoints", []),
    }


# ===========================================================================
# Parsing helpers
# ===========================================================================

def stat_lookup(statistics: list) -> dict:
    """
    Flatten Sofascore's statistics blocks into {stat_name: (home, away)}.
    Only the ALL period is used; values are kept as raw strings.
    """
    out = {}
    for period in statistics:
        if period.get("period") != "ALL":
            continue
        for group in period.get("groups", []):
            for item in group.get("statisticsItems", []):
                name = (item.get("name") or "").strip()
                if name:
                    out[name] = (item.get("home"), item.get("away"))
    return out


def to_num(value, default=0.0) -> float:
    """Parse '58%', '12', '1.85', '5/8' → float. Returns default on failure."""
    if value is None:
        return default
    s = str(value).strip().replace("%", "")
    if "/" in s:                      # e.g. '5/8' → take the numerator
        s = s.split("/")[0]
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else default


def get_stat(stats: dict, *names, default=0.0):
    """Look up a stat by any of several possible Sofascore labels."""
    for n in names:
        if n in stats:
            h, a = stats[n]
            return to_num(h, default), to_num(a, default)
    return default, default


def parse_incidents(incidents: list, home_id: int) -> dict:
    """
    Walk the incident feed and pull out goals and cards with their minute,
    then derive score-state features from the goal sequence.
    """
    goals, cards = [], []

    for inc in incidents:
        itype  = inc.get("incidentType")
        minute = inc.get("time")
        if minute is None:
            continue
        minute += inc.get("addedTime", 0) or 0
        is_home = inc.get("isHome")

        if itype == "goal":
            goals.append({
                "minute":  minute,
                "is_home": is_home,
                "class":   inc.get("incidentClass", ""),   # regular/penalty/ownGoal
            })
        elif itype == "card":
            cards.append({
                "minute":  minute,
                "is_home": is_home,
                "class":   (inc.get("incidentClass") or "").lower(),  # yellow/red/yellowRed
            })

    goals.sort(key=lambda g: g["minute"])
    cards.sort(key=lambda c: c["minute"])

    # ---- goal-derived features -------------------------------------------
    h = a = 0
    lead_changes   = 0
    prev_leader    = "level"
    time_level     = 0
    time_home_lead = 0
    time_away_lead = 0
    last_minute    = 0
    level_after_75 = True
    home_was_behind = away_was_behind = False
    comeback = False

    for g in goals:
        span = g["minute"] - last_minute
        if prev_leader == "level":
            time_level += span
        elif prev_leader == "home":
            time_home_lead += span
        else:
            time_away_lead += span
        last_minute = g["minute"]

        # own goals credit the opposing team
        scoring_home = g["is_home"]
        if g["class"] == "ownGoal":
            scoring_home = not scoring_home
        if scoring_home:
            h += 1
        else:
            a += 1

        leader = "home" if h > a else ("away" if a > h else "level")
        if leader != "level" and prev_leader != "level" and leader != prev_leader:
            lead_changes += 1
        if leader == "home":
            away_was_behind = True
        elif leader == "away":
            home_was_behind = True
        prev_leader = leader

    # tail segment to full time
    span = max(90 - last_minute, 0)
    if prev_leader == "level":
        time_level += span
    elif prev_leader == "home":
        time_home_lead += span
    else:
        time_away_lead += span

    # was the match still level after the 75th minute?
    h2 = a2 = 0
    for g in goals:
        if g["minute"] > 75:
            break
        scoring_home = g["is_home"]
        if g["class"] == "ownGoal":
            scoring_home = not scoring_home
        if scoring_home:
            h2 += 1
        else:
            a2 += 1
    level_after_75 = (h2 == a2)

    # comeback = a team went behind and finished level or ahead
    final_leader = "home" if h > a else ("away" if a > h else "level")
    if home_was_behind and final_leader in ("home", "level"):
        comeback = True
    if away_was_behind and final_leader in ("away", "level"):
        comeback = True

    # ---- card-derived features -------------------------------------------
    reds    = [c for c in cards if "red" in c["class"]]
    yellows = [c for c in cards if c["class"] == "yellow"]
    first_red = reds[0]["minute"] if reds else None

    # was the match within one goal when the first red was shown?
    red_when_close = False
    if first_red is not None:
        hh = aa = 0
        for g in goals:
            if g["minute"] > first_red:
                break
            scoring_home = g["is_home"]
            if g["class"] == "ownGoal":
                scoring_home = not scoring_home
            if scoring_home:
                hh += 1
            else:
                aa += 1
        red_when_close = abs(hh - aa) <= 1

    goal_minutes = [g["minute"] for g in goals]

    return {
        "goals_total":        len(goals),
        "first_goal_minute":  goal_minutes[0]  if goal_minutes else None,
        "last_goal_minute":   goal_minutes[-1] if goal_minutes else None,
        "goals_last_15":      sum(1 for m in goal_minutes if m >= 75),
        "goals_first_15":     sum(1 for m in goal_minutes if m <= 15),
        "lead_changes":       lead_changes,
        "comeback":           comeback,
        "goals_penalty":      sum(1 for g in goals if g["class"] == "penalty"),
        "goals_own":          sum(1 for g in goals if g["class"] == "ownGoal"),
        "time_level":         time_level,
        "time_home_lead":     time_home_lead,
        "time_away_lead":     time_away_lead,
        "level_after_75":     level_after_75,
        "yellow_cards":       len(yellows),
        "red_cards":          len(reds),
        "first_red_minute":   first_red,
        "red_second_half":    bool(first_red and first_red > 45),
        "red_when_close":     red_when_close,
    }


def parse_momentum(graph: list) -> dict:
    """Attack momentum: positive favours home, negative favours away."""
    vals = [p.get("value", 0) for p in graph if p.get("value") is not None]
    if not vals:
        return {
            "mom_peak_home": 0.0, "mom_peak_away": 0.0,
            "mom_flips": 0, "mom_avg_abs": 0.0,
        }
    flips = sum(
        1 for i in range(1, len(vals))
        if vals[i] and vals[i-1] and (vals[i] > 0) != (vals[i-1] > 0)
    )
    return {
        "mom_peak_home": round(max((v for v in vals if v > 0), default=0), 2),
        "mom_peak_away": round(abs(min((v for v in vals if v < 0), default=0)), 2),
        "mom_flips":     flips,
        "mom_avg_abs":   round(sum(abs(v) for v in vals) / len(vals), 2),
    }


# ===========================================================================
# Feature row per match
# ===========================================================================

FEATURE_COLS = [
    "event_id", "competition", "date", "home", "away", "home_score", "away_score",
    # goals
    "goals_total", "goal_difference", "is_draw",
    "first_goal_minute", "last_goal_minute", "goals_first_15", "goals_last_15",
    "lead_changes", "comeback", "goals_penalty", "goals_own",
    # score state
    "time_level", "time_home_lead", "time_away_lead", "level_after_75",
    # xG
    "xg_home", "xg_away", "xg_total", "xg_diff",
    "loser_higher_xg", "goals_minus_xg_home", "goals_minus_xg_away",
    # shots
    "shots_home", "shots_away", "shots_total",
    "shots_on_target_home", "shots_on_target_away",
    "big_chances_home", "big_chances_away",
    "big_chances_missed_home", "big_chances_missed_away",
    "avg_xg_per_shot",
    # possession / corners
    "possession_home", "possession_away", "corners_home", "corners_away",
    # cards
    "yellow_cards", "red_cards", "first_red_minute",
    "red_second_half", "red_when_close",
    # fouls
    "fouls_home", "fouls_away", "fouls_total", "fouls_diff",
    # momentum
    "mom_peak_home", "mom_peak_away", "mom_flips", "mom_avg_abs",
]


def build_feature_row(raw: dict, competition: str) -> dict:
    event = raw["event"]
    stats = stat_lookup(raw["statistics"])

    home_team = (event.get("homeTeam") or {})
    away_team = (event.get("awayTeam") or {})
    hg = (event.get("homeScore") or {}).get("current")
    ag = (event.get("awayScore") or {}).get("current")

    inc = parse_incidents(raw["incidents"], home_team.get("id"))
    mom = parse_momentum(raw["graph"])

    xg_h, xg_a = get_stat(stats, "Expected goals", "Expected Goals", "xG")
    sh_h, sh_a = get_stat(stats, "Total shots", "Total Shots")
    st_h, st_a = get_stat(stats, "Shots on target", "Shots on Target")
    bc_h, bc_a = get_stat(stats, "Big chances")
    bm_h, bm_a = get_stat(stats, "Big chances missed")
    po_h, po_a = get_stat(stats, "Ball possession", "Ball Possession")
    co_h, co_a = get_stat(stats, "Corner kicks", "Corner Kicks", "Corners")
    fo_h, fo_a = get_stat(stats, "Fouls")

    shots_total = sh_h + sh_a
    xg_total    = xg_h + xg_a

    # did the losing side out-create the winner?
    loser_higher_xg = False
    if hg is not None and ag is not None:
        if hg > ag:
            loser_higher_xg = xg_a > xg_h
        elif ag > hg:
            loser_higher_xg = xg_h > xg_a

    ts = event.get("startTimestamp")
    date = time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else ""

    # Extra time / penalty shootout, straight from Sofascore's score object.
    # extra1/extra2 are the two ET halves; Sofascore reports ET cumulatively,
    # so extra2 already includes normal time. penalties is the shootout total.
    hs, as_ = (event.get("homeScore") or {}), (event.get("awayScore") or {})
    et_hg = hs.get("extra2") if hs.get("extra2") is not None else hs.get("extra1")
    et_ag = as_.get("extra2") if as_.get("extra2") is not None else as_.get("extra1")
    pens_hg, pens_ag = hs.get("penalties"), as_.get("penalties")

    row = {
        "event_id":    event.get("id"),
        "competition": competition,
        "date":        date,
        "home":        home_team.get("nameCode") or home_team.get("shortName") or home_team.get("name", ""),
        "away":        away_team.get("nameCode") or away_team.get("shortName") or away_team.get("name", ""),
        "home_score":  hg,
        "away_score":  ag,
        "et_hg":   et_hg,
        "et_ag":   et_ag,
        "pens_hg": pens_hg,
        "pens_ag": pens_ag,

        "goal_difference": abs((hg or 0) - (ag or 0)),
        "is_draw":         hg == ag if hg is not None else None,

        "xg_home":  round(xg_h, 4),
        "xg_away":  round(xg_a, 4),
        "xg_total": round(xg_total, 4),
        "xg_diff":  round(xg_h - xg_a, 4),
        "loser_higher_xg":     loser_higher_xg,
        "goals_minus_xg_home": round((hg or 0) - xg_h, 4),
        "goals_minus_xg_away": round((ag or 0) - xg_a, 4),

        "shots_home":  int(sh_h), "shots_away": int(sh_a),
        "shots_total": int(shots_total),
        "shots_on_target_home": int(st_h), "shots_on_target_away": int(st_a),
        "big_chances_home": int(bc_h), "big_chances_away": int(bc_a),
        "big_chances_missed_home": int(bm_h), "big_chances_missed_away": int(bm_a),
        "avg_xg_per_shot": round(xg_total / shots_total, 4) if shots_total else 0.0,

        "possession_home": po_h, "possession_away": po_a,
        "corners_home": int(co_h), "corners_away": int(co_a),

        "fouls_home":  int(fo_h), "fouls_away": int(fo_a),
        "fouls_total": int(fo_h + fo_a),
        "fouls_diff":  int(abs(fo_h - fo_a)),
    }
    row.update(inc)
    row.update(mom)
    return row


# ===========================================================================
# Fetch loop
# ===========================================================================

def fetch_competition(comp: str, features_only: bool) -> list:
    cfg = COMPETITIONS[comp]
    os.makedirs(RAW_DIR, exist_ok=True)

    if not features_only:
        print(f"\nResolving season for {cfg['label']}...")
        season_id = find_season_id(cfg["tournament"], cfg["year"])
        if not season_id:
            sys.exit(f"Could not resolve season id for {comp}. "
                     f"Check the 'year' string in COMPETITIONS.")
        print(f"  season id = {season_id}")

        print("Listing finished matches...")
        events = list_events(cfg["tournament"], season_id)
        print(f"  {len(events)} finished matches found")

        for i, ev in enumerate(events, 1):
            eid      = ev["id"]
            raw_path = os.path.join(RAW_DIR, f"{comp}_{eid}.json")

            hn = (ev.get("homeTeam") or {}).get("nameCode", "?")
            an = (ev.get("awayTeam") or {}).get("nameCode", "?")

            if os.path.exists(raw_path):
                print(f"  [{i:>3}/{len(events)}] {comp} {eid}  {hn} vs {an} — cached, skipping")
                continue

            print(f"  [{i:>3}/{len(events)}] {comp} {eid}  {hn} vs {an} ...", end=" ", flush=True)
            raw = fetch_match(eid)
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2, ensure_ascii=False)

            print(f"{len(raw['incidents'])} incidents, {len(raw['graph'])} momentum pts")
            time.sleep(DELAY)

    # Build feature rows from the cache
    print("\nComputing features from cached files...")
    rows = []
    for fname in sorted(os.listdir(RAW_DIR)):
        if not (fname.startswith(f"{comp}_") and fname.endswith(".json")):
            continue
        try:
            with open(os.path.join(RAW_DIR, fname), encoding="utf-8") as f:
                raw = json.load(f)
            rows.append(build_feature_row(raw, comp))
        except Exception as e:
            print(f"  [skip] {fname}: {e}")

    rows.sort(key=lambda r: (r["date"], r["event_id"] or 0))
    return rows


def write_features(rows: list, comp: str) -> None:
    path = f"../data/match_stats/{comp}_match_stats.csv"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FEATURE_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[✓] Features CSV     : {path}  ({len(rows)} rows)")


# ===========================================================================
# WC2026 website output — preserves the existing frontend schema exactly
# ===========================================================================

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


# ---------------------------------------------------------------------------
# IMDb title matching — reused verbatim from the original update_matches.py so
# episode-to-match resolution behaves exactly as it did before.
# ---------------------------------------------------------------------------

COUNTRY_TO_CODE: dict[str, str] = {
    "afghanistan": "AFG", "albania": "ALB", "algeria": "ALG",
    "angola": "ANG", "argentina": "ARG", "armenia": "ARM",
    "australia": "AUS", "austria": "AUT", "azerbaijan": "AZE",
    "bahrain": "BHR", "bangladesh": "BAN", "belgium": "BEL",
    "bolivia": "BOL", "bosnia": "BIH", "bosnia and herzegovina": "BIH",
    "botswana": "BOT", "brazil": "BRA", "bulgaria": "BUL",
    "burkina faso": "BFA",
    "cameroon": "CMR", "canada": "CAN", "cape verde": "CPV",
    "chile": "CHI", "china": "CHN", "colombia": "COL",
    "comoros": "COM", "congo": "CGO", "costa rica": "CRC",
    "côte d'ivoire": "CIV", "ivory coast": "CIV", "cote d'ivoire": "CIV",
    "croatia": "CRO", "cuba": "CUB", "curaçao": "CUW", "curacao": "CUW",
    "czech republic": "CZE", "czechia": "CZE",
    "dr congo": "COD", "congo dr": "COD", "democratic republic of congo": "COD",
    "denmark": "DEN", "djibouti": "DJI",
    "ecuador": "ECU", "egypt": "EGY", "el salvador": "SLV",
    "england": "ENG", "eritrea": "ERI", "ethiopia": "ETH",
    "finland": "FIN", "france": "FRA",
    "gabon": "GAB", "gambia": "GAM", "georgia": "GEO",
    "germany": "GER", "ghana": "GHA", "greece": "GRE",
    "guatemala": "GUA", "guinea": "GUI", "guinea-bissau": "GNB",
    "haiti": "HAI", "honduras": "HON", "hungary": "HUN",
    "iceland": "ISL", "india": "IND", "indonesia": "IDN",
    "iran": "IRN", "ir iran": "IRN", "iraq": "IRQ", "ireland": "IRL",
    "israel": "ISR", "italy": "ITA",
    "jamaica": "JAM", "japan": "JPN", "jordan": "JOR",
    "kenya": "KEN", "korea republic": "KOR", "south korea": "KOR",
    "korea": "KOR", "kuwait": "KUW",
    "latvia": "LVA", "lebanon": "LIB", "libya": "LBA",
    "madagascar": "MAD", "mali": "MLI", "malta": "MLT",
    "mauritania": "MTN", "mexico": "MEX", "moldova": "MDA",
    "montenegro": "MNE", "morocco": "MAR", "mozambique": "MOZ",
    "namibia": "NAM", "nepal": "NEP", "netherlands": "NED",
    "new zealand": "NZL", "nicaragua": "NCA", "nigeria": "NGA",
    "north korea": "PRK", "north macedonia": "MKD", "norway": "NOR",
    "oman": "OMA",
    "pakistan": "PAK", "palestine": "PLE", "panama": "PAN",
    "paraguay": "PAR", "peru": "PER", "philippines": "PHI",
    "poland": "POL", "portugal": "POR",
    "qatar": "QAT",
    "republic of ireland": "IRL", "romania": "ROU",
    "russia": "RUS", "rwanda": "RWA",
    "saudi arabia": "KSA", "scotland": "SCO", "senegal": "SEN",
    "serbia": "SRB", "sierra leone": "SLE", "slovakia": "SVK",
    "slovenia": "SVN", "somalia": "SOM", "south africa": "RSA",
    "spain": "ESP", "sudan": "SDN", "sweden": "SWE",
    "switzerland": "SUI", "syria": "SYR",
    "tanzania": "TAN", "thailand": "THA", "togo": "TOG",
    "trinidad and tobago": "TRI", "tunisia": "TUN",
    "turkey": "TUR", "türkiye": "TUR",
    "uganda": "UGA", "ukraine": "UKR",
    "united arab emirates": "UAE",
    "united states": "USA", "usa": "USA",
    "uruguay": "URU", "uzbekistan": "UZB",
    "venezuela": "VEN", "vietnam": "VIE",
    "wales": "WAL",
    "yemen": "YEM",
    "zambia": "ZAM", "zimbabwe": "ZIM",
}


def normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower().strip())


def title_to_codes(episode_title: str) -> list:
    body = episode_title.split(":", 1)[-1] if ":" in episode_title else episode_title
    parts = re.split(r"\s+vs\.?\s+|\s+versus\s+", body, flags=re.IGNORECASE)
    codes = []
    for part in parts:
        key = normalise(part)
        code = COUNTRY_TO_CODE.get(key)
        if not code:
            for country, c in COUNTRY_TO_CODE.items():
                if country in key or key in country:
                    code = c
                    break
        if code:
            codes.append(code)
    return codes


def build_imdb_lookups(episodes: list):
    """{frozenset(codes): rating}, {date: [ratings]} — same as the original."""
    by_pair, by_date = {}, {}
    for ep in episodes:
        rating = ep.get("rating")
        y, mo, d = ep.get("year"), ep.get("month"), ep.get("day")
        date_str = f"{y}-{mo:02d}-{d:02d}" if (y and mo and d) else ""
        codes = title_to_codes(ep.get("title", ""))
        if len(codes) == 2:
            by_pair[frozenset(codes)] = rating
        elif date_str:
            by_date.setdefault(date_str, []).append(rating)
    return by_pair, by_date




def build_frontend(rows: list, schedule_path: str, imdb_path: str,
                   frontend_path: str, dry_run: bool) -> None:
    # -- schedule: match identity and venue metadata --------------------
    if not os.path.exists(schedule_path):
        print(f"[!] Schedule not found at {schedule_path} — skipping website output")
        return
    with open(schedule_path, encoding="utf-8") as f:
        sched_data = json.load(f)
    schedule = sched_data.get("matches", sched_data)
    print(f"[✓] Schedule         : {schedule_path}  ({len(schedule)} matches)")

    # -- IMDb ratings ---------------------------------------------------
    imdb_by_pair, imdb_by_date = {}, {}
    if os.path.exists(imdb_path):
        with open(imdb_path, encoding="utf-8") as f:
            imdb_data = json.load(f)
        episodes = imdb_data.get("episodes", imdb_data) if isinstance(imdb_data, dict) else imdb_data
        imdb_by_pair, imdb_by_date = build_imdb_lookups(episodes)
        print(f"[\u2713] IMDb file        : {imdb_path}  ({len(episodes)} episodes)")
    else:
        print(f"[!] IMDb file not found at {imdb_path} — imdb_score will be null")

    # -- preserve existing comments -------------------------------------
    existing = {}
    if os.path.exists(frontend_path):
        with open(frontend_path, encoding="utf-8") as f:
            for m in json.load(f):
                existing[m.get("match_id")] = m

    # -- index Sofascore rows by (date, home, away) and by team pair -----
    by_pair = {}
    for r in rows:
        by_pair[(norm(r["home"]), norm(r["away"]))] = r

    out, matched = [], 0
    for s in schedule:
        mid  = s.get("match_id")
        home = s.get("home", "")
        away = s.get("away", "")
        prev = existing.get(mid, {})

        row = by_pair.get((norm(home), norm(away)))
        if row:
            matched += 1

        # IMDb: match by team-code pair, then fall back to a unique date
        imdb_score = prev.get("imdb_score")
        pair_key = frozenset([home, away])
        if pair_key in imdb_by_pair:
            imdb_score = imdb_by_pair[pair_key]
        elif s.get("date") in imdb_by_date and len(imdb_by_date[s["date"]]) == 1:
            imdb_score = imdb_by_date[s["date"]][0]

        out.append({
            "home": home,
            "away": away,
            "hg":   row["home_score"] if row else None,
            "ag":   row["away_score"] if row else None,
            "et_hg":   (row.get("et_hg")   if row else None) or prev.get("et_hg"),
            "et_ag":   (row.get("et_ag")   if row else None) or prev.get("et_ag"),
            "pens_hg": (row.get("pens_hg") if row else None) or prev.get("pens_hg"),
            "pens_ag": (row.get("pens_ag") if row else None) or prev.get("pens_ag"),
            "source":  "sofascore" if row else None,
            "match_id":       mid,
            "date":           s.get("date"),
            "time_et":        s.get("time_et"),
            "stage":          s.get("stage"),
            "group":          s.get("group"),
            "stadium":        s.get("stadium"),
            "city":           s.get("city"),
            "country_played": s.get("country_played"),
            "imdb_score":     imdb_score,
            "oneline_comment": prev.get("oneline_comment", ""),
        })

    scored = sum(1 for m in out if m["hg"] is not None)
    rated  = sum(1 for m in out if m["imdb_score"] is not None)
    print(f"[✓] Matched to Sofascore: {matched}/{len(out)}")
    print(f"[✓] With scores      : {scored}")
    print(f"[✓] With IMDb scores : {rated}")

    if dry_run:
        print("\n[dry-run] First 2 entries:")
        print(json.dumps(out[:2], indent=2, ensure_ascii=False))
        return

    os.makedirs(os.path.dirname(frontend_path) or ".", exist_ok=True)
    with open(frontend_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"[✓] Updated          : {frontend_path}")


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    p = argparse.ArgumentParser(
        description="Sofascore: single source for match data (website + ML features)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python match_stats_fetch.py --competition wc2026\n"
            "  python match_stats_fetch.py --competition wc2022\n"
            "  python match_stats_fetch.py --competition pl2025\n"
            "  python match_stats_fetch.py --competition wc2026 --features-only\n"
            "  python match_stats_fetch.py --competition wc2026 --dry-run"
        ),
    )
    p.add_argument("--competition", required=True, choices=sorted(COMPETITIONS),
                   help="Which competition to fetch")
    p.add_argument("--features-only", action="store_true",
                   help="Rebuild outputs from the cache, no network calls")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the website JSON preview instead of writing it")
    p.add_argument("--schedule", default=SCHEDULE_PATH)
    p.add_argument("--imdb",     default=IMDB_PATH)
    p.add_argument("--frontend", default=FRONTEND_PATH)
    args = p.parse_args()

    rows = fetch_competition(args.competition, args.features_only)
    write_features(rows, args.competition)

    # Only WC2026 drives the website
    if args.competition == "wc2026":
        print()
        build_frontend(rows, args.schedule, args.imdb, args.frontend, args.dry_run)


if __name__ == "__main__":
    main()
