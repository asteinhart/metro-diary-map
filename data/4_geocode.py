"""Geocode the one specific location each diary entry mentions -> output/diary_geocoded.parquet.

`entities.py` gives us output/diary_entities.parquet -- one entry per row, each with
at most one `specific_locations` value (a restaurant, venue, address, cross-street,
park, or station, written roughly as it appeared in the entry). This step turns that
free-text place into a latitude/longitude so the entries can be mapped.

    uv run data/geocode.py                                   # geocode output/diary_entities.parquet
    uv run data/geocode.py --input output/diary_entities1.parquet   # a bigger calibration set
    uv run data/geocode.py --limit 30                        # only the first N entries
    uv run data/geocode.py --dry-run                         # classify + route every query, call nothing

No single geocoder covers NYC diary prose, so we classify each string and route it:

  * intersection ("68th Street and York Avenue")  -> DCP Geoservice/Geosupport Function 2,
        the city's authoritative intersection geocoder. Neither GeoSearch nor Nominatim
        resolves "A and B" syntax, so this is the only good free option -- it needs a
        free API key (register at https://geoservice.planning.nyc.gov/, then put it in
        the repo-root .env as GEOSERVICE_KEY). Each side is canonicalized to the full
        street name Geosupport requires ("96th" -> "96 Street", "Park" -> "Park Avenue",
        "Bleecker and Perry Streets" -> "Bleecker Street" / "Perry Street"), and the
        lookup falls through every borough so a missing/wrong neighborhood still resolves.
  * relational ("40th Street between Park and Lexington", "Spring Street off West Broadway")
        -> reduced to an intersection (the street + its first cross street) and sent to
        Geosupport too.
  * named place / address ("Carnegie Hall", "495 East 55th Street") -> NYC GeoSearch v2
        first (authoritative for NYC addresses and its venue layer -- parks, institutions),
        then Nominatim/OSM as a fallback, which is far better at small businesses and
        restaurants that GeoSearch's address-directory data misses ("Wo Hop", "Macy's").
        When the place name itself won't resolve but carries its own location ("Petco at
        86th Street and Lexington Avenue"), the trailing "at/on/in <street>" clause is
        geocoded instead and the point flagged low_confidence (the block, not the door).
  * transit station ("Spring Street station") -> division/line qualifiers are stripped
        ("96th Street West Side IRT station" -> "96th Street"); if the core is itself an
        intersection it routes to Geosupport, otherwise the core is geocoded as a place
        trying the query variants Nominatim accepts ("<core> station", then bare "<core>").
  * bare street with no number ("Amsterdam Avenue") -> has no single point, so we still
        place it (an arbitrary point on the street) but tag it low_confidence so the map
        layer can decide whether to show it.

GeoSearch happily returns confident garbage for things it can't find ("Wo Hop" ->
"1514 HIP HOP BOULEVARD", confidence 1.0), so a result is only accepted if it isn't a
`fallback` match and its name is similar enough to the query; otherwise we fall through
to Nominatim, and if that also fails the entry is recorded as unresolved rather than
pinned somewhere wrong. Nominatim has no name guard of its own, so as a final check on
*any* accepted result we confirm the matched label still contains the query's
distinctive words; when it doesn't ("Lundy's restaurant" -> "MARINA RESTAURANT, East
Elmhurst, NY, USA") we keep the point but tag it low_confidence so the map can hide it.

The run is resumable and cheap on repeats: every distinct location string is geocoded
once and cached to output/geocode_cache.parquet (keyed by the normalized query), so the
~600 location values collapse to a few hundred API calls and a re-run hits only new
strings. Nominatim's usage policy caps us at 1 request/second and wants a real
User-Agent (override via NOMINATIM_USER_AGENT in .env).
"""

from __future__ import annotations

import argparse
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

import dotenv
import polars as pl
import requests
from tqdm import tqdm

from entities import NEIGHBORHOODS

DATA_DIR = Path(__file__).resolve().parent
OUT_DIR = DATA_DIR / "output"
ENTITIES_PARQUET = OUT_DIR / "diary_entities.parquet"
GEOCODE_PARQUET = OUT_DIR / "diary_geocoded.parquet"
CACHE_PARQUET = OUT_DIR / "geocode_cache.parquet"

ENV_PATH = DATA_DIR.parent / ".env"
dotenv.load_dotenv(ENV_PATH)

# --- endpoints ----------------------------------------------------------------

