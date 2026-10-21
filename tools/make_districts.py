"""
ORACLE: one-time builder for the House district shapes on the Elections tab map.

What it does
  Downloads the congressional district boundaries used in the November 3, 2026 election, projects them
  exactly like the us-atlas map the app already draws (d3.geoAlbersUsa().scale(1300).translate([487.5, 305]),
  975 x 610), trims them to that map's state outlines (so no district spills into lakes or the ocean),
  simplifies them, and writes one small TopoJSON file per state:
      dashboard/districts/TX.json and docs/districts/TX.json   (feature ids match the race ids: TX-07, AK-AL)
  The app loads a state's file only when you open that state with the House layer on.

Where the lines come from (see SOURCES below)
  41 states kept their 2024 lines: the Census Bureau's 119th Congress cartographic boundaries (cb_2024, 1:500k).
  9 states redrew for 2026, and each uses the enacted plan from the state itself:
      TX PLANC2333 (Texas Legislative Council)            CA AB 604 / Prop. 50 (UC Berkeley Statewide Database, the state's redistricting database)
      NC S.L. 2025-95 (NC State Board of Elections)       OH plan adopted Oct 31, 2025 (Ohio OGRIP state GIS service)
      UT court-adopted Map 1 (Utah UGRC state GIS)        FL EOGPCRP2026 (Florida Legislature, Office of Economic & Demographic Research)
      TN 2026 plan (Tennessee state GIS, tnmap.tn.gov)     LA Act 2 of 2026 (Louisiana Legislature)
      AL 2023 legislative plan, in force since June 2, 2026 (Alabama Secretary of State)
  Missouri and Virginia also passed new maps, but courts kept their old lines for 2026, so they use the Census file.

How to run (needs four map libraries; keep them out of the main install with a throwaway environment)
  python -m venv .venv-maps
  .venv-maps/Scripts/pip install shapely pyshp pyproj topojson
  .venv-maps/Scripts/python tools/make_districts.py [--cache FOLDER] [--only TX,CA]
  Downloads (about 25 MB) are kept in the cache folder (default: your temp folder) so re-runs are quick.
  Re-run only if a state's map changes; then bump VERSION in docs/sw.js so phones fetch the new shapes.
"""
import argparse
import io
import json
import math
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

try:
    import shapefile  # pyshp
    import topojson
    from pyproj import CRS, Transformer
    from shapely.geometry import MultiPolygon, Point, Polygon, mapping, shape
    from shapely.ops import unary_union
    from shapely.validation import make_valid
except ImportError as e:
    sys.exit(f"Missing map library ({e.name}). See the 'How to run' notes at the top of this file.")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIRS = [ROOT / "dashboard" / "districts", ROOT / "docs" / "districts"]
ATLAS = "https://cdn.jsdelivr.net/npm/us-atlas@3.0.1/counties-albers-10m.json"
CENSUS = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_cd119_500k.zip"
ARC_QUERY = "query?where=1%3D1&outFields=*&outSR=4326&f=geojson"

