"""Turn cached article HTML into output/diary_entries.parquet -- one row per entry.

`extract.py fetch` caches each page's article body under output/html_cache/. This
step reads only that local cache (no network), so iterate on it freely:

    uv run data/parse.py            # all cached pages -> output/diary_entries.parquet
    uv run data/parse.py --limit 3  # only the first few pages (for calibration)

Two layouts, picked by publication year:

* Modern (2015+): each entry is an <h2> title, a "Dear Diary:" paragraph, the
  story, and a trailing <em> author with a leading dash. We split on the <h2>
  boundaries -- see `parse_modern`.

* Legacy (pre-2015): no per-entry <h2>. Titles and authors are inline text --
  the title in ALL CAPS before "Dear Diary", the author after the body either in
  ALL CAPS, italicized, dash-prefixed, or just a plain name on its own line (or
  tacked onto the end of the last sentence). We walk the paragraphs and use
  "Dear Diary" + author markers as the entry boundaries -- see `parse_legacy`.
"""

from __future__ import annotations

import argparse
import hashlib
import re

import lxml.html
import polars as pl

from extract import ENTRIES_PARQUET, OUT_DIR, cache_path, guid_for, load_meta

# Pages published before this use the legacy layout; 2015+ use modern <h2> markup.
LEGACY_BEFORE_YEAR = 2015

DEAR_DIARY_RE = re.compile(r"dear\s+diary\s*:?", re.IGNORECASE)
# leading em-dash / en-dash / hyphen(s) (and any spaces) before an author name
LEADING_DASH_RE = re.compile(r"^[\s—–\-]+")


def entry_id(uri: str | None, author: str | None, title: str | None, body: str | None) -> str:
    """Stable, content-based id for a single diary entry.

    A `uri` identifies a whole Metropolitan Diary column (~4 reader stories), so
    it is NOT unique per entry -- and neither is (uri, author), since most entries
    carry no byline. Hash the entry's actual content instead, so every story gets
    its own key. That key is what makes the downstream entities/geocode passes
    addressable (and resumable) per entry rather than collapsing several stories
    from the same column onto one row.
    """
    raw = "\x1f".join([uri or "", author or "", title or "", body or ""]).encode("utf-8")
    return "e" + hashlib.sha1(raw).hexdigest()[:16]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _text(el) -> str:
    return _norm(el.text_content())


# --- modern layout (2015+) ----------------------------------------------------


def parse_modern(body_html: str, meta: dict) -> list[dict]:
    """Split one modern page's article HTML into entries.

    Walks the article body in document order. Each <h2> opens a new entry; the
    paragraphs that follow are its body until the next <h2>. The trailing <em>
    in a block is the author (we strip the leading dash). Falls back to the
    metadata headline/byline when a page has no entry-level <h2>s.
    """
    tree = lxml.html.fromstring(body_html)

    # Flatten to the block elements we care about, in document order.
    blocks = tree.xpath(".//h1 | .//h2 | .//h3 | .//p")
    entries: list[dict] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur and (cur["body"] or cur["author"]):
            cur["body"] = "\n\n".join(cur["body"]).strip()
            entries.append(cur)
        cur = None

    for el in blocks:
        tag = el.tag.lower()
        txt = _text(el)
        if tag in ("h2", "h3"):  # entry boundary
            flush()
            cur = {"title": txt, "author": None, "body": []}
            continue
        if cur is None:
            continue  # preamble before the first heading
        # author = a paragraph that is just a trailing italic name
        ems = el.xpath("./em | ./i")
        em_text = " ".join(_text(e) for e in ems).strip()
        if em_text and len(em_text) >= len(txt) - 3:  # the <p> is essentially the <em>
            cur["author"] = LEADING_DASH_RE.sub("", em_text).strip()
            continue
        # once we've hit the author, ignore anything trailing (footers, notes)
        if txt and cur["author"] is None:
            cur["body"].append(txt)
    flush()

    # No entry headings on this page -> treat the whole thing as one entry.
    if not entries:
        body = "\n\n".join(_text(p) for p in tree.xpath(".//p") if _text(p)).strip()
        author = (meta.get("byline_original") or "").removeprefix("By ").strip() or None
        entries = [{"title": meta.get("headline_main"), "author": author, "body": body}]
    return entries


