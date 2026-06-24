"""Tag each diary entry with the NYC places it mentions, via a local LM Studio model.

`parse.py` gives us output/diary_entries.parquet -- one reader entry per
row. This step sends each entry to your locally-served model and asks four
questions:

    1. Is the subway mentioned?      -> subway_mentioned (bool) + subway_lines (list)
    2. Is a proper-named place mentioned? -> specific_location_mentioned (bool)
                                         + specific_locations (list: proper-named
                                           restaurants, venues, parks, stations...;
                                           generic "a park"/"a diner" excluded)
    3. Is a neighborhood mentioned?   -> neighborhood_mentioned (bool)
                                         + neighborhoods (list)
    4. Is a borough mentioned?        -> borough_mentioned (bool) + boroughs (list,
                                         explicitly named only, never inferred)

The model returns *structured output* (a fixed JSON schema), so every response is
a clean record we can drop straight into a column.

LM Studio exposes an OpenAI-compatible server (default http://localhost:1234/v1)
that answers synchronously -- there's no async batch job like the Anthropic API,
so this is a single streaming pass over the entries rather than submit/poll/collect.
Each entry becomes one chat completion, keyed by its `entry_id` (a stable
content hash assigned in parse.py) -- the same key the final dataset uses.

    uv run data/entities.py                 # extract all entries -> output/diary_entities.parquet
    uv run data/entities.py --dry-run       # preview the prompt for row 0, call nothing
    uv run data/entities.py --limit 3       # only the first 3 entries (for calibration)
    uv run data/entities.py --workers 4     # send N requests concurrently
    uv run data/entities.py --batch-size 50 # flush results to the parquet every 50 entries
    uv run data/entities.py --test          # 5 random entries/year -> diary_entities_test.parquet

The --test run is a quality check: it samples a few entries from every publication
year (seeded, so it's reproducible), runs them through the prompt, and writes the
output next to each entry's web_url/author/title/body so the results are easy to
eyeball. It never touches the main diary_entities.parquet. Tune --per-year and --seed.

The run is resumable: rows already present (and error-free) in
output/diary_entities.parquet are skipped (matched by entry_id), and results are written out in batches
(every --batch-size completions, default 25), so an interrupted run picks up where
it left off and never re-extracts an entry that already exists. Calibrate the
prompt first:

    uv run data/entities.py --dry-run --limit 3

Needs LM Studio running with a model loaded. Override the defaults via env in the
repo-root .env:  LMSTUDIO_BASE_URL (default http://localhost:1234/v1) and
LMSTUDIO_MODEL (default: whatever model LM Studio currently has loaded).
"""

from __future__ import annotations

import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import dotenv
import polars as pl
from openai import OpenAI
from pydantic import BaseModel
from tqdm import tqdm

DATA_DIR = Path(__file__).resolve().parent
OUT_DIR = DATA_DIR / "output"
ENTRIES_PARQUET = OUT_DIR / "diary_entries.parquet"
ENTITIES_PARQUET = OUT_DIR / "diary_entities.parquet"
# --test writes its quality-review sample here instead of the main parquet.
TEST_PARQUET = OUT_DIR / "diary_entities_test.parquet"

ENV_PATH = DATA_DIR.parent / ".env"  # repo-root .env, CWD-independent
dotenv.load_dotenv(ENV_PATH)

# LM Studio's OpenAI-compatible server. The API key is ignored by LM Studio but
# the SDK insists on a non-empty value. Leave LMSTUDIO_MODEL unset to use whatever
# model is currently loaded in LM Studio (resolved via /v1/models at runtime).
BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
API_KEY = os.getenv("LMSTUDIO_API_KEY", "lm-studio")
MODEL = os.getenv("LMSTUDIO_MODEL", "qwen/qwen3.5-9b")  # empty -> auto-detect loaded

MAX_TOKENS = 1024  # the structured record is tiny; this is plenty of headroom
TEMPERATURE = 0.0  # extraction, not creativity -- keep it deterministic
CHECKPOINT_EVERY = 25  # write the parquet this often so a crash loses little


