"""
manual_score.py
----------------
Manually patch score fields for one match in data/frontend/matches_wc2026.json.

Use this for matches the automated pipeline (update_matches.py) can't resolve
yet -- e.g. the openfootball source hasn't added the knockout-stage result.

Any match you patch here gets "source": "manual" stamped onto it.
update_matches.py checks that field: once a match is "manual", the pipeline
will never overwrite its score fields again on future runs -- it skips
straight past that match's score/et/pens and leaves them exactly as you set
them, no matter what openfootball says. (oneline_comment and imdb_score are
still updated normally.)

If openfootball later publishes the real result and you want the pipeline to
take over again, edit this match's "source" back to null (or just delete the
key) in matches_wc2026.json, then rerun update_matches.py.

Usage:
    # Interactive (safest -- shows the match before asking you to confirm)
    python3 manual_score.py --match-id 104

    # Non-interactive
    python3 manual_score.py --match-id 104 --hg 1 --ag 0

    # With extra time / penalties
    python3 manual_score.py --match-id 74 --hg 1 --ag 1 --et-hg 1 --et-ag 1 --pens-hg 3 --pens-ag 4

    # Also set the one-line comment
    python3 manual_score.py --match-id 104 --hg 1 --ag 0 --comment "Torres settles it deep into extra time"

    # Custom file path
    python3 manual_score.py --match-id 104 --hg 1 --ag 0 --file ../data/frontend/matches_wc2026.json

    # Release a match back to the automated pipeline
    python3 manual_score.py --match-id 104 --release
"""

import argparse
import json
import shutil
import sys


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("matches", [])


def find_match(matches: list[dict], match_id: int) -> dict | None:
    for m in matches:
        if m.get("match_id") == match_id:
            return m
    return None


def describe(m: dict) -> str:
    score = f"{m.get('hg')}-{m.get('ag')}" if m.get("hg") is not None else "unplayed"
    src = m.get("source") or "none"
    return (
        f"#{m.get('match_id')}  {m.get('date')}  {m.get('stage')}  "
        f"{m.get('home')} vs {m.get('away')}  [{score}]  (source: {src})"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Manually patch a match's score.")
    p.add_argument("--file", default="../data/frontend/matches_wc2026.json",
                    help="Path to matches_wc2026.json")
    p.add_argument("--match-id", type=int, required=True, help="match_id to patch")
    p.add_argument("--hg", type=int, help="Home goals (full/normal time)")
    p.add_argument("--ag", type=int, help="Away goals (full/normal time)")
    p.add_argument("--et-hg", type=int, help="Home goals after extra time")
    p.add_argument("--et-ag", type=int, help="Away goals after extra time")
    p.add_argument("--pens-hg", type=int, help="Home penalties")
    p.add_argument("--pens-ag", type=int, help="Away penalties")
    p.add_argument("--comment", help="oneline_comment text")
    p.add_argument("--release", action="store_true",
                    help="Clear 'source' back to null so update_matches.py "
                         "resumes fetching this match from openfootball")
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = p.parse_args()

    matches = load(args.file)
    match = find_match(matches, args.match_id)
    if match is None:
        print(f"[!] No match with match_id={args.match_id} found in {args.file}", file=sys.stderr)
        sys.exit(1)

    print("Current:")
    print(f"  {describe(match)}")

    updates: dict = {}

    if args.release:
        updates["source"] = None
    else:
        score_fields = {}
        if args.hg is not None:
            score_fields["hg"] = args.hg
        if args.ag is not None:
            score_fields["ag"] = args.ag
        if args.et_hg is not None:
            score_fields["et_hg"] = args.et_hg
        if args.et_ag is not None:
            score_fields["et_ag"] = args.et_ag
        if args.pens_hg is not None:
            score_fields["pens_hg"] = args.pens_hg
        if args.pens_ag is not None:
            score_fields["pens_ag"] = args.pens_ag

        if score_fields:
            updates.update(score_fields)
            updates["source"] = "manual"

        if args.comment is not None:
            updates["oneline_comment"] = args.comment

    if not updates:
        print("[!] Nothing to update -- pass at least --hg/--ag (and optionally "
              "--et-hg/--et-ag/--pens-hg/--pens-ag/--comment), or --release.",
              file=sys.stderr)
        sys.exit(1)

    if match.get("source") == "openfootball" and updates.get("source") == "manual":
        print("\n[!] Note: this match currently has source='openfootball' "
              "(already resolved by the live fetch). Patching it will lock "
              "in your values as 'manual' and stop future pipeline runs "
              "from touching it.")

    print("\nWill apply:")
    for k, v in updates.items():
        print(f"  {k:>16} : {match.get(k)!r}  ->  {v!r}")

    if not args.yes:
        confirm = input("\nApply these changes? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted, nothing written.")
            return

    match.update(updates)

    bak = args.file + ".bak"
    shutil.copy2(args.file, bak)
    print(f"[✓] Backup saved to {bak}")

    with open(args.file, "w", encoding="utf-8") as f:
        json.dump(matches, f, indent=2, ensure_ascii=False)

    print(f"[✓] Updated match #{args.match_id} in {args.file}")
    print(f"    {describe(match)}")


if __name__ == "__main__":
    main()