# kind: 'shp' = zipped shapefile, 'arcgis' = an ArcGIS REST layer (queried as GeoJSON); field = the district-number column
SOURCES = {
    "TX": dict(kind="shp", field="District", plan="PLANC2333, enacted Aug 29, 2025",
               by="Texas Legislative Council (data.capitol.texas.gov)",
               url="https://data.capitol.texas.gov/dataset/748c952b-e926-4f44-8d01-a738884b3ec8/resource/5712ebe1-d777-4d4a-b836-0534e17bca01/download/planc2333.zip"),
    "CA": dict(kind="shp", field="DISTRICT", plan="AB 604 (Proposition 50), approved Nov 4, 2025",
               by="Statewide Database, UC Berkeley (statewidedatabase.org)",
               url="https://statewidedatabase.org/pub/data/d25/AB604%202025-08-16.zip"),
    "NC": dict(kind="shp", field="DISTRICT", plan="S.L. 2025-95, enacted Oct 22, 2025",
               by="NC State Board of Elections (dl.ncsbe.gov)",
               url="https://s3.amazonaws.com/dl.ncsbe.gov/ShapeFiles/USCongress/SL%202025-95%20-%20Shapefile.zip"),
    "OH": dict(kind="arcgis", field="district", plan="Ohio Redistricting Commission plan, adopted Oct 31, 2025",
               by="Ohio OGRIP (maps.ohio.gov)",
               url="https://maps.ohio.gov/arcgis/rest/services/Hosted/Districts_2025_11_03_SHP/FeatureServer/0/"),
    "UT": dict(kind="arcgis", field="DISTRICT", plan="Map 1, adopted by the Third District Court Nov 10, 2025",
               by="Utah UGRC (gis.utah.gov)",
               url="https://services1.arcgis.com/99lidPhWCzftIe9K/ArcGIS/rest/services/political_us_congress_districts_2026_to_2032/FeatureServer/0/"),
    "FL": dict(kind="shp", field="DISTRICT", plan="EOGPCRP2026, signed May 4, 2026",
               by="Florida Office of Economic & Demographic Research (edr.state.fl.us)",
               url="https://edr.state.fl.us/Content/redistricting/2026redistricting/EOGPCRP2026.zip"),
    "TN": dict(kind="arcgis", field="DISTRICT", plan="2026 plan, signed May 7, 2026",
               by="Tennessee state GIS (tnmap.tn.gov)",
               url="https://tnmap.tn.gov/arcgis/rest/services/ADMINISTRATIVE_BOUNDARIES/LEGISLATIVE_DISTRICTS/MapServer/2/"),
    "LA": dict(kind="shp", field="DISTRICT_I", plan="Act 2 of the 2026 Regular Session, passed May 29, 2026",
               by="Louisiana Legislature (redist.legis.la.gov)",
               url="https://redist.legis.la.gov/2026_Files/Act2Congress/Shapefile/Shapefile%20-%20Act_2_(2026_RS).zip"),
    # The Secretary of State's zip holds only the .shp (no columns), so districts follow file order; 'check' proves it.
    "AL": dict(kind="shp", field="_order", plan="2023 legislative plan, in force since June 2, 2026",
               by="Alabama Secretary of State (sos.alabama.gov)",
               url="https://www.sos.alabama.gov/sites/default/files/2026-5-14/Shape%20Files.zip",
               check={"AL-01": (-88.043, 30.695), "AL-02": (-86.300, 32.366), "AL-05": (-86.586, 34.730), "AL-07": (-87.021, 32.407)}),
}
CENSUS_NOTE = dict(plan="119th Congress lines (unchanged for 2026)", by="US Census Bureau, cb_2024 cartographic boundaries (1:500k)")
AT_LARGE = {"AK", "DE", "ND", "SD", "VT", "WY"}


