"""Turn the diary parquets + NYC boundaries into GeoJSON for the MapLibre map.

Upstream we have, all joinable on `entry_id`:

    output/diary_entries.parquet    one row per entry: title, author, body, web_url, pub_year, ...
    output/diary_entities.parquet   per entry, the lists the model pulled out of the prose:
                                     subway_lines, specific_locations, neighborhoods, boroughs
    output/diary_geocoded.parquet   one geocoded *specific location* per entry -> lat/lon

and these reference geometries (use these, don't invent boundaries):

    input/borough_boundaries.geojson   5 borough MultiPolygons (boroname)
    input/subway.geojson               29 line MultiLineStrings, keyed by `service` (1, A, F, ...)
    input/area_places.geojson          named polygons for big places that geocode to one point
                                       (Central Park, ...); entries naming one are scattered
                                       across the whole polygon. Add features to extend it.
    output/neighborhoods.geojson       NYC neighborhood polygons with admin levels (built by
                                       6_neighborhoods.py). The specific neighborhood polygons
                                       (is_admin_0_union=False) plus one dissolved broad-area
                                       polygon per admin_0 (is_admin_0_union=True). We place a
                                       neighborhood-only entry inside its polygon here.

We emit three "views" of the same entries, each a GeoJSON the map loads as its own
source/layer, plus a body lookup and two slimmed basemap layers:

    locations.geojson   View 1 -- the entries whose specific location geocoded cleanly,
                        dropped at their exact lat/lon.
    subway.geojson      View 2 -- the entries that mention the subway, dropped at a random
                        point ON the line they named (matched to `service`), kept inside the
                        entry's borough polygon when we know it; entries that name no
                        matchable line ride a random point on the whole network instead.
    areas.geojson       View 3 -- every entry placed at the most specific spot we can, in
                        order: its exact geocoded point; else a random point inside the
                        neighborhood it names (matched in neighborhoods.geojson -- the specific
                        polygon when the name resolves there, else the broad admin_0 area);
                        else a random point inside its borough polygon (the borough it names,
                        or the one we learned the neighborhood sits in from entries that mention
                        both). Entries naming only a neighborhood we can't place anywhere are
                        skipped (count reported); `placement` records which tier landed each point.
    entries.json        {entry_id: body} -- the body text lives here, not in every feature,
                        so the geojson stays small; the map fetches it on click. Every body is
                        normalized to open "Dear Diary:" (added when the entry lacks it).
    boroughs.geojson    basemap: the borough polygons, trimmed + rounded.
    subway_lines.geojson basemap: the subway lines, one feature per service, trimmed + rounded.

Placement that isn't an exact geocode is random, so we seed the RNG and walk entries in
a fixed order -- re-running gives byte-identical output.

    uv run data/process.py            # write everything to diary-map/static/data/
    uv run data/process.py --limit 50 # only the first N entries (quick check)
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import time
from difflib import get_close_matches
from pathlib import Path

import geopandas as gpd
import polars as pl
from shapely.geometry import MultiLineString, Point, mapping
from shapely.ops import unary_union
from shapely.prepared import prep

DATA_DIR = Path(__file__).resolve().parent
OUT_DIR = DATA_DIR / "output"
INPUT_DIR = DATA_DIR / "input"

ENTRIES_PARQUET = OUT_DIR / "diary_entries.parquet"
ENTITIES_PARQUET = OUT_DIR / "diary_entities.parquet"
GEOCODE_PARQUET = OUT_DIR / "diary_geocoded.parquet"
# NYC neighborhood polygons + admin levels (built by 6_neighborhoods.py) -- used to place a
# neighborhood-only entry inside its polygon instead of scattering it across a whole borough.
NEIGHBORHOODS_GEOJSON = OUT_DIR / "neighborhoods.geojson"

BOROUGHS_GEOJSON = INPUT_DIR / "borough_boundaries.geojson"
SUBWAY_GEOJSON = INPUT_DIR / "subway.geojson"
# named polygons for big places that geocode to a single centroid (Central Park, ...) --
# entries naming one are scattered across the whole polygon instead of stacking. Add more
# features (each with a "name" matching the geocoded query) to extend this.
AREA_PLACES_GEOJSON = INPUT_DIR / "area_places.geojson"

# the SvelteKit app serves diary-map/static/ at /, so /data/<file> resolves here
MAP_DIR = DATA_DIR.parent / "diary-map" / "static" / "data"

SEED = 42
# how far to round output coordinates: ~0.1m for the entry points, ~1m for the basemap
POINT_PRECISION = 6
BASEMAP_PRECISION = 5
# coincident points (same station, venue, "Central Park") are fanned out within roughly
# this radius so they don't stack into one dot -- ~1.5 short NYC blocks
JITTER_RADIUS_M = 120.0

# entries write boroughs roughly; fold the variants we can onto the 5 official names
BOROUGH_ALIASES = {
    "the bronx": "Bronx",
    "bronx": "Bronx",
    "kings": "Brooklyn",
    "brooklyn": "Brooklyn",
    "manhattan": "Manhattan",
    "queens": "Queens",
    "staten island": "Staten Island",
}


def _canon_borough(raw) -> str | None:
    """Fold a free-text borough mention onto one of the 5 official names, or None."""
    return BOROUGH_ALIASES.get(str(raw).strip().lower())


def _norm_name(raw) -> str:
    """Match key for a place name: lowercase, punctuation folded to spaces, collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(raw or "").lower())).strip()


