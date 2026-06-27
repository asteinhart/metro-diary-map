"""Turn the hand-drawn NYC neighborhoods KML into a clean GeoJSON with admin levels.

    input/NYC NEIGHBORHOODS.kml   513 placemarks. 495 are neighborhood polygons;
                                  the other 18 are degenerate 2-point LineStrings used
                                  as section dividers ("MANHATTAN", "THE BRONX ...", "Note:") --
                                  we drop those and keep only Polygon / MultiPolygon features.

Each placemark name encodes a containment hierarchy in nested parentheses, from most
specific (the leading text) to broadest (the deepest paren):

    "Chelsea"                                  -> just one level
    "Columbia University (MORNINGSIDE HEIGHTS)" -> place inside a neighborhood
    "Murray Hill (MIDTOWN EAST (MIDTOWN))"      -> place / neighborhood / super-neighborhood

We flatten that into three columns:

    admin_2   most specific -- the leading name (always present)
    admin_1   its immediate parent neighborhood
    admin_0   the broadest containing area

Rules (per request):
  * The leading name is admin_2. Its first paren level is admin_1, the deepest is admin_0.
  * Fewer than three levels -> the missing (broader) slots repeat the broadest we have,
    so a name with no parens has admin_0 == admin_1 == admin_2.
  * "(GATED COMMUNITY)" / "(PRIVATE COMMUNITY)" parens (incl. typos) are qualifiers, not
    neighborhoods -- they're ignored when building the levels.
  * admin levels coming from the SHOUTED parenthetical names are normalized to Title Case;
    already mixed-case leading names (NoHo, DSNY, Columbia University) are left untouched.

On top of the per-neighborhood rows we add one extra row per admin_0: the geometric
union (dissolve) of every neighborhood sharing that admin_0, giving a clean broad-area
boundary. These combined rows carry is_admin_0_union=True; the neighborhood rows are
is_admin_0_union=False.

The source data is hand-entered and has some malformed/unbalanced parens; parsing is
best-effort and the per-name mapping is dumped to a CSV so it can be eyeballed.

    uv run data/6_neighborhoods.py
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely import force_2d

DATA_DIR = Path(__file__).resolve().parent
INPUT_DIR = DATA_DIR / "input"
OUT_DIR = DATA_DIR / "output"

KML = INPUT_DIR / "NYC NEIGHBORHOODS.kml"
OUT_GEOJSON = OUT_DIR / "neighborhoods.geojson"
OUT_REVIEW_CSV = OUT_DIR / "neighborhoods_admin_levels.csv"

# A parenthetical is a qualifier (not a neighborhood) when it says GATED/PRIVATE COMMUNITY.
# COMM+UNITY also catches the "COMMMUNITY" typos in the source.
_QUALIFIER_RE = re.compile(r"\b(?:GATED|PRIVATE)\b", re.I)
_COMMUNITY_RE = re.compile(r"COMM+UNITY", re.I)
_WS_RE = re.compile(r"\s+")


def is_qualifier(seg: str) -> bool:
    return bool(_QUALIFIER_RE.search(seg) and _COMMUNITY_RE.search(seg))


def smart_case(seg: str) -> str:
    """Title-case the SHOUTED parenthetical names; leave already-mixed-case names alone."""
    letters = [c for c in seg if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        return seg.title()
    return seg


def levels_from_name(raw: str) -> tuple[str, str, str]:
    """Parse a placemark name into (admin_0, admin_1, admin_2) = (broadest, middle, specific)."""
    name = _WS_RE.sub(" ", (raw or "").strip())

    # Split the name into text segments tagged by paren depth: depth 0 is the leading
    # (most specific) text, deeper parens are broader. Unbalanced parens are clamped.
    by_depth: dict[int, list[str]] = defaultdict(list)
    buf: list[str] = []
    depth = 0

    def flush() -> None:
        seg = _WS_RE.sub(" ", "".join(buf)).strip()
        if seg:
            by_depth[depth].append(seg)
        buf.clear()

    for ch in name:
        if ch == "(":
            flush()
            depth += 1
        elif ch == ")":
            flush()
            depth = max(0, depth - 1)
        else:
            buf.append(ch)
    flush()

    # Walk depths shallow -> deep to get a most-specific -> broadest chain, dropping qualifiers.
    chain: list[str] = []
    for d in sorted(by_depth):
        for seg in by_depth[d]:
            if not is_qualifier(seg):
                chain.append(smart_case(seg))

    if not chain:  # name was empty or only a qualifier
        chain = [smart_case(name) or name]

    specific = chain[0]
    immediate = chain[1] if len(chain) >= 2 else specific
    broadest = chain[-1]
    return broadest, immediate, specific  # admin_0, admin_1, admin_2


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kml", type=Path, default=KML)
    ap.add_argument("--out", type=Path, default=OUT_GEOJSON)
    ap.add_argument("--precision", type=int, default=6, help="coordinate decimal places in output")
    args = ap.parse_args()

    gdf = gpd.read_file(args.kml)

    # Keep only the actual neighborhood areas; drop the LineString section dividers / notes.
    polys = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    polys = polys[polys.geometry.notna() & ~polys.geometry.is_empty]
    polys["geometry"] = force_2d(polys.geometry)  # drop the KML altitude (z=0)

    levels = polys["Name"].apply(levels_from_name)
    polys["admin_0"] = [t[0] for t in levels]
    polys["admin_1"] = [t[1] for t in levels]
    polys["admin_2"] = [t[2] for t in levels]
    polys["name"] = polys["admin_2"]
    polys["name_raw"] = polys["Name"].str.replace(r"\s+", " ", regex=True).str.strip()

    out = polys[["name", "admin_0", "admin_1", "admin_2", "name_raw", "geometry"]].reset_index(drop=True)
    out = out.set_crs("EPSG:4326", allow_override=True)
    out["is_admin_0_union"] = False

    # One extra row per admin_0: the union of every neighborhood sharing that admin_0,
    # flagged so these broad-area boundaries can be told apart from the neighborhoods.
    combined = out.dissolve(by="admin_0", as_index=False)
    combined["name"] = combined["admin_0"]
    combined["admin_1"] = combined["admin_0"]
    combined["admin_2"] = combined["admin_0"]
    combined["name_raw"] = combined["admin_0"]
    combined["is_admin_0_union"] = True

    cols = ["name", "admin_0", "admin_1", "admin_2", "name_raw", "is_admin_0_union", "geometry"]
    out = gpd.GeoDataFrame(pd.concat([out[cols], combined[cols]], ignore_index=True), crs=out.crs)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_file(args.out, driver="GeoJSON", COORDINATE_PRECISION=args.precision)
    out.drop(columns="geometry").to_csv(OUT_REVIEW_CSV, index=False)

    # Summary so the messy hand-entered parses can be sanity-checked.
    hoods = out[~out["is_admin_0_union"]]
    dropped = len(gdf) - len(hoods)
    n_levels = hoods.apply(
        lambda r: len({r.admin_0, r.admin_1, r.admin_2}), axis=1
    )
    print(f"read {len(gdf)} placemarks; dropped {dropped} non-polygon (dividers/notes)")
    print(f"wrote {len(hoods)} neighborhood features -> {args.out}")
    print(f"  distinct admin_0 (broad areas): {hoods['admin_0'].nunique()}")
    print(f"  + {int(out['is_admin_0_union'].sum())} combined admin_0 union rows (is_admin_0_union=True)")
    print(f"  single-level / two-level / three-level: "
          f"{(n_levels == 1).sum()} / {(n_levels == 2).sum()} / {(n_levels == 3).sum()}")
    print(f"  review CSV -> {OUT_REVIEW_CSV}")
    print("\nexamples:")
    show = hoods[hoods["name_raw"].str.contains(r"\(", regex=True)].head(6)
    for _, r in show.iterrows():
        print(f"  {r.name_raw!r:60} -> a0={r.admin_0!r}  a1={r.admin_1!r}  a2={r.admin_2!r}")


if __name__ == "__main__":
    main()
