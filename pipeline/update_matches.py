"""
update_matches.py
-----------------
Pipeline that:
1. Reads data/schedule/schedule_matches.json as the source of truth for matches
2. Fetches scores (hg/ag) from openfootball
3. Reads the canonical data/imdb/wc2026.json (one file, overwritten each
   pipeline run -- git history is the changelog, so we don't keep dated
   snapshot copies around)
4. Writes data/frontend/matches_wc2026.json as a plain JSON array,
   keeping all fields the HTML frontend expects:
     home, away, hg, ag, imdb_score, oneline_comment
   plus extra-time / penalty fields (None when not applicable):
     et_hg, et_ag, pens_hg, pens_ag
   plus enriched fields:
     match_id, date, time_et, stage, group, stadium, city, country_played
   plus provenance:
     source  -- "openfootball" | "manual" | null (no score yet)

Score provenance / manual overrides
------------------------------------
Every match now carries a "source" field describing where its score came
from:
  - "openfootball" : score fetched live from openfootball this run
  - "manual"        : score was hand-patched (e.g. via manual_score.py)
                        because openfootball didn't have it yet
  - null            : no score available from any source (unplayed / unresolved)

On every run, this script loads the *existing* matches_wc2026.json first. Any
match whose existing "source" is "manual" is left completely alone for its
score fields (hg/ag/et_hg/et_ag/pens_hg/pens_ag) -- openfootball is NOT
consulted for that match, so a manual patch survives future pipeline runs
until you delete/change the "source" field yourself. Once openfootball
actually publishes that result, you can just remove/change "source" for that
match (or check out the previous version of matches_wc2026.json from git
history) and rerun to let the live fetch take over again.

oneline_comment is always preserved regardless of source, same as before.

Usage:
    python3 update_matches.py
    python3 update_matches.py --dry-run
"""

import argparse
import json
import os
import re
import requests
from datetime import datetime, timezone


OPENFOOTBALL_URL = (
    "https://raw.githubusercontent.com/openfootball/"
    "worldcup.json/master/2026/worldcup.json"
)

# ---------------------------------------------------------------------------
# Team name → FIFA 3-letter code
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


def to_code(name: str) -> str | None:
    key = name.lower().strip()
    if key in COUNTRY_TO_CODE:
        return COUNTRY_TO_CODE[key]
    for country, c in COUNTRY_TO_CODE.items():
        if country in key or key in country:
            return c
    return None


def normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower().strip())


def title_to_codes(episode_title: str) -> list[str]:
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


# ---------------------------------------------------------------------------
# Scores from openfootball  →  {frozenset(home, away): {...}}
# ---------------------------------------------------------------------------

