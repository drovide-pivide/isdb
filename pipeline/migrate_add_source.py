"""
migrate_add_source.py
----------------------
One-time migration: backfill the "source" field into an existing
matches_wc2026.json that predates this field.

Rule: if a match already has a score (hg is not None), it must have come
from openfootball (that's the only place scores could have come from before
this field existed), so it's tagged "source": "openfootball". Matches with
no score yet are tagged "source": null.

After running this once, update_matches.py and manual_score.py take over
maintaining "source" correctly on every future run.

Usage:
    python3 migrate_add_source.py ../data/frontend/matches_wc2026.json
"""

import json
import shutil
import sys


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python3 migrate_add_source.py <path to matches_wc2026.json>",
              file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    matches = data if isinstance(data, list) else data.get("matches", [])

    n_of, n_null, n_skipped = 0, 0, 0
    for m in matches:
        if "source" in m:
            n_skipped += 1
            continue
        if m.get("hg") is not None:
            m["source"] = "openfootball"
            n_of += 1
        else:
            m["source"] = None
            n_null += 1

    print(f"[✓] Tagged source='openfootball' : {n_of}")
    print(f"[✓] Tagged source=null           : {n_null}")
    if n_skipped:
        print(f"[i] Already had 'source', left alone : {n_skipped}")

    bak = path + ".bak"
    shutil.copy2(path, bak)
    print(f"[✓] Backup saved to {bak}")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(matches, f, indent=2, ensure_ascii=False)
    print(f"[✓] Migrated {path}")


if __name__ == "__main__":
    main()