# --- the reference lists the model checks each entry against ------------------

# Canonical subway route tokens. Express diamonds (<6>, 6X) normalize to the base
# route; the three S shuttles all report as "S"; the Staten Island Railway is SIR.
SUBWAY_LINES = "1 2 3 4 5 6 7 A B C D E F G J L M N Q R W Z S SIR".split()

# The five boroughs, canonical spelling. "the Bronx" -> "Bronx"; the model is told
# to name a borough only when the entry states it, never to infer it from a
# neighborhood -- so these double as both the reference list and the output tokens.
BOROUGHS = "Manhattan Brooklyn Queens Bronx".split() + ["Staten Island"]

# Grouped so the prompt is readable and the list stays easy to extend. Not
# exhaustive -- the model may also return a clearly-named neighborhood that's
# absent here -- but it anchors the canonical spelling for the common ones.
NEIGHBORHOODS = {
    "Manhattan": [
        "Financial District",
        "Battery Park City",
        "Tribeca",
        "SoHo",
        "NoHo",
        "Nolita",
        "Little Italy",
        "Chinatown",
        "Two Bridges",
        "Lower East Side",
        "East Village",
        "Greenwich Village",
        "West Village",
        "Meatpacking District",
        "Chelsea",
        "Flatiron District",
        "Gramercy",
        "Stuyvesant Town",
        "Kips Bay",
        "Murray Hill",
        "NoMad",
        "Koreatown",
        "Midtown",
        "Midtown East",
        "Hell's Kitchen",
        "Clinton",
        "Times Square",
        "Theater District",
        "Garment District",
        "Turtle Bay",
        "Sutton Place",
        "Hudson Yards",
        "Lincoln Square",
        "Upper West Side",
        "Upper East Side",
        "Lenox Hill",
        "Yorkville",
        "Carnegie Hill",
        "Manhattan Valley",
        "Morningside Heights",
        "Hamilton Heights",
        "Harlem",
        "Central Harlem",
        "East Harlem",
        "Spanish Harlem",
        "Washington Heights",
        "Hudson Heights",
        "Inwood",
        "Marble Hill",
        "Roosevelt Island",
    ],
    "Brooklyn": [
        "Greenpoint",
        "Williamsburg",
        "Bushwick",
        "Bedford-Stuyvesant",
        "Bed-Stuy",
        "Clinton Hill",
        "Fort Greene",
        "DUMBO",
        "Vinegar Hill",
        "Brooklyn Heights",
        "Downtown Brooklyn",
        "Boerum Hill",
        "Cobble Hill",
        "Carroll Gardens",
        "Red Hook",
        "Gowanus",
        "Park Slope",
        "Prospect Heights",
        "Crown Heights",
        "Prospect Lefferts Gardens",
        "Windsor Terrace",
        "Kensington",
        "Ditmas Park",
        "Flatbush",
        "East Flatbush",
        "Midwood",
        "Borough Park",
        "Sunset Park",
        "Bay Ridge",
        "Dyker Heights",
        "Bensonhurst",
        "Gravesend",
        "Sheepshead Bay",
        "Brighton Beach",
        "Coney Island",
        "Manhattan Beach",
        "Marine Park",
        "Mill Basin",
        "Canarsie",
        "East New York",
        "Brownsville",
        "Cypress Hills",
    ],
    "Queens": [
        "Astoria",
        "Ditmars Steinway",
        "Long Island City",
        "Sunnyside",
        "Woodside",
        "Jackson Heights",
        "Elmhurst",
        "Corona",
        "Flushing",
        "Forest Hills",
        "Rego Park",
        "Kew Gardens",
        "Briarwood",
        "Jamaica",
        "Hollis",
        "St. Albans",
        "Queens Village",
        "Bayside",
        "Whitestone",
        "College Point",
        "Fresh Meadows",
        "Glendale",
        "Ridgewood",
        "Maspeth",
        "Middle Village",
        "Howard Beach",
        "Ozone Park",
        "Richmond Hill",
        "Woodhaven",
        "Far Rockaway",
        "Rockaway Beach",
        "Rockaway Park",
        "Breezy Point",
        "Douglaston",
        "Little Neck",
    ],
    "Bronx": [
        "Mott Haven",
        "Port Morris",
        "Melrose",
        "Hunts Point",
        "Longwood",
        "Morrisania",
        "South Bronx",
        "Concourse",
        "Highbridge",
        "Morris Heights",
        "University Heights",
        "Fordham",
        "Belmont",
        "Tremont",
        "Mount Hope",
        "Kingsbridge",
        "Riverdale",
        "Spuyten Duyvil",
        "Norwood",
        "Bedford Park",
        "Pelham Bay",
        "Pelham Parkway",
        "Throgs Neck",
        "City Island",
        "Co-op City",
        "Soundview",
        "Castle Hill",
        "Parkchester",
        "Morris Park",
        "Wakefield",
        "Williamsbridge",
    ],
    "Staten Island": [
        "St. George",
        "Tompkinsville",
        "Stapleton",
        "Clifton",
        "Rosebank",
        "New Brighton",
        "West Brighton",
        "Port Richmond",
        "Mariners Harbor",
        "Tottenville",
        "Great Kills",
        "Eltingville",
        "Annadale",
        "New Dorp",
        "Dongan Hills",
        "Todt Hill",
        "Willowbrook",
        "Bulls Head",
    ],
}