# ---------- downloads ----------
def fetch(url, dest):
    """Download url to dest once (re-runs reuse the cached copy)."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ORACLE district builder)"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    dest.write_bytes(data)
    print(f"  downloaded {dest.name} ({len(data) / 1e6:.1f} MB)")
    return dest


def read_shp_zip(path, member_hint=None):
    """All records from the (congressional) shapefile inside a zip, reprojected to lon/lat."""
    z = zipfile.ZipFile(path)
    shps = [n for n in z.namelist() if n.lower().endswith(".shp") and not n.startswith("__MACOSX")]
    if member_hint:
        shps = [n for n in shps if member_hint.lower() in n.lower()] or shps
    if len(shps) != 1:
        raise SystemExit(f"{path.name}: expected one .shp, found {shps}")
    base = shps[0][:-4]
    member = lambda ext: next((n for n in z.namelist() if n.lower() == (base + ext).lower()), None)
    part = lambda ext: io.BytesIO(z.read(member(ext))) if member(ext) else None
    r = shapefile.Reader(shp=part(".shp"), shx=part(".shx"), dbf=part(".dbf"))
    prj = member(".prj")
    crs = CRS.from_wkt(z.read(prj).decode("utf-8", "replace")) if prj else CRS.from_epsg(4269)
    to_ll = None if crs.is_geographic else Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)
    # A shapefile shipped without its .dbf has no columns; its records are then numbered 1, 2, 3... in file order.
    recs = [rec.as_dict() for rec in r.records()] if member(".dbf") else [{"_order": i + 1} for i in range(len(r))]
    out = []
    for rec, shp in zip(recs, r.shapes()):
        g = shape(shp.__geo_interface__)
        if to_ll:
            g = transform_geom(g, lambda x, y: to_ll.transform(x, y))
        out.append((rec, g))
    return out


def read_arcgis(url, cache):
    dest = fetch(url + ARC_QUERY, cache)
    fc = json.loads(dest.read_text(encoding="utf-8"))
    if "features" not in fc:
        raise SystemExit(f"{url}: no features in the reply ({str(fc)[:200]})")
    return [(f["properties"], shape(f["geometry"])) for f in fc["features"]]


def transform_geom(g, fn):
    def ring(cs):
        return [fn(x, y) for x, y, *_ in cs]
    def poly(p):
        return Polygon(ring(p.exterior.coords), [ring(i.coords) for i in p.interiors])
    if g.geom_type == "Polygon":
        return poly(g)
    if g.geom_type == "MultiPolygon":
        return MultiPolygon([poly(p) for p in g.geoms])
    raise ValueError(g.geom_type)


# ---------- d3.geoAlbersUsa().scale(1300).translate([487.5, 305]), ported from d3-geo ----------
def conic_equal_area(rotate, center, parallels, k, tx, ty):
    r = math.radians
    sy0 = math.sin(r(parallels[0]))
    n = (sy0 + math.sin(r(parallels[1]))) / 2
    c = 1 + sy0 * (2 * n - sy0)
    r0 = math.sqrt(c) / n

    def raw(lam, phi):
        rho = math.sqrt(c - 2 * n * math.sin(phi)) / n
        return rho * math.sin(lam * n), r0 - rho * math.cos(lam * n)

    cx, cy = raw(r(center[0]), r(center[1]))

    def project(lon, lat):
        lam = r(lon + rotate)
        if lam > math.pi:
            lam -= 2 * math.pi
        elif lam < -math.pi:
            lam += 2 * math.pi
        x, y = raw(lam, r(lat))
        return tx + k * (x - cx), ty - k * (y - cy)
    return project


K, TX0, TY0 = 1300, 487.5, 305
LOWER48 = conic_equal_area(96, (-0.6, 38.7), (29.5, 45.5), K, TX0, TY0)
ALASKA = conic_equal_area(154, (-2, 58.5), (55, 65), K * 0.35, TX0 - 0.307 * K, TY0 + 0.201 * K)
HAWAII = conic_equal_area(157, (-3, 19.9), (8, 18), K, TX0 - 0.205 * K, TY0 + 0.212 * K)


def project_geom(g, st):
    fn = ALASKA if st == "AK" else HAWAII if st == "HI" else LOWER48
    return transform_geom(g, fn)


# ---------- the app's own state outlines (already projected), decoded from us-atlas TopoJSON ----------
def atlas_states(cache):
    topo = json.loads(fetch(ATLAS, cache / "counties-albers-10m.json").read_text(encoding="utf-8"))
    tr = topo.get("transform")
    arcs = []
    for a in topo["arcs"]:
        x = y = 0
        pts = []
        for p in a:
            if tr:
                x += p[0]; y += p[1]
                pts.append((x * tr["scale"][0] + tr["translate"][0], y * tr["scale"][1] + tr["translate"][1]))
            else:
                pts.append((p[0], p[1]))
        arcs.append(pts)

    def ring(ids):
        pts = []
        for i in ids:
            seg = arcs[i] if i >= 0 else arcs[~i][::-1]
            pts.extend(seg if not pts else seg[1:])
        return pts

    out = {}
    for g in topo["objects"]["states"]["geometries"]:
        polys = g["arcs"] if g["type"] == "MultiPolygon" else [g["arcs"]]
        geom = unary_union([make_valid(Polygon(ring(p[0]), [ring(h) for h in p[1:]])) for p in polys])
        out[g["id"]] = geom
    return out


# ---------- per-state build ----------
def district_id(st, raw):
    s = str(raw).strip()
    if s.upper() in ("AL", "00", "0", "98"):
        return f"{st}-AL"
    try:
        return f"{st}-{int(float(s)):02d}"
    except ValueError:
        raise SystemExit(f"{st}: can't read district number {raw!r}")


def polygons_only(g):
    g = make_valid(g)
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    parts = [p for p in getattr(g, "geoms", []) if p.geom_type in ("Polygon", "MultiPolygon")]
    return unary_union(parts) if parts else Polygon()


def build_state(st, items, outline, meta):
    """items: [(race_id, projected geometry)] -> TopoJSON dict, clipped to the map's state outline and simplified."""
    feats = []
    for rid, g in items:
        g = polygons_only(polygons_only(g).intersection(outline))
        g = MultiPolygon([p for p in (g.geoms if g.geom_type == "MultiPolygon" else [g]) if p.area > 1e-6]) if not g.is_empty else g
        if g.is_empty:
            print(f"  WARNING {rid}: nothing left after trimming to the state outline")
            continue
        feats.append({"type": "Feature", "id": rid, "properties": {"id": rid}, "geometry": mapping(g)})
    cover = unary_union([shape(f["geometry"]) for f in feats]).area / outline.area
    if cover < 0.97:  # the districts should fill the state; a big gap means a projection or source problem
        print(f"  WARNING {st}: districts cover only {cover:.1%} of the state outline")
    minx, miny, maxx, maxy = outline.bounds
    span = max(maxx - minx, maxy - miny)
    # About half a screen pixel when the state fills a phone screen and you then zoom in 2-3x more.
    tol = span / 2400
    topo = topojson.Topology({"type": "FeatureCollection", "features": feats}, prequantize=False, toposimplify=tol,
                             topoquantize=1e5 if span > 40 else 4e4, prevent_oversimplify=True, object_name="districts")
    t = json.loads(topo.to_json())
    for g in t["objects"]["districts"]["geometries"]:
        g["id"] = g.pop("properties", {}).get("id", g.get("id"))
    t.pop("bbox", None)
    t["meta"] = meta
    return t


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--cache", type=Path, default=Path(tempfile.gettempdir()) / "oracle-district-sources")
    ap.add_argument("--only", help="comma-separated state codes to rebuild, e.g. TX,CA")
    args = ap.parse_args()
    cache = args.cache
    only = set(args.only.upper().split(",")) if args.only else None

    elex = json.loads((ROOT / "data" / "elections.json").read_text(encoding="utf-8"))
    states = elex["meta"]["states"]  # 'TX': ['Texas', '48']
    race_ids = {k for k, r in elex["races"].items() if r["office"] == "house"}
    outlines = atlas_states(cache)

    print("Reading the Census file for unchanged states...")
    census = {}
    for rec, g in read_shp_zip(fetch(CENSUS, cache / "census" / "cb_2024_us_cd119_500k.zip")):
        st = next((a for a, v in states.items() if v[1] == rec["STATEFP"]), None)
        if st and rec["CD119FP"] != "ZZ":
            census.setdefault(st, []).append((district_id(st, rec["CD119FP"]), g))

    built, sizes, problems = {}, {}, []
    for st in sorted(states):
        if st == "DC" or (only and st not in only):
            continue
        fips = states[st][1]
        outline = outlines.get(fips)
        if outline is None:
            problems.append(f"{st}: not on the base map")
            continue
        if st in SOURCES:
            s = SOURCES[st]
            print(f"{st}: {s['plan']} ({s['by']})")
            try:
                if s["kind"] == "shp":
                    rows = read_shp_zip(fetch(s["url"], cache / st / urllib.parse.unquote(s["url"].rsplit("/", 1)[1])), s.get("hint"))
                else:
                    rows = read_arcgis(s["url"], cache / st / "layer.geojson")
                field = next((k for k in rows[0][0] if k.lower() == s["field"].lower()), None)
                if field is None:
                    raise SystemExit(f"{st}: no '{s['field']}' column; columns are {list(rows[0][0])}")
                merged = {}
                for rec, g in rows:
                    if str(rec[field]).strip() in ("", "0", "None", "ZZ"):
                        continue  # unassigned water, if any
                    merged.setdefault(district_id(st, rec[field]), []).append(g)
                items = [(rid, unary_union([make_valid(x) for x in gs])) for rid, gs in merged.items()]
                for rid, (lon, lat) in s.get("check", {}).items():  # known towns must land in the right district
                    hit = [r for r, g in items if g.contains(Point(lon, lat))]
                    if hit != [rid]:
                        raise ValueError(f"check failed: ({lon}, {lat}) should be in {rid}, found {hit}")
                meta = {"state": st, "plan": s["plan"], "source": s["by"], "redrawn": True}
            except Exception as e:  # leave the state out (the app then says its new lines aren't available)
                problems.append(f"{st}: {e}")
                print(f"  SKIPPED {st}: {e}")
                continue
        elif st in AT_LARGE:
            items = None
            meta = {"state": st, "plan": "one statewide seat", "source": "the state outline", "redrawn": False}
        else:
            items = census.get(st)
            meta = {"state": st, "plan": CENSUS_NOTE["plan"], "source": CENSUS_NOTE["by"], "redrawn": False}
        if items is None:
            items = [(f"{st}-AL", outline)]
            projected = items
        else:
            projected = [(rid, project_geom(g, st)) for rid, g in items]
        ids = sorted(r for r, _ in projected)
        want = sorted(r for r in race_ids if r.startswith(st + "-"))
        if ids != want:
            problems.append(f"{st}: district ids {ids} don't match the race list {want}")
            print(f"  SKIPPED {st}: ids {ids} vs races {want}")
            continue
        meta["built"] = date.today().isoformat()
        topo = build_state(st, projected, outline, meta)
        text = json.dumps(topo, separators=(",", ":"))
        for d in OUT_DIRS:
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{st}.json").write_text(text, encoding="utf-8", newline="\n")
        built[st] = len(ids)
        sizes[st] = len(text)
        print(f"  {st}: {len(ids)} districts, {len(text) / 1000:.0f} KB")

    total = sum(sizes.values())
    print(f"\nWrote {len(built)} states, {sum(built.values())} districts, {total / 1e6:.2f} MB per copy "
          f"(largest: {', '.join(f'{k} {v // 1000} KB' for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])[:4])}).")
    if problems:
        print("Problems (these states are left without district lines):\n  " + "\n  ".join(problems))
        sys.exit(1)


if __name__ == "__main__":
    main()
