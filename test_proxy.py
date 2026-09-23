#!/usr/bin/env python3
"""
test_proxy.py
--------------
Answers one question directly: does a given proxy actually get past
Sofascore's block? Hits the exact endpoint that returned 403 in GitHub
Actions, once with the proxy and (optionally) once without, so you can
compare both results side by side before wiring anything into
match_stats_fetch.py or a GitHub secret.

Usage:
    pip install curl_cffi

    # test without a proxy first, to confirm the block reproduces here too
    python3 test_proxy.py --no-proxy

    # then test with your proxy's credentials
    export PROXY_URL="http://username:password@proxy-host:port"
    python3 test_proxy.py
"""
from __future__ import annotations

import argparse
import os
import sys

URL = "https://api.sofascore.com/api/v1/unique-tournament/16/seasons"

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


def run(proxy_url: str | None, plain: bool = False) -> int:
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    mode = "plain requests, no impersonation" if plain else "curl_cffi, impersonate=chrome"
    where = f"through proxy ({proxy_url.split('@')[-1]})" if proxy_url else "without a proxy"

    print(f"Testing {where} — {mode}...")
    print(f"  GET {URL}\n")

    try:
        if plain:
            import requests as plain_requests  # only needed for this path
            r = plain_requests.get(URL, headers=HEADERS, proxies=proxies, timeout=20)
        else:
            from curl_cffi import requests as cffi_requests  # only needed for this path
            r = cffi_requests.get(URL, headers=HEADERS, proxies=proxies, timeout=20, impersonate="chrome")
    except Exception as e:
        print(f"FAILED — request errored out before getting a response: {type(e).__name__}: {e}")
        return 1

    print(f"  status: {r.status_code}")

    if r.status_code == 200:
        n = len(r.json().get("seasons", []))
        print(f"\nPASS — got real data back ({n} seasons listed).")
        if proxy_url:
            print("This proxy gets past Sofascore's block. Safe to wire into the workflow.")
        return 0

    if r.status_code == 403:
        print("\nFAIL — still blocked (403 Forbidden).")
        if proxy_url:
            print("This specific proxy/IP is still being blocked. Worth trying a different")
            print("provider or a fresh session before assuming residential proxies in")
            print("general won't work here.")
        else:
            print("Expected without a proxy — confirms the block reproduces here the same")
            print("way it did in GitHub Actions.")
        return 1

    print(f"\nUNEXPECTED — status {r.status_code}, response body follows:")
    print(r.text[:500])
    return 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-proxy", action="store_true",
                     help="test without a proxy, to confirm the block reproduces locally")
    ap.add_argument("--plain", action="store_true",
                     help="use plain requests instead of curl_cffi's browser impersonation — "
                          "isolates whether a proxy failure is curl_cffi-specific")
    args = ap.parse_args()

    if args.no_proxy:
        sys.exit(run(None, plain=args.plain))

    proxy_url = os.environ.get("PROXY_URL")
    if not proxy_url:
        print("error: PROXY_URL is not set.\n")
        print('  export PROXY_URL="http://username:password@proxy-host:port"')
        print("  python3 test_proxy.py\n")
        print("or run with --no-proxy to test the unproxied (expected-to-fail) case first.")
        sys.exit(1)

    sys.exit(run(proxy_url, plain=args.plain))


if __name__ == "__main__":
    main()