def _neighborhood_block() -> str:
    return "\n".join(
        f"  {boro}: {', '.join(hoods)}" for boro, hoods in NEIGHBORHOODS.items()
    )


# Flat lookup (lowercased) of every reference neighborhood. We don't try to keep
# neighborhoods out of specific_locations in the prompt -- a small model drifts on
# that anyway -- and instead drop them here, after extraction.
NEIGHBORHOOD_NAMES = {hood.lower() for hoods in NEIGHBORHOODS.values() for hood in hoods}


def drop_neighborhoods(locations: list[str]) -> list[str]:
    """Clean a raw specific_locations list: drop any reference neighborhood (it
    belongs in `neighborhoods`, not pinned as a place), then keep at most the first
    surviving place -- the one-location rule the downstream geocoder assumes."""
    kept = [
        loc for loc in locations if (loc or "").strip().lower() not in NEIGHBORHOOD_NAMES
    ]
    return kept[:1]


# A crisp recap of the hard rules. Small local models drift on multi-clause
# instructions, so we restate the constraints that matter most as a short
# checklist at the end of the prompt, where recency makes them most salient.
OUTPUT_RULES = """RULES -- follow exactly:
- Use only what the entry states. Never infer or guess a place that isn't named.
- If something isn't mentioned, set its boolean to false and its list to [].
- subway_lines: only lines explicitly named, normalized to the base route; [] otherwise.
- specific_locations: AT MOST ONE item -- the single PROPER-NAMED place the story centers on. \
A generic, unnamed place type ("a park", "a diner", "the laundromat") does NOT count -- leave it out. \
Never return more than one. Boroughs and "New York"/"the city" do NOT go here.
- neighborhoods: NYC neighborhoods only; a borough on its own is not a neighborhood.
- boroughs: only a borough the entry explicitly names; never inferred from a neighborhood or street."""