# NYC Planning Labs GeoSearch (Pelias over the city's address directory + a venue
# layer). v1 was permanently removed (HTTP 410) in favor of v2.
GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/search"
# DCP Geoservice -> Geosupport Function 2 (intersection). Needs a registered key.
GEOSERVICE_URL = (
    "https://geoservice.planning.nyc.gov/geoservice/geoservice.svc/Function_2"
)
GEOSERVICE_KEY = os.getenv("GEOSERVICE_KEY", "")
# OpenStreetMap Nominatim. Policy: <=1 req/s and a descriptive User-Agent with contact.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = os.getenv(
    "NOMINATIM_USER_AGENT", "metro-diary-map/0.1 (+asteinhart3@gmail.com)"
)

# --- tuning -------------------------------------------------------------------

# GeoSearch returns *something* for almost any input, so guard the result: reject a
# `fallback` match, and require the matched name to look like the query. The ratio is
# difflib.SequenceMatcher over the normalized query vs the matched name's core.
NAME_SIM_THRESHOLD = 0.55
# House-numbered addresses are trustworthy even when the street name is abbreviated,
# so accept those on a lower bar.
ADDRESS_SIM_THRESHOLD = 0.35

NOMINATIM_MIN_INTERVAL = 1.0  # seconds between Nominatim calls (their hard limit)
GEO_MIN_INTERVAL = 0.2  # be polite to the NYC services too
CHECKPOINT_EVERY = 25  # persist the cache this often so a crash loses little

# Bounding box bias for Nominatim so a bare "Fairway" or "Trinity Church" resolves in
# NYC, not Kansas. (left, top, right, bottom) = (W, N, E, S) lon/lat around the five boroughs.
NYC_VIEWBOX = "-74.30,40.92,-73.68,40.48"

BOROUGH_CODE = {
    "Manhattan": 1,
    "Bronx": 2,
    "Brooklyn": 3,
    "Queens": 4,
    "Staten Island": 5,
}
# neighborhood (lowercased) -> borough code, inverted from entities.py's reference list,
# so a row's extracted neighborhood can pick the borough for an intersection lookup.
NEIGHBORHOOD_BOROUGH: dict[str, int] = {
    hood.lower(): BOROUGH_CODE[boro]
    for boro, hoods in NEIGHBORHOODS.items()
    for hood in hoods
}


# --- query cleaning + classification ------------------------------------------

# transit words that turn a station name into a plain place name
_STATION_RE = re.compile(
    r"\b(?:[A-Z]\s+train|subway|train|path|ferry)?\s*(?:station|stop|terminal)\b",
    re.IGNORECASE,
)
# trailing things that aren't part of a place ("...crosstown bus")
_BUS_RE = re.compile(r"\b(?:crosstown\s+)?bus\b.*$", re.IGNORECASE)
# an intersection: "A and B", "A & B", "A at B", "corner of A and B", "intersection of A and B"
_INTERSECTION_RE = re.compile(
    r"^(?:the\s+)?(?:corner of\s+|intersection of\s+)?(.+?)\s+(?:and|&|at)\s+(.+)$",
    re.IGNORECASE,
)
# relational forms that reduce to (street, first cross street)
_BETWEEN_RE = re.compile(r"^(.+?)\s+between\s+(.+?)\s+and\s+(.+)$", re.IGNORECASE)
_OFF_NEAR_RE = re.compile(r"^(.+?)\s+(?:off|near)\s+(.+)$", re.IGNORECASE)
# a linear street/avenue token with no house number -> not a single point. Point-like
# suffixes (Square, Plaza, Circle, ...) are deliberately excluded -- "Herald Square" is a
# named place, not a street to flag low-confidence.
_STREET_WORD = r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Broadway|Parkway|Pkwy|Drive|Dr|Lane)"
_BARE_STREET_RE = re.compile(
    rf"^(?:the\s+)?(?:East|West|North|South|E|W|N|S)?\s*"
    rf"(?:\d{{1,3}}(?:st|nd|rd|th)?|First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth|[A-Z][a-z]+)"
    rf"\s+{_STREET_WORD}\.?$",
    re.IGNORECASE,
)
_HOUSE_NO_RE = re.compile(r"^\s*\d+[A-Za-z]?\s+\S")  # leading house number => address

