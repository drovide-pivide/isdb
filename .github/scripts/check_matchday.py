#!/usr/bin/env python3
"""
Checks whether yesterday (UTC) is a date in .github/pl_matchdays_2026_27.json,
and writes is_matchday=true/false to $GITHUB_OUTPUT accordingly. Run from the
repo root (the data-pipeline.yml job does a checkout first).
"""
import datetime
import json
import os
import sys
from pathlib import Path

MATCHDAYS_FILE = Path(__file__).resolve().parent.parent / "pl_matchdays_2026_27.json"


def main() -> None:
    yesterday = (
        datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=1)
    ).isoformat()

    if not MATCHDAYS_FILE.exists():
        print(f"warning: {MATCHDAYS_FILE} not found — treating as not a matchday")
        is_matchday = False
    else:
        with open(MATCHDAYS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        is_matchday = yesterday in data.get("match_dates", [])

    print(f"yesterday ({yesterday}) matchday: {is_matchday}")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as out:
            out.write(f"is_matchday={'true' if is_matchday else 'false'}\n")
    else:
        # not running inside GitHub Actions — just print, for local testing
        print(f"(no GITHUB_OUTPUT set; would have written is_matchday={is_matchday})")


if __name__ == "__main__":
    sys.exit(main())