# Worked input -> output pairs. Few-shot examples lift a 9B model's rule-following
# far more than prose does -- especially the one-location rule (example 2), the
# proper-name rule (example 3: a generic, unnamed place type is NOT a specific
# location), the bus-is-not-the-subway distinction (example 4), and the borough
# rules: example 4 shows a neighborhood does NOT imply its borough, example 5 shows
# an explicitly named borough sitting alongside a neighborhood.
FEW_SHOT = """Entry:
Title: A T-Bone Rides the 1

On the platform at the 34th Street station, a man boarded the downtown 1 train carrying a wrapped steak.
Output: {"subway_mentioned": true, "subway_lines": ["1"], "specific_location_mentioned": true, "specific_locations": ["34th Street"], "neighborhood_mentioned": false, "neighborhoods": [], "borough_mentioned": false, "boroughs": []}

Entry:
Title: A Long Walk

We started at the Museum of Natural History, cut through Riverside Park, and ended at a diner on West 110th Street.
Output: {"subway_mentioned": false, "subway_lines": [], "specific_location_mentioned": true, "specific_locations": ["Museum of Natural History"], "neighborhood_mentioned": false, "neighborhoods": [], "borough_mentioned": false, "boroughs": []}

Entry:
Title: Coffee and a Crossword

I grabbed a coffee at the corner deli, then did the crossword on a bench in the park.
Output: {"subway_mentioned": false, "subway_lines": [], "specific_location_mentioned": false, "specific_locations": [], "neighborhood_mentioned": false, "neighborhoods": [], "borough_mentioned": false, "boroughs": []}

Entry:
Title: Waiting on the B61

Standing at the bus stop in Red Hook, I chatted with a neighbor about the weather.
Output: {"subway_mentioned": false, "subway_lines": [], "specific_location_mentioned": false, "specific_locations": [], "neighborhood_mentioned": true, "neighborhoods": ["Red Hook"], "borough_mentioned": false, "boroughs": []}

Entry:
Title: Sunday in Brooklyn

I spent the afternoon wandering through Park Slope before catching the Q back to the Bronx.
Output: {"subway_mentioned": true, "subway_lines": ["Q"], "specific_location_mentioned": false, "specific_locations": [], "neighborhood_mentioned": true, "neighborhoods": ["Park Slope"], "borough_mentioned": true, "boroughs": ["Brooklyn", "Bronx"]}"""


SYSTEM_PROMPT = f"""You analyze entries from The New York Times "Metropolitan Diary" \
-- short reader-submitted stories about life in New York City.

For ONE entry, extract four things. Use only what the entry's text states -- never \
infer or guess a place that isn't named. If something isn't mentioned, set its boolean \
to false and its list to [].

1. SUBWAY
subway_mentioned = true if the entry refers to the NYC subway: a line, a station, a \
platform, a turnstile, or riding "the train"/"the subway"/"the express". List every \
line named in subway_lines as its canonical token:
  Numbered: 1 2 3 4 5 6 7   Lettered: A B C D E F G J L M N Q R W Z   Shuttle: S   Staten Island Railway: SIR
Normalize express variants to the base route ("6 express", "<6>", "6X" -> "6"). If no \
line is named, leave subway_lines empty. Buses, the LIRR, Metro-North, PATH, and taxis \
are NOT the subway.

2. SPECIFIC LOCATION
specific_location_mentioned = true ONLY if the entry names a place by its PROPER NAME -- \
a place you could find on a map by that name: a named restaurant, bar, cafe, shop or \
business ("Katz's Delicatessen", "the Strand"); a named building or venue ("Radio City \
Music Hall"); a named park ("Central Park", "Prospect Park"); a named museum ("the Met"); \
a named subway station ("Grand Central"); a named bridge ("the Brooklyn Bridge"); a street \
address; or a cross-street/intersection ("Broadway and 72nd"). \
A generic, unnamed place TYPE does NOT count and must be left out: "a park", "the \
laundromat", "a diner", "the deli", "a bodega", "the bus stop", "a coffee shop" -- these \
have no proper name, so they are not specific locations. \
List EXACTLY ONE place in specific_locations -- the proper-named one the story most centers \
on, written roughly as it appears. Never more than one. Do NOT put boroughs or \
"New York"/"the city" here.

3. NEIGHBORHOOD
neighborhood_mentioned = true if the entry names a NYC neighborhood. List each in \
neighborhoods, preferring the canonical name from the reference list below; include \
off-list neighborhoods by their common name. A borough on its own is NOT a neighborhood.

4. BOROUGH
borough_mentioned = true if the entry explicitly names a NYC borough. List each in \
boroughs using its canonical name: Manhattan, Brooklyn, Queens, Bronx, Staten Island \
("the Bronx" -> "Bronx"). Name a borough only when the entry states it -- do NOT infer \
it from a neighborhood, street, or landmark.

Reference list of NYC neighborhoods (by borough):
{_neighborhood_block()}

{OUTPUT_RULES}

EXAMPLES
{FEW_SHOT}"""