# --- "is this side of an A-and-B split actually a street?" --------------------
# The intersection regex above fires on any "and"/"&"/"at", so a named place like
# "Rose Center for Earth and Space" or "Gotham Bar and Grill" splits into two halves
# and gets (wrongly) routed to the intersection geocoder. We only call it an
# intersection when *both* halves look like streets, otherwise it's a plain place.
#
# Street-type suffixes that can end a single cross-street name. Broader than
# _STREET_WORD: the point-like suffixes are allowed here because "Waverly Place" or
# "Astor Place" are real cross streets, even though they aren't bare linear streets.
_SIDE_SUFFIX = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Broadway|Parkway|Pkwy"
    r"|Drive|Dr|Lane|Ln|Place|Pl|Terrace|Ter|Court|Ct|Square|Plaza|Circle"
    r"|Highway|Hwy|Slip|Walk|Row|Way)"
)
# A side that is *just* a street: optional "the"/direction, one name token or a
# number/ordinal, a street suffix, and an optional trailing direction ("Park Avenue
# South"). Multi-word noise ("Starbucks on 110th Street") and trailing clauses
# ("13th Street branch of Citibank") deliberately fail to match.
_STREET_SIDE_RE = re.compile(
    rf"^(?:the\s+)?"
    rf"(?:(?:East|West|North|South|E|W|N|S)\.?\s+)?"
    rf"(?:\d{{1,3}}(?:st|nd|rd|th|d)?"
    rf"|First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth"
    rf"|[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)"
    rf"\s+{_SIDE_SUFFIX}(?:\s+(?:North|South|East|West))?\.?$",
    re.IGNORECASE,
)
# A bare numbered cross street with the suffix dropped ("59th", "23d", "West 74th").
_BARE_ORDINAL_RE = re.compile(
    r"^(?:the\s+)?(?:(?:East|West|North|South|E|W|N|S)\.?\s+)?\d{1,3}(?:st|nd|rd|th|d)?$",
    re.IGNORECASE,
)
# "Avenue A".."Avenue D" in the East Village (the suffix word leads the name here).
_AVENUE_LETTER_RE = re.compile(r"^Avenue\s+[A-D]$", re.IGNORECASE)
# A shared plural suffix covers both halves at once: "Mulberry and Bleecker Streets",
# "Flatbush and Nostrand Avenues" -> test each half as "<name> Street"/"<name> Avenue".
_PLURAL_SUFFIX_RE = re.compile(
    r"^(.*?)\s+(Streets|Sts|Avenues|Aves|Roads|Boulevards|Blvds|Places|Drives|Lanes)\.?$",
    re.IGNORECASE,
)
_PLURAL_SINGULAR = {
    "streets": "Street", "sts": "St", "avenues": "Avenue", "aves": "Ave",
    "roads": "Road", "boulevards": "Boulevard", "blvds": "Blvd",
    "places": "Place", "drives": "Drive", "lanes": "Lane",
}
# NYC thoroughfares that commonly appear with no suffix word ("Amsterdam and 104th
# Street", "Lexington and 59th", "Franklin and Vesey"). Normalized (lowercased) keys.
KNOWN_BARE_STREETS = {
    "broadway", "amsterdam", "columbus", "lexington", "madison", "park",
    "lenox", "riverside", "york", "west end", "east end", "central park west",
    "franklin", "vesey", "bowery", "canal", "avenue of the americas",
}
# Numbered avenues written as words ("53rd and Fifth" -> Fifth Avenue). Bare, each
# reads as a cross street in an intersection and expands to "<word> Avenue" below.
_WORD_AVENUES = {
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "eleventh", "twelfth",
}


def _looks_like_street(side: str) -> bool:
    """Does one side of an A-and-B split read as a single street, not a place name?"""
    s = side.strip(" .,")
    if not s:
        return False
    if _STREET_SIDE_RE.match(s) or _BARE_ORDINAL_RE.match(s) or _AVENUE_LETTER_RE.match(s):
        return True
    n = normalize(s)
    return n in KNOWN_BARE_STREETS or n in _WORD_AVENUES


def _is_intersection(s1: str, s2: str) -> bool:
    """Both halves read as streets -- directly, or sharing a trailing plural suffix."""
    if _looks_like_street(s1) and _looks_like_street(s2):
        return True
    pm = _PLURAL_SUFFIX_RE.match(s2.strip())  # "A and B Streets" -> "A Street", "B Street"
    if pm:
        suffix = _PLURAL_SINGULAR[pm.group(2).lower()]
        return _looks_like_street(f"{s1} {suffix}") and _looks_like_street(
            f"{pm.group(1)} {suffix}"
        )
    return False


def pick_raw_query(locations: list[str] | None) -> str | None:
    """The single location string to geocode: the entry's first specific_location."""
    if not locations:
        return None
    raw = (locations[0] or "").strip()
    return raw or None


def clean_query(raw: str) -> str:
    """Trim a raw location into the string we actually geocode.

    Splits a trailing comma clause off ("Fairway, Broadway" -> "Fairway") *unless*
    the comma introduces a "between" clause we want to keep, and drops bus-route noise.
    """
    q = raw.strip()
    q = _BUS_RE.sub("", q).strip(" ,")
    # keep a "between ..." clause (it carries the cross streets); otherwise a comma
    # usually separates the place from a borough/qualifier we don't need
    if "," in q and not re.search(r"\bbetween\b", q, re.IGNORECASE):
        q = q.split(",", 1)[0].strip()
    return q