def fetch_scores() -> dict[frozenset, dict]:
    """Returns {frozenset(home, away): {"ft": (h,a), "et": (h,a) or None, "p": (h,a) or None}}"""
    print(f"  Fetching scores from openfootball...")
    r = requests.get(OPENFOOTBALL_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    scores = {}
    n_et = n_p = 0
    for m in r.json().get("matches", []):
        t1, t2 = m.get("team1", ""), m.get("team2", "")
        score_block = m.get("score", {})
        ft = score_block.get("ft")
        if not ft or re.match(r'^[WL]\d+$', t1) or re.match(r'^[WL]\d+$', t2):
            continue
        h, a = to_code(t1), to_code(t2)
        if not (h and a):
            continue
        et = score_block.get("et")
        p = score_block.get("p")
        if et:
            n_et += 1
        if p:
            n_p += 1
        scores[frozenset([h, a])] = {
            "ft": (ft[0], ft[1]),
            "et": (et[0], et[1]) if et else None,
            "p":  (p[0], p[1]) if p else None,
        }
    print(f"  Got {len(scores)} scores  ({n_et} went to extra time, {n_p} went to penalties)")
    return scores


# ---------------------------------------------------------------------------
# IMDb lookups
# ---------------------------------------------------------------------------

def build_imdb_lookups(episodes: list[dict]) -> tuple[dict, dict]:
    by_pair: dict[frozenset, float | None] = {}
    by_date: dict[str, list[float | None]] = {}
    for ep in episodes:
        title  = ep.get("title", "")
        rating = ep.get("rating")
        y, mo, d = ep.get("year"), ep.get("month"), ep.get("day")
        date_str = f"{y}-{mo:02d}-{d:02d}" if (y and mo and d) else ""
        codes = title_to_codes(title)
        if len(codes) == 2:
            by_pair[frozenset(codes)] = rating
        elif date_str:
            by_date.setdefault(date_str, []).append(rating)
    return by_pair, by_date


# ---------------------------------------------------------------------------
# Preserve existing comments + manual-override score data
# ---------------------------------------------------------------------------

def load_existing_data(matches_path: str) -> dict[frozenset, dict]:
    """
    Returns {frozenset([home, away]): existing_match_dict} for every
    already-resolved match currently in matches_wc2026.json. Used to
    preserve oneline_comment always, and to preserve hg/ag/et_*/pens_*
    whenever the existing entry's "source" == "manual".
    """
    if not os.path.exists(matches_path):
        return {}
    with open(matches_path, encoding="utf-8") as f:
        raw = json.load(f)
    existing = raw if isinstance(raw, list) else raw.get("matches", [])
    return {
        frozenset([m["home"], m["away"]]): m
        for m in existing
        if m.get("home") and m.get("away")
    }


# ---------------------------------------------------------------------------
# Build final match list
# ---------------------------------------------------------------------------

def build_matches(
    schedule: list[dict],
    scores: dict[frozenset, dict],
    by_pair: dict,
    by_date: dict,
    existing_data: dict[frozenset, dict],
) -> tuple[list[dict], int, int, int]:

    date_cursor: dict[str, int] = {}
    n_pair = n_date = n_manual = 0
    out = []

    for m in schedule:
        h, a = m.get("home", ""), m.get("away", "")
        if not (h and a):
            continue  # skip unresolved knockout slots

        pair_key = frozenset([h, a])
        date     = m.get("date", "")
        existing = existing_data.get(pair_key, {})

        # --- scores (hg / ag), plus extra time / penalties if applicable ---
        if existing.get("source") == "manual":
            # Never touch a manually-patched score -- openfootball is not
            # consulted for this match at all.
            hg, ag         = existing.get("hg"), existing.get("ag")
            et_hg, et_ag   = existing.get("et_hg"), existing.get("et_ag")
            pens_hg, pens_ag = existing.get("pens_hg"), existing.get("pens_ag")
            source = "manual"
            n_manual += 1
        else:
            score_entry = scores.get(pair_key)
            if score_entry:
                hg, ag = score_entry["ft"]
                et = score_entry["et"]
                pens = score_entry["p"]
                source = "openfootball"
            else:
                hg, ag = None, None
                et, pens = None, None
                source = None
            et_hg, et_ag = et if et else (None, None)
            pens_hg, pens_ag = pens if pens else (None, None)

        # --- imdb_score ---
        if pair_key in by_pair:
            imdb_score = by_pair[pair_key]
            n_pair += 1
        elif date in by_date:
            idx = date_cursor.get(date, 0)
            ratings = by_date[date]
            imdb_score = ratings[idx] if idx < len(ratings) else None
            date_cursor[date] = idx + 1
            if imdb_score is not None:
                n_date += 1
        else:
            imdb_score = None

        out.append({
            "home":           h,
            "away":           a,
            "hg":             hg,
            "ag":             ag,
            "et_hg":          et_hg,
            "et_ag":          et_ag,
            "pens_hg":        pens_hg,
            "pens_ag":        pens_ag,
            "source":         source,
            # enriched fields
            "match_id":       m.get("match_id"),
            "date":           date,
            "time_et":        m.get("time_et"),
            "stage":          m.get("stage"),
            "group":          m.get("group"),
            "stadium":        m.get("stadium"),
            "city":           m.get("city"),
            "country_played": m.get("country_played"),
            "imdb_score":     imdb_score,
            "oneline_comment": existing.get("oneline_comment", ""),
        })

    return out, n_pair, n_date, n_manual


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def update(
    schedule_path: str,
    matches_path: str,
    imdb_dir: str,
    prefix: str = "wc2026_",
    dry_run: bool = False,
) -> None:
    # 1. Load schedule
    with open(schedule_path, encoding="utf-8") as f:
        raw_schedule = json.load(f)
    schedule = raw_schedule if isinstance(raw_schedule, list) else raw_schedule.get("matches", [])
    print(f"[✓] Schedule loaded  : {schedule_path}  ({len(schedule)} total slots)")

    # 2. Fetch scores
    try:
        scores = fetch_scores()
    except Exception as e:
        print(f"[!] Could not fetch scores: {e} — source=null for all non-manual matches")
        scores = {}

    # 3. Canonical IMDb file (overwritten each run; git history is the log)
    imdb_path = os.path.join(imdb_dir, f"{prefix.rstrip('_')}.json")
    if not os.path.exists(imdb_path):
        raise FileNotFoundError(f"'{imdb_path}' not found -- run update_imdb.py first")
    print(f"[✓] IMDb file        : {imdb_path}")
    with open(imdb_path, encoding="utf-8") as f:
        imdb_data = json.load(f)
    episodes = imdb_data.get("episodes", imdb_data) if isinstance(imdb_data, dict) else imdb_data

    # 4. Preserve existing comments + manual overrides
    existing_data = load_existing_data(matches_path)

    # 5. Build IMDb lookups
    by_pair, by_date = build_imdb_lookups(episodes)

    # 6. Build output
    enriched, n_pair, n_date, n_manual = build_matches(
        schedule, scores, by_pair, by_date, existing_data
    )
    real_count = sum(1 for m in enriched if m["hg"] is not None)
    et_count   = sum(1 for m in enriched if m["et_hg"] is not None)
    pens_count = sum(1 for m in enriched if m["pens_hg"] is not None)
    print(f"[✓] Resolved matches : {len(enriched)}")
    print(f"[✓] With scores      : {real_count}  ({n_manual} manual, rest openfootball)")
    print(f"[✓] Went to ET       : {et_count}")
    print(f"[✓] Went to penalties: {pens_count}")
    print(f"[✓] IMDb scores found: {n_pair + n_date}  ({n_pair} by team pair, {n_date} by date)")

    if dry_run:
        print("\n[dry-run] First 3 entries:")
        print(json.dumps(enriched[:3], indent=2, ensure_ascii=False))
        return

    # 7. Write as plain array (what the HTML expects) -- git history covers rollback
    with open(matches_path, "w", encoding="utf-8") as f:
        json.dump(enriched, f, indent=2, ensure_ascii=False)
    print(f"[✓] Updated          : {matches_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build matches_wc2026.json")
    parser.add_argument("--schedule", default="../data/schedule/schedule_matches.json")
    parser.add_argument("--matches",  default="../data/frontend/matches_wc2026.json")
    parser.add_argument("--imdb-dir", default="../data/imdb/")
    parser.add_argument("--prefix",   default="wc2026_")
    parser.add_argument("--dry-run",  action="store_true")
    args = parser.parse_args()
    update(args.schedule, args.matches, args.imdb_dir, args.prefix, args.dry_run)


if __name__ == "__main__":
    main()