# --- structured output: the schema the model must return, and its validator ---

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "subway_mentioned": {
            "type": "boolean",
            "description": "Does the entry refer to the NYC subway?",
        },
        "subway_lines": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Canonical route tokens named, e.g. ['A','C','7']. Empty if none named.",
        },
        "specific_location_mentioned": {
            "type": "boolean",
            "description": "Does the entry name a place by its proper name (e.g. 'Central Park', not 'a park')?",
        },
        "specific_locations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Main proper-named place, roughly as written. Generic unnamed place types ('a park', 'a diner') excluded.",
        },
        "neighborhood_mentioned": {
            "type": "boolean",
            "description": "Does the entry name a NYC neighborhood?",
        },
        "neighborhoods": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Each neighborhood named, canonical spelling preferred.",
        },
        "borough_mentioned": {
            "type": "boolean",
            "description": "Does the entry explicitly name a NYC borough?",
        },
        "boroughs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Each borough explicitly named (Manhattan, Brooklyn, Queens, Bronx, Staten Island). Empty if none, never inferred.",
        },
    },
    "required": [
        "subway_mentioned",
        "subway_lines",
        "specific_location_mentioned",
        "specific_locations",
        "neighborhood_mentioned",
        "neighborhoods",
        "borough_mentioned",
        "boroughs",
    ],
    "additionalProperties": False,
}

# OpenAI-style response_format wrapper LM Studio understands. `strict` makes
# llama.cpp constrain generation to the schema (GBNF grammar) so the model can't
# drift off-format.
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "extraction",
        "strict": True,
        "schema": EXTRACTION_SCHEMA,
    },
}


class Extraction(BaseModel):
    subway_mentioned: bool
    subway_lines: list[str]
    specific_location_mentioned: bool
    specific_locations: list[str]
    neighborhood_mentioned: bool
    neighborhoods: list[str]
    borough_mentioned: bool
    boroughs: list[str]


def message_json(message) -> str:
    """Pull the JSON object text out of a chat-completion message.

    The response_format grammar guarantees the model emits one JSON object, but
    LM Studio's reasoning parser for the Qwen3 family routes that object into
    `reasoning_content` and leaves `content` empty -- so fall back to it, then
    slice out the {...} in case any stray prose rode along.
    """
    text = (message.content or "").strip()
    if not text:
        extra = getattr(message, "model_extra", None) or {}
        text = (extra.get("reasoning_content") or "").strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


# --- keys + client ------------------------------------------------------------
#
# Each row is keyed by its `entry_id` -- a stable content hash assigned in
# parse.py. We carry that column straight through rather than recomputing a key
# here: (uri, author) is NOT unique per entry (one column holds several stories,
# most without a byline), so hashing it would collapse distinct stories together.


def entry_text(row: dict) -> str:
    title = (row.get("title") or "").strip()
    body = (row.get("body") or "").strip()
    head = f"Title: {title}\n\n" if title else ""
    return f"{head}{body}"


def get_client() -> OpenAI:
    # Local inference is slow and the SDK's default timeout is short; give each
    # request plenty of room and a couple of automatic retries.
    return OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=600.0, max_retries=2)


def resolve_model(client: OpenAI) -> str:
    """The model id to call: the LMSTUDIO_MODEL override, else whatever's loaded."""
    if MODEL:
        return MODEL
    try:
        loaded = [m.id for m in client.models.list().data]
    except Exception as e:  # noqa: BLE001 -- server not up / unreachable
        raise SystemExit(
            f"could not reach LM Studio at {BASE_URL} ({e}); is the server running?"
        )
    if not loaded:
        raise SystemExit(
            "LM Studio has no model loaded; load one or set LMSTUDIO_MODEL in .env"
        )
    return loaded[0]


# --- extraction ---------------------------------------------------------------