# --------------------------------------------------------------------------------------
# geometry (shapely)
# --------------------------------------------------------------------------------------


def _lines_only(geom):
    """The line parts of a geometry (intersections can return points/collections)."""
    if geom.is_empty:
        return None
    kind = geom.geom_type
    if kind in ("LineString", "MultiLineString"):
        return geom if geom.length > 0 else None
    if kind == "GeometryCollection":
        parts = []
        for g in geom.geoms:
            if g.geom_type == "LineString" and g.length > 0:
                parts.append(g)
            elif g.geom_type == "MultiLineString":
                parts.extend(p for p in g.geoms if p.length > 0)
        return MultiLineString(parts) if parts else None
    return None


class Region:
    """A named polygon (a borough, a park), prepared for fast point-in-polygon darts."""

    def __init__(self, name: str, geom):
        self.name = name
        self.geom = geom
        self.prepared = prep(geom)
        self.minx, self.miny, self.maxx, self.maxy = geom.bounds

    def random_point(self, rng: random.Random) -> tuple:
        """A uniform random point inside the region (bbox rejection sampling)."""
        for _ in range(10000):
            p = Point(
                rng.uniform(self.minx, self.maxx), rng.uniform(self.miny, self.maxy)
            )
            if self.prepared.contains(p):
                return p.x, p.y
        p = self.geom.representative_point()  # degenerate fallback (shouldn't happen)
        return p.x, p.y


class LineGeom:
    """A subway line's geometry, set up to sample a random point along its length."""

    def __init__(self, geom):
        self.geom = geom  # shapely (Multi)LineString

    def random_point_in_borough(
        self, borough: Region | None, rng: random.Random
    ) -> tuple:
        """Random point on the line, clipped to `borough` when the line passes through it."""
        geom = self.geom
        if borough is not None:
            clipped = _lines_only(geom.intersection(borough.geom))
            if clipped is not None:
                geom = (
                    clipped  # the named line never enters the borough -> keep full line
                )
        p = geom.interpolate(rng.uniform(0, geom.length))
        return p.x, p.y


def _round_coords(coords, ndigits: int):
    """Recursively round a GeoJSON coordinate array to `ndigits` decimals."""
    if coords and isinstance(coords[0], (int, float)):
        return [round(coords[0], ndigits), round(coords[1], ndigits)]
    return [_round_coords(c, ndigits) for c in coords]


def spread_coincident(features: list, radius_m: float = JITTER_RADIUS_M) -> list:
    """Fan out point features that share an exact coordinate so they don't stack.

    Many entries geocode to the same spot (a station, a venue, "Central Park"). We move
    only the *displayed* geometry -- each entry's true location stays in spec_lat/spec_lon
    -- scattering coincident points to random offsets within `radius_m` (no regular ring or
    sunflower pattern). The jitter is seeded per spot from the shared coordinate, so it's
    stable across runs and identical across views (both derive from the same points).
    """
    groups: dict[tuple, list] = {}
    for f in features:
        groups.setdefault(tuple(f["geometry"]["coordinates"]), []).append(f)
    for (lon, lat), grp in groups.items():
        if len(grp) == 1:
            continue
        grp.sort(key=lambda f: f["properties"]["entry_id"])
        # per-spot RNG keyed on the shared coordinate -> stable across runs and views
        jrng = random.Random(
            f"{SEED}:{round(lon, POINT_PRECISION)}:{round(lat, POINT_PRECISION)}"
        )
        dlat = radius_m / 111_000.0  # meters -> degrees latitude
        dlon = radius_m / (
            111_000.0 * math.cos(math.radians(lat))
        )  # ... longitude here
        for f in grp:
            r = math.sqrt(
                jrng.random()
            )  # sqrt keeps the disk evenly filled, not center-heavy
            theta = jrng.uniform(0, 2 * math.pi)
            f["geometry"]["coordinates"] = [
                round(lon + r * dlon * math.cos(theta), POINT_PRECISION),
                round(lat + r * dlat * math.sin(theta), POINT_PRECISION),
            ]
    return features


