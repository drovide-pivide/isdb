"""
update_schedule.py
------------------
Fetches the openfootball WC2026 JSON (plain HTTP, no browser needed) and
fills home/away team codes into data/schedule/schedule_matches.json.

Source:
  https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json

Setup:
    pip install requests   (that's all)

Usage:
    python3 update_schedule.py
    python3 update_schedule.py --schedule ../data/schedule/schedule_matches.json
    python3 update_schedule.py --dry-run
"""

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone

import requests

SOURCE_URL = (
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
    "denmark": "DEN",
    "ecuador": "ECU", "egypt": "EGY", "el salvador": "SLV",
    "england": "ENG",
    "finland": "FIN", "france": "FRA",
    "gabon": "GAB", "gambia": "GAM", "georgia": "GEO",
    "germany": "GER", "ghana": "GHA", "greece": "GRE",
    "guatemala": "GUA", "guinea": "GUI",
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
    for country, code in COUNTRY_TO_CODE.items():
        if country in key or key in country:
            return code
    return None


# ---------------------------------------------------------------------------
# Fetch + parse openfootball
# Returns list of {"home": code, "away": code, "date": "YYYY-MM-DD"}
# Only includes matches where both teams are real names (not W101 placeholders)
# ---------------------------------------------------------------------------

def fetch_openfootball() -> list[dict]:
    print(f"  Fetching {SOURCE_URL} ...")
    r = requests.get(SOURCE_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    data = r.json()

    parsed = []
    for m in data.get("matches", []):
        t1 = m.get("team1", "")
        t2 = m.get("team2", "")
        date = m.get("date", "")

        # Skip placeholder entries like "W101", "L102"
        if re.match(r'^[WL]\d+$', t1) or re.match(r'^[WL]\d+$', t2):
            continue

        h = to_code(t1)
        a = to_code(t2)
        if h and a:
            parsed.append({"home": h, "away": a, "date": date})

    return parsed


# ---------------------------------------------------------------------------
# Apply to schedule
# Match by date + frozenset of codes (order-independent)
# ---------------------------------------------------------------------------

def apply_results(
    schedule: list[dict],
    scraped: list[dict],
) -> tuple[list[dict], int]:

    # Build lookup: (date, frozenset) → (home, away)
    lookup: dict[tuple, tuple] = {}
    for r in scraped:
        key = (r["date"], frozenset([r["home"], r["away"]]))
        lookup[key] = (r["home"], r["away"])

    updated = 0
    for match in schedule:
        # Already filled — skip
        if match.get("home") and match.get("away"):
            continue

        date = match.get("date", "")
        h = match.get("home", "")
        a = match.get("away", "")

        # Group stage: home/away are pre-filled in schedule; just confirm
        if h and a:
            key = (date, frozenset([h, a]))
            if key in lookup:
                # Already correct, nothing to change
                pass
            continue

        # Knockout: home/away empty — find match on same date in scraped
        # Exclude scraped matches already filled in the schedule
        filled_pairs_on_date = {
            frozenset([m2["home"], m2["away"]])
            for m2 in schedule
            if m2["date"] == date and m2.get("home") and m2.get("away")
        }
        same_day = [
            r for r in scraped
            if r["date"] == date
            and frozenset([r["home"], r["away"]]) not in filled_pairs_on_date
        ]
        # Find empty slots on this date in schedule
        empty_on_date = [
            m for m in schedule
            if m["date"] == date and not (m.get("home") and m.get("away"))
        ]

        if len(same_day) == 1 and len(empty_on_date) == 1:
            match["home"] = same_day[0]["home"]
            match["away"] = same_day[0]["away"]
            updated += 1
        elif len(same_day) > 1 and len(empty_on_date) == len(same_day):
            # Multiple matches same day — match by position (openfootball
            # lists them in kickoff-time order, same as schedule_matches.json)
            idx = empty_on_date.index(match)
            if idx < len(same_day):
                match["home"] = same_day[idx]["home"]
                match["away"] = same_day[idx]["away"]
                updated += 1

    return schedule, updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fill home/away in schedule_matches.json from openfootball",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 update_schedule.py\n"
            "  python3 update_schedule.py --schedule ../data/schedule/schedule_matches.json\n"
            "  python3 update_schedule.py --dry-run"
        ),
    )
    parser.add_argument(
        "--schedule",
        default="../data/schedule/schedule_matches.json",
        help="Path to schedule_matches.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing",
    )
    args = parser.parse_args()

    # Load schedule
    with open(args.schedule, encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, list):
        meta: dict = {}
        schedule: list[dict] = raw
    else:
        meta = {k: v for k, v in raw.items() if k != "matches"}
        schedule = raw.get("matches", [])

    print(f"[✓] Loaded {len(schedule)} matches from {args.schedule}")

    # Fetch
    try:
        scraped = fetch_openfootball()
    except Exception as e:
        print(f"[✗] Fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[✓] Fetched {len(scraped)} resolved matches from openfootball")

    # Apply
    updated_schedule, n_updated = apply_results(schedule, scraped)
    print(f"[✓] home/away filled for {n_updated} match(es)")

    if args.dry_run:
        filled = [m for m in updated_schedule if m.get("home") and m.get("away")]
        print(f"\n[dry-run] All filled matches ({len(filled)}):")
        for m in filled:
            print(f"  {m['match_id']:>3}  {m['date']}  {m['stage']:<14}  "
                  f"{m['home']:>3} vs {m['away']}")
        return

    if n_updated == 0:
        print("[!] Nothing new to update.")
        return

    bak = args.schedule + ".bak"
    shutil.copy2(args.schedule, bak)
    print(f"[✓] Backup saved to {bak}")

    out = {
        **meta,
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "matches": updated_schedule,
    }
    with open(args.schedule, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"[✓] Written to {args.schedule}")


if __name__ == "__main__":
    main()