def extract_one(client: OpenAI, model: str, meta: dict, text: str) -> dict:
    """Send one entry to the model and return its result record (or an error)."""
    rec = {
        **meta,
        "subway_mentioned": None,
        "subway_lines": None,
        "specific_location_mentioned": None,
        "specific_locations": None,
        "neighborhood_mentioned": None,
        "neighborhoods": None,
        "borough_mentioned": None,
        "boroughs": None,
        "error": None,
    }
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format=RESPONSE_FORMAT,
        )
        data = Extraction.model_validate_json(
            message_json(resp.choices[0].message)
        ).model_dump()
        # Drop a neighborhood that slipped into specific_locations and enforce the
        # one-location rule, then keep the bool in sync so geocode.py's filter never
        # disagrees with the list (mentioned=true but no place to geocode).
        data["specific_locations"] = drop_neighborhoods(data["specific_locations"])
        data["specific_location_mentioned"] = bool(data["specific_locations"])
        rec.update(data)
    except Exception as e:  # noqa: BLE001 -- record the failure, keep going
        rec["error"] = str(e)
    return rec


# --- load / merge / write -----------------------------------------------------


def load_entries(limit: int | None) -> pl.DataFrame:
    if not ENTRIES_PARQUET.exists():
        raise SystemExit(f"missing {ENTRIES_PARQUET}; run `parse.py` first")
    # web_url/pub_date/pub_year ride along for --test's quality-review output; the
    # main run only touches entry_id/uri/author/title/body.
    df = (
        pl.read_parquet(ENTRIES_PARQUET)
        .select(
            "entry_id", "uri", "web_url", "author", "title", "body", "pub_date", "pub_year"
        )
        .sort("pub_year", descending=True)
    )
    if limit:
        df = df.head(limit)
    return df


def load_existing() -> dict[str, dict]:
    """Prior results keyed by entry_id, so we can merge new rows into them.

    A parquet written before entry_id existed was keyed (and deduped) on the old
    colliding (uri, author) hash, so we can't trust it: return {} to re-extract
    everything under the per-entry key.
    """
    if not ENTITIES_PARQUET.exists():
        return {}
    df = pl.read_parquet(ENTITIES_PARQUET)
    if "entry_id" not in df.columns:
        return {}
    return {r["entry_id"]: r for r in df.iter_rows(named=True)}


def already_done_ids(existing: dict[str, dict]) -> set[str]:
    """entry_ids that already succeeded *under the current schema* (skip these).

    "Done" requires an error-free row that also carries the borough fields. Rows
    written before the borough column lack it, so they fall out of this set and
    get re-extracted on the next run rather than persisting with empty borough data.
    """
    return {
        rid
        for rid, r in existing.items()
        if r.get("error") is None and r.get("borough_mentioned") is not None
    }


def write_results(records: dict[str, dict]) -> pl.DataFrame:
    out = pl.DataFrame(
        list(records.values()),
        schema={
            "entry_id": pl.String,
            "uri": pl.String,
            "author": pl.String,
            "title": pl.String,
            "subway_mentioned": pl.Boolean,
            "subway_lines": pl.List(pl.String),
            "specific_location_mentioned": pl.Boolean,
            "specific_locations": pl.List(pl.String),
            "neighborhood_mentioned": pl.Boolean,
            "neighborhoods": pl.List(pl.String),
            "borough_mentioned": pl.Boolean,
            "boroughs": pl.List(pl.String),
            "error": pl.String,
        },
    ).sort("uri", "author")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_parquet(ENTITIES_PARQUET)
    return out


# --- run ----------------------------------------------------------------------