# --------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------


def load_boroughs() -> dict:
    """boroname -> Region."""
    gdf = gpd.read_file(BOROUGHS_GEOJSON)
    return {
        row["boroname"]: Region(row["boroname"], row.geometry)
        for _, row in gdf.iterrows()
    }


def load_subway_lines() -> tuple[dict, LineGeom]:
    """Return (service -> LineGeom, whole-network LineGeom).

    Duplicate features for the same service are merged into one geometry.
    """
    gdf = gpd.read_file(SUBWAY_GEOJSON)
    gdf["service"] = gdf["service"].str.upper()
    merged = gdf.dissolve(by="service")
    lines = {svc: LineGeom(geom) for svc, geom in merged.geometry.items()}
    network = LineGeom(unary_union(gdf.geometry.tolist()))
    return lines, network


def load_area_places() -> dict:
    """normalized place name -> Region, for big places to scatter across (e.g. Central Park)."""
    if not AREA_PLACES_GEOJSON.exists():
        return {}
    gdf = gpd.read_file(AREA_PLACES_GEOJSON)
    return {
        row["name"].strip().lower(): Region(row["name"], row.geometry)
        for _, row in gdf.iterrows()
    }


def load_neighborhoods() -> tuple[dict, dict]:
    """Two normalized-name -> Region lookups from neighborhoods.geojson.

    Returns (specific, broad):
      specific  the individual neighborhood polygons (is_admin_0_union=False), keyed by name
      broad     the one dissolved polygon per admin_0 (is_admin_0_union=True), keyed by admin_0

    A neighborhood-only entry is placed inside its `specific` polygon when its name resolves
    there, else inside the `broad` area for an admin_0. Duplicate names (e.g. Murray Hill, in
    both Midtown and Flushing) keep the first feature -- the file carries no borough column to
    disambiguate, and in this corpus the first listed is the likelier-intended one.
    """
    if not NEIGHBORHOODS_GEOJSON.exists():
        return {}, {}
    gdf = gpd.read_file(NEIGHBORHOODS_GEOJSON)
    specific: dict = {}
    broad: dict = {}
    for _, row in gdf.iterrows():
        target = broad if row["is_admin_0_union"] else specific
        target.setdefault(_norm_name(row["name"]), Region(row["name"], row.geometry))
    return specific, broad


def load_entries() -> list[dict]:
    """Join entries + entities + geocoded into one row per entry, sorted by entry_id."""
    entries = pl.read_parquet(ENTRIES_PARQUET).select(
        "entry_id", "title", "author", "pub_year", "web_url", "body"
    )
    entities = pl.read_parquet(ENTITIES_PARQUET).select(
        "entry_id",
        "subway_mentioned",
        "subway_lines",
        "specific_locations",
        "neighborhoods",
        "boroughs",
    )
    geo = (
        pl.read_parquet(GEOCODE_PARQUET)
        .filter(pl.col("status") == "ok")
        .select(
            "entry_id",
            "lat",
            "lon",
            "query_raw",
            "confidence",
            "low_confidence",
            "source",
            "category",
        )
    )
    joined = (
        entries.join(entities, on="entry_id", how="left")
        .join(geo, on="entry_id", how="left")
        .sort("entry_id")
    )
    return joined.to_dicts()


# --------------------------------------------------------------------------------------
# feature building
# --------------------------------------------------------------------------------------


def _join_list(value) -> str | None:
    if not value:
        return None
    return "; ".join(str(v) for v in value)


def _first(value) -> str | None:
    """First item of a list-ish value as a string, or None."""
    return str(value[0]) if value else None