def normalize(s: str) -> str:
    """Cache key: lowercase, punctuation-folded, whitespace-collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def classify(q: str) -> str:
    """One of: intersection / relational / station / bare_street / place."""
    if _STATION_RE.search(q):
        return "station"
    if _BETWEEN_RE.match(q) or _OFF_NEAR_RE.match(q):
        return "relational"
    m = _INTERSECTION_RE.match(q)
    # Only an intersection when both halves read as streets -- otherwise the "and"/"at"
    # belongs to a place name ("Rose Center for Earth and Space") and it routes as a place.
    if m and not _HOUSE_NO_RE.match(q) and _is_intersection(m.group(1), m.group(2)):
        return "intersection"
    if _BARE_STREET_RE.match(q):
        return "bare_street"
    return "place"


def parse_streets(q: str) -> tuple[str, str] | None:
    """Pull the two street names out of an intersection or relational string."""
    m = _BETWEEN_RE.match(
        q
    )  # "40th Street between Park and Lexington" -> (40th St, Park)
    if m:
        return _street(m.group(1)), _street(m.group(2))
    m = _OFF_NEAR_RE.match(
        q
    )  # "Spring Street off West Broadway" -> (Spring St, W Bway)
    if m:
        return _street(m.group(1)), _street(m.group(2))
    m = _INTERSECTION_RE.match(q)  # "68th Street and York Avenue"
    if m:
        return _street(m.group(1)), _street(m.group(2))
    return None


def _street(s: str) -> str:
    """Light cleanup of a single street name for Geosupport (it normalizes the rest)."""
    return re.sub(r"\s+", " ", s).strip(" .,")


# --- street canonicalization for Geosupport intersection lookups --------------
# Geosupport's Function 2 wants a full street name: bare ordinals ("96th") and bare
# thoroughfare names ("Lexington", "Park") return nothing, but "96 Street" and
# "Lexington Avenue" resolve. So before an intersection lookup we expand each side.

# bare thoroughfare name (normalized) -> the suffix Geosupport needs
_AVENUE_SUFFIX = {
    "lexington", "park", "madison", "amsterdam", "columbus", "york",
    "west end", "east end", "lenox", "convent", "mcdonald", "flatbush",
    "nostrand",
}
_DRIVE_SUFFIX = {"riverside"}
# a leading direction + number, suffix dropped ("96th", "West 74th", "23d")
_GEO_ORDINAL_RE = re.compile(
    r"^(?:(East|West|North|South|E|W|N|S)\.?\s+)?(\d{1,3})(?:st|nd|rd|th|d)?$",
    re.IGNORECASE,
)


def _apply_suffix(name: str, suffix: str) -> str:
    """Attach a shared plural suffix to one side: '18th' + 'Avenue' -> '18 Avenue'."""
    name = name.strip(" .,")
    m = _GEO_ORDINAL_RE.match(name)
    if m:
        direction = f"{m.group(1)} " if m.group(1) else ""
        return f"{direction}{m.group(2)} {suffix}"
    return f"{name} {suffix}"


def _canonical_street(s: str) -> str:
    """Expand a parsed street into the full form Geosupport's intersection lookup needs."""
    s = re.sub(r"^the\s+", "", s.strip(" .,"), flags=re.IGNORECASE)
    n = normalize(s)
    if n in _WORD_AVENUES:
        return f"{s} Avenue"  # "Fifth" -> "Fifth Avenue"
    m = _GEO_ORDINAL_RE.match(s)
    if m:
        direction = f"{m.group(1)} " if m.group(1) else ""
        return f"{direction}{m.group(2)} Street"  # "96th" -> "96 Street"
    if n in _AVENUE_SUFFIX:
        return f"{s} Avenue"
    if n in _DRIVE_SUFFIX:
        return f"{s} Drive"
    if n.startswith("avenue of the americas"):
        return "Avenue of the Americas"
    return s


def geo_intersection_streets(q: str) -> tuple[str, str] | None:
    """Parsed + canonicalized street pair ready for a Geosupport intersection lookup.

    Handles a shared plural suffix ("Bleecker and Perry Streets" -> "Bleecker Street",
    "Perry Street") and otherwise canonicalizes each side ("96th and Park" ->
    "96 Street", "Park Avenue").
    """
    streets = parse_streets(q)
    if not streets:
        return None
    s1, s2 = streets
    pm = _PLURAL_SUFFIX_RE.match(s2.strip())  # "Bleecker and Perry Streets"
    if pm:
        suffix = _PLURAL_SINGULAR[pm.group(2).lower()]
        return _apply_suffix(s1, suffix), _apply_suffix(pm.group(1), suffix)
    return _canonical_street(s1), _canonical_street(s2)


