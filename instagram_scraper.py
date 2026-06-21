"""
Instagram Public Hashtag Scraper
=================================
Collects public profile data (username, bio, followers, website, email)
from posts tagged with a given hashtag.

IMPORTANT NOTICE
----------------
Instagram's frontend is fully JavaScript-rendered since ~2022. The
window._sharedData payload and ?__a=1 JSON endpoints are gone or
require authentication. This script tries every static-HTML technique
available; if Instagram returns an empty shell, it will say so clearly
rather than silently producing empty results.

For reliable production use → official Meta Graph API:
  https://developers.facebook.com/docs/instagram-api

Requirements:
    pip install requests beautifulsoup4 fake-useragent lxml

Usage:
    python instagram_scraper.py --hashtag travel --limit 100
    python instagram_scraper.py --hashtag python --limit 50 --output leads.csv
"""

import argparse
import csv
import json
import logging
import re
import sys
import time
import random
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from fake_useragent import UserAgent
    _ua = UserAgent()
except Exception:
    _ua = None

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ig_scraper")

# ─── Constants ────────────────────────────────────────────────────────────────

BASE_URL     = "https://www.instagram.com"
HASHTAG_URL  = BASE_URL + "/explore/tags/{tag}/"
PROFILE_URL  = BASE_URL + "/{username}/"

# Regex to find e-mail addresses inside bio text
EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")

CSV_FIELDS = ["username", "bio", "follower_count", "website", "email", "profile_url"]

# ─── Session ──────────────────────────────────────────────────────────────────