def cmd_run(limit: int | None, dry_run: bool, workers: int, batch_size: int) -> None:
    df = load_entries(limit)
    existing = load_existing()
    done = already_done_ids(existing)

    # Skip anything already extracted under the current schema; we only call the
    # model for the rows that remain.
    pending = [
        r for r in df.iter_rows(named=True) if r["entry_id"] not in done
    ]
    print(
        f"{df.height} entries; {df.height - len(pending)} already done (skipped); "
        f"{len(pending)} to do."
    )

    if dry_run:
        if not pending:
            print("nothing to do.")
            return
        sample = pending[0]
        print("\n--- dry run: previewing row 0, called nothing ---")
        print(
            f"model: {MODEL or '(auto: currently-loaded model)'}  base_url: {BASE_URL}"
        )
        print(f"max_tokens: {MAX_TOKENS}  temperature: {TEMPERATURE}")
        print(f"\nsystem prompt ({len(SYSTEM_PROMPT)} chars):\n{SYSTEM_PROMPT}")
        print(f"\nuser content for row 0:\n{entry_text(sample)}")
        return

    if not pending:
        print("nothing to do.")
        return

    client = get_client()
    model = resolve_model(client)
    print(
        f"using model '{model}' at {BASE_URL} "
        f"({workers} worker(s), flushing every {batch_size})\n"
    )

    # Seed the merge map with prior rows; new results overwrite as they arrive.
    records: dict[str, dict] = dict(existing)
    lock = Lock()
    n_ok = n_err = 0
    completed = 0

    def task(row: dict) -> dict:
        meta = {
            "entry_id": row["entry_id"],
            "uri": row["uri"],
            "author": row["author"],
            "title": row["title"],
        }
        return extract_one(client, model, meta, entry_text(row))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(task, r): r for r in pending}
        bar = tqdm(total=len(pending), desc="extracting", unit="entry")
        for fut in as_completed(futures):
            row = futures[fut]
            rec = fut.result()
            rid = row["entry_id"]
            with lock:
                records[rid] = rec
                completed += 1
                if rec["error"] is None:
                    n_ok += 1
                else:
                    n_err += 1
                # Write the whole accumulated set every `batch_size` completions, so
                # an interrupted run keeps its progress and a re-run skips it.
                if completed % batch_size == 0:
                    write_results(records)
                bar.update(1)
                bar.set_postfix(ok=n_ok, err=n_err)
        bar.close()

    out = write_results(records)
    print(f"\ncollected {out.height} rows  (this run: ok={n_ok}, errored={n_err})")
    if n_err:
        print("re-run to retry the errored rows.")
    print(f"wrote -> {ENTITIES_PARQUET}")
    with pl.Config(fmt_str_lengths=40, tbl_rows=8):
        print(
            out.select(
                "title", "subway_lines", "neighborhoods", "boroughs", "specific_locations"
            ).head(8)
        )


# --- test run (quality calibration) -------------------------------------------
#
# A small, seeded sample -- N random entries per publication year -- run through
# the same prompt and written to its own files (never the main parquet), with the
# entry's web_url/author/title/body alongside the model's output so the results
# are easy to eyeball.

# Review-friendly column order for the test output. entry_id/uri ride along (the
# same per-entry key the main dataset uses) so the test sample can be fed straight
# into geocode.py. web_url/body sit last so the verdict columns read first.
TEST_COLUMNS = [
    "pub_year",
    "pub_date",
    "author",
    "title",
    "subway_mentioned",
    "subway_lines",
    "neighborhood_mentioned",
    "neighborhoods",
    "borough_mentioned",
    "boroughs",
    "specific_location_mentioned",
    "specific_locations",
    "error",
    "entry_id",
    "uri",
    "web_url",
    "body",
]
# Stable dtypes for the list/bool columns so empty samples don't infer as List(Null).
TEST_OVERRIDES = {
    "pub_year": pl.Int64,
    "subway_mentioned": pl.Boolean,
    "subway_lines": pl.List(pl.String),
    "specific_location_mentioned": pl.Boolean,
    "specific_locations": pl.List(pl.String),
    "neighborhood_mentioned": pl.Boolean,
    "neighborhoods": pl.List(pl.String),
    "borough_mentioned": pl.Boolean,
    "boroughs": pl.List(pl.String),
}


