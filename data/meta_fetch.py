"""Fetch the full run of Metropolitan Diary articles from the NYT Article Search API.

WHY `title OR kicker` (this is the non-obvious part):
Neither filter alone is complete, because the column changed publishing models and
the NYT only started applying the kicker tag recently. Per-year probes:

    1993:  kicker=0    title=52    union=52    (weekly Living-section column; NO kicker
                                                tag back then -> only the title catches it)
    2016:  kicker=261  title=1     union=262   (moved online: many individual entries with
                                                content headlines + the kicker tag -> only
                                                the kicker catches them)

  kicker:"METROPOLITAN DIARY"            -> finds the modern individual entries, whose
                                           display headline is the entry's content.
                                           Missing before ~the 2010s (tag not applied).
  headline.default:"Metropolitan Diary"  -> finds the older weekly column (headline was
                                           literally "Metropolitan Diary") and the
                                           digitized 1976+ archives. Also pulls a few
                                           pieces *about* the column ("45 Years of...",
                                           "Best of...", "How to Submit...").

So we fetch the UNION and let you classify afterward. Each row carries provenance flags
`has_kicker_tag`, `title_has_phrase`, and a heuristic `is_meta_article` to support that.
Expected total is ~3,000 (vs ~1,475 for kicker alone).

PAGINATION CAP: the API returns at most 1,000 results per query (100 pages x 10), so we
chunk into date windows and paginate within each. Because the modern era is dense
(~260/yr), a window can exceed the cap, so windows that do are split in half recursively
until they fit. The count lives in response.metadata.hits (NOT response.meta, despite the
swagger doc), and response.docs is null when a window is empty.

OUTPUT: every field the API returns is kept (nested fields JSON-serialized) plus
flattened convenience columns; written to output/metropolitan_diary.parquet.

RATE LIMITS: ~5 requests/minute, ~500/day -> we throttle. A full run is ~320 requests /
~65 min (best run in the background).

Run from anywhere:   uv run data/meta_fetch.py
Test a small slice:  MD_START_YEAR=2024 MD_END_YEAR=2024 MD_MAX_PAGES=1 uv run data/meta_fetch.py
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import date
from pathlib import Path

import dotenv
import polars as pl
import requests

ENV_PATH = (
    Path(__file__).resolve().parents[1] / ".env"
)  # repo-root .env, CWD-independent
dotenv.load_dotenv(ENV_PATH)


def get_nyt_key():
    return os.environ.get("NYT_KEY") or dotenv.get_key(str(ENV_PATH), "NYT_KEY")


def get_nyt_secret():
    return os.environ.get("NYT_SECRET") or dotenv.get_key(str(ENV_PATH), "NYT_SECRET")


# --- config (env-overridable, e.g. MD_START_YEAR=2024) -----------------------

API_URL = "https://api.nytimes.com/svc/search/v2/articlesearch.json"

TITLE_FQ = 'headline.default:"Metropolitan Diary"'  # old weekly column + 1976+ archives
KICKER_FQ = 'kicker:"METROPOLITAN DIARY"'  # modern individual entries
FQ = f"({TITLE_FQ}) OR ({KICKER_FQ})"  # the union -- the complete set

START_YEAR = int(os.environ.get("MD_START_YEAR", 1976))  # earliest entry in the index
END_YEAR = int(os.environ.get("MD_END_YEAR", date.today().year))
WINDOW_YEARS = int(
    os.environ.get("MD_WINDOW_YEARS", 3)
)  # initial window; over-cap windows auto-split
PAGE_SIZE = 10  # fixed by the API
API_MAX_PAGES = 100  # API hard cap per query
API_CAP = API_MAX_PAGES * PAGE_SIZE  # 1,000 results per query
MAX_PAGES = int(os.environ.get("MD_MAX_PAGES", API_MAX_PAGES))  # lower it to smoke-test

REQUEST_SLEEP = float(
    os.environ.get("MD_SLEEP", 12.0)
)  # seconds between requests (~5/min)
MAX_RETRIES = 5

OUT_DIR = Path(__file__).resolve().parent / "output"
PARQUET_PATH = OUT_DIR / "metropolitan_diary.parquet"

# articles *about* the column rather than entries of it
_META_HEADLINE_MARKERS = (
    "best of metropolitan diary",
    "vote for",
    "how to submit",
    "years of metropolitan diary",
    "metropolitan diary fans",
)

# --- fetching ----------------------------------------------------------------


def date_windows(start_year: int, end_year: int, step: int):
    """Yield non-overlapping (begin_date, end_date) strings in YYYYMMDD form."""
    year = start_year
    while year <= end_year:
        window_end = min(year + step - 1, end_year)
        yield f"{year}0101", f"{window_end}1231"
        year = window_end + 1


def fetch_page(
    session: requests.Session, api_key: str, begin: str, end: str, page: int
) -> dict:
    """Return the `response` object for one page, retrying on rate limits."""
    params = {
        "fq": FQ,
        "begin_date": begin,
        "end_date": end,
        "page": page,
        "sort": "oldest",
        "api-key": api_key,
    }
    for attempt in range(MAX_RETRIES):
        resp = session.get(API_URL, params=params, timeout=30)
        if resp.status_code == 429:
            backoff = REQUEST_SLEEP * (attempt + 2)
            print(f"    429 rate-limited; backing off {backoff:.0f}s")
            time.sleep(backoff)
            continue
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "OK":
            raise RuntimeError(f"API returned non-OK status: {data}")
        return data.get("response", {})
    raise RuntimeError(
        f"Exceeded {MAX_RETRIES} retries (rate limited) for {begin}-{end} page {page}"
    )


def parse_doc(doc: dict) -> dict:
    """Flatten one API doc into a single row, keeping EVERY field the API returns.

    Every top-level field is preserved as its own column. Nested fields (dict/list)
    are JSON-serialized so the row is CSV-safe and lossless -- including any field
    NYT adds in the future, which is captured automatically. Flattened convenience
    columns (headline_*, byline_*, locations/subjects/..., image_*) are added on top,
    along with provenance flags for classifying entries vs. about-the-column pieces.
    """
    # 1) keep every top-level field verbatim; serialize nested ones to JSON
    row = {
        key: (
            json.dumps(val, ensure_ascii=False)
            if isinstance(val, (dict, list))
            else val
        )
        for key, val in doc.items()
    }

    # 2) flattened convenience columns
    headline = doc.get("headline") or {}
    byline = doc.get("byline") or {}
    keywords = doc.get("keywords") or []
    multimedia = doc.get("multimedia") or {}
    default_img = multimedia.get("default") or {}
    thumb_img = multimedia.get("thumbnail") or {}
    main = headline.get("main") or ""
    kicker = headline.get("kicker") or ""
    type_of_material = doc.get("type_of_material") or ""
    pub_date = doc.get("pub_date") or ""

    def values_for(name: str) -> str:
        return "; ".join(k.get("value", "") for k in keywords if k.get("name") == name)

    # --- classification signals (so you can filter entries vs. noise later) ---
    kicker_norm = kicker.strip().lower()
    main_lower = main.lower()
    has_kicker_tag = kicker_norm == "metropolitan diary"
    title_has_phrase = "metropolitan diary" in main_lower
    is_meta_article = any(m in main_lower for m in _META_HEADLINE_MARKERS)
    is_correction = type_of_material.strip().lower() in ("correction", "editors' note")
    # a non-blank kicker that isn't the diary kicker => a cross-reference, not an entry
    # (e.g. "New York Today" roundups, "Lesson of the Day" Learning Network pieces)
    foreign_kicker = bool(kicker_norm) and not has_kicker_tag
    # recommended keep-flag: modern tagged entries, plus the old weekly/archive column
    # (title was literally "Metropolitan Diary", blank kicker), minus the obvious noise
    is_diary_entry = (
        (has_kicker_tag or title_has_phrase)
        and not is_meta_article
        and not is_correction
        and not foreign_kicker
    )

    row.update(
        {
            "headline_main": main,
            "headline_kicker": headline.get("kicker"),
            "headline_print": headline.get("print_headline"),
            "byline_original": byline.get("original"),
            "locations": values_for("Location"),
            "subjects": values_for("Subject"),
            "persons": values_for("Person"),
            "organizations": values_for("Organization"),
            "image_url": default_img.get("url")
            if isinstance(default_img, dict)
            else default_img,
            "image_thumbnail_url": thumb_img.get("url")
            if isinstance(thumb_img, dict)
            else thumb_img,
            "image_caption": multimedia.get("caption"),
            "image_credit": multimedia.get("credit"),
            "pub_year": int(pub_date[:4]) if pub_date[:4].isdigit() else None,
            # provenance + classification: filter on these after the fetch
            "has_kicker_tag": has_kicker_tag,
            "title_has_phrase": title_has_phrase,
            "foreign_kicker": foreign_kicker,
            "is_meta_article": is_meta_article,
            "is_correction": is_correction,
            "is_diary_entry": is_diary_entry,
        }
    )
    return row


def fetch_window(session, api_key, begin, end, rows) -> int:
    """Page through one date window, splitting it in half if it exceeds the API cap.

    Appends parsed docs to `rows`; returns the number of requests made.
    """
    label = f"{begin[:4]}-{end[:4]}"
    begin_year, end_year = int(begin[:4]), int(end[:4])

    response = fetch_page(session, api_key, begin, end, 0)
    requests_made = 1
    time.sleep(REQUEST_SLEEP)
    hits = (response.get("metadata") or {}).get("hits", 0)

    # Over the cap and still spanning multiple years -> split and recurse.
    if hits > API_CAP and end_year > begin_year:
        mid = (begin_year + end_year) // 2
        print(f"[{label}] hits={hits} > {API_CAP}; splitting at {mid}")
        requests_made += fetch_window(session, api_key, f"{begin_year}0101", f"{mid}1231", rows)
        requests_made += fetch_window(session, api_key, f"{mid + 1}0101", f"{end_year}1231", rows)
        return requests_made

    pages = min(MAX_PAGES, math.ceil(hits / PAGE_SIZE)) if hits else 0
    print(f"[{label}] hits={hits} -> {pages} page(s)")
    if hits > API_CAP:
        print(f"    WARNING: single-year window {label} has {hits} > {API_CAP}; "
              f"only the first {API_CAP} are reachable.")

    page = 0
    while page < pages:
        if page > 0:  # page 0 already fetched above
            response = fetch_page(session, api_key, begin, end, page)
            requests_made += 1
            time.sleep(REQUEST_SLEEP)
        docs = response.get("docs") or []
        if not docs:
            break
        rows.extend(parse_doc(d) for d in docs)
        page += 1
    return requests_made


# --- output ------------------------------------------------------------------


def write_output(rows: list[dict]) -> "pl.DataFrame":
    """Normalize rows to a stable schema, dedupe, and write the parquet. Returns the df.

    Called after every window as a checkpoint, so a rate-limit death (or any
    interruption) still leaves a valid parquet of everything fetched so far.
    """
    # Docs from different eras return slightly different top-level keys, so
    # union them (first-seen order) and fill gaps.
    columns = list(dict.fromkeys(k for r in rows for k in r))
    norm = [{c: r.get(c) for c in columns} for r in rows]
    df = (
        pl.DataFrame(norm)
        .unique(subset=["web_url"], keep="first")  # dedupe across window boundaries
        .sort("pub_date")
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.write_parquet(PARQUET_PATH)
    return df


# --- main --------------------------------------------------------------------


def main() -> None:
    api_key = get_nyt_key()
    if not api_key:
        raise SystemExit(f"NYT_KEY not found in {ENV_PATH}")

    print(
        f"Fetching Metropolitan Diary {START_YEAR}-{END_YEAR} (title OR kicker) "
        f"in <={WINDOW_YEARS}y windows (~{REQUEST_SLEEP:.0f}s/request).\n"
    )

    session = requests.Session()
    rows: list[dict] = []
    n_requests = 0
    for begin, end in date_windows(START_YEAR, END_YEAR, WINDOW_YEARS):
        n_requests += fetch_window(session, api_key, begin, end, rows)
        if rows:  # checkpoint after each window
            df = write_output(rows)
            print(f"    checkpoint: {df.height} rows -> {PARQUET_PATH.name}")

    if not rows:
        print("\nNo articles fetched.")
        return

    df = write_output(rows)
    print("\n--- done ---")
    print(f"articles fetched:  {df.height}")
    print(f"  likely entries:  {int(df['is_diary_entry'].sum())}  (is_diary_entry=True)")
    print(f"  has_kicker_tag:  {int(df['has_kicker_tag'].sum())}")
    print(f"  title_has_phrase:{int(df['title_has_phrase'].sum())}")
    print(f"  filtered noise:  meta={int(df['is_meta_article'].sum())} "
          f"foreign_kicker={int(df['foreign_kicker'].sum())} "
          f"correction={int(df['is_correction'].sum())}")
    print(f"date range:        {df['pub_date'].min()}  ->  {df['pub_date'].max()}")
    print(f"columns:           {df.width}")
    print(f"requests used:     {n_requests}")
    print(f"wrote:             {PARQUET_PATH}")


if __name__ == "__main__":
    main()
