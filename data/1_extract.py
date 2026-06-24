"""Fetch the Metropolitan Diary article pages and cache their HTML.

`meta_fetch.py` gives us ~2,600 article URLs (output/metropolitan_diary.parquet).
This script drives Chrome to download each page's article body into a local cache
(output/html_cache/). Turning that cache into entries is `parse.py`'s job.

The pages are paywalled (plain requests get 403), so we drive YOUR real Chrome.
Rather than automate a separate login, we start Chrome with remote debugging and
connect to it over CDP -- so the fetch reuses whatever NYT session that Chrome is
logged into. Log in once in that window; the dedicated profile (chrome-profile/)
remembers it across runs. (Chrome 136+ blocks remote debugging on the default
profile, so we use a dedicated one rather than your everyday Chrome.)

Two modes, run independently:

    uv run data/extract.py chrome           # launch Chrome w/ remote debugging; log into NYT here
    uv run data/extract.py fetch            # connect to that Chrome, cache each page's HTML (resumable)

Fetch is throttled and resumable (skips already-cached pages), so it's safe to
Ctrl-C and re-run. Then turn the cache into entries:

    uv run data/extract.py fetch --limit 3  # grab a few pages
    uv run data/parse.py --limit 3          # check the entries look right
"""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path

import polars as pl

DATA_DIR = Path(__file__).resolve().parent
OUT_DIR = DATA_DIR / "output"
META_PARQUET = OUT_DIR / "metropolitan_diary.parquet"
ENTRIES_PARQUET = OUT_DIR / "diary_entries.parquet"
HTML_CACHE = OUT_DIR / "html_cache"

START_URL = "https://www.nytimes.com/"
CDP_PORT = int(os.environ.get("MD_CDP_PORT", 9222))
CDP_URL = f"http://localhost:{CDP_PORT}"
CHROME_APP = os.environ.get(
    "MD_CHROME_APP", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
# Dedicated profile so `fetch` can connect over CDP (Chrome 136+ blocks remote
# debugging on the default profile) and so the NYT login persists across runs.
CHROME_PROFILE = DATA_DIR / "chrome-profile"
REQUEST_SLEEP = 3.0  # seconds between page loads -- be polite to nyt

# Candidate selectors for the article body, best-first. We save the first match
# so the parse step has a small, clean subtree instead of the whole page.
BODY_SELECTORS = [
    'section[name="articleBody"]',
    "article",
    "main",
    "body",
]


# --- guid: stable id + cache filename for a row -------------------------------


def guid_for(uri: str, web_url: str) -> str:
    """A filesystem-safe id. `uri` is like nyt://article/<guid>; fall back to url."""
    if uri and "/" in uri:
        return uri.rsplit("/", 1)[-1]
    return re.sub(r"[^a-zA-Z0-9]+", "-", web_url).strip("-")


def cache_path(guid: str) -> Path:
    return HTML_CACHE / f"{guid}.html"


def load_meta(limit: int | None) -> pl.DataFrame:
    if not META_PARQUET.exists():
        raise SystemExit(f"missing {META_PARQUET}; run meta_fetch.py first")
    df = (
        pl.read_parquet(META_PARQUET)
        .filter(pl.col("is_diary_entry"))
        .select(
            "uri",
            "web_url",
            "headline_main",
            "byline_original",
            "pub_date",
            "pub_year",
        )
        .sort("pub_date", descending=True)
    )
    if limit:
        df = df.head(limit)
    return df


# --- fetch --------------------------------------------------------------------


def extract_body_html(page) -> str:
    """Return the outerHTML of the first matching body selector (best-first)."""
    for sel in BODY_SELECTORS:
        el = page.query_selector(sel)
        if el:
            return el.evaluate("e => e.outerHTML")
    return page.content()


def cmd_fetch(limit: int | None) -> None:
    from playwright.sync_api import sync_playwright

    df = load_meta(limit)
    HTML_CACHE.mkdir(parents=True, exist_ok=True)

    # URIs we've already parsed into diary_entries -- don't re-pull those pages.
    done_uris: set[str] = set()
    if ENTRIES_PARQUET.exists():
        done_uris = set(pl.read_parquet(ENTRIES_PARQUET).get_column("uri").to_list())

    todo = [
        r
        for r in df.iter_rows(named=True)
        if r["uri"] not in done_uris
        and not cache_path(guid_for(r["uri"], r["web_url"])).exists()
    ]
    print(
        f"{df.height} entries; {df.height - len(todo)} already cached/parsed; "
        f"fetching {len(todo)}."
    )
    if not todo:
        return

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:  # noqa: BLE001
            raise SystemExit(
                f"couldn't connect to Chrome at {CDP_URL} ({e}).\n"
                f"Start it first (and log into NYT there):  uv run data/extract.py chrome"
            )
        # Reuse the already-open context so we inherit your logged-in NYT session.
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        try:
            for i, row in enumerate(todo, 1):
                guid = guid_for(row["uri"], row["web_url"])
                try:
                    page.goto(
                        row["web_url"], wait_until="domcontentloaded", timeout=45_000
                    )
                    html = extract_body_html(page)
                    cache_path(guid).write_text(html, encoding="utf-8")
                    print(f"  [{i}/{len(todo)}] {len(html):>7,d}b  {row['web_url']}")
                except Exception as e:  # noqa: BLE001 -- one bad page shouldn't kill the run
                    print(f"  [{i}/{len(todo)}] ERROR {row['web_url']}: {e}")
                time.sleep(REQUEST_SLEEP)
        finally:
            page.close()  # leave your Chrome open; just close the tab we opened
    print(f"done -> {HTML_CACHE}")


def cmd_chrome() -> None:
    """Launch your Chrome with remote debugging so `fetch` can connect over CDP."""
    import subprocess

    if not Path(CHROME_APP).exists():
        raise SystemExit(
            f"Chrome not found at {CHROME_APP}; set MD_CHROME_APP to its path."
        )
    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            CHROME_APP,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={CHROME_PROFILE}",
            START_URL,
        ]
    )
    print(
        f"Launched Chrome (debugging on :{CDP_PORT}, profile {CHROME_PROFILE.name}/).\n"
        "First time? Log into your NYT account in that window.\n"
        "Leave it open, then run:  uv run data/extract.py fetch"
    )


# --- main ---------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser(
        "chrome", help="launch Chrome with remote debugging (log into NYT here)"
    )
    f = sub.add_parser("fetch", help="cache each page's article HTML")
    f.add_argument("--limit", type=int, help="only the first N pages (for calibration)")
    args = ap.parse_args()

    if args.cmd == "chrome":
        cmd_chrome()
    elif args.cmd == "fetch":
        cmd_fetch(args.limit)


if __name__ == "__main__":
    main()