def sample_per_year(df: pl.DataFrame, per_year: int, seed: int) -> pl.DataFrame:
    """Up to `per_year` random entries from each publication year.

    Seeded so the same calibration set comes back on every run -- you want to
    compare prompt tweaks against a fixed sample, not chase a moving target.
    """
    return (
        df.filter(pl.col("pub_year").is_not_null())
        .with_columns(
            pl.int_range(pl.len()).shuffle(seed=seed).over("pub_year").alias("_r")
        )
        .filter(pl.col("_r") < per_year)
        .drop("_r")
        .sort("pub_year", "pub_date")
    )


def write_test_results(records: list[dict]) -> pl.DataFrame:
    """Build the review frame and write it to the test parquet."""
    out = pl.DataFrame(records, schema_overrides=TEST_OVERRIDES).select(TEST_COLUMNS)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_parquet(TEST_PARQUET)
    return out


def cmd_test(per_year: int, workers: int, seed: int, dry_run: bool) -> None:
    df = load_entries(None)
    sample = sample_per_year(df, per_year, seed)
    n_years = sample["pub_year"].n_unique()
    print(
        f"test set: {sample.height} entries "
        f"(up to {per_year}/year across {n_years} years, seed={seed})"
    )

    if dry_run:
        first = sample.row(0, named=True)
        print("\n--- dry run: previewing row 0, called nothing ---")
        print(f"model: {MODEL or '(auto: currently-loaded model)'}  base_url: {BASE_URL}")
        print(f"\nsystem prompt ({len(SYSTEM_PROMPT)} chars):\n{SYSTEM_PROMPT}")
        print(f"\nuser content for row 0:\n{entry_text(first)}")
        return

    client = get_client()
    model = resolve_model(client)
    print(f"using model '{model}' at {BASE_URL} ({workers} worker(s))\n")

    def task(row: dict) -> dict:
        meta = {
            "entry_id": row["entry_id"],
            "uri": row["uri"],
            "author": row["author"],
            "title": row["title"],
        }
        rec = extract_one(client, model, meta, entry_text(row))
        # carry the context columns so the output is reviewable on its own
        rec["web_url"] = row["web_url"]
        rec["pub_year"] = row["pub_year"]
        rec["pub_date"] = row["pub_date"]
        rec["body"] = row["body"]
        return rec

    rows = list(sample.iter_rows(named=True))
    records: list[dict] = []
    n_ok = n_err = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(task, r) for r in rows]
        bar = tqdm(total=len(rows), desc="test", unit="entry")
        for fut in as_completed(futures):
            rec = fut.result()
            records.append(rec)
            if rec["error"] is None:
                n_ok += 1
            else:
                n_err += 1
            bar.update(1)
            bar.set_postfix(ok=n_ok, err=n_err)
        bar.close()

    out = write_test_results(records).sort("pub_year", "title")
    print(f"\ntest run done (ok={n_ok}, errored={n_err})")
    print(f"wrote -> {TEST_PARQUET}")
    with pl.Config(fmt_str_lengths=28, tbl_rows=12):
        print(
            out.select(
                "pub_year", "title", "subway_lines", "neighborhoods",
                "boroughs", "specific_locations",
            )
        )


# --- main ---------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--limit", type=int, help="only the first N entries (for calibration)"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="preview the prompt for row 0, call nothing",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=1,
        help="concurrent requests to LM Studio (default 1; raise if your server parallelizes)",
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=CHECKPOINT_EVERY,
        help=f"flush results to the parquet every N completions (default {CHECKPOINT_EVERY})",
    )
    ap.add_argument(
        "--test",
        action="store_true",
        help="quality run: sample N random entries per year -> diary_entities_test.parquet",
    )
    ap.add_argument(
        "--per-year",
        type=int,
        default=5,
        help="entries sampled per publication year in --test mode (default 5)",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=0,
        help="random seed for the --test sample, so the set is reproducible (default 0)",
    )
    args = ap.parse_args()
    if args.test:
        cmd_test(args.per_year, args.workers, args.seed, args.dry_run)
    else:
        cmd_run(args.limit, args.dry_run, args.workers, args.batch_size)


if __name__ == "__main__":
    main()