def build_session() -> requests.Session:
    """
    Create a requests.Session with rotating browser-like headers.
    Uses fake_useragent if available; falls back to a static Chrome UA.
    """
    ua_string = (
        _ua.random if _ua else
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
    session = requests.Session()
    session.headers.update({
        "User-Agent":                ua_string,
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate, br",
        "Connection":                "keep-alive",
        "Referer":                   "https://www.instagram.com/",
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "same-origin",
        "DNT":                       "1",
        "Upgrade-Insecure-Requests": "1",
    })
    return session


def _rotate_ua(session: requests.Session) -> None:
    """Refresh the User-Agent header to reduce fingerprinting."""
    if _ua:
        session.headers["User-Agent"] = _ua.random

# ─── HTML parsing helpers ─────────────────────────────────────────────────────

def _safe_get(url: str, session: requests.Session, timeout: int = 15) -> requests.Response | None:
    """
    GET a URL with unified error handling.
    Returns the Response on 2xx, None on network errors / 4xx / 5xx.
    """
    try:
        resp = session.get(url, timeout=timeout)
    except requests.exceptions.ConnectionError:
        log.error("No internet connection (failed: %s).", url)
        sys.exit(1)
    except requests.exceptions.Timeout:
        log.warning("Request timed out: %s", url)
        return None
    except requests.exceptions.RequestException as exc:
        log.warning("Request error: %s", exc)
        return None

    if resp.status_code == 404:
        log.warning("404 Not Found: %s", url)
        return None
    if resp.status_code == 429:
        log.warning("429 Rate-limited. Sleeping 30 s …")
        time.sleep(30)
        return None
    if not resp.ok:
        log.warning("HTTP %d for %s", resp.status_code, url)
        return None

    return resp


def _parse_embedded_json(html: str) -> list[dict]:
    """
    Instagram bakes page data into <script type="application/json"> tags.
    Returns all parseable JSON objects found in the page as a flat list of dicts.
    Also tries the legacy window._sharedData variable as a fallback.
    """
    results: list[dict] = []
    soup = BeautifulSoup(html, "lxml")

    # Strategy 1 — <script type="application/json">
    for tag in soup.find_all("script", {"type": "application/json"}):
        text = tag.string
        if not text:
            continue
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                results.append(obj)
        except json.JSONDecodeError:
            pass

    # Strategy 2 — window._sharedData (legacy, rarely present post-2022)
    for tag in soup.find_all("script", {"type": "text/javascript"}):
        text = tag.string or ""
        m = re.search(r"window\._sharedData\s*=\s*(\{.*?\});", text, re.DOTALL)
        if m:
            try:
                results.append(json.loads(m.group(1)))
            except json.JSONDecodeError:
                pass

    return results


def _walk_for_key(obj, key: str, found: list | None = None, depth: int = 0) -> list:
    """
    Recursively walk any JSON structure and collect all values
    associated with `key`. Depth-limited to 20 levels.
    """
    if found is None:
        found = []
    if depth > 20:
        return found
    if isinstance(obj, dict):
        if key in obj:
            found.append(obj[key])
        for v in obj.values():
            _walk_for_key(v, key, found, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_key(item, key, found, depth + 1)
    return found


def _find_user_node(obj, username: str, depth: int = 0) -> dict | None:
    """
    Search a JSON tree for a profile-shaped dict whose 'username' matches.
    A profile node is identified by having 'biography' or 'edge_followed_by'.
    """
    if depth > 20:
        return None
    if isinstance(obj, dict):
        uname = obj.get("username", "")
        if isinstance(uname, str) and uname.lower() == username.lower():
            if "biography" in obj or "edge_followed_by" in obj or "follower_count" in obj:
                return obj
        for v in obj.values():
            result = _find_user_node(v, username, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _find_user_node(item, username, depth + 1)
            if result:
                return result
    return None

# ─── Hashtag → usernames ──────────────────────────────────────────────────────

def fetch_usernames_from_hashtag(
    session: requests.Session, hashtag: str, limit: int
) -> list[str]:
    """
    Load the public hashtag explore page and extract post-author usernames.

    Instagram's initial HTML typically contains ~12 posts.  Pagination via
    GraphQL cursors requires authentication; we therefore cap collection at
    whatever the first page yields (up to `limit`).

    Returns a deduplicated list of usernames.
    """
    url = HASHTAG_URL.format(tag=hashtag)
    log.info("Fetching hashtag page: %s", url)

    resp = _safe_get(url, session)
    if not resp:
        return []

    html = resp.text

    # --- JSON path ---
    blobs = _parse_embedded_json(html)
    usernames: list[str] = []
    for blob in blobs:
        found = _walk_for_key(blob, "username")
        for u in found:
            if isinstance(u, str) and u not in usernames:
                usernames.append(u)

    if usernames:
        log.info("Extracted %d username(s) from embedded JSON.", len(usernames))
    else:
        # --- Regex fallback on raw HTML ---
        raw = re.findall(r'"username"\s*:\s*"([A-Za-z0-9._]+)"', html)
        usernames = list(dict.fromkeys(raw))
        if usernames:
            log.info("Regex fallback found %d username(s).", len(usernames))

    if not usernames:
        log.warning(
            "Could not extract any usernames from #%s. "
            "Instagram likely returned a JavaScript-only shell page. "
            "Recommended alternative: Meta Graph API or a headless browser.",
            hashtag,
        )

    return usernames[:limit]

# ─── Profile scraper ──────────────────────────────────────────────────────────

def scrape_profile(session: requests.Session, username: str) -> dict | None:
    """
    Fetch a public Instagram profile page and extract:
      username, bio, follower_count, website, email (parsed from bio).

    Returns None if the page is blocked, private, or data cannot be parsed.
    """
    _rotate_ua(session)
    url = PROFILE_URL.format(username=username)

    resp = _safe_get(url, session)
    if not resp:
        return None

    blobs = _parse_embedded_json(resp.text)
    user_node: dict | None = None

    for blob in blobs:
        user_node = _find_user_node(blob, username)
        if user_node:
            break

    if not user_node:
        # Last-resort regex extraction directly from raw HTML
        bio_m    = re.search(r'"biography"\s*:\s*"([^"]*)"', resp.text)
        url_m    = re.search(r'"external_url"\s*:\s*"([^"]*)"', resp.text)
        flw_m    = re.search(r'"edge_followed_by"\s*:\s*\{"count"\s*:\s*(\d+)', resp.text)
        fc_m     = re.search(r'"follower_count"\s*:\s*(\d+)', resp.text)

        if bio_m or url_m or flw_m:
            user_node = {
                "biography":        bio_m.group(1).encode().decode("unicode_escape") if bio_m else "",
                "external_url":     url_m.group(1) if url_m else "",
                "edge_followed_by": {"count": int(flw_m.group(1))} if flw_m else {},
                "follower_count":   int(fc_m.group(1)) if fc_m else 0,
            }

    if not user_node:
        log.warning("    No profile data found for @%s (private / blocked).", username)
        return None

    bio      = user_node.get("biography") or ""
    bio      = bio.replace("\\n", " ").strip()
    website  = user_node.get("external_url") or user_node.get("website_url") or ""
    followers = (
        user_node.get("edge_followed_by", {}).get("count")
        or user_node.get("follower_count")
        or 0
    )
    email_m  = EMAIL_RE.search(bio)

    return {
        "username":       username,
        "bio":            bio,
        "follower_count": followers,
        "website":        website,
        "email":          email_m.group(0) if email_m else "",
        "profile_url":    url,
    }

# ─── CSV output ───────────────────────────────────────────────────────────────

def save_to_csv(records: list[dict], filepath: Path) -> None:
    """Write scraped profile records to a UTF-8 CSV file."""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    log.info("Saved %d record(s) → %s", len(records), filepath.resolve())

# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scrape public Instagram profile data from a hashtag page."
    )
    p.add_argument("--hashtag", required=True, help="Hashtag without the # symbol")
    p.add_argument("--limit",   type=int, default=50, metavar="N",
                   help="Maximum number of profiles to collect (default: 50)")
    p.add_argument("--output",  default="", metavar="FILE",
                   help="Output CSV path (auto-generated if omitted)")
    p.add_argument("--delay",   type=float, nargs=2, default=[2.0, 5.0],
                   metavar=("MIN", "MAX"),
                   help="Random delay range in seconds between requests (default: 2 5)")
    return p.parse_args()


def main() -> None:
    args   = parse_args()
    tag    = args.hashtag.lstrip("#")
    limit  = max(1, args.limit)
    delay_min, delay_max = sorted(args.delay)

    output_path = (
        Path(args.output) if args.output
        else Path(f"ig_{tag}_{datetime.now():%Y%m%d_%H%M%S}.csv")
    )

    log.info("=" * 58)
    log.info("Hashtag  : #%s", tag)
    log.info("Limit    : %d profiles", limit)
    log.info("Delay    : %.1f–%.1f s between requests", delay_min, delay_max)
    log.info("Output   : %s", output_path)
    log.info("=" * 58)

    session   = build_session()
    usernames = fetch_usernames_from_hashtag(session, tag, limit)

    if not usernames:
        log.error("No usernames found — cannot continue.")
        sys.exit(1)

    log.info("Collected %d username(s). Starting profile scrape …", len(usernames))

    records: list[dict] = []

    for i, username in enumerate(usernames, start=1):
        log.info("[%d/%d] @%s", i, len(usernames), username)

        profile = scrape_profile(session, username)
        if profile:
            records.append(profile)
            log.info(
                "    followers=%-8s  website=%-30s  email=%s",
                profile["follower_count"] or "?",
                profile["website"]        or "—",
                profile["email"]          or "—",
            )
        else:
            log.warning("    Skipped @%s.", username)

        if i < len(usernames):
            delay = random.uniform(delay_min, delay_max)
            log.info("    Waiting %.1f s …", delay)
            time.sleep(delay)

    log.info("=" * 58)

    if records:
        save_to_csv(records, output_path)
        log.info(
            "Done. %d/%d profiles collected successfully.",
            len(records), len(usernames),
        )
    else:
        log.warning("No data collected.")
        log.warning(
            "Instagram returned JavaScript-only pages. Options:\n"
            "  1. Use the official Meta Graph API (https://developers.facebook.com/docs/instagram-api)\n"
            "  2. Use Playwright/Selenium for full browser rendering\n"
            "  3. Use a third-party service (PhantomBuster, Apify)"
        )


if __name__ == "__main__":
    main()