def _boro_phrase(name: str) -> str:
    """A borough name as it reads after 'in' -- only the Bronx takes an article."""
    return "the Bronx" if name == "Bronx" else name


# every entry should read as a diary entry, opening "Dear Diary:". Most NYT entries already do;
# some open "Dear Diary," and many carry no salutation at all -- match any leading variant
# (case/punctuation/whitespace) so we can strip it and re-add the canonical one exactly once.
_DEAR_DIARY_RE = re.compile(r"^\s*dear\s+diary\s*[:,]?\s*", re.IGNORECASE)


def _dear_diary_body(body: str) -> str:
    """Normalize a body to open with 'Dear Diary: ', adding the salutation when it's missing."""
    return f"Dear Diary:\n\n{_DEAR_DIARY_RE.sub('', body, count=1).lstrip()}"


def build_neighborhood_borough_map(rows: list[dict]) -> dict:
    """neighborhood (normalized) -> borough, learned from entries that name both.

    The whole-borough fallback for a neighborhood we can't place precisely (it isn't in
    neighborhoods.geojson): whenever an entry mentions a neighborhood *and* a borough we
    learn which borough that neighborhood sits in, then reuse it for neighborhood-only entries.
    """
    pairs: dict[str, dict] = {}
    for row in rows:
        boros = [b for b in (_canon_borough(b) for b in row.get("boroughs") or []) if b]
        if not boros:
            continue
        for nbhd in row.get("neighborhoods") or []:
            counts = pairs.setdefault(_norm_name(nbhd), {})
            for b in boros:
                counts[b] = counts.get(b, 0) + 1
    # pick the most-co-mentioned borough for each neighborhood (ties broken by name)
    return {nbhd: max(sorted(counts), key=counts.get) for nbhd, counts in pairs.items()}


def _resolve_borough(row: dict, boroughs: dict, nbhd_map: dict) -> Region | None:
    """A borough for the entry -- the whole-borough fallback when nothing finer is available.

    Prefer a borough the entry names outright; otherwise infer one from a neighborhood it
    names (via `nbhd_map`, for neighborhoods missing from neighborhoods.geojson). None when
    the entry pins no borough at all.
    """
    for raw in row.get("boroughs") or []:
        canon = _canon_borough(raw)
        if canon and canon in boroughs:
            return boroughs[canon]
    for nbhd in row.get("neighborhoods") or []:
        canon = nbhd_map.get(_norm_name(nbhd))
        if canon and canon in boroughs:
            return boroughs[canon]
    return None


# Direction words that flip a place to its opposite ("East" vs "West" Harlem, "Upper" vs
# "Lower"): a fuzzy name match that adds, drops, or swaps one is almost always wrong, so we
# block it. SequenceMatcher rates "west harlem"/"east harlem" ~0.9 -- exactly the trap.
_DIRECTION_TOKENS = {"east", "west", "north", "south", "upper", "lower", "central"}
# How close a name has to be to count as the same neighborhood ("Koreatown" -> "Korea Town").
_NBHD_FUZZY_CUTOFF = 0.86


def _direction_tokens(key: str) -> set:
    return {t for t in key.split() if t in _DIRECTION_TOKENS}


def _fuzzy_region(key: str, index: dict) -> Region | None:
    """Closest-name Region in `index`, rejecting candidates that swap a direction word."""
    for cand in get_close_matches(key, list(index), n=3, cutoff=_NBHD_FUZZY_CUTOFF):
        if _direction_tokens(key) == _direction_tokens(cand):
            return index[cand]
    return None


def match_neighborhood(name, specific: dict, broad: dict) -> Region | None:
    """The smallest neighborhood polygon a name resolves to, or None.

    Most specific first: an exact specific-neighborhood polygon, then the broad admin_0 area;
    only if neither matches exactly do we fall back to a guarded closest-string match (again
    specific before broad). So a name that is only an admin_0 ("Midtown" with no specific
    polygon of its own) lands in the dissolved broad-area boundary.
    """
    key = _norm_name(name)
    if not key:
        return None
    return (
        specific.get(key)
        or broad.get(key)
        or _fuzzy_region(key, specific)
        or _fuzzy_region(key, broad)
    )


def _resolve_neighborhood(row: dict, specific: dict, broad: dict) -> Region | None:
    """The neighborhood polygon for the entry -- the first of its neighborhoods that matches."""
    for nbhd in row.get("neighborhoods") or []:
        region = match_neighborhood(nbhd, specific, broad)
        if region is not None:
            return region
    return None