def strip_station(q: str) -> str:
    """A station name as a plain place: '34th Street R train station' -> '34th Street'."""
    return _STATION_RE.sub("", q).strip(" -,") or q


# Transit division / line qualifiers ("BMT", "IRT", "West Side", "Metro-North",
# "No. 6", "Q train", "of the Lexington Avenue subway") that aren't part of the
# station's place name. Stripping them leaves the core street or intersection, which
# the geocoders can find ("96th Street West Side IRT station" -> "96th Street").
_STATION_QUALIFIER_RE = re.compile(
    r"\b(?:BMT|IND|IRT|PATH|Metro[- ]North|West Side|East Side|"
    r"No\.?\s*\d+|[A-Z]\s+train|of the .*?subway)\b",
    re.IGNORECASE,
)


def station_core(q: str) -> str:
    """A station string reduced to its core place/intersection name."""
    s = strip_station(q)
    s = _STATION_QUALIFIER_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" -,.")
    return s or q


def infer_borough(neighborhoods: list[str] | None) -> int | None:
    """Borough code for an intersection lookup, from the entry's extracted neighborhood."""
    for hood in neighborhoods or []:
        code = NEIGHBORHOOD_BOROUGH.get((hood or "").lower())
        if code:
            return code
    return None


# --- geocoders ----------------------------------------------------------------

_last_call: dict[str, float] = {}


def _throttle(source: str, min_interval: float) -> None:
    last = _last_call.get(source, 0.0)
    wait = min_interval - (time.monotonic() - last)
    if wait > 0:
        time.sleep(wait)
    _last_call[source] = time.monotonic()


def _name_core(s: str) -> str:
    """A label's place name for similarity comparison (drop the ', Borough, NY, USA' tail)."""
    return normalize(s.split(",", 1)[0])


# Generic words that don't pin a place: a query and a matched label both containing
# "restaurant" is no evidence they're the same restaurant. We strip these (plus
# borough/filler/street-type words) from the query before checking how much of it the
# matched label actually accounts for.
_GENERIC_TOKENS = {
    # filler + geography
    "the", "a", "an", "of", "and", "at", "in", "on", "off", "near",
    "new", "york", "ny", "nyc", "usa", "us",
    "manhattan", "brooklyn", "queens", "bronx", "staten", "island",
    # street-type suffixes -- the distinctive part is the street's *name*, not "street"
    "street", "st", "avenue", "ave", "road", "rd", "boulevard", "blvd",
    "place", "pl", "drive", "dr", "lane", "ln", "parkway", "pkwy",
    "court", "ct", "terrace", "ter", "square", "plaza", "circle",
    # generic venue types
    "restaurant", "restaurants", "cafe", "diner", "bar", "grill", "deli",
    "tavern", "lounge", "club", "pub", "bakery", "shop", "store", "market",
    "park", "building", "hotel", "theater", "theatre", "house", "company", "co",
}


def _distinct_tokens(s: str) -> set[str]:
    """The query words that actually identify a place (drop generic/filler tokens)."""
    return {t for t in normalize(s).split() if len(t) > 1 and t not in _GENERIC_TOKENS}


def label_covers_query(query: str, label: str | None) -> bool:
    """Does the matched label actually name the place the query asked for?

    Geocoders -- Nominatim especially, which applies no name guard of its own --
    will confidently return an unrelated place: "Lundy's restaurant" ->
    "MARINA RESTAURANT, East Elmhurst, NY, USA". We check what share of the query's
    *distinctive* words (generic venue/street/borough words removed) survive in the
    label; when most are missing the caller keeps the point but flags it
    low_confidence. Returns True when there's nothing distinctive left to verify.
    """
    if not label:
        return False
    q_tokens = _distinct_tokens(query)
    if not q_tokens:
        return True
    label_tokens = set(normalize(label).split())
    covered = len(q_tokens & label_tokens) / len(q_tokens)
    return covered >= 0.5


