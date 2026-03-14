# /// script
# dependencies = [
#   "curl_cffi",
# ]
# ///
"""
Coppermind wikitext extractor.

Usage:
    python extract.py                 # full download (skips existing files)
    python extract.py --since 2024-01-15  # only pages changed since this date
    python extract.py --force         # re-download all pages including existing
    python extract.py --since 2024-01-15 --force  # re-download all changed pages
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone

from curl_cffi import requests

URL = "https://coppermind.net/w/api.php"
OUTPUT_DIR = "coppermind_raw"
STATE_FILE = "extractor_state.json"
LAST_RUN_FILE = "extractor_last_run.json"

MAX_RETRIES = 5
RETRY_DELAY = 5.0  # seconds, multiplied by attempt number
BATCH_DELAY = 1.0  # polite pause between successful batches

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Base params for a full allpages crawl
FULL_CRAWL_PARAMS = {
    "action": "query",
    "generator": "allpages",
    "gaplimit": "50",
    "prop": "revisions",
    "rvprop": "content",
    "rvslots": "main",
    "format": "json",
}

# Base params for an incremental recentchanges crawl
INCREMENTAL_PARAMS = {
    "action": "query",
    "list": "recentchanges",
    "rcprop": "title|timestamp",
    "rctype": "edit|new",
    "rclimit": "50",
    "format": "json",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def clean_filename(title: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", title)


def extract_wikitext(page_info: dict) -> str | None:
    revisions = page_info.get("revisions")
    if not revisions:
        return None
    rev = revisions[0]
    try:
        return rev["slots"]["main"]["*"]
    except (KeyError, TypeError):
        pass
    return rev.get("*")


def fetch_page_content(title: str) -> str | None:
    """Fetch wikitext for a single page by title (used in incremental mode)."""
    params = {
        "action": "query",
        "titles": title,
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "format": "json",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                URL, params=params, impersonate="chrome120", timeout=30
            )
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")
            data = response.json()
            pages = data.get("query", {}).get("pages", {})
            for page_info in pages.values():
                return extract_wikitext(page_info)
        except Exception as e:
            print(f"  Attempt {attempt}/{MAX_RETRIES} failed fetching '{title}': {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    return None


def save_page(title: str, wikitext: str) -> None:
    filepath = os.path.join(OUTPUT_DIR, f"{clean_filename(title)}.txt")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(wikitext)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        print(
            f"Resuming from saved state (gapcontinue: {saved.get('gapcontinue', 'start')})"
        )
        return saved
    return dict(FULL_CRAWL_PARAMS)


def save_state(params: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(params, f)


def clear_state() -> None:
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)


def save_last_run() -> None:
    """Record the timestamp of a successful full run for future --since use."""
    with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_run": datetime.now(timezone.utc).isoformat()}, f)


def load_last_run() -> str | None:
    if os.path.exists(LAST_RUN_FILE):
        with open(LAST_RUN_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("last_run")
    return None


# ---------------------------------------------------------------------------
# Full crawl
# ---------------------------------------------------------------------------


def run_full_crawl(force: bool) -> None:
    """Download all pages. Skips existing files unless --force."""
    params = load_state()
    total = len(os.listdir(OUTPUT_DIR))
    mode = "force re-download" if force else "skip existing"
    print(f"Full crawl ({mode}) → '{OUTPUT_DIR}'  ({total} files already present)")

    while True:
        try:
            next_params, saved = _fetch_allpages_batch(params, force)
            total += saved

            if next_params is None:
                clear_state()
                save_last_run()
                print(f"Full crawl complete. {total} total files in '{OUTPUT_DIR}'.")
                break

            params = next_params
            save_state(params)
            print(f"  Batch done (+{saved} new/updated). Total: {total}.")
            time.sleep(BATCH_DELAY)

        except RuntimeError as e:
            print(f"Fatal: {e}")
            print("State saved — re-run to resume from this point.")
            break


def _fetch_allpages_batch(params: dict, force: bool) -> tuple[dict | None, int]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                URL, params=params, impersonate="chrome120", timeout=30
            )
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")

            data = response.json()
            pages = data.get("query", {}).get("pages", {})
            saved = 0

            for page_info in pages.values():
                title = page_info.get("title", "Unknown")
                filepath = os.path.join(OUTPUT_DIR, f"{clean_filename(title)}.txt")

                if not force and os.path.exists(filepath):
                    continue

                wikitext = extract_wikitext(page_info)
                if wikitext is None:
                    continue

                save_page(title, wikitext)
                saved += 1

            if "continue" in data:
                next_params = dict(params)
                next_params.update(data["continue"])
                return next_params, saved
            else:
                return None, saved

        except Exception as e:
            print(f"  Attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
            else:
                raise RuntimeError(f"Batch failed after {MAX_RETRIES} attempts.") from e


# ---------------------------------------------------------------------------
# Incremental update (--since)
# ---------------------------------------------------------------------------


def run_incremental(since: str, force: bool) -> None:
    """
    Download only pages modified since a given ISO 8601 date/timestamp.
    Uses the recentchanges API to get the list of changed titles, then
    fetches each page's content individually.

    The --since value can be:
      - A date:      2024-01-15
      - A datetime:  2024-01-15T10:30:00Z
      - "last"       use the timestamp from the previous successful full run
    """
    if since == "last":
        since = load_last_run()
        if not since:
            print(
                "No last_run record found. Run a full crawl first, or provide an explicit date."
            )
            return
        print(f"Using last run timestamp: {since}")

    # Normalise to MediaWiki timestamp format (ISO 8601 with Z)
    if "T" not in since:
        since = since + "T00:00:00Z"
    elif not since.endswith("Z"):
        since = since + "Z"

    print(f"Incremental update — fetching pages changed since {since}")

    # Collect all changed titles via recentchanges (paginated)
    params = dict(INCREMENTAL_PARAMS)
    params["rcstart"] = datetime.now(timezone.utc).isoformat()  # newest first
    params["rcend"] = since  # stop here
    params["rcdir"] = "older"

    changed_titles: set[str] = set()
    while True:
        try:
            response = requests.get(
                URL, params=params, impersonate="chrome120", timeout=30
            )
            if response.status_code != 200:
                print(f"HTTP {response.status_code} fetching recentchanges — aborting.")
                return
            data = response.json()
            for entry in data.get("query", {}).get("recentchanges", []):
                changed_titles.add(entry["title"])

            if "continue" in data:
                params.update(data["continue"])
                time.sleep(BATCH_DELAY)
            else:
                break
        except Exception as e:
            print(f"Error fetching recentchanges: {e}")
            break

    if not changed_titles:
        print("No pages changed since the specified date.")
        return

    print(f"Found {len(changed_titles)} changed pages. Fetching content...")
    updated, skipped = 0, 0

    for title in sorted(changed_titles):
        filepath = os.path.join(OUTPUT_DIR, f"{clean_filename(title)}.txt")
        if not force and not os.path.exists(filepath):
            # Page is new (not in our vault yet) — fetch it
            pass
        elif not force and os.path.exists(filepath):
            # Page exists — fetch it anyway since it changed
            pass  # always re-fetch changed pages regardless of force flag

        wikitext = fetch_page_content(title)
        if wikitext:
            save_page(title, wikitext)
            updated += 1
            print(f"  Updated: {title}")
        else:
            skipped += 1
            print(f"  [WARN] Could not fetch: {title}")
        time.sleep(0.5)  # gentler rate for individual fetches

    print(f"Incremental update complete. {updated} updated, {skipped} failed.")
    # Update the last_run timestamp after a successful incremental pass
    save_last_run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download raw wikitext from Coppermind.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--since",
        metavar="DATE",
        help=(
            "Only fetch pages changed since this date (YYYY-MM-DD or ISO 8601). "
            "Use 'last' to use the timestamp of the previous successful full run."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download pages even if the local file already exists.",
    )
    args = parser.parse_args()

    if args.since:
        run_incremental(args.since, force=args.force)
    else:
        run_full_crawl(force=args.force)


if __name__ == "__main__":
    main()