def _place_label(
    row: dict, placement: str, borough: Region | None = None, nbhd: str | None = None
) -> str | None:
    """A short, human-readable note of where this dot was actually dropped.

    Mirrors the view the dot belongs to -- a specific place, a subway line, or a borough --
    so the popup can say where we put it (and, for inferred boroughs, where that is).
    """
    if placement == "specific":
        return _first(row.get("specific_locations")) or row.get("query_raw")
    if placement.startswith("area:"):
        return placement.split(":", 1)[1]
    if placement.startswith("subway:"):
        svc = placement.split(":", 1)[1]
        base = "On the subway" if svc == "network" else f"On the {svc} train"
        return (
            f"{base} in {_boro_phrase(borough.name)}" if borough is not None else base
        )
    if placement == "neighborhood":
        return f"Somewhere in {nbhd}" if nbhd else None
    if placement == "borough":
        return (
            f"Somewhere in {_boro_phrase(borough.name)}"
            if borough is not None
            else None
        )
    return None


def base_properties(row: dict) -> dict:
    """The per-entry fields shared by every view's features (body lives in entries.json)."""
    lat, lon = row.get("lat"), row.get("lon")
    return {
        "entry_id": row["entry_id"],
        "title": row.get("title"),
        "author": row.get("author"),
        "pub_year": row.get("pub_year"),
        "web_url": row.get("web_url"),
        "borough": _join_list(row.get("boroughs")),
        "neighborhood": _join_list(row.get("neighborhoods")),
        "subway_line": _join_list(row.get("subway_lines")),
        "specific_location": _join_list(row.get("specific_locations")),
        # the geocoded specific-location point, kept even when the dot is placed elsewhere
        "spec_lat": round(lat, POINT_PRECISION) if lat is not None else None,
        "spec_lon": round(lon, POINT_PRECISION) if lon is not None else None,
        # geocode quality, for filtering in MapLibre (null when the point isn't a geocode --
        # i.e. borough/neighborhood/subway placements). `low_confidence` is the clean filter;
        # raw `confidence` isn't comparable across sources, so `geocode_source`/`category` help.
        "confidence": round(c, 6) if (c := row.get("confidence")) is not None else None,
        "low_confidence": row.get("low_confidence"),
        "geocode_source": row.get("source"),
        "geocode_category": row.get("category"),
    }


def point_feature(
    row: dict, lon: float, lat: float, placement: str, place_label: str | None = None
) -> dict:
    props = base_properties(row)
    props["placement"] = placement
    # where the dot actually landed, phrased for the popup (specific place / line / borough)
    props["place_label"] = place_label
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [round(lon, POINT_PRECISION), round(lat, POINT_PRECISION)],
        },
        "properties": props,
    }


def feature_collection(features: list) -> dict:
    return {"type": "FeatureCollection", "features": features}


def build_area_positions(
    rows: list[dict], area_places: dict, rng: random.Random
) -> dict:
    """entry_id -> (lon, lat, place_name) for entries whose geocode names a big area place.

    Entries that just wrote "Central Park" all geocode to one centroid; instead we scatter
    each across the whole polygon. Computed once and shared by every view so the same entry
    lands in the same spot when layers are toggled. Deterministic (sorted by entry_id).
    """
    positions = {}
    for row in sorted(rows, key=lambda r: r["entry_id"]):
        q = row.get("query_raw")
        if row.get("lat") is None or not q:
            continue
        region = area_places.get(str(q).strip().lower())
        if region is not None:
            lon, lat = region.random_point(rng)
            positions[row["entry_id"]] = (lon, lat, region.name)
    return positions


def geocoded_feature(row: dict, area_positions: dict) -> dict:
    """A feature at the entry's geocoded point -- scattered across an area place if it named one."""
    scattered = area_positions.get(row["entry_id"])
    if scattered is not None:
        lon, lat, name = scattered
        placement = f"area:{name}"
        return point_feature(row, lon, lat, placement, _place_label(row, placement))
    return point_feature(
        row, row["lon"], row["lat"], "specific", _place_label(row, "specific")
    )


# --------------------------------------------------------------------------------------
# the three views
# --------------------------------------------------------------------------------------


def build_locations(rows: list[dict], area_positions: dict) -> dict:
    """View 1: entries with a clean geocode, at their exact point (or scattered in an area)."""
    feats = [
        geocoded_feature(row, area_positions)
        for row in rows
        if row.get("lat") is not None and row.get("lon") is not None
    ]
    return feature_collection(feats)