def geosearch(query: str) -> dict | None:
    """NYC GeoSearch v2, guarded against confident-but-wrong fallback matches."""
    _throttle("geosearch", GEO_MIN_INTERVAL)
    try:
        r = requests.get(GEOSEARCH_URL, params={"text": query, "size": 1}, timeout=20)
        r.raise_for_status()
        feats = r.json().get("features", [])
    except Exception:  # noqa: BLE001 -- treat any failure as "no match", try the fallback
        return None
    if not feats:
        return None
    f = feats[0]
    p = f["properties"]
    lon, lat = f["geometry"]["coordinates"]
    name = p.get("name") or p.get("label") or ""
    is_address = p.get("layer") == "address" or _HOUSE_NO_RE.match(query) is not None
    threshold = ADDRESS_SIM_THRESHOLD if is_address else NAME_SIM_THRESHOLD
    sim = SequenceMatcher(None, normalize(query), _name_core(name)).ratio()
    if p.get("match_type") == "fallback" or sim < threshold:
        return None  # let the caller fall through to Nominatim
    return {
        "lat": lat,
        "lon": lon,
        "source": "geosearch",
        "label": p.get("label"),
        "confidence": p.get("confidence"),
    }


def nominatim(query: str) -> dict | None:
    """OpenStreetMap Nominatim, biased to the five boroughs. Good for businesses/POIs."""
    _throttle("nominatim", NOMINATIM_MIN_INTERVAL)
    try:
        r = requests.get(
            NOMINATIM_URL,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 1,
                "viewbox": NYC_VIEWBOX,
                "bounded": 1,
            },
            headers={"User-Agent": NOMINATIM_UA},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    d = data[0]
    return {
        "lat": float(d["lat"]),
        "lon": float(d["lon"]),
        "source": "nominatim",
        "label": d.get("display_name"),
        "confidence": float(d["importance"]) if d.get("importance") else None,
    }