# --- legacy layout (pre-2015) -------------------------------------------------
#
# These pages have no per-entry headings. Each entry is roughly:
#
#     [TITLE IN CAPS]      (optional, before "Dear Diary")
#     Dear Diary:
#     ...the story...
#     AUTHOR NAME          (after the body -- ALL CAPS, italic, dash-prefixed, or
#                           a plain name; on its own line or tacked onto the end)
#
# but the markup is inconsistent across the years, so we lean on three signals:
# "Dear Diary" starts an entry, an author marker ends one, and an ALL-CAPS line
# is a title (before the body) or an author (after it).

_JUNK_LINES = {"advertisement", "•", "·", "*"}
# lowercase words that betray prose (so a short line isn't mistaken for a name)
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "for", "with", "in", "on", "at",
    "by", "from", "as", "but", "that", "this", "is", "was", "were", "are", "i",
    "my", "we", "he", "she", "it", "they", "you", "our", "his", "her", "their",
    "so", "if", "when", "who", "had", "has", "have", "not", "no", "all",
}
_NAME_PARTICLES = {"van", "von", "de", "del", "della", "la", "da", "di", "du",
                   "den", "der", "ter", "bin", "al", "mc", "mac"}
# a run of ALL-CAPS words at the very start of a line -> inline title (e.g.
# "OUTDOOR CONCERT For me, it's a cliffhanger" where the title runs into the body)
_LEAD_TITLE_RE = re.compile(r"^([A-Z][A-Z'’.\-]+(?:\s+[A-Z][A-Z'’.\-]+){0,4})\s+(?=[A-Z][a-z])")