def build_subway(
    rows: list[dict],
    boroughs: dict,
    nbhd_map: dict,
    lines: dict,
    network: LineGeom,
    service_set: set,
    rng: random.Random,
) -> dict:
    """View 2: subway-mentioning entries, dropped on the line they named."""
    feats = []
    for row in rows:
        if not row.get("subway_mentioned"):
            continue
        borough = _resolve_borough(row, boroughs, nbhd_map)
        matched = next(
            (
                str(s).upper()
                for s in (row.get("subway_lines") or [])
                if str(s).upper() in service_set
            ),
            None,
        )
        if matched:
            geom, placement = lines[matched], f"subway:{matched}"
        else:
            geom, placement = network, "subway:network"
        lon, lat = geom.random_point_in_borough(borough, rng)
        feats.append(
            point_feature(
                row, lon, lat, placement, _place_label(row, placement, borough)
            )
        )
        if len(feats) % 250 == 0:
            log(f"    subway: placed {len(feats)} ...")
    return feature_collection(feats)


def build_areas(
    rows: list[dict],
    boroughs: dict,
    nbhd_map: dict,
    nbhd_specific: dict,
    nbhd_broad: dict,
    area_positions: dict,
    rng: random.Random,
) -> tuple[dict, int]:
    """View 3: most specific spot available, in tiers (`placement` records which one did it):

      specific      its exact geocode (scattered across an area place if it named one)
      neighborhood  a random point inside the neighborhood polygon its name resolves to
      borough       a random point inside the borough it names, or the borough we inferred a
                    neighborhood sits in (for neighborhoods missing from neighborhoods.geojson)

    Returns (FeatureCollection, n_skipped); skipped entries named only a neighborhood that
    matched no polygon and mapped to no borough, so they have nowhere to land.
    """
    feats = []
    skipped = 0
    for i, row in enumerate(rows):
        if i and i % 2000 == 0:
            log(f"    areas: scanned {i}/{len(rows)}, placed {len(feats)} ...")
        if row.get("lat") is not None and row.get("lon") is not None:
            feats.append(geocoded_feature(row, area_positions))
            continue
        region = _resolve_neighborhood(row, nbhd_specific, nbhd_broad)
        if region is not None:
            lon, lat = region.random_point(rng)
            feats.append(
                point_feature(
                    row,
                    lon,
                    lat,
                    "neighborhood",
                    _place_label(row, "neighborhood", nbhd=region.name),
                )
            )
            continue
        borough = _resolve_borough(row, boroughs, nbhd_map)
        if borough is not None:
            lon, lat = borough.random_point(rng)
            feats.append(
                point_feature(
                    row, lon, lat, "borough", _place_label(row, "borough", borough)
                )
            )
        elif row.get("neighborhoods"):
            skipped += 1
    return feature_collection(feats), skipped


# --------------------------------------------------------------------------------------
# basemap layers
# --------------------------------------------------------------------------------------


def _geom_feature(geom, properties: dict, precision: int) -> dict:
    g = mapping(geom)
    return {
        "type": "Feature",
        "geometry": {
            "type": g["type"],
            "coordinates": _round_coords(g["coordinates"], precision),
        },
        "properties": properties,
    }


def build_basemap_boroughs() -> dict:
    gdf = gpd.read_file(BOROUGHS_GEOJSON)
    feats = [
        _geom_feature(
            row.geometry,
            {"boroname": row["boroname"], "borocode": row.get("borocode")},
            BASEMAP_PRECISION,
        )
        for _, row in gdf.iterrows()
    ]
    return feature_collection(feats)


def build_basemap_subway() -> dict:
    """One feature per service, geometries merged, trimmed + rounded."""
    gdf = gpd.read_file(SUBWAY_GEOJSON)
    merged = gdf.dissolve(by="service", aggfunc="first")
    feats = [
        _geom_feature(
            row.geometry,
            {"service": svc, "service_name": row.get("service_name", "")},
            BASEMAP_PRECISION,
        )
        for svc, row in merged.iterrows()
    ]
    return feature_collection(feats)


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------


_T0 = 0.0


def log(msg: str) -> None:
    """Progress line, prefixed with elapsed seconds since the run started."""
    print(f"[{time.perf_counter() - _T0:5.1f}s] {msg}", flush=True)


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    log(f"  wrote {path.name}  ({path.stat().st_size / 1e6:.1f} MB)")