def _find_latlon(obj) -> tuple[float, float] | None:
    """Recursively pull a (lat, lon) pair out of a Geoservice JSON response.

    The Function 2 payload nests the geographic coordinates inside Geosupport work
    areas whose exact key names vary by display mode, so we walk the structure for the
    first plausible latitude/longitude pair rather than hard-coding a path.
    """
    if isinstance(obj, dict):
        lat = lon = None
        for k, v in obj.items():
            kl = k.lower()
            if isinstance(v, (str, int, float)):
                if "latitude" in kl and lat is None:
                    lat = v
                elif "longitude" in kl and lon is None:
                    lon = v
        if lat is not None and lon is not None:
            try:
                la, lo = float(lat), float(lon)
                if la and lo:
                    return la, lo
            except (TypeError, ValueError):
                pass
        for v in obj.values():
            found = _find_latlon(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_latlon(v)
            if found:
                return found
    return None


def geoservice_intersection(s1: str, s2: str, borough: int) -> dict | None:
    """DCP Geoservice/Geosupport Function 2 -- authoritative NYC intersection geocoder."""
    if not GEOSERVICE_KEY:
        return None
    _throttle("geoservice", GEO_MIN_INTERVAL)
    try:
        r = requests.get(
            GEOSERVICE_URL,
            params={
                "Borough1": borough,
                "Street1": s1,
                "Borough2": borough,
                "Street2": s2,
                "key": GEOSERVICE_KEY,
            },
            timeout=20,
        )
        if r.status_code == 401:
            raise SystemExit(
                "Geoservice rejected GEOSERVICE_KEY (401). Register a key at "
                "https://geoservice.planning.nyc.gov/ and set it in the repo-root .env."
            )
        r.raise_for_status()
        latlon = _find_latlon(r.json())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        return None
    if not latlon:
        return None
    lat, lon = latlon
    return {
        "lat": lat,
        "lon": lon,
        "source": "geoservice",
        "label": f"{s1} & {s2}",
        "confidence": None,
    }


def geoservice_try(s1: str, s2: str, neighborhoods: list[str] | None) -> dict | None:
    """Geosupport intersection, trying the entry's inferred borough first then the rest.

    Function 2 needs a borough and only returns a hit when the two streets actually
    cross in it, so an entry whose neighborhood is missing or points to the wrong
    borough still resolves by falling through the remaining boroughs.
    """
    order: list[int] = []
    inferred = infer_borough(neighborhoods)
    if inferred:
        order.append(inferred)
    for b in (1, 3, 4, 2, 5):  # Manhattan, Brooklyn, Queens, Bronx, Staten Island
        if b not in order:
            order.append(b)
    for b in order:
        r = geoservice_intersection(s1, s2, b)
        if r:
            return r
    return None


# --- routing ------------------------------------------------------------------

UNRESOLVED = {
    "lat": None,
    "lon": None,
    "source": None,
    "label": None,
    "confidence": None,
}

# Many places carry their own location ("Barnes & Noble at 66th Street and Broadway",
# "Apple store on Fifth Avenue"). When the place name itself won't geocode, the
# trailing "at/on/in <street or intersection>" still pins the block.
_LOCATION_TAIL_RE = re.compile(r"\b(?:at|on|in)\s+(.+)$", re.IGNORECASE)
_GEOCODABLE_TAIL_RE = re.compile(
    r"(?:street|avenue|broadway|\bst\b|\bave\b|plaza|square|\band\b|\d)", re.IGNORECASE
)


def location_tail(q: str) -> str | None:
    """A trailing 'at/on/in <street or intersection>' phrase worth geocoding on its own."""
    m = _LOCATION_TAIL_RE.search(q)
    if not m:
        return None
    tail = m.group(1).strip(" .,")
    return tail if _GEOCODABLE_TAIL_RE.search(tail) else None


def geocode_query(raw: str, neighborhoods: list[str] | None) -> dict:
    """Classify one cleaned location string, route it, and return a result record."""
    q = clean_query(raw)
    category = classify(q)
    low_confidence = category == "bare_street"
    result: dict | None = None
    match_query = q  # the place-name we expect the matched label to name

    if category in ("intersection", "relational"):
        streets = geo_intersection_streets(q)
        if streets:
            result = geoservice_try(streets[0], streets[1], neighborhoods)
            # relational strings still name a real street -> a place lookup is a sane backup
            if result is None and category == "relational":
                match_query = streets[0]
                result = geosearch(streets[0]) or nominatim(
                    f"{streets[0]}, New York, NY"
                )
                low_confidence = True
    elif category == "station":
        core = match_query = station_core(q)
        # some "stations" are really an intersection ("96th Street and Broadway station")
        if classify(core) == "intersection":
            streets = geo_intersection_streets(core)
            if streets:
                result = geoservice_try(streets[0], streets[1], neighborhoods)
        # otherwise the core is a street/place name -- try the variants Nominatim accepts
        # ("subway station" suffixed breaks it; "<core> station" or "<core>" works)
        for attempt in (
            lambda: geosearch(core),
            lambda: nominatim(f"{q}, New York, NY"),
            lambda: nominatim(f"{core} station, New York, NY"),
            lambda: nominatim(f"{core}, New York, NY"),
        ):
            if result:
                break
            result = attempt()
    else:  # place / bare_street -- GeoSearch first, then Nominatim for POIs it misses
        result = geosearch(q) or nominatim(f"{q}, New York, NY")
        # nothing matched the place name itself -> fall back to an embedded location
        # ("Petco at 86th Street and Lexington Avenue"), placed at the block, low-confidence
        if result is None:
            tail = location_tail(q)
            if tail:
                sub = geocode_query(tail, neighborhoods)
                if sub["status"] == "ok":
                    match_query = tail
                    low_confidence = True
                    result = {k: sub[k] for k in (
                        "lat", "lon", "source", "label", "confidence")}

    # A geocoder can confidently pin an unrelated place (Nominatim has no name guard):
    # if the matched label doesn't account for the query's distinctive words, keep the
    # point but flag it low_confidence. Skip intersections -- their label is built from
    # the query streets, so the check is meaningless there.
    if (
        result
        and result["source"] != "geoservice"
        and not label_covers_query(match_query, result["label"])
    ):
        low_confidence = True

    rec = {
        "query_raw": raw,
        "query": q,
        "category": category,
        **(result or UNRESOLVED),
        "low_confidence": low_confidence,
        "status": "ok" if result else "unresolved",
    }
    return rec


# --- load / cache / write -----------------------------------------------------

CACHE_SCHEMA = {
    "query_norm": pl.String,
    "query_raw": pl.String,
    "query": pl.String,
    "category": pl.String,
    "lat": pl.Float64,
    "lon": pl.Float64,
    "source": pl.String,
    "label": pl.String,
    "confidence": pl.Float64,
    "low_confidence": pl.Boolean,
    "status": pl.String,
}


def load_entries(input_path: Path, limit: int | None) -> pl.DataFrame:
    if not input_path.exists():
        raise SystemExit(
            f"missing {input_path}; run `entities.py` first (or pass --input)"
        )
    df = pl.read_parquet(input_path)
    df = df.filter(pl.col("specific_location_mentioned") & (pl.col("error").is_null()))
    if limit:
        df = df.head(limit)
    return df


def load_cache() -> dict[str, dict]:
    if not CACHE_PARQUET.exists():
        return {}
    df = pl.read_parquet(CACHE_PARQUET)
    return {r["query_norm"]: r for r in df.iter_rows(named=True)}


def write_cache(cache: dict[str, dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(list(cache.values()), schema=CACHE_SCHEMA).sort(
        "query_norm"
    ).write_parquet(CACHE_PARQUET)


def write_results(df: pl.DataFrame) -> pl.DataFrame:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.write_parquet(GEOCODE_PARQUET)
    return df


# --- run ----------------------------------------------------------------------


def cmd_run(input_path: Path, limit: int | None, dry_run: bool) -> None:
    df = load_entries(input_path, limit)

    # one (raw_query, neighborhoods) per entry; the cache key is the normalized query
    entries = []
    for r in df.iter_rows(named=True):
        raw = pick_raw_query(r.get("specific_locations"))
        if raw:
            entries.append(
                {
                    "entry_id": r["entry_id"],
                    "uri": r["uri"],
                    "author": r["author"],
                    "title": r["title"],
                    "raw": raw,
                    "neighborhoods": r.get("neighborhoods"),
                }
            )

    uniques: dict[
        str, dict
    ] = {}  # query_norm -> a representative entry to geocode from
    for e in entries:
        uniques.setdefault(normalize(clean_query(e["raw"])), e)

    print(
        f"{df.height} entries with a location; {len(entries)} have a value; "
        f"{len(uniques)} distinct queries to geocode."
    )

    if dry_run:
        print("\n--- dry run: classification + routing, no calls ---")
        from collections import Counter

        cats = Counter(classify(clean_query(e["raw"])) for e in uniques.values())
        for cat, n in cats.most_common():
            print(f"  {cat:<12} {n}")
        print("\nsamples:")
        for e in list(uniques.values())[:25]:
            q = clean_query(e["raw"])
            print(f"  [{classify(q):<12}] {e['raw']!r} -> {q!r}")
        if not GEOSERVICE_KEY:
            print(
                "\nNOTE: GEOSERVICE_KEY is unset -- intersections will be unresolved. "
                "Register at https://geoservice.planning.nyc.gov/ and add it to .env."
            )
        return

    if not GEOSERVICE_KEY:
        print(
            "WARNING: GEOSERVICE_KEY unset -- intersections won't resolve. "
            "Register at https://geoservice.planning.nyc.gov/ and add it to .env.\n"
        )

    cache = load_cache()
    pending = [(qn, e) for qn, e in uniques.items() if qn not in cache]
    print(f"{len(uniques) - len(pending)} cached; {len(pending)} to geocode.\n")

    n_ok = n_unresolved = 0
    bar = tqdm(total=len(pending), desc="geocoding", unit="query")
    for i, (qn, e) in enumerate(pending, 1):
        rec = geocode_query(e["raw"], e["neighborhoods"])
        cache[qn] = {
            "query_norm": qn,
            **{k: rec[k] for k in CACHE_SCHEMA if k != "query_norm"},
        }
        if rec["status"] == "ok":
            n_ok += 1
        else:
            n_unresolved += 1
        if i % CHECKPOINT_EVERY == 0:
            write_cache(cache)
        bar.update(1)
        bar.set_postfix(ok=n_ok, unresolved=n_unresolved)
    bar.close()
    write_cache(cache)

    # join the cache back onto every entry -> one geocoded row per diary entry
    cache_df = pl.DataFrame(list(cache.values()), schema=CACHE_SCHEMA)
    entries_df = pl.DataFrame(
        [
            {
                "entry_id": e["entry_id"],
                "uri": e["uri"],
                "author": e["author"],
                "title": e["title"],
                "query_norm": normalize(clean_query(e["raw"])),
            }
            for e in entries
        ]
    )
    out = (
        entries_df.join(cache_df, on="query_norm", how="left")
        .drop("query_norm")
        .sort("entry_id")
    )
    write_results(out)

    resolved = int((out["status"] == "ok").sum())
    print(
        f"\ngeocoded {out.height} entries: {resolved} resolved, "
        f"{out.height - resolved} unresolved  (this run: ok={n_ok}, unresolved={n_unresolved})"
    )
    print(f"wrote -> {GEOCODE_PARQUET}  (cache -> {CACHE_PARQUET})")
    with pl.Config(fmt_str_lengths=34, tbl_rows=12):
        print(
            out.select("query_raw", "category", "source", "lat", "lon", "status").head(
                12
            )
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--input",
        type=Path,
        default=ENTITIES_PARQUET,
        help="entities parquet to geocode (default output/diary_entities.parquet)",
    )
    ap.add_argument(
        "--limit", type=int, help="only the first N entries (for calibration)"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="classify + route every query and print the buckets, call nothing",
    )
    args = ap.parse_args()
    input_path = args.input if args.input.is_absolute() else DATA_DIR / args.input
    cmd_run(input_path, args.limit, args.dry_run)


if __name__ == "__main__":
    main()
