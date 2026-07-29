"""
episode_ratings.py
------------------
Get IMDb ratings for every episode of a TV series, given its title ID.
Uses Playwright to render JavaScript-heavy IMDb pages.

Setup (one-time):
    pip install playwright
    playwright install chromium

Usage:
    python3 episode_ratings.py tt0903747
    python3 episode_ratings.py tt0903747 --season 3
    python3 episode_ratings.py tt0903747 --output ratings.json
"""

import argparse
import json
import re
import sys
import time

from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def get_next_data(page, url: str, retries: int = 3) -> dict:
    """Navigate to a URL and extract the __NEXT_DATA__ JSON blob.
    Retries if the JSON is truncated (IMDb sometimes serves a partial blob
    before the page has fully hydrated).
    """
    for attempt in range(1, retries + 1):
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("script#__NEXT_DATA__", state="attached", timeout=15000)
        # Extra wait on retries to let the page settle
        if attempt > 1:
            page.wait_for_timeout(2000 * attempt)
        raw = page.eval_on_selector("script#__NEXT_DATA__", "el => el.textContent")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            if attempt == retries:
                raise RuntimeError(
                    f"__NEXT_DATA__ still malformed after {retries} attempts "
                    f"({url}): {exc}"
                ) from exc
            print(f"    [warn] JSON truncated on attempt {attempt}, retrying...")
            time.sleep(1)


# ---------------------------------------------------------------------------
# Series info
# ---------------------------------------------------------------------------

def get_series_info(page, title_id: str) -> tuple:
    url = f"https://www.imdb.com/title/{title_id}/"
    data = get_next_data(page, url)

    props = data.get("props", {}).get("pageProps", {})
    above = props.get("aboveTheFoldData", {})
    main  = props.get("mainColumnData", {})

    series_name = (
        above.get("titleText", {}).get("text")
        or main.get("titleText", {}).get("text")
        or title_id
    )

    # Season list lives in mainColumnData.episodes.seasons
    seasons_raw = (
        main.get("episodes", {}).get("seasons")
        or above.get("episodes", {}).get("seasons")
        or []
    )
    seasons = sorted(s["number"] for s in seasons_raw if s.get("number"))

    # Fallback: scrape the episode guide page selector
    if not seasons:
        ep_url = f"https://www.imdb.com/title/{title_id}/episodes/"
        page.goto(ep_url, wait_until="domcontentloaded", timeout=30000)
        options = page.query_selector_all("select[id='bySeason'] option, select[aria-label*='eason'] option")
        for opt in options:
            val = opt.get_attribute("value") or ""
            if val.lstrip("-").isdigit() and int(val) > 0:
                seasons.append(int(val))
        seasons = sorted(set(seasons))

    return series_name, seasons


# ---------------------------------------------------------------------------
# Episodes for one season
# ---------------------------------------------------------------------------