def cmd_run(limit: int | None) -> None:
    global _T0
    _T0 = time.perf_counter()
    rng = random.Random(SEED)

    log("loading boundaries (boroughs, subway lines, area places, neighborhoods) ...")
    boroughs = load_boroughs()
    lines, network = load_subway_lines()
    service_set = set(lines.keys())
    area_places = load_area_places()
    nbhd_specific, nbhd_broad = load_neighborhoods()
    log(
        f"  {len(boroughs)} boroughs, {len(lines)} subway services, {len(area_places)} area places, "
        f"{len(nbhd_specific)} neighborhoods (+{len(nbhd_broad)} broad areas)"
    )

    log("loading diary entries (entries + entities + geocoded) ...")
    rows = load_entries()
    # learn neighborhood -> borough from the full dataset before any --limit slice
    nbhd_map = build_neighborhood_borough_map(rows)
    log(
        f"  {len(rows)} entries | {len(nbhd_map)} neighborhood->borough mappings learned"
    )
    if limit:
        rows = rows[:limit]
        log(f"  --limit {limit}: keeping first {len(rows)}")
    # scatter big-area entries (Central Park, ...) across their polygon, shared across views
    area_positions = build_area_positions(rows, area_places, rng)
    log(f"  {len(area_positions)} entries scattered across an area place")

    MAP_DIR.mkdir(parents=True, exist_ok=True)

    log("building view 1: locations ...")
    locations = build_locations(rows, area_positions)
    log("building view 2: subway (placing each on a line) ...")
    subway = build_subway(rows, boroughs, nbhd_map, lines, network, service_set, rng)
    log("building view 3: areas (most-specific placement) ...")
    areas, skipped = build_areas(
        rows, boroughs, nbhd_map, nbhd_specific, nbhd_broad, area_positions, rng
    )

    # fan out remaining dots that geocoded to the same spot (subway points sit on the line --
    # leave them; area-scattered points are already spread across their polygon)
    log("fanning out coincident dots ...")
    spread_coincident(locations["features"])
    spread_coincident(areas["features"])

    # bodies live in their own file (the map fetches on click); only ship the ones that
    # actually appear in a view -- entries placed nowhere can never be clicked
    shown = {
        f["properties"]["entry_id"]
        for fc in (locations, subway, areas)
        for f in fc["features"]
    }
    bodies = {
        r["entry_id"]: _dear_diary_body(r["body"])
        for r in rows
        if r["entry_id"] in shown and r.get("body")
    }

    log("writing output ...")
    write_json(MAP_DIR / "locations.geojson", locations)
    write_json(MAP_DIR / "subway.geojson", subway)
    write_json(MAP_DIR / "areas.geojson", areas)
    write_json(MAP_DIR / "entries.json", bodies)
    write_json(MAP_DIR / "boroughs.geojson", build_basemap_boroughs())
    write_json(MAP_DIR / "subway_lines.geojson", build_basemap_subway())

    on_network = sum(
        1
        for f in subway["features"]
        if f["properties"]["placement"] == "subway:network"
    )
    area_tiers: dict[str, int] = {}
    for f in areas["features"]:
        tier = f["properties"]["placement"]
        area_tiers[tier] = area_tiers.get(tier, 0) + 1
    scattered = sum(
        1
        for f in locations["features"]
        if f["properties"]["placement"].startswith("area:")
    )
    print(f"entries processed         : {len(rows)}")
    print(
        f"locations.geojson  (dots) : {len(locations['features'])}"
        f"  ({scattered} scattered across an area place)"
    )
    print(
        f"subway.geojson     (lines): {len(subway['features'])}"
        f"  ({on_network} on whole-network -- no matchable line named)"
    )
    print(f"areas.geojson      (most-specific): {len(areas['features'])}")
    for tier in ("specific", "neighborhood", "borough"):
        print(f"    {tier:13}: {area_tiers.get(tier, 0)}")
    print(f"  skipped (neighborhood matched no polygon and no borough): {skipped}")
    print(f"entries.json       (bodies): {len(bodies)}")
    print(f"-> {MAP_DIR}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--limit", type=int, help="only the first N entries (for a quick check)"
    )
    args = ap.parse_args()
    cmd_run(args.limit)


if __name__ == "__main__":
    main()