def _is_allcaps(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def _looks_like_name(s: str) -> bool:
    """A short line that reads as a person's name (ALL-CAPS names included)."""
    s = LEADING_DASH_RE.sub("", s.strip().strip("•·")).strip()
    s = re.sub(r"\s*(?:\.\s*){2,}$", "", s)  # drop the old "Name . . ." sign-off
    if not s or len(s) > 45 or any(ch.isdigit() for ch in s):
        return False
    if ":" in s:  # a colon means dialogue ("Woman: ..."), not a byline
        return False
    if s[-1] in ",;:!?…”\"'":  # a name doesn't end on sentence/quote punctuation
        return False
    words = s.split()
    if not 1 <= len(words) <= 6:
        return False
    if any(w.lower().strip(".,'’") in _STOPWORDS for w in words):
        return False
    for w in words:
        w = w.strip("•·().,'’\"")
        if not w:
            continue
        if w.lower().rstrip(".") in _NAME_PARTICLES:
            continue
        if not w[0].isupper():
            return False
    return True


def _classify(line: str, italic: bool, nxt: str | None, has_body: bool) -> str:
    """One of DD / TITLE / AUTHOR / BODY, using a one-line lookahead.

    An ALL-CAPS / name-like line is a TITLE when prose follows it and an AUTHOR
    when it follows a body and is itself followed by the next entry (a "Dear
    Diary", another name, or the end of the page).
    """
    if DEAR_DIARY_RE.match(line):
        return "DD"
    dash = bool(LEADING_DASH_RE.match(line)) and _looks_like_name(line)
    capsy = _is_allcaps(line) and len(line.split()) <= 6 and len(line) <= 45
    if not (dash or italic and _looks_like_name(line) or _looks_like_name(line) or capsy):
        return "BODY"
    if dash or (italic and _looks_like_name(line)):  # an explicit author marker
        return "AUTHOR" if has_body else "BODY"
    nxt_prose = nxt is not None and not (
        DEAR_DIARY_RE.match(nxt) or _looks_like_name(nxt) or _is_allcaps(nxt)
    )
    if capsy and nxt_prose:  # ALL CAPS heading sitting above its story
        return "TITLE"
    if has_body:
        return "AUTHOR"
    if capsy:
        return "TITLE"
    return "BODY"


def _clean_author(s: str) -> str | None:
    s = LEADING_DASH_RE.sub("", s.strip().strip("•·")).strip()
    s = re.sub(r"\s*(?:\.\s*){2,}$", "", s)  # drop the old "Name . . ." sign-off
    s = s.strip(" ,;:")
    if _is_allcaps(s):  # all-caps is a styling artifact -> normalize the casing
        s = s.title()
    return s or None


def _peel_trailing_author(part: str) -> list[str]:
    """Split a paragraph that ends with an inline author into [body, author]."""
    # "...some story. -- WALTER GRAY"  /  "...some story — Jane Doe"
    m = re.search(r"(.*?)(?:[—–]\s*|--\s*)([A-Z][^—–]{1,45})$", part)
    if m and len(m.group(1).strip()) >= 15 and _looks_like_name(m.group(2)):
        return [m.group(1).strip(), "-- " + m.group(2).strip()]
    # "...end of the story.” Daniel R. Garodnick"  (name after sentence punctuation).
    # `.*?` is non-greedy so initials' periods ("J. J. Levine") aren't eaten into
    # the body; require >=2 words so a stray final word isn't taken for a name.
    m = re.search(r"(.*?[.!?…”\"’])\s+([A-Z][\w.'’\-]*(?:\s+[A-Z][\w.'’\-]*){1,3})$", part)
    if m and len(m.group(1).strip()) >= 25 and _looks_like_name(m.group(2)):
        return [m.group(1).strip(), m.group(2).strip()]
    return [part]


def _peel_leading_title(line: str) -> tuple[str | None, str]:
    """Split a body line that starts with an inline ALL-CAPS title."""
    m = _LEAD_TITLE_RE.match(line)
    if not m:
        return None, line
    title = m.group(1).strip()
    if len(re.sub(r"[^A-Z]", "", title)) < 5:  # need a real all-caps run, not "I A"
        return None, line
    return title, line[m.end():].strip()


def _legacy_lines(blocks) -> list[tuple[str, bool]]:
    """Flatten blocks to (text, is_italic) lines, splitting inline boundaries."""
    out: list[tuple[str, bool]] = []
    for el in blocks:
        txt = _text(el)
        if not txt or txt.lower().strip("•·* ") in {"advertisement", ""}:
            continue
        if set(txt) <= {"•", "·", "*", " "}:
            continue
        ems = el.xpath("./em | ./i | ./b | ./strong")
        em_text = " ".join(_text(e) for e in ems).strip()
        italic = bool(em_text) and len(em_text) >= len(txt) - 3
        # split before each "Dear Diary", then peel any trailing inline author
        for part in re.split(r"(?i)(?=\bdear\s+diary\b)", txt):
            part = part.strip(" •·")
            if not part:
                continue
            for frag in _peel_trailing_author(part):
                frag = frag.strip(" •·")
                if frag:
                    out.append((frag, italic))
    return out


def parse_legacy(blocks, meta: dict) -> list[dict]:
    lines = _legacy_lines(blocks)
    entries: list[dict] = []
    title: str | None = None
    body: list[str] = []
    author: str | None = None

    def flush():
        nonlocal title, body, author
        if body or author:
            entries.append(
                {"title": title, "author": author, "body": "\n\n".join(body).strip()}
            )
        title, body, author = None, [], None

    n = len(lines)
    for i, (text, italic) in enumerate(lines):
        nxt = lines[i + 1][0] if i + 1 < n else None
        kind = _classify(text, italic, nxt, bool(body))
        if kind == "DD":
            if body or author:
                flush()
            body.append("Dear Diary:")
            rest = DEAR_DIARY_RE.sub("", text, count=1).strip(" :—–-").strip()
            if rest:
                body.append(rest)
        elif kind == "TITLE":
            if body or author:
                flush()
            title = text
        elif kind == "AUTHOR":
            author = _clean_author(text)
            flush()
        else:  # BODY
            if not body and not title:  # an entry may lead with an inline title
                peeled, text = _peel_leading_title(text)
                if peeled:
                    title = peeled
            if text:
                body.append(text)
    flush()
    return entries


# --- dispatch -----------------------------------------------------------------


def parse_page(body_html: str, meta: dict) -> list[dict]:
    year = meta.get("pub_year")
    if year is not None and year < LEGACY_BEFORE_YEAR:
        tree = lxml.html.fromstring(body_html)
        blocks = tree.xpath(".//h1 | .//h2 | .//h3 | .//p")
        entries = parse_legacy(blocks, meta)
        if not entries:  # nothing matched -> whole page as one entry
            body = "\n\n".join(_text(p) for p in blocks if _text(p)).strip()
            byline = (meta.get("byline_original") or "").removeprefix("By ").strip()
            entries = [{"title": meta.get("headline_main"), "author": byline or None, "body": body}]
    else:
        entries = parse_modern(body_html, meta)

    for e in entries:
        e["uri"] = meta["uri"]
        e["web_url"] = meta["web_url"]
        e["pub_date"] = meta["pub_date"]
        e["pub_year"] = meta["pub_year"]
    return entries


def cmd_parse(limit: int | None) -> None:
    df = load_meta(limit)
    rows: list[dict] = []
    missing = 0
    total = df.height
    for i, meta in enumerate(df.iter_rows(named=True), start=1):
        if i % 100 == 0 or i == total:
            print(f"parsing page {i}/{total}  (entries so far: {len(rows)})")
        path = cache_path(guid_for(meta["uri"], meta["web_url"]))
        if not path.exists():
            missing += 1
            continue
        rows.extend(parse_page(path.read_text(encoding="utf-8"), meta))

    if not rows:
        raise SystemExit(
            f"no cached HTML found (missing={missing}); run the fetch step first"
        )

    out = (
        pl.DataFrame(rows)
        # drop "From the comments" sections -- not reader diary entries
        .filter(
            pl.col("title").is_null()
            | (
                pl.col("title").str.strip_chars().str.to_lowercase()
                != "from the comments"
            )
        )
        .with_columns(pl.col("body").str.len_chars().alias("body_len"))
        # drop stray fragments (e.g. a leftover affiliation line)
        .filter(pl.col("body_len") >= 20)
        # dedupe entries that recur across overlapping pages
        .unique(subset=["title", "body"], keep="first")
        # a stable per-entry key, hashed from the entry's content (see entry_id):
        # (uri, author) alone collides, since one column holds several stories and
        # most have no byline.
        .with_columns(
            pl.struct("uri", "author", "title", "body")
            .map_elements(
                lambda s: entry_id(s["uri"], s["author"], s["title"], s["body"]),
                return_dtype=pl.String,
            )
            .alias("entry_id")
        )
        .sort("pub_date")
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_parquet(ENTRIES_PARQUET)

    n_dup_ids = out.height - out["entry_id"].n_unique()
    print(f"pages parsed: {df.height - missing}  (missing cache: {missing})")
    print(f"entries:      {out.height}")
    if n_dup_ids:
        print(f"  WARNING: {n_dup_ids} duplicate entry_id(s) -- entries share content")
    print(f"  with author:  {int(out['author'].is_not_null().sum())}")
    print(f"  with title:   {int(out['title'].is_not_null().sum())}")
    print(f"  empty body:   {int((out['body_len'] == 0).sum())}")
    print(f"wrote -> {ENTRIES_PARQUET}")
    with pl.Config(fmt_str_lengths=60, tbl_rows=6):
        print(out.select("title", "author", "body_len").head(6))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="only the first N pages (for calibration)")
    cmd_parse(ap.parse_args().limit)


if __name__ == "__main__":
    main()