def get_episodes_for_season(page, title_id: str, season: int) -> list:
    url = f"https://www.imdb.com/title/{title_id}/episodes/?season={season}"
    data = get_next_data(page, url)

    props = data.get("props", {}).get("pageProps", {})

    # Path 1: contentData > section > episodes > items
    items = (
        props.get("contentData", {})
             .get("section", {})
             .get("episodes", {})
             .get("items", [])
    )

    # Path 2: mainColumnData > episodes > episodes > items
    if not items:
        items = (
            props.get("mainColumnData", {})
                 .get("episodes", {})
                 .get("episodes", {})
                 .get("items", [])
        )

    # Path 3: walk entire props looking for a list of episode dicts
    if not items:
        raw = json.dumps(props)
        # Find all episode id/rating pairs via regex as last resort
        episodes_out = []
        for m in re.finditer(r'"id"\s*:\s*"(tt\d+)".*?"aggregateRating"\s*:\s*([\d.]+)', raw):
            episodes_out.append({
                "id": m.group(1),
                "title": "",
                "season": season,
                "episode": None,
                "year": None, "month": None, "day": None,
                "rating": float(m.group(2)),
                "votes": None,
                "url": f"https://www.imdb.com/title/{m.group(1)}/",
            })
        return episodes_out

    episodes = []
    for item in items:
        ep_id    = item.get("id", "")
        ep_title = item.get("titleText") or item.get("title") or ""
        ep_num   = item.get("episode")
        rating   = item.get("aggregateRating")
        votes    = item.get("voteCount")
        release  = item.get("releaseDate") or {}

        episodes.append({
            "id": ep_id,
            "title": ep_title,
            "season": season,
            "episode": ep_num,
            "year":  release.get("year"),
            "month": release.get("month"),
            "day":   release.get("day"),
            "rating": float(rating) if rating is not None else None,
            "votes":  int(votes)    if votes  is not None else None,
            "url": f"https://www.imdb.com/title/{ep_id}/" if ep_id else None,
        })

    return episodes


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_table(series_name: str, episodes: list) -> None:
    print(f"\n{'='*70}")
    print(f"  {series_name}")
    print(f"{'='*70}")
    print(f"{'S':>3}  {'E':>3}  {'Rating':>6}  {'Votes':>8}  Title")
    print(f"{'-'*3}  {'-'*3}  {'-'*6}  {'-'*8}  {'-'*40}")

    for ep in episodes:
        s      = ep["season"]  if ep["season"]  is not None else "?"
        e      = ep["episode"] if ep["episode"] is not None else "?"
        rating = f"{ep['rating']:.1f}" if ep["rating"] else "N/A"
        votes  = f"{ep['votes']:,}"    if ep["votes"]  else "N/A"
        title  = (ep["title"] or "")[:45]
        print(f"{s:>3}  {e:>3}  {rating:>6}  {votes:>8}  {title}")

    rated = [ep for ep in episodes if ep["rating"]]
    if rated:
        avg = sum(ep["rating"] for ep in rated) / len(rated)
        print(f"\n  Episodes: {len(episodes)}  |  Rated: {len(rated)}  |  Avg rating: {avg:.2f}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch IMDb episode ratings for a given title ID.",
        epilog=(
            "Examples:\n"
            "  python3 episode_ratings.py tt32915471 --output wc_2026_ratings.json\n"
            "  python3 episode_ratings.py tt12729982 --output wc_2022_ratings.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("title_id", help="IMDb title ID, e.g. tt0903747")
    parser.add_argument("--season", type=int, default=None,
                        help="Fetch only this season (default: all)")
    parser.add_argument("--output", default=None,
                        help="Save results to a JSON file")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Delay between season requests in seconds (default: 0.5)")
    args = parser.parse_args()

    title_id = args.title_id.strip()
    if not title_id.startswith("tt"):
        title_id = f"tt{title_id}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        print(f"\nFetching series info for {title_id}...")
        try:
            series_name, all_seasons = get_series_info(page, title_id)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            browser.close()
            sys.exit(1)

        print(f"Series : {series_name}")
        print(f"Seasons: {all_seasons}")

        target_seasons = [args.season] if args.season else all_seasons
        if not target_seasons:
            print("No seasons found.", file=sys.stderr)
            browser.close()
            sys.exit(1)

        all_episodes = []
        for season_num in target_seasons:
            print(f"  Fetching season {season_num}...")
            try:
                eps = get_episodes_for_season(page, title_id, season_num)
                all_episodes.extend(eps)
                print(f"    → {len(eps)} episodes fetched")
            except Exception as exc:
                print(f"    Warning: season {season_num} failed – {exc}", file=sys.stderr)
            time.sleep(args.delay)

        browser.close()

    if not all_episodes:
        print("No episode data retrieved.", file=sys.stderr)
        sys.exit(1)

    print_table(series_name, all_episodes)

    if args.output:
        out = {
            "title_id": title_id,
            "series": series_name,
            "total_episodes": len(all_episodes),
            "episodes": all_episodes,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        print(f"Results saved to {args.output}")


if __name__ == "__main__":
    main()